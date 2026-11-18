from datetime import date

from ..util import FQDBError, day


def select(store, dataset, frequency, start_date=None, end_date=None, year=None, month=None):
    schema = store.dataset(dataset)
    if frequency not in schema.frequencies:
        raise FQDBError(f"Unsupported frequency: {frequency}")
    if year:
        import calendar

        first = int(month or 1)
        last = int(month or 12)
        start_date = date(int(year), first, 1).isoformat()
        end_date = date(int(year), last, calendar.monthrange(int(year), last)[1]).isoformat()
    start_date = day(start_date) if start_date else None
    end_date = day(end_date) if end_date else None
    if start_date and end_date and start_date > end_date:
        raise FQDBError("start_date must not exceed end_date")
    return store.entries(dataset, frequency, start_date, end_date), start_date, end_date
