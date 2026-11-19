"""Read-only dashboard views of the current on-disk magnet catalog."""

from collections import Counter, defaultdict

from ...catalog.magnet_manifest import dataset_path, json_object, parse_dataset
from ...schema.assets import DEFAULT_DATASET
from ...util import FQDBError, day, safe_path
from .rows import page, task_row

_WAITING_PEERS = {"waiting_peers", "waiting_metadata_peers"}
_RUNNING = {"queued", "fetching_metadata", "downloading", "checking", "moving", "retry_wait", "preparing"}


def _package_status(package, task):
    if package["magnet_status"] == "invalid" or package["local_status"] == "invalid":
        return "anomaly"
    if task:
        status = task["status"]
        if status in _WAITING_PEERS or (
            "no_available_peers" in (task["reason"] or "") and package["version_status"] != "current"
        ):
            return "no_peers"
        if status in _RUNNING:
            return "downloading"
    if package["local_status"] == "new_version" or package.get("version_status") == "new_version":
        return "revision"
    return {"available": "local", "missing": "missing"}.get(package["local_status"], "pending")


def _edge(packages, name, function):
    return function((p[name] for p in packages if p.get(name)), default=None)


def _scope_stats(dataset, packages):
    local = [p for p in packages if p["local_status"] in ("available", "invalid", "new_version")]
    counts = Counter(p["status"] for p in packages)
    counts_by_period = Counter(p["period_kind"] for p in packages)
    ordered = sorted(packages, key=lambda p: p.get("start_date") or "")
    complete = all(p.get("start_date") and p.get("end_date") for p in ordered)
    separate = complete and all(
        left["end_date"] < right["start_date"] for left, right in zip(ordered, ordered[1:])
    )
    published_rows = (
        sum(p["rows"] for p in packages)
        if separate and all(p.get("rows") is not None for p in packages)
        else None
    )
    valid = [p for p in packages if p["local_status"] == "available"]
    return dataset | {
        "published": len(packages),
        "local": len(local),
        "bytes": sum((p.get("observed") or {}).get("size_bytes") or 0 for p in local),
        "published_rows": published_rows,
        "published_instruments": (
            packages[0].get("instruments") if len(packages) == 1 else dataset.get("instruments")
        ),
        "start_date": _edge(packages, "start_date", min),
        "end_date": _edge(packages, "end_date", max),
        "latest_published": _edge(packages, "end_date", max),
        "latest_local": max(
            (p["observed"]["end_date"] for p in valid if (p.get("observed") or {}).get("end_date")),
            default=None,
        ),
        "status": next(
            (
                status
                for status in ("anomaly", "revision", "no_peers", "downloading", "missing", "local")
                if counts[status]
            ),
            "pending",
        ),
        "counts": dict(counts),
        "period_counts": dict(counts_by_period),
        "downloadable": sum(p["magnet_status"] == "available" for p in packages),
        "coverage_pending": sum(
            p["local_status"] == "available" and p.get("coverage_status") not in ("verified", "available")
            for p in packages
        ),
    }


def _period_months(package):
    period = package["period"]
    if package["period_kind"] != "year":
        return {period[4:6]}
    first, last = package.get("start_date"), package.get("end_date")
    if not first or not last:
        return set()
    return {f"{number:02}" for number in range(int(first[5:7]), int(last[5:7]) + 1)}


class CatalogViews:
    def __init__(self, owner):
        self.owner = owner

    def _snapshot(self, *, catalog_path=None, scope=None):
        catalog = self.owner.catalogs.inspect(catalog_path=catalog_path, scope=scope)
        groups = defaultdict(list)
        packages = []
        for package in catalog["packages"]:
            task = package.get("task")
            row = package | {
                "status": _package_status(package, task),
                "task": task_row(task) if task else None,
            }
            groups[row["scope"]].append(row)
            packages.append(row)
        return catalog["datasets"], packages, groups, catalog.get("issues", [])

    @staticmethod
    def _scope(datasets, dataset, frequency):
        scope = f"{dataset}/{frequency}"
        found = next((d for d in datasets if d["scope"] == scope), None)
        if found is None:
            raise FQDBError(f"数据集目录中没有 {scope}")
        return found

    def summary(self, runtime, include_runtime="true"):
        if include_runtime not in ("true", "false"):
            raise FQDBError("include_runtime must be true or false")
        datasets, packages, groups, issues = self._snapshot()
        tasks = [self.owner.transfers.task_state(task) for task in self.owner.store.tasks(newest_first=True)]
        rows = [_scope_stats(d, groups[d["scope"]]) for d in datasets]
        counts = Counter(p["status"] for p in packages)
        issues = [issue for issue in issues if issue["code"] in ("dataset_descriptor", "package_descriptor")]
        result = {
            "totals": {
                "registered": len(rows),
                "published_datasets": sum(r["published"] > 0 for r in rows),
                "published": len(packages),
                "local_files": sum(r["local"] for r in rows),
                "bytes": sum(r["bytes"] for r in rows),
                "missing": counts["missing"],
                "anomaly": counts["anomaly"],
                "revision": counts["revision"],
                "no_peers": counts["no_peers"],
                "latest_local": max((r["latest_local"] for r in rows if r["latest_local"]), default=None),
                "coverage_pending": sum(r["coverage_pending"] for r in rows),
            },
            "datasets": rows,
            "issues": issues[:20],
            "issue_count": len(issues),
            "task_counts": dict(Counter(t["status"] for t in tasks)),
            "latest_task": task_row(tasks[0]) if tasks else None,
        }
        if include_runtime == "true":
            result["runtime"] = runtime()
        return result

    def dataset(self, dataset, frequency):
        datasets, _, groups, _ = self._snapshot(scope=f"{dataset}/{frequency}")
        index = self._scope(datasets, dataset, frequency)
        packages = groups[index["scope"]]
        registered = self.owner.store.dataset(dataset, required=False)
        specific = (
            registered.for_frequency(frequency)
            if registered and frequency in registered.frequencies
            else None
        )
        types = specific.fields if specific else {}
        query_supported = bool(specific and set(types) == set(index["fields"]))
        identities = {p.get("info_hash") for p in packages if p["local_status"] == "available"}
        symbols = {
            symbol
            for entry in self.owner.store.entries(dataset, frequency)
            if entry.info_hash in identities
            for symbol in (entry.symbols or ())
        }
        return {
            "schema": {
                "fields": {field: types.get(field) for field in index["fields"]},
                "timezone": specific.timezone if specific else None,
                "query_supported": query_supported,
                "instrument_column": index["instrument_column"],
                "time_column": index["time_column"],
            },
            "summary": _scope_stats(index, packages),
            "symbols": sorted(symbols)[:100],
        }

    def browse(
        self,
        dataset,
        frequency,
        year=None,
        month=None,
        status=None,
        start_date=None,
        end_date=None,
        offset=0,
        limit=40,
    ):
        datasets, _, groups, _ = self._snapshot(scope=f"{dataset}/{frequency}")
        index = self._scope(datasets, dataset, frequency)
        packages = groups[index["scope"]]
        months = defaultdict(set)
        for package in packages:
            months[package["period"][:4]].update(_period_months(package))
        first = day(start_date) if start_date else None
        last = day(end_date) if end_date else None
        if first and last and first > last:
            raise FQDBError("开始日期不能晚于结束日期")
        selected = [
            p
            for p in packages
            if (not year or p["period"][:4] == str(year))
            and (not month or str(month).zfill(2) in _period_months(p))
            and (not status or p["status"] == status)
            and (not first or not p.get("end_date") or p["end_date"] >= first)
            and (not last or not p.get("start_date") or p["start_date"] <= last)
        ]
        selected.sort(key=lambda p: (p.get("end_date") or "", p["period"]), reverse=True)
        return page(selected, offset, limit) | {
            "years": sorted(months, reverse=True),
            "year_months": {key: sorted(value) for key, value in months.items()},
        }

    def package(self, catalog_path):
        datasets, packages, _, _ = self._snapshot(catalog_path=catalog_path)
        found = next((p for p in packages if p["path"] == catalog_path), None)
        if found is None:
            raise FQDBError(f"当前目录中没有数据包 {catalog_path}")
        index = self._scope(datasets, found["dataset"], found["frequency"])
        schema = self.owner.store.dataset(index["dataset"], required=False)
        query_supported = (
            found["local_status"] == "available"
            and found["version_status"] == "current"
            and schema is not None
            and index["frequency"] in schema.frequencies
            and set(schema.fields_for(index["frequency"])) == set(index["fields"])
        )
        return found | {"query_supported": query_supported}

    def audit(self, dataset, frequency, start_date=None, end_date=None, status=None, offset=0, limit=30):
        return self.browse(
            dataset,
            frequency,
            status=status,
            start_date=start_date,
            end_date=end_date,
            offset=offset,
            limit=limit,
        )

    def validate_query(self, args):
        scope = f"{args.get('dataset', DEFAULT_DATASET)}/{args.get('frequency', '1d')}"
        relative = dataset_path(scope)
        root = self.owner.store.root
        path = safe_path(root, f"catalog/{relative}")
        if path != root / "catalog" / relative or not path.is_file():
            raise FQDBError(f"数据集目录中没有 {scope}")
        parse_dataset(relative, json_object(path.read_bytes()))
