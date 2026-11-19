"""Download scope, validation and task commands."""

from functools import partial

from .. import jobs
from ..service.subscriptions import DEFAULT_INTERVAL


def _scope_args(parser, *, subscription=False):
    dataset = {"nargs": "?"} if subscription else {}
    parser.add_argument("dataset", help="Market-qualified dataset such as cn/stock/base", **dataset)
    parser.add_argument("--frequency", default=None if subscription else "1d")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    if not subscription:
        parser.add_argument("--year", type=int)
        parser.add_argument("--month", type=int)


def _package_options(parser):
    parser.add_argument(
        "--granularity",
        choices=["auto", "day", "month", "year"],
        help="Select available packages covering the range (default), or request a specific package kind",
    )
    parser.add_argument(
        "--daily-fallback",
        dest="allow_daily_fallback",
        action="store_true",
        help="Explicitly use daily seeds if the requested month/year package is unavailable",
    )


def _action(client, args, *, action):
    return client.call(action, args)


def _download(client, args):
    background = args.pop("background")
    result = client.call("sync", args)
    return result if background else client.wait(result)


def _repair(client, args):
    execute = args.pop("execute")
    report = client.call("check", args)
    result = client.call("repair", {"report": report, "dry_run": not execute})
    return client.wait(result) if execute else result


def _jobs(_, args):
    if args["operation"] == "list":
        return jobs.list()
    if not args["id"]:
        raise ValueError("jobs status/cancel requires a job id")
    handler = jobs.status if args["operation"] == "status" else jobs.cancel
    return handler(args["id"])


def register(cli):
    plan = cli.add("plan", "Preview a download without transferring data", partial(_action, action="plan"))
    _scope_args(plan)
    _package_options(plan)
    for name, description in (
        ("sync", "Download the selected data range"),
        ("download", "Download a day, month, year or custom range"),
    ):
        download = cli.add(name, description, _download)
        _scope_args(download)
        _package_options(download)
        download.add_argument("--background", action="store_true")
    check = cli.add("check", "Check local files and data quality", partial(_action, action="check"))
    _scope_args(check)
    check.add_argument("--deep", action="store_true")
    repair = cli.add("repair", "Preview repairs; --execute downloads the required files", _repair)
    _scope_args(repair)
    repair.add_argument("--deep", action="store_true")
    repair.add_argument("--execute", action="store_true")
    update = cli.add("update", "Refresh the local magnet catalog", partial(_action, action="update"))
    update.add_argument("--source", help="An alternative mirror with the same catalog layout")
    subscribe = cli.add(
        "subscribe",
        "Schedule catalog checks and optional downloads after Gateway starts",
        partial(_action, action="subscribe"),
    )
    _scope_args(subscribe, subscription=True)
    _package_options(subscribe)
    subscribe.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL, help="Seconds between checks (default: %(default)s)"
    )
    subscribe.add_argument("--at")
    subscribe.add_argument("--timezone", default="UTC")
    subscribe.add_argument(
        "--download", action="store_true", help="Automatically download new packages in the selected scope"
    )
    task = cli.add(
        "task", "Pause, resume, cancel or retry a selected download", partial(_action, action="task")
    )
    task.add_argument("id")
    task.add_argument("operation", choices=["pause", "resume", "cancel", "retry"])
    jobs_command = cli.add("jobs", "List, inspect or cancel submitted jobs", _jobs)
    jobs_command.add_argument("operation", choices=["list", "status", "cancel"])
    jobs_command.add_argument("id", nargs="?")
