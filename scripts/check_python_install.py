"""Exercise an installed FQDB wheel using a new, explicit synthetic data root.

The main development environment is .venvs/FQDB (CPython 3.11). Run this with
the selected interpreter after installing the built wheel in that Python
environment; editable development installs are intentionally rejected. Nothing
is fetched from a quotation provider or published remotely.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys
import sysconfig
import time


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def installed_package():
    import fqdb

    distribution = importlib.metadata.distribution("fqdb")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    require(
        not direct_url.get("dir_info", {}).get("editable"),
        "Install the built wheel, not an editable checkout",
    )
    files = {str(path).replace("\\", "/") for path in distribution.files or []}
    require("fqdb/__init__.py" in files, "Installed RECORD does not contain the package")
    require(
        distribution.metadata["Requires-Python"] == ">=3.11", "Built wheel has an outdated Python requirement"
    )
    require(
        Path(fqdb.__file__).resolve() == Path(distribution.locate_file("fqdb/__init__.py")).resolve(),
        "Imported module does not come from this installed wheel",
    )
    entry_points = {p.name: p.value for p in distribution.entry_points if p.group == "console_scripts"}
    require(
        entry_points == {"fqdb": "fqdb.cli:main"}, "Distribution must expose exactly the fqdb console command"
    )
    require(importlib.util.find_spec("free_quant_db") is None, "Removed package is still importable")
    package_root = Path(distribution.locate_file("")).resolve()
    target_scripts = package_root / ("Scripts" if os.name == "nt" else "bin")
    script_dir = target_scripts if target_scripts.is_dir() else Path(sysconfig.get_path("scripts"))
    require(
        not (script_dir / ("fqd.exe" if os.name == "nt" else "fqd")).exists(),
        "Removed CLI is still installed",
    )
    command = script_dir / ("fqdb.exe" if os.name == "nt" else "fqdb")
    require(command.is_file(), "fqdb console script is not installed in this interpreter")
    from fqdb.catalog import models
    from fqdb.web import app

    return (
        fqdb,
        command,
        {
            "name": distribution.metadata["Name"],
            "version": distribution.version,
            "requires_python": distribution.metadata["Requires-Python"],
            "module": fqdb.__file__,
            "catalog_module": models.__file__,
            "web_module": app.__file__,
            "console_scripts": entry_points,
            "console_path": str(command),
            "removed_package_absent": True,
            "removed_command_absent": True,
        },
    )


def exercise(root, report):
    import httpx
    import libtorrent as lt
    import pandas as pd
    import pyarrow as pa
    from filelock import FileLock

    fqdb, command, package = installed_package()
    # Child CLI and daemon processes must import this installed wheel too,
    # including when validating a pip --target installation beside a checkout.
    installed_root = str(Path(fqdb.__file__).resolve().parent.parent)
    previous_path = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = installed_root + (os.pathsep + previous_path if previous_path else "")
    from fqdb.demo import market_frame, market_schema
    from fqdb.layout import runtime_path
    from fqdb.service.client import Client

    report.update(
        package=package,
        versions={
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "abi": sys.implementation.cache_tag,
            "gil_disabled_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
            "sqlite": sqlite3.sqlite_version,
            "libtorrent": lt.__version__,
            "libtorrent_distribution": importlib.metadata.version("libtorrent"),
            "pandas": pd.__version__,
            "numpy": importlib.metadata.version("numpy"),
            "pyarrow": pa.__version__,
            "platform": platform.platform(),
        },
    )
    fqdb.init(root)
    client = Client(root)
    client.config.update(listen="127.0.0.1:0", discovery=False)
    gateway = None
    gateway_log = root / "SYNTHETIC-gateway.log"
    try:
        days = ["2024-01-02", "2024-01-03"]
        schema = market_schema(days=days, symbols=["000100.SZ", "600000.SH"])
        fqdb.register_dataset(schema)
        source = runtime_path(root, "imports") / "SYNTHETIC-installed-wheel.csv"
        market_frame(days=days, symbols=["000100.SZ", "600000.SH"]).to_csv(source, index=False)
        published = client.call(
            "publish", {"dataset": schema.id, "frequency": "1d", "input_path": str(source)}
        )
        require(
            published["synthetic"] and len(published["entries"]) == 2,
            "Synthetic publication did not produce two day packages",
        )
        checked = fqdb.check(schema.id, "1d", start_date=days[0], end_date=days[-1], deep=True)
        require(checked.summary["file_verified"] == 2, "Deep check did not verify both files")
        require(checked.summary["anomalies"] == 0, "Deep check reported synthetic file anomalies")
        fields, symbols = ["close", "volume"], ["000100.SZ", "600000.SH"]
        queried = fqdb.load(schema.id, symbols=symbols, start=days[0], end=days[-1], fields=fields)
        require(queried.shape == (4, 2), "Installed query returned an unexpected frame")
        require(queried.attrs["synthetic"], "Query lost explicit synthetic provenance")
        require(client.endpoint() is None, "Finite SDK operations started a background service")
        with gateway_log.open("w") as log:
            gateway = subprocess.Popen(
                [sys.executable, "-m", "fqdb", "gateway", "--no-browser", "-d", str(root)],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 30
        endpoint = None
        with httpx.Client(trust_env=False, timeout=5) as browser:
            while time.monotonic() < deadline:
                require(gateway.poll() is None, "Gateway exited: " + gateway_log.read_text())
                endpoint = client.endpoint()
                if endpoint and browser.get(endpoint["url"]).status_code == 200:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Installed gateway did not become ready")
        before_pid = endpoint["pid"]
        cli = subprocess.run(
            [str(command), "-d", str(root), "status"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        cli_status = json.loads(cli.stdout)
        require(cli_status["data_dir"] == str(root), "CLI and SDK selected different roots")
        require(client.endpoint()["pid"] == before_pid, "CLI created a second owner")
        url = endpoint["url"]
        with httpx.Client(trust_env=False, timeout=20) as browser:
            index = browser.get(url)
            index.raise_for_status()
            require("FreeQuantDB" in index.text, "Dashboard index is missing")
            static = browser.get(url + "/static/app.js")
            static.raise_for_status()
            # This uses real SQLite JSON/window-function projections, beyond a
            # health response or merely importing a newer Python interpreter.
            summary = browser.get(url + "/api/console/summary")
            summary.raise_for_status()
            require(
                summary.json()["runtime"]["provider"] is True,
                "Gateway did not enable automatic seeding",
            )
        expected_seeds = {entry["info_hash"] for entry in published["entries"]}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = client.get("/api/status")
            ready = {transfer["id"] for transfer in status["transfers"] if transfer["seeding"]}
            if ready == expected_seeds:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Gateway did not restore the two synthetic seeds")
        report["checks"] = {
            "synthetic_daily_packages": len(published["entries"]),
            "deep_verified_files": checked.summary["file_verified"],
            "query_rows": len(queried),
            "same_sdk_cli_web_pid": before_pid,
            "dashboard_status": index.status_code,
            "static_status": static.status_code,
            "console_summary_status": summary.status_code,
            "restored_seeds": len(ready),
        }
        session = re.search(r"[?&]session=([A-Za-z0-9_-]+)", gateway_log.read_text())
        require(session is not None, "Gateway did not print its session URL")
        client.call("gateway_close", {"session_id": session[1]})
        require(gateway.wait(timeout=15) == 0, "Foreground gateway did not exit normally")
        require(client.endpoint() == endpoint, "Closing Gateway stopped the seed worker")
        paused = client.call("provider_stop")
        require(not paused["provider"] and not paused["settings"]["auto_seed"], "Pause was not saved")
        require(not paused["transfers"], "Pause left native seeds running")
        report["checks"]["seed_worker_survived_gateway"] = True
        deadline = time.monotonic() + 20
        while client.endpoint() and time.monotonic() < deadline:
            time.sleep(0.1)
        require(client.endpoint() is None, "Gateway left an idle worker")
    finally:
        if client.endpoint():
            client.call("shutdown")
        if gateway is not None and gateway.poll() is None:
            try:
                gateway.wait(timeout=15)
            except subprocess.TimeoutExpired:
                gateway.terminate()
                gateway.wait(timeout=10)
        # HTTP stops before libtorrent releases the data-root ownership lock.
        with FileLock(runtime_path(root, "state") / "service.lock", timeout=30):
            pass
    offline = fqdb.get_price(symbols, days[0], days[-1], fields=fields)
    require(len(offline) == 4 and client.endpoint() is None, "Offline query started a service or lost rows")
    report["checks"]["offline_query_rows"] = len(offline)
    report["checks"]["service_stopped"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-d", "--data-dir", required=True, help="New/empty explicit root for clearly synthetic data"
    )
    parser.add_argument(
        "--report", help="JSON report; defaults to SYNTHETIC-install-result.json in that new root"
    )
    args = parser.parse_args()
    root = Path(args.data_dir).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[1]
    require(not root.is_relative_to(checkout), "Put generated data outside the source checkout")
    require(
        not root.exists() or not any(root.iterdir()),
        "Choose a new or empty synthetic root; existing data is never reused",
    )
    root.mkdir(parents=True, exist_ok=True)
    os.environ["FQDB_USER_CONFIG"] = str(root / ".install-check-user.json")
    report = {"status": "running", "synthetic": True, "data_root": str(root)}
    destination = (
        Path(args.report).expanduser().resolve() if args.report else root / "SYNTHETIC-install-result.json"
    )
    require(not destination.is_relative_to(checkout), "Put verification reports outside the source checkout")
    started = time.perf_counter()
    try:
        exercise(root, report)
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    report["elapsed_seconds"] = time.perf_counter() - started
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
