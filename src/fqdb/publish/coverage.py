"""Require evidence before recording that a package covers a complete period."""

from datetime import datetime
from zoneinfo import ZoneInfo

from ..catalog.models import period_bounds
from ..util import FQDBError, day


def confirm_coverage(schema, frequency, days, kind, period, complete, as_of, counts=None):
    first, last = period_bounds(kind, period)
    if not first <= days[0] <= days[-1] <= last:
        raise FQDBError("Input rows fall outside the requested package period")
    if as_of:
        as_of = day(as_of)
        if not days[-1] <= as_of <= last:
            raise FQDBError("as_of must cover all included trading days within the package period")
    else:
        as_of = days[-1]
    if complete:
        calendar = schema.calendars.get(frequency)
        if not calendar or first < min(calendar) or last > max(calendar):
            raise FQDBError("Complete period requires a trusted calendar spanning its full boundaries")
        expected = sorted(d for d in calendar if first <= d <= last)
        if expected != list(days) or last >= datetime.now(ZoneInfo(schema.timezone)).date().isoformat():
            raise FQDBError("An unfinished or missing-day period cannot be declared complete")
        for d in days:
            records = schema.instruments.get(d)
            if not records:
                raise FQDBError("Complete period requires authoritative instrument/record coverage")
            actual = (counts or {}).get(d, {})
            if set(actual) - records.keys():
                raise FQDBError(f"Complete period has instruments without coverage metadata: {d}")
            for symbol, expectation in records.items():
                status = expectation.get("status")
                amount = actual.get(symbol, 0)
                if status in ("suspended", "not_listed", "expired"):
                    if amount:
                        raise FQDBError(f"Records conflict with legal absence metadata: {d}, {symbol}")
                elif status != "trading" or expectation.get("expected_rows") is None:
                    raise FQDBError(f"Complete period requires confirmed expected rows: {d}, {symbol}")
                elif amount != expectation["expected_rows"]:
                    raise FQDBError(
                        f"Incomplete record coverage: {d}, {symbol}, actual={amount}, expected={expectation['expected_rows']}"
                    )
    return as_of
