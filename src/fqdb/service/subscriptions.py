"""Schedule catalog checks and scoped downloads in the active Gateway worker."""

from datetime import datetime, time as dt_time, timedelta, timezone
import json
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..catalog.magnet import OFFICIAL_SOURCE
from ..util import FQDBError
from .catalogs import scope_bounds

DEFAULT_INTERVAL = 3 * 60 * 60
_SCOPE_OPTIONS = ("start_date", "end_date", "year", "month")
_PACKAGE_OPTIONS = ("granularity", "allow_daily_fallback")
_SELECTION_OPTIONS = ("dataset", "frequency", "source", "download", *_SCOPE_OPTIONS, *_PACKAGE_OPTIONS)
_SCHEDULE_OPTIONS = ("interval", "at", "timezone", "enabled")
_RESULT_FIELDS = ("last_check", "last_result", "last_error")


def next_run(body, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    if not body.get("at"):
        return timestamp + int(body.get("interval", DEFAULT_INTERVAL))
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
    def __init__(self, owner):
        self.owner, self.store, self.jobs = owner, owner.store, owner.jobs
        self.gateway_active = False
        self.initial_checks = set()
        self.resume_pending = set()
        self.wake_at = 0
        self.last_schedule = 0

    def activate_gateway(self):
        if not self.gateway_active:
            self.gateway_active = True
            self.initial_checks = {
                row["id"] for row in self.store.subscriptions() if row["body"].get("enabled", True)
            }
            self.resume_pending = self.initial_checks.copy()
            self.wake_at = 0 if self.initial_checks else None
            self.last_schedule = 0

    def has_work(self):
        return self.gateway_active and self.wake_at is not None

    def _wake(self):
        self.wake_at = 0
        self.last_schedule = 0

    def _cancel_unused(self, job_id):
        if job_id and not any(row["body"].get("running_job") == job_id for row in self.store.subscriptions()):
            self.jobs.set(job_id, status="cancelled")

    def unsubscribe(self, identity):
        previous = self.store.subscription(identity)
        self.store.unsubscribe(identity)
        if previous:
            self._cancel_unused(previous["body"].get("running_job"))
        self.initial_checks.discard(identity)
        self.resume_pending.discard(identity)
        self._wake()
        return {"removed": identity}

    def subscribe(self, options):
        body = {key: options[key] for key in (*_SELECTION_OPTIONS, *_SCHEDULE_OPTIONS) if key in options}
        body.setdefault("enabled", True)
        body.setdefault("download", False)
        if type(body["enabled"]) is not bool or type(body["download"]) is not bool:
            raise FQDBError("Subscription enabled and download settings must be boolean")
        if body.get("dataset") is not None and not isinstance(body["dataset"], str):
            raise FQDBError("A subscription dataset must be a registered dataset identifier")
        if body.get("dataset"):
            body["frequency"] = body.get("frequency") or "1d"
            scope_bounds(**{key: body[key] for key in _SCOPE_OPTIONS if key in body})
            if (
                body.get("granularity") not in (None, "auto", "day", "month", "year")
                or type(body.get("allow_daily_fallback", False)) is not bool
            ):
                raise FQDBError("Invalid download granularity or daily fallback option")
            identity = f"{body['dataset']}/{body['frequency']}"
        else:
            for key in ("dataset", "frequency", *_SCOPE_OPTIONS, *_PACKAGE_OPTIONS):
                body.pop(key, None)
            body["download"] = False
            identity = "catalog"

        interval = body.get("interval", DEFAULT_INTERVAL)
        try:
            if int(interval) < 10 or (isinstance(interval, float) and not interval.is_integer()):
                raise ValueError()
            body["interval"] = int(interval)
        except (ValueError, TypeError, OverflowError) as exc:
            raise FQDBError("Subscription interval must be an integer of at least 10 seconds") from exc
        if body.get("at"):
            try:
                if dt_time.fromisoformat(body["at"]).tzinfo is not None:
                    raise ValueError()
                ZoneInfo(body.get("timezone", "UTC"))
            except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
                raise FQDBError("Daily subscription requires a local time and valid IANA time zone") from exc
        elif body.get("at") is not None:
            body.pop("at", None)
        if body.get("source") is not None and (
            not isinstance(body["source"], str) or not body["source"].strip()
        ):
            raise FQDBError("A catalog source address is required")

        previous = self.store.subscription(identity)
        last = previous["body"] if previous else {}
        same_scope = previous and all(body.get(key) == last.get(key) for key in _SELECTION_OPTIONS)
        if same_scope:
            body.update({key: last[key] for key in _RESULT_FIELDS if key in last})
        body["generation"] = uuid.uuid4().hex
        due = time.time() if self.gateway_active and body["enabled"] else next_run(body)
        self.store.subscribe(body, next_run=due, reset_selection=not same_scope)
        self._cancel_unused(last.get("running_job"))
        if self.gateway_active and body["enabled"]:
            self.resume_pending.add(identity)
        self._wake()
        return {"id": identity, "download": body["download"], "next_run": due}

    def _save(self, identity, body, due):
        with self.store.db() as db:
            db.execute(
                "UPDATE subscriptions SET body=?,next_run=? WHERE id=?",
                (json.dumps(body), due, identity),
            )

    def _finish(self, body, job, identity):
        result, error = job["result"], job["error"]
        if job["status"] == "complete" and job["kind"] == "subscription_check":
            outcome = result["subscriptions"].get(identity, {})
            result, error = (
                result["catalog"] | {"task_ids": outcome.get("task_ids", [])},
                outcome.get("error"),
            )
        elif job["status"] == "paused":
            error = "上次目录检查在服务停止时中断。"
        elif job["status"] == "cancelled":
            error = "目录检查已取消。"
        body.update(last_check=job["updated_at"], last_result=result, last_error=error, running_job=None)

    def schedule(self, *, manual_provider=False):
        if time.monotonic() - self.last_schedule < 1:
            return
        self.last_schedule = time.monotonic()
        self.jobs.tick()
        timestamp = time.time()
        if not (self.gateway_active or manual_provider) or self.wake_at is None or self.wake_at > timestamp:
            return
        next_wake = float("inf")
        due_by_source = {}
        sources = self.owner.config.read()["sources"]
        default_source = sources[0] if sources else OFFICIAL_SOURCE
        for row in self.store.subscriptions():
            identity, body = row["id"], row["body"]
            if not body.get("enabled", True):
                continue
            if identity in self.resume_pending:
                if body.get("download") and body.get("dataset"):
                    self.owner.catalogs.resume_subscription(body)
                self.resume_pending.discard(identity)
            due = row["next_run"]
            if running := body.get("running_job"):
                job = self.jobs.get(running)
                if job["status"] in ("queued", "running"):
                    next_wake = min(next_wake, timestamp + 1)
                    continue
                self._finish(body, job, identity)
                due = next_run(body, timestamp)
                self._save(identity, body, due)
            if identity not in self.initial_checks and due > timestamp:
                next_wake = min(next_wake, due)
                continue
            source = body.get("source") or default_source
            due_by_source.setdefault(source, {})[identity] = body
        for source, checks in due_by_source.items():
            job = self.jobs.submit(
                "subscription_check",
                {
                    "source": source,
                    "checks": {identity: body.get("generation") for identity, body in checks.items()},
                    "download": self.gateway_active,
                },
            )
            for identity, body in checks.items():
                body["running_job"] = job["job_id"]
                self._save(identity, body, next_run(body, timestamp))
                self.initial_checks.discard(identity)
            next_wake = min(next_wake, timestamp + 1)
        self.wake_at = None if next_wake == float("inf") else next_wake

    def _current(self, identity, generation):
        if self.owner.closing or getattr(self.owner, "lifecycle_stopping", False):
            return None
        row = self.store.subscription(identity)
        if row:
            body = row["body"]
            if body.get("enabled", True) and body.get("generation") == generation:
                return body
        return None

    def check(self, args, progress):
        with self.owner.lock:
            checks = {
                identity: body
                for identity, generation in args["checks"].items()
                if (body := self._current(identity, generation)) is not None
            }
        if not checks:
            return {"catalog": {}, "subscriptions": {}}
        refreshed = self.owner.catalogs.refresh({"source": args["source"]})
        outcomes = {}
        for index, (identity, body) in enumerate(checks.items()):
            if not (args["download"] and body.get("download") and body.get("dataset")):
                continue
            try:
                generation = body.get("generation")
                with self.owner.lock:
                    if self._current(identity, generation) is None:
                        continue
                entries, observation = self.owner.catalogs.subscription_candidates(body, refreshed)
                with self.owner.lock:
                    if self._current(identity, generation) is None:
                        continue
                    progress(index, len(checks), "queueing subscribed packages")
                    ids = self.owner.catalogs.enqueue_subscription(entries, identity)
                    if observation is not None:
                        self.store.save_subscription_packages(identity, observation)
                outcomes[identity] = {"task_ids": ids}
            except (FQDBError, OSError) as exc:
                outcomes[identity] = {"error": str(exc)}
        summary = {"source": refreshed["source"], "package_count": len(refreshed.get("packages", []))}
        for key in ("added", "updated", "removed", "hidden"):
            summary[key] = len(refreshed.get(key, []))
        return {"catalog": summary, "subscriptions": outcomes}
