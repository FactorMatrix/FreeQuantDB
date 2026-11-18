"""Current-only synthetic storage, quality, coverage and offline-query contracts."""

import pandas as pd
import pytest

from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.publish.publisher import Publisher
from fqdb.publish.paths import immutable_file
from fqdb.schema.base import Dataset
from fqdb.storage.metadata import Store
from fqdb.storage.query import MissingDataError, get_price
from fqdb.util import FQDBError, safe_path
from fqdb.validation.coverage import coverage
from fqdb.validation.files import inspect_file, passed
from fqdb.validation.quality import validate

DAYS = ["2026-11-12", "2026-11-13", "2026-11-16"]
STOCK = ["000100.SZ", "600000.SH"]
FUTURE = ["RB2701.SHFE", "I2701.DCE"]


def sample_schema(asset="stock", days=None):
    schema = market_schema(asset, days=days or DAYS, symbols=FUTURE if asset == "future" else STOCK)
    if asset == "stock":
        for row in schema.instruments.values():
            row["000001.SZ"] = {"status": "suspended", "expected_rows": 0}
            row["000002.SZ"] = {"status": "not_listed", "expected_rows": 0}
    return schema


def sample_frame(asset="stock", days=None, *, frequency="1d", **options):
    return market_frame(
        asset, frequency, days=days or DAYS[:2], symbols=FUTURE if asset == "future" else STOCK, **options
    )


def setup_store(root, asset="stock"):
    cfg = Config(root).ensure()
    store = Store(cfg.root)
    schema = sample_schema(asset)
    store.register(schema)
    source = cfg.root / ".state/imports/synthetic.csv"
    sample_frame(asset).to_csv(source, index=False)
    Publisher(store).publish(schema.id, "1d", source)
    return store, schema, source


def test_current_package_identities_query_and_revision_corruption(data_root):
    store, schema, source = setup_store(data_root)
    day = store.entries(start_date=DAYS[0], end_date=DAYS[0])[0]
    month = Publisher(store).aggregate(schema.id, "1d", "month", "202611")["entries"][0]
    year = Publisher(store).aggregate(schema.id, "1d", "year", "2026")["entries"][0]
    assert len({day.info_hash, month["info_hash"], year["info_hash"]}) == 3
    original = immutable_file(data_root, day).read_bytes()
    assert Publisher(store).publish(schema.id, "1d", source)["entries"][0]["info_hash"] == day.info_hash
    result = get_price(store, STOCK, DAYS[0], DAYS[1], fields=["close"])
    assert isinstance(result.index, pd.RangeIndex) and result.columns.tolist() == ["close"]
    assert result.shape == (4, 1) and result.attrs["synthetic"]
    assert get_price(store, STOCK[0], DAYS[0], DAYS[0], fields=["volume"]).iloc[0, 0] == 100
    with pytest.raises(FQDBError, match="Unavailable"):
        get_price(store, STOCK[0], DAYS[0], DAYS[1], fields=["unknown"])
    with pytest.raises(FQDBError, match="adjust_type|unadjusted"):
        get_price(store, STOCK[0], DAYS[0], DAYS[1], adjust_type="pre")
    with pytest.raises(MissingDataError, match=DAYS[2]):
        get_price(store, STOCK[0], DAYS[0], DAYS[2])
    assert passed(inspect_file(data_root, day, schema))
    sample_frame(days=[DAYS[0]], correction=10).to_csv(source, index=False)
    revised = Publisher(store).publish(schema.id, "1d", source)["entries"][0]
    assert revised["version"] == 2 and revised["file"] == day.file
    assert immutable_file(data_root, day).read_bytes() == original
    assert get_price(store, STOCK[0], DAYS[0], DAYS[0], fields=["close"]).iloc[0, 0] == 20.5
    path = data_root / revised["file"]
    with path.open("r+b") as f:
        f.write(b"BROKEN")
    with pytest.raises(MissingDataError, match="unverified|verified|changed"):
        get_price(store, STOCK[0], DAYS[0], DAYS[0])
    assert (
        inspect_file(data_root, store.entries(schema.id, "1d", DAYS[0], DAYS[0])[0], schema)["file"]
        == "damaged"
    )


def test_coverage_calendar_symbols_unknown_and_night_session(data_root):
    store, schema, _ = setup_store(data_root)
    report = coverage(schema, "1d", store.entries(), store.active(), DAYS[0], DAYS[2], data_root, True)
    statuses = {r["date"]: r["status"] for r in report["days"]}
    assert statuses["2026-11-14"] == "closed" and statuses[DAYS[2]] == "source_unpublished"
    assert report["numerator"] == 2 and report["denominator"] == 3
    assert any(r["symbol"] == "000001.SZ" and r["status"] == "legal_no_data" for r in report["instruments"])
    assert any(r["symbol"] == "000002.SZ" and r["status"] == "legal_no_data" for r in report["instruments"])
    schema.calendars, schema.instruments = {}, {}
    unknown = coverage(schema, "1d", store.entries(), store.active(), root=data_root)
    assert unknown["denominator"] is None and unknown["status"] == "pending_calendar"
    assert all(r["status"] == "pending_instrument_metadata" for r in unknown["instruments"])
    night = sample_frame("future", [DAYS[1]], frequency="1m", night=True)
    good = validate(night, sample_schema("future"), DAYS[1], frequency="1m")
    assert str(good.trading_time.iloc[0].date()) == DAYS[0]


def test_quality_errors_and_unsupported_contract_are_explicit(data_root):
    schema = sample_schema()
    with pytest.raises(FQDBError):
        Dataset(**(schema.to_dict() | {"id": "zz/sensor/observation"}))
    broken = sample_frame()
    broken.loc[0, "high"] = -1
    with pytest.raises(FQDBError, match="OHLC"):
        validate(broken, schema)
    duplicate = pd.concat([sample_frame(), sample_frame()], ignore_index=True)
    duplicate.loc[len(duplicate) - 1, "close"] += 0.01
    duplicate.attrs.clear()
    with pytest.raises(FQDBError, match="duplicate"):
        validate(duplicate, schema)
    with pytest.raises(FQDBError, match="Schema fields"):
        validate(sample_frame().drop(columns=["prev_close"]), schema)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "stock/../../escape", "C:/evil", "stock\\evil"])
def test_path_boundaries(data_root, path):
    with pytest.raises(FQDBError, match="path|Path"):
        safe_path(data_root, path)


def test_session_windows_and_nonfinite_values(data_root):
    schema = sample_schema("future")
    schema.sessions = {DAYS[1]: [{"start": DAYS[0] + "T21:00:00+08:00", "end": DAYS[0] + "T23:00:00+08:00"}]}
    night = sample_frame("future", [DAYS[1]], frequency="1m", night=True)
    assert len(validate(night, schema, DAYS[1], frequency="1m")) == 2
    night["trading_time"] = pd.Timestamp(DAYS[1] + " 03:00", tz="Asia/Shanghai")
    with pytest.raises(FQDBError, match="outside declared sessions"):
        validate(night, schema, DAYS[1], frequency="1m")
    broken = sample_frame()
    broken.loc[0, "volume"] = float("inf")
    with pytest.raises(FQDBError, match="nonfinite"):
        validate(broken, sample_schema())


def test_future_night_partition_offline_price_query(data_root):
    store = Store(Config(data_root).ensure().root)
    schema = sample_schema("future")
    store.register(schema)
    night = sample_frame("future", [DAYS[1]], frequency="1m", night=True)
    result = Publisher(store).publish(schema.id, "1m", night)
    assert result["entries"][0]["trading_date"] == DAYS[1]
    prices = get_price(store, FUTURE[0], DAYS[1], DAYS[1], dataset=schema.id, frequency="1m")
    assert prices.columns.tolist() == list(schema.fields_for("1m")) and len(prices) == 1
    assert str(prices.trading_time.iloc[0].date()) == DAYS[0]


def test_query_distinguishes_missing_symbol_and_legal_absence(data_root):
    store, _, _ = setup_store(data_root)
    with pytest.raises(MissingDataError, match="no local records"):
        get_price(store, "999999.SZ", DAYS[0], DAYS[1], fields=["close"])
    legal = get_price(store, "000001.SZ", DAYS[0], DAYS[1], fields=["close"])
    assert legal.empty and len(legal.attrs["legal_absences"]) == 2


def prepare_inputs(root):
    from fqdb.util import atomic_json

    root = Config(root).ensure().root
    schema = sample_schema()
    descriptor = root / ".state/imports/SYNTHETIC-stock-schema.json"
    source = root / ".state/imports/SYNTHETIC-stock.csv"
    atomic_json(descriptor, schema.to_dict())
    sample_frame().to_csv(source, index=False)
    return {"descriptor": str(descriptor), "input": str(source)}
