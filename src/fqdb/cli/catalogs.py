"""Trusted catalogs and explicitly selected import sources."""

import json
from pathlib import Path

from .. import catalog, sources


def _catalog(client, args):
    operation = args["operation"]
    if operation == "list":
        return {"sources": catalog.list_sources(), **catalog.scan()}
    if operation == "refresh":
        return catalog.refresh(args["source"])
    if operation == "migrate":
        fields = {key: args[key] for key in ("source", "dataset", "frequency", "period")}
        if not all(fields.values()):
            raise ValueError(
                "catalog migrate requires the original root, --dataset, --frequency and --period"
            )
        return client.call("catalog_migrate", fields)
    if operation in ("add", "remove"):
        if not args["source"]:
            raise ValueError("catalog add/remove requires a source")
        handler = catalog.add if operation == "add" else catalog.remove
        return handler(args["source"])
    if operation in ("import", "check", "download"):
        if not args["source"]:
            raise ValueError(f"catalog {operation} requires a path")
        if operation == "import":
            return catalog.import_local(args["source"], relative=args["relative"])
        if operation == "check":
            return catalog.check(args["source"])
        return catalog.download(args["source"], background=args["background"])
    if operation == "export":
        output = args["output"] or args["source"]
        if not output:
            raise ValueError("catalog export requires --output or a directory")
        scope = {
            key: args[key]
            for key in ("dataset", "frequency", "year", "month", "start_date", "end_date", "period")
            if args[key] is not None
        }
        return catalog.export(
            output,
            dry_run=not args["execute"],
            **scope,
        )
    return catalog.scan(deep=args["deep"])


def _sources(_, args):
    operation = args["operation"]
    if operation == "list":
        return sources.list()
    if not args["identity"]:
        raise ValueError("sources configure/fetch requires an input id")
    if operation == "configure":
        if not args["adapter"]:
            raise ValueError("sources configure requires --adapter")
        settings = json.loads(
            Path(args["settings_file"]).read_text() if args["settings_file"] else args["settings"] or "{}"
        )
        return sources.configure(
            args["identity"], args["adapter"], settings, authorize_real=args["authorize_real"]
        )
    if not all(args[key] for key in ("dataset", "start_date", "end_date")):
        raise ValueError("sources fetch requires dataset, --start-date and --end-date")
    return sources.fetch(
        args["identity"],
        args["dataset"],
        args["frequency"],
        args["start_date"],
        args["end_date"],
        symbols=args["symbol"],
        version=args["version"],
        dry_run=not args["execute"],
        background=args["background"],
        authorize_real=args["authorize_real"],
    )


def register(cli):
    index = cli.add("catalog", "Scan, refresh, import or download from the magnet catalog", _catalog)
    index.add_argument(
        "operation",
        choices=[
            "add",
            "remove",
            "list",
            "refresh",
            "import",
            "check",
            "download",
            "export",
            "migrate",
            "scan",
        ],
    )
    index.add_argument("source", nargs="?")
    index.add_argument("--output")
    index.add_argument("--relative", help="Canonical catalog path for a single imported description")
    index.add_argument("--background", action="store_true")
    index.add_argument("--dataset", help="Only export packages of the specified market dataset")
    index.add_argument("--frequency")
    index.add_argument("--year", type=int)
    index.add_argument("--month", type=int)
    index.add_argument("--start-date")
    index.add_argument("--end-date")
    index.add_argument("--period", help="Exact YYYY, YYYYMM or YYYYMMDD package for a public export")
    index.add_argument("--execute", action="store_true")
    index.add_argument("--deep", action="store_true")
    adapters = cli.add("sources", "Configure optional import adapters or preview an import", _sources)
    adapters.add_argument("operation", choices=["configure", "list", "fetch"])
    adapters.add_argument("identity", nargs="?")
    adapters.add_argument("dataset", nargs="?", help="Market-qualified dataset such as cn/stock/base")
    adapters.add_argument("--adapter")
    settings = adapters.add_mutually_exclusive_group()
    settings.add_argument(
        "--settings", help="Adapter settings as JSON; use environment variable names for credentials"
    )
    settings.add_argument("--settings-file", help="Read adapter settings from a local JSON file")
    adapters.add_argument("--frequency", default="1d")
    adapters.add_argument("--start-date")
    adapters.add_argument("--end-date")
    adapters.add_argument("--symbol", action="append")
    adapters.add_argument("--version", type=int, default=1)
    adapters.add_argument(
        "--execute", action="store_true", help="Execute the scoped fetch; default previews only"
    )
    adapters.add_argument("--background", action="store_true")
    adapters.add_argument("--authorize-real", action="store_true")
