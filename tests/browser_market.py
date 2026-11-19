"""Chromium acceptance of magnet catalog discovery, audit and refresh.

Use an empty test root and the existing Playwright browser installation. This
script creates synthetic descriptors and one synthetic local Parquet file.
"""

import argparse
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from playwright.sync_api import expect, sync_playwright

from fqdb.catalog.magnet_manifest import SCHEMA_VERSION, dataset_path, package_path
from fqdb.demo import market_frame, market_schema
from fqdb.publish.publisher import Publisher
from fqdb.schema.market import market_fields
from fqdb.service.client import Client
from gateway_helpers import loopback_config
from fqdb.storage.metadata import Store
from fqdb.util import atomic_json


def dataset(name, fields, instrument, time):
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "fields": fields,
        "instrument_column": instrument,
        "time_column": time,
    }


def package(first, last, size, magnet=None):
    body = {
        "start_date": first,
        "end_date": last,
        "rows": 1,
        "instruments": 1,
        "trading_days": 1,
        "size_bytes": size,
    }
    return body | ({"magnet": magnet} if magnet else {})


def prepare(root):
    client_root = root / "SYNTHETIC-client"
    mirror = root / "SYNTHETIC-mirror"
    config = loopback_config(client_root)
    config.update(sources=[str(mirror)])
    store = Store(client_root)
    days = ["2026-11-17"]
    symbols = ["510300.SH"]
    schema = market_schema("etf", days=days, symbols=symbols)
    store.register(schema)
    Publisher(store).publish(schema.id, "1d", market_frame("etf", "1d", days=days, symbols=symbols))
    local_scope = "xx/index/constituents/daily"
    local_path = package_path(local_scope, "20261118")
    local_file = client_root / "data" / local_path.removesuffix(".json")
    local_file.parent.mkdir(parents=True)
    pq.write_table(
        pa.table({"symbol": ["TEST"], "date": [date(2026, 11, 18)], "weight": [1.0]}),
        local_file,
    )
    atomic_json(
        client_root / "catalog" / dataset_path(local_scope),
        dataset("合成本地数据", ["symbol", "date", "weight"], "symbol", "date"),
    )
    atomic_json(
        client_root / "catalog" / local_path,
        package("2026-11-18", "2026-11-18", local_file.stat().st_size),
    )
    remote_scope = "cn/stock/base/1d"
    remote_index = dataset_path(remote_scope)
    remote_path = package_path(remote_scope, "202610")
    atomic_json(
        mirror / "index.json",
        {"schema_version": SCHEMA_VERSION, "datasets": [remote_index], "packages": [remote_path]},
    )
    atomic_json(
        mirror / remote_index,
        dataset("合成镜像行情", list(market_fields("1d")), "instrument", "trading_date"),
    )
    atomic_json(
        mirror / remote_path,
        package("2026-10-08", "2026-10-08", 1200, "magnet:?xt=urn:btih:" + "22" * 20),
    )
    return Client(client_root), mirror, local_path, remote_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    root = Path(parser.parse_args().data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise SystemExit("Choose an empty synthetic test directory.")
    client, mirror, local_path, remote_path = prepare(root)
    errors = []
    try:
        session = client.call("gateway_open")
        entry = client.endpoint()["url"] + "/?session=" + session["session_id"]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(entry)
            expect(page.locator("#overview-datasets")).to_contain_text("合成本地数据")
            expect(page.locator("#overview-datasets")).not_to_contain_text("合成镜像行情")
            page.screenshot(path=str(root / "01-offline-overview.png"), full_page=True)

            page.get_by_role("button", name="合成本地数据", exact=True).click()
            expect(page.locator(".catalog-tree")).to_contain_text("xx")
            page.get_by_text("2026 年", exact=True).click()
            page.get_by_role("button", name="11 月", exact=True).click()
            expect(page.locator("#distribution-table")).to_contain_text("无磁力链接")
            expect(page.locator(f'[data-package-download="{local_path}"]')).to_be_disabled()
            page.locator(f'[data-package="{local_path}"]').click()
            expect(page.locator("#package-detail")).to_contain_text("待核对")
            page.get_by_role("button", name="核对覆盖").click()
            expect(page.locator("#package-detail")).to_contain_text("已核对")
            page.screenshot(path=str(root / "02-local-package.png"), full_page=True)
            page.locator("#close-package").click()

            page.get_by_role("button", name="更新磁力链接").click()
            expect(page.locator("#catalog-refresh-result")).to_contain_text("目录已同步")
            page.locator('.sidebar [data-page="overview"]').click()
            expect(page.locator("#overview-datasets")).to_contain_text("合成镜像行情")
            page.get_by_role("button", name="合成镜像行情", exact=True).click()
            expect(page.locator(f'[data-package-download="{remote_path}"]')).to_be_enabled()
            page.get_by_role("button", name="按范围下载").click()
            page.locator('#download-form [name="start_date"]').fill("2026-10-08")
            page.locator('#download-form [name="end_date"]').fill("2026-10-08")
            page.locator("#plan").click()
            expect(page.locator("#plan-table")).to_contain_text("2026-10")
            expect(page.locator("#sync")).to_be_enabled()
            page.screenshot(path=str(root / "03-download-preview.png"), full_page=True)

            (mirror / "index.json").rename(mirror / "broken-index.json")
            page.get_by_role("button", name="更新磁力链接").click()
            expect(page.locator("#catalog-refresh-result")).to_contain_text("同步失败，继续使用本地目录")
            page.locator('.sidebar [data-page="overview"]').click()
            expect(page.locator("#overview-datasets")).to_contain_text("合成镜像行情")
            page.screenshot(path=str(root / "04-failed-refresh.png"), full_page=True)
            page.locator('[data-open-dataset="cn/etf/base"]').click()
            page.get_by_role("tab", name="行情查询").click()
            expect(page.locator("#query-table")).to_contain_text("510300.SH")
            page.screenshot(path=str(root / "05-local-query.png"), full_page=True)
            page.locator("#exit-gateway").click()
            expect(page.locator("#connection")).to_contain_text("管理界面已退出")
            browser.close()
        assert errors == [], errors
        print(
            "Chromium verified offline catalog, package audit, missing magnet, refresh, download preview, failure retention and local query."
        )
    finally:
        if client.endpoint():
            client.call("shutdown")


if __name__ == "__main__":
    main()
