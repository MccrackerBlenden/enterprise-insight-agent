"""V0.4-B: verify telemetry without any billable model calls."""
from types import SimpleNamespace

from app.telemetry import RunTelemetry, token_counts, response_tokens
from app.data import create_demo_db
from app.metric_analysis import analyze_metric
from evals.run import cases_load, evaluate_live_events, _aggregate


def test_usage_normalization_does_not_create_false_zeros():
    assert token_counts(None) is None
    assert token_counts({'total_tokens': 12}) is None
    assert token_counts({'prompt_tokens': 12, 'completion_tokens': 4}) == {
        'input_tokens': 12, 'output_tokens': 4, 'total_tokens': 16,
    }
    assert token_counts({'input_tokens': -1, 'output_tokens': 2}) is None


def test_usage_from_ai_message_takes_priority_over_provider_metadata():
    resp = SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
        usage_metadata={'input_tokens': 20, 'output_tokens': 5, 'total_tokens': 25},
        response_metadata={'token_usage': {'prompt_tokens': 999, 'completion_tokens': 999}},
    ))]], llm_output=None)
    assert response_tokens(resp)['input_tokens'] == 20


def test_run_telemetry_collects_without_prompts_or_tool_inputs(monkeypatch):
    for key in ('EIA_PRICE_INPUT_CNY_PER_1M', 'EIA_PRICE_OUTPUT_CNY_PER_1M'):
        monkeypatch.delenv(key, raising=False)
    callback = RunTelemetry('qwen-plus')
    callback.on_chat_model_start({'name':'llm'}, [['SENSITIVE PROMPT']], run_id='model-1')
    callback.on_tool_start({'name':'analyze_metrics'}, 'SELECT SECRET PASSWORD', run_id='tool-1')
    callback.on_tool_end('SECRET DATA', run_id='tool-1')
    fake = SimpleNamespace(generations=[], llm_output={'token_usage': {
        'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
    }})
    callback.on_llm_end(fake, run_id='model-1')
    snap = callback.snapshot()
    assert snap['model_calls'] == 1 and snap['tool_calls'] == 1
    assert snap['token_usage']['total_tokens'] == 120
    assert snap['token_usage']['complete'] is True
    assert snap['cost_cny_estimate'] is None
    assert 'SECRET' not in str(snap)
    assert len(snap['tools']) == 1 and snap['tools'][0]['name'] == 'analyze_metrics'


def test_partial_usage_does_not_masquerade_as_complete():
    callback = RunTelemetry('qwen-plus')
    callback.on_chat_model_start({}, [], run_id='r1')
    callback.on_llm_end(SimpleNamespace(generations=[], llm_output={}), run_id='r1')
    callback.on_chat_model_start({}, [], run_id='r2')
    callback.on_llm_end(SimpleNamespace(generations=[], llm_output={
        'token_usage': {'prompt_tokens': 9, 'completion_tokens': 4}}), run_id='r2')
    snap = callback.snapshot()
    assert snap['model_calls'] == 2
    assert snap['token_usage']['reported_calls'] == 1
    assert snap['token_usage']['complete'] is False
    assert snap['cost_cny_estimate'] is None


def test_cost_is_only_manual_estimate_with_complete_usage(monkeypatch):
    monkeypatch.setenv('EIA_PRICE_INPUT_CNY_PER_1M', '3')
    monkeypatch.setenv('EIA_PRICE_OUTPUT_CNY_PER_1M', '9')
    callback = RunTelemetry('qwen-plus')
    callback.on_chat_model_start({}, [], run_id='r1')
    callback.on_llm_end(SimpleNamespace(generations=[], llm_output={'token_usage': {
        'prompt_tokens': 1000000, 'completion_tokens': 1000000}}), run_id='r1')
    snap = callback.snapshot()
    assert snap['cost_cny_estimate'] == 12.0
    assert snap['cost_basis'] == 'manual_flat_rate_estimate'


def test_evaluator_reads_real_telemetry_without_changing_scoring(tmp_path):
    db = create_demo_db(tmp_path/'d.sqlite')
    case = next(c for c in cases_load() if c['id']=='M02')
    artifact = analyze_metric('revenue','industry_year',2023,2025,path=db)
    stream = [
        ('tool_call',{'name':'analyze_metrics'}),
        ('metric_artifact',artifact),
        ('done',{'answer':'根据工具结果完成营业收入分析'}),
        ('telemetry',{'schema_version':1, 'model_calls':2, 'model_failures':0,
            'model_durations_ms':[500,600], 'tools':[{'name':'analyze_metrics','duration_ms':10}],
            'token_usage':{'input_tokens':900,'output_tokens':100,'total_tokens':1000,
                           'reported_calls':2,'model_calls':2,'complete':True},
            'cost_cny_estimate':None, 'cost_basis':'unknown_not_billed_cost'}),
    ]
    result = evaluate_live_events(case,stream,db,1700)
    assert result['status']=='passed'
    assert result['model_calls']==2
    assert result['token_usage']['total_tokens']==1000
    assert result['model_cost'] is None
    report=_aggregate('live',[result],db)
    assert report['tokens']['total_tokens']==1000
    assert report['model_calls']==2 and report['cost'] is None


def test_missing_telemetry_stays_unknown(tmp_path):
    db = create_demo_db(tmp_path/'d.sqlite')
    case = next(c for c in cases_load() if c['id']=='M02')
    result=evaluate_live_events(case,[('done',{'answer':'x'})],db,1)
    assert result['model_calls'] is None
    assert result['token_usage'] is None
    summary=_aggregate('live',[result],db)
    assert summary['tokens'] is None and summary['model_calls'] is None
