"""Regression: SQL-only analysis must be inspected, not blindly passed or failed."""
from app.data import create_demo_db
from evals.run import cases_load, evaluate_live_events, verify_sql_metric_evidence


def m02():
    return next(c for c in cases_load() if c['id'] == 'M02')


GOOD_SQL = '''SELECT c.industry AS industry, f.year AS year,
  ROUND(SUM(f.revenue), 2) AS total_revenue
  FROM monthly_financials f
  JOIN companies c ON c.company_id = f.company_id
  WHERE f.year BETWEEN 2023 AND 2025
  GROUP BY c.industry, f.year
  ORDER BY c.industry, f.year;'''


def test_sql_industry_year_financials_can_be_independently_verified(tmp_path):
    db = create_demo_db(tmp_path/'one.sqlite')
    audit = verify_sql_metric_evidence(GOOD_SQL, m02(), db)
    assert audit['verified'] is True
    assert audit['matched_rows'] == 9
    assert audit['expected_rows'] == 9
    assert 'SELECT' not in str(audit)


def test_sql_wrong_measure_does_not_pass(tmp_path):
    db = create_demo_db(tmp_path/'two.sqlite')
    sql = GOOD_SQL.replace('SUM(f.revenue)', 'SUM(f.net_profit)')
    audit = verify_sql_metric_evidence(sql, m02(), db)
    assert audit['verified'] is False
    assert audit['reason'] == 'measure_mismatch'


def test_sql_missing_industry_year_groups_does_not_pass(tmp_path):
    db = create_demo_db(tmp_path/'three.sqlite')
    sql = GOOD_SQL.replace('BETWEEN 2023 AND 2025', '= 2025')
    audit = verify_sql_metric_evidence(sql, m02(), db)
    assert audit['verified'] is False
    assert audit['reason'] == 'row_count_mismatch'


def test_sql_only_verified_data_is_still_not_a_verified_chart(tmp_path):
    db = create_demo_db(tmp_path/'four.sqlite')
    events = [
        ('tool_call', {'name': 'get_schema', 'args': {}}),
        ('tool_result', {'name': 'get_schema', 'message': 'schema...'}),
        ('tool_call', {'name': 'query_database', 'args': {'sql': GOOD_SQL}}),
        ('tool_result', {'name': 'query_database', 'message': '{"rows":[],"truncated":false}'}),
        ('done', {'answer': '这是年度营收汇总。'}),
    ]
    result = evaluate_live_events(m02(), events, db, 8000)
    assert result['status'] == 'failed'
    assert result['sql_evidence_verified'] is True
    assert result['structured_artifact_present'] is False
    assert result['evidence_diagnosis'] == 'sql_numeric_evidence_verified_but_structured_artifact_missing'


def test_failed_tool_call_does_not_claim_sql_evidence(tmp_path):
    db = create_demo_db(tmp_path/'five.sqlite')
    events = [
        ('tool_call', {'name': 'query_database', 'args': {'sql': GOOD_SQL}}),
        ('tool_result', {'name': 'query_database', 'message': '{"error":"oops"}'}),
        ('done', {'answer': 'done'}),
    ]
    result = evaluate_live_events(m02(), events, db, 9000)
    assert result['status'] == 'failed'
    assert result['sql_evidence_verified'] is False
    assert result['tool_errors'] == 1
