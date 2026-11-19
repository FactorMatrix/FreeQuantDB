"""Verify that a selected pathname still refers to its original data storage."""

from pathlib import Path
import os
import re
import stat
import sys

from .util import FQDBError


class DataRootUnavailable(FQDBError):
    def __init__(self, root, detail):
        super().__init__(
            f"Data directory unavailable: {root}. {detail}. Restore its storage or select another data directory."
        )


def directory_stamp(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(f"{path} is not a directory")
    return info.st_dev, info.st_ino


def file_stamp(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"{path} is not a regular file")
    return info.st_dev, info.st_ino, info.st_mtime_ns


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _mounts():
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) >= 6 and right:
            yield fields[2], Path(_unescape(fields[4])), _unescape(fields[3]), right.split()[0]


def _configured_mounts():
    try:
        lines = Path("/etc/fstab").read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        fields = line.partition("#")[0].split()
        if len(fields) >= 4 and fields[1].startswith("/"):
            yield Path(_unescape(fields[1]))


def check_configured_mount(root):
    """Protect a first use before FQDB has recorded the storage in its own configuration."""
    if not sys.platform.startswith("linux"):
        return
    expected = max(
        (path for path in _configured_mounts() if root.is_relative_to(path)),
        key=lambda p: len(p.parts),
        default=None,
    )
    if (
        expected is not None
        and expected.parent != expected
        and not any(path == expected for _, path, _, _ in _mounts())
    ):
        raise DataRootUnavailable(root, f"the configured mount {expected} is not mounted")


def mount_info(root, device):
    """Read the active Linux mount without persisting reboot-dependent device numbers."""
    if not sys.platform.startswith("linux"):
        return None
    identity = f"{os.major(device)}:{os.minor(device)}"
    mounts = [
        (point, subtree, filesystem)
        for dev, point, subtree, filesystem in _mounts()
        if dev == identity and root.is_relative_to(point)
    ]
    if not mounts:
        raise OSError(f"Cannot identify mounted storage for {root}")
    location, filesystem_root, filesystem = max(mounts, key=lambda entry: len(entry[0].parts))
    return {"location": str(location), "root": filesystem_root, "filesystem": filesystem}
