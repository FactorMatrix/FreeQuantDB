from datetime import date, timedelta

import pyarrow.parquet as pq

from ..publish.paths import resolve_file
from ..schema.trading_days import minute_day_groups


def dates(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    for offset in range((last - first).days + 1):
        yield (first + timedelta(days=offset)).isoformat()


def coverage(
    schema, frequency, entries, local, start=None, end=None, root=None, deep=False, observed_days=None
):
    observations = observed_days or {}
    sparse = {e.period: e for e in entries if e.sparse}
    published = {
        d: e
        for e in sorted(entries, key=lambda x: {"day": 2, "month": 1, "year": 0}[x.period_kind])
        for d in (e.days or observations.get(e.info_hash, []))
    }
    available = {
        d: e
        for e, _ in sorted(local, key=lambda item: {"day": 2, "month": 1, "year": 0}[item[0].period_kind])
        for d in (e.days or observations.get(e.info_hash, []))
    }
    calendar = schema.calendars.get(frequency)
    start = start or (min(published) if published else None)
    end = end or (max(published) if published else None)
    # Calendar is only authoritative within its declared min/max range.
    known = (
        calendar is not None
        and start
        and end
        and calendar
        and start >= min(calendar)
        and end <= max(calendar)
    )
    expected = set(d for d in (calendar or []) if (not start or d >= start) and (not end or d <= end))
    rows = []
    for d in dates(start, end) if start and end else sorted(published):
        period = d.replace("-", "")
        possible = [sparse[period[:n]] for n in (4, 6, 8) if period[:n] in sparse]
        if known and d not in expected:
            status = "closed"
        elif d in published:
            status = "local" if d in available else "published_but_local_missing"
        elif possible:
            status = (
                "source_record_gap"
                if known and all(e.info_hash in observations for e in possible)
                else "pending_trading_dates"
            )
        elif known:
            status = "source_unpublished"
        else:
            status = "pending_calendar"
        rows.append({"date": d, "status": status})
    symbols = []
    minute_indexes = {}
    published = {d: e for d, e in published.items() if (not start or d >= start) and (not end or d <= end)}
    for d, entry in published.items():
        meta = schema.instruments.get(d)
        if not meta:
            symbols.append({"date": d, "status": "pending_instrument_metadata"})
            continue
        counts = None
        if deep and d in available and root:
            local_entry = available[d]
            path = resolve_file(root, local_entry)
            try:
                name = "instrument"
                if local_entry.sparse and frequency == "1d":
                    table = pq.read_table(
                        path, columns=[name], filters=[("trading_date", "=", date.fromisoformat(d))]
                    )
                else:
                    with pq.ParquetFile(path) as parquet:
                        if local_entry.sparse:
                            if path not in minute_indexes:
                                minute_indexes[path] = (
                                    minute_day_groups(parquet, local_entry.start, local_entry.end) or {}
                                )
                            groups = minute_indexes[path]
                        else:
                            groups = local_entry.day_groups
                        table = parquet.read_row_groups(groups[d], columns=[name])
                counts = table.to_pandas()[name].value_counts()
            except Exception:
                pass
        for symbol, spec in meta.items():
            status = spec.get("status", "unknown")
            actual = int(counts.get(symbol, 0)) if counts is not None else None
            expected_rows = spec.get("expected_rows")
            result = (
                "legal_no_data"
                if status in ("suspended", "not_listed", "expired")
                else "pending_record_metadata"
            )
            if status == "trading" and expected_rows is not None and actual is not None:
                result = "verified" if actual == expected_rows else "source_record_gap"
            if result == "legal_no_data" and actual:
                result = "metadata_conflict"
            symbols.append(
                {
                    "date": d,
                    "symbol": symbol,
                    "status": result,
                    "actual": actual,
                    "expected": expected_rows,
                    "basis": spec,
                }
            )
    numerator = len(expected & set(available)) if known else None
    return {
        "status": "known" if known else "pending_calendar",
        "numerator": numerator,
        "denominator": len(expected) if known else None,
        "basis": "explicit dataset calendar (bounded range)" if known else "待确认：缺少范围内可信交易日历",
        "days": rows,
        "instruments": symbols,
    }
