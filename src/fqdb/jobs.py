"""Persistent local service work, including checks, imports and catalog refresh."""

import time

from fqdb import api


def submit(kind, **args):
    return api.client().call("job_submit", {"kind": kind, "args": args})


def list():
    return api.client().call("jobs")


def status(identity):
    return api.client().call("job_status", {"id": identity})


def cancel(identity):
    return api.client().call("job_cancel", {"id": identity})


def wait(identity, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        row = status(identity)
        if row["status"] in ("complete", "failed", "cancelled", "paused"):
            return row
        if time.monotonic() >= deadline:
            return row | {"wait_timed_out": True}
        time.sleep(0.2)
