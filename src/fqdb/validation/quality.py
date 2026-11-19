from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa

from ..util import FQDBError, day
from ..schema.assets import asset_contract, market_contract

RULES = {}


def register_rule(name, function):
    """A rule accepts a normalized DataFrame and returns a list of actionable errors."""
    RULES[name] = function


def normalize(frame, schema, trading_date=None, *, frequency=None, trading_dates=None):
    schema = schema.for_frequency(frequency) if frequency else schema
    return _normalize_market(frame, schema, trading_date, trading_dates)


def errors(frame, schema, trading_date=None, *, frequency=None, trading_dates=None, check_units=True):
    schema = schema.for_frequency(frequency) if frequency else schema
    return _market_errors(frame, schema, trading_date, trading_dates, check_units)


def validate(frame, schema, trading_date=None, *, frequency=None, trading_dates=None, check_units=True):
    schema = schema.for_frequency(frequency) if frequency else schema
    data = normalize(frame, schema, trading_date, trading_dates=trading_dates)
    failures = errors(data, schema, trading_date, check_units=check_units)
    if failures:
        raise FQDBError("; ".join(failures))
    return data


def _source_dates(frame, schema, trading_date, trading_dates):
    raw = trading_dates if trading_dates is not None else frame.attrs.get("trading_dates")
    if raw is None and trading_date is not None:
        raw = [trading_date] * len(frame)
    if raw is None and "trading_date" in frame:
        raw = frame.trading_date.tolist()
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or len(raw) != len(frame):
        raise FQDBError("trading_dates metadata must contain one reliable source trading day per row")
    result = []
    for index, value in enumerate(raw):
        try:
            if pd.isna(value):
                raise ValueError("missing trading day")
            result.append(day(str(value)))
        except Exception as exc:
            raise FQDBError(f"Invalid source trading_date metadata at row {index}: {value!r}") from exc
    return result


def _normalize_market(frame, schema, trading_date, trading_dates):
    data = frame.copy()
    missing, extra = set(schema.fields) - set(data), set(data) - set(schema.fields)
    if missing or extra:
        raise FQDBError(f"Schema fields: missing={sorted(missing)}, undeclared={sorted(extra)}")
    source_dates = _source_dates(frame, schema, trading_date, trading_dates)
    for name, dtype in schema.fields.items():
        try:
            if dtype == "string":
                wrong = data[name].notna() & ~data[name].map(lambda value: isinstance(value, str))
                if wrong.any():
                    raise ValueError("instrument must remain a string; numeric codes lose leading zeroes")
                data[name] = data[name].astype("string")
            elif dtype == "timestamp":
                times = pd.to_datetime(data[name])
                if times.dt.tz is None:
                    raise ValueError("timezone is required; declare the source timezone before conversion")
                if times.dt.nanosecond.fillna(0).ne(0).any():
                    raise ValueError("timestamp has sub-microsecond precision; refusing silent truncation")
                data[name] = times.dt.tz_convert(schema.timezone).dt.as_unit("us")
            elif dtype == "date":
                values = pd.to_datetime(data[name])
                if values.dt.tz is not None or not values.dropna().eq(values.dropna().dt.normalize()).all():
                    raise ValueError("trading_date must be a date, not a timestamp with intraday time")
                data[name] = values.dt.date
            else:
                if pd.api.types.is_bool_dtype(data[name]):
                    raise ValueError("boolean values are not market prices or quantities")
                data[name] = pa.array(data[name], type=pa.type_for_alias(dtype), from_pandas=True).to_numpy(
                    zero_copy_only=False
                )
        except Exception as exc:
            raise FQDBError(f"Invalid field {name} ({dtype}): {exc}") from exc
    # Keep source-day metadata aligned through ordering and duplicate removal,
    # without adding any physical market column to the returned DataFrame.
    marker = "_fqdb_source_trading_date"
    if source_dates is not None:
        data[marker] = source_dates
    duplicates = data[data.duplicated(schema.primary_key, keep=False)]
    if not duplicates.empty:
        for key, group in duplicates.groupby(schema.primary_key, dropna=False):
            if len(group.drop_duplicates()) != 1:
                raise FQDBError(f"conflicting duplicate primary key: {key!r}")
    data = data.drop_duplicates(schema.primary_key).sort_values(schema.primary_key).reset_index(drop=True)
    dates = data.pop(marker).tolist() if marker in data else None
    result = data[list(schema.fields)]
    result.attrs = {"trading_dates": dates} if dates is not None else {}
    return result


def _market_errors(frame, schema, trading_date, trading_dates, check_units):
    from ..schema.market import validate_instrument

    failures = []
    asset, market = schema.asset, schema.market
    contract = asset_contract(asset, market=market)
    frequency = schema.active_frequency or ("1d" if "trading_date" in schema.fields else "1m")
    if frame.empty:
        failures.append("empty market package")
    for column in schema.fields:
        if column not in contract.nullable_fields and frame[column].isna().any():
            failures.append(f"missing required market field: {column}")
    if frame.duplicated(schema.primary_key).any():
        failures.append("duplicate primary key")
    for index, instrument in frame.instrument.items():
        if pd.isna(instrument):
            continue
        try:
            validate_instrument(instrument, asset, market=market)
        except FQDBError as exc:
            failures.append(f"row {index}: {exc}")
    if check_units and not schema.units_confirmed:
        failures.append("units are not confirmed; declare verified volume and amount units before publishing")
    if schema.units.get("volume") not in contract.volume_units:
        failures.append(f"unknown or incompatible volume unit for {asset}: {schema.units.get('volume')!r}")
    amount_units = contract.amount_units or market_contract(market).amount_units
    if schema.units.get("amount") not in amount_units:
        failures.append(f"unknown amount unit: {schema.units.get('amount')!r}")
    if schema.units.get("price") not in contract.price_units:
        failures.append(f"unknown or incompatible price unit for {asset}: {schema.units.get('price')!r}")
    numeric = [name for name, dtype in schema.fields.items() if dtype == "float64"]
    for column in numeric:
        if not np.isfinite(frame[column].dropna()).all():
            failures.append(f"nonfinite value: {column}")
    for column in contract.positive_fields:
        if (frame[column].dropna() <= 0).any():
            failures.append(f"nonpositive {column}")
    if (
        (frame.high < frame[["open", "close", "low"]].max(axis=1))
        | (frame.low > frame[["open", "close", "high"]].min(axis=1))
    ).any():
        failures.append("invalid OHLC range")
    for column in ("volume", "amount", "open_interest"):
        if column in schema.fields and (frame[column].dropna() < 0).any():
            failures.append(f"negative {column}")
    dates = _source_dates(frame, schema, trading_date, trading_dates)
    if dates is None:
        failures.append(
            "missing reliable trading_dates metadata for minute records; never infer night-session dates"
        )
        dates = []
    if "trading_date" in frame and dates != frame.trading_date.astype(str).tolist():
        failures.append("daily trading_date conflicts with source trading-date metadata")
    if trading_date and any(value != day(trading_date) for value in dates):
        failures.append("wrong trading_date partition (night sessions require reliable source trading_date)")
    declared = schema.calendars.get(frequency)
    if declared and set(dates) - set(declared):
        failures.append(
            f"source trading dates outside declared calendar: {sorted(set(dates) - set(declared))}"
        )
    time_column = "trading_time" if frequency == "1m" else None
    if time_column and dates:
        grouped = pd.Series(dates, index=frame.index)
        for date_value in sorted(set(dates)):
            windows = schema.sessions.get(date_value)
            if windows:
                group = frame.loc[grouped.eq(date_value)]
                within = pd.Series(False, index=group.index)
                for window in windows:
                    begin, end = pd.Timestamp(window["start"]), pd.Timestamp(window["end"])
                    if begin.tzinfo is None or end.tzinfo is None:
                        failures.append(f"declared sessions lack timezone: {date_value}")
                        continue
                    within |= group[time_column].between(begin, end, inclusive="both")
                if not within.all():
                    failures.append(f"records outside declared sessions: {date_value}")
    for name in schema.rules:
        if name not in ("ohlc", "nonnegative"):
            if name not in RULES:
                failures.append(f"quality plugin unavailable: {name}")
            else:
                failures.extend(RULES[name](frame))
    return failures
