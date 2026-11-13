from fastapi.testclient import TestClient

from app.data import create_demo_db
from app.db import run_readonly_query, overview
from app.main import app


def test_database_is_reproducible(tmp_path):
    a = tmp_path / "one.sqlite"
    b = tmp_path / "two.sqlite"
    create_demo_db(a)
    create_demo_db(b)
    first = run_readonly_query("SELECT COUNT(*) AS n, ROUND(SUM(revenue),2) AS t FROM monthly_financials", a)
    second = run_readonly_query("SELECT COUNT(*) AS n, ROUND(SUM(revenue),2) AS t FROM monthly_financials", b)
    assert first == second
    assert first["rows"][0]["n"] == 432


def test_accounting_identity(tmp_path):
    path = create_demo_db(tmp_path / "data.sqlite")
    result = run_readonly_query("SELECT MAX(ABS(net_profit-(revenue-cost-expenses))) AS diff FROM monthly_financials", path)
    assert result["rows"][0]["diff"] < 0.001


def test_sql_is_strictly_readonly(tmp_path):
    path = create_demo_db(tmp_path / "test.sqlite")
    for sql in ["DROP TABLE companies", "DELETE FROM companies", "PRAGMA user_version=5",
                "SELECT * FROM companies; DELETE FROM companies", "SELECT * FROM sqlite_master"]:
        try:
            run_readonly_query(sql, path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Unsafe query accepted: {sql}")
    assert run_readonly_query("SELECT COUNT(*) AS n FROM companies", path)["rows"][0]["n"] == 12


def test_ranking_contains_real_calculations(tmp_path):
    path = create_demo_db(tmp_path / "report.sqlite")
    data = overview(path)
    assert data["companies"] == 12
    assert data["records"] == 432
    assert len(data["ranking"]) == 12
    assert data["ranking"][0]["delta"] <= data["ranking"][-1]["delta"]


def test_api_start_and_no_fake_agent(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["model_ready"] is False
        assert client.get("/api/overview").json()["records"] == 432
        with client.stream("POST", "/api/run/stream", json={"question": "分析企业利润率变化"}) as result:
            body = "".join(result.iter_text())
        assert "event: error" in body
        assert "event: done" not in body
