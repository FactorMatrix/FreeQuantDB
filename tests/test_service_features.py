"""Offline synthetic service integration, including persistent background work."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import threading
import time

from fastapi.testclient import TestClient

import fqdb
from fqdb import api
from fqdb.config import Config
from fqdb.publish.catalog import export_public_catalog
from fqdb.service import checks, core
from fqdb.service.client import Client
from fqdb.service.subscriptions import DEFAULT_INTERVAL, next_run
from fqdb.service.jobs import Jobs
from fqdb.web.app import create_app
from magnet_helpers import mirror
from test_contracts import setup_store
from gateway_helpers import gateway_session, loopback_config


def wait_job(client, identity):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        row = client.call("job_status", {"id": identity})
        if row["status"] in ("complete", "failed", "cancelled"):
            return row
        time.sleep(0.1)
    raise AssertionError("Synthetic service job timed out")


def shutdown(client):
    if client.endpoint():
        client.call("shutdown")
    end = time.monotonic() + 15
    while client.endpoint() and time.monotonic() < end:
        time.sleep(0.1)


def test_sdk_subscriptions_refresh_catalog_without_implicit_download(data_root):
    publisher = data_root / "SYNTHETIC-publisher"
    store, schema, _ = setup_store(publisher)
    exported = data_root / "SYNTHETIC-json-catalog"
    export_public_catalog(store, exported)
    subscriber = data_root / "SYNTHETIC-subscriber"
    loopback_config(subscriber)
    fqdb.init(subscriber)
    client = Client(subscriber)
    session = ExitStack()
    try:
        fqdb.catalog.add(exported)
        result = fqdb.subscribe(schema.id, "1d", interval=10)
        assert result["download"] is False
        assert client.endpoint() is None
        session.enter_context(gateway_session(client))
        end = time.monotonic() + 15
        while time.monotonic() < end:
            state = client.get("/api/status")
            subscription = state["subscriptions"][0]
            if subscription["body"].get("last_result"):
                break
            time.sleep(0.2)
        body = subscription["body"]
        assert body["download"] is False and body["last_result"]["package_count"] == 2
        assert body["last_result"]["source"] and body["last_error"] is None
        assert not state["published"] and state["tasks"] == [] and state["transfers"] == []
        assert list((subscriber / "data").rglob("*.parquet")) == []
        assert list((subscriber / "torrents").rglob("*.torrent")) == []
        assert Config(subscriber).read()["source_checks"][str(exported)]["status"] == "catalog_available"
        direct = api.subscribe(schema.id, "1d", interval=10, enabled=False)
        assert direct["download"] is False
        automatic = fqdb.subscribe(schema.id, "1d", interval=10, download=True, enabled=False)
        assert automatic["download"] is True
        scheduled = fqdb.subscribe(schema.id, "1d", at="15:30", timezone="Asia/Shanghai", enabled=False)
        assert scheduled["next_run"] > time.time() and scheduled["download"] is False
        session.close()
        shutdown(client)
        restored = client.get("/api/status")["subscriptions"][0]
        assert restored["body"]["timezone"] == "Asia/Shanghai"
        assert restored["body"]["interval"] == DEFAULT_INTERVAL == 3 * 3600
        assert restored["body"]["download"] is False and restored["next_run"] > time.time()
        assert client.endpoint() is None
        assert Config().root == subscriber
    finally:
        session.close()
        shutdown(client)


def test_daily_schedule_uses_requested_timezone_and_dst():
    # Identical wall-clock schedules in Shanghai and UTC represent different instants.
    timestamp = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc).timestamp()
    china = next_run({"at": "15:30", "timezone": "Asia/Shanghai"}, timestamp)
    utc = next_run({"at": "15:30", "timezone": "UTC"}, timestamp)
    assert datetime.fromtimestamp(china, timezone.utc).isoformat() == "2026-11-17T07:30:00+00:00"
    assert utc - china == 8 * 3600
    # New York's skipped 02:30 rolls forward to 03:30 on spring transition.
    before = datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc).timestamp()
    skipped = next_run({"at": "02:30", "timezone": "America/New_York"}, before)
    assert datetime.fromtimestamp(skipped, timezone.utc).isoformat() == "2026-03-08T07:30:00+00:00"


def test_async_check_and_direct_check_leave_status_available(data_root, monkeypatch):
    setup_store(data_root)
    owner = core.Owner(Config(data_root))
    entered, release = threading.Event(), threading.Event()
    original = checks.inspect_file

    def slow_inspect(*args, **kwargs):
        entered.set()
        assert release.wait(10), "Synthetic release signal missing"
        return original(*args, **kwargs)

    monkeypatch.setattr(checks, "inspect_file", slow_inspect)
    owner.start()
    client = TestClient(
        create_app(owner, "SYNTHETIC-service-test"),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer SYNTHETIC-service-test"},
    )
    try:
        response = client.post(
            "/api/action/job_submit",
            json={"kind": "check", "args": {"dataset": "cn/stock/base", "frequency": "1d", "deep": True}},
        )
        assert response.status_code == 200
        identity = response.json()["job_id"]
        assert entered.wait(5)
        begin = time.monotonic()
        state = client.get("/api/status")
        assert state.status_code == 200 and time.monotonic() - begin < 2
        assert any(j["id"] == identity for j in state.json()["management"]["jobs"])
        release.set()
        end = time.monotonic() + 10
        while owner.jobs.get(identity)["status"] not in ("complete", "failed") and time.monotonic() < end:
            time.sleep(0.1)
        job = owner.jobs.get(identity)
        assert job["status"] == "complete" and job["progress"] == 1
        assert job["result"]["summary"]["file_verified"] == 2
        entered.clear()
        release.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            check = pool.submit(
                client.post,
                "/api/action/check",
                json={"dataset": "cn/stock/base", "frequency": "1d", "deep": True},
            )
            assert entered.wait(5)
            begin = time.monotonic()
            assert client.get("/api/status").status_code == 200
            assert time.monotonic() - begin < 2
            release.set()
            assert check.result(timeout=10).status_code == 200
    finally:
        release.set()
        owner.close()


def test_persisted_interrupted_job_requires_new_explicit_request(data_root):
    store, _, _ = setup_store(data_root)
    # An interrupted job is visible after restart without being replayed.
    jobs = Jobs(store, lambda *_: {})
    with store.db() as c:
        c.execute(
            "INSERT INTO service_jobs VALUES(?,?,?,'running',0,NULL,NULL,NULL,?,?)",
            (
                "SYNTHETIC-interrupted",
                "check",
                json.dumps({"dataset": "cn/stock/base", "frequency": "1d"}),
                "2026-11-17T00:00:00Z",
                "2026-11-17T00:00:00Z",
            ),
        )
    jobs.close()
    owner = core.Owner(Config(data_root))
    try:
        interrupted = owner.jobs.get("SYNTHETIC-interrupted")
        assert interrupted["status"] == "paused" and interrupted["detail"] == "manual_resume_required"
        fqdb.init(data_root)
        waiting = fqdb.jobs.wait("SYNTHETIC-interrupted", timeout=0)
        assert waiting["status"] == "paused" and "wait_timed_out" not in waiting
        assert Client(data_root).endpoint() is None
        owner.start()
        time.sleep(0.2)
        assert owner.jobs.get("SYNTHETIC-interrupted")["status"] == "paused"
        with store.db() as db:
            assert (
                db.execute(
                    "SELECT status FROM service_jobs WHERE id=?", ("SYNTHETIC-interrupted",)
                ).fetchone()[0]
                == "running"
            )
        retried_id = owner.dispatch(
            "job_submit", {"kind": "check", "args": {"dataset": "cn/stock/base", "frequency": "1d"}}
        )["job_id"]
        assert retried_id != "SYNTHETIC-interrupted"
        end = time.monotonic() + 10
        while owner.jobs.get(retried_id)["status"] != "complete" and time.monotonic() < end:
            time.sleep(0.1)
        row = owner.jobs.get(retried_id)
        assert row["status"] == "complete" and row["result"]["summary"]["published"] == 2
        assert owner.jobs.get("SYNTHETIC-interrupted")["status"] == "paused"
    finally:
        owner.close()


def test_interrupted_check_resumes_subscription_only_after_gateway_activation(data_root, monkeypatch):
    store, schema, _ = setup_store(data_root)
    Jobs(store, lambda *_: {}).close()
    identity = "SYNTHETIC-interrupted-subscription"
    stamp = "2026-11-17T00:00:00Z"
    body = {"dataset": schema.id, "frequency": "1d", "download": False, "running_job": identity}
    subscription = store.subscribe(body)
    with store.db() as db:
        db.execute(
            "INSERT INTO service_jobs VALUES(?,?,?,'running',0,NULL,NULL,NULL,?,?)",
            (identity, "catalog_refresh", json.dumps(body), stamp, stamp),
        )
    owner = core.Owner(Config(data_root).ensure())
    owner.config.update(auto_seed=False)
    monkeypatch.setattr(owner.catalogs, "refresh", lambda args: {"packages": [], "source": args["source"]})
    try:
        owner.subscriptions.schedule()
        assert len(owner.jobs.list()) == 1
        assert owner.jobs.get(identity)["status"] == "paused"
        assert owner._engine is None
        owner.activate_gateway()
        with owner.lock:
            owner.subscriptions.schedule()
        resumed = owner.store.subscription(subscription)
        assert resumed["body"].get("enabled", True)
        assert resumed["body"]["running_job"] != identity
        assert len(owner.jobs.list()) == 2
        assert owner.jobs.get(identity)["status"] == "paused"
        assert owner.has_work()
    finally:
        owner.close()


def test_gateway_batches_due_catalog_checks_and_keeps_idle_schedule(data_root, monkeypatch):
    store, schema, _ = setup_store(data_root)
    owner = core.Owner(Config(data_root).ensure())
    owner.config.update(auto_seed=False)
    refreshed, calls = threading.Event(), []

    def refresh(args):
        calls.append(args["source"])
        refreshed.set()
        return {"packages": [], "source": args["source"]}

    monkeypatch.setattr(owner.catalogs, "refresh", refresh)
    try:
        local_catalog = str(data_root / "SYNTHETIC-catalog")
        missing_scope = owner.subscriptions.subscribe({"source": local_catalog, "download": True})
        assert missing_scope["id"] == "catalog" and not missing_scope["download"]
        owner.subscriptions.subscribe({"source": local_catalog, "dataset": schema.id})
        owner.activate_gateway()
        owner.start()
        assert refreshed.wait(5)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            rows = store.subscriptions()
            if all(row["body"].get("last_check") for row in rows):
                break
            time.sleep(0.1)
        assert len(rows) == 2 and all(row["body"].get("last_check") for row in rows)
        assert all(row["body"]["last_result"]["task_ids"] == [] for row in rows)
        assert all(row["next_run"] > time.time() + DEFAULT_INTERVAL - 10 for row in rows)
        assert calls == [local_catalog]
        assert owner._engine is None and not owner.gateway_enabled and owner.has_work()
    finally:
        owner.close()


def test_disabling_subscription_during_selection_prevents_stale_enrollment(data_root, monkeypatch):
    store, schema, _ = setup_store(data_root)
    owner = core.Owner(Config(data_root).ensure())
    owner.config.update(auto_seed=False)
    entered, release = threading.Event(), threading.Event()
    enqueued = []

    def candidates(body, result):
        entered.set()
        assert release.wait(5)
        return [], {"SYNTHETIC-package.json": "SYNTHETIC-identity"}

    monkeypatch.setattr(owner.catalogs, "refresh", lambda args: {"source": args["source"]})
    monkeypatch.setattr(owner.catalogs, "subscription_candidates", candidates)
    monkeypatch.setattr(owner.catalogs, "enqueue_subscription", lambda *_: enqueued.append(True))
    options = {"dataset": schema.id, "download": True}
    identity = owner.dispatch("subscribe", options)["id"]
    owner.activate_gateway()
    owner.start()
    try:
        assert entered.wait(5)
        job = store.subscription(identity)["body"]["running_job"]
        owner.dispatch("subscribe", options | {"enabled": False})
        assert owner.jobs.get(job)["status"] == "cancelled"
        release.set()
    finally:
        release.set()
        owner.close()
    assert not enqueued
    assert store.subscription_packages(identity) is None
    assert store.subscription(identity)["body"]["enabled"] is False


def test_public_management_facades_and_cli_use_one_owner(data_root):
    import subprocess
    import sys

    publisher = data_root / "SYNTHETIC-publisher"
    store, schema, _ = setup_store(publisher)
    root = data_root / "SYNTHETIC-manager"
    loopback_config(root)
    fqdb.init(root)
    client = Client(root)
    session = ExitStack()

    def cli(*arguments):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "fqdb",
                *arguments,
                "--data-dir",
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    try:
        session.enter_context(gateway_session(client))
        fqdb.catalog.add(mirror(publisher))
        owner_pid = client.endpoint()["pid"]
        fqdb.peers.add("127.0.0.2", 23456, enabled=False, note="SYNTHETIC disabled node")
        assert cli("peers", "list")["nodes"][0]["enabled"] is False
        fqdb.peers.enable("127.0.0.2", 23456)
        assert cli("peers", "disable", "127.0.0.2", "23456")["nodes"][0]["enabled"] is False
        fqdb.peers.remove("127.0.0.2", 23456)
        assert fqdb.peers.list()["nodes"] == []
        cli("provider", "priority", "set", "127.0.0.2", "--level", "high")
        assert fqdb.provider.priorities()["rules"][0]["level"] == "high"
        assert cli("provider", "priority", "list")["rules"][0]["ip"] == "127.0.0.2"
        fqdb.provider.enable_priority("127.0.0.2", False)
        assert not fqdb.provider.priorities()["rules"][0]["enabled"]
        fqdb.provider.remove_priority("127.0.0.2")
        scanned = fqdb.catalog.scan()
        assert len(scanned["packages"]) == 2 and all(
            p["local_status"] == "missing" for p in scanned["packages"]
        )
        fqdb.register_dataset(schema)
        fqdb.sources.configure(
            "SYNTHETIC-input", "synthetic", {"days": ["2026-11-12"], "symbols": ["000100.SZ", "600000.SH"]}
        )
        assert fqdb.sources.list()["sources"][0]["availability"]["status"] == "synthetic"
        assert cli("sources", "list")["sources"][0]["id"] == "SYNTHETIC-input"
        preview = fqdb.sources.fetch(
            "SYNTHETIC-input", schema.id, start_date="2026-11-12", end_date="2026-11-12"
        )
        assert preview["dry_run"] and not preview["network_performed"]
        assert list((root / "data").rglob("*.parquet")) == []
        job = fqdb.sources.fetch(
            "SYNTHETIC-input",
            schema.id,
            start_date="2026-11-12",
            end_date="2026-11-12",
            version=2,
            dry_run=False,
            background=True,
        )
        result = fqdb.jobs.wait(job["job_id"], timeout=20)
        assert result["status"] == "complete" and len(result["result"]["published"]) == 1
        assert cli("jobs", "status", job["job_id"])["status"] == "complete"
        assert any(j["id"] == job["job_id"] for j in fqdb.jobs.list()["jobs"])
        assert cli("jobs", "list")["jobs"]
        assert cli("catalog", "remove", str(mirror(publisher)))["sources"] == []
        assert fqdb.catalog.list_sources() == []
        assert client.endpoint()["pid"] == owner_pid
    finally:
        session.close()
        shutdown(client)
