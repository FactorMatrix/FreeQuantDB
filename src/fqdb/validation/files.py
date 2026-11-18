from datetime import date

import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..bt.torrents import torrent_cache_path, validate_daily, verify_pieces
from ..publish.paths import resolve_file
from ..schema.trading_days import minute_day_groups
from ..util import FQDBError, sha256
from .quality import validate


def parquet_days(parquet, start=None, end=None):
    """Locate observed daily dates with date-column projection and row-group pruning."""
    groups = {}
    column = parquet.schema.names.index("trading_date")
    for index in range(parquet.num_row_groups):
        group = parquet.metadata.row_group(index)
        if not group.num_rows:
            continue
        stats = group.column(column).statistics
        if stats and stats.has_min_max:
            first, last = stats.min.isoformat(), stats.max.isoformat()
            if (start and last < start) or (end and end < first):
                continue
            if first == last and stats.null_count == 0:
                groups.setdefault(first, []).append(index)
                continue
        markers = parquet.read_row_group(index, columns=["trading_date"])["trading_date"]
        for value in pc.unique(markers).to_pylist():
            if value is None:
                raise FQDBError("Missing trading_date in physical Parquet rows")
            label = value.isoformat()
            if (not start or start <= label) and (not end or label <= end):
                groups.setdefault(label, []).append(index)
    return groups


def _validate_parquet(path, entry, schema):
    from ..schema.market import arrow_schema

    specific = schema.for_frequency(entry.frequency)
    parquet = pq.ParquetFile(path)
    if not parquet.schema_arrow.remove_metadata().equals(arrow_schema(specific)):
        raise FQDBError("Physical market column order/types do not match the declared contract")
    if entry.sparse:
        groups = (
            parquet_days(parquet)
            if entry.frequency == "1d"
            else minute_day_groups(parquet, entry.start, entry.end)
        )
        if groups is None:
            return None, []
        if not groups or min(groups) < entry.start or max(groups) > entry.end:
            raise FQDBError("Observed trading dates must fall within the package period")
    else:
        groups = entry.day_groups
        flattened = [g for indexes in groups.values() for g in indexes]
        if set(groups) != set(entry.days) or sorted(flattened) != list(range(parquet.num_row_groups)):
            raise FQDBError("Trading-day index must cover each row group exactly once")
    # Date-major daily files allow one group to hold the whole day's market.
    # Minute files retain instrument/time ordering, including overnight bars.
    physical_keys = ["trading_date", "instrument"] if entry.frequency == "1d" else specific.primary_key
    previous = None
    for batch in parquet.iter_batches(batch_size=65536, columns=physical_keys):
        keys = list(batch.to_pandas()[physical_keys].itertuples(index=False, name=None))
        if (
            keys != sorted(keys)
            or any(a == b for a, b in zip(keys, keys[1:]))
            or (keys and previous is not None and keys[0] <= previous)
        ):
            raise FQDBError("Physical market primary keys must be globally sorted and unique")
        if keys:
            previous = keys[-1]
    rows = 0
    from ..schema.digest import logical_digest

    for d, indexes in groups.items():
        # One trading day bounds memory even for a multi-year client catalogue.
        raw = parquet.read_row_groups(indexes)
        if entry.sparse and entry.frequency == "1d":
            raw = raw.filter(pc.equal(raw["trading_date"], date.fromisoformat(d)))
        raw = raw.to_pandas()
        data = validate(
            raw,
            specific,
            d,
            frequency=entry.frequency,
            trading_dates=[d] * len(raw),
            check_units=not entry.sparse or schema.units_confirmed,
        )
        if len(data) != len(raw):
            raise FQDBError("Duplicate keys stored in physical market data")
        if list(raw[specific.primary_key].itertuples(index=False, name=None)) != list(
            data[specific.primary_key].itertuples(index=False, name=None)
        ):
            raise FQDBError("Physical market primary keys are not sorted within a trading day")
        if not entry.sparse and logical_digest(data, specific) != entry.daily_sources[d]:
            raise FQDBError("Trading-day row-group index or source digest contradicts physical content")
        rows += len(data)
    if entry.rows is not None and rows != entry.rows:
        raise FQDBError("Manifest row count does not match physical market data")
    if entry.sparse and rows != parquet.metadata.num_rows:
        raise FQDBError("Observed trading dates do not account for all physical rows")
    return rows, sorted(groups)


def validate_parquet(path, entry, schema):
    """Validate physical columns, row keys and the available date evidence."""
    return _validate_parquet(path, entry, schema)[0]


def inspect_file_stat(stat, entry, cached=None):
    """Report file presence and verification freshness from a single stat."""
    result = {
        "file": "missing",
        "format": "pending",
        "quality": "pending",
        "bt_pieces": "pending",
        "reason": [],
    }
    if stat is None:
        result["reason"].append("published_but_local_missing")
        return result
    expected_size = entry.size if entry.size is not None else (cached or {}).get("size")
    if expected_size is not None and stat.st_size != expected_size:
        result.update(file="damaged", reason=["size_mismatch"])
        return result
    fresh = cached and stat.st_mtime_ns == cached["mtime_ns"] and stat.st_size == cached["size"]
    full = fresh and cached.get("verification", "full") == "full"
    result.update(
        file="verified_cached" if fresh else "recheck_required",
        format="verified_cached" if full else "pending",
        quality="verified_cached" if full else "pending",
        bt_pieces="verified_cached" if fresh and not full else "pending",
    )
    return result


def inspect_file(root, entry, schema, deep=True, cached=None):
    path = resolve_file(root, entry)
    try:
        stat = path.stat()
    except FileNotFoundError:
        stat = None
    result = inspect_file_stat(stat, entry, None if deep else cached)
    if not deep or result["file"] in ("missing", "damaged"):
        return result
    result["sha256"] = "not_provided"
    if entry.sparse:
        result["observed_size"] = stat.st_size
        result["observed_mtime_ns"] = stat.st_mtime_ns
    if entry.sha256 is not None:
        if sha256(path) != entry.sha256:
            result.update(file="damaged", sha256="damaged", reason=["sha256_mismatch"])
            return result
        result.update(file="verified", sha256="verified", integrity="sha256")
    try:
        metadata = torrent_cache_path(root, entry)
        if not metadata.exists():
            result["reason"].append("metadata_not_cached")
        else:
            ti = validate_daily(metadata.read_bytes(), entry, root)
            result["bt_pieces"] = (
                "verified" if stat.st_size == ti.total_size() and verify_pieces(ti, path) else "damaged"
            )
            if result["bt_pieces"] != "verified":
                result["reason"].append("piece_hash_mismatch")
                if entry.sha256 is None:
                    result["file"] = "damaged"
            elif entry.sha256 is None:
                result.update(file="verified", integrity="bt_pieces")
    except Exception as exc:
        result["reason"].append(f"torrent_invalid: {exc}")
    if entry.sparse and result["bt_pieces"] != "verified":
        return result
    try:
        rows, days = _validate_parquet(path, entry, schema)
        result["format"] = "verified"
        if rows is None:
            result["reason"].append("pending_trading_day_index")
            return result
        result["quality"] = "verified"
        if entry.sparse and not schema.units_confirmed:
            result["quality"] = "pending"
            result["reason"].append("pending_units")
        result["rows"] = rows
        if entry.sparse:
            result["trading_days"] = days
    except Exception as exc:
        result["format"] = "invalid"
        result["quality"] = "invalid"
        result["reason"].append(f"source_content_invalid: {exc}")
    return result


def passed(result):
    return all(result[k] == "verified" for k in ("file", "format", "quality", "bt_pieces"))
