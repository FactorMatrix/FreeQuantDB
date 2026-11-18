"""Write local package descriptions and export a magnet-only catalog mirror."""

from datetime import date
from pathlib import Path
import calendar
import json

from ..bt.torrents import make_magnet, torrent_cache_path, validate_daily
from ..catalog.magnet import SCHEMA_VERSION, MagnetCatalog, dataset_path, package_path
from ..layout import runtime_path
from ..util import FQDBError, atomic_json, day, safe_path
from .paths import resolve_file


def _dataset_document(store, entry):
    schema = store.dataset(entry.dataset).for_frequency(entry.frequency)
    instrument, time = schema.primary_key
    return {
        "schema_version": SCHEMA_VERSION,
        "name": schema.id,
        "fields": list(schema.fields),
        "instrument_column": instrument,
        "time_column": time,
    }


def _package_document(entry):
    return {
        "start_date": entry.start,
        "end_date": entry.end,
        "rows": entry.rows,
        "instruments": len(entry.symbols),
        "trading_days": len(entry.days),
        "size_bytes": entry.size,
        "magnet": entry.magnet or make_magnet(entry),
    }


def write_publication(store, entry):
    """Describe an already verified local publication without exposing native metadata."""
    return MagnetCatalog(store.root).write_publication(
        f"{entry.dataset}/{entry.frequency}",
        _dataset_document(store, entry),
        entry.period,
        _package_document(entry),
    )


def _scope_dates(year, month, start_date, end_date):
    first = day(start_date) if start_date else None
    last = day(end_date) if end_date else None
    if month is not None and not 1 <= int(month) <= 12:
        raise FQDBError("month must be between 1 and 12")
    if year is not None:
        year = int(year)
        first_month, last_month = int(month or 1), int(month or 12)
        year_start = date(year, first_month, 1).isoformat()
        year_end = date(year, last_month, calendar.monthrange(year, last_month)[1]).isoformat()
        first = max(first or year_start, year_start)
        last = min(last or year_end, year_end)
    if first and last and first > last:
        raise FQDBError("start_date must not exceed end_date")
    return first, last


def _without_archived_children(entries):
    completed = {(e.dataset, e.frequency, e.period) for e in entries if e.complete}
    for entry in entries:
        if any(
            (entry.dataset, entry.frequency, entry.period[:length]) in completed
            for length in (4, 6)
            if length < len(entry.period)
        ):
            continue
        yield entry


def export_public_catalog(
    store,
    output,
    *,
    dataset=None,
    frequency=None,
    period=None,
    year=None,
    month=None,
    start_date=None,
    end_date=None,
    dry_run=False,
):
    """Export verified, visible local publications to one v5 catalog root."""
    if type(dry_run) is not bool:
        raise FQDBError("dry_run must be boolean")
    output = Path(output).expanduser().resolve()
    root = runtime_path(store.root, "catalog")
    if output == root.resolve():
        raise FQDBError("Export to a separate directory")
    if dataset and frequency and frequency not in store.dataset(dataset).frequencies:
        raise FQDBError(f"Unsupported frequency: {frequency}")
    first, last = _scope_dates(year, month, start_date, end_date)
    entries = []
    documents = {}
    for entry, evidence in store.active(dataset, frequency, first, last):
        if entry.sparse or evidence["verification"] != "full" or (period and entry.period != period):
            continue
        if month is not None and not any(int(trading_day[5:7]) == int(month) for trading_day in entry.days):
            continue
        if store.aggregate_stale(entry):
            continue
        scope = f"{entry.dataset}/{entry.frequency}"
        package_relative = package_path(scope, entry.period)
        dataset_relative = dataset_path(scope)
        package = safe_path(root, package_relative)
        descriptor = safe_path(root, dataset_relative)
        if not package.is_file() or not descriptor.is_file():
            continue
        # Hand-edited discovery metadata is local. An official mirror should
        # advertise only the verified publication this descriptor refers to.
        expected = _package_document(entry)
        if json.loads(package.read_text()) != expected:
            continue
        stat = resolve_file(store.root, entry, immutable=True).stat()
        if stat.st_size != evidence["size"] or stat.st_mtime_ns != evidence["mtime_ns"]:
            raise FQDBError(f"Published package needs verification: {entry.file}")
        validate_daily(torrent_cache_path(store.root, entry).read_bytes(), entry, store.root)
        entries.append(entry)
        documents[entry.info_hash] = (package_relative, dataset_relative, expected)

    datasets, packages = {}, {}
    for entry in _without_archived_children(entries):
        package_relative, dataset_relative, expected = documents[entry.info_hash]
        datasets[dataset_relative] = _dataset_document(store, entry)
        packages[package_relative] = expected
    if not packages:
        raise FQDBError("No verified local publications match the requested catalog scope")
    body = {"schema_version": SCHEMA_VERSION, "datasets": sorted(datasets), "packages": sorted(packages)}
    files = dict(sorted((datasets | packages).items())) | {"index.json": body}
    if not dry_run:
        targets = {relative: safe_path(output, relative) for relative in files}
        for relative, payload in files.items():
            atomic_json(targets[relative], payload)
    return {
        "output_dir": str(output),
        "schema_version": SCHEMA_VERSION,
        "magnet_only": True,
        "dry_run": dry_run,
        "entries": len(packages),
        "datasets": body["datasets"],
        "files": [{"path": relative} for relative in files],
        "external_publication": False,
    }
