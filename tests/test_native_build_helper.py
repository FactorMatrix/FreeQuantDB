"""Native build inputs remain verified and bounded before executing upstream code."""

import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tarfile

import pytest

SPEC = importlib.util.spec_from_file_location(
    "fqdb_native_builder_test", Path(__file__).resolve().parents[1] / "scripts/build_libtorrent.py"
)
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


def source_archive(root, *, name="package/value.txt", link=None):
    path = root / "synthetic-source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        item = tarfile.TarInfo(name)
        if link:
            item.type = tarfile.SYMTYPE
            item.linkname = link
            archive.addfile(item)
        else:
            body = b"SYNTHETIC source archive; not market data"
            item.size = len(body)
            archive.addfile(item, io.BytesIO(body))
    source = builder.Source(
        path.name,
        "https://example.invalid/never-requested",
        hashlib.sha256(path.read_bytes()).hexdigest(),
        "package",
    )
    return source, path


def test_native_sources_use_verified_cache_without_network(data_root, monkeypatch):
    source, archive = source_archive(data_root)
    downloads = data_root / "cache/downloads"
    downloads.mkdir(parents=True)
    cached = downloads / source.name
    cached.write_bytes(archive.read_bytes())
    monkeypatch.setattr(
        builder.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("verified offline cache must not request network"),
    )
    assert builder.archive_file(source, data_root / "cache", offline=True) == cached
    destination = data_root / "extracted"
    # Parent source tarballs contain empty git-submodule directory placeholders.
    destination.mkdir()
    assert builder.extract_source(source, cached, destination) == destination
    assert (destination / "value.txt").read_bytes().startswith(b"SYNTHETIC source")
    assert builder.extract_source(source, cached, destination) == destination
    cached.write_bytes(b"damaged cached archive")
    with pytest.raises(ValueError, match="checksum mismatch"):
        builder.archive_file(source, data_root / "cache", offline=True)
    assert cached.read_bytes() == b"damaged cached archive"


@pytest.mark.parametrize(
    "name,link", [("package/../escape", None), ("package/link", "../../escape"), ("wrong-root/value", None)]
)
def test_native_archive_rejects_escape_and_unexpected_root(data_root, name, link):
    source, archive = source_archive(data_root, name=name, link=link)
    with pytest.raises(ValueError):
        builder.extract_source(source, archive, data_root / "extracted")
    assert not (data_root / "escape").exists()
    assert not (data_root / "extracted").exists()


def test_native_builder_preserves_unverified_existing_source(data_root):
    source, archive = source_archive(data_root)
    destination = data_root / "existing"
    destination.mkdir()
    sentinel = destination / "user-file"
    sentinel.write_bytes(b"owned pre-existing file")
    with pytest.raises(FileExistsError, match="Unverified source"):
        builder.extract_source(source, archive, destination)
    assert sentinel.read_bytes() == b"owned pre-existing file"
