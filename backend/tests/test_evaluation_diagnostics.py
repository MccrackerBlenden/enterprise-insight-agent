"""No-API tests for live evaluation diagnostics and single-case selection."""
from app.data import create_demo_db
from app.metric_analysis import analyze_metric
from evals.run import cases_load, evaluate_live_events, select_live_cases
import pytest


def test_select_single_case_without_preceding_calls():
    cases = cases_load()
    selected = select_live_cases(cases, 1, 'M02')
    assert [item['id'] for item in selected] == ['M02']
    assert [item['id'] for item in select_live_cases(cases, 2)] == ['M01', 'M02']


def test_reject_unknown_live_case():
    with pytest.raises(ValueError, match='未找到可执行'):
        select_live_cases(cases_load(), 1, 'X_DOES_NOT_EXIST')


def test_sql_only_is_diagnosed_not_falsely_passed(tmp_path):
    path = create_demo_db(tmp_path/'d.sqlite')
    case = next(c for c in cases_load() if c['id'] == 'M02')
    events = [
        ('tool_call', {'name':'query_database'}),
        ('tool_result', {'name':'query_database','message':'{"rows":[{"year":2023}]}' }),
        ('done', {'answer':'SQL 查询后输出年度收入。'}),
    ]
    result = evaluate_live_events(case,events,path,123)
    assert result['status'] == 'failed'
    assert result['evidence_diagnosis'] == 'sql_only_no_structured_metric_artifact'
    assert result['observed_tools'] == ['query_database']


def test_wrong_metric_artifact_metadata_is_recorded(tmp_path):
    path = create_demo_db(tmp_path/'d.sqlite')
    case = next(c for c in cases_load() if c['id'] == 'M02')
    artifact = analyze_metric('margin','industry_year',2023,2025,path=path)
    events = [
        ('tool_call', {'name':'analyze_metrics'}),
        ('metric_artifact',artifact),
        ('done', {'answer':'完成趋势分析'}),
    ]
    result = evaluate_live_events(case,events,path,123)
    assert result['status'] == 'failed'
    assert result['evidence_diagnosis'] == 'analyze_metrics_called_without_matching_artifact'
    observed = result['observed_metric_artifacts'][0]
    assert observed['metric'] == 'margin'
    assert observed['group_by'] == 'industry_year'
    assert observed['start_year'] == 2023
    assert observed['end_year'] == 2025


def test_matching_artifact_diagnosis_passes(tmp_path):
    path = create_demo_db(tmp_path/'d.sqlite')
    case = next(c for c in cases_load() if c['id'] == 'M02')
    artifact = analyze_metric('revenue','industry_year',2023,2025,path=path)
    events = [
        ('tool_call', {'name':'analyze_metrics'}),
        ('metric_artifact',artifact),
        ('done', {'answer':'收入趋势已完成'}),
    ]
    result = evaluate_live_events(case,events,path,123)
    assert result['status'] == 'passed', result['problems']
    assert result['evidence_diagnosis'] == 'matching_metric_artifact_observed'
