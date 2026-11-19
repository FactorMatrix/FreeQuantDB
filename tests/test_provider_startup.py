"""Explicit one-click sharing prepares actual, isolated synthetic local packages."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import threading
import time
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import fqdb
from fqdb.bt.torrents import create_metadata, torrent_cache_path, validate_daily
from fqdb.catalog.magnet import SCHEMA_VERSION, MagnetCatalog
from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.publish.paths import immutable_file
from fqdb.schema.market import arrow_schema
from fqdb.service.core import Owner
from fqdb.service import provision
from fqdb.service.client import Client
from fqdb.storage.metadata import Store
from gateway_helpers import loopback_config
from test_contracts import setup_store


def wait_for(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Synthetic provider transition timed out")


def test_empty_provider_is_truthful_and_never_allocates_native(data_root, monkeypatch):
    owner = Owner(Config(data_root))
    monkeypatch.setattr("fqdb.service.core.Engine", lambda *_: pytest.fail("No data must not create BT"))
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is False
        assert result["provision"]["status"] == "no_data"
        assert result["provision"]["eligible"] == 0
        assert not owner.has_work() and owner._engine is None
        assert result["transfers"] == []
    finally:
        owner.close()


def test_python_foreground_empty_root_returns_without_leaving_worker(data_root):
    fqdb.init(data_root)
    result = fqdb.provider.run()
    assert result["provider"] is False
    assert result["provision"]["status"] == "no_data"
    client = Client(data_root)
    wait_for(lambda: client.endpoint() is None and not (data_root / ".state/state/service.json").exists())


def test_missing_private_metadata_is_rebuilt_without_changing_original_identities(data_root):
    store, _, _ = setup_store(data_root)
    entries = store.entries()
    original = {e.info_hash: immutable_file(data_root, e).read_bytes() for e in entries}
    for entry in entries:
        torrent_cache_path(data_root, entry).unlink()
    with store.db() as db:
        db.execute("DELETE FROM verified_versions")
    owner = Owner(Config(data_root))
    owner.start()
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is True
        assert result["provision"]["eligible"] == 2
        assert result["provision"]["prepared"] == 2
        assert {e.info_hash for e in store.entries()} == set(original)
        for entry in entries:
            assert immutable_file(data_root, entry).read_bytes() == original[entry.info_hash]
            validate_daily(torrent_cache_path(data_root, entry).read_bytes(), entry, data_root)
        wait_for(lambda: len([t for t in owner.status()["transfers"] if t["seeding"]]) == 2)
        state = owner.status()
        assert state["provision"]["status"] == "waiting_peers"
        assert all(t["uploaded"] == 0 and t["peers"] == 0 for t in state["transfers"])
    finally:
        owner.close()


def test_fresh_verified_data_uses_cache_instead_of_repeating_full_audits(data_root, monkeypatch):
    setup_store(data_root)
    owner = Owner(Config(data_root))
    assert owner.seeding.status()["eligible"] is None
    assert owner._engine is None  # Availability is unconfirmed before an explicit local preparation.

    def forbidden(*_, **__):
        pytest.fail("Fresh verified data must not repeat full file/Parquet/native preflight audits")

    monkeypatch.setattr(provision, "sha256", forbidden)
    monkeypatch.setattr(provision, "validate_parquet", forbidden)
    monkeypatch.setattr(provision, "verify_pieces", forbidden)
    monkeypatch.setattr(owner.seeding, "verify", forbidden)
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is True and result["provision"]["eligible"] == 2
    finally:
        owner.close()


def test_changed_valid_file_is_revalidated_automatically(data_root, monkeypatch):
    store, _, _ = setup_store(data_root)
    entry = store.entries()[0]
    path = immutable_file(data_root, entry)
    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10_000_000))
    checked = []
    validate = provision.validate_parquet

    def recording(path, entry, schema):
        checked.append(entry.info_hash)
        return validate(path, entry, schema)

    monkeypatch.setattr(provision, "validate_parquet", recording)
    owner = Owner(Config(data_root))
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is True
        assert checked == [entry.info_hash]
        assert store.verified_versions()[entry.info_hash]["mtime_ns"] == path.stat().st_mtime_ns
    finally:
        owner.close()


@pytest.mark.parametrize("condition", ["missing", "corrupt"])
def test_unavailable_data_never_claims_successful_sharing(data_root, condition):
    store, _, _ = setup_store(data_root)
    for entry in store.entries():
        if condition == "missing":
            (data_root / entry.file).unlink()
            immutable_file(data_root, entry).unlink()
        else:
            immutable_file(data_root, entry).write_bytes(b"SYNTHETIC CORRUPT CONTENT")
    owner = Owner(Config(data_root))
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is False
        assert result["provision"]["eligible"] == 0
        assert result["provision"]["status"] == (
            "missing_files" if condition == "missing" else "invalid_data"
        )
        assert result["transfers"] == [] and not owner.has_work()
    finally:
        owner.close()


def raw_parquet(root, *, frequency="1d", confirmed=True):
    config = Config(root).ensure()
    store = Store(config.root)
    schema = market_schema("future" if frequency == "1m" else "stock", days=["2026-11-13"])
    schema.units_confirmed = confirmed
    store.register(schema)
    frame = market_frame(schema.asset, frequency, days=["2026-11-13"])
    frame = frame.sort_values(["instrument", "trading_date" if frequency == "1d" else "trading_time"])
    path = root / "data" / schema.id / frequency / "2026/11/20261113.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pandas(frame, schema=arrow_schema(schema, frequency), preserve_index=False),
        path,
        compression="zstd",
    )
    return store, schema, path


def describe_package(root, schema, path, *, frequency="1d", magnet=None):
    concrete = schema.for_frequency(frequency)
    instrument, time_column = concrete.primary_key
    content = pq.read_table(path, columns=[instrument])
    package = {
        "start_date": "2026-11-13",
        "end_date": "2026-11-13",
        "rows": len(content),
        "instruments": len(content[instrument].unique()),
        "trading_days": 1,
        "size_bytes": path.stat().st_size,
    }
    if magnet:
        package["magnet"] = magnet
    MagnetCatalog(root).write_publication(
        f"{schema.id}/{frequency}",
        {
            "schema_version": SCHEMA_VERSION,
            "name": schema.id,
            "fields": list(concrete.fields),
            "instrument_column": instrument,
            "time_column": time_column,
        },
        path.stem,
        package,
    )


def test_canonical_daily_bytes_with_catalog_magnet_can_join_its_swarm(data_root):
    store, schema, path = raw_parquet(data_root)
    identity, _ = create_metadata(path)
    describe_package(data_root, schema, path, magnet=f"magnet:?xt=urn:btih:{identity}")
    original = path.read_bytes()
    owner = Owner(Config(data_root))
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is True
        assert result["provision"]["eligible"] == result["provision"]["prepared"] == 1
        assert path.read_bytes() == original
        entry = store.active()[0][0]
        assert entry.info_hash == identity and entry.sha256 is None
        assert immutable_file(data_root, entry).read_bytes() == original
        validate_daily(torrent_cache_path(data_root, entry).read_bytes(), entry, data_root)
    finally:
        owner.close()


@pytest.mark.parametrize(
    "frequency,confirmed",
    [
        ("1d", False),
        ("1m", True),
    ],
)
def test_local_catalog_without_magnet_stays_local_only(data_root, frequency, confirmed):
    store, schema, path = raw_parquet(data_root, frequency=frequency, confirmed=confirmed)
    describe_package(data_root, schema, path, frequency=frequency)
    original = path.read_bytes()
    owner = Owner(Config(data_root))
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is False and result["provision"]["status"] == "no_data"
        assert MagnetCatalog(data_root).scan()["packages"][0]["magnet_status"] == "missing"
        assert store.entries() == [] and path.read_bytes() == original
    finally:
        owner.close()


def test_unknown_non_zstd_data_is_not_silently_repacked(data_root):
    store, _, path = raw_parquet(data_root)
    table = pq.read_table(path)
    pq.write_table(table, path, compression="snappy")
    original = path.read_bytes()
    owner = Owner(Config(data_root))
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is False and result["provision"]["status"] == "no_data"
        assert store.entries() == [] and path.read_bytes() == original
    finally:
        owner.close()


def test_missing_foreign_piece_parameters_never_create_a_replacement_swarm(data_root):
    source = data_root / "SYNTHETIC-source"
    source_store, schema, _ = setup_store(source)
    entry = source_store.entries()[0]
    original = immutable_file(source, entry)
    identity, _ = create_metadata(original, piece_size=64 * 1024)
    assert identity != entry.info_hash
    store = Store(loopback_config(data_root / "SYNTHETIC-receiver").root)
    store.register(schema)
    custom_entry = replace(entry, info_hash=identity, magnet="")
    store.add_entry(custom_entry)
    store.stage_entry(custom_entry, original)
    owner = Owner(Config(store.root))
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is False
        assert any(i["reason"] == "missing_torrent_metadata" for i in result["provision"]["issues"])
        assert [e.info_hash for e in store.entries()] == [identity]
        assert not torrent_cache_path(store.root, custom_entry).exists()
    finally:
        owner.close()


def test_stop_during_preparation_wins_over_late_activation(data_root, monkeypatch):
    owner = Owner(Config(data_root))
    entered, release = threading.Event(), threading.Event()

    def blocked(*_, **__):
        entered.set()
        assert release.wait(5)
        return {"status": "waiting_peers", "eligible": 1, "checked": 1, "prepared": 0, "issues": []}

    monkeypatch.setattr(provision, "prepare", blocked)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(owner.dispatch, "provider_start", {})
            assert entered.wait(5)
            stopped = owner.dispatch("provider_stop", {})
            assert stopped["provider"] is False
            release.set()
            result = future.result(timeout=5)
        assert result["provider"] is False
        assert result["provision"]["status"] == "stopped"
        assert owner._engine is None and not owner.has_work()
    finally:
        release.set()
        owner.close()


def test_paused_local_download_can_be_explicitly_shared_and_stopped_without_resuming(data_root):
    store, _, _ = setup_store(data_root)
    identity = store.enqueue(store.entries()[0])
    store.set_task(identity, status="paused")
    owner = Owner(Config(data_root))
    try:
        result = owner.dispatch("provider_start", {})
        assert result["provider"] is True and identity in owner.engine.handles
        assert store.task(identity)["status"] == "paused"
        owner.dispatch("provider_stop", {})
        assert owner.engine.handles == {} and store.task(identity)["status"] == "paused"
    finally:
        owner.close()


def test_provider_status_excludes_uploads_from_unrelated_download_handles(data_root):
    store, _, _ = setup_store(data_root)
    owner = Owner(Config(data_root))

    def handle(upload_rate, peers):
        return SimpleNamespace(
            is_valid=lambda: True,
            status=lambda: SimpleNamespace(upload_rate=upload_rate, num_peers=peers, is_seeding=True),
        )

    owner._engine = SimpleNamespace(
        handles={e.info_hash: handle(0, 0) for e in store.entries()}
        | {"SYNTHETIC-unrelated-download": handle(500, 3)},
        moves={},
        close=lambda: None,
    )
    owner.seeding.enabled = True
    try:
        result = owner.seeding.status()
        assert result["eligible"] == 2 and result["status"] == "waiting_peers"
    finally:
        owner.close()
