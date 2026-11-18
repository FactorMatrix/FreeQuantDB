from fastapi.testclient import TestClient

from fqdb.config import Config
from fqdb.service.core import Owner
from fqdb.web.app import create_app


def test_local_web_security_real_publish_query_and_settings(data_root):
    from test_contracts import sample_schema, prepare_inputs

    owner = Owner(Config(data_root))
    owner.shutdown = lambda: None
    owner.start()
    try:
        c = TestClient(create_app(owner, "test-token"), base_url="http://127.0.0.1")
        assert c.get("/api/status").status_code == 401
        assert c.post(
            "/api/action/gateway_open",
            json={},
            headers={"Authorization": "Bearer test-token"},
        ).status_code == 200
        assert c.get("/").status_code == 200
        assert c.get("/static/app.js").status_code == 200
        assert c.get("/api/status").status_code == 200
        assert (
            c.post(
                "/api/action/provider_start", json={}, headers={"origin": "https://untrusted.example"}
            ).status_code
            == 403
        )
        assert c.get("/", headers={"host": "rebinding.example"}).status_code == 403
        assert c.post("/api/action/register", json=sample_schema().to_dict()).status_code == 200
        inputs = prepare_inputs(data_root)
        result = c.post(
            "/api/action/publish",
            json={"dataset": "cn/stock/base", "frequency": "1d", "input_path": inputs["input"]},
        )
        assert result.status_code == 200, result.text
        query = {
            "instruments": ["000100.SZ"],
            "start_date": "2026-11-12",
            "end_date": "2026-11-13",
            "fields": ["trading_date", "close"],
            "limit": 1,
        }
        first = c.post("/api/query", json=query).json()
        assert len(first["records"]) == 1 and first["next_offset"] == 1 and first["metadata"]["synthetic"]
        second = c.post("/api/query", json=query | {"offset": 1}).json()
        assert first["records"][0]["trading_date"] != second["records"][0]["trading_date"]
        assert second["next_offset"] is None
        csv = c.post("/api/query", json=query | {"export": True})
        assert csv.status_code == 200 and "close" in csv.text and "text/csv" in csv.headers["content-type"]
        report = c.post("/api/action/check", json={"dataset": "cn/stock/base", "frequency": "1d", "deep": True})
        assert report.status_code == 200
        assert report.json()["summary"]["file_verified"] == 2
        assert c.post("/api/action/settings", json={"max_active": 2}).json()["max_active"] == 2
        assert (
            c.post(
                "/api/action/subscribe", json={"dataset": "cn/stock/base", "frequency": "1d", "interval": 3600}
            ).status_code
            == 200
        )
        assert c.get("/api/status").json()["subscriptions"]
    finally:
        owner.close()
