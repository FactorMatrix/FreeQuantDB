"""Real loopback transfers of the current market format; no public peers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

import numpy as np
import pytest

from fqdb.bt.torrents import create, make_magnet, torrent_cache_path
from fqdb.catalog.models import Entry
from fqdb.publish.catalog import write_publication
from fqdb.schema.digest import logical_digest
from fqdb.publish.paths import immutable_file
from fqdb.service.client import Client
from fqdb.storage.metadata import Store
from fqdb.storage.query import get_price
from fqdb.util import atomic_bytes, sha256
from gateway_helpers import loopback_config
from magnet_helpers import mirror
from test_contracts import DAYS, STOCK, sample_frame, sample_schema


def until(fn, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for real service/BT state")


@pytest.fixture
def nodes(data_root):
    clients = [Client(data_root / name) for name in ("A", "B", "C")]
    try:
        for c in clients:
            loopback_config(c.config.root)
            c.config.update(peer_timeout=8, max_retries=0)
            c.ensure()
        yield clients
    finally:
        for c in clients:
            if c.endpoint():
                c.call("shutdown")
        for c in clients:
            until(lambda: c.endpoint() is None, 20)


def prepare_publisher(client, data=None):
    client.call("register", sample_schema().to_dict())
    path = client.config.root / ".state/imports/SYNTHETIC.csv"
    (sample_frame() if data is None else data).to_csv(path, index=False)
    result = client.call("publish", {"dataset": "cn/stock/base", "frequency": "1d", "input_path": str(path)})
    client.call("provider_start", {"datasets": ["cn/stock/base/1d"]})
    until(lambda: len(client.get("/api/status")["transfers"]) == len(result["entries"]))
    return result


def connect(client, peer):
    port = peer.get("/api/status")["listen_port"]
    return client.call("peers_add", {"ip": "127.0.0.1", "port": port})


def test_real_transfer_community_fallback_and_version_repair(nodes):
    a, b, c = nodes
    published = prepare_publisher(a)
    source = str(mirror(a.config.root))
    for receiver in (b, c):
        receiver.call("source", {"source": source})
    connect(b, a)
    scope = {"dataset": "cn/stock/base", "frequency": "1d"}
    day = b.call("plan", scope | {"start_date": DAYS[0], "end_date": DAYS[0]})
    for period in ({"year": 2026, "month": 11}, {"year": 2026}):
        missing = b.call("plan", scope | period | {"granularity": "month" if period.get("month") else "year"})
        assert missing["fallback_required"] and missing["items"] == []
        selected = b.call("plan", scope | period | {"allow_daily_fallback": True})
        assert selected["daily_fallback_selected"]
        assert selected["items"][0]["entry"]["info_hash"] == day["items"][0]["entry"]["info_hash"]
    synced = b.wait(b.call("sync", scope | {"year": 2026, "month": 11, "allow_daily_fallback": True}), 40)
    assert synced["status"] == "complete"
    assert all(t["status"] == "complete" and t["metrics"]["downloaded"] > 0 for t in synced["tasks"])
    until(lambda: not b.get("/api/status")["transfers"])
    assert not b.get("/api/status")["provider"]
    repeat = b.call("sync", scope | {"start_date": DAYS[0], "end_date": DAYS[1]})
    assert not repeat["task_ids"] and repeat["status"] == "complete"
    assert len(b.get("/api/status")["tasks"]) == 2
    b.call("provider_start", {"datasets": ["cn/stock/base/1d"], "upload_limit": 1024 * 1024})
    until(lambda: len(b.get("/api/status")["transfers"]) == 2)
    a.call("shutdown")
    until(lambda: a.endpoint() is None)
    connect(c, b)
    received = c.wait(c.call("sync", scope | {"start_date": DAYS[0], "end_date": DAYS[1]}), 40)
    assert received["status"] == "complete" and all(t["metrics"]["downloaded"] > 0 for t in received["tasks"])
    for e in published["entries"]:
        assert sha256(c.config.root / e["file"]) == e["sha256"]
    missing = published["entries"][0]
    (c.config.root / missing["file"]).unlink()
    report = c.call("check", scope | {"start_date": DAYS[0], "end_date": DAYS[2], "deep": True})
    preview = c.call("repair", {"report": report, "dry_run": True})
    assert len(preview["items"]) == 1
    assert c.wait(c.call("repair", {"report": report, "dry_run": False}), 40)["status"] == "complete"
    path = c.config.root / missing["file"]
    with path.open("r+b") as stream:
        stream.seek(32)
        stream.write(b"broken synthetic bytes")
    damaged = c.call("check", scope | {"deep": True})
    assert damaged["files"][0]["file"] == "damaged"
    assert c.wait(c.call("repair", {"report": damaged, "dry_run": False}), 40)["status"] == "complete"
    assert sha256(path) == missing["sha256"]
    a.ensure()
    correction = a.config.root / ".state/imports/correction.csv"
    sample_frame(days=[DAYS[0]], correction=2).to_csv(correction, index=False)
    old = Entry(**missing)
    old_bytes = immutable_file(a.config.root, old).read_bytes()
    revision = a.call("publish", scope | {"input_path": str(correction)})["entries"][0]
    # Restarting/publishing no longer re-enables the previous provider session.
    a.call("provider_start", {"datasets": ["cn/stock/base/1d"]})
    assert revision["info_hash"] != old.info_hash and revision["file"] == old.file
    assert immutable_file(a.config.root, old).read_bytes() == old_bytes
    c.call("source", {"source": str(mirror(a.config.root))})
    revised_entry = Entry(**revision)
    atomic_bytes(
        torrent_cache_path(c.config.root, revised_entry),
        torrent_cache_path(a.config.root, revised_entry).read_bytes(),
        immutable=True,
    )
    c.call("settings", {"peer_nodes": [], "peer_timeout": 2})
    failed = c.wait(c.call("sync", scope | {"start_date": DAYS[0], "end_date": DAYS[0]}), 20)
    assert failed["status"] == "failed" and "no_available_peers" in failed["tasks"][0]["reason"]
    assert Store(c.config.root).active()[0][0].version == 1
    c.call("settings", {"peer_timeout": 8})
    connect(c, a)
    assert (
        c.wait(c.call("sync", scope | {"start_date": DAYS[0], "end_date": DAYS[0]}), 40)["status"]
        == "complete"
    )
    assert Store(c.config.root).active()[0][0].version == 2
    assert get_price(Store(c.config.root), STOCK[0], DAYS[0], DAYS[0], fields=["close"]).iloc[0, 0] == 12.5
    b.call("provider_stop")
    until(lambda: not b.get("/api/status")["transfers"])


def large_daily_frame():
    rng = np.random.default_rng(731)
    row = sample_frame(days=[DAYS[0]]).iloc[:1]
    # The compact Parquet layout still needs several BT pieces for resume tests.
    data = row.loc[row.index.repeat(16_000)].reset_index(drop=True)
    data["instrument"] = [f"{100000 + i:06d}.SZ" for i in range(len(data))]
    data["open"] = rng.uniform(10, 100, len(data))
    data["high"] = data.open + 1
    data["low"] = data.open - 1
    data["close"] = data.open + 0.5
    data["amount"] = rng.uniform(1, 100000, len(data))
    return data


def test_resume_after_process_shutdown_and_pause_cancel(nodes):
    a, b, _ = nodes
    prepare_publisher(a, large_daily_frame())
    a.call("settings", {"upload_limit": 128 * 1024})
    b.call("source", {"source": str(mirror(a.config.root))})
    connect(b, a)
    scope = {"dataset": "cn/stock/base", "frequency": "1d", "start_date": DAYS[0], "end_date": DAYS[0]}
    result = b.call("sync", scope)
    identity = result["task_ids"][0]
    until(
        lambda: any(t["id"] == identity and 0 < t["progress"] < 1 for t in b.get("/api/status")["tasks"]), 40
    )
    b.call("task", {"id": identity, "operation": "pause"})
    assert b.get("/api/status")["tasks"][0]["status"] == "paused"
    b.call("task", {"id": identity, "operation": "resume"})
    until(lambda: (b.config.root / f".state/state/{identity}.fastresume").exists(), 15)
    import os
    import signal

    os.kill(b.endpoint()["pid"], signal.SIGKILL)
    until(lambda: b.endpoint() is None)
    assert list((b.config.root / ".state/staging" / identity).glob("*.parquet"))
    a.call("settings", {"upload_limit": 0})
    b.call("task", {"id": identity, "operation": "resume"})
    assert b.wait({"task_ids": [identity]}, 50)["status"] == "complete"
    assert len(b.get("/api/status")["tasks"]) == 1 and len(Store(b.config.root).active()) == 1
    b.call("task", {"id": identity, "operation": "cancel"})
    assert Store(b.config.root).active()[0][0].info_hash == identity


def test_trusted_bytes_can_still_contain_invalid_market_records(nodes):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from fqdb.schema.market import arrow_schema

    a, b, _ = nodes
    good = Entry(**prepare_publisher(a)["entries"][0])
    data = sample_frame(days=[DAYS[0]])
    data.loc[0, "high"] = -1
    directory = a.config.root / ".state/staging/SYNTHETIC-invalid-release"
    directory.mkdir()
    path = directory / Path(good.file).name
    pq.write_table(
        pa.Table.from_pandas(data, schema=arrow_schema(sample_schema(), "1d"), preserve_index=False),
        path,
        compression="zstd",
    )
    torrent = directory / path.with_suffix(".torrent").name
    identity = create(path, torrent)
    entry = replace(
        good,
        version=2,
        size=path.stat().st_size,
        sha256=sha256(path),
        info_hash=identity,
        magnet="",
        day_groups={DAYS[0]: [0]},
        daily_sources={DAYS[0]: logical_digest(data, sample_schema().for_frequency("1d"))},
    )
    entry = replace(entry, magnet=make_magnet(entry))
    store = Store(a.config.root)
    store.stage_entry(entry, path)
    atomic_bytes(torrent_cache_path(a.config.root, entry), torrent.read_bytes(), immutable=True)
    store.add_entry(entry)
    store.activate(entry)
    write_publication(store, entry)
    until(lambda: any(t["id"] == identity and t["seeding"] for t in a.get("/api/status")["transfers"]))
    b.call("source", {"source": str(mirror(a.config.root))})
    connect(b, a)
    result = b.wait(
        b.call(
            "sync",
            {"dataset": "cn/stock/base", "frequency": "1d", "start_date": DAYS[0], "end_date": DAYS[0]},
        ),
        30,
    )
    assert result["status"] == "complete"
    assert result["tasks"][0]["metrics"]["bt_pieces_verified"]
    assert Store(b.config.root).active()[0][0].info_hash == identity
    report = b.call(
        "check",
        {
            "dataset": "cn/stock/base",
            "frequency": "1d",
            "start_date": DAYS[0],
            "end_date": DAYS[0],
            "deep": True,
        },
    )
    assert report["files"][0]["bt_pieces"] == "verified"
    assert report["files"][0]["quality"] == "invalid"
    assert any("OHLC" in reason for reason in report["files"][0]["reason"])
    repair = b.call("repair", {"report": report, "dry_run": True})
    assert repair["items"] == [] and repair["unrepairable_source_content"]


def test_public_catalog_download_activates_native_bytes_without_a_second_scan(nodes, data_root, monkeypatch):
    from fqdb import util
    from fqdb.bt import torrents
    from fqdb.publish.catalog import export_public_catalog
    from fqdb.publish import paths
    from fqdb.service.core import Owner
    from fqdb.validation import files

    producer, _, _ = nodes
    entry = Entry(**prepare_publisher(producer)["entries"][0])
    source = data_root / "SYNTHETIC-PUBLIC-V5"
    export_public_catalog(Store(producer.config.root), source, period=entry.period)
    consumer = Owner(loopback_config(data_root / "v4-receiver"))
    consumer.catalogs.add(str(source))
    consumer.peers.add("127.0.0.1", producer.get("/api/status")["listen_port"])

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("Ordinary download must only use libtorrent for payload integrity")

    consumer.start()
    try:
        with monkeypatch.context() as guard:
            for module in (paths, util, files):
                guard.setattr(module, "sha256", unexpected_scan)
            guard.setattr(torrents, "verify_pieces", unexpected_scan)
            guard.setattr(files, "verify_pieces", unexpected_scan)
            guard.setattr(files, "validate_parquet", unexpected_scan)
            planned = consumer.catalogs.plan(
                {
                    "dataset": entry.dataset,
                    "frequency": entry.frequency,
                    "start_date": entry.trading_date,
                    "end_date": entry.trading_date,
                },
                download=True,
            )
            assert planned["task_ids"] == [entry.info_hash] and planned["plan"]["bytes"] == entry.size

            def settled():
                task = consumer.store.task(entry.info_hash)
                return task if task["status"] in ("complete", "failed") else None

            task = until(settled)
        assert task["status"] == "complete", task["reason"]
        assert task["metrics"]["bt_pieces_verified"] and task["metrics"]["metadata_validated"]
        assert task["metrics"]["payload_before_validation"] == 0 and task["metrics"]["downloaded"] > 0
        active, evidence = consumer.store.active()[0]
        assert active.sha256 is None and active.size == entry.size and not active.days
        assert evidence["verification"] == "bt" and sha256(consumer.store.root / active.file) == entry.sha256
        checked = consumer.checks.check(
            entry.dataset, entry.frequency, entry.trading_date, entry.trading_date, deep=True
        )
        (result,) = checked["files"]
        assert result["bt_pieces"] == "verified" and result["sha256"] == "not_provided"
        assert result["quality"] == "pending" and result["trading_days"] == [entry.trading_date]
        assert checked["summary"]["anomalies"] == 0
        consumer.seeding.start({"datasets": [f"{entry.dataset}/{entry.frequency}"]})
        until(lambda: any(t["seeding"] for t in consumer.transfer_status()))
        assert consumer.status()["provider"]
        assert not consumer.seeding.stop({})["provider"]
    finally:
        consumer.close()
