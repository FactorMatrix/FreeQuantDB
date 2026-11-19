"""Protect independent work when a caller replaces its implicit data root."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

from fastapi.testclient import TestClient

from fqdb.config import Config
from fqdb.service.client import Client
from fqdb.service.core import Owner
from fqdb.web.app import create_app
from test_contracts import setup_store
from test_gateway_autosync import share_ready
from test_gateway_lifecycle import shutdown, stopped, wait_for


def test_implicit_worker_drains_user_job_and_suspends_automated_checks(data_root):
    owner = Owner(Config(data_root))
    stopped = Event()
    owner.shutdown = stopped.set
    commands = ("check", "subscription_check")
    entered = {kind: Event() for kind in commands}
    complete = {kind: Event() for kind in commands}

    def execute(kind, args, progress):
        entered[kind].set()
        assert complete[kind].wait(5)
        return {}

    owner.jobs.execute = execute
    app = create_app(owner, "SYNTHETIC-worker-release")
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer SYNTHETIC-worker-release"},
    ) as client:
        try:
            owner.activate_background()
            subscription = owner.subscriptions.subscribe({"source": str(data_root / "SYNTHETIC-catalog")})
            for kind in commands:
                owner.jobs.submit(kind, {})
                assert entered[kind].wait(5)

            response = client.post("/api/action/background_release", json={})
            assert response.status_code == 200
            assert response.json() == {"status": "draining", "reason": "active_work"}
            assert owner.has_work()
            assert not stopped.is_set()
            assert owner.status()["background_draining"]
            entry = owner.store.subscription(subscription["id"])
            assert owner.subscriptions._current(entry["id"], entry["body"]["generation"]) is None
            owner.subscriptions.schedule()
            assert len(owner.jobs.list()) == len(commands)

            complete["check"].set()
            wait_for(lambda: not owner.has_work())
            assert owner.seeding.has_work()
            assert owner.jobs.has_work()
            response = client.post("/api/action/background_release", json={})
            assert response.status_code == 200
            assert response.json() == {"status": "stopping"}
            assert stopped.is_set()
            assert client.post("/api/action/background_activate", json={}).status_code == 503
        finally:
            for signal in complete.values():
                signal.set()
            owner.close()


def test_explicit_activation_cancels_pending_retirement(data_root):
    owner = Owner(Config(data_root))
    try:
        owner.activate_background()
        owner.background_draining = True
        assert not owner.has_work()
        owner.activate_background(explicit=True)
        assert owner.has_work()
        assert not owner.background_draining and owner.background_explicit
    finally:
        owner.close()


def test_detached_implicit_worker_exits_after_explicit_catalog_job_finishes(data_root):
    store, _, _ = setup_store(data_root)
    client = Client(data_root)
    received, complete = Event(), Event()

    class CatalogHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.set()
            complete.wait(15)
            self.send_error(503)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler) as server:
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            endpoint = client.activate()
            identity = store.entries()[0].info_hash
            wait_for(lambda: share_ready(client, identity))
            mirror = f"http://127.0.0.1:{server.server_port}/index.json"
            job = client.call("job_submit", {"kind": "catalog_refresh", "args": {"source": mirror}})
            assert received.wait(5)
            assert client.release_implicit() == {"status": "draining", "reason": "active_work"}
            assert client.call("job_status", {"id": job["job_id"]})["status"] == "running"
            assert client.endpoint()["pid"] == endpoint["pid"]
            assert share_ready(client, identity)

            complete.set()
            wait_for(lambda: stopped(client))
            assert client.call("job_status", {"id": job["job_id"]})["status"] == "failed"
        finally:
            complete.set()
            if client.endpoint() is not None:
                shutdown(client)
            server.shutdown()
            server_thread.join()
