"""An end-to-end stream regression with a FAKE agent; never calls LLM/API."""
import json

from app.data import create_demo_db
from app.db import run_readonly_query
from app import agent as agent_module
from app.sql_artifact import build_verified_sql_artifact
from evals.run import cases_load, evaluate_live_events
from langchain_core.messages import AIMessage, ToolMessage

QUESTION = "比较2023至2025年三个行业的营业收入趋势，给出逐年数值。"
SQL = """SELECT c.industry AS industry, f.year AS year,
ROUND(SUM(f.revenue),2) AS total_revenue
FROM monthly_financials f JOIN companies c ON f.company_id=c.company_id
WHERE f.year BETWEEN 2023 AND 2025 GROUP BY c.industry,f.year;"""


def make_run(monkeypatch, db, invalidate=False):
    output = run_readonly_query(SQL, db)
    if invalidate:
        output["rows"][0]["total_revenue"] = -99

    class FakeAgent:
        def stream(self, *args, **kwargs):
            yield {"model": {"messages": [AIMessage(content="", tool_calls=[
                {"name": "get_schema", "args": {}, "id": "c1"}
            ])]}}
            yield {"tools": {"messages": [ToolMessage(name="get_schema", content="schema", tool_call_id="c1")]}}
            yield {"model": {"messages": [AIMessage(content="", tool_calls=[
                {"name": "query_database", "args": {"sql": SQL}, "id": "c2"}
            ])]}}
            yield {"tools": {"messages": [ToolMessage(name="query_database", content=json.dumps(output), tool_call_id="c2")]}}
            yield {"model": {"messages": [AIMessage(content="已根据数据库信息完成营业收入趋势分析。") ]}}

    monkeypatch.setenv("OPENAI_MODEL", "fake-no-network")
    monkeypatch.setattr(agent_module, "is_ready", lambda: True)
    monkeypatch.setattr(agent_module, "_model", lambda: object())
    monkeypatch.setattr(agent_module, "create_agent", lambda **_kwargs: FakeAgent())
    # The agent bridge normally uses its own demo DB. Point it at this isolated
    # fixture instead, retaining the EXACT same converter logic.
    monkeypatch.setattr(agent_module, "build_verified_sql_artifact",
                        lambda question, result: build_verified_sql_artifact(question, result, db))
    return list(agent_module.run_agent_stream(QUESTION))


def test_sql_only_agent_stream_emits_verified_metric_artifact(monkeypatch, tmp_path):
    db = create_demo_db(tmp_path / "live-fixture.sqlite")
    events = make_run(monkeypatch, db)
    assert [name for name, _ in events].count("metric_artifact") == 1
    assert [name for name, _ in events].count("done") == 1
    assert len([1 for name, value in events if name == "tool_call" and value["name"] == "query_database"]) == 1
    case = next(c for c in cases_load() if c["id"] == "M02")
    scored = evaluate_live_events(case, events, db, 2000)
    assert scored["status"] == "passed", scored["problems"]
    assert scored["observed_tools"] == ["get_schema", "query_database"]
    assert scored["structured_artifact_present"] is True


def test_sql_only_agent_stream_cannot_artificially_pass_bad_data(monkeypatch, tmp_path):
    db = create_demo_db(tmp_path / "bad-fixture.sqlite")
    events = make_run(monkeypatch, db, invalidate=True)
    assert not [1 for name, _ in events if name == "metric_artifact"]
    case = next(c for c in cases_load() if c["id"] == "M02")
    scored = evaluate_live_events(case, events, db, 2000)
    assert scored["status"] == "failed"
