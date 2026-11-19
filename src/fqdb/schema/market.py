"""The physical market-data contract, independent of providers and transport."""

from __future__ import annotations

import pyarrow as pa

from ..util import FQDBError
from .assets import DEFAULT_MARKET, asset_contract

VALUES = ("open", "high", "low", "close", "volume", "amount")
DAILY_FIELDS = {
    "instrument": "string",
    "trading_date": "date",
    **dict.fromkeys(VALUES, "float64"),
    "prev_close": "float64",
}
FUTURES_DAILY_FIELDS = {
    **DAILY_FIELDS,
    **dict.fromkeys(("settlement", "prev_settlement", "open_interest", "limit_up", "limit_down"), "float64"),
}
MINUTE_FIELDS = {"instrument": "string", "trading_time": "timestamp", **dict.fromkeys(VALUES, "float64")}


def market_fields(frequency, *, asset="stock"):
    if frequency == "1d":
        return dict(FUTURES_DAILY_FIELDS if asset == "future" else DAILY_FIELDS)
    if frequency == "1m":
        return dict(MINUTE_FIELDS)
    raise FQDBError(f"Market format supports 1d or 1m, not {frequency!r}")


def market_primary_key(frequency):
    return ["instrument", "trading_date" if frequency == "1d" else "trading_time"]


def validate_instrument(value, asset, *, market=DEFAULT_MARKET):
    """Validate an identifier with its declared venue or index namespace."""
    if not isinstance(value, str):
        raise FQDBError(f"instrument must be a string, including leading zeroes: {value!r}")
    if not asset_contract(asset, market=market).accepts_symbol(value):
        raise FQDBError(f"Invalid {asset} instrument code or suffix: {value!r}")
    return value


def arrow_schema(schema, frequency=None):
    """Return exact physical types, without a Pandas index or metadata columns."""
    selected = schema.for_frequency(frequency) if frequency else schema
    fields = selected.fields
    types = {
        "string": pa.string(),
        "date": pa.date32(),
        "timestamp": pa.timestamp("us", tz=selected.timezone),
    }
    return pa.schema([(name, types.get(dtype) or pa.type_for_alias(dtype)) for name, dtype in fields.items()])
