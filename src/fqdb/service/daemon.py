"""Detached worker for Gateway leases, subscriptions, transfers and sharing."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
import threading
import time

from filelock import FileLock, Timeout
import uvicorn

from ..config import Config, DataRootUnavailable
from ..layout import runtime_path
from ..util import atomic_json
from ..web.app import create_app
from .core import Owner
from .lifetime import Lifetime


IDLE_SECONDS = 3.0
STARTUP_SECONDS = 5.0


def process_alive(pid):
    """A manually running provider ends with its controlling Python process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        try:
            # kill(pid, 0) also succeeds for a dead process awaiting its parent.
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            return state != "Z"
        except FileNotFoundError:
            return False
        except (OSError, IndexError):
            pass
    return True


def serve(root=None, port=0):
    config = Config(root).ensure()
    with config.pin():
        _serve(config, port)


def _serve(config, port):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    def storage_unavailable(error):
        # A detached/replaced mount invalidates every absolute runtime path.
        # Skip ordinary shutdown, which persists resume data to that path.
        logging.error("%s", error)
        os._exit(1)

    config.check_available()
    lock = FileLock(runtime_path(config.root, "state") / "service.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise SystemExit("A worker already owns this data directory; use fqdb status/gateway")
    owner = sock = monitor = None
    stop_monitor = threading.Event()
    endpoint_path = runtime_path(config.root, "state") / "service.json"
    try:
        owner = Owner(config)
        owner.storage_unavailable = storage_unavailable
        owner.lifetime = Lifetime(owner)
        owner.lifecycle_requests = 0
        owner.lifecycle_lock = threading.RLock()
        owner.lifecycle_stopping = False
        token = secrets.token_urlsafe(32)
        app = create_app(owner, token)

        class WorkerServer(uvicorn.Server):
            def handle_exit(self, sig, frame):
                if self.should_exit and sig == signal.SIGINT:
                    self.force_exit = True
                # Uvicorn's default re-raises captured signals before returning
                # to our finally block. Own the shutdown so native resume data,
                # endpoint metadata and the data-root lock are released first.
                owner.shutdown()

        server = WorkerServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        owner.lifecycle_exiting = lambda: server.should_exit
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        config.check_available()
        atomic_json(endpoint_path, {"url": url, "token": token, "pid": os.getpid()})
        endpoint_path.chmod(0o600)

        def shutdown():
            owner.lifecycle_stopping = True
            owner.lifetime.close_all()
            server.should_exit = True

        owner.shutdown = shutdown
        owner.start()

        def supervise():
            idle_since = time.monotonic()
            startup_until = idle_since + STARTUP_SECONDS
            while not stop_monitor.wait(0.2):
                try:
                    config.check_available()
                    with owner.lock:
                        parent_pid = owner.seeding.parent_pid
                        if parent_pid is not None and not process_alive(parent_pid):
                            # A foreground provider exit does not set the
                            # user's persistent Gateway pause preference.
                            owner.dispatch("provider_stop", {"parent_pid": parent_pid})
                    # Lease expiry changes runtime gateway authorization before
                    # considering work. Polls alone never keep a worker alive.
                    gateway = owner.lifetime.has_gateway()
                    busy = gateway or owner.has_work()
                    if busy:
                        idle_since = time.monotonic()
                    elif time.monotonic() >= max(startup_until, idle_since + IDLE_SECONDS):
                        with owner.lifecycle_lock:
                            if owner.lifecycle_requests:
                                continue
                            if owner.has_work() or owner.lifetime.has_gateway():
                                idle_since = time.monotonic()
                                continue
                            owner.lifecycle_stopping = True
                            owner.shutdown()
                            return
                except DataRootUnavailable as exc:
                    storage_unavailable(exc)
                except Exception:
                    logging.exception("Worker lifecycle check failed")
                    owner.shutdown()
                    return

        monitor = threading.Thread(target=supervise, name="fqdb-lifecycle", daemon=True)
        monitor.start()
        print(f"FreeQuantDB worker: {url} | data: {config.root}", flush=True)
        server.run(sockets=[sock])
    finally:
        stop_monitor.set()
        if monitor:
            monitor.join(timeout=2)
        try:
            config.check_available()
            if owner:
                owner.close()
        except DataRootUnavailable as exc:
            storage_unavailable(exc)
        finally:
            if sock:
                sock.close()
            try:
                config.check_available()
            except DataRootUnavailable as exc:
                storage_unavailable(exc)
            endpoint_path.unlink(missing_ok=True)
            lock.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    serve(args.data_dir, args.port)
