"""The sole offline query entry point for the current market contract."""

from __future__ import annotations

from ..schema.assets import DEFAULT_DATASET
from ..util import FQDBError


class MissingDataError(FQDBError):
    pass


def get_price(
    store,
    instruments=None,
    start_date=None,
    end_date=None,
    frequency="1d",
    fields=None,
    *,
    dataset=DEFAULT_DATASET,
    adjust_type="none",
    allow_partial=False,
    limit=None,
    offset=0,
    start_time=None,
    end_time=None,
):
    from .market import get_market_price

    return get_market_price(
        store,
        dataset,
        instruments,
        start_date,
        end_date,
        frequency=frequency,
        fields=fields,
        adjust_type=adjust_type,
        allow_partial=allow_partial,
        limit=limit,
        offset=offset,
        start_time=start_time,
        end_time=end_time,
    )
