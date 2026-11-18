"""Compare local Parquet evidence with a catalog descriptor without contacting peers."""

from datetime import date, datetime
import hashlib
import json

import pyarrow as pa
import pyarrow.parquet as pq

from ..util import FQDBError, atomic_json, safe_path
from .magnet_manifest import PACKAGE_STATS
from .models import period_bounds
from ..schema.trading_days import minute_day_groups

_LIGHT = ("size_bytes", "fields", "rows")
_COVERAGE = ("instruments", "trading_days", "start_date", "end_date")


def problem(path, code, message, *, expected=None, observed=None):
    return {"path": path, "code": code, "message": message, "expected": expected, "observed": observed}


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    raise FQDBError(f"Expected a date or timestamp, found {value!r}")


def _coverage(reader, dataset, package):
    instrument, time = dataset["instrument_column"], dataset["time_column"]
    symbols, dates = set(), set()
    explicit_day = package["frequency"] != "1m" or pa.types.is_date(reader.schema_arrow.field(time).type)
    if not explicit_day:
        bounds = period_bounds(package["period_kind"], package["period"])
        assigned = minute_day_groups(reader, *bounds)
        if assigned:
            dates.update(date.fromisoformat(d) for d in assigned)
    columns = [instrument, time] if explicit_day else [instrument]
    for batch in reader.iter_batches(columns=columns, batch_size=65536):
        for symbol in batch[0].unique().to_pylist():
            if symbol is None or symbol == "":
                raise FQDBError("Parquet contains an empty instrument key")
            symbols.add(symbol)
        if explicit_day:
            dates.update(_date(value) for value in batch[1].unique().to_pylist())
    if not symbols:
        raise FQDBError("Parquet contains no usable instrument keys")
    return {"instruments": len(symbols)} | (
        {
            "trading_days": len(dates),
            "start_date": min(dates).isoformat(),
            "end_date": max(dates).isoformat(),
        }
        if dates
        else {}
    )


def _fingerprint(path, dataset):
    stat = path.stat()
    return [
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        dataset["instrument_column"],
        dataset["time_column"],
    ]


def _cached(path, fingerprint):
    try:
        evidence = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(evidence, dict)
        or evidence.get("fingerprint") != fingerprint
        or not isinstance(evidence.get("observed"), dict)
        or not set(_LIGHT) <= evidence["observed"].keys()
        or "coverage_error" not in evidence
    ):
        return None
    return evidence


def _evidence(path, dataset, package, fingerprint, deep):
    observed = {"size_bytes": fingerprint[2]}
    coverage_error = None
    with pq.ParquetFile(path) as parquet:
        observed["fields"] = parquet.schema_arrow.names
        observed["rows"] = parquet.metadata.num_rows
        if deep and all(column in observed["fields"] for column in fingerprint[-2:]):
            try:
                observed.update(_coverage(parquet, dataset, package))
            except (pa.ArrowException, OSError, ValueError, TypeError) as exc:
                coverage_error = str(exc)
    if _fingerprint(path, dataset) != fingerprint:
        raise FQDBError("Parquet changed while it was being checked")
    return {"fingerprint": fingerprint, "observed": observed, "coverage_error": coverage_error}


def inspect_package(root, cache_dir, dataset, package, *, deep=False):
    relative = package["file"]
    expected = {field: package[field] for field in PACKAGE_STATS} | {"fields": dataset["fields"]}
    result = {"expected": expected, "observed": {}, "issues": [], "coverage_status": "pending"}
    try:
        path = safe_path(root, relative)
        if not path.exists():
            return result | {"local_status": "missing"}
        if not path.is_file():
            raise FQDBError("Data path is not a regular Parquet file")
        fingerprint = _fingerprint(path, dataset)
        result["observed"]["size_bytes"] = fingerprint[2]
        cache = cache_dir / (hashlib.sha256(relative.encode()).hexdigest() + ".json")
        evidence = None if deep else _cached(cache, fingerprint)
        if evidence is None:
            evidence = _evidence(path, dataset, package, fingerprint, deep)
            try:
                atomic_json(cache, evidence)
            except OSError:
                pass  # Verification remains usable with a read-only evidence cache.
    except (pa.ArrowException, OSError, ValueError) as exc:
        return result | {
            "local_status": "invalid",
            "issues": [
                problem(relative, "parquet", str(exc), expected=expected, observed=result["observed"])
            ],
        }
    observed = evidence["observed"]
    light = [
        problem(
            relative, field, f"Parquet {field} differs", expected=expected[field], observed=observed[field]
        )
        for field in _LIGHT
        if expected[field] != observed[field]
    ]
    coverage = [
        problem(
            relative, field, f"Parquet {field} differs", expected=expected[field], observed=observed[field]
        )
        for field in _COVERAGE
        if field in observed and expected[field] != observed[field]
    ]
    status = "mismatch" if coverage else "verified" if "trading_days" in observed else "pending"
    if evidence["coverage_error"]:
        coverage.append(problem(relative, "coverage", evidence["coverage_error"]))
        status = "error"
    return result | {
        "local_status": "invalid" if light or coverage else "available",
        "coverage_status": status,
        "issues": light + coverage,
        "observed": observed,
    }
