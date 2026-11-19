"""Exercise scheduled catalog updates with real detached workers and loopback BT peers."""

from contextlib import contextmanager
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

from fqdb.bt.torrents import torrent_cache_path
from fqdb.publish.catalog import export_public_catalog
from fqdb.publish.paths import immutable_file
from fqdb.service.client import Client
from fqdb.storage.metadata import Store
from gateway_helpers import gateway_session, loopback_config
from magnet_helpers import mirror
from test_contracts import DAYS, sample_frame, setup_store
from test_gateway_autoseed import download_magnet
from test_gateway_lifecycle import shutdown, stopped, wait_for


def cli(client, *args):
    command = [sys.executable, "-m", "fqdb", "-d", str(client.config.root), *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@contextmanager
def gateway_terminal(client, root):
    log = root / "SYNTHETIC-gateway-console.log"
    with log.open("w") as console:
        process = subprocess.Popen(
            [sys.executable, "-m", "fqdb", "gateway", "--no-browser", "-d", str(client.config.root)],
            stdout=console,
            stderr=subprocess.STDOUT,
        )
    try:
        wait_for(lambda: log.read_text().startswith("FQDB gateway: "))
        yield process, wait_for(client.endpoint)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)


def subscription(client):
    return client.get("/api/status")["subscriptions"][0]


def share_ready(client, *identities):
    status = client.get("/api/status")
    unfinished = {task["id"] for task in status["tasks"] if task["status"] != "complete"}
    seeding = {item["id"] for item in status["transfers"] if item["seeding"]}
    return set(identities) <= seeding - unfinished


@pytest.mark.skipif(os.name == "nt", reason="Closing the CLI terminal uses POSIX SIGINT")
def test_autosync_waits_without_gateway_and_seeds_original_and_revised_packages(data_root):
    publisher, schema, source = setup_store(data_root / "SYNTHETIC-publisher")
    origin = Client(publisher.root)
    receiver = Client(loopback_config(data_root / "SYNTHETIC-receiver").root)
    requested = publisher.entries(schema.id, "1d", DAYS[1], DAYS[1])[0]
    unrelated = publisher.entries(schema.id, "1d", DAYS[0], DAYS[0])[0]
    catalog = data_root / "SYNTHETIC-mirror"
    export_public_catalog(publisher, catalog, period=unrelated.period)
    receiver.call("source", {"source": str(catalog)})
    try:
        published = origin.call("provider_start")
        receiver.call("peers_add", {"ip": "127.0.0.1", "port": published["listen_port"]})
        setting = cli(
            receiver,
            "subscribe",
            schema.id,
            "--interval",
            "10",
            "--download",
            "--start-date",
            DAYS[1],
            "--end-date",
            DAYS[1],
        )
        assert setting["download"] is True
        assert receiver.endpoint() is None

        with gateway_terminal(receiver, data_root) as (terminal, endpoint):
            first = wait_for(lambda: subscription(receiver)["body"].get("last_check"))
            assert subscription(receiver)["body"]["last_result"]["task_ids"] == []
            assert receiver.get("/api/status")["transfers"] == []
            with httpx.Client(trust_env=False, timeout=3) as http:
                assert http.get(endpoint["url"] + "/").status_code == 200
                terminal.send_signal(signal.SIGINT)
                assert terminal.wait(timeout=15) == 0
                assert http.get(endpoint["url"] + "/").status_code == 403

        export_public_catalog(publisher, catalog)
        wait_for(lambda: share_ready(receiver, requested.info_hash), timeout=28)
        assert receiver.endpoint()["pid"] == endpoint["pid"]
        wait_for(lambda: subscription(receiver)["body"]["last_check"] > first)
        installed = Store(receiver.config.root)
        assert [task["id"] for task in installed.tasks()] == [requested.info_hash]
        assert installed.task(requested.info_hash)["body"]["entry"]["magnet"] == requested.magnet
        assert installed.task(requested.info_hash)["body"]["origin"] == "subscription"
        old_file = immutable_file(receiver.config.root, requested)
        old_meta = torrent_cache_path(receiver.config.root, requested)
        old_content, old_metadata = old_file.read_bytes(), old_meta.read_bytes()

        sample_frame(days=[DAYS[1]], correction=2).to_csv(source, index=False)
        revised = origin.call("publish", {"dataset": schema.id, "frequency": "1d", "input_path": str(source)})
        revised_hash = revised["entries"][0]["info_hash"]
        assert revised_hash != requested.info_hash
        export_public_catalog(publisher, catalog)
        wait_for(lambda: share_ready(receiver, requested.info_hash, revised_hash), timeout=28)
        assert old_file.read_bytes() == old_content and old_meta.read_bytes() == old_metadata

        unavailable = catalog.with_name("SYNTHETIC-unavailable-mirror")
        catalog.rename(unavailable)
        wait_for(lambda: subscription(receiver)["body"].get("last_error"), timeout=22)
        assert share_ready(receiver, requested.info_hash, revised_hash)
        shutdown(origin)
        assert (
            download_magnet(
                data_root / "SYNTHETIC-third-peer",
                requested.magnet,
                receiver.get("/api/status")["listen_port"],
                Path(requested.file).name,
            )
            == old_content
        )

        last_check = subscription(receiver)["body"]["last_check"]
        attempts = {task["id"]: task["attempts"] for task in installed.tasks()}
        unavailable.rename(catalog)
        wait_for(lambda: subscription(receiver)["body"]["last_check"] > last_check, timeout=22)
        saved = subscription(receiver)
        assert saved["body"]["last_error"] is None and saved["body"]["last_result"]["task_ids"] == []
        assert {task["id"]: task["attempts"] for task in installed.tasks()} == attempts

        cli(receiver, "service", "stop")
        wait_for(lambda: stopped(receiver))
        assert subscription(receiver)["body"]["last_check"] == saved["body"]["last_check"]
        assert receiver.endpoint() is None
        with gateway_session(receiver):
            wait_for(
                lambda: subscription(receiver)["body"]["last_check"] > saved["body"]["last_check"], timeout=8
            )
            wait_for(lambda: share_ready(receiver, revised_hash))
            assert receiver.endpoint()["pid"] != endpoint["pid"]
            assert subscription(receiver)["body"]["download"] is True
            assert {task["id"]: task["attempts"] for task in installed.tasks()} == attempts
    finally:
        for client in (receiver, origin):
            if client.endpoint():
                shutdown(client)


def test_catalog_only_subscription_uses_default_interval_and_never_downloads_without_scope(data_root):
    publisher, _, _ = setup_store(data_root / "SYNTHETIC-publisher")
    receiver = Client(loopback_config(data_root / "SYNTHETIC-receiver").root)
    receiver.call("source", {"source": str(mirror(publisher.root))})
    try:
        assert cli(receiver, "subscribe", "--download")["download"] is False
        assert receiver.endpoint() is None
        with gateway_session(receiver):
            wait_for(lambda: subscription(receiver)["body"].get("last_check"))
        saved = subscription(receiver)
        assert saved["body"]["interval"] == 10800 and saved["body"]["download"] is False
        assert 10790 <= saved["next_run"] - time.time() <= 10800
        assert saved["body"]["last_result"]["package_count"] == 2
        assert receiver.get("/api/status")["tasks"] == []
    finally:
        if receiver.endpoint():
            shutdown(receiver)
