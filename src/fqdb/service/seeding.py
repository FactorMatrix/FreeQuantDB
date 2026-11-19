"""Restore verified local packages and manage native seed handles."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address
import re
from stat import S_ISREG
import time

import libtorrent as lt

from ..bt.torrents import torrent_cache_path, validate_daily
from ..publish.paths import resolve_file
from ..util import FQDBError, safe_path
from .provision import selected

SEED_RECONCILE_SECONDS = 2
AUTO_SEED_RESCAN_SECONDS = 60
SEED_RESTORE_BATCH = 8


def normalize_listen(value):
    message = "BT listen address must be IPv4:PORT or [IPv6]:PORT, with a port from 0 to 65535"
    if not isinstance(value, str):
        raise FQDBError(message)
    match = re.fullmatch(r"(?:([0-9.]+)|\[([0-9a-fA-F:.]+)\]):([0-9]{1,5})", value)
    if match is None or int(match[3]) > 65535:
        raise FQDBError(message)
    port = int(match[3])
    try:
        if match[1] is not None:
            return f"{IPv4Address(match[1])}:{port}"
        return f"[{IPv6Address(match[2])}]:{port}"
    except ValueError as exc:
        raise FQDBError(message) from exc


class Seeding:
    """Own local sharing authorization for a single service session."""

    def __init__(self, owner):
        self.owner, self.store, self.config, self.lock = owner, owner.store, owner.config, owner.lock
        self.enabled = False
        self.automatic = False
        self.auto_activated = False
        self.parent_pid = None
        self.generation = 0
        self.verifying_seeds = set()
        self.issues = []
        self.native_issues = {}
        self._candidates = None
        self._restore_pending = False
        self._refresh_after = 0
        self._reconcile_after = 0
        self.state = {
            "status": "stopped",
            "message": "做种已停止。",
            "eligible": None,
            "checked": 0,
            "prepared": 0,
            "issues": [],
        }

    def has_work(self):
        if self.verifying_seeds:
            return True
        if not self.enabled or not self.automatic:
            return self.enabled
        engine = self.owner._engine
        return self._restore_pending or (engine is not None and bool(engine.uploads))

    def start_auto(self):
        with self.lock:
            self.auto_activated = True
            if self.enabled or self.state["status"] == "preparing" or not self.config.read()["auto_seed"]:
                return
            self.enabled = self.automatic = True
            self._candidates = None
            self._restore_pending = True
            self.native_issues.clear()
            self.state = self.state | {"checked": 0, "prepared": 0, "issues": []}
            self._invalidate()

    def deactivate(self):
        self.enabled = False
        self.automatic = False
        self.parent_pid = None
        self.generation += 1
        self._restore_pending = False
        self._invalidate()
        self.state = self.state | {"status": "stopped", "message": "做种已暂停。", "eligible": 0, "issues": []}

    def stop(self, args):
        if "parent_pid" in args and args["parent_pid"] != self.parent_pid:
            return self.owner.status()
        self.deactivate()
        if "parent_pid" in args:
            self.config.update(provider=False)
            if self.auto_activated:
                self.start_auto()
        else:
            self.config.update(provider=False, auto_seed=False)
        engine = self.owner._engine
        if engine is not None:
            self.reconcile()
        return self.owner.status()

    def verify(self, entry):
        # Keep the owner polling native alerts while hashing runs in libtorrent.
        with self.lock:
            task = self.store.task(entry.info_hash)
            if task and task["status"] not in ("complete", "failed", "cancelled", "paused"):
                raise FQDBError("Pause or cancel the active download before publishing the same content")
            # A cached native handle may still have open descriptors for damaged
            # bytes. Local publication must hash the newly validated snapshot.
            self.owner.engine.remove(entry.info_hash)
            try:
                handle = self.owner.engine.add(
                    entry.info_hash,
                    torrent_cache_path(self.config.root, entry),
                    resolve_file(self.config.root, entry, immutable=True).parent,
                    recheck=True,
                    upload_only=True,
                    magnet=entry.magnet,
                )
                handle.set_flags(lt.torrent_flags.upload_mode)
            except Exception:
                self.owner.engine.remove(entry.info_hash)
                raise
            self.verifying_seeds.add(entry.info_hash)
        deadline = time.monotonic() + 60
        try:
            while not handle.status().is_seeding:
                with self.lock:
                    if not self.owner.thread.is_alive():
                        self.owner.engine.poll()
                    if entry.info_hash in self.owner.engine.errors:
                        raise FQDBError(self.owner.engine.errors.pop(entry.info_hash))
                if time.monotonic() > deadline:
                    raise FQDBError("Local libtorrent seed verification timed out; catalog not published")
                time.sleep(0.05)
        finally:
            with self.lock:
                self.verifying_seeds.discard(entry.info_hash)
                if not self.wants_seed(entry):
                    self.owner.engine.remove(entry.info_hash)

    def wants_seed(self, entry):
        return self.enabled and selected(entry, self.config.read())

    def native_error(self, identity, reason):
        self.native_issues[identity] = reason
        self._invalidate()

    def storage_changed(self, identity):
        self.native_issues.pop(identity, None)
        self._invalidate()

    def _invalidate(self):
        self._refresh_after = 0
        self._reconcile_after = 0

    def refresh_candidates(self):
        """Read seed evidence without holding the owner lock across filesystem work."""
        with self.lock:
            now = time.monotonic()
            if self.owner.closing or now < self._refresh_after:
                return
            if not self.enabled and not (self.auto_activated and self.owner.gateway_enabled):
                return
            interval = AUTO_SEED_RESCAN_SECONDS if self.automatic else SEED_RECONCILE_SECONDS
            deadline = now + interval
            self._refresh_after = deadline
            native_issues = self.native_issues.copy()
            uploading = self.owner._engine.uploads.copy() if self.automatic and self.owner._engine else set()
        try:
            ready, issues = self._scan_candidates(self.config.read(), native_issues, uploading)
        except Exception:
            with self.lock:
                if self._refresh_after == deadline:
                    self._restore_pending = False
            raise
        with self.lock:
            if self._refresh_after != deadline or self.owner.closing:
                return
            self._candidates = ready
            self.issues = issues
            if self.enabled and self.owner._engine is None:
                self._restore_pending = bool(ready)
            self._refresh_after = time.monotonic() + interval
            self._reconcile_after = 0

    def _scan_candidates(self, cfg, native_issues, uploading):
        active = {e.info_hash: e for e, _ in self.store.active()}
        cached = self.store.verified_versions()
        # Keep in-session uploads on their verified immutable copies after a revision.
        historical = cached.keys() - active.keys()
        if not cfg["seed_old_versions"]:
            historical &= uploading
        entries = active | self.store.entries_by_hash(historical)
        issues = []
        ready = {}
        for identity, entry in entries.items():
            if not selected(entry, cfg):
                continue
            try:
                path = resolve_file(self.config.root, entry, immutable=True)
                snapshots = [path.stat()]
                if identity in active:
                    snapshots.append(safe_path(self.config.root, entry.file).stat())
                metadata_exists = torrent_cache_path(self.config.root, entry).is_file()
            except FileNotFoundError:
                issues.append({"entry": entry.to_dict(), "reason": "published_but_local_missing"})
                continue
            except (FQDBError, OSError) as exc:
                issues.append({"entry": entry.to_dict(), "reason": "data_invalid", "detail": str(exc)})
                continue
            evidence = cached.get(identity)
            replaced = len(snapshots) == 2 and (
                snapshots[0].st_dev == snapshots[1].st_dev and snapshots[0].st_ino != snapshots[1].st_ino
            )
            if replaced or not evidence or any(
                not S_ISREG(stat.st_mode)
                or (stat.st_size, stat.st_mtime_ns) != (evidence["size"], evidence["mtime_ns"])
                for stat in snapshots
            ):
                issues.append({"entry": entry.to_dict(), "reason": "local_file_changed_or_unverified"})
            elif not metadata_exists:
                issues.append({"entry": entry.to_dict(), "reason": "missing_torrent_metadata"})
            elif identity in native_issues:
                issues.append(
                    {"entry": entry.to_dict(), "reason": "data_invalid", "detail": native_issues[identity]}
                )
            else:
                ready[identity] = entry
        return ready, issues

    def status(self):
        result = dict(self.state)
        candidates = self._candidates
        if self.enabled:
            if candidates is None:
                return result | {
                    "status": "checking",
                    "message": "正在检查本地数据，请稍候。",
                    "eligible": None,
                }
            states = (
                [
                    h.status()
                    for identity, h in self.owner._engine.handles.items()
                    if identity in candidates and h.is_valid() and identity not in self.owner._engine.moves
                ]
                if self.owner._engine is not None
                else []
            )
            seeding = [s for s in states if s.is_seeding]
            eligible = len(candidates)
            if any(s.upload_rate > 0 for s in seeding):
                status, message = "sharing", f"正在上传，可提供 {eligible} 个数据包。"
            elif any(s.num_peers > 0 for s in seeding):
                status, message = "ready", f"可提供 {eligible} 个数据包，已连接，等待数据请求。"
            elif seeding:
                status, message = "waiting_peers", f"已开启做种，可提供 {eligible} 个数据包，等待连接。"
            elif candidates:
                status, message = "checking", "正在检查本地数据，请稍候。"
            elif any(i["reason"] == "published_but_local_missing" for i in self.issues):
                status, message = "missing_files", "本地数据文件缺失，暂时无法做种。"
            elif any(i["reason"] == "missing_torrent_metadata" for i in self.issues):
                status, message = "pending_metadata", "缺少原始种子元数据，暂时无法做种。"
            elif self.issues:
                status, message = "invalid_data", "本地文件有异常，暂时无法做种。"
            else:
                status, message = "no_data", "暂无可做种数据。"
            result.update(status=status, message=message, eligible=eligible, issues=self.issues)
        elif self.auto_activated and result["status"] == "stopped":
            result.update(eligible=len(candidates) if candidates is not None else None, issues=self.issues)
        return result

    def start(self, args):
        from .provision import prepare
        from ..catalog.magnet_manifest import scope_identity
        from ..util import day

        if args.get("auto") is True:
            if args.keys() != {"auto"}:
                raise FQDBError("Automatic seeding uses the saved provider settings")
            with self.lock:
                self.config.update(auto_seed=True)
                self.start_auto()
                return self.owner.status()

        cfg = self.config.read()
        scopes = args.get("datasets", cfg["provider_scopes"])
        if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise FQDBError("Provider datasets must be a list of market/asset/data_type/frequency names")
        for scope in scopes:
            identity = scope_identity(scope)
            dataset, frequency = identity["dataset"], identity["frequency"]
            schema = self.store.dataset(dataset, required=False)
            if schema and frequency in schema.frequencies:
                continue
            if not any(entry.sparse for entry, _ in self.store.active(dataset, frequency)):
                raise FQDBError(f"No verified local BT package for provider scope: {scope}")
        parent_pid = args.get("parent_pid")
        if parent_pid is not None and (type(parent_pid) is not int or parent_pid <= 0):
            raise FQDBError("Provider parent_pid must be a positive process ID")
        limit = args.get("upload_limit", cfg["upload_limit"])
        if type(limit) is not int or limit < 0:
            raise FQDBError("upload_limit must be a nonnegative integer")
        start = args.get("start_date", cfg["provider_start"])
        end = args.get("end_date", cfg["provider_end"])
        start, end = day(start) if start else None, day(end) if end else None
        if start and end and start > end:
            raise FQDBError("Provider start_date must not exceed end_date")
        historical = args.get("seed_old_versions", cfg["seed_old_versions"])
        if type(historical) is not bool:
            raise FQDBError("seed_old_versions must be boolean")
        listen = normalize_listen(args["listen"]) if "listen" in args else None
        settings = cfg | {
            "provider_scopes": scopes,
            "provider_start": start,
            "provider_end": end,
            "upload_limit": limit,
            "seed_old_versions": historical,
        }
        with self.lock:
            if listen is not None:
                self.owner.peers.settings({"listen": listen})
            self.generation += 1
            generation = self.generation
            self.enabled = False
            self.automatic = False
            self.parent_pid = None
            self.issues = []
            self.native_issues.clear()
            self._candidates = None
            self._restore_pending = False
            self._invalidate()
            self.state = {
                "status": "preparing",
                "message": "正在准备并校验本地数据。",
                "eligible": None,
                "checked": 0,
                "prepared": 0,
                "issues": [],
            }

        def cancelled():
            return self.owner.closing or generation != self.generation

        with self.owner.storage_lock:
            if cancelled():
                return self.owner.status()
            try:
                prepared = prepare(self.store, self.verify, settings, cancelled=cancelled)
            except Exception:
                if not cancelled():
                    self.state = {
                        "status": "invalid_data",
                        "message": "本地数据准备失败，请检查文件和元数据。",
                        "eligible": 0,
                        "checked": 0,
                        "prepared": 0,
                        "issues": [],
                    }
                raise
            with self.lock:
                if cancelled():
                    return self.owner.status()
                if prepared["eligible"] and listen is not None:
                    session = self.owner.engine.session
                    port = int(listen.rsplit(":", 1)[1])
                    if not session.is_listening() or (port and session.listen_port() != port):
                        self.state = prepared | {
                            "status": "invalid_data",
                            "message": f"无法在 {listen} 上监听 BT 连接。",
                        }
                        raise FQDBError(f"Could not bind BT listen address {listen}")
                self.state = prepared
                self.config.update(
                    provider=bool(prepared["eligible"]),
                    provider_scopes=scopes,
                    provider_start=start,
                    provider_end=end,
                    upload_limit=limit,
                    seed_old_versions=historical,
                )
                if prepared["eligible"]:
                    if parent_pid is None:
                        self.config.update(auto_seed=True)
                    self.owner.peers.apply_upload_limit(limit)
                    self.owner.peers.apply_priority()
                    self.enabled = True
                    self.parent_pid = parent_pid
                    self._invalidate()
                    self.refresh_candidates()
                    self.owner.tick()
                return self.owner.status()

    def reconcile(self):
        wanted = self._candidates if self.enabled else {}
        if wanted is None:
            return
        now = time.monotonic()
        if now < self._reconcile_after:
            return
        self._reconcile_after = now + SEED_RECONCILE_SECONDS
        root = self.config.root
        engine = self.owner.engine
        for identity in engine.uploads - self.verifying_seeds:
            if identity not in wanted:
                engine.remove(identity)
            elif identity not in engine.moves:
                status = engine.handles[identity].status()
                if not status.is_seeding and status.state not in (
                    lt.torrent_status.queued_for_checking,
                    lt.torrent_status.checking_resume_data,
                    lt.torrent_status.checking_files,
                ):
                    self.native_error(identity, "本地文件未通过 BT 分片检查。")
                    engine.remove(identity)
        self._restore_pending = False
        attempted = 0
        for identity, entry in wanted.items():
            if identity in engine.handles or identity in self.native_issues or self.owner.transfers.is_active(identity):
                continue
            if self.automatic and attempted >= SEED_RESTORE_BATCH:
                self._restore_pending = True
                self._reconcile_after = 0
                break
            attempted += 1
            metadata = torrent_cache_path(root, entry)
            try:
                validate_daily(metadata.read_bytes(), entry, root)
                engine.add(
                    identity,
                    metadata,
                    resolve_file(root, entry, immutable=True).parent,
                    upload_only=True,
                    magnet=entry.magnet,
                )
            except Exception as exc:
                self.native_error(identity, str(exc))
