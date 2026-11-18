import json
from pathlib import Path
import time

import libtorrent as lt

from fqdb.bt.engine import Engine
from fqdb.bt.torrents import (
    create_metadata,
    magnet_hash,
    metadata_torrent,
    torrent_cache_path,
    validate_daily,
)
from fqdb.catalog.magnet import MagnetCatalog, dataset_path, package_path
from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.layout import runtime_path
from fqdb.publish.catalog import export_public_catalog
from fqdb.publish.paths import immutable_file
from fqdb.publish.publisher import Publisher, import_native_publication
from fqdb.storage.metadata import Store


def _producer(root):
    config = Config(root).ensure()
    store = Store(config.root)
    days = ["2026-11-13"]
    schema = market_schema(days=days)
    store.register(schema)
    return store, schema, days


def test_native_publication_keeps_catalog_json_and_private_swarm_history(data_root):
    store, schema, days = _producer(data_root)
    publisher = Publisher(store)
    old = Entry(**publisher.publish(schema.id, "1d", market_frame(days=days))["entries"][0])
    current = Entry(**publisher.publish(schema.id, "1d", market_frame(days=days, correction=1))["entries"][0])
    scope = f"{schema.id}/1d"
    package = package_path(scope, current.period)
    index = dataset_path(scope)
    assert not (data_root / "torrents").exists()
    assert not list(data_root.rglob("*.torrent"))
    assert torrent_cache_path(data_root, old).suffix == ".btmeta"
    assert torrent_cache_path(data_root, old).is_relative_to(data_root / ".state")
    assert torrent_cache_path(data_root, old).is_file()
    assert immutable_file(data_root, old).read_bytes() != (data_root / current.file).read_bytes()

    local = MagnetCatalog(data_root).scan()
    assert not local["issues"]
    assert [item["path"] for item in local["packages"]] == [package]
    assert local["packages"][0]["info_hash"] == current.info_hash
    assert local["packages"][0]["rows"] == current.rows
    assert local["packages"][0]["file"] == current.file

    destination = data_root / "synthetic-mirror"
    export_public_catalog(store, destination)
    assert json.loads((destination / "index.json").read_text()) == {
        "schema_version": 5,
        "datasets": [index],
        "packages": [package],
    }
    assert json.loads((destination / package).read_text()) == json.loads(
        (data_root / "catalog" / package).read_text()
    )


def test_explicit_native_import_preserves_original_hash_and_tracker(data_root):
    original, schema, days = _producer(data_root / "synthetic-origin")
    entry = Entry(**Publisher(original).publish(schema.id, "1d", market_frame(days=days))["entries"][0])
    source = immutable_file(original.root, entry)
    native = validate_daily(torrent_cache_path(original.root, entry).read_bytes(), entry, original.root)
    tracker = "https://tracker.example/announce"
    legacy = metadata_torrent(native, [{"url": tracker, "tier": 0}])

    receiver, _, _ = _producer(data_root / "synthetic-converted")
    converted = Entry(**import_native_publication(receiver, entry, source, legacy))
    visible = receiver.root / converted.file
    assert visible.name == Path(entry.file).name
    assert source.read_bytes() == visible.read_bytes()
    assert not source.samefile(visible)
    assert magnet_hash(converted.magnet) == entry.info_hash
    assert "tracker.example" in converted.magnet
    assert torrent_cache_path(receiver.root, converted).read_bytes() == legacy
    assert not list(receiver.root.rglob("*.torrent"))


def test_catalog_transport_accepts_new_market_scope_without_producer_schema(data_root):
    producer, schema, days = _producer(data_root / "synthetic-origin")
    original = Entry(**Publisher(producer).publish(schema.id, "1d", market_frame(days=days))["entries"][0])
    receiver = Store(Config(data_root / "synthetic-generic-transport").ensure().root)
    descriptor = Entry.from_magnet("synthetic-market/fx/quote", "5m", original.period, original.magnet)
    descriptor = receiver.add_magnet(descriptor)
    assert descriptor.file == "data/synthetic-market/fx/quote/5m/2026/11/20261113.parquet"
    assert descriptor.timezone is None

    private = torrent_cache_path(receiver.root, descriptor)
    private.parent.mkdir(parents=True)
    private.write_bytes(torrent_cache_path(producer.root, original).read_bytes())
    source = immutable_file(producer.root, original)
    activated = receiver.activate_bt(descriptor, source, original.size)
    assert activated.sha256 is None
    assert receiver.active()[0][0].info_hash == original.info_hash
    assert (receiver.root / activated.file).read_bytes() == source.read_bytes()


def test_provider_does_not_reuse_download_resume_for_immutable_seed(data_root, monkeypatch):
    config = Config(data_root).ensure()
    source = data_root / ".state/synthetic-verified/20261113.parquet"
    source.parent.mkdir()
    source.write_bytes(b"SYNTHETIC native resume block" * (64 * 1024))
    identity, payload = create_metadata(source)
    metadata = source.with_suffix(".btmeta")
    metadata.write_bytes(payload)

    native = lt.torrent_info(payload)
    partial = lt.add_torrent_params()
    partial.ti = native
    partial.save_path = str(runtime_path(data_root, "staging") / identity)
    partial.have_pieces = [True] + [False] * (native.num_pieces() - 1)
    resume = runtime_path(data_root, "state") / f"{identity}.fastresume"
    stale = bytes(lt.write_resume_data_buf(partial))
    resume.write_bytes(stale)

    def unexpected_resume(*_):
        raise AssertionError("Provider must not load a partial download resume")

    monkeypatch.setattr(lt, "read_resume_data", unexpected_resume)
    engine = Engine(data_root, config.read())
    try:
        handle = engine.add(identity, metadata, source.parent, recheck=True, upload_only=True)
        until = time.monotonic() + 10
        while not handle.status().is_seeding and time.monotonic() < until:
            engine.poll()
            time.sleep(0.01)
        assert handle.status().is_seeding and handle.status().progress == 1.0
    finally:
        engine.close()
    assert resume.read_bytes() == stale


def test_mirror_lists_completed_archive_and_uses_smaller_packages_after_correction(data_root):
    store = Store(Config(data_root).ensure().root)
    days = ["2010-01-04", "2010-01-05"]
    schema = market_schema(days=days)
    schema.calendars["1d"] = ["2009-12-31", *days, "2010-02-01"]
    store.register(schema)
    publisher = Publisher(store)
    entries = publisher.publish(schema.id, "1d", market_frame(days=days))["entries"]
    publisher.aggregate(schema.id, "1d", "month", "201001", complete=True)
    destination = data_root / "synthetic-mirror"
    scope = f"{schema.id}/1d"
    index = destination / "index.json"
    export_public_catalog(store, destination)
    assert json.loads(index.read_text())["packages"] == [package_path(scope, "201001")]
    assert all((store.root / entry["file"]).exists() for entry in entries)

    publisher.publish(schema.id, "1d", market_frame(days=days[:1], correction=1))
    export_public_catalog(store, destination)
    assert json.loads(index.read_text())["packages"] == [package_path(scope, e["period"]) for e in entries]
