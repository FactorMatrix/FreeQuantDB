from __future__ import annotations

import json

import pandas as pd
import pyarrow.parquet as pq

from ..util import FQDBError, day
from .base import source_path


class LocalFiles:
    real_network = False

    def __init__(self, settings):
        self.settings = settings

    def availability(self):
        path = source_path(self.settings)
        return {
            "status": "ready",
            "path": str(path),
            "source": "user-selected local file",
            "bytes": path.stat().st_size,
            "range": "determined from file records",
        }

    def fetch(self, request, schema):
        path = source_path(self.settings)
        data = pd.read_csv(path) if path.suffix.lower() == ".csv" else pq.read_table(path).to_pandas()
        if request.frequency == "1d":
            if "trading_date" not in data:
                raise FQDBError("Daily local input requires trading_date")
            days = pd.to_datetime(data.trading_date).dt.date.astype(str).tolist()
        else:
            days = self.settings.get("trading_dates")
            if self.settings.get("trading_dates_path"):
                from pathlib import Path

                days = json.loads(Path(self.settings["trading_dates_path"]).expanduser().read_text())
            if not isinstance(days, list) or len(days) != len(data):
                raise FQDBError("Minute local input requires an external, row-aligned trading_dates list")
        dates = pd.Series([day(str(value)) for value in days], index=data.index)
        mask = dates.between(request.start_date, request.end_date)
        if request.symbols:
            mask &= data.instrument.isin(request.symbols)
        selected = data.loc[mask].copy()
        selected.attrs["trading_dates"] = dates[mask].tolist()
        return selected


class Synthetic:
    """Explicit, deterministic fake upstream for lifecycle tests, never real prices."""

    real_network = False

    def __init__(self, settings):
        self.settings = settings

    def availability(self):
        return {
            "status": "synthetic",
            "source": "SYNTHETIC upstream adapter; not real market data",
            "dates": self.settings.get("days", []),
            "range": "explicitly configured synthetic days",
        }

    def fetch(self, request, schema):
        if not schema.synthetic or "synthetic" not in schema.source.lower():
            raise FQDBError("Synthetic adapter requires a synthetic dataset descriptor and provenance")
        days = [d for d in self.settings.get("days", []) if request.start_date <= d <= request.end_date]
        from ..demo import market_frame

        symbols = request.symbols or tuple(self.settings.get("symbols", []))
        if not symbols:
            raise FQDBError("Synthetic market input requires explicit example instruments")
        if not days:
            result = pd.DataFrame(columns=list(schema.fields_for(request.frequency)))
            result.attrs["trading_dates"] = []
            return result
        return market_frame(
            schema.asset,
            request.frequency,
            days=days,
            symbols=symbols,
            timezone=schema.timezone,
            n=int(self.settings.get("rows_per_day", 1)),
            night=bool(self.settings.get("night", False)),
        )
