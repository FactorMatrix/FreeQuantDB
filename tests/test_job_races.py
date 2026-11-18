"""Deterministic lifecycle interleavings against the real SQLite job queue."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from fqdb.config import Config
from fqdb.service.jobs import Jobs
from fqdb.storage.metadata import Store
from fqdb.util import FQDBError
from test_contracts import sample_frame


def settled(jobs, identity):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        with jobs.guard:
            active = identity in jobs.active
        if not active:
            return jobs.get(identity)
        time.sleep(0.01)
    raise AssertionError("Synthetic job did not settle")


@pytest.mark.parametrize("transition", ["running", "complete", "failed"])
def test_cancel_wins_against_worker_state_transition(data_root, monkeypatch, transition):
    store = Store(Config(data_root).ensure().root)
    entered, release, executed = threading.Event(), threading.Event(), threading.Event()

    def execute(*_):
        executed.set()
        if transition == "failed":
            raise ValueError("SYNTHETIC worker failure")
        return {"synthetic": True}

    jobs = Jobs(store, execute)
    original = jobs.set

    def before_transition(identity, **values):
        if values.get("status") == transition:
            entered.set()
            assert release.wait(5), "Synthetic barrier was not released"
        return original(identity, **values)

    monkeypatch.setattr(jobs, "set", before_transition)
    try:
        identity = jobs.submit("check", {})["job_id"]
        assert entered.wait(5)
        assert jobs.set(identity, status="cancelled")
        assert jobs.get(identity)["status"] == "cancelled"
        release.set()
        row = settled(jobs, identity)
        assert row["status"] == "cancelled" and row["result"] is None and row["error"] is None
        if transition == "running":
            assert not executed.is_set()
    finally:
        release.set()
        jobs.close()


def test_restart_does_not_replay_public_or_unbound_internal_jobs(data_root):
    store = Store(Config(data_root).ensure().root)
    jobs = Jobs(store, lambda *_: {})
    jobs.close()
    stamp = "2026-11-17T00:00:00Z"
    with store.db() as c:
        for identity, kind, state in (
            ("SYNTHETIC-preparation", "transfer_prepare", "running"),
            ("SYNTHETIC-validation", "download_validate", "queued"),
            ("SYNTHETIC-public", "check", "running"),
            ("SYNTHETIC-done", "download_validate", "complete"),
        ):
            c.execute(
                "INSERT INTO service_jobs VALUES(?,?,?,?,0,NULL,NULL,NULL,?,?)",
                (identity, kind, json.dumps({}), state, stamp, stamp),
            )
    executed = []
    restarted = Jobs(store, lambda kind, *_: executed.append(kind) or {"synthetic": True})
    try:
        assert restarted.get("SYNTHETIC-preparation")["status"] == "paused"
        assert restarted.get("SYNTHETIC-validation")["status"] == "paused"
        assert restarted.get("SYNTHETIC-done")["status"] == "complete"
        assert restarted.get("SYNTHETIC-public")["status"] == "paused"
        restarted.tick()
        assert not restarted.has_work() and not executed
    finally:
        restarted.close()


def test_stopping_queue_rejects_new_work_while_finishing_existing_job(data_root):
    store = Store(Config(data_root).ensure().root)
    entered, release = threading.Event(), threading.Event()

    def execute(*_):
        entered.set()
        assert release.wait(5)
        return {"synthetic": True}

    jobs = Jobs(store, execute)
    with ThreadPoolExecutor(max_workers=1) as pool:
        identity = jobs.submit("check", {})["job_id"]
        assert entered.wait(5)
        closing = pool.submit(jobs.close)
        end = time.monotonic() + 5
        while not jobs.closed and time.monotonic() < end:
            time.sleep(0.01)
        try:
            with pytest.raises(FQDBError, match="stopping"):
                jobs.submit("check", {})
            jobs.tick()
        finally:
            release.set()
            closing.result(timeout=5)
    assert jobs.get(identity)["status"] == "complete"
    assert len(jobs.list()) == 1


def test_owner_close_keeps_native_polling_until_publish_verification_finishes(data_root, monkeypatch):
    from test_contracts import sample_schema
    from fqdb.service.core import Owner

    owner = Owner(Config(data_root))
    schema = sample_schema(days=["2026-11-12"])
    owner.store.register(schema)
    source = data_root / ".state/imports/SYNTHETIC-close-publish.csv"
    sample_frame(days=["2026-11-12"]).to_csv(source, index=False)
    entered, release = threading.Event(), threading.Event()
    native_verify = owner.seeding.verify

    def deferred_verify(entry):
        entered.set()
        assert release.wait(5), "Synthetic publish barrier was not released"
        native_verify(entry)

    monkeypatch.setattr(owner.seeding, "verify", deferred_verify)
    owner.start()
    try:
        submitted = owner.dispatch(
            "job_submit",
            {"kind": "publish", "args": {"dataset": schema.id, "frequency": "1d", "input_path": str(source)}},
        )
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            closing = pool.submit(owner.close)
            end = time.monotonic() + 5
            while not owner.closing and time.monotonic() < end:
                time.sleep(0.01)
            assert owner.closing
            release.set()
            closing.result(timeout=10)
        row = owner.jobs.get(submitted["job_id"])
        assert row["status"] == "complete" and len(owner.store.entries()) == 1
        assert owner.running is False and owner.engine.session is None
    finally:
        release.set()
        if owner.running:
            owner.close()
