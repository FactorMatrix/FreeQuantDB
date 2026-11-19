"""Single-owner runtime for a data root."""

from __future__ import annotations

import logging
import threading
import time

from ..bt.engine import Engine
from ..config import Config
from ..publish.publisher import Publisher
from ..schema.base import Dataset
from ..schema.registry import install_defaults
from ..storage.metadata import Store
from ..util import FQDBError
from .actions import dispatch
from .adapters import UpstreamSources
from .catalogs import Catalogs
from .checks import Checks
from .jobs import Jobs
from .peers import PeerSettings
from .seeding import Seeding
from .subscriptions import Subscriptions
from .transfers import Transfers

log = logging.getLogger(__name__)


class Owner:
    """Coordinate the runtime components and the locks shared by SDK and HTTP."""

    def __init__(self, config: Config):
        self.config = config.ensure()
        self.store = Store(config.root)
        install_defaults(self.store)
        self.lock = threading.RLock()
        self.storage_lock = threading.RLock()
        self._engine = None
        self.gateway_enabled = False
        self.running = True
        self.closing = False
        self.last_error = None
        self.thread = threading.Thread(target=self.run, name="fqdb-owner", daemon=True)

        self.peers = PeerSettings(self)
        self.transfers = Transfers(self)
        self.seeding = Seeding(self)
        self.checks = Checks(self.store, self.lock, self.transfers.activate)
        self.publisher = Publisher(self.store, verify_seed=lambda entry: self.seeding.verify(entry))
        self.catalogs = Catalogs(self)
        self.upstream = UpstreamSources(self)
        self.public_jobs = {
            "check": lambda args, progress: self.checks.check(**args, progress=progress),
            "publish": lambda args, progress: self.publish(args, progress),
            "aggregate": lambda args, progress: self.aggregate(args, progress),
            "source_fetch": self.upstream.fetch,
            "scan": lambda args, progress: self.catalogs.scan(**args, progress=progress),
            "catalog_refresh": lambda args, progress: self.catalogs.refresh(args),
        }
        self.internal_jobs = {
            "transfer_prepare": self.transfers.prepare,
        }
        self.jobs = Jobs(self.store, self.execute_job)
        self.subscriptions = Subscriptions(self.store, self.jobs)

    @property
    def engine(self):
        """Allocate native networking only for explicitly requested BT work."""
        with self.lock:
            if self._engine is None:
                if not self.running:
                    raise FQDBError("The data worker has stopped")
                self._engine = Engine(self.config.root, self.config.read())
                self.peers.apply_priority()
            return self._engine

    def set_gateway(self, enabled):
        with self.lock:
            self.gateway_enabled = bool(enabled)

    def activate_gateway(self):
        with self.lock:
            if self.closing:
                raise FQDBError("The data worker has stopped")
            # Start the native listener even when there are no local packages.
            self.engine
            self.seeding.start_auto()

    def has_work(self):
        with self.lock:
            return self.seeding.has_work() or self.transfers.has_work() or self.jobs.has_work()

    def start(self):
        self.thread.start()

    def close(self):
        with self.lock:
            self.closing = True
            self.seeding.deactivate()
        # Active publication jobs still need native alerts while draining.
        self.jobs.close()
        self.peers.close()
        self.running = False
        if self.thread.is_alive():
            self.thread.join(timeout=30)
        with self.lock:
            if self._engine is not None:
                self._engine.close()

    def run(self):
        while self.running:
            try:
                with self.lock:
                    self.tick()
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("Service tick failed")
            time.sleep(0.1)

    def tick(self):
        if self._engine is not None:
            self._engine.poll()
        if self.closing:
            return
        self.subscriptions.schedule(
            subscriptions=self.gateway_enabled or (self.seeding.enabled and not self.seeding.automatic)
        )
        if self._engine is None and not (self.seeding.has_work() or self.transfers.has_work()):
            return
        self.transfers.tick(self.config.read())
        self.seeding.reconcile()

    def register(self, args):
        schema = Dataset(**args)
        self.store.register(schema)
        return schema.to_dict()

    def publish(self, args, progress=None):
        with self.storage_lock:
            return self.publisher.publish(**args, progress=progress)

    def aggregate(self, args, progress=None):
        with self.storage_lock:
            return self.publisher.aggregate(**args, progress=progress)

    def dispatch(self, action, args):
        return dispatch(self, action, args)

    def execute_job(self, kind, args, progress):
        handler = self.public_jobs.get(kind) or self.internal_jobs.get(kind)
        if handler is None:
            raise FQDBError("Unsupported job kind")
        return handler(args, progress)

    def submit_job(self, kind, args=None):
        if kind not in self.public_jobs:
            raise FQDBError("Unsupported service job kind")
        return self.jobs.submit(kind, args or {})

    def cancel_job(self, identity):
        self.jobs.set(identity, status="cancelled")
        return self.jobs.get(identity)

    def management_status(self):
        with self.lock:
            return {
                "nodes": self.peers.nodes(),
                "priority": self.peers.priority_status(),
                "adapters": self.upstream.source_list(),
                "catalog_checks": self.config.read()["source_checks"],
                "jobs": self.jobs.list(),
            }

    def transfer_status(self):
        with self.lock:
            result = []
            for identity, handle in self._engine.handles.items() if self._engine is not None else []:
                if handle.is_valid():
                    state = handle.status()
                    result.append(
                        {
                            "id": identity,
                            "seeding": state.is_seeding,
                            "progress": float(state.progress),
                            "peers": state.num_peers,
                            "upload_rate": state.upload_rate,
                            "download_rate": state.download_rate,
                            "uploaded": max(state.all_time_upload, state.total_payload_upload),
                        }
                    )
            return result

    def status(self):
        with self.lock:
            provision = self.seeding.status()
            return {
                "data_dir": str(self.config.root),
                "provider": self.seeding.enabled,
                "provider_parent_pid": self.seeding.parent_pid,
                "provider_issues": self.seeding.issues,
                "provision": provision,
                "listen_port": self._engine.session.listen_port()
                if self._engine is not None and self._engine.session is not None
                else None,
                "transfers": self.transfer_status(),
                "settings": self.config.read(),
                "last_error": self.last_error,
                "datasets": self.store.datasets(),
                "tasks": [self.transfers.task_state(task) for task in self.store.tasks()],
                "subscriptions": self.store.subscriptions(),
                "management": self.management_status(),
                "local": [e.to_dict() for e, _ in self.store.active()],
                "published": [e.to_dict() for e in self.store.entries()],
            }
