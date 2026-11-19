"""Verify native metadata gating and restart with isolated synthetic peers."""

from dataclasses import replace
import random
import time
from types import SimpleNamespace

import libtorrent as lt
import pytest

from fqdb.bt.engine import Engine
from fqdb.bt.torrents import create_metadata, torrent_cache_path
from fqdb.catalog.magnet import MagnetCatalog, package_path
from fqdb.catalog.models import Entry
from fqdb.service.client import Client
from fqdb.storage.metadata import Store
from fqdb.util import FQDBError, safe_path, sha256
from gateway_helpers import loopback_config

DATASET = "cn/stock/base"
SCOPE = f"{DATASET}/1d"
PERIOD = "2005"


def test_discovery_setting_overrides_disabled_flags_saved_in_old_download_resume():
    params = lt.add_torrent_params()
    flags = lt.torrent_flags.disable_dht | lt.torrent_flags.disable_lsd | lt.torrent_flags.disable_pex
    params.flags |= flags
    enabled = Engine.prepare(SimpleNamespace(discovery=True), params)
    assert not enabled.flags & flags
    disabled = Engine.prepare(SimpleNamespace(discovery=False), params)
    assert disabled.flags & flags == flags


def _native_fixture(path, filename, *, size=256 * 1024):
    config = loopback_config(path)
    directory = config.root / ".state/imports"
    source = directory / filename
    source.write_bytes(random.Random(7).randbytes(size))
    identity, payload = create_metadata(source, piece_size=64 * 1024)
    metadata = directory / "synthetic-metadata.btmeta"
    metadata.write_bytes(payload)
    magnet = "magnet:?xt=urn:btih:" + identity
    entry = replace(Entry.from_magnet(DATASET, "1d", PERIOD, magnet), size=size)
    return config, entry, source, metadata


def _until(condition, *engines, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for engine in engines:
            engine.poll()
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for native test fixture")


def _connect(sender, receiver, condition, timeout=15):
    def online():
        receiver.connect([("127.0.0.1", sender.session.listen_port())])
        return condition()

    _until(online, sender, receiver, timeout=timeout)


def test_wrong_native_filename_never_unlocks_payload(data_root):
    config, entry, source, metadata = _native_fixture(data_root / "synthetic-seeder", "unrelated.parquet")
    client = loopback_config(data_root / "empty-receiver")
    sender, receiver = Engine(config.root, config.read()), Engine(client.root, client.read())
    staged = client.root / ".state/staging"
    try:
        seed = sender.add(entry.info_hash, metadata, source.parent, recheck=True, upload_only=True)
        _until(lambda: seed.status().is_seeding, sender)
        handle = receiver.add_magnet(entry.info_hash, entry.magnet, staged)
        _connect(sender, receiver, lambda: handle.status().has_metadata)
        assert handle.status().total_payload_download == 0
        with pytest.raises(FQDBError, match="Torrent metadata does not match"):
            receiver.accept_metadata(entry.info_hash, entry)
        assert handle.status().total_payload_download == 0
        assert not list(staged.rglob("*.parquet"))
        assert not torrent_cache_path(client.root, entry).exists()
    finally:
        if receiver.session is not None:
            receiver.close()
        sender.close()


def test_valid_metadata_and_partial_payload_resume_after_restart(data_root):
    config, entry, source, metadata = _native_fixture(
        data_root / "synthetic-seeder", "2005.parquet", size=2 * 1024 * 1024
    )
    client = loopback_config(data_root / "empty-receiver")
    sender = Engine(config.root, config.read() | {"upload_limit": 128 * 1024})
    receiver = Engine(client.root, client.read())
    consumer = loopback_config(data_root / "synthetic-partial-peer")
    third = Engine(consumer.root, consumer.read())
    staged = client.root / ".state/staging"
    reconnect = {"min_reconnect_time": 1, "peer_connect_timeout": 3}
    try:
        for engine in (sender, receiver, third):
            engine.session.apply_settings(reconnect)
        seed = sender.add(entry.info_hash, metadata, source.parent, recheck=True, upload_only=True)
        _until(lambda: seed.status().is_seeding, sender)
        handle = receiver.add_magnet(entry.info_hash, entry.magnet, staged)
        _connect(sender, receiver, lambda: handle.status().has_metadata)
        assert receiver.accept_metadata(entry.info_hash, entry)
        cached = torrent_cache_path(client.root, entry)
        assert cached.is_file()
        handle = receiver.handles[entry.info_hash]
        _connect(sender, receiver, lambda: handle.status().num_pieces > 0)
        assert not handle.status().is_seeding
        assert receiver.metadata_audit[entry.info_hash]["payload_before_validation"] == 0
        seed.pause()
        partial_peer = third.add_magnet(entry.info_hash, entry.magnet, consumer.root / ".state/staging")
        _connect(receiver, third, lambda: partial_peer.status().has_metadata)
        assert third.accept_metadata(entry.info_hash, entry)
        partial_peer = third.handles[entry.info_hash]
        _connect(receiver, third, lambda: partial_peer.status().num_pieces > 0)
        assert not handle.status().is_seeding
        assert partial_peer.status().total_payload_download > 0
        receiver.close()
        assert list((client.root / ".state/state").glob("*.fastresume"))

        receiver = Engine(client.root, client.read())
        receiver.session.apply_settings(reconnect)
        sender.session.apply_settings({"upload_rate_limit": 0})
        seed.resume()
        handle = receiver.add(entry.info_hash, cached, staged, magnet=entry.magnet)
        _connect(sender, receiver, lambda: handle.status().is_seeding, timeout=20)
        assert sha256(staged / source.name) == sha256(source)
    finally:
        third.close()
        if receiver.session is not None:
            receiver.close()
        sender.close()


def test_missing_metadata_peers_do_not_activate_or_write_payload(data_root):
    client = Client(data_root / "synthetic-offline-client")
    loopback_config(client.config.root).update(peer_timeout=2, max_retries=0)
    catalog = MagnetCatalog(client.config.root)
    catalog.write_publication(
        SCOPE,
        {
            "schema_version": 5,
            "name": "Synthetic offline fixture",
            "fields": ["instrument", "trading_date"],
            "instrument_column": "instrument",
            "time_column": "trading_date",
        },
        PERIOD,
        {
            "start_date": "2005-01-04",
            "end_date": "2005-01-04",
            "rows": 1,
            "instruments": 1,
            "trading_days": 1,
            "size_bytes": 1024,
            "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
        },
    )
    try:
        done = client.wait(client.call("download_package", {"catalog_path": package_path(SCOPE, PERIOD)}), 20)
        assert done["status"] == "failed" and done["tasks"][0]["reason"] == "no_metadata_peers"
        assert done["tasks"][0]["metrics"]["downloaded"] == 0
        assert not Store(client.config.root).active()
        assert not list(client.config.root.rglob("*.parquet"))
        assert not list(client.config.root.rglob("*.btmeta"))
    finally:
        if client.endpoint():
            client.call("shutdown")


def test_symlink_cannot_escape_data_root(data_root):
    root = data_root / "inside"
    root.mkdir()
    (data_root / "outside").mkdir()
    (root / "link").symlink_to(data_root / "outside", target_is_directory=True)
    with pytest.raises(FQDBError, match="escapes"):
        safe_path(root, "link/escape")
