"""Chromium seeding controls on synthetic data and loopback-only BT."""

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
from gateway_helpers import loopback_config


def until(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Synthetic seeding transition timed out")


def stopped(client):
    if client.endpoint() is not None:
        return False
    try:
        with FileLock(client.config.root / ".state/state/service.lock", timeout=0):
            return not (client.config.root / ".state/state/service.json").exists()
    except Timeout:
        return False


def build(client, *, missing_file=False):
    days, symbols = ["2026-11-13"], ["000100.SZ"]
    schema = market_schema(days=days, symbols=symbols, frequency="1d")
    client.call("register", schema.to_dict())
    source = client.config.root / ".state/imports/SYNTHETIC-seeding.csv"
    market_frame(days=days, symbols=symbols).to_csv(source, index=False)
    result = client.call("publish", {"dataset": schema.id, "frequency": "1d", "input_path": str(source)})
    entry = Entry(**result["entries"][0])
    if missing_file:
        safe_path(client.config.root, entry.file).unlink()
        immutable_file(client.config.root, entry).unlink(missing_ok=True)
    assert client.endpoint() is None, "Preparing a fixture must not leave a background service"
    return entry


def configure_upload(page, client):
    form = page.locator("#provider-limit")
    limit = form.get_by_label("总上传上限（MB/s）")
    expect(limit).to_have_value("2.5")
    limit.fill("0")
    form.get_by_role("button", name="保存限速").click()
    expect(page.locator("#priority-note")).to_contain_text("设置上传总限速后")
    assert client.call("priority_list")["global_upload_limit"] == 0

    limit.fill("1.234567")
    form.get_by_role("button", name="保存限速").click()
    expect(page.locator("#priority-note")).not_to_contain_text("设置上传总限速后")
    assert client.config.read()["upload_limit"] == 1_234_567
    assert client.call("priority_list")["global_upload_limit"] == 1_234_567

    priority = page.locator("#provider-priority")
    rules = page.locator("#provider-rules")
    for ip in ("127.0.0.2", "::1"):
        priority.get_by_label("节点 IP").fill(ip)
        priority.get_by_role("button", name="添加优先 IP").click()
        expect(rules.get_by_text(ip, exact=True)).to_be_visible()
    status = client.call("priority_list")
    assert len(status["rules"]) == 2
    assert all(rule["level"] == "high" and rule["enabled"] for rule in status["rules"])
    assert len({rule["class_id"] for rule in status["rules"]}) == 1
    expect(page.locator("#priority-note")).to_contain_text("重连后生效")
    return rules


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
    for client in clients.values():
        loopback_config(client.config.root).update(upload_limit=2_500_000)
    expected_entry = build(clients["ready"])
    build(clients["missing"], missing_file=True)
    checks, errors, failed_api = [], [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for name, client in clients.items():
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
                endpoint, session = client.open_gateway()
                page.goto(endpoint["url"] + "/?session=" + session["session_id"] + "#/provider")
                expect(page.locator("#provider-state")).to_have_text("做种已开启")
                toggle = page.locator("#provider-toggle")
                expect(toggle).to_have_text("暂停做种")
                expect(page.locator("#provider-metrics")).to_contain_text("0 MB/s")
                if name == "ready":
                    import libtorrent as lt

                    expect(page.locator("#provider-message")).to_have_text("等待节点")
                    metrics = page.locator("#provider-metrics .metric-card")
                    expect(metrics.filter(has_text="可做种数据包")).to_contain_text("1")
                    until(lambda: any(
                        row["id"] == expected_entry.info_hash and row["seeding"]
                        for row in client.get("/api/status")["transfers"]
                    ))
                    cached = torrent_cache_path(client.config.root, expected_entry)
                    assert info_hash(lt.torrent_info(str(cached))) == expected_entry.info_hash
                    rules = configure_upload(page, client)
                    page.screenshot(path=str(evidence / "seeding-waiting.png"), full_page=True)
                    rules.get_by_role("button", name="删除 127.0.0.2").click()
                    expect(rules.get_by_text("127.0.0.2", exact=True)).not_to_be_visible()
                    assert [rule["ip"] for rule in client.call("priority_list")["rules"]] == ["::1"]
                    toggle.click()
                    expect(page.locator("#provider-state")).to_have_text("做种已暂停")
                    expect(metrics.filter(has_text="可做种数据包")).to_contain_text("1")
                    assert client.get("/api/status")["transfers"] == []
                    assert client.config.read()["auto_seed"] is False
                    page.screenshot(path=str(evidence / "seeding-paused.png"), full_page=True)
                    toggle.click()
                    expect(page.locator("#provider-state")).to_have_text("做种已开启")
                    expect(page.locator("#provider-message")).to_have_text("等待节点")
                    checks.extend([
                        "Gateway restores the original synthetic torrent without waiting for a user click.",
                        "Upload limit saves in bytes per second and applies to libtorrent, including unlimited.",
                        "IPv4 and IPv6 rules share a high-priority native class and can be deleted.",
                        "Page pauses and resumes seeding and persists the pause preference.",
                    ])
                else:
                    expect(page.locator("#provider-message")).to_have_text(
                        "暂无可做种数据" if name == "empty" else "本地文件缺失"
                    )
                    assert client.get("/api/status")["transfers"] == []
                    if name == "missing":
                        expect(page.locator("#provider-issues")).to_contain_text("本地文件缺失")
                    checks.append(f"{name}: page accurately shows unavailable data and zero upload rate.")
                    page.screenshot(path=str(evidence / (name + ".png")), full_page=True)
                page.close()
            browser.close()
        assert errors == [] and failed_api == [], (errors, failed_api)
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
