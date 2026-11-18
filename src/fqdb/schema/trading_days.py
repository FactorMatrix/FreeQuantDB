"""File-bound source trading dates for the minute market format."""

import json

from ..util import FQDBError, day

MINUTE_DAY_INDEX = b"fqdb.trading_day_row_groups.v1"


def encode_minute_day_groups(groups):
    return json.dumps(sorted(groups.items()), separators=(",", ":")).encode()


def minute_day_groups(parquet, first, last):
    """Return a complete partition of physical row groups, or None if unindexed."""
    metadata = parquet.metadata
    raw = (metadata.metadata or {}).get(MINUTE_DAY_INDEX)
    if raw is None:
        return None
    try:
        pairs = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise FQDBError("Invalid minute trading-day row-group index") from exc
    if not isinstance(pairs, list) or not pairs:
        raise FQDBError("Invalid minute trading-day row-group index")
    count = metadata.num_row_groups
    visited = bytearray(count)
    groups = {}
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise FQDBError("Invalid minute trading-day row-group index")
        trading_day, indexes = pair
        try:
            valid_date = isinstance(trading_day, str) and day(trading_day) == trading_day
        except ValueError:
            valid_date = False
        if (
            not valid_date
            or not first <= trading_day <= last
            or trading_day in groups
            or not isinstance(indexes, list)
            or not indexes
        ):
            raise FQDBError("Invalid source date in minute trading-day row-group index")
        previous = -1
        for index in indexes:
            if type(index) is not int or not previous < index < count or visited[index]:
                raise FQDBError("Duplicate, unordered or out-of-range minute Parquet row group")
            visited[index] = 1
            previous = index
        groups[trading_day] = indexes
    if not all(visited):
        raise FQDBError("Minute trading-day index omits physical Parquet row groups")
    return groups
