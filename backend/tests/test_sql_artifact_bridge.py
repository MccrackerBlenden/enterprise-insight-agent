"""Regression for M02's successful SQL-but-missing-artifact failure."""
from copy import deepcopy

from app.data import create_demo_db
from app.db import run_readonly_query
from app.sql_artifact import build_verified_sql_artifact, requested_industry_trend
from evals.run import cases_load, evaluate_live_events, verify_metric_artifact

QUESTION = "比较2023至2025年三个行业的营业收入趋势，给出逐年数值。"
GOOD_SQL = """SELECT c.industry AS industry, f.year AS year,
  ROUND(SUM(f.revenue),2) AS total_revenue
FROM monthly_financials f JOIN companies c ON c.company_id=f.company_id
WHERE f.year BETWEEN 2023 AND 2025
GROUP BY c.industry, f.year ORDER BY c.industry, f.year;"""


def fixture(tmp_path):
    db = create_demo_db(tmp_path / "bridge.sqlite")
    result = run_readonly_query(GOOD_SQL, db)
    assert len(result["rows"]) == 9
    return db, result


def test_extracts_only_unambiguous_supported_trend():
    assert requested_industry_trend(QUESTION) == ("revenue", 2023, 2025)
    assert requested_industry_trend("对比2023—2025年各行业净利润率年度趋势") == ("margin", 2023, 2025)
    assert requested_industry_trend("对比2023至2025年各行业收入和利润率趋势") is None
    assert requested_industry_trend("哪个企业利润最好？") is None
    assert requested_industry_trend("比较2023至2026年各行业营业收入趋势") is None


def test_sql_numeric_evidence_becomes_existing_verified_chart_contract(tmp_path):
    db, result = fixture(tmp_path)
    artifact = build_verified_sql_artifact(QUESTION, result, db)
    assert artifact is not None
    assert artifact["kind"] == "metric_analysis"
    assert artifact["metric"] == "revenue"
    assert artifact["group_by"] == "industry_year"
    assert artifact["chart"]["kind"] == "line"
    assert artifact["evidence"]["route"] == "query_database"
    assert artifact["evidence"]["matched_rows"] == 9
    case = next(c for c in cases_load() if c["id"] == "M02")
    assert verify_metric_artifact(artifact, case, db) == []


def test_bridge_rejects_incorrect_or_incomplete_rows(tmp_path):
    db, result = fixture(tmp_path)
    for change in ("mismatch", "missing", "duplicate", "invalid_year", "missing_revenue", "truncated", "error"):
        bad = deepcopy(result)
        if change == "mismatch":
            bad["rows"][0]["total_revenue"] += 10
        elif change == "missing":
            bad["rows"].pop()
        elif change == "duplicate":
            bad["rows"][0] = dict(bad["rows"][1])
        elif change == "invalid_year":
            bad["rows"][0]["year"] = 2022
        elif change == "missing_revenue":
            bad["rows"][0].pop("total_revenue")
        elif change == "truncated":
            bad["truncated"] = True
        elif change == "error":
            bad["error"] = "database-error"
        assert build_verified_sql_artifact(QUESTION, bad, db) is None, change


def test_bridge_rejects_unrelated_question_and_fake_response(tmp_path):
    db, result = fixture(tmp_path)
    assert build_verified_sql_artifact("2025年各企业利润率排名", result, db) is None
    assert build_verified_sql_artifact(QUESTION, {"rows": []}, db) is None
    assert build_verified_sql_artifact(QUESTION, {"rows": result["rows"], "truncated": "false"}, db) is None


def test_converted_sql_stream_passes_real_m02_evaluator(tmp_path):
    db, result = fixture(tmp_path)
    artifact = build_verified_sql_artifact(QUESTION, result, db)
    case = next(c for c in cases_load() if c["id"] == "M02")
    events = [
        ("tool_call", {"name": "get_schema", "args": {}}),
        ("tool_result", {"name": "get_schema", "message": "schema"}),
        ("tool_call", {"name": "query_database", "args": {"sql": GOOD_SQL}}),
        ("tool_result", {"name": "query_database", "message": "query succeeded"}),
        ("metric_artifact", artifact),
        ("done", {"answer": "已生成经过验证的逐年营业收入分析。"}),
    ]
    verdict = evaluate_live_events(case, events, db, 8000)
    assert verdict["status"] == "passed", verdict["problems"]
    assert verdict["sql_evidence_verified"] is True
    assert verdict["structured_artifact_present"] is True
    assert verdict["observed_tools"] == ["get_schema", "query_database"]


def test_invalid_sql_cannot_get_artifact_or_eval_pass(tmp_path):
    db, result = fixture(tmp_path)
    bad = deepcopy(result)
    bad["rows"][0]["total_revenue"] = 0
    assert build_verified_sql_artifact(QUESTION, bad, db) is None
    case = next(c for c in cases_load() if c["id"] == "M02")
    events = [
        ("tool_call", {"name": "query_database", "args": {"sql": "SELECT 1 AS dummy"}}),
        ("tool_result", {"name": "query_database", "message": "query succeeded"}),
        ("done", {"answer": "answer"}),
    ]
    verdict = evaluate_live_events(case, events, db, 8000)
    assert verdict["status"] == "failed"
    assert verdict["structured_artifact_present"] is False
