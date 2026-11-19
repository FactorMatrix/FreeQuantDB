from __future__ import annotations

import json
import os
from pathlib import Path

from .layout import detect_layout, ensure_layout, runtime_path
from .util import FQDBError, atomic_json

_current_root: Path | None = None
DEFAULT_UPLOAD_LIMIT = 1_000_000
DEFAULTS = {
    "listen": "0.0.0.0:0",
    "discovery": True,
    "max_active": 4,
    "connections_limit": 80,
    "download_limit": 0,
    "upload_limit": DEFAULT_UPLOAD_LIMIT,
    "peer_timeout": 60,
    "max_retries": 2,
    "retry_interval": 10,
    "peer_nodes": [],
    "provider": False,
    "auto_seed": True,
    "provider_scopes": [],
    "provider_start": None,
    "provider_end": None,
    "seed_old_versions": False,
    "sources": [],
    "priority_rules": [],
    "source_adapters": [],
    "source_checks": {},
}


def user_config_path() -> Path:
    explicit = os.environ.get("FQDB_USER_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return parent / "FQDB/config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fqdb/config.json"


def saved_root() -> Path | None:
    path = user_config_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        selected = value.get("data_dir")
        if selected is not None and (not isinstance(selected, str) or not selected):
            raise ValueError("data_dir must be a nonempty string")
        return Path(selected).expanduser().resolve() if selected else None
    except (ValueError, TypeError, AttributeError) as exc:
        raise FQDBError(f"Invalid user configuration {path}: {exc}") from exc


def resolve_root(root=None) -> Path:
    """Explicit -> initialized process -> environment -> saved -> default."""
    if root is not None:
        if str(root) == "":
            raise FQDBError("An explicit data directory cannot be empty")
        return Path(root).expanduser().resolve()
    if _current_root is not None:
        return _current_root
    if configured := os.environ.get("FQDB_DATA_DIR"):
        return Path(configured).expanduser().resolve()
    selected = saved_root()
    if selected is not None:
        return selected
    return (Path.home() / ".fqdb").resolve()


def select_root(root, persist=True) -> Path:
    global _current_root
    selected = resolve_root(root)
    if persist:
        path = user_config_path()
        value = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(value, dict):
            raise FQDBError(f"Invalid user configuration: {path}")
        atomic_json(path, value | {"schema_version": 1, "data_dir": str(selected)})
        path.chmod(0o600)
    _current_root = selected
    return selected


class Config:
    def __init__(self, root=None):
        self.root = resolve_root(root)
        self.layout = detect_layout(self.root)
        self.path = runtime_path(self.root, "config")

    def ensure(self):
        self.layout, self.path = ensure_layout(self.root)
        if not self.path.exists():
            atomic_json(self.path, DEFAULTS)
        return self

    def read(self):
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        if not isinstance(saved, dict):
            raise FQDBError(f"Invalid data root configuration: {self.path}")
        return DEFAULTS | {key: saved[key] for key in DEFAULTS.keys() & saved.keys()}

    def update(self, **values):
        unknown = values.keys() - DEFAULTS.keys()
        if unknown:
            raise FQDBError(f"Unknown settings: {sorted(unknown)}")
        for key in ("max_active", "connections_limit", "peer_timeout", "retry_interval"):
            if key in values and (not isinstance(values[key], int) or values[key] < 1):
                raise FQDBError(f"{key} must be a positive integer")
        for key in ("upload_limit", "download_limit", "max_retries"):
            if key in values and (not isinstance(values[key], int) or values[key] < 0):
                raise FQDBError(f"{key} must be a nonnegative integer")
        if "auto_seed" in values and type(values["auto_seed"]) is not bool:
            raise FQDBError("auto_seed must be boolean")
        if "peer_nodes" in values:
            from .bt.priority import canonical_ip

            nodes = values["peer_nodes"]
            if not isinstance(nodes, list):
                raise FQDBError("Peer nodes must be a list of node records")
            normalized = {}
            for node in nodes:
                if not isinstance(node, dict) or node.keys() != {"ip", "port", "enabled", "note"}:
                    raise FQDBError("Invalid peer node fields")
                address, port = canonical_ip(node["ip"]), node["port"]
                if type(port) is not int or not 0 < port < 65536:
                    raise FQDBError("Peer port must be 1..65535")
                if type(node["enabled"]) is not bool:
                    raise FQDBError("Peer enabled must be a boolean")
                if not isinstance(node["note"], str) or len(node["note"]) > 240:
                    raise FQDBError("Peer note must be a string of at most 240 characters")
                if (address, port) in normalized:
                    raise FQDBError("Duplicate peer node")
                normalized[address, port] = node | {"ip": address}
            values["peer_nodes"] = list(normalized.values())
        result = self.read() | values
        atomic_json(self.path, result)
        return result
