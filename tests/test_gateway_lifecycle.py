"""Synthetic lifecycle checks: work runs only after an explicit activation."""

import os
import signal
import subprocess
import sys
import time

from filelock import FileLock, Timeout
import httpx
import pytest

import fqdb
from fqdb.catalog.models import Entry
from fqdb.catalog.magnet import MagnetCatalog
from fqdb.config import Config
from fqdb.service import core
from fqdb.service.client import Client
from fqdb.service.jobs import Jobs
from fqdb.service.lifetime import Lifetime
from fqdb.storage.metadata import Store
from gateway_helpers import gateway_session
from magnet_helpers import mirror
from test_contracts import setup_store


def wait_for(predicate, *, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Synthetic lifecycle transition did not finish")


def stopped(client):
    if client.endpoint() is not None:
        return False
    try:
        with FileLock(client.config.root / ".state/state/service.lock", timeout=0):
            return not (client.config.root / ".state/state/service.json").exists()
    except Timeout:
        return False


def shutdown(client):
    if client.endpoint() is not None:
        client.call("shutdown")
    wait_for(lambda: stopped(client))


def test_passive_sdk_and_finite_configuration_do_not_start_native_or_service(data_root, monkeypatch):
    fqdb.init(data_root)

    def forbidden(*args, **kwargs):
        raise AssertionError("Passive management must not start BT or a background process")

    monkeypatch.setattr(core, "Engine", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    client = Client(data_root)
    assert len(fqdb.datasets()) == 3
    assert fqdb.tasks() == []
    assert fqdb.peers.list()["nodes"] == []
    assert fqdb.sources.list()["sources"] == []
    assert fqdb.jobs.list()["jobs"] == []
    assert fqdb.provider.status()["service_running"] is False
    assert fqdb.provider.priorities()["effective"] is False
    fqdb.peers.add("127.0.0.2", 23456, enabled=False, note="SYNTHETIC dormant node")
    nodes = fqdb.peers.list()["nodes"]
    assert nodes[0]["enabled"] is False
    assert client.config.read()["peer_nodes"] == nodes
    with pytest.raises(ValueError, match="Unknown settings.*peers"):
        client.call("settings", {"peers": [["127.0.0.2", 23456]]})
    client.call("settings", {"max_active": 2})
    assert Config(data_root).read()["max_active"] == 2
    assert client.get("/api/status")["provider"] is False
    assert "text" in client.get("/api/logs")
    assert client.endpoint() is None
    assert not (data_root / ".state/state/service.json").exists()


def test_persisted_provider_setting_does_not_activate_new_owner(data_root, monkeypatch):
    config = Config(data_root).ensure()
    config.update(provider=True, upload_limit=12345, provider_scopes=["cn/stock/base/1d"])

    def forbidden(*args, **kwargs):
        raise AssertionError("A stored provider preference must not start BT")

    monkeypatch.setattr(core, "Engine", forbidden)
    owner = core.Owner(config)
    try:
        owner.start()
        time.sleep(0.2)
        assert owner.status()["provider"] is False
        assert not owner.has_work()
        assert config.read()["upload_limit"] == 12345
        assert config.read()["provider_scopes"] == ["cn/stock/base/1d"]
    finally:
        owner.close()


def test_browser_lease_expires_without_the_terminal_extending_it(data_root, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("fqdb.service.lifetime.time.monotonic", lambda: clock[0])
    owner = core.Owner(Config(data_root))
    try:
        lifetime = Lifetime(owner)
        session_id = lifetime.open()["session_id"]
        assert owner.gateway_enabled
        assert lifetime.heartbeat(session_id, browser=True)["browser_seen"]
        clock[0] += lifetime.ttl - 1
        assert lifetime.heartbeat(session_id, browser=False)["active"]
        clock[0] += 2
        assert lifetime.heartbeat(session_id, browser=False)["active"] is False
        assert not owner.gateway_enabled
        assert not owner.has_work()
    finally:
        owner.close()


def test_worker_exits_when_no_download_or_provider_is_active(data_root):
    fqdb.init(data_root)
    client = Client(data_root)
    try:
        endpoint = client.ensure()
        with httpx.Client(trust_env=False, timeout=3) as http:
            response = http.get(endpoint["url"] + "/")
        assert response.status_code in (403, 404, 503)
        wait_for(lambda: stopped(client))
        assert fqdb.provider.status()["service_running"] is False
    finally:
        if client.endpoint():
            shutdown(client)


def test_explicit_provider_keeps_worker_alive_until_stopped(data_root):
    setup_store(data_root)
    fqdb.init(data_root)
    client = Client(data_root)
    try:
        result = fqdb.provider.start(upload_limit=12345)
        assert result["provider"] is True
        endpoint = client.endpoint()
        assert endpoint is not None
        time.sleep(6)
        assert client.endpoint()["pid"] == endpoint["pid"]
        assert fqdb.provider.status()["provider"] is True
        fqdb.provider.stop()
        wait_for(lambda: stopped(client))
        assert fqdb.provider.status()["service_running"] is False
    finally:
        if client.endpoint():
            shutdown(client)


def test_closing_gateway_keeps_only_explicit_provider_alive(data_root):
    setup_store(data_root)
    fqdb.init(data_root)
    client = Client(data_root)
    try:
        with gateway_session(client):
            endpoint = client.endpoint()
            with httpx.Client(trust_env=False, timeout=3) as http:
                assert http.get(endpoint["url"] + "/").status_code == 200
            fqdb.provider.start()
        time.sleep(6)
        assert client.endpoint()["pid"] == endpoint["pid"]
        with httpx.Client(trust_env=False, timeout=3) as http:
            assert http.get(endpoint["url"] + "/").status_code in (403, 404, 503)
        fqdb.provider.stop()
        wait_for(lambda: stopped(client))
    finally:
        if client.endpoint():
            shutdown(client)


def test_explicit_download_survives_gateway_exit_and_stops_when_paused(data_root):
    publisher = data_root / "SYNTHETIC-publisher"
    _, schema, _ = setup_store(publisher)
    receiver = data_root / "SYNTHETIC-receiver"
    fqdb.init(receiver)
    fqdb.catalog.add(mirror(publisher))
    assert callable(fqdb.sync)
    client = Client(receiver)
    try:
        with gateway_session(client):
            result = fqdb.download(schema.id, date="2026-11-12", granularity="day", background=True)
            assert len(result["task_ids"]) == 1
            identity = result["task_ids"][0]
            endpoint = client.endpoint()
        # The synthetic publisher is deliberately offline. Waiting for peers is
        # genuine, user-activated download work and must retain its worker.
        time.sleep(6)
        assert client.endpoint()["pid"] == endpoint["pid"]
        task = next(row for row in fqdb.tasks() if row["id"] == identity)
        assert task["status"] in (
            "waiting_peers",
            "downloading",
            "fetching_metadata",
            "waiting_metadata_peers",
        )
        assert task["progress"] < 1
        client.call("task", {"id": identity, "operation": "pause"})
        wait_for(lambda: stopped(client))
        assert fqdb.tasks()[0]["status"] == "paused"
        assert client.endpoint() is None
    finally:
        if client.endpoint():
            shutdown(client)


def test_new_download_does_not_activate_other_interrupted_work(data_root):
    publisher = data_root / "SYNTHETIC-publisher"
    _, schema, _ = setup_store(publisher)
    config = Config(data_root / "SYNTHETIC-receiver").ensure()
    store = Store(config.root)
    catalog = MagnetCatalog(config.root)
    catalog.sync(mirror(publisher))
    old = next(p for p in catalog.scan()["packages"] if p["start_date"] == "2026-11-12")
    old_entry = store.add_magnet(Entry.from_magnet(schema.id, "1d", old["period"], old["magnet"]))
    old_identity = store.enqueue(old_entry)
    Jobs(store, lambda *_: {}).close()
    stamp = "2026-11-17T00:00:00Z"
    with store.db() as db:
        db.execute(
            "INSERT INTO service_jobs VALUES(?,?,?,'running',0,NULL,NULL,NULL,?,?)",
            ("SYNTHETIC-unrelated-job", "check", "{}", stamp, stamp),
        )
    owner = core.Owner(config)
    try:
        result = owner.dispatch(
            "sync",
            {"dataset": schema.id, "frequency": "1d", "start_date": "2026-11-13", "end_date": "2026-11-13"},
        )
        assert len(result["task_ids"]) == 1
        identity = result["task_ids"][0]
        assert identity != old_identity
        owner.start()
        wait_for(
            lambda: (
                owner.store.task(identity)["status"]
                in ("waiting_peers", "downloading", "fetching_metadata", "waiting_metadata_peers")
            )
        )
        assert owner.store.task(old_identity)["status"] == "queued"
        assert old_identity not in owner.engine.handles
        displayed = next(task for task in owner.status()["tasks"] if task["id"] == old_identity)
        assert displayed["status"] == "paused" and displayed["reason"] == "manual_resume_required"
        interrupted = owner.jobs.get("SYNTHETIC-unrelated-job")
        assert interrupted["status"] == "paused" and interrupted["detail"] == "manual_resume_required"
        with store.db() as db:
            assert (
                db.execute(
                    "SELECT status FROM service_jobs WHERE id=?", ("SYNTHETIC-unrelated-job",)
                ).fetchone()[0]
                == "running"
            )
    finally:
        owner.close()


def test_completed_native_download_releases_receiver_worker(data_root):
    publisher = data_root / "SYNTHETIC-publisher"
    _, schema, _ = setup_store(publisher)
    source = Client(publisher)
    receiver = data_root / "SYNTHETIC-receiver"
    client = Client(receiver)
    fqdb.init(receiver)
    try:
        state = source.call("provider_start", {"datasets": [schema.id + "/1d"]})
        assert state["listen_port"] > 0
        fqdb.catalog.add(mirror(publisher))
        fqdb.peers.add("127.0.0.1", state["listen_port"])
        result = fqdb.download(schema.id, date="2026-11-12", granularity="day", timeout=20)
        assert result["status"] == "complete"
        assert result["tasks"][0]["status"] == "complete"
        assert result["tasks"][0]["progress"] == 1
        wait_for(lambda: stopped(client))
        frame = fqdb.load(schema.id, start="2026-11-12", end="2026-11-12", fields=["close"])
        assert len(frame) == 2 and frame.attrs["synthetic"] is None
        assert client.endpoint() is None
    finally:
        if client.endpoint():
            shutdown(client)
        if source.endpoint():
            shutdown(source)


@pytest.mark.skipif(os.name == "nt", reason="Ctrl+C delivery uses POSIX SIGINT")
def test_gateway_cli_foreground_exit_releases_service(data_root):
    client = Client(data_root)
    process = subprocess.Popen(
        [sys.executable, "-m", "fqdb", "gateway", "--no-browser", "--data-dir", str(data_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        endpoint = wait_for(client.endpoint)
        with httpx.Client(trust_env=False, timeout=3) as http:
            wait_for(lambda: http.get(endpoint["url"] + "/").status_code == 200)
        assert process.poll() is None
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stdout + stderr
        wait_for(lambda: stopped(client))
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)
        if client.endpoint():
            shutdown(client)


@pytest.mark.skipif(os.name == "nt", reason="Ctrl+C delivery uses POSIX SIGINT")
@pytest.mark.parametrize("exit_signal", [signal.SIGINT, signal.SIGTERM])
def test_provider_python_run_stops_with_its_script(data_root, exit_signal):
    setup_store(data_root)
    client = Client(data_root)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fqdb,sys; fqdb.provider.run(data_dir=sys.argv[1], upload_limit=12345)",
            str(data_root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for(lambda: client.endpoint() and client.get("/api/status")["provider"])
        assert process.poll() is None
        process.send_signal(exit_signal)
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == (0 if exit_signal == signal.SIGINT else -signal.SIGTERM), stdout + stderr
        wait_for(lambda: stopped(client))
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)
        if client.endpoint():
            shutdown(client)
