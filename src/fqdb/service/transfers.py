"""Durable download authorization, native handles and transfer jobs."""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
import time

import libtorrent as lt

from ..bt.torrents import torrent_cache_path, validate_daily, validate_info
from ..catalog.models import Entry
from ..layout import runtime_path
from ..publish.paths import resolve_file
from ..util import FQDBError

log = logging.getLogger(__name__)

ACTIVE_STATES = (
    "downloading",
    "checking",
    "waiting_peers",
    "fetching_metadata",
    "waiting_metadata_peers",
    "preparing",
)
WORK_STATES = (*ACTIVE_STATES, "queued", "retry_wait")


class Transfers:
    """Drive only downloads authorized by this data root's current owner."""

    def __init__(self, owner):
        self.owner, self.store, self.config, self.lock = owner, owner.store, owner.config, owner.lock
        self._authorized = set()
        self._receiving = set()
        self.started = {}
        self.preparation_jobs = {}
        self.last_peer_connect = 0

    def activate(self, task_ids, *, queued=False):
        """Authorize downloads selected directly or through a saved subscription."""
        with self.lock:
            selected = set(task_ids)
            if not queued:
                for identity in selected - self._authorized:
                    task = self.store.task(identity)
                    if task and task["status"] in ACTIVE_STATES:
                        self.store.set_task(identity, status="queued", reason="explicit_restart_resume")
            self._authorized.update(selected)

    def task_state(self, task):
        """Project interrupted durable work without changing it during a read."""
        engine = self.owner._engine
        identity = task["id"]
        handle = engine.handles.get(identity) if engine is not None else None
        if handle is None or not handle.is_valid():
            task = task | {
                "metrics": task.get("metrics", {})
                | {"peers": 0, "download_rate": 0, "upload_rate": 0, "seeding": False}
            }
        elif task["status"] == "complete":
            metrics = self._metrics(identity, handle.status())
            metrics["downloaded"] = max(metrics["downloaded"], task["metrics"].get("downloaded", 0))
            task = task | {"metrics": task["metrics"] | metrics}
        if identity not in self._authorized and task["status"] in WORK_STATES:
            return task | {
                "status": "paused",
                "reason": "manual_resume_required",
            }
        return task

    def has_work(self):
        return bool(self._authorized) and any(
            task["id"] in self._authorized and task["status"] in WORK_STATES for task in self.store.tasks()
        )

    def is_active(self, identity):
        if identity not in self._authorized:
            return False
        task = self.store.task(identity)
        return task is not None and task["status"] in WORK_STATES

    @property
    def receiving(self):
        return bool(self._receiving)

    def begin(self, task):
        identity, body = task["id"], task["body"]
        root = self.config.root
        job_id = self.preparation_jobs.get(identity)
        if job_id is None:
            job = self.owner.jobs.submit("transfer_prepare", body)
            self.preparation_jobs[identity] = job["job_id"]
            self.store.set_task(identity, status="preparing", reason=None)
            return
        job = self.owner.jobs.get(job_id)
        if job["status"] in ("queued", "running"):
            return
        if job["status"] != "complete":
            raise FQDBError(job["error"] or "Transfer preparation cancelled")
        self.preparation_jobs.pop(identity, None)
        self.owner.engine.remove(identity)
        save = runtime_path(root, "staging") / identity
        entry = Entry(**body["entry"])
        cached = torrent_cache_path(root, entry)
        if not cached.is_file():
            if not entry.magnet:
                raise FQDBError("Package has no magnet link")
            self.owner.engine.add_magnet(identity, entry.magnet, save)
        else:
            self.owner.engine.add(identity, cached, save, magnet=entry.magnet)
        self.started[identity] = time.monotonic()
        self.store.set_task(
            identity,
            status="checking" if cached.is_file() else "fetching_metadata",
            reason=None,
            attempts=task["attempts"] + 1,
        )

    def finish_file(self, task, handle, status):
        entry = Entry(**task["body"]["entry"])
        info = validate_info(handle.torrent_file(), entry, self.config.root)
        if Path(status.save_path) != runtime_path(self.config.root, "staging") / task["id"]:
            raise FQDBError("Completed torrent is outside its download staging directory")
        source = Path(status.save_path) / Path(entry.file).name
        entry = self.store.activate_bt(entry, source, info.files().file_size(0))
        identity = task["id"]
        self.owner.seeding.storage_changed(identity)
        metrics = self.store.task(identity)["metrics"] | {"bt_pieces_verified": True}
        self.store.set_task(identity, status="complete", reason=None, progress=1, metrics=metrics)
        self.started.pop(identity, None)
        self._authorized.discard(identity)
        if self.owner.seeding.wants_seed(entry):
            try:
                self.owner.engine.promote(
                    identity, source, resolve_file(self.config.root, entry, immutable=True)
                )
                return
            except Exception:
                # The complete local data can be restored from its final path.
                log.exception("Could not transition completed torrent %s to its installed path", identity)
        self.owner.engine.remove(identity)
        try:
            source.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove completed staging file for %s", identity, exc_info=True)

    def fail(self, task, reason, retry):
        cfg = self.config.read()
        self.owner.engine.remove(task["id"])
        self.preparation_jobs.pop(task["id"], None)
        current = self.store.task(task["id"])
        can_retry = retry and current["attempts"] <= cfg["max_retries"]
        if not can_retry:
            self._authorized.discard(task["id"])
            self.started.pop(task["id"], None)
        self.store.set_task(
            task["id"],
            status="retry_wait" if can_retry else "failed",
            reason=reason,
            next_retry=time.time() + cfg["retry_interval"],
        )
        log.warning("Task %s: %s", task["id"], reason)

    def tick(self, cfg):
        self._start_ready(cfg)
        self._receiving = {
            identity
            for identity, handle in list(self.owner.engine.handles.items())
            if self._update_handle(identity, handle, cfg)
        }
        self.owner.peers.apply_upload_limit(cfg["upload_limit"])
        if time.monotonic() - self.last_peer_connect >= 1:
            self.owner.engine.connect(
                [(node["ip"], node["port"]) for node in cfg["peer_nodes"] if node["enabled"]]
            )
            self.last_peer_connect = time.monotonic()

    def _start_ready(self, cfg):
        tasks = (
            {t["id"]: t for t in self.store.tasks() if t["id"] in self._authorized}
            if self._authorized
            else {}
        )
        active = sum(t["status"] in ACTIVE_STATES for t in tasks.values())
        for identity, task in tasks.items():
            if task["status"] == "retry_wait" and time.time() >= task["next_retry"]:
                self.store.set_task(identity, status="queued")
                task["status"] = "queued"
            if task["status"] == "preparing" or (task["status"] == "queued" and active < cfg["max_active"]):
                try:
                    self.begin(task)
                    if task["status"] == "queued":
                        active += 1
                except Exception as exc:
                    self.fail(task, str(exc), retry=False)

    def _metrics(self, identity, s):
        return {
            "peers": s.num_peers,
            "download_rate": s.download_rate,
            "upload_rate": s.upload_rate,
            "uploaded": max(s.all_time_upload, s.total_payload_upload),
            "downloaded": max(s.all_time_download, s.total_payload_download),
            "seeding": s.is_seeding,
            "save_path": s.save_path,
        } | self.owner.engine.metadata_audit.get(identity, {})

    def _update_handle(self, identity, handle, cfg):
        engine = self.owner.engine
        if not handle.is_valid() or identity in self.owner.seeding.verifying_seeds:
            return
        if identity in engine.uploads:
            reason = engine.errors.pop(identity, None)
            if reason:
                if identity in engine.moves:
                    self.owner.seeding.storage_changed(identity)
                else:
                    self.owner.seeding.native_error(identity, reason)
                engine.remove(identity)
            return
        task = self.store.task(identity)
        reason = engine.errors.pop(identity, None)
        if reason:
            if task:
                self.fail(task, reason, retry=False)
            else:
                engine.remove(identity)
            return
        if not task or task["status"] not in ACTIVE_STATES:
            return
        s = handle.status()
        metrics = self._metrics(identity, s)
        self.store.set_task(identity, progress=float(s.progress), metrics=metrics)
        if identity in self.owner.engine.metadata_pending:
            try:
                accepted = self.owner.engine.accept_metadata(identity, Entry(**task["body"]["entry"]))
            except Exception as exc:
                self.fail(task, f"metadata_invalid: {exc}", retry=False)
                return
            if accepted:
                self.started[identity] = time.monotonic()
                self.store.set_task(
                    identity,
                    status="checking",
                    reason=None,
                    metrics=metrics | self.owner.engine.metadata_audit[identity],
                )
            elif time.monotonic() - self.started.get(identity, time.monotonic()) > cfg["peer_timeout"]:
                self.fail(task, "no_metadata_peers" if s.num_peers == 0 else "metadata_timeout", retry=True)
            else:
                self.store.set_task(
                    identity,
                    status="fetching_metadata" if s.num_peers else "waiting_metadata_peers",
                    reason=None if s.num_peers else "no_metadata_peers",
                    progress=0,
                )
            # Zero-priority files can look complete to libtorrent before
            # acceptance. Never pass this phase into completion handling.
            return
        if s.is_seeding:
            try:
                self.finish_file(task, handle, s)
            except Exception as exc:
                self.fail(task, f"validation_or_storage_failed: {exc}", retry=False)
        elif time.monotonic() - self.started.get(identity, time.monotonic()) > cfg["peer_timeout"]:
            self.fail(task, "no_available_peers_or_transfer_stalled", retry=True)
        else:
            old_progress = task["progress"]
            if s.progress > old_progress or s.download_rate > 0:
                self.started[identity] = time.monotonic()
            state = (
                "checking"
                if s.state in (lt.torrent_status.checking_files, lt.torrent_status.checking_resume_data)
                else ("waiting_peers" if s.num_peers == 0 else "downloading")
            )
            self.store.set_task(
                identity, status=state, reason="no_available_peers" if state == "waiting_peers" else None
            )
            return (
                identity in self._authorized
                and s.state == lt.torrent_status.downloading
                and s.num_peers > 0
                and (
                    s.download_payload_rate > 0
                    or s.total_payload_download > task["metrics"].get("downloaded", 0)
                )
            )

    def control(self, identity, operation):
        if not self.store.task(identity):
            raise FQDBError("Unknown task")
        engine = self.owner._engine
        if operation in ("pause", "cancel"):
            self._authorized.discard(identity)
            self.started.pop(identity, None)
            if engine is not None:
                engine.save_resume()
                engine.remove(identity)
            self.store.set_task(identity, status="paused" if operation == "pause" else "cancelled")
        elif operation in ("resume", "retry"):
            self.activate([identity])
            if engine is not None:
                engine.remove(identity)
            self.store.set_task(identity, status="queued", attempts=0, reason=None)
        else:
            raise FQDBError("Unknown task operation")
        self._receiving.discard(identity)
        if engine is not None:
            self.owner.peers.apply_upload_limit(self.config.read()["upload_limit"])
        self.preparation_jobs.pop(identity, None)
        return self.store.task(identity)

    def prepare(self, args, progress):
        entry = Entry(**args["entry"])
        cached = torrent_cache_path(self.store.root, entry)
        if cached.is_file():
            validate_daily(cached.read_bytes(), entry, self.store.root)
        save = runtime_path(self.store.root, "staging") / entry.info_hash
        save.mkdir(parents=True, exist_ok=True)
        candidate = save / Path(entry.file).name
        existing = resolve_file(self.store.root, entry)
        with self.owner.storage_lock:
            if existing.is_file() and not candidate.exists():
                progress(0, 1, "copying existing pieces into isolated staging")
                shutil.copy2(existing, candidate)
        return {"save_path": str(save)}
