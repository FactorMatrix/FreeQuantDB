"""Connect CLI and Python calls to the worker which owns their data directory."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qsl, urlsplit

import httpx
from filelock import FileLock, Timeout

from ..config import Config
from ..layout import runtime_path
from ..util import FQDBError
from .actions import starts_worker


class _WorkerStopping(FQDBError):
    """The server rejected this request before dispatch during idle shutdown."""


def _check_stopping(response):
    if response.status_code == 503:
        try:
            body = response.json()
        except ValueError:
            return
        if (
            isinstance(body, dict)
            and body.get("error") == "worker_stopping"
            and body.get("retryable") is True
        ):
            raise _WorkerStopping("Worker is completing idle shutdown")


class Client:
    def __init__(self, root=None):
        self.config = Config(root).ensure()

    def endpoint(self):
        self.config.check_available()
        path = runtime_path(self.config.root, "state") / "service.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            with httpx.Client(trust_env=False, timeout=1) as c:
                response = c.get(
                    value["url"] + "/api/health", headers={"Authorization": "Bearer " + value["token"]}
                )
                if response.is_success and response.json().get("data_dir") == str(self.config.root):
                    return value
        except (ValueError, KeyError, TypeError, httpx.HTTPError, OSError):
            pass
        return None

    def ensure(self, port=0):
        """Explicitly start a worker; without activated work it stops when idle.

        The dashboard requires a separate gateway lease. Ordinary RPC and local
        reads do not use this entry point.
        """
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise FQDBError("Service port must be an integer between 0 and 65535")
        endpoint = self.endpoint()
        if endpoint:
            return endpoint
        deadline = time.monotonic() + 45
        with FileLock(runtime_path(self.config.root, "state") / "startup.lock", timeout=45):
            ownership = FileLock(runtime_path(self.config.root, "state") / "service.lock")
            while time.monotonic() < deadline:
                endpoint = self.endpoint()
                if endpoint:
                    return endpoint
                try:
                    ownership.acquire(timeout=0)
                    ownership.release()
                    break
                except Timeout:
                    time.sleep(0.1)
            else:
                raise FQDBError("Existing owner has not finished stopping; inspect service.log")
            options = (
                {"start_new_session": True}
                if os.name != "nt"
                else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
            )
            with open(runtime_path(self.config.root, "logs") / "service.log", "ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "fqdb.service.daemon",
                        "--data-dir",
                        str(self.config.root),
                        "--port",
                        str(port),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=os.environ | {"FQDB_AUTOSTART": "0"},
                    **options,
                )
            while time.monotonic() < deadline and process.poll() is None:
                endpoint = self.endpoint()
                if endpoint:
                    # Reap child exits while the SDK stays alive. The worker may
                    # outlive this process only for explicitly activated work.
                    threading.Thread(target=process.wait, name="fqdb-worker-exit", daemon=True).start()
                    return endpoint
                time.sleep(0.1)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        raise FQDBError(
            f"Service failed to start; see {runtime_path(self.config.root, 'logs') / 'service.log'}"
        )

    @contextmanager
    def _local(self):
        """Serialize finite operations with the same ownership lock as workers."""
        lock = FileLock(runtime_path(self.config.root, "state") / "service.lock")
        deadline = time.monotonic() + 45
        while True:
            endpoint = self.endpoint()
            if endpoint:
                yield endpoint, None
                return
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                if time.monotonic() >= deadline:
                    raise FQDBError("Existing owner is starting or stopping; retry when it has settled")
                time.sleep(0.1)
        owner = None
        try:
            from .core import Owner

            owner = Owner(self.config)
            yield None, owner
        finally:
            try:
                if owner is not None:
                    owner.close()
            finally:
                lock.release()

    @staticmethod
    def _post(endpoint, action, args, timeout):
        with httpx.Client(trust_env=False, timeout=timeout) as c:
            response = c.post(
                endpoint["url"] + "/api/action/" + action,
                json=args,
                headers={"Authorization": "Bearer " + endpoint["token"]},
            )
        _check_stopping(response)
        if not response.is_success:
            raise FQDBError(response.text)
        return response.json()

    def call(self, action, args=None, timeout=120):
        return self._retry_unaccepted(lambda: self._call_once(action, args, timeout))

    def activate(self, *, explicit=False):
        """Start or reuse this root's sharing and subscription worker without a webpage."""
        def activate_owner():
            endpoint = self.ensure()
            self._post(endpoint, "background_activate", {"explicit": explicit}, timeout=30)
            return endpoint

        return self._retry_unaccepted(activate_owner)

    def release_implicit(self):
        """Retire an implicit worker after current operations, preserving explicit ownership."""
        endpoint = self.endpoint()
        if endpoint is not None:
            try:
                result = self._post(endpoint, "background_release", {}, timeout=30)
            except (httpx.ConnectError, _WorkerStopping):
                pass
            else:
                if result["status"] in {"kept", "draining"}:
                    return result
        self.config.check_available()
        ownership = FileLock(runtime_path(self.config.root, "state") / "service.lock", timeout=45)
        with ownership:
            return {"status": "stopped"}

    def open_gateway(self, port=0, *, persistent=False):
        def open_session():
            endpoint = self.ensure(port=port)
            return endpoint, self._post(endpoint, "gateway_open", {"persistent": persistent}, timeout=120)

        return self._retry_unaccepted(open_session)

    @staticmethod
    def _retry_unaccepted(operation):
        for attempt in range(2):
            try:
                return operation()
            except (httpx.ConnectError, _WorkerStopping) as exc:
                # Connection failure occurs before dispatch. A worker can finish
                # its idle shutdown between endpoint discovery and this POST.
                # Timeouts/read failures may follow a mutation and never retry.
                if attempt:
                    raise FQDBError(
                        "Worker stopped before accepting the request; retry the operation"
                    ) from exc
                time.sleep(0.1)

    def _call_once(self, action, args=None, timeout=120):
        args = args or {}
        endpoint = self.endpoint()
        if endpoint:
            return self._post(endpoint, action, args, timeout)
        if starts_worker(action, args):
            return self._post(self.ensure(), action, args, timeout)
        if action == "shutdown":
            return {"status": "stopped"}
        if action.startswith("gateway_"):
            return {"status": "stopped", "gateway": False}
        with self._local() as (endpoint, owner):
            if endpoint:
                return self._post(endpoint, action, args, timeout)
            return owner.dispatch(action, args)

    @staticmethod
    def _get(endpoint, path):
        with httpx.Client(trust_env=False, timeout=30) as c:
            response = c.get(endpoint["url"] + path, headers={"Authorization": "Bearer " + endpoint["token"]})
        _check_stopping(response)
        response.raise_for_status()
        return response.json()

    def get(self, path):
        for attempt in range(2):
            try:
                return self._get_once(path)
            except (httpx.ConnectError, _WorkerStopping) as exc:
                if attempt:
                    raise FQDBError("Worker stopped before accepting the read; retry the operation") from exc
                time.sleep(0.1)

    def _get_once(self, path):
        endpoint = self.endpoint()
        if endpoint:
            return self._get(endpoint, path)
        with self._local() as (endpoint, owner):
            if endpoint:
                return self._get(endpoint, path)
            route = urlsplit(path)
            if route.path == "/api/status":
                return owner.status() | {"service_running": False}
            if route.path == "/api/health":
                return {"data_dir": str(self.config.root), "status": "stopped"}
            if route.path == "/api/reports":
                return owner.store.reports()
            if route.path == "/api/logs":
                log = runtime_path(self.config.root, "logs") / "service.log"
                if not log.exists():
                    return {"text": "No service has been started"}
                with log.open("rb") as f:
                    f.seek(max(0, log.stat().st_size - 32000))
                    return {"text": f.read().decode(errors="replace")}
            if route.path.startswith("/api/console/"):
                from ..web.dashboard import Dashboard

                view = route.path.removeprefix("/api/console/")
                return Dashboard(owner).view(view, dict(parse_qsl(route.query)))
            raise FQDBError(f"Unknown local read: {route.path}")

    def wait(self, result, timeout=300):
        ids = list(result.get("task_ids", []))
        if not ids and result.get("status") in ("fallback_required", "source_unpublished"):
            return result
        deadline = time.monotonic() + timeout
        while ids and time.monotonic() < deadline:
            tasks = {t["id"]: t for t in self.get("/api/status")["tasks"]}
            selected = [tasks[i] for i in ids]
            if any(t["status"] in ("failed", "cancelled", "paused") for t in selected):
                return result | {"status": "failed", "tasks": selected}
            if all(t["status"] == "complete" for t in selected):
                return result | {
                    "status": "source_unpublished"
                    if result.get("plan", {}).get("source_unpublished")
                    else "complete",
                    "tasks": selected,
                }
            time.sleep(0.2)
        return result | ({"status": "timeout", "task_ids": ids} if ids else {})
