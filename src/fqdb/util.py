from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath


class FQDBError(ValueError):
    """An actionable data, configuration or task error."""


def now():
    return datetime.now(timezone.utc).isoformat()


def day(value):
    return date.fromisoformat(str(value)).isoformat()


def safe_path(root: Path, relative: str) -> Path:
    p = PurePosixPath(relative)
    if (
        not relative
        or p.is_absolute()
        or any(x in ("..", "") for x in p.parts)
        or "\\" in relative
        or ":" in relative
    ):
        raise FQDBError(f"Unsafe relative path: {relative}")
    target = root.joinpath(*p.parts).resolve()
    if not target.is_relative_to(root.resolve()):
        raise FQDBError(f"Path escapes data root: {relative}")
    return target


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_bytes(path: Path, value: bytes, immutable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fqdb-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        if immutable:
            # Hard-link creation fails atomically if the version already exists.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_json(path, value, immutable=False):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode(), immutable)


def install_immutable(source: Path, destination: Path, *, verify_copy=True):
    """Publish complete bytes exclusively, including when staging is on another FS."""
    import errno
    import shutil

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".fqdb-copy-")
    try:
        with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
            before = os.fstat(incoming.fileno())
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            after = os.fstat(incoming.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or os.fstat(
                outgoing.fileno()
            ).st_size != before.st_size:
                raise FQDBError("Cross-filesystem staging source changed while copying")
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        if verify_copy and sha256(source) != sha256(temporary):
            raise FQDBError("Cross-filesystem staging copy checksum mismatch")
        # The final link is within the target FS and fails if a version exists.
        # Unsupported exclusive publication is an explicit failure, never overwrite.
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
