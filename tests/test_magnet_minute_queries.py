"""Synthetic minute packages use attached source dates when read offline."""

from pathlib import Path
import shutil

import pyarrow.parquet as pq
import pytest

import fqdb
from fqdb.bt.torrents import create_metadata, torrent_cache_path
from fqdb.catalog.magnet import MagnetCatalog
from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.publish.publisher import Publisher
from fqdb.schema.registry import base_dataset
from fqdb.schema.trading_days import MINUTE_DAY_INDEX, minute_day_groups
from fqdb.storage.metadata import Store
from fqdb.storage.query import MissingDataError, get_price
from fqdb.util import atomic_bytes
from fqdb.validation.coverage import coverage
from fqdb.validation.files import inspect_file

DATASET = "cn/future/base"
DAYS = ["2026-11-13", "2026-11-17"]
SYMBOL = "RB2701.SHFE"


def _published(root):
    sender = Store(Config(root).ensure().root)
    sender.register(market_schema("future", DAYS, symbols=[SYMBOL]))
    frame = market_frame("future", "1m", DAYS, symbols=[SYMBOL], night=True)
    value = Publisher(sender).publish(DATASET, "1m", frame, granularity="month", period="202611")
    entry = Entry(**value["entries"][0])
    return sender, entry


def _receiver(root):
    store = Store(Config(root).ensure().root)
    store.register(base_dataset("future"))
    return store


def _activate(store, path):
    identity, payload = create_metadata(path)
    entry = store.add_magnet(Entry.from_magnet(DATASET, "1m", "202611", "magnet:?xt=urn:btih:" + identity))
    atomic_bytes(torrent_cache_path(store.root, entry), payload)
    return store.activate_bt(entry, path, path.stat().st_size)


def test_downloaded_minute_package_uses_official_session_dates_from_footer(data_root):
    producer, source = _published(data_root / "synthetic-producer")
    receiver = _receiver(data_root / "synthetic-consumer")
    path = receiver.root / ".state/staging" / Path(source.file).name
    shutil.copyfile(producer.root / source.file, path)
    with pq.ParquetFile(path) as parquet:
        assert minute_day_groups(parquet, "2026-11-01", "2026-11-30") == source.day_groups
        assert "trading_date" not in parquet.schema_arrow.names
    entry = _activate(receiver, path)
    assert entry.info_hash == source.info_hash and entry.sparse and not entry.days
    first = get_price(receiver, SYMBOL, DAYS[0], DAYS[0], "1m", ["trading_time"], dataset=DATASET)
    assert len(first) == 1 and first.trading_time.iloc[0].date().isoformat() == "2026-11-12"
    assert first.attrs["selected_sources"] == {DAYS[0]: entry.info_hash}
    all_dates = fqdb.load(DATASET, frequency="1m", fields=["trading_time"], data_dir=receiver.root)
    assert set(all_dates.attrs["selected_sources"]) == set(DAYS)

    catalog = MagnetCatalog(receiver.root)
    catalog.import_local(producer.root / "catalog")
    package = catalog.scan(deep=True)["packages"][0]
    assert package["coverage_status"] == "verified"
    assert package["observed"]["start_date"] == DAYS[0] and package["observed"]["trading_days"] == 2
    schema = market_schema("future", DAYS, symbols=[SYMBOL])
    audit = inspect_file(receiver.root, entry, schema)
    assert audit["trading_days"] == DAYS and audit["rows"] == 2
    report = coverage(
        schema,
        "1m",
        [entry],
        receiver.active(),
        DAYS[0],
        DAYS[-1],
        receiver.root,
        deep=True,
        observed_days={entry.info_hash: audit["trading_days"]},
    )
    assert [row["actual"] for row in report["instruments"]] == [1, 1]


@pytest.mark.parametrize("metadata", [None, b'[["2026-11-13",[1]]]'])
def test_minute_data_without_valid_footer_is_never_inferred(data_root, metadata):
    producer, source = _published(data_root / "synthetic-producer")
    receiver = _receiver(data_root / "synthetic-consumer")
    path = receiver.root / ".state/staging" / Path(source.file).name
    table = pq.read_table(producer.root / source.file).replace_schema_metadata(None)
    with pq.ParquetWriter(path, table.schema) as writer:
        writer.write_table(table)
        if metadata is not None:
            writer.add_key_value_metadata({MINUTE_DAY_INDEX: metadata})
    entry = _activate(receiver, path)
    with pytest.raises(MissingDataError, match="trading-day row-group index|minute Parquet row group"):
        get_price(receiver, SYMBOL, DAYS[0], DAYS[-1], "1m", dataset=DATASET)
    audit = inspect_file(receiver.root, entry, receiver.dataset(DATASET))
    assert "trading_days" not in audit
    assert audit["quality"] == ("invalid" if metadata else "pending")
