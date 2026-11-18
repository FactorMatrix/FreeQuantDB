"""The data-root layout: catalog/, data/ and private runtime state."""

from __future__ import annotations
import json
from pathlib import Path
from filelock import FileLock
from .util import FQDBError, atomic_json

INTERNAL = ("state", "staging", "imports", "logs")
LAYOUT_MARKER = {"schema_version": 3, "mode": "canonical"}


def runtime_path(root, name):
    root = Path(root)
    if name == "catalog":
        return root / "catalog"
    if name == "config":
        return root / ".state/config.json"
    if name in INTERNAL:
        return root / ".state" / name
    raise FQDBError(f"Unknown runtime directory: {name}")


def detect_layout(root):
    root = Path(root)
    obsolete = [
        name
        for name in (*INTERNAL, "bundles", "config.json", ".fqdb-layout.lock")
        if (root / name).exists() or (root / name).is_symlink()
    ]
    if obsolete:
        raise FQDBError(
            f"Unsupported obsolete data layout: {obsolete}; use a data root with a magnet catalog"
        )
    marker = root / ".state/layout.json"
    if not marker.exists():
        if (root / ".state/config.json").exists():
            raise FQDBError("Unrecognized data root: current layout marker is missing")
        return "empty"
    try:
        value = json.loads(marker.read_text())
    except (ValueError, OSError) as exc:
        raise FQDBError("Invalid current layout marker") from exc
    if value != LAYOUT_MARKER:
        raise FQDBError("Unsupported data layout; expected the magnet catalog layout (version 3)")
    return "canonical"


def ensure_layout(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    detect_layout(root)
    (root / ".state").mkdir(exist_ok=True)
    with FileLock(root / ".state/layout.lock", timeout=30):
        mode = detect_layout(root)
        for name in ("catalog", "data", *(f".state/{n}" for n in INTERNAL)):
            (root / name).mkdir(parents=True, exist_ok=True)
        if mode == "empty":
            atomic_json(root / ".state/layout.json", LAYOUT_MARKER)
    return "canonical", runtime_path(root, "config")
