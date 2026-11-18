"""Provision, peer management, and upload priority commands."""

from .. import peers, provider
from ..config import DEFAULT_UPLOAD_LIMIT


def _priority(args):
    operation = args["priority_operation"]
    if operation == "list":
        return provider.priorities()
    if not args["ip"]:
        raise ValueError("provider priority requires an IP address")
    if operation == "set":
        return provider.set_priority(
            args["ip"],
            args["level"],
            enabled=not args["disabled"],
            note=args["note"],
        )
    if operation == "remove":
        return provider.remove_priority(args["ip"])
    if operation in ("enable", "disable"):
        return provider.enable_priority(args["ip"], operation == "enable")
    raise ValueError("provider priority requires set/list/remove/enable/disable")


def _provide(_, args):
    operation = args["operation"]
    listen = args["listen"]
    if listen is not None:
        if operation not in ("start", "run"):
            raise ValueError("--listen is only supported with provider start or run")
        from ..service.seeding import normalize_listen

        listen = normalize_listen(listen)
    if operation == "status":
        return provider.status()
    if operation == "stop":
        return provider.stop()
    if operation == "priority":
        return _priority(args)
    activate = provider.run if operation == "run" else provider.start
    scope = {name: args[name] for name in ("start_date", "end_date") if args[name] is not None}
    if listen is not None:
        scope["listen"] = listen
    result = activate(args["dataset"], args["upload_limit"], **scope)
    if operation == "start":
        return result
    provision = result["provision"]
    return {
        "provider": result["provider"],
        "provision": {key: provision[key] for key in ("status", "message", "eligible", "checked", "prepared")}
        | {"issue_count": len(provision["issues"])},
    }


def _peers(_, args):
    operation = args["operation"]
    if operation == "list":
        return peers.list()
    ip, port = args["ip"], args["port"]
    if not ip or port is None:
        raise ValueError("peers operation requires IP and port")
    if operation == "add":
        return peers.add(ip, port, enabled=not args["disabled"], note=args["note"])
    if operation == "remove":
        return peers.remove(ip, port)
    return peers.enable(ip, port, operation == "enable")


def register(cli):
    provision = cli.add("provider", "Share available local data; run stays in the foreground", _provide)
    provision.add_argument(
        "operation",
        choices=["start", "run", "stop", "status", "priority"],
        help="run: foreground; start: keep providing until stop; status/priority: inspect or configure",
    )
    provision.add_argument(
        "--dataset", action="append", help="Optional dataset/frequency scope such as cn/stock/base/1d"
    )
    provision.add_argument(
        "--upload-limit",
        type=int,
        help=f"Upload rate in bytes per second (default {DEFAULT_UPLOAD_LIMIT:,}). Use 0 for unlimited.",
    )
    provision.add_argument("--start-date")
    provision.add_argument("--end-date")
    provision.add_argument(
        "--listen",
        metavar="IP:PORT",
        help="BT listener for start or run. Use [IPv6]:PORT for IPv6. Changing it requires an idle worker.",
    )
    provision.add_argument(
        "priority_operation", nargs="?", choices=["set", "list", "remove", "enable", "disable"]
    )
    provision.add_argument("ip", nargs="?")
    provision.add_argument("--level", choices=["normal", "high"], default="high")
    provision.add_argument("--note", default="")
    provision.add_argument("--disabled", action="store_true")
    nodes = cli.add("peers", "Manage optional BT peer connections", _peers)
    nodes.add_argument("operation", choices=["add", "list", "remove", "enable", "disable"])
    nodes.add_argument("ip", nargs="?")
    nodes.add_argument("port", type=int, nargs="?")
    nodes.add_argument("--note", default="")
    nodes.add_argument("--disabled", action="store_true")
