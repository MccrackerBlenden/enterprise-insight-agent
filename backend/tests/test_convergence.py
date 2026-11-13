from types import SimpleNamespace

from app.convergence import inspect_history, needs_final_answer


def ai():
    return SimpleNamespace(type="ai")


def tool(name, content):
    return SimpleNamespace(type="tool", name=name, content=content)


def test_two_successful_sql_queries_require_final_answer():
    messages = [ai(), tool("get_schema", "tables"), ai(),
                tool("query_database", '{"rows": [{"x": 1}]}'), ai(),
                tool("query_database", '{"rows": [{"x": 2}]}')]
    stats = inspect_history(messages)
    assert stats == {"model_rounds": 3, "schema_calls": 1, "sql_attempts": 2, "sql_success": 2}
    assert needs_final_answer(stats)


def test_one_successful_query_allows_model_to_choose_next_step():
    stats = inspect_history([ai(), tool("query_database", '{"rows": [{"x": 3}]}')])
    assert stats["sql_success"] == 1
    assert not needs_final_answer(stats)


def test_three_failed_queries_stop_without_fake_success():
    stats = inspect_history([ai(), tool("query_database", '{"error": "SQL invalid"}'),
                             ai(), tool("query_database", "not json"),
                             ai(), tool("query_database", '{"error": "no table"}')])
    assert stats["sql_success"] == 0
    assert needs_final_answer(stats)


def test_four_model_rounds_force_termination():
    stats = inspect_history([ai(), ai(), ai(), ai()])
    assert needs_final_answer(stats)
