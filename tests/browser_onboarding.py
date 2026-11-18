"""Chromium one-click provision acceptance on explicitly synthetic local roots."""

import argparse
import json
from pathlib import Path
import time

from filelock import FileLock, Timeout
from playwright.sync_api import expect, sync_playwright

from fqdb.bt.torrents import info_hash, torrent_cache_path
from fqdb.catalog.models import Entry
from fqdb.demo import market_frame, market_schema
from fqdb.publish.paths import immutable_file
from fqdb.service.client import Client
from fqdb.util import safe_path


def until(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Synthetic provision transition timed out")


def stopped(client):
    if client.endpoint() is not None:
        return False
    try:
        with FileLock(client.config.root / ".state/state/service.lock", timeout=0):
            return not (client.config.root / ".state/state/service.json").exists()
    except Timeout:
        return False


def build(client, *, missing_seed=False, missing_file=False):
    days, symbols = ["2026-11-13"], ["000100.SZ"]
    schema = market_schema(days=days, symbols=symbols, frequency="1d")
    client.call("register", schema.to_dict())
    source = client.config.root / ".state/imports/SYNTHETIC-onboarding.csv"
    market_frame(days=days, symbols=symbols).to_csv(source, index=False)
    result = client.call("publish", {"dataset": schema.id, "frequency": "1d", "input_path": str(source)})
    entry = Entry(**result["entries"][0])
    if missing_seed:
        torrent_cache_path(client.config.root, entry).unlink()
        safe_path(client.config.root / "torrents", entry.torrent).unlink(missing_ok=True)
    if missing_file:
        safe_path(client.config.root, entry.file).unlink()
        immutable_file(client.config.root, entry).unlink(missing_ok=True)
    assert client.endpoint() is None, "Preparing a local fixture must not leave a service"
    return entry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    root = parser.parse_args().data_dir.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit("Use a new empty SYNTHETIC test directory")
    root.mkdir(parents=True, exist_ok=True)
    evidence = root / "browser-evidence"
    evidence.mkdir()
    clients = {name: Client(root / ("SYNTHETIC-" + name)) for name in ("empty", "ready", "missing")}
    expected_entry = build(clients["ready"], missing_seed=True)
    build(clients["missing"], missing_file=True)
    checks, errors, failed_api = [], [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for name in ("empty", "ready", "missing"):
                client = clients[name]
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "response",
                    lambda response: (
                        failed_api.append({"url": response.url, "status": response.status})
                        if "/api/" in response.url and response.status >= 400
                        else None
                    ),
                )
                starts = []
                page.on(
                    "request",
                    lambda request: (
                        starts.append(request.post_data_json)
                        if request.url.endswith("/api/action/provider_start")
                        else None
                    ),
                )
                session = client.call("gateway_open")
                page.goto(client.endpoint()["url"] + "/?session=" + session["session_id"] + "#/provider")
                expect(page.locator("#provider-state")).to_have_text("提供模式：关闭")
                expect(page.locator("#provider-start")).to_have_text("开始做种")
                expect(page.locator("#provider-metrics")).to_contain_text("待确认")
                assert starts == [] and client.get("/api/status")["provider"] is False
                assert page.locator("#provider-options").get_attribute("open") is None
                if name == "ready":
                    page.screenshot(path=str(evidence / "before-start.png"), full_page=True)

                with page.expect_response(
                    lambda response: response.url.endswith("/api/action/provider_start"), timeout=60000
                ) as started:
                    page.locator("#provider-start").click()
                result = started.value.json()
                assert starts == [{}], "One click must directly start default provision without configuration"
                assert page.locator("#provider-options").get_attribute("open") is None
                provision = result["provision"]
                expect(page.locator("#provider-message")).to_have_text(provision["message"])
                if name == "ready":
                    import libtorrent as lt

                    assert result["provider"] and provision["eligible"] == 1
                    assert provision["status"] == "waiting_peers"
                    recovered = torrent_cache_path(client.config.root, expected_entry)
                    assert recovered.is_file()
                    assert info_hash(lt.torrent_info(str(recovered))) == expected_entry.info_hash
                    until(lambda: any(row["seeding"] for row in client.get("/api/status")["transfers"]))
                    expect(page.locator("#provider-transfers")).to_contain_text("等待连接", timeout=15000)
                    current = client.get("/api/status")
                    assert current["settings"]["provider_scopes"] == []
                    assert all(row["uploaded"] == 0 and row["peers"] == 0 for row in current["transfers"])
                    page.screenshot(path=str(evidence / "one-click-waiting.png"), full_page=True)
                    page.locator("#provider-stop").click()
                    expect(page.locator("#provider-state")).to_have_text("提供模式：关闭")
                    until(lambda: client.get("/api/status")["transfers"] == [])
                    checks.append(
                        "One click prepares missing seeds with the original info-hash and waits for peers without claiming uploaded traffic."
                    )
                else:
                    assert result["provider"] is False and provision["eligible"] == 0
                    assert provision["status"] == ("no_data" if name == "empty" else "missing_files")
                    expect(page.locator("#provider-state")).to_have_text("提供模式：关闭")
                    expect(page.locator("#provider-stop")).to_be_disabled()
                    assert client.get("/api/status")["transfers"] == []
                    if name == "missing":
                        expect(page.locator("#provider-issues")).to_contain_text("本地文件缺失")
                    page.screenshot(path=str(evidence / (name + ".png")), full_page=True)
                    checks.append(
                        f"{name}: one-click response truthfully reports unavailable local data and leaves provision off."
                    )
                page.close()
                until(lambda: stopped(client))
            browser.close()
        assert errors == [] and failed_api == [], (errors, failed_api)
        checks.append(
            "Import, local fixture preparation and opening the gateway never enable provision; all test workers exit after their sessions close."
        )
        result = {
            "synthetic_only": True,
            "checks": checks,
            "javascript_errors": errors,
            "failed_api": failed_api,
        }
        (evidence / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({"checks": len(checks), "evidence": str(evidence)}, ensure_ascii=False, indent=2))
    finally:
        for client in clients.values():
            if client.endpoint() is not None:
                client.call("shutdown")
            until(lambda: stopped(client))


if __name__ == "__main__":
    main()
