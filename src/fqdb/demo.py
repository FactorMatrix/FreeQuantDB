"""Explicitly synthetic fixtures; never fetches or represents real market data."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from .schema.registry import base_dataset


MARKET_SYMBOLS = {
    "stock": ("000100.SZ", "600000.SH", "430047.BJ"),
    "etf": ("510300.SH", "159915.SZ"),
    "future": ("RB2701.SHFE", "I2701.DCE", "TA2701.CZCE", "IF2701.CFFEX"),
}


def market_schema(asset="stock", days=None, *, symbols=None, frequency=None, rows_per_day=1):
    """Clearly labelled synthetic market-format descriptor; no provider calls."""
    days = days or ["2026-11-13", "2026-11-17"]
    symbols = symbols or MARKET_SYMBOLS[asset]
    schema = base_dataset(asset)
    schema.synthetic = True
    schema.source = "FreeQuantDB SYNTHETIC market-format fixture; codes are examples, prices are fabricated"
    schema.license = "CC0-1.0 synthetic fixture"
    schema.units_confirmed = True
    schema.timestamp_meaning = "synthetic bar end; minute timestamp is the actual local bar time"
    schema.units["prev_close"] = "synthetic source original previous close; not an adjustment factor"
    schema.calendars = {f: list(days) for f in schema.frequencies}
    schema.instruments = {
        d: {symbol: {"status": "trading", "expected_rows": rows_per_day} for symbol in symbols} for d in days
    }
    return schema.for_frequency(frequency) if frequency else schema


def market_frame(
    asset="stock",
    frequency="1d",
    days=None,
    *,
    symbols=None,
    n=1,
    correction=0,
    night=False,
    timezone="Asia/Shanghai",
):
    """Return exactly nine/eight synthetic columns and external source-day metadata.

    ``night=True`` uses a preceding natural evening only to construct a known
    synthetic test case. Production partitioning never performs this inference.
    """
    from .schema.market import market_fields

    days = days or ["2026-11-13", "2026-11-17"]
    symbols = symbols or MARKET_SYMBOLS[asset]
    records, source_days = [], []
    for d in days:
        for symbol in symbols:
            for minute in range(n if frequency == "1m" else 1):
                price = 10.0 + minute * 0.001 + correction
                record = {"instrument": symbol}
                if frequency == "1d":
                    record["trading_date"] = datetime.fromisoformat(d).date()
                else:
                    stamp = pd.Timestamp(d + " 09:31", tz=timezone)
                    if night:
                        stamp = pd.Timestamp(d + " 21:01", tz=timezone) - pd.Timedelta(days=1)
                    record["trading_time"] = stamp + pd.Timedelta(minutes=minute)
                record.update(
                    {
                        "open": price,
                        "high": price + 1,
                        "low": price - 1,
                        "close": price + 0.5,
                        "volume": 100.0 + minute,
                        "amount": 1000.0 + minute,
                    }
                )
                if frequency == "1d":
                    record["prev_close"] = 9.5
                records.append(record)
                source_days.append(d)
    result = pd.DataFrame(records, columns=list(market_fields(frequency)))
    result.attrs["trading_dates"] = source_days
    return result
