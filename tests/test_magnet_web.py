"""Dashboard views use the same on-disk descriptors as magnet download actions."""

from datetime import date

from fastapi.testclient import TestClient
import pyarrow as pa
import pyarrow.parquet as pq

from fqdb.catalog.magnet_manifest import SCHEMA_VERSION, dataset_path, package_path
from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.service.core import Owner
from fqdb.util import atomic_json
from fqdb.web.app import create_app


CN = "cn/stock/base/1d"
OTHER = "xx/index/constituents/daily"
DATE = "2026-11-18"
MON = "2026-11-01"


def _index(scope, fields, instrument, time):
    return {
        "schema_version": SCHEMA_VERSION,
        "name": f"Synthetic test: {scope}",
        "fields": fields,
        "instrument_column": instrument,
        "time_column": time,
    }


def _description(*, first=DATE, size=1234, rows=1, magnet=None):
    return {
        "start_date": first,
        "end_date": DATE,
        "rows": rows,
        "instruments": 1,
        "trading_days": 1,
        "size_bytes": size,
    } | ({"magnet": magnet} if magnet else {})


def _open(owner):
    client = TestClient(create_app(owner, "test-token"), base_url="http://127.0.0.1")
    response = client.post(
        "/api/action/gateway_open", json={}, headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200
    assert client.get("/").status_code == 200
    return client


def test_catalog_views_offline_and_manual_file_evidence(data_root, monkeypatch):
    owner = Owner(Config(data_root / "client"))
    catalog = owner.store.root / "catalog"
    client = _open(owner)
    try:

        def forbidden(*args, **kwargs):
            raise AssertionError("Opening and scanning the dashboard must not contact a source")

        monkeypatch.setattr("fqdb.catalog.magnet.MagnetCatalog.sync", forbidden)
        before = client.get("/api/console/summary").json()
        assert before["datasets"] == []
        assert owner.store.datasets(), "Database registrations must not populate the directory view"
        assert "更新磁力链接" in client.get("/").text

        dataset = catalog / dataset_path(OTHER)
        fields = ["symbol", "date", "weight"]
        atomic_json(dataset, _index(OTHER, fields, "symbol", "date"))
        descriptor = package_path(OTHER, "20261118")
        actual = owner.store.root / "data" / descriptor.removesuffix(".json")
        actual.parent.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "symbol": ["TEST1"],
                    "date": [date.fromisoformat(DATE)],
                    "weight": [1.0],
                }
            ),
            actual,
        )
        atomic_json(catalog / descriptor, _description(size=actual.stat().st_size))

        summary = client.get("/api/console/summary").json()
        scope = summary["datasets"][0]
        assert (scope["market"], scope["asset"], scope["data_type"], scope["frequency"]) == (
            "xx",
            "index",
            "constituents",
            "daily",
        )
        assert scope["local"] == 1 and scope["published_rows"] == 1
        assert scope["published_instruments"] == 1
        assert summary["totals"]["bytes"] == actual.stat().st_size
        listing = client.get(
            "/api/console/browse",
            params={
                "dataset": "xx/index/constituents",
                "frequency": "daily",
                "year": 2026,
                "month": 11,
            },
        ).json()
        assert listing["years"] == ["2026"] and listing["year_months"] == {"2026": ["11"]}
        package = listing["items"][0]
        assert (package["magnet_status"], package["local_status"], package["coverage_status"]) == (
            "missing",
            "available",
            "pending",
        )
        response = client.post("/api/action/download_package", json={"catalog_path": descriptor})
        assert response.status_code == 400 and "magnet" in response.json()["error"]
        assert owner.store.tasks() == [] and owner._engine is None

        checked = client.post("/api/action/catalog_check", json={"catalog_path": descriptor}).json()
        assert checked["coverage_status"] == "verified"
        assert checked["observed"]["fields"] == fields and checked["observed"]["trading_days"] == 1
        data = client.get("/api/console/package", params={"catalog_path": descriptor}).json()
        assert data["expected"]["instruments"] == data["observed"]["instruments"] == 1

        atomic_json(catalog / descriptor, _description(rows=2, size=actual.stat().st_size))
        invalid = client.get("/api/console/package", params={"catalog_path": descriptor}).json()
        assert invalid["status"] == "anomaly"
        assert any(
            problem["code"] == "rows" and problem["expected"] == 2 and problem["observed"] == 1
            for problem in invalid["issues"]
        )
        dataset.unlink()
        assert client.get("/api/console/summary").json()["datasets"] == []
        assert client.get("/api/console/package", params={"catalog_path": descriptor}).status_code == 400
        assert actual.exists()
    finally:
        client.close()
        owner.close()


def test_package_waiting_for_peers_and_failed_refresh_keep_same_catalog(data_root):
    owner = Owner(Config(data_root / "client"))
    catalog = owner.store.root / "catalog"
    client = _open(owner)
    try:
        identity = "11" * 20
        magnet = f"magnet:?xt=urn:btih:{identity}"
        schema = owner.store.dataset("cn/stock/base").for_frequency("1d")
        atomic_json(catalog / dataset_path(CN), _index(CN, list(schema.fields), "instrument", "trading_date"))
        available = package_path(CN, "202611")
        atomic_json(catalog / available, _description(first=MON, magnet=magnet))
        broken = package_path(CN, "20261118")
        atomic_json(catalog / broken, _description(magnet="not-a-magnet"))

        def package(path):
            return client.get("/api/console/package", params={"catalog_path": path}).json()

        assert package(available)["status"] == "missing"
        assert package(broken)["magnet_status"] == "invalid" and package(broken)["status"] == "anomaly"
        args = {"dataset": "cn/stock/base", "frequency": "1d", "year": 2026, "month": 11}
        preview = client.post("/api/console/plan", json=args).json()
        assert preview["total"] == 1 and preview["to_download"] == 1
        assert preview["items"][0]["package"]["path"] == available

        entry = owner.store.add_magnet(Entry.from_magnet("cn/stock/base", "1d", "202611", magnet))
        task = owner.store.enqueue(entry)
        owner.transfers.activate([task])
        owner.store.set_task(task, status="waiting_metadata_peers")
        waiting = package(available)
        assert waiting["status"] == "no_peers"
        assert waiting["local_status"] == "missing" and waiting["magnet_status"] == "available"
        assert waiting["task"]["id"] == identity
        assert owner._engine is None

        data_before = (catalog / available).read_bytes()
        bad_source = data_root / "missing-source"
        result = client.post("/api/action/catalog_refresh", json={"source": str(bad_source)})
        assert result.status_code == 400 and result.json()["error"]
        assert (catalog / available).read_bytes() == data_before
        assert package(available)["magnet"] == magnet
    finally:
        client.close()
        owner.close()
