from types import SimpleNamespace
from app.convergence import inspect_history, needs_final_answer


def test_metric_analysis_is_real_evidence_when_audited():
    messages = [SimpleNamespace(type='ai', content=''),
        SimpleNamespace(type='tool', name='analyze_metrics',
                        content='{"kind":"metric_analysis","audit":{"status":"passed"},"rows":[{"value":1}]}')]
    stats = inspect_history(messages)
    assert stats['metric_success'] == 1
    assert not needs_final_answer(stats)
    assert needs_final_answer({**stats, 'metric_success': 2})


def test_failed_metric_analysis_is_not_evidence():
    messages = [SimpleNamespace(type='tool', name='analyze_metrics', content='{"error":"bad"}')]
    assert inspect_history(messages)['metric_success'] == 0
