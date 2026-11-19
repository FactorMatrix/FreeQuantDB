"""Data-root, dashboard and local publication commands."""

from functools import partial
import json
from pathlib import Path


def _init(client, _):
    config = client.config
    settings = config.read()
    return {
        "data_dir": str(config.root),
        "provider": settings["provider"],
        "layout": config.layout,
        "settings": settings,
    }


def _gateway(_, args):
    from ..web import run

    run(open_browser=not args["no_browser"], port=args["port"])


def _service(client, args):
    endpoint = client.endpoint()
    if args["operation"] == "stop":
        return client.call("shutdown") if endpoint else {"status": "stopped"}
    return {"status": "running" if endpoint else "stopped", "url": endpoint["url"] if endpoint else None}


def _status(client, _):
    if client.endpoint() is None:
        return {
            "service_running": False,
            "data_dir": str(client.config.root),
            "provider": False,
            "provider_configured": client.config.read()["provider"],
        }
    return client.get("/api/status")


def _read_view(client, _, *, path, key=None):
    result = client.get(path)
    return result if key is None else result[key]


def _settings(client, args):
    updates = {}
    if args["discovery"] is not None:
        updates["discovery"] = args["discovery"] == "on"
    if args["listen"] is not None:
        updates["listen"] = args["listen"]
    return client.call("settings", updates) if updates else client.config.read()


def _register_dataset(client, args):
    return client.call("register", json.loads(Path(args["descriptor"]).read_text()))


def _publish(client, args):
    args["granularity"] = args["granularity"] or "day"
    aggregate = args.pop("aggregate")
    dates_file = args.pop("trading_dates_file")
    if aggregate:
        args.pop("version")
        if args.pop("input_path") is not None or dates_file:
            raise ValueError(
                "--aggregate reads verified daily packages; omit input_path and trading-dates-file"
            )
        return client.call("aggregate", args)
    if dates_file:
        args["trading_dates"] = json.loads(Path(dates_file).read_text())
    if not args["input_path"]:
        raise ValueError("publish requires input_path or --aggregate")
    return client.call("publish", args)


def register(cli):
    cli.add("init", "Save a default data root; other commands initialize automatically", _init)
    gateway = cli.add("gateway", "Open the management web UI; Ctrl+C closes this gateway session", _gateway)
    gateway.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser")
    gateway.add_argument("--port", type=int, default=0)
    service = cli.add("service", "Inspect or stop the active worker", _service)
    service.add_argument("operation", choices=["stop", "status"])
    cli.add("status", "Show local status without starting a service", _status)
    settings = cli.add("settings", "Inspect or configure BitTorrent discovery and listener", _settings)
    settings.add_argument(
        "--discovery", choices=("on", "off"), help="Enable or disable DHT and peer discovery"
    )
    settings.add_argument("--listen", help="BitTorrent listen address, such as 0.0.0.0:46881")
    cli.add("logs", "Show recent worker logs", partial(_read_view, path="/api/logs"))
    cli.add("datasets", "List registered datasets", partial(_read_view, path="/api/status", key="datasets"))
    cli.add(
        "register", "Register a dataset description from a local JSON file", _register_dataset
    ).add_argument("descriptor")
    publish = cli.add("publish", "Validate and publish local data packages", _publish)
    publish.add_argument("dataset", help="Market-qualified dataset such as cn/stock/base")
    publish.add_argument("input_path", nargs="?")
    publish.add_argument("--frequency", default="1d")
    publish.add_argument("--version", type=int, default=1)
    publish.add_argument(
        "--granularity", choices=["day", "month", "year"], help="Package frequency (default: day)"
    )
    publish.add_argument("--period", help="YYYY, YYYYMM or YYYYMMDD matching the selected granularity")
    publish.add_argument("--trading-dates-file", help="JSON per-row source trading dates for minute input")
    publish.add_argument(
        "--aggregate", action="store_true", help="Stream a month/year from verified daily packages"
    )
