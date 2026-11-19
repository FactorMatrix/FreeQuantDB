from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import uuid

from filelock import FileLock

from .layout import detect_layout, ensure_layout, runtime_path
from .root_safety import DataRootUnavailable, check_configured_mount, directory_stamp, file_stamp, mount_info
from .util import FQDBError, atomic_json

_current_root: Path | None = None
DEFAULT_UPLOAD_LIMIT = 500_000
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


def _user_config():
    path = user_config_path()
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or any(
            not isinstance(value.get(key, {}), dict) for key in ("data_roots", "data_aliases")
        ):
            raise ValueError("expected a configuration object and a data root registry")
        _saved_path(value)
        return value
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise FQDBError(f"Invalid user configuration {path}: {exc}") from exc


def _saved_path(user):
    value = user.get("data_dir")
    if value is None:
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("data_dir must be an absolute path")
    # Keep the recorded pathname so a replacement symlink cannot redirect it.
    return Path(value)


def saved_root() -> Path | None:
    return _saved_path(_user_config())


def _requested_path(root, user):
    if root is None:
        if _current_root is not None:
            return _current_root
        root = os.environ.get("FQDB_DATA_DIR") or _saved_path(user) or Path.home() / ".fqdb"
    if str(root) == "":
        raise FQDBError("An explicit data directory cannot be empty")
    return Path(os.path.abspath(Path(root).expanduser()))


def _resolve(candidate, user):
    if str(candidate) in user.get("data_roots", {}) or candidate == _saved_path(user):
        return candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise DataRootUnavailable(candidate, str(exc)) from exc
    alias = user.get("data_aliases", {}).get(str(candidate))
    if alias is not None and str(resolved) != alias:
        raise DataRootUnavailable(candidate, f"the recorded directory alias no longer points to {alias}")
    return resolved


def resolve_root(root=None) -> Path:
    """Explicit -> initialized process -> environment -> saved -> default."""
    user = _user_config()
    return _resolve(_requested_path(root, user), user)


def select_root(root, persist=True) -> Path:
    global _current_root
    if persist:
        config = Config(root).ensure()
        config.remember(select=True)
        _current_root = config.root
    else:
        _current_root = resolve_root(root)
    return _current_root


class Config:
    def __init__(self, root=None):
        user = _user_config()
        self._requested = _requested_path(root, user)
        self.root = _resolve(self._requested, user)
        self._expected = user.get("data_roots", {}).get(str(self.root))
        self._known = str(self.root) in user.get("data_roots", {}) or self.root == _saved_path(user)
        self._identity = None
        self._snapshot = None
        self._marker = self.root / ".state/root-id.json"
        self._directories = (self.root, self.root / ".state", self.root / "data", self.root / "catalog")
        if self._expected is None:
            try:
                for path in dict.fromkeys((self._requested, self.root)):
                    check_configured_mount(path)
            except OSError as exc:
                raise DataRootUnavailable(self.root, str(exc)) from exc
        if self._known:
            self.check_available()
        self.layout = detect_layout(self.root)
        self.path = runtime_path(self.root, "config")

    def _stamps(self):
        return tuple(directory_stamp(path) for path in self._directories), file_stamp(self._marker)

    def _existing_storage(self):
        root_device, _ = directory_stamp(self.root)
        if self._expected is None and self._known and detect_layout(self.root) != "canonical":
            raise DataRootUnavailable(self.root, "the configured data layout is missing")
        try:
            file_stamp(self._marker)
        except FileNotFoundError:
            if self._expected is not None:
                raise DataRootUnavailable(self.root, "the recorded data root marker is missing") from None
            return None
        token = json.loads(self._marker.read_text())
        if not isinstance(token, str) or uuid.UUID(token).hex != token:
            raise DataRootUnavailable(self.root, "the data root marker is invalid")
        identity = {"token": token, "mount": mount_info(self.root, root_device)}
        if self._known and self._expected is not None and identity != self._expected:
            raise DataRootUnavailable(self.root, "the path no longer identifies the recorded storage")
        return identity

    def check_available(self):
        """Fail before using a known root or its active mount after a change."""
        try:
            if self._snapshot is None:
                self._identity = self._existing_storage()
                if self._identity is not None:
                    self._snapshot = self._stamps()
            elif self._stamps() != self._snapshot:
                raise DataRootUnavailable(self.root, "the data directory or its mount has changed")
        except DataRootUnavailable:
            raise
        except (OSError, ValueError) as exc:
            raise DataRootUnavailable(self.root, str(exc)) from exc

    @contextmanager
    def pin(self):
        """Keep the mounted directory open for the lifetime of the worker on POSIX."""
        self.check_available()
        descriptor = None
        try:
            if hasattr(os, "O_DIRECTORY"):
                try:
                    descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    info = os.fstat(descriptor)
                    if self._snapshot is not None and (info.st_dev, info.st_ino) != self._snapshot[0][0]:
                        raise DataRootUnavailable(self.root, "the data directory changed while opening it")
                except OSError as exc:
                    raise DataRootUnavailable(self.root, str(exc)) from exc
            yield self
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def ensure(self):
        try:
            if self._known or self.root.exists():
                self.check_available()
            else:
                self.root.mkdir(parents=True, exist_ok=True)
            with self.pin():
                self.layout, self.path = ensure_layout(self.root)
                for path, value in ((self._marker, uuid.uuid4().hex), (self.path, DEFAULTS)):
                    if not path.exists():
                        try:
                            atomic_json(path, value, immutable=True)
                        except FileExistsError:
                            pass  # Another initializer already committed the file.
                self.check_available()
        except OSError as exc:
            raise DataRootUnavailable(self.root, str(exc)) from exc
        return self

    def remember(self, *, select=False):
        """Record storage outside the data mount, optionally making it the default."""
        self.check_available()
        if self._identity is None:
            raise DataRootUnavailable(self.root, "the data root has not been initialized")
        old = _user_config()
        key = str(self.root)
        alias = str(self._requested)
        if (
            old.get("data_roots", {}).get(key) == self._identity
            and (alias == key or old.get("data_aliases", {}).get(alias) == key)
            and (not select or old.get("data_dir") == key)
        ):
            return
        path = user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(path.with_name(path.name + ".lock"), timeout=30):
            self.check_available()
            user = _user_config()
            roots = user.get("data_roots", {})
            if key in roots and roots[key] != self._identity:
                raise DataRootUnavailable(
                    self.root, "another process recorded different storage for this path"
                )
            user.update(schema_version=1, data_roots=roots | {key: self._identity})
            if alias != key:
                aliases = user.get("data_aliases", {})
                if aliases.get(alias, key) != key:
                    raise DataRootUnavailable(
                        self._requested, "another process recorded different storage for this alias"
                    )
                user["data_aliases"] = aliases | {alias: key}
            if select:
                user["data_dir"] = key
            atomic_json(path, user)
        self._expected = self._identity
        self._known = True

    def read(self):
        if self._known or self._snapshot is not None:
            self.check_available()
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
        with self.pin():
            atomic_json(self.path, result)
        return result
