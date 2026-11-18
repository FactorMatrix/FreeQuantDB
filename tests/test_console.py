from datetime import date, timedelta

from fastapi.testclient import TestClient
import pyarrow.parquet as pq
import pytest

from fqdb.catalog.magnet_manifest import package_path
from fqdb.config import Config
from fqdb.publish.paths import immutable_file
from fqdb.service.core import Owner
from fqdb.util import atomic_json
from fqdb.web.app import create_app
from test_contracts import prepare_inputs, sample_frame, sample_schema


SCOPE = {"dataset": "cn/stock/base", "frequency": "1d"}
IDENTITY = "cn/stock/base/1d"


@pytest.fixture
def console_client(data_root):
    owner = Owner(Config(data_root))
    owner.shutdown = lambda: None
    owner.start()
    client = TestClient(create_app(owner, "console-test"), base_url="http://127.0.0.1")
    assert (
        client.post(
            "/api/action/gateway_open", json={}, headers={"Authorization": "Bearer console-test"}
        ).status_code
        == 200
    )
    assert client.get("/").status_code == 200
    inputs = prepare_inputs(data_root)
    owner.dispatch("register", sample_schema().to_dict())
    owner.dispatch("publish", SCOPE | {"input_path": inputs["input"]})
    try:
        yield client, owner
    finally:
        client.close()
        owner.close()


def console(client, view, **params):
    response = client.get(f"/api/console/{view}", params=None if view == "summary" else SCOPE | params)
    assert response.status_code == 200, response.text
    return response.json()


def test_publishing_exposes_only_written_catalog_and_query_schema(console_client):
    client, owner = console_client
    summary = console(client, "summary")
    assert summary["totals"]["registered"] == 1
    assert summary["totals"]["published_datasets"] == 1
    assert summary["totals"]["local_files"] == 2
    row = summary["datasets"][0]
    assert (row["market"], row["asset"], row["data_type"]) == ("cn", "stock", "base")
    assert row["status"] == "local" and row["published_rows"] == 4
    assert row["published_instruments"] is None, "Distinct instruments cannot be summed across packages"
    browsing = console(client, "browse", year=2026, month=11, limit=1)
    assert browsing["years"] == ["2026"] and browsing["year_months"]["2026"] == ["11"]
    assert len(browsing["items"]) == 1 and browsing["total"] == 2
    package = client.get("/api/console/package", params={"catalog_path": browsing["items"][0]["path"]}).json()
    assert package["query_supported"] and package["version_status"] == "current"
    assert package["info_hash"] in {e.info_hash for e in owner.store.entries()}
    dataset = console(client, "dataset")
    assert "close" in dataset["schema"]["fields"] and dataset["schema"]["query_supported"]
    assert dataset["symbols"] == ["000100.SZ", "600000.SH"]
    assert (
        client.get(
            "/api/console/browse",
            params=SCOPE
            | {
                "start_date": "2027-01-01",
                "end_date": "2026-01-01",
            },
        ).status_code
        == 400
    )


def test_archive_plan_chooses_one_covering_package_and_keeps_revision_history(console_client):
    client, owner = console_client
    original = owner.store.entries()[0]
    archived = owner.dispatch("aggregate", SCOPE | {"granularity": "year", "period": "2026"})["entries"][0]
    scopes = (
        {"start_date": original.trading_date, "end_date": original.trading_date},
        {"year": 2026, "month": 11},
        {"year": 2026},
    )
    for dates in scopes:
        result = client.post("/api/console/plan", json=SCOPE | dates).json()
        assert result["total"] == 1 and result["ready"] == 1
        assert result["items"][0]["entry"]["info_hash"] == archived["info_hash"]
    prior = immutable_file(owner.config.root, original).read_bytes()
    source = owner.config.root / ".state/imports/SYNTHETIC-stock.csv"
    sample_frame(days=[original.trading_date], correction=2).to_csv(source, index=False)
    revised = owner.dispatch("publish", SCOPE | {"input_path": str(source)})["entries"][0]
    assert revised["version"] == 2 and revised["file"] == original.file
    assert immutable_file(owner.config.root, original).read_bytes() == prior
    assert revised["info_hash"] != original.info_hash
    current = client.get(
        "/api/console/package",
        params={
            "catalog_path": package_path(IDENTITY, original.period),
        },
    ).json()
    assert current["version_status"] == "current" and current["info_hash"] == revised["info_hash"]


def test_query_reports_missing_symbol(console_client):
    client, _ = console_client
    query = {
        "instruments": ["000100.SZ", "999999.SZ"],
        "start_date": "2026-11-12",
        "end_date": "2026-11-13",
        "limit": 1,
    }
    assert client.post("/api/query", json=query).status_code == 400
    assert client.get("/api/console/nonexistent").status_code == 404


def test_light_reads_use_footer_and_bound_multiyear_lists(console_client, monkeypatch):
    client, owner = console_client

    def reject_payload(*args, **kwargs):
        pytest.fail("Light dashboard scan must only read Parquet metadata")

    monkeypatch.setattr(pq, "read_table", reject_payload)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", reject_payload)
    for offset in range(210):
        value = date(2023, 12, 1) + timedelta(days=offset * 4)
        path = package_path(IDENTITY, value.strftime("%Y%m%d"))
        atomic_json(
            owner.config.root / "catalog" / path,
            {
                "start_date": value.isoformat(),
                "end_date": value.isoformat(),
                "rows": 1,
                "instruments": 1,
                "trading_days": 1,
                "size_bytes": 100,
            },
        )
    result = console(client, "browse", limit=10000)
    assert result["total"] == 212 and len(result["items"]) == 200
    assert {"2023", "2024", "2025", "2026"} <= set(result["years"])
    assert console(client, "audit", limit=20)["limit"] == 20
    assert client.get("/api/console/summary").status_code == 200
