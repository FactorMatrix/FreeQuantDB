"""Native IP-based upload scheduling, without per-IP bandwidth guarantees.

One shared high-priority peer class is reused for every high rule. The IP class
filter affects new connections only; already observed connections retain their
previous assignment and are explicitly reported as needing reconnection.

References: libtorrent 2.0.11's peer-class APIs, whose Python binding exposes
get_peer_class/set_peer_class as dicts and does not expose per-peer class IDs.
"""

from __future__ import annotations

from collections import defaultdict
from ipaddress import ip_address
from typing import Iterable

import libtorrent as lt

from ..util import FQDBError

MAX_RULES = 256
HIGH_PRIORITY = 8
NORMAL_PRIORITY = 1


def canonical_ip(value: str) -> str:
    if not isinstance(value, str) or "%" in value:
        raise FQDBError("Target must be an IPv4 or IPv6 address without a zone or port")
    try:
        address = ip_address(value.strip())
    except ValueError as exc:
        raise FQDBError("Target must be an IPv4 or IPv6 address, not a host name or network") from exc
    return str(getattr(address, "ipv4_mapped", None) or address)


def validate_rule(ip: str, level: str = "high", enabled: bool = True, note: str = "") -> dict:
    ip = canonical_ip(ip)
    if level not in ("normal", "high"):
        raise FQDBError("Priority level must be 'normal' or 'high'")
    if not isinstance(enabled, bool):
        raise FQDBError("Priority enabled must be a boolean")
    if not isinstance(note, str) or len(note) > 240:
        raise FQDBError("Priority note must be a string of at most 240 characters")
    return {"ip": ip, "level": level, "enabled": enabled, "note": note}


def normalize_rules(rules: Iterable[dict]) -> list[dict]:
    if not isinstance(rules, (list, tuple)) or len(rules) > MAX_RULES:
        raise FQDBError(f"At most {MAX_RULES} IP priority rules are supported")
    result = []
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or "ip" not in rule or set(rule) - {"ip", "level", "enabled", "note"}:
            raise FQDBError("Invalid IP priority rule fields")
        candidate = validate_rule(**rule)
        if candidate["ip"] in seen:
            raise FQDBError("Duplicate IP priority rule")
        seen.add(candidate["ip"])
        result.append(candidate)
    return sorted(result, key=lambda r: (ip_address(r["ip"]).version, int(ip_address(r["ip"]))))


class PriorityManager:
    """Own the FQDB peer-class IP mapping for one existing libtorrent session.

    The caller persists ``rules`` and owns the session, including its global
    upload limit. This object never changes that limit, forces disconnections,
    opens another session or applies per-IP rate caps.
    """

    def __init__(self, session, rules=(), upload_limit: int | None = None):
        normalized = normalize_rules(rules)
        if upload_limit is not None and (type(upload_limit) is not int or upload_limit < 0):
            raise FQDBError("Global upload limit must be a nonnegative integer")
        self.session = session
        self.rules: list[dict] = []
        self.requested_upload_limit = upload_limit
        self._assignments: dict[tuple, str] = {}
        self._high_class = int(session.create_peer_class("fqdb-high-upload"))
        if self._high_class >= 32:
            session.delete_peer_class(self._high_class)
            raise FQDBError("No peer-class bit available for IP mapping; libtorrent supports IDs 0–31")
        info = session.get_peer_class(self._high_class)
        info["upload_priority"] = HIGH_PRIORITY
        info["download_priority"] = NORMAL_PRIORITY
        info["upload_limit"] = 0
        info["download_limit"] = 0
        self.session.set_peer_class(self._high_class, info)
        self._filter = None
        self.apply(normalized, upload_limit=upload_limit)

    @property
    def class_id(self):
        return self._high_class

    @staticmethod
    def validate_rule(ip, level="high", enabled=True, note=""):
        return validate_rule(ip, level, enabled, note)

    @staticmethod
    def normalize_rules(rules):
        return normalize_rules(rules)

    def _level(self, ip):
        return next((r["level"] for r in self.rules if r["enabled"] and r["ip"] == ip), "normal")

    def _peers(self, handles=None):
        if handles is None:
            handles = self.session.get_torrents()
        elif isinstance(handles, dict):
            handles = handles.values()
        peers = []
        for handle in list(handles):
            if not handle.is_valid():
                continue
            try:
                identity = str(handle.info_hashes().v1)
                for peer in handle.get_peer_info():
                    if peer.flags & (lt.peer_info.connecting | lt.peer_info.handshake):
                        continue
                    ip = canonical_ip(peer.ip[0])
                    endpoint = tuple(peer.ip)
                    # 2.0 exposes a tuple; 2.1 exposes the same endpoint via a method.
                    local_endpoint = peer.local_endpoint
                    local = tuple(local_endpoint() if callable(local_endpoint) else local_endpoint)
                    key = (identity, endpoint, local, str(peer.pid))
                    peers.append((key, ip, peer))
            except (RuntimeError, ValueError):
                # A handle may be removed asynchronously between validity and read.
                continue
        return peers

    def apply(self, rules, upload_limit=None):
        normalized = normalize_rules(rules)
        if upload_limit is not None and (type(upload_limit) is not int or upload_limit < 0):
            raise FQDBError("Global upload limit must be a nonnegative integer")
        previous = {r["ip"]: r["level"] for r in self.rules if r["enabled"]}
        # Observe current assignments before changing the filter. Native filter
        # updates do not retroactively reclassify these connections.
        for key, ip, _ in self._peers():
            self._assignments.setdefault(key, self._level(ip))
        self.rules = normalized
        if upload_limit is not None:
            self.requested_upload_limit = upload_limit
        mapping = lt.ip_filter()
        # Keep every connection under the user's original global limiter. Do
        # not exempt private/loopback addresses or modify the TCP type filter.
        global_mask = 1 << int(self.session.global_peer_class_id)
        mapping.add_rule("0.0.0.0", "255.255.255.255", global_mask)
        mapping.add_rule("::", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", global_mask)
        for rule in normalized:
            if rule["enabled"] and rule["level"] == "high":
                mapping.add_rule(rule["ip"], rule["ip"], global_mask | (1 << self._high_class))
        self.session.set_peer_class_filter(mapping)
        self._filter = mapping
        # A synchronous barrier also covers connections that raced the filter
        # update; conservatively retain their old assignment until reconnect.
        self.session.get_settings()
        for key, ip, _ in self._peers():
            self._assignments.setdefault(key, previous.get(ip, "normal"))
        return self.status()

    def status(self, handles=None):
        peers = self._peers(handles)
        # A caller may show only selected torrents. Do not forget the previous
        # class of another still-connected torrent while filtering the view.
        live = {key for key, _, _ in (self._peers() if handles is not None else peers)}
        self._assignments = {key: level for key, level in self._assignments.items() if key in live}
        grouped = defaultdict(list)
        connections = []
        for key, ip, peer in peers:
            assigned = self._assignments.setdefault(key, self._level(ip))
            item = {
                "ip": ip,
                "endpoint": list(peer.ip),
                "assigned_level": assigned,
                "desired_level": self._level(ip),
                "pending_reconnect": assigned != self._level(ip),
                "upload_rate": int(peer.payload_up_speed),
                "uploaded": int(peer.total_upload),
            }
            grouped[ip].append(item)
            connections.append(item)
        native = self.session.get_settings()
        upload_limit = int(native.get("upload_rate_limit", 0))
        local_bypass = bool(native.get("ignore_limits_on_local_network", False))
        result = []
        for rule in self.rules:
            observed = grouped.get(rule["ip"], [])
            pending = any(p["pending_reconnect"] for p in observed)
            local_exception = local_bypass and (
                ip_address(rule["ip"]).is_private or ip_address(rule["ip"]).is_loopback
            )
            if not rule["enabled"]:
                state, reason = (
                    ("pending_reconnect", "existing_connection_keeps_previous_class")
                    if pending
                    else ("disabled", "rule_disabled")
                )
            elif rule["level"] == "normal":
                state, reason = (
                    ("pending_reconnect", "existing_connection_keeps_previous_class")
                    if pending
                    else ("normal", "normal_priority")
                )
            elif upload_limit <= 0:
                state, reason = "waiting_rate_limit", "global_upload_limit_disabled"
            elif local_exception:
                state, reason = "local_limit_exception", "local_network_limit_exception_enabled"
            elif pending:
                state, reason = "pending_reconnect", "existing_connection_keeps_previous_class"
            elif not observed:
                state, reason = "waiting_connection", "target_not_connected"
            else:
                state, reason = "active", "native_peer_class_for_new_connection"
            result.append(
                rule
                | {
                    "state": state,
                    "reason": reason,
                    "effective": state == "active",
                    "connected": bool(observed),
                    "connections": len(observed),
                    "pending_reconnect": pending,
                    "upload_rate": sum(p["upload_rate"] for p in observed) if observed else None,
                    "uploaded": sum(p["uploaded"] for p in observed) if observed else None,
                    "statistics_scope": "current_live_connections",
                    "lifetime_uploaded": None,
                    "class_id": self._high_class
                    if rule["level"] == "high"
                    else int(self.session.global_peer_class_id),
                    "class_mask": int(self._filter.access(rule["ip"])),
                    "native_upload_priority": self.session.get_peer_class(self._high_class)["upload_priority"]
                    if rule["level"] == "high"
                    else self.session.get_peer_class(self.session.global_peer_class_id)["upload_priority"],
                }
            )
        return {
            "rules": result,
            "global_upload_limit": upload_limit,
            "high_class_id": self._high_class,
            "high_upload_priority": int(self.session.get_peer_class(self._high_class)["upload_priority"]),
            "normal_upload_priority": int(
                self.session.get_peer_class(self.session.global_peer_class_id)["upload_priority"]
            ),
            "pending_reconnect": [p for p in connections if p["pending_reconnect"]],
            "connections": connections,
            "rule_limit": MAX_RULES,
            "peer_classes_used": 1,
            "statistics_scope": "current_live_connections",
            "note": "Relative native scheduling only under rate limiting; no guaranteed speed or ratio. IP is not identity.",
        }
