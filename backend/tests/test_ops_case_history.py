"""A11: cross-source case provenance remains read-only and incident-scoped."""
from __future__ import annotations

import pytest

from app.ops.action_tasks import ActionCoordinator, TaskStore
from app.ops.case_history import CaseNotFound, build_case_history
from app.ops.service import OpsService
from app.ops.store import OpsStore


@pytest.fixture
def domain(tmp_path):
    ops = OpsStore(tmp_path / 'ops.sqlite')
    ops.init()
    tasks = TaskStore(tmp_path / 'tasks.sqlite')
    tasks.init()
    return ops, tasks


def test_empty_case_history_has_current_business_context(domain):
    ops, tasks = domain
    data = build_case_history(ops, tasks, 'INC-1001')
    assert data['synthetic'] is True
    assert data['incident_id'] == 'INC-1001'
    assert data['order_id'] == 'ORD-1001'
    assert data['order_status'] == 'pending'
    assert data['items'] == []
    assert data['linked_tasks'] == []
    assert data['consistency'] == 'read_only_non_atomic_cross_database_projection'


def test_case_history_combines_task_and_business_audit_without_other_order(domain):
    ops, tasks = domain
    tid = ActionCoordinator(ops, tasks).propose('INC-1001')['task_id']
    OpsService(ops).reserve('ORD-1001', 'WH-WEST', 'casehistory-reserve-1001')
    OpsService(ops).reserve('ORD-1002', 'WH-EAST', 'casehistory-reserve-1002')
    report = build_case_history(ops, tasks, 'INC-1001')
    assert tid in [row['task_id'] for row in report['linked_tasks']]
    assert any(row['source'] == 'task_event' and row['task_id'] == tid for row in report['items'])
    assert any(row['source'] == 'business_audit' and 'ORD-1001' in row['description']
               for row in report['items'])
    assert all('ORD-1002' not in row['description'] for row in report['items'])
    assert all(not row['id'].startswith('business_audit:2') for row in report['items'])
    assert report['items'] == build_case_history(ops, tasks, 'INC-1001')['items']


def test_case_history_can_read_without_task_store_file(domain):
    ops, tasks = domain
    tasks.path.unlink()
    result = build_case_history(ops, tasks, 'INC-1001')
    assert result['linked_tasks'] == []
    assert result['source_counts']['task_event'] == 0
    assert not tasks.path.exists()


def test_invalid_limit_and_missing_case_fail_closed(domain):
    ops, tasks = domain
    for bad in (0, 201, True, -5, '3'):
        with pytest.raises(ValueError):
            build_case_history(ops, tasks, 'INC-1001', bad)
    with pytest.raises(CaseNotFound):
        build_case_history(ops, tasks, 'INC-NOT-FOUND')


def test_limit_is_bounded_and_stable(domain):
    ops, tasks = domain
    task_id = ActionCoordinator(ops, tasks).propose('INC-1001')['task_id']
    tasks.review(task_id, True)
    report = build_case_history(ops, tasks, 'INC-1001', limit=1)
    assert len(report['items']) == 1
    assert report['has_more'] is True
    assert report['linked_tasks'][0]['task_id'] == task_id


def test_no_artificial_business_mutations(domain):
    ops, tasks = domain
    ActionCoordinator(ops, tasks).propose('INC-1001')
    with ops._connection() as con:
        before_audit = con.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0]
        before_receipts = con.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0]
    build_case_history(ops, tasks, 'INC-1001')
    with ops._connection() as con:
        assert con.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0] == before_audit
        assert con.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0] == before_receipts


def test_readonly_http_case_endpoint_and_validation(domain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.ops import api
    ops, tasks = domain
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    client = TestClient(app)
    report = client.get('/api/ops/incidents/INC-1001/case-history')
    assert report.status_code == 200
    assert report.json()['incident_id'] == 'INC-1001'
    assert report.json()['items'] == []
    assert client.get('/api/ops/incidents/UNKNOWN/case-history').status_code == 404
    assert client.get('/api/ops/incidents/INC-1001/case-history?limit=201').status_code == 422
    assert client.get('/api/ops/incidents/INC-1001/case-history?limit=0').status_code == 422


def test_detected_signal_is_linked_by_order_without_cross_case_leakage(domain):
    from datetime import datetime, timezone
    from app.ops.detection import DetectionEngine
    ops, tasks = domain
    DetectionEngine(ops).scan(at=datetime(2026, 11, 12, tzinfo=timezone.utc))
    affected = build_case_history(ops, tasks, 'INC-1003')
    assert any(x['source'] == 'detection' and 'inventory_shortage' in x['description']
               for x in affected['items'])
    unrelated = build_case_history(ops, tasks, 'INC-1002')
    assert not any(x['source'] == 'detection' and 'inventory_shortage' in x['description']
                   for x in unrelated['items'])
