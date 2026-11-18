"""Build a native libtorrent wheel for this CPython on Linux.

Uses unmodified, SHA-256 pinned official sources. Boost and libtorrent are
statically linked; OpenSSL is supplied by the host build toolchain. The
result uses an honest native Linux wheel tag, without ABI retagging.
Nothing is installed into the invoking Python or uploaded anywhere.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request
import zipfile


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    sha256: str
    prefix: str


BOOST = Source(
    "boost_1_92_0.tar.bz2",
    "https://archives.boost.io/release/1.92.0/source/boost_1_92_0.tar.bz2",
    "5c1d40cb8e19adbf740a4ec2da35b3e58f3f5804b1dce44deb53df72193cbc6c",
    "boost_1_92_0",
)
TORRENT_COMMIT = "56ae8caba38bf154ffc210403cb23f91d0ecaa49"
TORRENT = Source(
    "libtorrent-2.1.1.tar.gz",
    f"https://codeload.github.com/arvidn/libtorrent/tar.gz/{TORRENT_COMMIT}",
    "29127305cca0def7f5327f826957dd58c17ef0965e9d5cd899ad37bd9859320b",
    f"libtorrent-{TORRENT_COMMIT}",
)
TRY_COMMIT = "105cce59972f925a33aa6b1c3109e4cd3caf583d"
TRY_SIGNAL = Source(
    f"try_signal-{TRY_COMMIT}.tar.gz",
    f"https://codeload.github.com/arvidn/try_signal/tar.gz/{TRY_COMMIT}",
    "6f111b0d77429a8051be4faed06cf23fb03cf6ee233967c84fdd9d8d3b42ba8e",
    f"try_signal-{TRY_COMMIT}",
)
RTC_COMMIT = "a02b751917ac8afc8c58dc6f4461d25ff9465d48"
LIBDATACHANNEL = Source(
    f"libdatachannel-{RTC_COMMIT}.tar.gz",
    f"https://codeload.github.com/paullouisageneau/libdatachannel/tar.gz/{RTC_COMMIT}",
    "12a126ac7cab6bfb4a2c90c9c105ba86687870f793f986a4e64f2351946294d3",
    f"libdatachannel-{RTC_COMMIT}",
)
SOURCES = (BOOST, TORRENT, TRY_SIGNAL, LIBDATACHANNEL)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def archive_file(source: Source, cache: Path, offline: bool = False) -> Path:
    target = cache / "downloads" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if digest(target) != source.sha256:
            raise ValueError(f"Cached archive checksum mismatch: {target}")
        return target
    if offline:
        raise FileNotFoundError(f"Offline source archive missing: {target}")
    # A failed download is never accepted as a completed cache entry.
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=source.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            print(f"Downloading {source.name}", flush=True)
            with urllib.request.urlopen(source.url, timeout=60) as response:
                shutil.copyfileobj(response, stream)
            stream.flush()
            os.fsync(stream.fileno())
            if digest(temporary) != source.sha256:
                raise ValueError(f"Downloaded source checksum mismatch: {source.url}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def contained(root: Path, relative: str) -> Path:
    value = PurePosixPath(relative)
    if value.is_absolute() or ".." in value.parts or "\\" in relative:
        raise ValueError(f"Unsafe archive path: {relative}")
    target = root.joinpath(*value.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Archive path escapes source directory: {relative}")
    return target


def extract_source(source: Source, archive: Path, destination: Path) -> Path:
    marker = destination / ".fqdb-native-source.json"
    expected = {"url": source.url, "sha256": source.sha256}
    if destination.is_dir() and not any(destination.iterdir()):
        destination.rmdir()  # An empty git-submodule placeholder from its parent archive.
    if destination.exists():
        if marker.is_file() and json.loads(marker.read_text()) == expected:
            return destination
        raise FileExistsError(
            f"Unverified source directory already exists: {destination}; choose a new cache"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="fqdb-source-") as scratch:
        scratch_root = Path(scratch)
        with tarfile.open(archive) as package:
            for member in package:
                parts = PurePosixPath(member.name).parts
                if not parts or parts[0] != source.prefix:
                    raise ValueError(f"Unexpected archive root: {member.name}")
                target = contained(scratch_root, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(exist_ok=True)
                elif member.isfile():
                    content = package.extractfile(member)
                    if content is None:
                        raise ValueError(f"Missing archive body: {member.name}")
                    with content, target.open("wb") as output:
                        shutil.copyfileobj(content, output)
                    target.chmod(member.mode & 0o777)
                    os.utime(target, (member.mtime, member.mtime))
                elif member.issym():
                    if Path(member.linkname).is_absolute():
                        raise ValueError(f"Absolute archive symlink: {member.name}")
                    linked = (target.parent / member.linkname).resolve()
                    if not linked.is_relative_to(scratch_root.resolve()):
                        raise ValueError(f"Archive symlink escapes source directory: {member.name}")
                    target.symlink_to(member.linkname)
                elif member.islnk():
                    os.link(contained(scratch_root, member.linkname), target)
                else:
                    raise ValueError(f"Unsupported archive member: {member.name}")
        extracted = scratch_root / source.prefix
        (extracted / marker.name).write_text(json.dumps(expected, sort_keys=True), encoding="utf-8")
        extracted.rename(destination)
    return destination


def run(command: list[str], cwd: Path, environment: dict[str, str]) -> None:
    print("Running " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def verify_wheel(wheel: Path, workspace: Path) -> dict:
    with tempfile.TemporaryDirectory(dir=workspace, prefix="verify-") as scratch:
        root = Path(scratch)
        with zipfile.ZipFile(wheel) as package:
            for name in package.namelist():
                contained(root, name)
            package.extractall(root)
        check = r"""
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path.insert(0,str(root))
import libtorrent as lt
assert Path(lt.__file__).is_relative_to(root),lt.__file__
assert lt.__version__.startswith('2.1.1'),lt.__version__
info=lt.torrent_info(lt.bencode({b'info':{b'name':b'SYNTHETIC-metadata-only',b'length':1,b'piece length':16384,b'pieces':b'\0'*20}}))
assert info.info_section()
session=lt.session({'listen_interfaces':'127.0.0.1:0','enable_dht':False,'enable_lsd':False,'enable_upnp':False,'enable_natpmp':False})
category=session.create_peer_class('fqdb-build-verification')
settings=session.get_peer_class(category)
settings['upload_priority']=8
session.set_peer_class(category,settings)
assert session.get_peer_class(category)['upload_priority']==8
session.delete_peer_class(category)
session.pause()
print(json.dumps({'libtorrent':lt.__version__,'python':sys.version.split()[0],'metadata_and_peer_class':'passed'}))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", check, str(root)],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        verified = json.loads(result.stdout)
        ldd = shutil.which("ldd")
        if ldd:
            native = next(root.rglob("*.so"))
            dependencies = subprocess.run(
                [ldd, str(native)], capture_output=True, text=True, check=True
            ).stdout
            if (
                "not found" in dependencies
                or "libboost" in dependencies
                or "libtorrent-rasterbar" in dependencies
            ):
                raise RuntimeError("Unexpected unresolved or non-static native dependency: " + dependencies)
            verified["dynamic_libraries"] = [
                line.split()[0] for line in dependencies.splitlines() if line.strip()
            ]
        return verified


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--offline", action="store_true", help="Use only already verified source archives")
    args = parser.parse_args()
    if sys.version_info < (3, 11) or platform.python_implementation() != "CPython":
        parser.error("Use a CPython 3.11 or later interpreter")
    if sys.platform != "linux":
        parser.error("This helper currently builds Linux wheels; use upstream instructions on other systems")
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        parser.error("Free-threaded interpreters are not verified by this helper")
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if shutil.which("g++") is None or shutil.which("make") is None:
        parser.error("Install a C++ compiler and make before building native dependencies")
    try:
        import setuptools  # noqa: F401
        import wheel  # noqa: F401
    except ImportError:
        parser.error("Install setuptools and wheel in the invoking interpreter first")
    default_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "fqdb" / "native"
    cache = (
        (args.cache_dir or Path(os.environ.get("FQDB_NATIVE_CACHE", default_cache))).expanduser().resolve()
    )
    wheel_dir = args.wheel_dir.expanduser().resolve()
    wheel_dir.mkdir(parents=True, exist_ok=True)
    archives = {source.name: archive_file(source, cache, args.offline) for source in SOURCES}
    boost = extract_source(BOOST, archives[BOOST.name], cache / BOOST.prefix)
    source = extract_source(TORRENT, archives[TORRENT.name], cache / "libtorrent-source-2.1.1")
    extract_source(TRY_SIGNAL, archives[TRY_SIGNAL.name], source / "deps/try_signal")
    extract_source(LIBDATACHANNEL, archives[LIBDATACHANNEL.name], source / "deps/libdatachannel")
    environment = os.environ.copy()
    environment.update(
        {
            "BOOST_ROOT": str(boost),
            "BOOST_BUILD_PATH": str(boost / "tools/build"),
            "PATH": str(boost) + os.pathsep + environment.get("PATH", ""),
            "B2_EXTRA_ARGS": "",
        }
    )
    if not (boost / "b2").exists():
        run(
            [
                "./bootstrap.sh",
                "--with-toolset=gcc",
                "--with-libraries=python",
                "--with-python=" + sys.executable,
            ],
            boost,
            environment,
        )
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    workspace = cache / "targets" / tag
    workspace.mkdir(parents=True, exist_ok=True)
    # Isolate setup.py's bdist staging directories between interpreter versions.
    target_source = workspace / "libtorrent"
    if not target_source.exists():
        shutil.copytree(
            source,
            target_source,
            ignore=shutil.ignore_patterns(".git", "build", "bin", "*.egg-info", "__pycache__"),
        )
    arguments = shlex.join(
        [
            "libtorrent-link=static",
            "boost-link=static",
            "crypto=openssl",
            "webtorrent=off",
            "cxxstd=17",
            f"-j{args.jobs}",
            "--build-dir=" + str(cache / "objects" / tag),
        ]
    )
    run(
        [
            sys.executable,
            "setup.py",
            "build_ext",
            "--b2-args=" + arguments,
            "bdist_wheel",
            "--dist-dir=" + str(wheel_dir),
        ],
        target_source / "bindings/python",
        environment,
    )
    candidates = list(wheel_dir.glob(f"libtorrent-2.1.1-{tag}-{tag}-*.whl"))
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one wheel for {tag}: {candidates}")
    result = verify_wheel(candidates[0], workspace)
    result.update(
        {
            "wheel": str(candidates[0]),
            "sha256": digest(candidates[0]),
            "platform": platform.platform(),
            "boost": "1.92.0",
            "torrent_commit": TORRENT_COMMIT,
            "jobs": args.jobs,
            "linking": "static Boost/libtorrent; system-toolchain OpenSSL; optional WebTorrent disabled",
            "sources": {source.name: source.sha256 for source in SOURCES},
        }
    )
    (wheel_dir / f"native-build-{tag}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
