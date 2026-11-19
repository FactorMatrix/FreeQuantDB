"""Real Chromium gateway lifecycle acceptance, using a new empty synthetic root."""

import argparse
import json
from pathlib import Path
import re
import os
import signal
import subprocess
import sys
import time

from playwright.sync_api import expect, sync_playwright

from fqdb.service.client import Client
from gateway_helpers import loopback_config


def until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Synthetic gateway lifecycle transition timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    root = parser.parse_args().data_dir.resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit("Use a new empty SYNTHETIC test directory")
    root.mkdir(parents=True, exist_ok=True)
    evidence = root / "browser-evidence"
    evidence.mkdir()
    client = Client(root / "SYNTHETIC-data")
    loopback_config(client.config.root)
    log_path = evidence / "gateway.log"
    checks, errors = [], []

    def start_gateway():
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "fqdb",
                    "gateway",
                    "--no-browser",
                    "--data-dir",
                    str(client.config.root),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        url = until(lambda: re.search(r"FQDB gateway: (http://\S+)", log_path.read_text()))[1]
        return process, url

    process = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            process, url = start_gateway()
            page.goto(url)
            expect(page.locator("#connection")).to_contain_text("本地服务在线")
            for name in ("overview", "datasets", "downloads", "validation", "settings"):
                page.locator(f'.sidebar [data-page="{name}"]').click()
                expect(page.locator("#view")).not_to_contain_text("当前页面尚未加载完成")
                expect(page.locator("#view")).not_to_contain_text("正在读取本地状态…")
                if name == "downloads":
                    expect(page.locator("#transfer-settings")).to_be_visible()
                    expect(page.locator("#provider-state")).to_have_count(0)
            page.locator('.sidebar [data-page="overview"]').click()
            page.screenshot(path=str(evidence / "gateway-open.png"), full_page=True)
            checks.append(
                "Five real management pages open without foreground sharing reminders."
            )
            page.reload()
            expect(page.locator("#connection")).to_contain_text("本地服务在线")
            time.sleep(4)
            assert client.endpoint() is not None
            checks.append("Browser refresh preserves its gateway within the unload grace period.")
            page.locator("#exit-gateway").click()
            expect(page.locator("#view")).to_contain_text("管理界面已退出")
            page.screenshot(path=str(evidence / "gateway-closed.png"), full_page=True)
            assert process.wait(timeout=15) == 0
            until(
                lambda: (
                    client.endpoint() is None
                    and not (client.config.root / ".state/state/service.json").exists()
                )
            )
            checks.append("Exit button releases the foreground gateway and its otherwise idle worker.")

            process, url = start_gateway()
            page.goto(url)
            expect(page.locator("#connection")).to_contain_text("本地服务在线")
            page.close()
            assert process.wait(timeout=15) == 0
            until(
                lambda: (
                    client.endpoint() is None
                    and not (client.config.root / ".state/state/service.json").exists()
                )
            )
            checks.append("Closing the browser tab releases its lease and stops the empty worker.")

            for terminate in (False, True):
                process, url = start_gateway()
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(url)
                expect(page.locator("#connection")).to_contain_text("本地服务在线")
                if terminate:
                    os.kill(client.endpoint()["pid"], signal.SIGTERM)
                else:
                    client.call("shutdown")
                assert process.wait(timeout=15) == 0
                until(
                    lambda: (
                        client.endpoint() is None
                        and not (client.config.root / ".state/state/service.json").exists()
                    )
                )
                page.close()
                checks.append(
                    "Worker SIGTERM closes active browser streams without retaining a process."
                    if terminate
                    else "Explicit service shutdown closes active browser streams without retaining a process."
                )
            browser.close()
        assert errors == [], errors
        (evidence / "results.json").write_text(
            json.dumps({"synthetic_only": True, "checks": checks, "errors": errors}, indent=2)
        )
        print(json.dumps({"checks": len(checks), "errors": errors, "evidence": str(evidence)}, indent=2))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if client.endpoint() is not None:
            client.call("shutdown")


if __name__ == "__main__":
    main()
