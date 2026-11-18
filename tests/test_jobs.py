import time

from fqdb.config import Config
from fqdb.service.jobs import Jobs
from fqdb.storage.metadata import Store


def wait_until(fn, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Job did not finish")


def test_persistent_bounded_jobs_progress_failure_and_restart(data_root):
    store = Store(Config(data_root).ensure().root)

    def execute(kind, args, progress):
        progress(1, 2, "reading explicitly chosen local metadata")
        if args.get("fail"):
            raise ValueError("synthetic failure")
        return {"value": args["value"]}

    jobs = Jobs(store, execute)
    try:
        first = jobs.submit("check", {"value": 42})["job_id"]
        wait_until(lambda: jobs.get(first)["status"] == "complete")
        assert jobs.get(first)["result"] == {"value": 42}
        failed = jobs.submit("check", {"fail": True})["job_id"]
        wait_until(lambda: jobs.get(failed)["status"] == "failed")
        assert jobs.get(failed)["error"] == "synthetic failure"
    finally:
        jobs.close()
    with store.db() as c:
        c.execute("UPDATE service_jobs SET status='running',result=NULL WHERE id=?", (first,))
    restarted = Jobs(store, execute)
    try:
        restarted.tick()
        assert restarted.get(first)["status"] == "paused"
        assert not restarted.has_work()
        assert len(restarted.list()) == 2
    finally:
        restarted.close()
