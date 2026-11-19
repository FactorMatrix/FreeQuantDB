"""Common paginated outputs for dashboard table views."""


def page(items, offset=0, limit=50):
    offset, limit = max(0, int(offset)), min(200, max(1, int(limit)))
    return {"items": items[offset : offset + limit], "total": len(items), "offset": offset, "limit": limit}


def task_row(task):
    entry = task["body"]["entry"]
    file = entry.get("file", "")
    return {
        k: task[k] for k in ("id", "status", "reason", "progress", "attempts", "updated_at", "metrics")
    } | {
        "dataset": entry.get("dataset"),
        "frequency": entry.get("frequency"),
        "date": entry.get("trading_date"),
        "period_kind": entry.get("period_kind", "day"),
        "period": entry.get("period", ""),
        "start_date": entry.get("start_date") or entry.get("trading_date"),
        "end_date": entry.get("end_date") or entry.get("trading_date"),
        "kind": task["body"]["kind"],
        "origin": task["body"].get("origin"),
        "size": entry.get("size") if entry.get("size") is not None else task["metrics"].get("native_size"),
        "version": entry.get("version"),
        "catalog_path": file.removeprefix("data/") + ".json" if file.startswith("data/") else None,
    }
