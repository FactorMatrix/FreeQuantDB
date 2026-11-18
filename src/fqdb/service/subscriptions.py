"""Subscription timing and explicit catalog refresh scheduling."""

from datetime import datetime, time as dt_time, timedelta, timezone
import json
import time
from zoneinfo import ZoneInfo

from ..util import FQDBError


def next_run(body, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    if not body.get("at"):
        return timestamp + int(body.get("interval", 86400))
    zone = ZoneInfo(body.get("timezone", "UTC"))
    clock = dt_time.fromisoformat(body["at"])
    local = datetime.fromtimestamp(timestamp, zone)
    candidate = datetime.combine(local.date(), clock, tzinfo=zone)
    # Resolve a skipped local time forwards; fold=0 chooses one occurrence only.
    candidate = candidate.astimezone(timezone.utc).astimezone(zone)
    if candidate.timestamp() <= timestamp:
        candidate = datetime.combine(local.date() + timedelta(days=1), clock, tzinfo=zone)
        candidate = candidate.astimezone(timezone.utc).astimezone(zone)
    return candidate.timestamp()


class Subscriptions:
    def __init__(self, store, jobs):
        self.store = store
        self.jobs = jobs
        self.last_schedule = 0

    def unsubscribe(self, identity):
        with self.store.db() as c:
            c.execute("DELETE FROM subscriptions WHERE id=?", (identity,))
        return {"removed": identity}

    def subscribe(self, body):
        body = dict(body)
        body.setdefault("download", False)
        if body["download"] is not False:
            raise FQDBError("Subscriptions refresh the catalog; choose packages explicitly to download them")
        if body.get("at"):
            dt_time.fromisoformat(body["at"])
            ZoneInfo(body.get("timezone", "UTC"))
        identity = self.store.subscribe(body)
        if body.get("at"):
            with self.store.db() as c:
                c.execute("UPDATE subscriptions SET next_run=? WHERE id=?", (next_run(body), identity))
        return {
            "id": identity,
            "download": body["download"],
            "next_run": next(s["next_run"] for s in self.store.subscriptions() if s["id"] == identity),
        }

    def schedule(self, *, subscriptions=False):
        if time.monotonic() - self.last_schedule < 1:
            return
        self.last_schedule = time.monotonic()
        self.jobs.tick()
        if not subscriptions:
            return
        for subscription in self.store.subscriptions():
            body = subscription["body"]
            if not body.get("enabled", True):
                continue
            running = body.get("running_job")
            if running:
                job = self.jobs.get(running)
                if job["status"] == "paused":
                    # An interrupted job is visible for manual retry. Do not
                    # replay it or silently create another copy on UI entry.
                    body.update(
                        enabled=False,
                        running_job=None,
                        last_error="上次检查已中断，请手动启用订阅或刷新目录。",
                    )
                    with self.store.db() as c:
                        c.execute(
                            "UPDATE subscriptions SET body=? WHERE id=?",
                            (json.dumps(body), subscription["id"]),
                        )
                    continue
                if job["status"] in ("queued", "running"):
                    continue
                body.update(
                    last_check=job["updated_at"],
                    last_result=job["result"],
                    last_error=job["error"],
                    running_job=None,
                )
                with self.store.db() as c:
                    c.execute(
                        "UPDATE subscriptions SET body=?,next_run=? WHERE id=?",
                        (json.dumps(body), next_run(body), subscription["id"]),
                    )
                continue
            if subscription["next_run"] > time.time():
                continue
            job = self.jobs.submit("catalog_refresh", body)
            body["running_job"] = job["job_id"]
            with self.store.db() as c:
                c.execute(
                    "UPDATE subscriptions SET body=?,next_run=? WHERE id=?",
                    (json.dumps(body), next_run(body), subscription["id"]),
                )
