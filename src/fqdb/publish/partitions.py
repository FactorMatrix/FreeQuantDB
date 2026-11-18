"""Group verified trading days into native FQDB publication periods."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..util import FQDBError, day


PERIOD_LENGTHS = {"day": 8, "month": 6, "year": 4}


@dataclass(frozen=True)
class ProductionPartition:
    granularity: str
    period: str
    trading_days: tuple[str, ...]


def plan_partitions(
    trading_days: Iterable[str], granularity: str = "calendar", *, as_of: str | None = None
) -> tuple[ProductionPartition, ...]:
    """Use calendar tiers relative to an explicit date, independent of source freshness."""
    if granularity not in PERIOD_LENGTHS and granularity != "calendar":
        raise FQDBError("Unsupported production granularity")
    if granularity == "calendar" and as_of is None:
        raise FQDBError("Calendar production requires an explicit as_of date")
    cutoff = day(as_of) if as_of is not None else None
    dates = sorted(day(value) for value in trading_days)
    if len(dates) != len(set(dates)):
        raise FQDBError("Duplicate declared trading days")
    if cutoff and dates and dates[-1] > cutoff:
        raise FQDBError("Source trading day is after the production as_of date")
    groups = {}
    for trading_day in dates:
        kind = granularity
        if kind == "calendar":
            if trading_day[:4] < cutoff[:4]:
                kind = "year"
            elif trading_day[:7] < cutoff[:7]:
                kind = "month"
            else:
                kind = "day"
        period = trading_day.replace("-", "")[: PERIOD_LENGTHS[kind]]
        groups.setdefault((kind, period), []).append(trading_day)
    return tuple(
        ProductionPartition(kind, period, tuple(grouped)) for (kind, period), grouped in groups.items()
    )
