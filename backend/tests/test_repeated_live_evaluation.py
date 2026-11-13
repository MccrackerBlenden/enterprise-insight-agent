"""V0.4-B1: repeat scheduling and aggregation do not call billable APIs."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.data import create_demo_db
from app.metric_analysis import analyze_metric
from evals import run as eval_module
from evals.run import (
    cases_load, main, plan_live_runs, run_live, summarize_live_repetitions,
)


def test_exact_repeat_schedule_and_limit():
    cases = cases_load()
    assert [(c['id'], repetition) for c, repetition in plan_live_runs(cases, 1, 'M02', 3)] == [
        ('M02', 1), ('M02', 2), ('M02', 3),
    ]
    assert [(c['id'], repetition) for c, repetition in plan_live_runs(cases, 2, None, 2)] == [
        ('M01', 1), ('M01', 2), ('M02', 1), ('M02', 2),
    ]
    for repeat in (-1, 0, 4):
        with pytest.raises(ValueError, match='repeat'):
            plan_live_runs(cases, 1, None, repeat)
    with pytest.raises(ValueError, match='安全上限'):
        plan_live_runs(cases, 5, None, 3)
    with pytest.raises(ValueError, match='未找到'):
        plan_live_runs(cases, 1, 'DOES_NOT_EXIST', 2)


def test_preview_is_free_even_without_key(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('Plan must not touch live model')
    monkeypatch.setattr(eval_module, 'run_live', forbidden)
    rc = main(['live', '--case-id', 'M02', '--repeat', '3', '--plan'])
    assert rc == 0
    output = capsys.readouterr().out
    assert '预计任务执行次数：3' in output
    assert output.count('M02 第') == 3
    assert main(['live', '--case-id', 'M02', '--repeat', '3']) == 2
    assert main(['offline', '--repeat', '2']) == 2
    assert main(['live', '--repeat', '4', '--plan']) == 2


def test_group_summary_counts_paths_tokens_without_fake_zeros():
    results = [
        {'id':'M02', 'status':'passed', 'observed_tools':['get_schema', 'analyze_metrics'],
         'problems':[], 'duration_ms': 1000, 'model_calls':2,
         'token_usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120,'complete':True}},
        {'id':'M02', 'status':'failed', 'observed_tools':['get_schema', 'query_database'],
         'problems':['缺少结构化成果'], 'duration_ms': 2000, 'model_calls':2,
         'token_usage':None},
        {'id':'M02', 'status':'passed', 'observed_tools':['analyze_metrics'],
         'problems':[], 'duration_ms': 3000, 'model_calls':1,
         'token_usage':{'input_tokens':50,'output_tokens':10,'total_tokens':60,'complete':True}},
    ]
    summary = summarize_live_repetitions(results)['M02']
    assert summary['repetitions'] == 3 and summary['passed'] == 2
    assert summary['observed_pass_fraction'] == 0.6667
    assert summary['latency_ms_p50'] == 2000
    assert summary['model_calls_total'] == 5
    assert summary['model_calls_mean'] == 1.67
    assert summary['tool_path_counts']['get_schema → query_database'] == 1
    assert summary['failure_reason_counts'] == {'缺少结构化成果':1}
    assert summary['tokens_complete'] is False
    assert summary['tokens_total'] is None
    results[1]['token_usage'] = {'input_tokens':90,'output_tokens':10,'total_tokens':100,'complete':True}
    assert summarize_live_repetitions(results)['M02']['tokens_total']['total_tokens'] == 280


def test_run_live_invokes_independent_agent_for_each_repetition(monkeypatch, tmp_path):
    cases = cases_load()
    db = create_demo_db(tmp_path/'d.sqlite')
    monkeypatch.setattr(eval_module, 'ensure_demo_db', lambda p: db)
    from app import config
    monkeypatch.setattr(config, 'is_ready', lambda: True)
    actual_calls = []
    artifact = analyze_metric('revenue', 'industry_year', 2023, 2025, path=db)
    def fake_stream(prompt):
        actual_calls.append(prompt)
        yield 'tool_call', {'name':'analyze_metrics'}
        yield 'metric_artifact', artifact
        yield 'done', {'answer':'已分析行业收入年度趋势。'}
        yield 'telemetry', {'schema_version':1,'model_calls':1,
                            'token_usage':{'input_tokens':100,'output_tokens':30,
                                           'total_tokens':130,'complete':True}}
    monkeypatch.setitem(sys.modules, 'app.agent', SimpleNamespace(run_agent_stream=fake_stream))
    report = run_live(cases, 1, 'M02', 3)
    assert len(actual_calls) == 3
    assert len(report['results']) == 3
    assert [r['repetition'] for r in report['results']] == [1,2,3]
    assert report['passed'] == 3
    assert report['repetitions_per_case'] == 3
    assert report['by_case']['M02']['tool_path_counts'] == {'analyze_metrics':3}
    assert report['by_case']['M02']['tokens_total']['total_tokens'] == 390
