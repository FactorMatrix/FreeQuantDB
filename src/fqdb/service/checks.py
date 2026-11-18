"""Audits and repair plans for immutable market packages."""

import json
import time

from ..catalog.models import Entry
from ..planning.planner import plan
from ..publish.paths import resolve_file
from ..util import FQDBError, now
from ..validation.coverage import coverage
from ..validation.files import inspect_file, passed


class Checks:
    def __init__(self, store, lock, activate_downloads):
        self.store = store
        self.lock = lock
        self.activate_downloads = activate_downloads

    def check(self, dataset, frequency, start_date=None, end_date=None, deep=False, progress=None, **scope):
        from ..catalog.index import select

        entries, start, end = select(
            self.store, dataset, frequency, start_date, end_date, scope.get("year"), scope.get("month")
        )
        schema = self.store.dataset(dataset)
        local = self.store.active(dataset, frequency, start, end)
        coverage_entries = entries
        latest = {e.key: e for e in entries}
        selected = {latest.get(e.key, e).info_hash: latest.get(e.key, e) for e, _ in local}
        covered = {d for e in selected.values() if not self.store.aggregate_stale(e) for d in e.days}
        wanted = {d for e in entries for d in e.days if (not start or d >= start) and (not end or d <= end)}
        preferred = scope.get("granularity") or (
            "month" if scope.get("month") else "year" if scope.get("year") else "day"
        )
        for d in sorted(wanted - covered):
            if d in covered:
                continue
            choices = [e for e in entries if d in e.days and not self.store.aggregate_stale(e)]
            choices.sort(
                key=lambda e: (
                    e.period_kind != preferred,
                    {"day": 0, "month": 1, "year": 2}[e.period_kind],
                )
            )
            if choices:
                entry = choices[0]
                selected[entry.info_hash] = entry
                covered.update(entry.days)
        if any(entry.sparse for entry in entries):
            preview = plan(
                self.store,
                dataset,
                frequency,
                start_date,
                end_date,
                year=scope.get("year"),
                month=scope.get("month"),
                granularity=scope.get("granularity"),
            )
            for item in preview["items"]:
                entry = Entry(**item["entry"])
                if entry.sparse:
                    selected[entry.info_hash] = entry
        # Optional overlapping packages are not missing local data. Inspect
        # selected local content and one source for each uncovered day.
        entries = list(selected.values())
        cached = {e.info_hash: c for e, c in local}
        rows = []
        begin = time.perf_counter()
        for index, entry in enumerate(entries):
            path = resolve_file(self.store.root, entry)
            before = path.stat() if path.exists() else None
            if progress:
                progress(index, len(entries), f"checking {entry.trading_date} v{entry.version}")
            result = inspect_file(self.store.root, entry, schema, deep, cached.get(entry.info_hash))
            if self.store.aggregate_stale(entry):
                result["quality"] = "pending"
                result["reason"].append("aggregate_source_revision")
            if deep and passed(result) and not entry.sparse:
                with self.lock:
                    after = path.stat()
                    if before and (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                        self.store.activate(entry)
                    else:
                        result.update(file="recheck_required")
                        result["reason"].append("file_changed_during_check")
            rows.append({"entry": entry.to_dict(), **result})
        result = {
            "dataset": dataset,
            "frequency": frequency,
            "start_date": start,
            "end_date": end,
            "rule_version": schema.rule_version,
            "created_at": now(),
            "deep": deep,
            "files": rows,
            "coverage": coverage(
                schema,
                frequency,
                coverage_entries,
                self.store.active(dataset, frequency, start, end),
                start,
                end,
                self.store.root,
                deep,
                observed_days={
                    r["entry"]["info_hash"]: r["trading_days"]
                    for r in rows
                    if r["format"] == "verified" and r["bt_pieces"] == "verified" and r.get("trading_days")
                },
            ),
            "elapsed_seconds": time.perf_counter() - begin,
            "summary": {
                "published": len(entries),
                "file_verified": sum(r["file"] in ("verified", "verified_cached") for r in rows),
                "anomalies": sum(
                    r["file"] == "damaged"
                    or r["format"] == "invalid"
                    or r["quality"] == "invalid"
                    or r["bt_pieces"] == "damaged"
                    for r in rows
                ),
            },
        }
        result["id"] = self.store.report(result)
        return result

    def repair(self, report, dry_run=True):
        items = [
            r["entry"]
            for r in report["files"]
            if r["file"] not in ("verified", "verified_cached") and r["format"] != "invalid"
        ]
        blocked = [r for r in report["files"] if r["format"] == "invalid"]
        result = {
            "items": items,
            "bytes": None if any(e["size"] is None for e in items) else sum(e["size"] for e in items),
            "unrepairable_source_content": blocked,
            "source_record_gaps": [
                r
                for r in report["coverage"].get("instruments", [])
                if r["status"] in ("source_record_gap", "metadata_conflict")
            ],
            "stale_packages": [
                r["entry"] for r in report["files"] if "aggregate_source_revision" in r["reason"]
            ],
            "source_unpublished": [
                d for d in report["coverage"]["days"] if d["status"] == "source_unpublished"
            ],
            "pending_metadata": report["coverage"]["status"] != "known",
            "task_ids": [],
        }
        if not dry_run:
            for item in items:
                e = Entry(**item)
                if e not in self.store.entries(latest=False):
                    # JSON converts tuples to lists: compare manifest identities separately.
                    if not any(
                        json.loads(json.dumps(x.to_dict())) == json.loads(json.dumps(e.to_dict()))
                        for x in self.store.entries(latest=False)
                    ):
                        raise FQDBError("Repair entry is not trusted")
                result["task_ids"].append(self.store.enqueue(e, force=True))
            self.activate_downloads(result["task_ids"])
        return result
