"""Verify the session-wide cap with real loopback transfers of synthetic bytes."""

import random

import libtorrent as lt

from fqdb.bt.torrents import create_metadata
from fqdb.catalog.magnet import MagnetCatalog, package_path
from fqdb.service.client import Client
from gateway_helpers import gateway_session, loopback_config
from test_gateway_autosync import share_ready
from test_gateway_lifecycle import shutdown, wait_for
from test_priority_e2e import add, native_session

SCOPE = "cn/stock/base/1d"
DEFAULT_SEED_LIMIT = 500_000


def synthetic_torrent(directory, year, size):
    payload = directory / f"{year}.parquet"
    payload.write_bytes(random.Random(year).randbytes(size))
    identity, metadata = create_metadata(payload, piece_size=64 * 1024)
    return identity, lt.torrent_info(metadata)


def test_active_download_lifts_the_cap_for_old_seeds_and_restores_saved_limit(data_root):
    publisher = native_session("127.0.0.1")
    consumer = native_session("127.0.0.2")
    client = Client(loopback_config(data_root / "SYNTHETIC-switching-client").root)
    source = data_root / "SYNTHETIC-native-payloads"
    source.mkdir()
    catalog = MagnetCatalog(client.config.root)
    packages = {}
    try:
        for year, size in ((2004, 8 * 1024 * 1024), (2005, 3 * 1024 * 1024)):
            identity, info = synthetic_torrent(source, year, size)
            packages[year] = (identity, info)
            catalog.write_publication(
                SCOPE,
                {
                    "schema_version": 5,
                    "name": "Synthetic upload limit fixture",
                    "fields": ["instrument", "trading_date"],
                    "instrument_column": "instrument",
                    "time_column": "trading_date",
                },
                str(year),
                {
                    "start_date": f"{year}-01-05",
                    "end_date": f"{year}-01-05",
                    "rows": 1,
                    "instruments": 1,
                    "trading_days": 1,
                    "size_bytes": size,
                    "magnet": "magnet:?xt=urn:btih:" + identity,
                },
            )
        old_id, old_info = packages[2004]
        new_id, new_info = packages[2005]
        old_origin = add(publisher, old_info, source)
        wait_for(lambda: old_origin.status().is_seeding)
        client.call("peers_add", {"ip": "127.0.0.1", "port": publisher.listen_port()})

        def wait_for_mode(path, limit, mode):
            def observed():
                status = client.get(path)
                if status["effective_upload_limit"] == limit and status["upload_limit_mode"] == mode:
                    return status

            return wait_for(observed)

        def verify_cap(limit, *, downloading=False):
            mode = "downloading" if downloading else "seeding"
            status = wait_for_mode("/api/status", limit, mode)
            # The native cap and priority rules must describe the same worker tick.
            priority = status["management"]["priority"]
            assert priority["global_upload_limit"] == limit
            return priority

        with gateway_session(client):
            verify_cap(DEFAULT_SEED_LIMIT)
            old = client.call("download_package", {"catalog_path": package_path(SCOPE, "2004")})
            assert old["task_ids"] == [old_id]
            assert client.wait(old, timeout=30)["status"] == "complete"
            wait_for(lambda: share_ready(client, old_id))
            verify_cap(DEFAULT_SEED_LIMIT)

            client.call("priority_set", {"ip": "127.0.0.2"})
            new = client.call("download_package", {"catalog_path": package_path(SCOPE, "2005")})
            assert new["task_ids"] == [new_id]
            wait_for(
                lambda: any(
                    task["id"] == new_id and task["status"] == "waiting_metadata_peers"
                    for task in client.get("/api/status")["tasks"]
                )
            )
            verify_cap(DEFAULT_SEED_LIMIT)

            new_origin = add(publisher, new_info, source)
            new_origin.set_upload_limit(96 * 1024)
            wait_for(lambda: new_origin.status().is_seeding)
            priority = verify_cap(0, downloading=True)
            assert priority["rules"][0]["state"] == "waiting_rate_limit"
            runtime = wait_for_mode("/api/console/runtime", 0, "downloading")
            assert runtime["settings"]["upload_limit"] == DEFAULT_SEED_LIMIT

            old_consumer = add(consumer, old_info, data_root / "SYNTHETIC-old-seed-peer")
            port = client.get("/api/status")["listen_port"]
            old_consumer.connect_peer(("127.0.0.1", port))
            # This older torrent alone exceeds the former session cap, with
            # room for the native allocator's initial burst.
            wait_for(lambda: old_consumer.status().total_payload_download >= 5 * 1024 * 1024, timeout=7)
            verify_cap(0, downloading=True)

            saved = 321_000
            client.call("settings", {"upload_limit": saved})
            assert client.config.read()["upload_limit"] == saved
            verify_cap(0, downloading=True)
            client.call("task", {"id": new_id, "operation": "pause"})
            verify_cap(saved)
            client.call("task", {"id": new_id, "operation": "resume"})
            verify_cap(0, downloading=True)
            client.call("task", {"id": new_id, "operation": "cancel"})
            verify_cap(saved)

            client.call("task", {"id": new_id, "operation": "retry"})
            verify_cap(0, downloading=True)
            new_origin.set_upload_limit(0)
            assert client.wait(new, timeout=30)["status"] == "complete"
            wait_for(lambda: share_ready(client, old_id, new_id))
            verify_cap(saved)
            assert client.config.read()["upload_limit"] == saved
    finally:
        shutdown(client)
        consumer.pause()
        publisher.pause()
