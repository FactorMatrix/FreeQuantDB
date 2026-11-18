"""Canonical visible market paths and immutable native-seeding locations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from ..layout import runtime_path
from ..util import FQDBError, install_immutable, safe_path, sha256


def immutable_file(root, entry):
    return safe_path(Path(root), f".state/history/{entry.info_hash}/{Path(entry.file).name}")


def immutable_metadata(root, entry):
    return safe_path(Path(root), f".state/history/{entry.info_hash}/{Path(entry.file).name}.btmeta")


def current_hash(root, entry):
    db = runtime_path(root, "state") / "index.sqlite3"
    if not db.exists():
        return None
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30) as connection:
        row = connection.execute(
            "SELECT hash FROM active WHERE dataset=? AND frequency=? AND day=?", entry.key
        ).fetchone()
    return row[0] if row else None


def resolve_file(root, entry, *, immutable=False):
    if immutable or current_hash(root, entry) != entry.info_hash:
        return immutable_file(root, entry)
    # An active file must not fall back to a history copy when damaged/missing.
    return safe_path(Path(root), entry.file)


def install_snapshot(root, entry, source):
    target = immutable_file(root, entry)
    source = Path(source)
    if source.stat().st_size != entry.size or sha256(source) != entry.sha256:
        raise FQDBError("Staged market file does not match the trusted manifest")
    if target.exists():
        if target.stat().st_size != entry.size or sha256(target) != entry.sha256:
            # A canonical name and its content snapshot can share an inode.
            # Restore damaged bytes only after the replacement has proved the
            # exact same trusted content identity; never overwrite good history.
            atomic_link(source, target)
    else:
        install_immutable(source, target)
    return target


def install_bt_snapshot(root, entry, source):
    """Install bytes already verified by the native BitTorrent session."""
    target = immutable_file(root, entry)
    atomic_link(source, target, verify_copy=False)
    return target


def atomic_link(source, destination, *, verify_copy=True):
    """Replace one visible name without rewriting bytes used by old seed handles."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".fqdb-switch-")
    os.close(fd)
    temporary = Path(temporary)
    temporary.unlink()
    try:
        install_immutable(source, temporary, verify_copy=verify_copy)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def journal_path(root, entry):
    digest = hashlib.sha256(json.dumps(entry.key).encode()).hexdigest()
    return Path(root) / ".state/commits" / (digest + ".json")
