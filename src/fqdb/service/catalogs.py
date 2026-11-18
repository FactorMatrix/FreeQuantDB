"""Connect the file catalog to explicit download and refresh operations."""

from calendar import monthrange
from dataclasses import replace
from datetime import date

from ..catalog.magnet import OFFICIAL_SOURCE, MagnetCatalog
from ..catalog.models import Entry
from ..publish.paths import resolve_file
from ..util import FQDBError, day, now


def scope_bounds(start_date=None, end_date=None, year=None, month=None):
    if month is not None and year is None:
        raise FQDBError("A download month requires a year")
    first = day(start_date) if start_date else None
    last = day(end_date) if end_date else None
    if year is not None:
        lower_month, upper_month = (1, 12) if month is None else (month, month)
        try:
            lower = date(year, lower_month, 1).isoformat()
            upper = date(year, upper_month, monthrange(year, upper_month)[1]).isoformat()
        except (TypeError, ValueError) as exc:
            raise FQDBError("Invalid calendar year or month") from exc
        first, last = max(first or lower, lower), min(last or upper, upper)
    if first and last and first > last:
        raise FQDBError("start_date must not exceed end_date")
    return first, last


def select_periods(packages, first, last):
    """Prefer canonical archive packages over overlapping smaller packages."""
    selected = []
    kind_order = {"year": 0, "month": 1, "day": 2}

    def priority(item):
        unavailable = item["magnet_status"] != "available" and item["local_status"] != "available"
        return unavailable, kind_order[item["period_kind"]], item["path"]

    for package in sorted(packages, key=priority):
        lower, upper = max(package["start_date"], first or ""), min(package["end_date"], last or "9999-12-31")
        if any(start <= lower <= upper <= end for start, end, _ in selected):
            continue
        selected.append((lower, upper, package))
    return [package for _, _, package in sorted(selected, key=lambda result: result[:2])]


class Catalogs:
    def __init__(self, owner):
        self.owner, self.store, self.config = owner, owner.store, owner.config
        self.catalog = MagnetCatalog(self.store.root)

    def inspect(self, *, deep=False, catalog_path=None):
        scanned = self.catalog.scan(deep=deep, path=catalog_path)
        active = {entry.file: (entry, evidence) for entry, evidence in self.store.active()}
        tasks = {task["id"]: self.owner.transfers.task_state(task) for task in self.store.tasks()}
        for package in scanned["packages"]:
            previous = active.get(package["file"])
            identity = package.get("info_hash")
            package["local_hash"] = previous[0].info_hash if previous else None
            package["task"] = tasks.get(identity)
            package["version_status"] = "unknown"
            if previous and package["local_status"] != "missing":
                if identity and previous[0].info_hash != identity:
                    # The file on disk belongs to the previously installed package.
                    package["local_status"] = "new_version"
                    package["version_status"] = "new_version"
                elif package["local_status"] == "available":
                    entry, evidence = previous
                    path = resolve_file(self.store.root, entry)
                    try:
                        stat = path.stat()
                    except FileNotFoundError:
                        continue
                    if (stat.st_size, stat.st_mtime_ns) == (evidence["size"], evidence["mtime_ns"]):
                        package["version_status"] = "current"
        return scanned

    def _package(self, path, *, deep=False):
        packages = self.inspect(catalog_path=path, deep=deep)["packages"]
        if len(packages) != 1:
            raise FQDBError(f"No visible catalog package: {path}")
        return packages[0]

    def check_package(self, catalog_path):
        return self._package(catalog_path, deep=True)

    def _entry(self, package):
        value = Entry.from_magnet(
            package["dataset"], package["frequency"], package["period"], package["magnet"]
        )
        return replace(value, size=package["size_bytes"])

    def _item(self, package):
        if package["magnet_status"] != "available":
            return {
                "entry": None,
                "package": package,
                "action": "no_magnet",
                "local": package["local_status"],
            }
        entry = self.store.validate_entry(self._entry(package))
        action = (
            "ready"
            if package["version_status"] == "current"
            else "revision"
            if package["version_status"] == "new_version"
            else "verify_or_download"
        )
        return {
            "entry": entry.to_dict(),
            "package": package,
            "action": action,
            "local": package["local_status"],
        }

    def preview(
        self,
        dataset,
        frequency,
        start_date=None,
        end_date=None,
        year=None,
        month=None,
        *,
        granularity=None,
        allow_daily_fallback=False,
    ):
        first, last = scope_bounds(start_date, end_date, year, month)
        preferred_kind = "month" if month else "year" if year else "day"
        expected = granularity or (preferred_kind if allow_daily_fallback else "auto")
        if expected not in ("auto", "day", "month", "year") or type(allow_daily_fallback) is not bool:
            raise FQDBError("Invalid download granularity or daily fallback option")
        snapshot = self.inspect()
        candidates = [
            item
            for item in snapshot["packages"]
            if item["dataset"] == dataset
            and item["frequency"] == frequency
            and item["start_date"]
            and item["end_date"]
            and (not first or item["end_date"] >= first)
            and (not last or item["start_date"] <= last)
        ]
        preferred = [item for item in candidates if expected in ("auto", item["period_kind"])]
        fallback = expected in ("month", "year") and not preferred
        if fallback and allow_daily_fallback:
            preferred = [item for item in candidates if item["period_kind"] == "day"]
        items = [self._item(item) for item in select_periods(preferred, first, last)]
        schema = self.store.dataset(dataset, required=False)
        calendar = schema.calendars.get(frequency, []) if schema else []
        unpublished = [
            d
            for d in calendar
            if (not first or first <= d)
            and (not last or d <= last)
            and not any(item["start_date"] <= d <= item["end_date"] for item in candidates)
        ]
        return {
            "dataset": dataset,
            "frequency": frequency,
            "start_date": first,
            "end_date": last,
            "items": items,
            "bytes": sum(
                i["entry"]["size"] for i in items if i["action"] in ("verify_or_download", "revision")
            ),
            "source_unpublished": unpublished,
            "catalog_empty": not items,
            "calendar": "known_within_declared_range" if calendar else "pending",
            "expected_kind": expected,
            "available_granularities": sorted({i["period_kind"] for i in candidates}),
            "fallback_required": fallback,
            "daily_fallback_selected": fallback and allow_daily_fallback,
            "stale_packages": [],
            "fallback_reason": "Requested package kind unavailable" if fallback else None,
        }

    def _enqueue(self, items):
        ids = []
        with self.owner.lock:
            for item in items:
                if item["action"] in ("ready", "no_magnet"):
                    continue
                entry = self.store.add_magnet(self._entry(item["package"]))
                old = self.store.task(entry.info_hash)
                force = old is not None and old["status"] in ("complete", "failed", "cancelled")
                ids.append(self.store.enqueue(entry, force=force))
            self.owner.transfers.activate(ids)
        return ids

    def plan(self, scope, *, download=False):
        preview = self.preview(**scope)
        if not download:
            return preview
        if preview["fallback_required"] and not preview["daily_fallback_selected"]:
            return {"task_ids": [], "plan": preview, "status": "fallback_required"}
        ids = self._enqueue(preview["items"])
        if ids:
            status = "queued"
        elif preview["catalog_empty"]:
            status = "source_unpublished"
        elif any(item["action"] == "no_magnet" and item["local"] != "available" for item in preview["items"]):
            status = "no_magnet"
        else:
            status = "complete"
        return {"task_ids": ids, "plan": preview, "status": status}

    def download_package(self, catalog_path):
        package = self._package(catalog_path)
        if package["magnet_status"] != "available":
            raise FQDBError(f"Package has no usable magnet link: {catalog_path}")
        ids = self._enqueue([self._item(package)])
        return {"task_ids": ids, "package": package, "status": "queued" if ids else "complete"}

    def export(self, output_dir, *, dry_run=False, **scope):
        from ..publish.catalog import export_public_catalog

        return export_public_catalog(self.store, output_dir, dry_run=dry_run, **scope)

    def refresh(self, body):
        sources = self.config.read()["sources"]
        source = body.get("source") or (sources[0] if sources else OFFICIAL_SOURCE)
        if not isinstance(source, str) or not source.strip():
            raise FQDBError("A catalog source address is required")
        try:
            with self.owner.storage_lock:
                result = self.catalog.sync(source)
        except Exception as exc:
            self._record_source(source, "catalog_unavailable", error=str(exc))
            raise
        self._record_source(
            source,
            "catalog_available",
            result={
                key: result[key]
                for key in ("added", "updated", "removed", "hidden", "unchanged")
                if key in result
            },
        )
        return result

    def _record_source(self, source, status, **details):
        with self.owner.lock:
            checks = self.config.read()["source_checks"]
            self.config.update(
                source_checks=checks | {source: {"checked_at": now(), "status": status, **details}}
            )

    def add(self, source):
        # Sync before saving so a failed import cannot replace the working mirror.
        result = self.refresh({"source": source})
        with self.owner.lock:
            sources = self.config.read()["sources"]
            self.config.update(sources=[source, *(item for item in sources if item != source)])
        return result

    def remove(self, source):
        sources = self.config.read()["sources"]
        settings = self.config.update(sources=[item for item in sources if item != source])
        return {"sources": settings["sources"], "removed": source}

    def import_local(self, source, relative=None):
        with self.owner.storage_lock:
            return self.catalog.import_local(source, relative=relative)

    def migrate(self, source, dataset, frequency, period):
        from ..publish.migrate import migrate_package

        with self.owner.storage_lock:
            return migrate_package(self.store, source, dataset=dataset, frequency=frequency, period=period)

    def scan(self, *, deep=False, progress=None):
        return self.inspect(deep=deep)
