"""Offline market reads select one verified source per trading day."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..planning.planner import package_dates
from ..schema.digest import contract_hash
from ..publish.paths import resolve_file
from ..schema.market import arrow_schema
from ..schema.trading_days import minute_day_groups
from ..util import FQDBError, day
from ..validation.files import parquet_days


def _unchanged(path, evidence):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return stat.st_mtime_ns == evidence["mtime_ns"] and stat.st_size == evidence["size"]


def _sparse_parquet(path, physical):
    from .query import MissingDataError

    try:
        parquet = pq.ParquetFile(path)
    except (ValueError, OSError) as exc:
        raise MissingDataError(f"Invalid Parquet package {path}: {exc}; run check explicitly") from exc
    if not parquet.schema_arrow.remove_metadata().equals(physical):
        parquet.close()
        raise MissingDataError(f"Physical column order/types differ from market contract: {path}")
    return parquet


def _sparse_groups(parquet, entry, frequency, first, last):
    if frequency == "1d":
        return parquet_days(parquet, first, last)
    groups = minute_day_groups(parquet, entry.start, entry.end)
    return (
        None
        if groups is None
        else {trading_day: indexes for trading_day, indexes in groups.items() if first <= trading_day <= last}
    )


def local_sparse_dates(store, descriptor, frequency, active):
    physical = arrow_schema(descriptor.for_frequency(frequency))
    observed = set()
    for entry, evidence in active:
        if not entry.sparse:
            continue
        path = resolve_file(store.root, entry, immutable=True)
        if _unchanged(resolve_file(store.root, entry), evidence) and _unchanged(path, evidence):
            with _sparse_parquet(path, physical) as parquet:
                observed.update(_sparse_groups(parquet, entry, frequency, entry.start, entry.end) or {})
    return observed


def get_market_price(
    store,
    dataset,
    instruments,
    start_date,
    end_date,
    *,
    frequency="1d",
    fields=None,
    adjust_type="none",
    allow_partial=False,
    limit=None,
    offset=0,
    start_time=None,
    end_time=None,
    all_instruments=False,
):
    from .query import MissingDataError

    if adjust_type != "none":
        raise FQDBError("Only unadjusted market data is available (adjust_type='none')")
    start, end = day(start_date), day(end_date)
    if start > end:
        raise FQDBError("Start trading date must not exceed end trading date")
    descriptor = store.dataset(dataset)
    schema = descriptor.for_frequency(frequency)
    ids = [instruments] if isinstance(instruments, str) else list(instruments or [])
    if not ids and not all_instruments:
        raise FQDBError("At least one instrument is required")
    from ..schema.market import validate_instrument

    for identity in ids:
        validate_instrument(identity, descriptor.asset, market=descriptor.market)
    selected = list(schema.fields) if fields is None else list(fields)
    if not selected or len(selected) != len(set(selected)) or set(selected) - schema.fields.keys():
        raise FQDBError(f"Unavailable or duplicate fields: {selected}; allowed={list(schema.fields)}")
    if frequency == "1d" and (start_time or end_time):
        raise FQDBError("Time filters apply only to minute data")
    bounds = []
    for value in (start_time, end_time):
        stamp = pd.Timestamp(value) if value else None
        if stamp is not None:
            if stamp.tz is None:
                raise FQDBError("Minute time filters must carry an explicit timezone")
            stamp = stamp.tz_convert(schema.timezone)
        bounds.append(stamp)
    if all(v is not None for v in bounds) and bounds[0] > bounds[1]:
        raise FQDBError("Start time must not exceed end time")
    published = store.entries(dataset, frequency, start, end)
    latest = {e.key: e for e in published}
    active = store.active(dataset, frequency, start, end)
    active_files, sparse_files = {}, {}
    unindexed = []
    physical = arrow_schema(schema)
    for entry, evidence in active:
        if latest.get(entry.key, entry).info_hash != entry.info_hash or store.aggregate_stale(entry):
            continue
        path = resolve_file(store.root, entry)
        immutable = resolve_file(store.root, entry, immutable=True)
        if not _unchanged(path, evidence) or not _unchanged(immutable, evidence):
            continue
        if entry.sparse:
            try:
                parquet = _sparse_parquet(immutable, physical)
                window_start, window_end = max(entry.start, start), min(entry.end, end)
                groups = _sparse_groups(parquet, entry, frequency, window_start, window_end)
            except (ValueError, OSError) as exc:
                raise MissingDataError(
                    f"Invalid Parquet package {entry.file}: {exc}; run check explicitly"
                ) from exc
            if groups is None:
                parquet.close()
                unindexed.append(entry.period)
                continue
            sparse_files[entry.info_hash] = (parquet, groups)
        active_files[entry.info_hash] = immutable
    calendar = descriptor.calendars.get(frequency)
    expected = {d for d in (calendar or []) if start <= d <= end}
    declared = {d for e in published for d in e.days if start <= d <= end}
    observed = {d for _, groups in sparse_files.values() for d in groups}
    wanted = sorted(expected | declared | observed)
    chosen, missing, stale = {}, [], []
    by_day = {}
    expected_hash = contract_hash(descriptor)
    for entry, _ in active:
        identity = entry.info_hash
        if identity not in active_files:
            continue
        available = sparse_files[identity][1] if entry.sparse else entry.days
        for d in available:
            if start <= d <= end:
                if entry.schema_hash and entry.schema_hash != expected_hash:
                    raise FQDBError(
                        "Dataset contract changed; stored content cannot be silently reinterpreted"
                    )
                by_day.setdefault(d, []).append((entry, active_files[identity]))
    for d in wanted:
        candidates = by_day.get(d, [])
        digests = {e.daily_sources.get(d) for e, _ in candidates if e.daily_sources.get(d)}
        daily = next((e for e in published if e.period_kind == "day" and d in e.days), None)
        if daily and daily.daily_sources.get(d):
            required = daily.daily_sources[d]
            candidates = [(e, p) for e, p in candidates if e.daily_sources.get(d) == required]
            digests = {e.daily_sources.get(d) for e, _ in candidates}
        if len(digests) > 1:
            raise MissingDataError(
                f"Conflicting content versions for trading day {d}; rebuild affected packages"
            )
        if not candidates:
            had_local = any(
                d in e.days or (e.sparse and e.info_hash not in sparse_files and e.start <= d <= e.end)
                for e, _ in active
            )
            (stale if had_local else missing).append(d)
            continue
        candidates.sort(
            key=lambda item: (
                {"day": 0, "month": 1, "year": 2}[item[0].period_kind],
                item[0].sparse,
                -item[0].version,
            )
        )
        chosen[d] = candidates[0]
    covered = set(chosen)
    for entry in published:
        if entry.info_hash in sparse_files:
            covered.update(package_dates(entry, start, end))
    unavailable = (
        [
            e.period
            for e in published
            if e.sparse and e.info_hash not in sparse_files and package_dates(e, start, end) - covered
        ]
        if not calendar or not (min(calendar) <= start and end <= max(calendar))
        else []
    )
    if unindexed and (not chosen or (unavailable or missing or stale) and not allow_partial):
        raise MissingDataError(f"Minute packages {unindexed} have no explicit trading-day row-group index")
    if unavailable and not allow_partial:
        raise MissingDataError(
            f"Missing/unverified packages={unavailable}; fetch the nominal periods explicitly"
        )
    if (missing or stale) and not allow_partial:
        raise MissingDataError(f"Missing={missing}, stale/unverified={stale}; run sync/check explicitly")
    if not chosen:
        raise MissingDataError("No verified local market data in range; query never downloads automatically")
    frames, counts, last_key = [], {}, {}
    collected, seen, has_more = 0, 0, False
    read_columns = list(dict.fromkeys(["instrument", *schema.primary_key, *selected]))
    instruments = pa.array(ids, type=pa.string()) if not all_instruments else None
    for d, (entry, path) in chosen.items():
        if entry.sparse:
            parquet, observed_groups = sparse_files[entry.info_hash]
            groups = observed_groups[d]
        else:
            parquet = pq.ParquetFile(path)
            if not parquet.schema_arrow.remove_metadata().equals(physical):
                raise MissingDataError(
                    f"Physical column order/types differ from market contract: {entry.file}"
                )
            groups = entry.day_groups.get(d)
        if (
            not groups
            or len(groups) != len(set(groups))
            or any(not isinstance(g, int) or g < 0 or g >= parquet.num_row_groups for g in groups)
        ):
            raise MissingDataError(f"Missing or invalid trading-day row-group index: {entry.file}, {d}")
        for batch in parquet.iter_batches(batch_size=4096, row_groups=groups, columns=read_columns):
            if instruments is not None:
                batch = batch.filter(pc.is_in(batch.column("instrument"), value_set=instruments))
            if entry.sparse and frequency == "1d":
                batch = batch.filter(pc.equal(batch.column("trading_date"), date.fromisoformat(d)))
            data = batch.to_pandas()
            if frequency == "1d" and not data.trading_date.eq(date.fromisoformat(d)).all():
                raise MissingDataError(f"Trading-day index contradicts physical rows: {entry.file}, {d}")
            for symbol, count in data.instrument.value_counts().items():
                counts[(d, symbol)] = counts.get((d, symbol), 0) + int(count)
            for symbol, records in data.groupby("instrument", sort=False):
                values = records[schema.primary_key[1]]
                if (
                    not values.is_monotonic_increasing
                    or values.duplicated().any()
                    or (symbol in last_key and len(values) and values.iloc[0] <= last_key[symbol])
                ):
                    raise MissingDataError(
                        f"Duplicate or conflicting trading-day primary keys for {symbol}; inspect source date assignments"
                    )
                if len(values):
                    last_key[symbol] = values.iloc[-1]
            if frequency == "1m":
                if bounds[0] is not None:
                    data = data[data.trading_time >= bounds[0]]
                if bounds[1] is not None:
                    data = data[data.trading_time <= bounds[1]]
            if limit is not None:
                skip = min(len(data), max(0, offset - seen))
                seen += len(data)
                page = data.iloc[skip : skip + max(0, limit - collected)]
                collected += len(page)
                has_more |= len(data) - skip > len(page)
                data = page
            if not data.empty:
                frames.append(data[read_columns])
    gaps, legal, unconfirmed = [], [], []
    for d in chosen:
        for symbol in descriptor.instruments.get(d, {}) if all_instruments else ids:
            spec = descriptor.instruments.get(d, {}).get(symbol)
            actual = counts.get((d, symbol), 0)
            if spec and spec.get("status") in ("suspended", "not_listed", "expired"):
                legal.append({"date": d, "instrument": symbol, "basis": spec})
                if actual:
                    gaps.append({"date": d, "instrument": symbol, "reason": "metadata_conflict"})
            elif spec and spec.get("expected_rows") is not None and actual != spec["expected_rows"]:
                gaps.append(
                    {"date": d, "instrument": symbol, "actual": actual, "expected": spec["expected_rows"]}
                )
            elif chosen[d][0].sparse and not actual:
                unconfirmed.append({"date": d, "instrument": symbol})
    absent = [
        s
        for s in ids
        if not any(counts.get((d, s), 0) for d in chosen)
        and not all(any(x["date"] == d and x["instrument"] == s for x in legal) for d in chosen)
    ]
    if (gaps or absent) and not allow_partial:
        raise MissingDataError(f"Record gaps={gaps}; no local records for={absent}; inspect check report")
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=selected)
    cache_by_hash = {e.info_hash: cache for e, cache in active}
    for entry, path in chosen.values():
        evidence = cache_by_hash[entry.info_hash]
        if not _unchanged(path, evidence):
            raise MissingDataError(f"Market file changed while reading: {entry.file}")
    if limit is None and not result.empty:
        result = result.sort_values(schema.primary_key)
    # Only requested business columns: no hidden identity index or date helper column.
    result = result[selected].reset_index(drop=True)
    sparse_read = any(e.sparse for e, _ in chosen.values())
    result.attrs.update(
        {
            "format_version": 2,
            "synthetic": None if sparse_read else descriptor.synthetic,
            "source": None if sparse_read else descriptor.source,
            "timezone": descriptor.timezone,
            "date_basis": (
                "observed Parquet trading_date"
                if sparse_read and frequency == "1d"
                else "inclusive trading_date via verified row-group index"
            ),
            "missing": missing,
            "stale": stale,
            "unavailable_packages": unavailable,
            "record_gaps": gaps,
            "legal_absences": legal,
            "unconfirmed_records": unconfirmed,
            "coverage": "calendar_known" if calendar else "pending_calendar",
            "page_has_more": has_more if limit is not None else None,
            "selected_sources": {d: e.info_hash for d, (e, _) in chosen.items()},
        }
    )
    return result
