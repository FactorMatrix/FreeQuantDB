"""Persistent bounded service jobs for file/network work, outside the BT owner lock."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid

from ..util import FQDBError, now


class Jobs:
    def __init__(self, store, execute):
        self.store, self.execute = store, execute
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fqdb-job")
        self.guard = threading.Lock()
        self.active = set()
        self.authorized = set()
        self.closed = False
        with store.db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS service_jobs(
                id TEXT PRIMARY KEY,kind TEXT,args TEXT,status TEXT,progress REAL,
                detail TEXT,result TEXT,error TEXT,created_at TEXT,updated_at TEXT)""")

    def has_work(self):
        with self.guard:
            if self.active:
                return True
            with self.store.db() as c:
                return any(
                    r[0] in self.authorized
                    for r in c.execute("SELECT id FROM service_jobs WHERE status IN ('queued','running')")
                )

    def submit(self, kind, args):
        identity = uuid.uuid4().hex
        with self.guard:
            if self.closed:
                raise FQDBError("Service jobs are stopping; restart the service before submitting work")
            with self.store.db() as c:
                c.execute(
                    "INSERT INTO service_jobs VALUES(?,?,?,'queued',0,NULL,NULL,NULL,?,?)",
                    (identity, kind, json.dumps(args), now(), now()),
                )
            self.authorized.add(identity)
        self.tick()
        return {"job_id": identity, "status": "queued"}

    def tick(self):
        with self.guard:
            if self.closed:
                return
            with self.store.db() as c:
                rows = c.execute(
                    "SELECT id FROM service_jobs WHERE status='queued' ORDER BY created_at"
                ).fetchall()
            for row in rows:
                identity = row[0]
                if identity in self.authorized and identity not in self.active and len(self.active) < 2:
                    self.active.add(identity)
                    self.pool.submit(self._run, identity)

    def _run(self, identity):
        try:
            # Claim atomically: a concurrent queued cancellation wins without
            # being overwritten by the worker entering its running state.
            if not self.set(identity, status="running"):
                return
            row = self.get(identity)
            if row["status"] == "cancelled":
                return

            def progress(done, total, detail=""):
                if self.get(identity)["status"] == "cancelled":
                    raise FQDBError("Job cancelled; completed immutable files remain")
                if not self.set(identity, progress=float(done / total) if total else 0, detail=detail):
                    raise FQDBError("Job cancelled; completed immutable files remain")

            result = self.execute(row["kind"], row["args"], progress)
            self.set(identity, status="complete", progress=1, result=json.dumps(result), detail="completed")
        except Exception as exc:
            self.set(identity, status="failed", error=str(exc))
        finally:
            with self.guard:
                self.active.discard(identity)

    def get(self, identity):
        with self.store.db() as c:
            row = c.execute("SELECT * FROM service_jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise FQDBError("Unknown service job")
        result = dict(row) | {
            "args": json.loads(row["args"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }
        if identity not in self.authorized and row["status"] in ("queued", "running"):
            result.update(status="paused", detail="manual_resume_required")
        return result

    def list(self):
        with self.store.db() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM service_jobs ORDER BY created_at DESC LIMIT 50")]
        return [self.get(identity) for identity in ids]

    def set(self, identity, **values):
        if not values.keys() <= {"status", "progress", "detail", "result", "error"}:
            raise FQDBError("Invalid job update")
        transitions = {
            "running": ("queued",),
            "complete": ("running",),
            "failed": ("running",),
            "cancelled": ("queued", "running"),
        }
        target = values.get("status")
        if target is not None and target not in transitions:
            raise FQDBError("Invalid job state transition")
        permitted = transitions[target] if target else ("running",)
        values["updated_at"] = now()
        with self.store.db() as c:
            changed = c.execute(
                "UPDATE service_jobs SET "
                + ",".join(k + "=?" for k in values)
                + " WHERE id=? AND status IN ("
                + ",".join("?" for _ in permitted)
                + ")",
                (*values.values(), identity, *permitted),
            )
            return bool(changed.rowcount)

    def close(self):
        with self.guard:
            self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
