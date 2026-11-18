"""Canonical paths and descriptors shared by local and remote catalogs."""

from datetime import date
import json
import re

from ..bt.torrents import magnet_hash
from ..util import FQDBError
from .models import period_bounds

SCHEMA_VERSION = 5
DATASET_FIELDS = {"schema_version", "name", "fields", "instrument_column", "time_column"}
PACKAGE_STATS = ("start_date", "end_date", "rows", "instruments", "trading_days", "size_bytes")
PACKAGE_FIELDS = set(PACKAGE_STATS)
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_PERIOD_KIND = {4: "year", 6: "month", 8: "day"}


def json_object(payload):
    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise FQDBError("Duplicate JSON key")
        return value

    try:
        value = json.loads(payload, object_pairs_hook=pairs)
    except (UnicodeError, ValueError) as exc:
        raise FQDBError(f"Invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FQDBError("JSON must contain an object")
    return value


def scope_identity(scope):
    parts = scope.split("/") if isinstance(scope, str) else []
    if len(parts) != 4 or any(_SEGMENT.fullmatch(part) is None for part in parts):
        raise FQDBError("Dataset scope must be market/asset/data_type/frequency")
    market, asset, data_type, frequency = parts
    return {
        "scope": scope,
        "dataset": f"{market}/{asset}/{data_type}",
        "market": market,
        "asset": asset,
        "data_type": data_type,
        "frequency": frequency,
    }


def dataset_path(scope):
    return scope_identity(scope)["scope"] + "/index.json"


def _package_location(scope, period):
    kind = _PERIOD_KIND.get(len(period)) if isinstance(period, str) else None
    first, last = period_bounds(kind, period)
    scope_identity(scope)
    directory = f"{scope}/{period[:4]}"
    if kind != "year":
        directory += "/" + period[4:6]
    return f"{directory}/{period}.parquet.json", kind, first, last


def package_path(scope, period):
    return _package_location(scope, period)[0]


def _dataset_location(path):
    if not isinstance(path, str) or not path.endswith("/index.json"):
        raise FQDBError("Expected a dataset index path")
    scope = path.removesuffix("/index.json")
    return scope_identity(scope)


def _package_identity(path):
    if not isinstance(path, str) or not path.endswith(".parquet.json"):
        raise FQDBError("Expected a Parquet package descriptor path")
    parts = path.split("/")
    identity = scope_identity("/".join(parts[:4]))
    period = parts[-1].removesuffix(".parquet.json")
    canonical, kind, first, last = _package_location(identity["scope"], period)
    if canonical != path:
        raise FQDBError("Noncanonical Parquet package descriptor path")
    return identity | {"period": period, "period_kind": kind, "path": path}, first, last


def parse_dataset(path, payload):
    identity = _dataset_location(path)
    if not isinstance(payload, dict) or payload.keys() != DATASET_FIELDS:
        raise FQDBError("Invalid dataset descriptor fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise FQDBError("Unsupported dataset descriptor version")
    fields = payload["fields"]
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field.strip() for field in fields)
        or len(fields) != len(set(fields))
    ):
        raise FQDBError("Dataset fields must be a list of distinct column names")
    if not isinstance(payload["name"], str) or not payload["name"].strip():
        raise FQDBError("Dataset must have a display name")
    instrument, time = payload["instrument_column"], payload["time_column"]
    if instrument not in fields or time not in fields or instrument == time:
        raise FQDBError("Dataset instrument and time columns must name distinct fields")
    return identity | {"path": path} | payload


def parse_package(path, payload, *, require_magnet=False, strict_magnet=True):
    identity, first, last = _package_identity(path)
    if not isinstance(payload, dict) or payload.keys() - {"magnet"} != PACKAGE_FIELDS:
        raise FQDBError("Invalid package descriptor fields")
    start, end = payload["start_date"], payload["end_date"]
    try:
        if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise FQDBError("Package dates must use YYYY-MM-DD") from exc
    if not first <= start <= end <= last:
        raise FQDBError("Package coverage falls outside its path period")
    if any(type(payload[name]) is not int or payload[name] <= 0 for name in PACKAGE_STATS[2:]):
        raise FQDBError("Package statistics must be positive integers")
    date_count = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if not (
        payload["instruments"] <= payload["rows"]
        and payload["trading_days"] <= min(date_count, payload["rows"])
    ):
        raise FQDBError("Package instrument or trading-day count exceeds its coverage")
    magnet = payload.get("magnet")
    status, identity_hash, error = "missing", None, None
    if "magnet" in payload or require_magnet:
        try:
            identity_hash = magnet_hash(magnet)
        except (FQDBError, ValueError) as exc:
            if strict_magnet or require_magnet:
                raise FQDBError(f"Invalid package magnet: {exc}") from exc
            status, error = "invalid", str(exc)
        else:
            status = "available"
    data_file = "data/" + path.removesuffix(".json")
    return (
        identity
        | payload
        | {
            "file": data_file,
            "data_file": data_file,
            "magnet": magnet,
            "info_hash": identity_hash,
            "magnet_status": status,
            "magnet_error": error,
        }
    )


def parse_root(payload):
    if (
        not isinstance(payload, dict)
        or payload.keys() != {"schema_version", "datasets", "packages"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != SCHEMA_VERSION
    ):
        raise FQDBError("Expected version 5 catalog index")
    for kind in ("datasets", "packages"):
        paths = payload[kind]
        if (
            not isinstance(paths, list)
            or len(paths) > 100_000
            or any(not isinstance(path, str) for path in paths)
            or len(set(paths)) != len(paths)
        ):
            raise FQDBError(f"Invalid or duplicate {kind} paths in catalog index")
    scopes = {_dataset_location(path)["scope"] for path in payload["datasets"]}
    for path in payload["packages"]:
        identity, _, _ = _package_identity(path)
        if identity["scope"] not in scopes:
            raise FQDBError(f"Package references an unlisted dataset: {path}")
    return payload
