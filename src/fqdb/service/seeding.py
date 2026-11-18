"""Explicit provider lifecycle and verified native seed handles."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address
import re
import time

import libtorrent as lt

from ..bt.torrents import torrent_cache_path
from ..publish.paths import resolve_file
from ..util import FQDBError
from .provision import selected


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
        self.parent_pid = None
        self.generation = 0
        self.verifying_seeds = set()
        self.issues = []
        self.state = {
            "status": "stopped",
            "message": "做种已停止。",
            "eligible": None,
            "checked": 0,
            "prepared": 0,
            "issues": [],
        }

    def has_work(self):
        return bool(self.enabled or self.verifying_seeds)

    def deactivate(self):
        self.enabled = False
        self.parent_pid = None
        self.generation += 1
        self.state = self.state | {"status": "stopped", "message": "做种已停止。"}

    def stop(self, args):
        if "parent_pid" in args and args["parent_pid"] != self.parent_pid:
            return self.owner.status()
        self.deactivate()
        self.config.update(provider=False)
        engine = self.owner._engine
        for identity in list(engine.handles) if engine is not None else []:
            task = self.store.task(identity)
            if task is None or task["status"] in ("complete", "failed", "cancelled", "paused"):
                engine.remove(identity)
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

    def seed_candidates(self):
        if not self.enabled:
            self.issues = []
            return []
        cfg = self.config.read()
        entries = (
            self.store.entries(latest=False)
            if cfg["seed_old_versions"]
            else [e for e, _ in self.store.active()]
        )
        cached = self.store.verified_versions()
        self.issues = []
        ready = []
        for entry in entries:
            if not selected(entry, cfg):
                continue
            try:
                stat = resolve_file(self.config.root, entry, immutable=True).stat()
            except FileNotFoundError:
                self.issues.append({"entry": entry.to_dict(), "reason": "published_but_local_missing"})
                continue
            evidence = cached.get(entry.info_hash)
            # A changed file must be explicitly revalidated before providing.
            if evidence and stat.st_size == evidence["size"] and stat.st_mtime_ns == evidence["mtime_ns"]:
                ready.append(entry)
            else:
                self.issues.append({"entry": entry.to_dict(), "reason": "local_file_changed_or_unverified"})
        return ready

    def status(self):
        result = dict(self.state)
        if self.enabled:
            eligible_ids = {entry.info_hash for entry in self.seed_candidates()}
            eligible = len(eligible_ids)
            states = (
                [
                    h.status()
                    for identity, h in self.owner._engine.handles.items()
                    if identity in eligible_ids and h.is_valid()
                ]
                if self.owner._engine is not None
                else []
            )
            if any(s.upload_rate > 0 for s in states):
                status, message = "sharing", f"正在上传，可提供 {eligible} 个数据包。"
            elif any(s.num_peers > 0 for s in states):
                status, message = "ready", f"可提供 {eligible} 个数据包，已连接，等待数据请求。"
            elif eligible:
                status, message = "waiting_peers", f"已开启做种，可提供 {eligible} 个数据包，等待连接。"
            else:
                status, message = "invalid_data", "当前没有通过校验的可提供数据。"
            result.update(status=status, message=message, eligible=eligible)
        return result

    def start(self, args):
        from .provision import prepare
        from ..catalog.magnet_manifest import scope_identity
        from ..util import day

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
                engine = self.owner._engine
                if engine is not None and listen != engine.session.get_settings()["listen_interfaces"]:
                    raise FQDBError("Stop active BT work before changing the listen address")
                if listen != self.config.read()["listen"]:
                    self.config.update(listen=listen)
            self.generation += 1
            generation = self.generation
            self.enabled = False
            self.parent_pid = None
            self.issues = []
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
                    self.owner.engine.session.apply_settings({"upload_rate_limit": limit})
                    self.owner.peers.apply_priority()
                    self.enabled = True
                    self.parent_pid = parent_pid
                    self.owner.tick()
                return self.owner.status()

    def reconcile(self):
        root = self.config.root
        wanted = {e.info_hash: e for e in self.seed_candidates()}
        for identity, h in list(self.owner.engine.handles.items()):
            task = self.store.task(identity)
            if (
                identity not in wanted
                and identity not in self.verifying_seeds
                and (task is None or task["status"] in ("complete", "failed", "cancelled", "paused"))
            ):
                self.owner.engine.remove(identity)
        for identity, entry in wanted.items():
            task = self.store.task(identity)
            if identity not in self.owner.engine.handles and (
                not task or task["status"] in ("complete", "failed", "cancelled", "paused")
            ):
                handle = self.owner.engine.add(
                    identity,
                    torrent_cache_path(root, entry),
                    resolve_file(root, entry, immutable=True).parent,
                    recheck=True,
                    upload_only=True,
                    magnet=entry.magnet,
                )
                handle.set_flags(lt.torrent_flags.upload_mode)
