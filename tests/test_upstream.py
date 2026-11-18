from pathlib import Path
import errno

import pytest

from fqdb.adapters import FetchRequest, adapter
from fqdb.config import Config
from test_contracts import sample_schema
from fqdb.catalog.magnet import MagnetCatalog
from fqdb.publish.publisher import Publisher
from fqdb.storage.metadata import Store
from fqdb.storage.query import get_price
from fqdb.util import FQDBError, install_immutable


def test_synthetic_and_local_upstream_publish_query_scan(data_root):
    store = Store(Config(data_root).ensure().root)
    schema = sample_schema(days=["2024-01-02", "2024-01-03"])
    store.register(schema)
    request = FetchRequest(schema.id, "1d", "2024-01-02", "2024-01-03", ("000100.SZ",))
    provider = adapter("synthetic", {"days": ["2024-01-02", "2024-01-03"]})
    data = provider.fetch(request, schema)
    assert len(data) == 2 and provider.availability()["status"] == "synthetic"
    source = data_root / ".state/imports/SYNTHETIC-adapter.csv"
    data.to_csv(source, index=False)
    imported = adapter("local", {"path": str(source)}).fetch(request, schema)
    assert len(imported) == 2
    Publisher(store).publish(schema.id, "1d", source)
    assert len(get_price(store, "000100.SZ", "2024-01-02", "2024-01-03")) == 2
    first = store.entries()[0]
    original = (data_root / first.file).read_bytes()
    with store.db() as c:
        c.execute("DELETE FROM active")
        c.execute("DELETE FROM entries")
    result = MagnetCatalog(data_root).scan(deep=True)
    assert len(result["packages"]) == 2 and store.active() == []
    assert all(
        p["local_status"] == "available" and p["coverage_status"] == "verified" for p in result["packages"]
    )
    assert (data_root / first.file).read_bytes() == original


def test_upstream_refuses_unknown_adapters_and_fabrication(data_root):
    schema = sample_schema()
    request = FetchRequest(schema.id, "1d", "2024-01-02", "2024-01-03", ("000100.SZ",))
    with pytest.raises(FQDBError, match="Unknown"):
        adapter("unknown-provider")
    schema.synthetic = False
    schema.source = "No real provider configured"
    with pytest.raises(FQDBError, match="synthetic"):
        adapter("synthetic", {"days": ["2024-01-02"]}).fetch(request, schema)
    unknown = Config(data_root).ensure().root / "data/UNKNOWN.parquet"
    unknown.write_bytes(b"clearly-not-real-market-data")
    result = MagnetCatalog(data_root).scan()
    assert not result["packages"] and unknown.is_file()


def test_exdev_publish_is_exclusive_and_keeps_complete_bytes(data_root, monkeypatch):
    source = data_root / "source.bin"
    target = data_root / "other/file.bin"
    source.write_bytes(b"SYNTHETIC cross-device fixture" * 100)
    import os

    real_link = os.link

    def link(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "cross-device link")
        return real_link(src, dst)

    monkeypatch.setattr(os, "link", link)
    install_immutable(source, target)
    assert target.read_bytes() == source.read_bytes()
    before = target.read_bytes()
    source.write_bytes(b"do not overwrite")
    with pytest.raises(FileExistsError):
        install_immutable(source, target)
    assert target.read_bytes() == before


def test_publish_cancellation_preserves_completed_daily_catalog(data_root):
    store = Store(Config(data_root).ensure().root)
    schema = sample_schema(days=["2024-01-02", "2024-01-03"])
    store.register(schema)
    request = FetchRequest(schema.id, "1d", "2024-01-02", "2024-01-03", ("000100.SZ",))
    data = adapter("synthetic", {"days": ["2024-01-02", "2024-01-03"]}).fetch(request, schema)
    source = data_root / ".state/imports/SYNTHETIC-cancel.csv"
    data.to_csv(source, index=False)

    def cancel_after_first_day(done, total, detail):
        if store.entries():
            raise FQDBError("explicit cooperative cancellation")

    with pytest.raises(FQDBError, match="cancellation"):
        Publisher(store).publish(schema.id, "1d", source, progress=cancel_after_first_day)
    entries = store.entries()
    assert len(entries) == 1 and entries[0].trading_date == "2024-01-02"
    import json

    from fqdb.bt.torrents import magnet_hash
    from fqdb.catalog.magnet import package_path

    descriptor = package_path(f"{schema.id}/1d", entries[0].period)
    catalog = json.loads((data_root / "catalog" / descriptor).read_text())
    assert magnet_hash(catalog["magnet"]) == entries[0].info_hash
    assert len(get_price(store, "000100.SZ", "2024-01-02", "2024-01-02")) == 1
    assert not (data_root / "data/cn/stock/base/1d/2024/01/20240103.parquet").exists()
