"""Regression tests for model-produced single-statement SQL terminators."""
import pytest

from app.data import create_demo_db
from app.db import run_readonly_query


def test_simple_select_with_terminal_semicolon(tmp_path):
    path = create_demo_db(tmp_path / "demo.sqlite")
    result = run_readonly_query("  SELECT COUNT(*) AS total FROM companies; \n", path)
    assert result["rows"] == [{"total": 12}]
    assert result["truncated"] is False


def test_model_style_annual_join_query_with_semicolon(tmp_path):
    path = create_demo_db(tmp_path / "demo.sqlite")
    sql = """SELECT c.company_name, c.industry, f.year,
           SUM(f.revenue) AS revenue, SUM(f.cost) AS cost,
           SUM(f.expenses) AS expenses, SUM(f.net_profit) AS net_profit
           FROM monthly_financials f, companies c
           WHERE f.company_id = c.company_id AND f.year IN (2024, 2025)
           GROUP BY c.company_name, c.industry, f.year;"""
    result = run_readonly_query(sql, path)
    assert result["row_count"] == 24
    assert len(result["rows"]) == 24
    assert all(r["revenue"] > 0 for r in result["rows"])
    assert {r["year"] for r in result["rows"]} == {2024, 2025}


def test_with_cte_single_statement_with_semicolon(tmp_path):
    path = create_demo_db(tmp_path / "demo.sqlite")
    result = run_readonly_query(
        "WITH s AS (SELECT COUNT(*) AS n FROM companies) SELECT n FROM s;",
        path,
    )
    assert result["rows"] == [{"n": 12}]


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM companies; SELECT 1;",
    "SELECT COUNT(*) FROM companies;;",
    "SELECT COUNT(*) FROM companies; -- disguised second statement",
    "SELECT COUNT(*) FROM companies /* comment */;",
    "DELETE FROM companies;",
    "UPDATE companies SET company_name = 'oops' WHERE company_id = 'C01';",
    "PRAGMA journal_mode;",
    "WITH c AS (SELECT 1) SELECT * FROM c; DROP TABLE companies;",
])
def test_stacked_or_unsafe_sql_remains_rejected(tmp_path, sql):
    path = create_demo_db(tmp_path / "demo.sqlite")
    with pytest.raises(ValueError):
        run_readonly_query(sql, path)
    assert run_readonly_query("SELECT COUNT(*) AS total FROM companies", path)["rows"] == [{"total": 12}]


def test_semicolon_does_not_bypass_read_authorizer(tmp_path):
    path = create_demo_db(tmp_path / "demo.sqlite")
    with pytest.raises(ValueError):
        run_readonly_query("SELECT * FROM sqlite_master;", path)
