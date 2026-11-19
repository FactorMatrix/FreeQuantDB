"""Synthetic loopback swarms, including a client with no FQDB metadata cache."""

import os
from pathlib import Path
import time

import libtorrent as lt
import pytest

from fqdb.bt.torrents import torrent_cache_path
from fqdb.layout import runtime_path
from fqdb.publish.paths import immutable_file
from fqdb.service.client import Client
from fqdb.service.core import Owner
from fqdb.storage.metadata import Store
from gateway_helpers import gateway_session, loopback_config
from magnet_helpers import mirror
from test_contracts import setup_store


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Synthetic BT transition did not finish")


def download_magnet(root, magnet, port, filename):
    """Connect an ordinary native client with only the advertised magnet."""
    session = lt.session(
        {
            "listen_interfaces": "127.0.0.1:0",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "allow_multiple_connections_per_ip": True,
        }
    )
    params = lt.parse_magnet_uri(magnet)
    params.save_path = str(root)
    params.flags &= ~(lt.torrent_flags.paused | lt.torrent_flags.auto_managed)
    handle = session.add_torrent(params)
    try:
        handle.connect_peer(("127.0.0.1", port))
        wait_for(lambda: handle.status().is_seeding)
        assert handle.status().total_payload_download > 0
        return (root / filename).read_bytes()
    finally:
        session.remove_torrent(handle)
        session.pause()


def test_gateway_download_seeds_original_magnet_after_exit_and_restart(data_root):
    publisher = loopback_config(data_root / "SYNTHETIC-publisher")
    store, _, _ = setup_store(publisher.root)
    entry = store.entries()[0]
    filename = Path(entry.file).name
    original = (publisher.root / entry.file).read_bytes()
    receiver = loopback_config(data_root / "SYNTHETIC-receiver")
    a, b = Client(publisher.root), Client(receiver.root)
    try:
        origin = a.call("provider_start")
        b.call("source", {"source": str(mirror(publisher.root))})
        b.call("peers_add", {"ip": "127.0.0.1", "port": origin["listen_port"]})
        with gateway_session(b):
            running = b.endpoint()
            wait_for(lambda: b.get("/api/status")["provision"]["status"] == "no_data")
            result = b.wait(
                b.call(
                    "sync",
                    {
                        "dataset": entry.dataset,
                        "frequency": entry.frequency,
                        "start_date": entry.trading_date,
                        "end_date": entry.trading_date,
                    },
                ),
                30,
            )
            assert result["status"] == "complete", result
            assert result["tasks"][0]["id"] == entry.info_hash

        a.call("shutdown")
        wait_for(lambda: a.endpoint() is None)
        final = immutable_file(receiver.root, entry)
        resume = runtime_path(receiver.root, "state") / f"{entry.info_hash}.fastresume"
        staging = runtime_path(receiver.root, "staging") / entry.info_hash / filename
        wait_for(lambda: resume.is_file() and not staging.exists())
        assert final.samefile(receiver.root / entry.file)
        assert Path(lt.read_resume_data(resume.read_bytes()).save_path) == final.parent
        cached = lt.torrent_info(torrent_cache_path(receiver.root, entry).read_bytes())
        assert str(cached.info_hashes().v1) == entry.info_hash
        assert bytes(cached.info_section()) == bytes(
            lt.torrent_info(torrent_cache_path(publisher.root, entry).read_bytes()).info_section()
        )

        assert b.endpoint()["pid"] == running["pid"]
        with gateway_session(b):
            assert b.endpoint()["pid"] == running["pid"]
            assert b.get("/api/status")["provision"]["eligible"] == 1
        assert download_magnet(
            data_root / "SYNTHETIC-third-node", entry.magnet, b.get("/api/status")["listen_port"], filename
        ) == original

        b.call("shutdown")
        wait_for(lambda: b.endpoint() is None)
        with gateway_session(b):
            assert b.endpoint()["pid"] != running["pid"]
            status = wait_for(
                lambda: next(
                    (s for s in b.get("/api/status")["transfers"] if s["id"] == entry.info_hash and s["seeding"]),
                    None,
                )
            )
            assert status["progress"] == 1
            assert b.get("/api/status")["provider"]
        assert download_magnet(
            data_root / "SYNTHETIC-third-after-restart",
            entry.magnet,
            b.get("/api/status")["listen_port"],
            filename,
        ) == original
        recorded, _ = Store(receiver.root).active()[0]
        assert recorded.info_hash == entry.info_hash
    finally:
        for client in (a, b):
            if client.endpoint():
                client.call("shutdown")
            wait_for(lambda: client.endpoint() is None)


def test_gateway_seeds_verified_files_without_resuming_an_interrupted_task(data_root):
    store, _, _ = setup_store(data_root)
    entry = store.entries()[0]
    identity = store.enqueue(entry)
    store.set_task(identity, status="checking")
    owner = Owner(loopback_config(data_root))
    owner.start()
    try:
        owner.activate_gateway()
        wait_for(lambda: identity in {row["id"] for row in owner.transfer_status()})
        runtime = owner.status()
        assert identity in {row["id"] for row in runtime["transfers"]}
        assert runtime["tasks"][0]["status"] == "paused"
        assert store.task(identity)["status"] == "checking"
        paused = owner.dispatch("provider_stop", {})
        assert paused["transfers"] == []
        assert paused["provision"]["eligible"] == len(store.active())
    finally:
        owner.close()


@pytest.mark.parametrize("damage", ["missing", "changed", "missing_metadata", "invalid_metadata"])
def test_gateway_reports_unusable_seed_and_never_overwrites_local_data(data_root, damage):
    store, _, _ = setup_store(data_root)
    entry = store.entries()[0]
    config = loopback_config(data_root)
    metadata = torrent_cache_path(data_root, entry)
    visible = data_root / entry.file
    if damage == "missing":
        visible.unlink()
        expected = "published_but_local_missing"
    elif damage == "changed":
        stat = visible.stat()
        visible.unlink()
        visible.write_bytes(b"S" * stat.st_size)
        os.utime(visible, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        expected = "local_file_changed_or_unverified"
    elif damage == "missing_metadata":
        metadata.unlink()
        expected = "missing_torrent_metadata"
    else:
        metadata.write_bytes(b"SYNTHETIC damaged metadata")
        expected = "data_invalid"
    original = visible.read_bytes() if visible.exists() else None
    owner = Owner(config)
    owner.start()
    try:
        owner.activate_gateway()
        wait_for(
            lambda: any(
                issue["entry"]["info_hash"] == entry.info_hash and issue["reason"] == expected
                for issue in owner.status()["provision"]["issues"]
            )
        )
        status = owner.status()
        assert all(row["id"] != entry.info_hash for row in status["transfers"])
        assert (visible.read_bytes() if visible.exists() else None) == original
    finally:
        owner.close()
