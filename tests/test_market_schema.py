"""Synthetic acceptance of the physical daily/minute market contract."""

from dataclasses import replace
from datetime import date
import json
import re

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from fqdb.demo import market_frame, market_schema
from fqdb.config import Config
from fqdb.catalog.magnet import MagnetCatalog
from fqdb.catalog.models import Entry
from fqdb.publish.catalog import export_public_catalog
from fqdb.publish.publisher import Publisher
from fqdb.query import load
from fqdb.schema import Dataset, arrow_schema, base_dataset, market_fields, validate_instrument
from fqdb.schema.assets import AssetContract, MARKET_CONTRACTS, MarketContract
from fqdb.schema.market import DAILY_FIELDS, MINUTE_FIELDS
from fqdb.service.core import Owner
from fqdb.storage.metadata import Store
from fqdb.storage.query import get_price
from fqdb.util import FQDBError
from fqdb.validation.files import validate_parquet
from fqdb.validation.quality import validate
from fqdb.web.app import create_app

FUTURES_EXTRA_COLUMNS = ("settlement", "prev_settlement", "open_interest", "limit_up", "limit_down")


@pytest.mark.parametrize("asset", ["stock", "etf", "lof", "future"])
@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_exact_market_columns_physical_types_and_compression(data_root, asset, frequency):
    schema = market_schema(asset).for_frequency(frequency)
    original = market_frame(asset, frequency, night=asset == "future")
    validated = validate(original, schema)
    expected = DAILY_FIELDS if frequency == "1d" else MINUTE_FIELDS
    if asset == "future" and frequency == "1d":
        expected = expected | dict.fromkeys(FUTURES_EXTRA_COLUMNS, "float64")
    assert validated.columns.tolist() == list(expected)
    assert isinstance(validated.index, pd.RangeIndex)
    table = pa.Table.from_pandas(validated, schema=arrow_schema(schema), preserve_index=False)
    target = data_root / f"SYNTHETIC-{asset}-{frequency}.parquet"
    pq.write_table(table.replace_schema_metadata(None), target, compression="zstd")
    physical = pq.read_schema(target)
    assert physical == arrow_schema(schema)
    assert physical.names == list(expected)
    assert physical.field("instrument").type == pa.string()
    assert pq.ParquetFile(target).metadata.row_group(0).column(0).compression == "ZSTD"
    if frequency == "1d":
        assert physical.field("trading_date").type == pa.date32()
        assert "prev_close" in physical.names
        if asset == "future":
            assert all(physical.field(column).type == pa.float64() for column in FUTURES_EXTRA_COLUMNS)
    else:
        assert physical.field("trading_time").type == pa.timestamp("us", tz="Asia/Shanghai")
        assert "prev_close" not in physical.names and "trading_date" not in physical.names
    actual = pq.read_table(target).to_pandas()
    assert actual.columns.tolist() == list(expected)
    assert not any(name.startswith("__index") for name in physical.names)


@pytest.mark.parametrize("asset", ["stock", "etf", "lof", "future"])
def test_current_contract_roundtrip_frequency_selection(asset):
    schema = base_dataset(asset)
    # JSON maps may be serialized in canonical key order; physical order remains contractual.
    roundtrip = Dataset(**json.loads(json.dumps(schema.to_dict(), sort_keys=True)))
    assert roundtrip.id == f"cn/{asset}/base" and roundtrip.format_version == 2
    assert list(roundtrip.for_frequency("1d").fields) == list(market_fields("1d", asset=asset))
    assert list(roundtrip.for_frequency("1m").fields) == list(MINUTE_FIELDS)
    assert roundtrip.for_frequency("1m").primary_key == ["instrument", "trading_time"]
    assert "prev_close" not in roundtrip.fields_for("1m")


@pytest.mark.parametrize("custom", [False, True])
def test_worker_refreshes_only_unchanged_default_field_declarations(data_root, custom):
    config = Config(data_root).ensure()
    store = Store(config.root)
    former = base_dataset("future").to_dict()
    former["fields"] = DAILY_FIELDS
    former["frequency_fields"]["1d"] = DAILY_FIELDS
    if custom:
        former["source"] = "Synthetic user registry"
    with store.db() as connection:
        connection.execute("INSERT INTO datasets VALUES (?, ?)", (former["id"], json.dumps(former)))

    owner = Owner(config)
    try:
        assert owner.store.dataset("cn/stock/base").fields == DAILY_FIELDS
        assert owner.store.dataset("cn/etf/base").fields == DAILY_FIELDS
        if custom:
            assert former in owner.store.datasets()
        else:
            assert owner.store.dataset(former["id"]).to_dict() == base_dataset("future").to_dict()
    finally:
        owner.close()


def test_futures_daily_requires_settlement_fields_and_accepts_zero_records():
    schema = market_schema("future", frequency="1d")
    frame = market_frame("future", "1d")
    for column in FUTURES_EXTRA_COLUMNS:
        with pytest.raises(FQDBError, match=f"missing=\\['{column}'\\]"):
            validate(frame.drop(columns=column), schema)
    recorded_zeros = frame.assign(**dict.fromkeys(FUTURES_EXTRA_COLUMNS, 0.0))
    validated = validate(recorded_zeros, schema)
    assert not validated[list(FUTURES_EXTRA_COLUMNS)].to_numpy().any()
    with pytest.raises(FQDBError, match="negative open_interest"):
        validate(frame.assign(open_interest=-1.0), schema)


def test_index_daily_nullable_values_through_catalog_planning_and_queries(data_root):
    days = ["2025-12-26", "2025-12-27"]
    symbol = "TST99999.INDX"
    schema = market_schema("index", days=days, symbols=[symbol])
    schema.calendars.clear()
    schema.instruments.clear()
    schema = Dataset(**json.loads(json.dumps(schema.to_dict(), sort_keys=True)))
    assert list(schema.fields) == list(DAILY_FIELDS)
    assert {key: schema.units[key] for key in ("price", "volume", "amount")} == {
        "price": "points",
        "volume": "reported",
        "amount": "reported",
    }
    with pytest.raises(FQDBError, match="supports frequencies 1d"):
        replace(schema, frequencies=["1d", "1m"])
    with pytest.raises(FQDBError, match="Frequency not declared"):
        schema.for_frequency("1m")

    frame = market_frame("index", days=days, symbols=[symbol])
    optional = ["open", "high", "low", "prev_close", "volume", "amount"]
    frame.loc[1, optional] = float("nan")
    owner = Owner(Config(data_root))
    try:
        owner.store.register(schema)
        published = Publisher(owner.store).publish(schema.id, "1d", frame, granularity="year", period="2025")
        entry = Entry(**published["entries"][0])
        path = data_root / entry.file
        assert entry.days == tuple(days) and not entry.complete
        assert pq.read_schema(path) == arrow_schema(schema)
        assert validate_parquet(path, entry, schema) == 2

        catalog = data_root / "SYNTHETIC-index-catalog"
        export_public_catalog(owner.store, catalog, dataset=schema.id, frequency="1d")
        consumer = Owner(Config(data_root / "SYNTHETIC-index-consumer"))
        try:
            MagnetCatalog(consumer.store.root).sync(str(catalog))
            year = consumer.catalogs.preview(schema.id, "1d", year=2025, granularity="year")
            weekend = consumer.catalogs.preview(schema.id, "1d", start_date=days[-1], end_date=days[-1])
            assert [item["entry"]["info_hash"] for item in year["items"]] == [entry.info_hash]
            assert weekend["items"] == year["items"] and weekend["calendar"] == "pending"
            remote = Entry(**year["items"][0]["entry"])
            assert remote.sparse and validate_parquet(path, remote, base_dataset("index")) == 2
        finally:
            consumer.close()

        result = load(schema.id, symbols=symbol, start=days[0], end=days[-1], data_dir=data_root)
        assert result.close.tolist() == [10.5, 10.5]
        assert result.loc[1, optional].isna().all()
        app = create_app(owner, "synthetic-token")
        owner.lifetime.open()
        request = {"dataset": schema.id, "instruments": [symbol], "start_date": days[-1], "end_date": days[-1]}
        with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer synthetic-token"}) as api:
            response = api.post("/api/query", json=request)
        assert response.status_code == 200, response.text
        record = response.json()["records"][0]
        assert record["close"] == 10.5 and all(record[field] is None for field in optional)
    finally:
        owner.close()


@pytest.mark.parametrize(
    "change,message",
    [
        ({"close": float("nan")}, "missing required market field: close"),
        ({"open": 0.0}, "nonpositive open"),
        ({"low": 0.0}, "nonpositive low"),
        ({"high": 0.0}, "nonpositive high"),
        ({"close": -1.0}, "nonpositive close"),
        ({"prev_close": 0.0}, "nonpositive prev_close"),
        ({"open": float("inf")}, "nonfinite value: open"),
        ({"high": 5.0, "open": float("nan"), "low": float("nan")}, "invalid OHLC range"),
        ({"volume": -1.0}, "negative volume"),
        ({"amount": -1.0}, "negative amount"),
    ],
)
def test_index_rejects_invalid_observations(change, message):
    with pytest.raises(FQDBError, match=message):
        validate(market_frame("index").assign(**change), market_schema("index"))


def test_market_registry_isolates_paths_catalog_and_queries(data_root, monkeypatch):
    usa_stock = AssetContract(("XNYS",), re.compile(r"[A-Z]+"), ("shares",), ("USD",))
    monkeypatch.setitem(
        MARKET_CONTRACTS,
        "test",
        MarketContract("America/New_York", ("USD",), {"stock": usa_stock}),
    )
    symbols = ["000100.SZ", "600000.SH"]
    cn = market_schema("stock", days=["2026-11-13"], symbols=symbols)
    other = replace(base_dataset("stock", market="test"), synthetic=True, units_confirmed=True)
    store = Store(Config(data_root).ensure().root)
    publisher = Publisher(store)
    frame = market_frame("stock", "1d", days=["2026-11-13"], symbols=symbols)
    for schema, rows in ((cn, frame), (other, frame.assign(instrument=["ALFA.XNYS", "BETA.XNYS"]))):
        store.register(schema)
        publisher.publish(schema.id, "1d", rows)
        stem = f"{schema.id}/1d/2026/11/20261113"
        assert (data_root / "data" / (stem + ".parquet")).is_file()
        assert (data_root / "catalog" / (stem + ".parquet.json")).is_file()

    assert {entry["dataset"] for entry in MagnetCatalog(data_root).scan()["packages"]} == {cn.id, other.id}
    result = get_price(store, "ALFA.XNYS", "2026-11-13", "2026-11-13", dataset=other.id)
    assert result.instrument.tolist() == ["ALFA.XNYS"]
    with pytest.raises(FQDBError, match="instrument"):
        get_price(store, "ALFA.XNYS", "2026-11-13", "2026-11-13", dataset=cn.id)


@pytest.mark.parametrize(
    "value,asset",
    [
        ("000100.SZ", "stock"),
        ("600000.SH", "stock"),
        ("430047.BJ", "stock"),
        ("159915.SZ", "etf"),
        ("510300.SH", "etf"),
        ("160999.SZ", "lof"),
        ("501999.SH", "lof"),
        ("Z9ZZ99.SH", "index"),
        ("987654.SZ", "index"),
        ("987654.BJ", "index"),
        ("TST99999.INDX", "index"),
        ("987654ZZZ01.INDX", "index"),
        ("987654ZZZ001.INDX", "index"),
        ("RB2701.SHFE", "future"),
        ("I2701.DCE", "future"),
        ("L2701F.DCE", "future"),
        ("PP2702F.DCE", "future"),
        ("V2712F.DCE", "future"),
        ("TA2701.CZCE", "future"),
        ("IF2701.CFFEX", "future"),
        ("SC2701.INE", "future"),
        ("SI2701.GFEX", "future"),
    ],
)
def test_explicit_instrument_suffixes(value, asset):
    assert validate_instrument(value, asset) == value


@pytest.mark.parametrize(
    "value,asset",
    [
        (100, "stock"),
        ("100.SZ", "stock"),
        ("000100.XS", "stock"),
        ("000100.sz", "stock"),
        ("600000.UNKNOWN", "stock"),
        ("RB701.CZCE", "future"),
        ("RB2700.SHFE", "future"),
        ("RB2701", "future"),
        ("rb2701.SHFE", "future"),
        ("L2701F.SHFE", "future"),
        ("I2701F.DCE", "future"),
        ("L2713F.DCE", "future"),
        ("160999.BJ", "lof"),
        ("16099.SZ", "lof"),
        ("ZZ9999.UNKNOWN", "index"),
        ("Z9ZZ99.SZ", "index"),
        ("Z9ZZ99.IND", "index"),
        ("0050.INDX", "index"),
        ("TST99999.indx", "index"),
        ("987654-ZZ901.INDX", "index"),
    ],
)
def test_unknown_or_ambiguous_instruments_are_not_guessed(value, asset):
    with pytest.raises(FQDBError):
        validate_instrument(value, asset)


def test_minute_without_daily_fields_and_explicit_night_trading_date():
    schema = market_schema("future", days=["2026-11-17"], frequency="1m")
    raw = market_frame("future", "1m", days=["2026-11-17"], night=True)
    result = validate(raw, schema, trading_date="2026-11-17")
    assert "prev_close" not in result and "trading_date" not in result
    assert result.trading_time.dt.date.eq(date(2026, 11, 16)).all()
    assert result.attrs["trading_dates"] == ["2026-11-17"] * len(result)
    untrusted = raw.copy()
    untrusted.attrs.clear()
    with pytest.raises(FQDBError, match="missing reliable trading_dates"):
        validate(untrusted, schema)
    with pytest.raises(FQDBError, match="wrong trading_date partition"):
        validate(raw, schema, trading_date="2026-11-16")


def test_external_day_index_stays_aligned_after_sorting_and_identical_deduplication():
    schema = market_schema("future", frequency="1m")
    raw = market_frame("future", "1m", n=2, night=True)
    expected = dict(zip(zip(raw.instrument, raw.trading_time), raw.attrs["trading_dates"]))
    duplicate = pd.concat([raw, raw.iloc[:1]], ignore_index=True)
    duplicate.attrs["trading_dates"] = raw.attrs["trading_dates"] + raw.attrs["trading_dates"][:1]
    reverse = duplicate.iloc[::-1].reset_index(drop=True)
    reverse.attrs["trading_dates"] = duplicate.attrs["trading_dates"][::-1]
    normalized = validate(reverse, schema)
    assert len(normalized) == len(raw)
    assert normalized.attrs["trading_dates"] == [
        expected[key] for key in zip(normalized.instrument, normalized.trading_time)
    ]
    assert list(normalized.columns) == list(MINUTE_FIELDS)


def test_reject_conflicting_duplicates_missing_prev_close_bad_ohlc_and_unknown_units():
    schema = market_schema("stock", frequency="1d")
    raw = market_frame("stock")
    with pytest.raises(FQDBError, match="prev_close"):
        validate(raw.drop(columns="prev_close"), schema)
    null_previous = raw.copy()
    null_previous.loc[0, "prev_close"] = float("nan")
    with pytest.raises(FQDBError, match="missing required market field: prev_close"):
        validate(null_previous, schema)
    conflicting = pd.concat([raw, raw.iloc[:1]], ignore_index=True)
    conflicting.attrs.clear()
    conflicting.loc[len(conflicting) - 1, "close"] = 50.0
    with pytest.raises(FQDBError, match="conflicting duplicate primary key"):
        validate(conflicting, schema)
    bad_ohlc = raw.copy()
    bad_ohlc.loc[0, "low"] = 99.0
    with pytest.raises(FQDBError, match="invalid OHLC"):
        validate(bad_ohlc, schema)
    with pytest.raises(FQDBError, match="units are not confirmed"):
        validate(raw, replace(schema, units_confirmed=False))
    with pytest.raises(FQDBError, match="unknown amount unit"):
        validate(raw, replace(schema, units=schema.units | {"amount": "unknown"}))
    with pytest.raises(FQDBError, match="undeclared"):
        validate(raw.assign(exchange="SZ"), schema)


def test_reject_naive_minute_timestamps_and_submicrosecond_truncation():
    schema = market_schema(frequency="1m")
    raw = market_frame(frequency="1m")
    naive = raw.copy()
    naive.trading_time = naive.trading_time.dt.tz_localize(None)
    with pytest.raises(FQDBError, match="timezone is required"):
        validate(naive, schema)
    precise = raw.copy()
    precise.trading_time = precise.trading_time + pd.Timedelta(nanoseconds=1)
    with pytest.raises(FQDBError, match="sub-microsecond"):
        validate(precise, schema)


def test_source_day_conflict_and_session_mismatch_report_explicitly():
    schema = market_schema(frequency="1d")
    raw = market_frame()
    false_days = ["2026-11-17"] * len(raw)
    with pytest.raises(FQDBError, match="conflicts with source trading-date"):
        validate(raw, schema, trading_dates=false_days)
    schema = market_schema("future", days=["2026-11-17"], frequency="1m")
    raw = market_frame("future", "1m", days=["2026-11-17"], night=True)
    schema.sessions = {
        "2026-11-17": [{"start": "2026-11-17T09:00:00+08:00", "end": "2026-11-17T15:00:00+08:00"}]
    }
    with pytest.raises(FQDBError, match="outside declared sessions"):
        validate(raw, schema)


def test_current_chinese_contract_requires_declared_local_timezone_and_price_unit():
    schema = market_schema()
    with pytest.raises(FQDBError, match="Asia/Shanghai"):
        replace(schema, timezone="UTC")
    with pytest.raises(FQDBError, match="price unit"):
        validate(market_frame(), replace(schema, units=schema.units | {"price": "unknown"}))
    future = market_schema("future", frequency="1d")
    assert len(validate(market_frame("future"), replace(future, units=future.units | {"price": "points"})))
