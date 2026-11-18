"""Strict synthetic market publication and overlapping package identities."""

from pathlib import Path
import json

import pyarrow.parquet as pq
import pytest

from fqdb.catalog.magnet import MagnetCatalog
from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.publish.catalog import export_public_catalog
from fqdb.publish.publisher import Publisher
from fqdb.publish.paths import immutable_file, resolve_file
from fqdb.schema.market import arrow_schema
from fqdb.storage.metadata import Store
from fqdb.planning.planner import plan


@pytest.mark.parametrize("asset", ["stock", "etf", "future"])
@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_all_market_assets_exact_types_and_day_month_year(data_root, asset, frequency):
    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-13", "2026-11-17"]
    schema = market_schema(asset, days=days, frequency=frequency, rows_per_day=2 if frequency == "1m" else 1)
    store.register(schema)
    frame = market_frame(asset, frequency, days=days, n=2, night=asset == "future")
    published = Publisher(store).publish(schema.id, frequency, frame)
    assert len(published["entries"]) == 2
    for body in published["entries"]:
        entry = Entry(**body)
        assert Path(entry.file).name == entry.trading_date.replace("-", "") + ".parquet"
        assert ".v" not in entry.file
        assert pq.read_schema(data_root / entry.file) == arrow_schema(schema, frequency)
        assert len(pq.read_schema(data_root / entry.file)) == (9 if frequency == "1d" else 8)
        parquet = pq.ParquetFile(data_root / entry.file)
        assert entry.day_groups == {entry.trading_date: list(range(parquet.num_row_groups))}
        physical = parquet.read().to_pandas()
        assert physical.equals(
            physical.sort_values(schema.for_frequency(frequency).primary_key).reset_index(drop=True)
        )
        assert immutable_file(data_root, entry).is_file()
        assert resolve_file(data_root, entry).read_bytes() == immutable_file(data_root, entry).read_bytes()
    month = Entry(**Publisher(store).aggregate(schema.id, frequency, "month", "202611")["entries"][0])
    year = Entry(**Publisher(store).aggregate(schema.id, frequency, "year", "2026")["entries"][0])
    assert Path(month.file).name == "202611.parquet"
    assert Path(year.file).name == "2026.parquet"
    assert len({month.info_hash, year.info_hash, *[e["info_hash"] for e in published["entries"]]}) == 4
    assert set(month.day_groups) == set(days)
    from fqdb.validation.files import validate_parquet

    for period_entry in (month, year):
        physical = pq.read_table(data_root / period_entry.file).to_pandas()
        order = (
            ["trading_date", "instrument"]
            if frequency == "1d"
            else schema.for_frequency(frequency).primary_key
        )
        assert physical.equals(physical.sort_values(order).reset_index(drop=True))
        if frequency == "1d":
            assert period_entry.day_groups == {day: [index] for index, day in enumerate(days)}
        assert validate_parquet(data_root / period_entry.file, period_entry, schema) == len(physical)
    assert set(month.daily_sources) == set(days)
    assert len(store.entries(schema.id, frequency, "2026-11-17", "2026-11-17")) == 3
    monthly = plan(store, schema.id, frequency, year=2026, month=11)
    assert [item["entry"]["info_hash"] for item in monthly["items"]] == [month.info_hash]
    assert monthly["fallback_required"] is False
    if frequency == "1m" and asset == "future":
        actual = pq.read_table(data_root / month.file).to_pandas()
        assert actual.trading_time.iloc[0].date().isoformat() == "2026-11-12"
        assert "trading_date" not in actual and "prev_close" not in actual
    # Re-running an unchanged publication leaves every content identity alone.
    before = (data_root / month.file).stat().st_mtime_ns
    again = Entry(**Publisher(store).aggregate(schema.id, frequency, "month", "202611")["entries"][0])
    assert again.info_hash == month.info_hash and (data_root / month.file).stat().st_mtime_ns == before


def test_period_requires_explicit_daily_fallback_and_export_preserves_periods(data_root):
    Config(data_root).ensure()
    store = Store(data_root)
    schema = market_schema(days=["2026-11-13"])
    store.register(schema)
    Publisher(store).publish(schema.id, "1d", market_frame(days=["2026-11-13"]))
    result = plan(store, schema.id, "1d", year=2026, month=11, granularity="month")
    assert result["fallback_required"] and result["items"] == []
    expanded = plan(store, schema.id, "1d", year=2026, month=11, allow_daily_fallback=True)
    assert expanded["daily_fallback_selected"] and len(expanded["items"]) == 1
    Publisher(store).aggregate(schema.id, "1d", "year", "2026")
    target = data_root / "local-catalog-review"
    exported = export_public_catalog(store, target)
    assert exported["entries"] == 2
    client = data_root / "SYNTHETIC-client"
    Config(client).ensure()
    catalog = MagnetCatalog(client)
    catalog.sync(str(target))
    assert {e["period_kind"] for e in catalog.scan()["packages"]} == {"day", "year"}
    assert len(json.loads((target / "index.json").read_text())["packages"]) == 2


@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_source_parquet_shared_row_group_is_repartitioned_by_explicit_day(data_root, frequency):
    import pyarrow as pa

    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-13", "2026-11-17"]
    schema = market_schema(days=days, frequency=frequency)
    store.register(schema)
    frame = market_frame(frequency=frequency, days=days)
    source = data_root / ".state/imports/SYNTHETIC-multi-day-source.parquet"
    # Deliberately include Pandas attrs and two days in one source row group.
    # Publication must not mistake the retained full attrs for a per-day proof.
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), source)
    proof = frame.attrs["trading_dates"] if frequency == "1m" else None
    body = Publisher(store).publish(
        schema.id, frequency, source, granularity="month", period="202611", trading_dates=proof
    )
    entry = Entry(**body["entries"][0])
    assert set(entry.day_groups) == set(days)
    parquet = pq.ParquetFile(data_root / entry.file)
    groups_per_day = 1 if frequency == "1d" else len(entry.symbols)
    assert parquet.num_row_groups == groups_per_day * len(days)
    physical = parquet.read().to_pandas()
    order = (
        ["trading_date", "instrument"] if frequency == "1d" else schema.for_frequency(frequency).primary_key
    )
    assert physical.equals(physical.sort_values(order).reset_index(drop=True))
    from fqdb.validation.files import validate_parquet

    assert validate_parquet(data_root / entry.file, entry, schema) == len(physical)
