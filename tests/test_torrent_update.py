"""A canonical name can change safely while its former immutable seed survives."""

import pytest

from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.publish.publisher import Publisher
from fqdb.publish.paths import immutable_file, resolve_file
from fqdb.bt.torrents import torrent_cache_path, validate_daily, verify_pieces
from fqdb.storage.metadata import Store
from fqdb.util import FQDBError


def descriptor(root, entry):
    return root / "catalog" / (entry.file.removeprefix("data/") + ".json")


def test_revision_preserves_previous_swarm_and_marks_aggregates_stale(data_root):
    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-13"]
    schema = market_schema(days=days)
    store.register(schema)
    first = Entry(**Publisher(store).publish(schema.id, "1d", market_frame(days=days))["entries"][0])
    month = Entry(**Publisher(store).aggregate(schema.id, "1d", "month", "202611")["entries"][0])
    old_bytes, old_torrent = (
        immutable_file(data_root, first).read_bytes(),
        torrent_cache_path(data_root, first).read_bytes(),
    )
    second = Entry(
        **Publisher(store).publish(schema.id, "1d", market_frame(days=days, correction=1))["entries"][0]
    )
    assert first.file == second.file
    assert first.info_hash != second.info_hash and first.sha256 != second.sha256
    assert second.version == 2 and ".v" not in second.file
    assert immutable_file(data_root, first).read_bytes() == old_bytes
    assert torrent_cache_path(data_root, first).read_bytes() == old_torrent
    assert verify_pieces(validate_daily(old_torrent, first, data_root), resolve_file(data_root, first))
    assert store.aggregate_stale(month)
    rebuilt = Entry(**Publisher(store).aggregate(schema.id, "1d", "month", "202611")["entries"][0])
    assert rebuilt.info_hash != month.info_hash and not store.aggregate_stale(rebuilt)
    current = data_root / second.file
    current.unlink()
    assert resolve_file(data_root, second) == current and not resolve_file(data_root, second).exists()
    assert resolve_file(data_root, first).exists()


def test_failed_native_verification_keeps_old_visible_bytes_and_catalog(data_root):
    Config(data_root).ensure()
    store = Store(data_root)
    schema = market_schema(days=["2026-11-13"])
    store.register(schema)
    first = Entry(
        **Publisher(store).publish(schema.id, "1d", market_frame(days=["2026-11-13"]))["entries"][0]
    )
    before = (data_root / first.file).read_bytes()
    catalog = descriptor(data_root, first).read_bytes()

    def fail(_):
        raise FQDBError("SYNTHETIC native seed verification failed")

    with pytest.raises(FQDBError, match="SYNTHETIC"):
        Publisher(store, verify_seed=fail).publish(
            schema.id, "1d", market_frame(days=["2026-11-13"], correction=1)
        )
    assert (data_root / first.file).read_bytes() == before
    assert descriptor(data_root, first).read_bytes() == catalog
    assert store.active(schema.id, "1d")[0][0].info_hash == first.info_hash


def test_later_daily_publication_invalidates_previous_partial_period(data_root):
    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-13", "2026-11-17"]
    schema = market_schema(days=days)
    store.register(schema)
    Publisher(store).publish(schema.id, "1d", market_frame(days=[days[0]]))
    month = Entry(**Publisher(store).aggregate(schema.id, "1d", "month", "202611")["entries"][0])
    assert not store.aggregate_stale(month)
    Publisher(store).publish(schema.id, "1d", market_frame(days=[days[1]]))
    assert store.aggregate_stale(month)


def test_complete_claim_checks_every_declared_instrument_and_record_count(data_root):
    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2010-01-04"]
    schema = market_schema(days=days)
    schema.calendars["1d"] = ["2009-12-31", *days, "2010-02-01"]
    store.register(schema)
    subset = market_frame(days=days, symbols=["000100.SZ"])
    with pytest.raises(FQDBError, match="Incomplete record coverage"):
        Publisher(store).publish(schema.id, "1d", subset, granularity="month", period="201001", complete=True)
    assert store.entries() == []
    result = Publisher(store).publish(
        schema.id, "1d", market_frame(days=days), granularity="month", period="201001", complete=True
    )
    assert result["entries"][0]["complete"]


def test_catalog_failure_restores_old_query_and_package_description(data_root, monkeypatch):
    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-13"]
    schema = market_schema(days=days)
    store.register(schema)
    first = Entry(**Publisher(store).publish(schema.id, "1d", market_frame(days=days))["entries"][0])
    original = (data_root / first.file).read_bytes()
    original_description = descriptor(data_root, first).read_bytes()
    import fqdb.publish.publisher as publisher_module

    def fail(*_):
        raise OSError("SYNTHETIC catalog write failure")

    monkeypatch.setattr(publisher_module, "write_publication", fail)
    with pytest.raises(OSError, match="SYNTHETIC"):
        Publisher(store).publish(schema.id, "1d", market_frame(days=days, correction=1))
    assert (data_root / first.file).read_bytes() == original
    assert descriptor(data_root, first).read_bytes() == original_description
    assert [e.info_hash for e in store.entries(schema.id)] == [first.info_hash]
    assert store.active(schema.id, "1d")[0][0].info_hash == first.info_hash


def test_pending_atomic_switch_recovers_old_bytes_after_interruption(data_root):
    from fqdb.publish.paths import atomic_link, journal_path
    from fqdb.util import atomic_json

    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-13"]
    schema = market_schema(days=days)
    store.register(schema)
    first = Entry(**Publisher(store).publish(schema.id, "1d", market_frame(days=days))["entries"][0])
    second = Entry(
        **Publisher(store).publish(schema.id, "1d", market_frame(days=days, correction=1))["entries"][0]
    )
    # Reconstruct the precise interruption window: visible new bytes, old DB pointer.
    with store.db() as c:
        stat = immutable_file(data_root, first).stat()
        c.execute(
            "UPDATE active SET hash=?,mtime_ns=?,size=? WHERE dataset=? AND frequency=? AND day=?",
            (first.info_hash, stat.st_mtime_ns, stat.st_size, *first.key),
        )
    atomic_link(immutable_file(data_root, second), data_root / second.file)
    atomic_json(journal_path(data_root, second), {"old": first.to_dict(), "new": second.to_dict()})
    recovered = Store(data_root)
    assert (data_root / first.file).read_bytes() == immutable_file(data_root, first).read_bytes()
    assert recovered.active(schema.id, "1d")[0][0].info_hash == first.info_hash
    assert not journal_path(data_root, second).exists()


def test_private_history_retains_each_original_magnet(data_root):
    import json

    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-13"]
    schema = market_schema(days=days)
    store.register(schema)
    first = Publisher(store).publish(schema.id, "1d", market_frame(days=days))["entries"][0]
    second = Publisher(store).publish(schema.id, "1d", market_frame(days=days, correction=1))["entries"][0]
    body = json.loads(descriptor(data_root, Entry(**second)).read_text())
    assert body["magnet"] == second["magnet"]
    assert {e.magnet for e in store.entries(latest=False)} == {first["magnet"], second["magnet"]}
    assert first["magnet"] != second["magnet"]


def test_cross_day_minute_duplicate_primary_key_is_rejected_before_publication(data_root):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from fqdb.schema.market import arrow_schema

    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-17", "2026-11-18"]
    symbols = ["RB2701.SHFE"]
    schema = market_schema("future", days=days, frequency="1m", symbols=symbols)
    store.register(schema)
    first = market_frame("future", "1m", [days[0]], symbols=symbols, night=True)
    second = market_frame("future", "1m", [days[1]], symbols=symbols, night=True)
    # Two separately valid source partitions claim the same minute PK belongs
    # to different trading days. Per-day validation alone cannot detect this.
    second["trading_time"] = first.trading_time
    source = data_root / ".state/imports/SYNTHETIC-cross-day-duplicate.parquet"
    with pq.ParquetWriter(source, arrow_schema(schema, "1m")) as writer:
        for data in (first, second):
            writer.write_table(
                pa.Table.from_pandas(
                    data, schema=arrow_schema(schema, "1m"), preserve_index=False
                ).replace_schema_metadata(None)
            )
    with pytest.raises(FQDBError, match="Conflicting trading-date proof for duplicate primary key"):
        Publisher(store).publish(
            schema.id,
            "1m",
            source,
            granularity="month",
            period="202611",
            trading_dates={days[0]: [0], days[1]: [1]},
        )
    assert store.entries() == [] and not list((data_root / "data").rglob("*.parquet"))


def test_interleaving_source_trading_day_ranges_are_rejected(data_root):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pandas as pd
    from fqdb.schema.market import arrow_schema

    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-17", "2026-11-18"]
    symbols = ["RB2701.SHFE"]
    schema = market_schema("future", days=days, frequency="1m", symbols=symbols, rows_per_day=2)
    store.register(schema)
    first = market_frame("future", "1m", [days[0]], symbols=symbols, n=2, night=True)
    second = market_frame("future", "1m", [days[1]], symbols=symbols, n=2, night=True)
    first.loc[1, "trading_time"] = first.loc[0, "trading_time"] + pd.Timedelta(minutes=2)
    second["trading_time"] = first.trading_time + pd.Timedelta(minutes=1)
    source = data_root / ".state/imports/SYNTHETIC-interleaved-trading-days.parquet"
    with pq.ParquetWriter(source, arrow_schema(schema, "1m")) as writer:
        for data in (first, second):
            writer.write_table(
                pa.Table.from_pandas(
                    data, schema=arrow_schema(schema, "1m"), preserve_index=False
                ).replace_schema_metadata(None)
            )
    with pytest.raises(FQDBError, match="Interleaving trading-day time ranges"):
        Publisher(store).publish(
            schema.id,
            "1m",
            source,
            granularity="year",
            period="2026",
            trading_dates={days[0]: [0], days[1]: [1]},
        )
    assert store.entries() == []
