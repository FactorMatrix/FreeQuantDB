"""BT endpoint settings and rules for allocating native upload bandwidth."""

from ..bt.priority import PriorityManager, canonical_ip
from ..util import FQDBError


class PeerSettings:
    def __init__(self, owner):
        self.owner = owner
        self.config = owner.config
        self._priority = None

    @property
    def priority(self):
        if self._priority is None:
            cfg = self.config.read()
            self._priority = PriorityManager(
                self.owner.engine.session, cfg.get("priority_rules", []), cfg["upload_limit"]
            )
        return self._priority

    def priority_status(self):
        if self.owner._engine is not None and self.owner._engine.session is not None:
            return self.priority.status(self.owner._engine.handles)
        cfg = self.config.read()
        return {
            "rules": [
                rule
                | {
                    "state": "inactive",
                    "reason": "native_session_not_running",
                    "effective": False,
                    "connected": False,
                    "connections": 0,
                    "pending_reconnect": False,
                    "upload_rate": None,
                    "uploaded": None,
                }
                for rule in cfg.get("priority_rules", [])
            ],
            "global_upload_limit": cfg["upload_limit"],
            "effective": False,
            "reason": "native_session_not_running",
            "connections": [],
            "pending_reconnect": [],
            "rule_limit": 256,
            "peer_classes_used": 0,
            "statistics_scope": "current_live_connections",
        }

    def apply_priority(self):
        if self.owner._engine is None:
            return self.priority_status()
        cfg = self.config.read()
        return self.priority.apply(cfg.get("priority_rules", []), upload_limit=cfg["upload_limit"])

    def nodes(self):
        return self.config.read()["peer_nodes"]

    def save_nodes(self, nodes):
        return {"nodes": self.config.update(peer_nodes=nodes)["peer_nodes"]}

    @staticmethod
    def _endpoint(ip, port):
        port = int(port)
        if not 0 < port < 65536:
            raise FQDBError("Peer port must be 1..65535")
        return canonical_ip(ip), port

    @staticmethod
    def _enabled(value):
        if not isinstance(value, bool):
            raise FQDBError("enabled must be boolean")
        return value

    def add(self, ip, port, enabled=True, note=""):
        endpoint = self._endpoint(ip, port)
        enabled = self._enabled(enabled)
        nodes = [n for n in self.nodes() if (n["ip"], n["port"]) != endpoint]
        return self.save_nodes(
            nodes + [{"ip": endpoint[0], "port": endpoint[1], "enabled": enabled, "note": str(note)}]
        )

    def remove(self, ip, port):
        endpoint = self._endpoint(ip, port)
        return self.save_nodes([n for n in self.nodes() if (n["ip"], n["port"]) != endpoint])

    def enable(self, ip, port, enabled):
        endpoint = self._endpoint(ip, port)
        enabled = self._enabled(enabled)
        nodes = self.nodes()
        selected = next((n for n in nodes if (n["ip"], n["port"]) == endpoint), None)
        if selected is None:
            raise FQDBError("Unknown peer node")
        selected["enabled"] = enabled
        return self.save_nodes(nodes)

    def _save_rules(self, rules):
        self.config.update(priority_rules=PriorityManager.normalize_rules(rules))
        self.apply_priority()
        return self.priority_status()

    def set_priority(self, ip, level="high", enabled=True, note="", reconnect=False):
        ip = canonical_ip(ip)
        if reconnect:
            raise FQDBError(
                "Automatic reconnect is not performed; rules apply to new connections, reconnect explicitly"
            )
        rule = PriorityManager.validate_rule(ip, level, self._enabled(enabled), note)
        return self._save_rules([r for r in self.config.read()["priority_rules"] if r["ip"] != ip] + [rule])

    def remove_priority(self, ip):
        ip = canonical_ip(ip)
        return self._save_rules([r for r in self.config.read()["priority_rules"] if r["ip"] != ip])

    def enable_priority(self, ip, enabled):
        ip = canonical_ip(ip)
        enabled = self._enabled(enabled)
        rules = self.config.read()["priority_rules"]
        selected = next((r for r in rules if r["ip"] == ip), None)
        if selected is None:
            raise FQDBError("Unknown priority rule")
        selected["enabled"] = enabled
        return self._save_rules(rules)

    def settings(self, args):
        if "listen" in args:
            from .seeding import normalize_listen

            args["listen"] = normalize_listen(args["listen"])
        if "discovery" in args and type(args["discovery"]) is not bool:
            raise FQDBError("Discovery must be boolean")
        current = self.config.read()
        engine = self.owner._engine
        network_changed = any(key in args and args[key] != current[key] for key in ("listen", "discovery"))
        if network_changed and engine is not None and engine.handles:
            raise FQDBError("Stop active BitTorrent work before changing listen or discovery")
        result = self.config.update(**args)
        if engine is not None:
            native = {
                "upload_rate_limit": result["upload_limit"],
                "download_rate_limit": result["download_limit"],
                "connections_limit": result["connections_limit"],
            }
            if "listen" in args and result["listen"] != current["listen"]:
                native["listen_interfaces"] = result["listen"]
            if "discovery" in args and result["discovery"] != current["discovery"]:
                engine.discovery = result["discovery"]
                native.update(enable_dht=engine.discovery, enable_lsd=engine.discovery)
            engine.session.apply_settings(native)
        self.apply_priority()
        return result

    def close(self):
        # Release PriorityManager's session reference before closing the engine.
        self._priority = None
