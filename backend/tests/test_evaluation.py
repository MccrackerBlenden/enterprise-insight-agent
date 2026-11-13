"""No network calls: evaluate the evaluation system itself."""
from copy import deepcopy

from app.data import create_demo_db
from app.metric_analysis import analyze_metric
from app.verified_analysis import build_verified_decline
from evals.run import (
    cases_load, evaluate_live_events, evaluate_offline_case,
    expected_decline, expected_metric, run_offline, verify_metric_artifact,
)


def test_dataset_unique_and_complete():
    cases=cases_load()
    assert len(cases)>=30
    assert len({case['id'] for case in cases}) == len(cases)
    assert sum(c['kind']=='metric' for c in cases)>=10
    assert any(c['kind']=='sql_deny' for c in cases)
    assert any(c['kind']=='decline' for c in cases)


def test_offline_all_cases_and_original_db_unmodified():
    result=run_offline(cases_load())
    assert result['mode']=='offline'
    assert result['failed']==0, [r for r in result['results'] if r['status']=='failed']
    assert result['passed']==len(cases_load())
    assert result['tokens'] is None and result['cost'] is None


def test_corrupted_metric_artifact_is_detected(tmp_path):
    path=create_demo_db(tmp_path/'demo.sqlite')
    case=next(c for c in cases_load() if c['id']=='M01')
    artifact=analyze_metric(case['metric'],case['group_by'],case['start_year'],case['end_year'],
                            case['order'],case['top_n'],path)
    assert not verify_metric_artifact(artifact,case,path)
    bad=deepcopy(artifact)
    bad['rows'][0]['value']+=5
    assert '数值不符' in str(verify_metric_artifact(bad,case,path))
    bad2=deepcopy(artifact)
    bad2['rows'][0]['label']='虚构的错误企业'
    assert '排名/分组不符' in str(verify_metric_artifact(bad2,case,path))
    bad3=deepcopy(artifact)
    bad3['chart']['series'][0]['points'][0]['value']+=1
    assert '图表数据与指标证据表不一致' in str(verify_metric_artifact(bad3,case,path))


def test_reference_oracle_is_independent_and_stable(tmp_path):
    path=create_demo_db(tmp_path/'test.sqlite')
    case=next(c for c in cases_load() if c['id']=='M02')
    rows=expected_metric(case,path)
    assert len(rows)==9
    assert all(r['months']==48 for r in rows)
    assert len(expected_decline(path,3))==3


def test_fake_stream_does_not_count_as_real_success(tmp_path):
    path=create_demo_db(tmp_path/'d.sqlite')
    case=next(c for c in cases_load() if c['id']=='M01')
    fake=[('status',{'message':'开始'}),('done',{'answer':'一切正常','tool_calls':0})]
    result=evaluate_live_events(case,fake,path,100)
    assert result['status']=='failed'
    assert result['model_cost'] is None
    assert any('没有实际工具' in reason for reason in result['problems'])


def test_valid_stream_evidence_scores_pass_without_claiming_text_truth(tmp_path):
    path=create_demo_db(tmp_path/'d.sqlite')
    case=next(c for c in cases_load() if c['id']=='M01')
    artifact=analyze_metric('margin','company',2025,2025,'desc',12,path)
    events=[('tool_call',{'name':'analyze_metrics'}),('metric_artifact',artifact),
            ('tool_result',{'name':'analyze_metrics','message':'{"kind":"metric_analysis"}'}),
            ('done',{'answer':'已根据真实结果生成企业排名。'})]
    result=evaluate_live_events(case,events,path,321)
    assert result['status']=='passed'
    assert result['prose_factual_accuracy']=='not_automatically_verified'
    assert result['token_usage'] is None


def test_db_reject_and_eval_output_shape(tmp_path):
    path=create_demo_db(tmp_path/'d.sqlite')
    case=next(c for c in cases_load() if c['id']=='X01')
    result=evaluate_offline_case(case,path)
    assert result['status']=='passed'
    assert 'duration_ms' in result
