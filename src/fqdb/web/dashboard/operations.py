"""Task, transfer and configuration views for the dashboard."""

from collections import Counter, defaultdict

from ...layout import runtime_path
from .rows import page, task_row


class OperationViews:
    def __init__(self, owner):
        self.owner = owner
        self.store = owner.store

    def settings(self):
        return {
            "settings": self.owner.config.read(),
            "data_dir": str(self.store.root),
            "subscriptions": self.store.subscriptions(),
        }

    def runtime(self):
        with self.owner.lock:
            transfers = self.owner.transfer_status()
            if transfers:
                entries = self.store.entries_by_hash(t["id"] for t in transfers)
                for transfer in transfers:
                    entry = entries.get(transfer["id"])
                    transfer.update(
                        dataset=entry.dataset if entry else None,
                        frequency=entry.frequency if entry else None,
                        date=entry.trading_date if entry else None,
                        period_kind=entry.period_kind if entry else None,
                        period=entry.period if entry else None,
                    )
            return {
                "provider": self.owner.seeding.enabled,
                "provision": self.owner.seeding.status(),
                "provider_issues": self.owner.seeding.issues,
                "settings": self.owner.config.read(),
                "transfers": transfers,
                "download_rate": sum(t["download_rate"] for t in transfers),
                "upload_rate": sum(t["upload_rate"] for t in transfers),
                "peer_connections": sum(t["peers"] for t in transfers),
                "data_dir": str(self.store.root),
                "last_error": self.owner.last_error,
                "subscriptions": self.store.subscriptions(),
            }

    def layout(self, **filters):
        root = self.store.root
        paths = {"data": root / "data", "catalog": root / "catalog"}
        paths.update({name: runtime_path(root, name) for name in ("state", "staging", "imports", "logs")})
        return {
            "data_dir": str(root),
            "layout": "current",
            "format_version": 2,
            "paths": {name: {"path": str(path), "exists": path.is_dir()} for name, path in paths.items()},
        }

    def features(self, **filters):
        with self.owner.lock:
            return self.owner.management_status()

    def publisher_options(self):
        return {
            "items": [
                {"dataset": schema["id"], "frequency": frequency}
                for schema in self.store.datasets()
                for frequency in schema["frequencies"]
            ]
        }

    def tasks(self, dataset=None, frequency=None, status=None, month=None, offset=0, limit=50):
        def period_key(row):
            if row["period_kind"] == "year":
                return row["period"]
            return row["date"][:7] if row["date"] else "—"

        rows = [
            task_row(self.owner.transfers.task_state(task)) for task in self.store.tasks(newest_first=True)
        ]
        rows = [
            r
            for r in rows
            if (not dataset or r["dataset"] == dataset)
            and (not frequency or r["frequency"] == frequency)
            and (not status or r["status"] == status)
            and (not month or period_key(r).startswith(month))
        ]
        groups = defaultdict(list)
        for r in rows:
            groups[(r["dataset"], r["frequency"], period_key(r))].append(r)
        grouped = []
        for (group_dataset, group_frequency, group_month), items in groups.items():
            known_size = all(t["size"] is not None for t in items)
            weights = [max(t["size"], 1) if known_size else 1 for t in items]
            grouped.append(
                {
                    "dataset": group_dataset,
                    "frequency": group_frequency,
                    "month": group_month,
                    "total": len(items),
                    "counts": dict(Counter(t["status"] for t in items)),
                    "progress": sum(t["progress"] * w for t, w in zip(items, weights)) / sum(weights),
                    "size": sum(t["size"] for t in items) if known_size else None,
                }
            )
        return page(rows, offset, limit) | {
            "groups": grouped,
            "counts": dict(Counter(r["status"] for r in rows)),
        }

    def plan(self, **args):
        offset, limit = args.pop("offset", 0), args.pop("limit", 50)
        result = self.owner.dispatch("plan", args)
        items = result.pop("items")
        unpublished = result.pop("source_unpublished", [])
        return (
            page(items, offset, limit)
            | result
            | {
                "ready": sum(i["action"] == "ready" for i in items),
                "revision": sum(i["action"] == "revision" for i in items),
                "to_download": sum(i["action"] in ("revision", "verify_or_download") for i in items),
                "no_magnet": sum(i["action"] == "no_magnet" for i in items),
                "source_unpublished": unpublished[:50],
                "source_unpublished_count": len(unpublished),
            }
        )
