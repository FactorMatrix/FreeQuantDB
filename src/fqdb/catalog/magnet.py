"""Filesystem catalog discovery, local publication and transactional remote synchronization."""

from base64 import b64decode, b64encode
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from filelock import FileLock
import httpx

from ..util import FQDBError, atomic_bytes, atomic_json, safe_path
from .magnet_manifest import (
    SCHEMA_VERSION,
    dataset_path,
    json_object,
    package_path,
    parse_dataset,
    parse_package,
    parse_root,
)
from .remote import MAX_CATALOG, read_source, source_root
from .verification import inspect_package, problem

__all__ = [
    "MagnetCatalog",
    "OFFICIAL_SOURCE",
    "SCHEMA_VERSION",
    "dataset_path",
    "package_path",
    "parse_dataset",
    "parse_package",
    "parse_root",
]

OFFICIAL_SOURCE = "https://raw.githubusercontent.com/FactorMatrix/FreeQuantDB/main/catalog/index.json"


def _encode(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode() + b"\n"


def _digest(payload):
    return hashlib.sha256(payload).hexdigest() if payload is not None else None


def _set(path, payload):
    if payload is None:
        path.unlink(missing_ok=True)
    else:
        atomic_bytes(path, payload)


def _read(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _descriptor(path, payload, *, remote=False, strict_magnet=True):
    try:
        if len(payload) > MAX_CATALOG:
            raise FQDBError("Catalog descriptor exceeds the supported size")
        body = json_object(payload)
        if path.endswith("/index.json"):
            return parse_dataset(path, body)
        return parse_package(path, body, require_magnet=remote, strict_magnet=strict_magnet)
    except (ValueError, TypeError) as exc:
        raise FQDBError(f"{path}: {exc}") from exc


class MagnetCatalog:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.directory = self.root / "catalog"
        self.state = self.root / ".state/catalog"
        self.sync_state = self.state / "sync.json"
        self.journal = self.state / "transaction.json"

    @contextmanager
    def _locked(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with FileLock(self.state / "lock", timeout=30):
            self._recover()
            yield

    def _path(self, relative):
        target = safe_path(self.root, "catalog/" + relative)
        if target != self.directory / relative:
            raise FQDBError(f"Catalog paths must be canonical and must not use symlinks: {relative}")
        return target

    def _recover(self):
        if self.journal.exists():
            journal = json_object(self.journal.read_bytes())
            for relative, encoded in journal["files"].items():
                _set(self._path(relative), b64decode(encoded) if encoded is not None else None)
            sync = journal["sync"]
            if journal["restore_sync"]:
                _set(self.sync_state, b64decode(sync) if sync is not None else None)
            self.journal.unlink()

    def _commit(self, changes, records=None):
        if not changes and records is None:
            return
        files = {relative: _read(self._path(relative)) for relative in changes}
        previous = _read(self.sync_state) if records is not None else None
        atomic_json(
            self.journal,
            {
                "files": {
                    path: b64encode(data).decode() if data is not None else None
                    for path, data in files.items()
                },
                "restore_sync": records is not None,
                "sync": b64encode(previous).decode() if previous is not None else None,
            },
        )
        try:
            for relative, payload in changes.items():
                _set(self._path(relative), payload)
            if records is not None:
                atomic_json(self.sync_state, {"files": records})
            self.journal.unlink()
        except BaseException:
            self._recover()
            raise

    def _paths(self, pattern):
        return sorted(path.relative_to(self.directory).as_posix() for path in self.directory.rglob(pattern))

    def _scan(self, *, deep=False, path=None, changed=()):
        issues = []
        datasets = {}
        for relative in self._paths("index.json"):
            if relative == "index.json":
                continue
            try:
                dataset = _descriptor(relative, self._path(relative).read_bytes())
                datasets[dataset["scope"]] = dataset
            except (OSError, FQDBError) as exc:
                issues.append(problem(relative, "dataset_descriptor", str(exc)))
        packages = []
        for relative in self._paths("*.parquet.json"):
            if path is not None and relative != path:
                continue
            scope = "/".join(relative.split("/")[:4])
            if scope not in datasets:
                continue
            try:
                package = _descriptor(relative, self._path(relative).read_bytes(), strict_magnet=False)
            except (OSError, FQDBError) as exc:
                issues.append(problem(relative, "package_descriptor", str(exc)))
                continue
            package.update(
                inspect_package(
                    self.root,
                    self.state / "inspections",
                    datasets[scope],
                    package,
                    deep=deep or relative in changed,
                )
            )
            if package["magnet_error"]:
                package["issues"].append(problem(relative, "magnet", package["magnet_error"]))
            issues.extend(package["issues"])
            packages.append(package)
        return {"datasets": list(datasets.values()), "packages": packages, "issues": issues, "errors": issues}

    def scan(self, *, deep=False, path=None):
        """Discover files first, then compare descriptors to local Parquet evidence."""
        with self._locked():
            return self._scan(deep=deep, path=path)

    def _read_remote(self, source):
        with httpx.Client() as client:
            return self._fetch_snapshot(source, client)

    def _fetch_snapshot(self, source, client):
        def fetch(relative):
            try:
                return read_source(source, relative, cache_dir=self.state / "http", client=client)
            except (OSError, ValueError, httpx.HTTPError) as exc:
                raise FQDBError(f"Cannot read catalog object {relative}: {exc}") from exc

        try:
            index = parse_root(json_object(fetch("index.json")))
        except FQDBError as exc:
            raise FQDBError(f"Catalog index: {exc}") from exc
        payloads = {}
        identities = set()
        for path in index["datasets"] + index["packages"]:
            payload = fetch(path)
            descriptor = _descriptor(path, payload, remote=True)
            identity = descriptor.get("info_hash")
            if identity is not None:
                if identity in identities:
                    raise FQDBError(f"Multiple package paths use one magnet identity: {path}")
                identities.add(identity)
            payloads[path] = payload
        return payloads

    def _records(self):
        content = _read(self.sync_state)
        return json_object(content)["files"] if content is not None else {}

    def _sync_changes(self, remote, records):
        changes, added, updated, removed = {}, [], [], []
        available = {path: _read(self._path(path)) for path in records.keys() | remote.keys()}
        for path, record in records.items():
            record["hidden"] = available[path] is None
        for path, payload in remote.items():
            current = available[path]
            record = records.get(path)
            if record and record["hidden"]:
                continue
            if not path.endswith("/index.json"):
                parent = dataset_path("/".join(path.split("/")[:4]))
                if records.get(parent, {}).get("hidden"):
                    continue
            if current is not None and (record is None or _digest(current) != record["digest"]):
                records.setdefault(path, {"digest": None, "hidden": False})
                continue
            if current != payload:
                changes[path] = payload
                (added if current is None else updated).append(path)
            records[path] = {"digest": _digest(payload), "hidden": False}
        for path in records.keys() - remote.keys():
            current = available[path]
            if current is not None and _digest(current) == records[path]["digest"]:
                changes[path] = None
                removed.append(path)
                del records[path]
        return changes, {"added": sorted(added), "updated": sorted(updated), "removed": sorted(removed)}

    def sync(self, source=OFFICIAL_SOURCE):
        """Apply a complete validated remote snapshot while preserving manual files."""
        source = source_root(source)
        self.state.mkdir(parents=True, exist_ok=True)
        # Serialize syncs without preventing offline readers from scanning while HTTP is in flight.
        with FileLock(self.state / "sync.lock", timeout=30):
            remote = self._read_remote(source)
            with self._locked():
                records = self._records()
                changes, result = self._sync_changes(remote, records)
                if changes or records != self._records():
                    self._commit(changes, records)
                scan = self._scan(changed=set(result["added"] + result["updated"]))
            return (
                scan
                | result
                | {"source": source, "hidden": sorted(path for path, r in records.items() if r["hidden"])}
            )

    def _write_manual(self, descriptors):
        records = self._records()
        changes = {}
        for relative, payload in descriptors.items():
            _descriptor(relative, payload)
            previous = _read(self._path(relative))
            if previous != payload:
                changes[relative] = payload
            record = records.get(relative)
            if record is None or record["hidden"]:
                records[relative] = {"digest": record["digest"] if record else None, "hidden": False}
        self._commit(changes, records if records != self._records() else None)

    def write_dataset(self, scope, payload):
        with self._locked():
            self._write_manual({dataset_path(scope): _encode(payload)})

    def write_package(self, scope, period, payload):
        with self._locked():
            if not self._path(dataset_path(scope)).is_file():
                raise FQDBError("A dataset index is required before writing its package")
            self._write_manual({package_path(scope, period): _encode(payload)})

    def write_publication(self, scope, dataset_payload, period, package_payload):
        with self._locked():
            self._write_manual(
                {
                    dataset_path(scope): _encode(dataset_payload),
                    package_path(scope, period): _encode(package_payload),
                }
            )

    def import_local(self, source, *, relative=None):
        """Install a directory/index snapshot or one descriptor as manual metadata."""
        incoming = Path(source).expanduser().resolve()
        with self._locked():
            descriptors = self._manual_descriptors(incoming, relative)
            for path, payload in descriptors.items():
                info = _descriptor(path, payload)
                parent = dataset_path(info["scope"])
                if (
                    not path.endswith("/index.json")
                    and parent not in descriptors
                    and not self._path(parent).is_file()
                ):
                    raise FQDBError(f"Import dataset index {parent} before its package {path}")
            self._write_manual(descriptors)
            return self._scan(changed=descriptors) | {"imported": sorted(descriptors)}

    def _manual_descriptors(self, incoming, relative):
        if incoming.is_dir():
            if relative is not None:
                raise FQDBError("An explicit descriptor path requires a single local JSON file")
            if (incoming / "catalog").is_dir():
                incoming /= "catalog"
            if not (incoming / "index.json").is_file():
                return {
                    path.relative_to(incoming).as_posix(): path.read_bytes()
                    for pattern in ("index.json", "*.parquet.json")
                    for path in sorted(incoming.rglob(pattern))
                }
            incoming /= "index.json"
        payload = incoming.read_bytes()
        if relative is None and incoming.name == "index.json" and "datasets" in json_object(payload):
            index = parse_root(json_object(payload))
            return {
                path: safe_path(incoming.parent, path).read_bytes()
                for path in index["datasets"] + index["packages"]
            }
        if relative is not None:
            return {relative: payload}
        for length in (5, 6, 7):
            inferred = "/".join(incoming.parts[-length:])
            try:
                info = _descriptor(inferred, payload)
            except FQDBError:
                continue
            descriptors = {inferred: payload}
            if not inferred.endswith("/index.json"):
                index = dataset_path(info["scope"])
                source_index = incoming.parents[length - 1] / index
                if source_index.is_file() and not self._path(index).is_file():
                    descriptors[index] = source_index.read_bytes()
            return descriptors
        raise FQDBError("Supply the canonical catalog-relative path for this descriptor")
