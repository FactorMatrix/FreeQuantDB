"""Offline strict market queries and explicit old-data conversion."""

from dataclasses import replace

import pandas as pd
import pytest

from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.publish.publisher import Publisher
from fqdb.query import load
from fqdb.storage.metadata import Store
from fqdb.storage.query import MissingDataError, get_price
from fqdb.util import FQDBError


def market_store(root, asset="stock", frequency="1d", days=None, n=1):
    days = days or ["2026-11-13", "2026-11-17"]
    Config(root).ensure()
    store = Store(root)
    schema = market_schema(asset, days, frequency=frequency, rows_per_day=n)
    store.register(schema)
    return store, schema


@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_overlapping_packages_select_unique_daily_source_and_exact_projection(data_root, frequency):
    store, schema = market_store(data_root, frequency=frequency, n=2 if frequency == "1m" else 1)
    published = Publisher(store).publish(schema.id, frequency, market_frame(frequency=frequency, n=2))
    Publisher(store).aggregate(schema.id, frequency, "month", "202611")
    Publisher(store).aggregate(schema.id, frequency, "year", "2026")
    result = load(schema.id, frequency=frequency, data_dir=data_root, start="2026-11-13", end="2026-11-17")
    assert list(result.columns) == list(schema.fields_for(frequency))
    assert isinstance(result.index, pd.RangeIndex)
    assert not result.duplicated(schema.for_frequency(frequency).primary_key).any()
    assert len(result) == (6 if frequency == "1d" else 12)
    selected = result.attrs["selected_sources"]
    assert set(selected.values()) == {e["info_hash"] for e in published["entries"]}
    projected = load(
        schema.id,
        frequency=frequency,
        data_dir=data_root,
        fields=["close", "instrument"],
        start="2026-11-13",
        end="2026-11-13",
    )
    assert list(projected.columns) == ["close", "instrument"]
    if frequency == "1m":
        with pytest.raises(FQDBError, match="Unavailable"):
            load(
                schema.id,
                frequency=frequency,
                data_dir=data_root,
                fields=["prev_close"],
                start="2026-11-13",
                end="2026-11-13",
            )


def test_period_only_future_minute_uses_index_for_night_session(data_root):
    days = ["2026-11-17", "2026-11-18"]
    store, schema = market_store(data_root, "future", "1m", days, n=2)
    source = market_frame("future", "1m", days, n=2, night=True)
    result = Publisher(store).publish(schema.id, "1m", source, granularity="month", period="202611")
    assert len(store.entries()) == 1
    entry = Entry(**result["entries"][0])
    selected = load(schema.id, frequency="1m", data_dir=data_root, start=days[0], end=days[0])
    assert len(selected) == len(entry.symbols) * 2
    assert selected.trading_time.dt.date.astype(str).unique().tolist() == ["2026-11-16"]
    assert selected.attrs["selected_sources"] == {days[0]: entry.info_hash}
    narrowed = load(
        schema.id,
        frequency="1m",
        data_dir=data_root,
        start=days[0],
        end=days[0],
        start_time="2026-11-16T21:02:00+08:00",
        end_time="2026-11-16T21:02:00+08:00",
    )
    assert len(narrowed) == len(entry.symbols) and narrowed.trading_time.dt.minute.eq(2).all()


def test_conflicting_period_versions_do_not_silently_merge(data_root):
    days = ["2026-11-13"]
    store, schema = market_store(data_root, days=days)
    Publisher(store).publish(schema.id, "1d", market_frame(days=days), granularity="month", period="202611")
    Publisher(store).publish(
        schema.id, "1d", market_frame(days=days, correction=1), granularity="year", period="2026"
    )
    with pytest.raises(MissingDataError, match="Conflicting"):
        load(schema.id, data_dir=data_root, start=days[0], end=days[0])


def test_corrupt_unverified_or_invalid_index_has_explicit_failure(data_root, monkeypatch):
    days = ["2026-11-13"]
    store, schema = market_store(data_root, "future", "1m", days)
    entry = Entry(
        **Publisher(store).publish(
            schema.id,
            "1m",
            market_frame("future", "1m", days, night=True),
            granularity="year",
            period="2026",
        )["entries"][0]
    )
    from fqdb.storage.market import get_market_price

    old_active = store.active
    monkeypatch.setattr(
        store, "active", lambda *a, **kw: [(replace(e, day_groups={}), c) for e, c in old_active(*a, **kw)]
    )
    with pytest.raises(MissingDataError, match="row-group"):
        get_market_price(store, schema.id, entry.symbols, days[0], days[0], frequency="1m")
    monkeypatch.setattr(store, "active", old_active)
    path = data_root / entry.file
    path.unlink()
    with pytest.raises(MissingDataError, match="stale/unverified"):
        get_market_price(store, schema.id, entry.symbols, days[0], days[0], frequency="1m")


def test_separate_day_files_with_conflicting_minute_keys_fail_even_paged(data_root):
    Config(data_root).ensure()
    store = Store(data_root)
    days = ["2026-11-13", "2026-11-17"]
    schema = market_schema("future", days=days, frequency="1m", symbols=["RB2701.SHFE"])
    store.register(schema)
    first = market_frame("future", "1m", days=[days[0]], symbols=["RB2701.SHFE"], night=True)
    second = market_frame("future", "1m", days=[days[1]], symbols=["RB2701.SHFE"], night=True)
    second["trading_time"] = first.trading_time.tolist()
    Publisher(store).publish(schema.id, "1m", first)
    Publisher(store).publish(schema.id, "1m", second)
    for limit in (None, 1):
        with pytest.raises(MissingDataError, match="conflicting trading-day primary keys"):
            get_price(
                store,
                instruments=["RB2701.SHFE"],
                dataset=schema.id,
                frequency="1m",
                start_date=days[0],
                end_date=days[1],
                fields=["close"],
                limit=limit,
            )
