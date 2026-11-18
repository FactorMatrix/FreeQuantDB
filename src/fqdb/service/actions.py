"""Register each service operation with its lock and worker-start policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..util import FQDBError

if TYPE_CHECKING:
    from .core import Owner


@dataclass(frozen=True)
class Action:
    run: Callable[[Owner, dict], Any]
    locked: bool = False
    worker: bool | Callable[[dict], bool] = False


ACTIONS = {
    "register": Action(lambda owner, args: owner.register(args), locked=True),
    "plan": Action(lambda owner, args: owner.catalogs.plan(args)),
    "sync": Action(lambda owner, args: owner.catalogs.plan(args, download=True), worker=True),
    "update": Action(lambda owner, args: owner.catalogs.refresh(args)),
    "download_package": Action(lambda owner, args: owner.catalogs.download_package(**args), worker=True),
    "source": Action(lambda owner, args: owner.catalogs.add(args["source"])),
    "source_remove": Action(lambda owner, args: owner.catalogs.remove(args["source"]), locked=True),
    "source_list": Action(lambda owner, args: owner.upstream.source_list(), locked=True),
    "source_configure": Action(lambda owner, args: owner.upstream.configure(args), locked=True),
    "source_fetch": Action(
        lambda owner, args: owner.upstream.fetch_action(args),
        worker=lambda args: bool(args.get("background")) and not args.get("dry_run", True),
    ),
    "catalog_scan": Action(lambda owner, args: owner.catalogs.scan(**args)),
    "catalog_refresh": Action(lambda owner, args: owner.catalogs.refresh(args)),
    "catalog_check": Action(lambda owner, args: owner.catalogs.check_package(**args)),
    "catalog_import": Action(lambda owner, args: owner.catalogs.import_local(**args)),
    "catalog_migrate": Action(lambda owner, args: owner.catalogs.migrate(**args)),
    "catalog_export": Action(lambda owner, args: owner.catalogs.export(**args)),
    "check": Action(lambda owner, args: owner.checks.check(**args)),
    "repair": Action(
        lambda owner, args: owner.checks.repair(**args),
        locked=True,
        worker=lambda args: not args.get("dry_run", True),
    ),
    "publish": Action(lambda owner, args: owner.publish(args)),
    "aggregate": Action(lambda owner, args: owner.aggregate(args)),
    "provider_start": Action(lambda owner, args: owner.seeding.start(args), worker=True),
    "provider_stop": Action(lambda owner, args: owner.seeding.stop(args), locked=True),
    "task": Action(
        lambda owner, args: owner.transfers.control(args["id"], args["operation"]),
        locked=True,
        worker=lambda args: args.get("operation") in {"resume", "retry"},
    ),
    "settings": Action(lambda owner, args: owner.peers.settings(args), locked=True),
    "subscribe": Action(lambda owner, args: owner.subscriptions.subscribe(args), locked=True),
    "unsubscribe": Action(lambda owner, args: owner.subscriptions.unsubscribe(args["id"]), locked=True),
    "job_submit": Action(lambda owner, args: owner.submit_job(**args), locked=True, worker=True),
    "job_status": Action(lambda owner, args: owner.jobs.get(args["id"]), locked=True),
    "job_cancel": Action(lambda owner, args: owner.cancel_job(args["id"]), locked=True),
    "jobs": Action(lambda owner, args: {"jobs": owner.jobs.list()}, locked=True),
    "peers_add": Action(lambda owner, args: owner.peers.add(**args), locked=True),
    "peers_remove": Action(lambda owner, args: owner.peers.remove(**args), locked=True),
    "peers_enable": Action(lambda owner, args: owner.peers.enable(**args), locked=True),
    "peers_list": Action(lambda owner, args: {"nodes": owner.peers.nodes()}, locked=True),
    "priority_set": Action(lambda owner, args: owner.peers.set_priority(**args), locked=True),
    "priority_remove": Action(lambda owner, args: owner.peers.remove_priority(**args), locked=True),
    "priority_enable": Action(lambda owner, args: owner.peers.enable_priority(**args), locked=True),
    "priority_list": Action(lambda owner, args: owner.peers.priority_status(), locked=True),
}


def dispatch(owner, name, args):
    action = ACTIONS.get(name)
    if action is None:
        raise FQDBError(f"Unknown action: {name}")
    if action.locked:
        with owner.lock:
            return action.run(owner, args)
    return action.run(owner, args)


def starts_worker(name, args):
    if name == "gateway_open":
        return True
    action = ACTIONS.get(name)
    if action is None:
        return False
    return action.worker(args) if callable(action.worker) else action.worker
