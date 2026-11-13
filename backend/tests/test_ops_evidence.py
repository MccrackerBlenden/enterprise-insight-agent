"""A7: durable investigation provenance, controlled task binding and old API compatibility."""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ops import api, investigation_agent
from app.ops.action_tasks import ActionCoordinator, TaskStore
from app.ops.evidence import capture_evidence
from app.ops.service import StateConflict, NotFound
from app.ops.store import OpsStore


@pytest.fixture
def environment(tmp_path):
    ops = OpsStore(tmp_path/'ops.sqlite')
    ops.init()
    tasks = TaskStore(tmp_path/'tasks.sqlite')
    tasks.init()
    return ops, tasks


def test_deterministic_proposal_captures_free_evidence_without_write(environment):
    ops, tasks = environment
    task = ActionCoordinator(ops, tasks).propose('INC-1001')
    inv = tasks.get_investigation(task['proposal']['investigation_id'])
    assert inv['model_status'] == 'not_run'
    assert inv['evidence_sha256'] == task['proposal']['evidence_sha256']
    assert inv['evidence']['quote']['recommended_warehouse_id'] == 'WH-WEST'
    assert inv['evidence']['incident']['order_id'] == 'ORD-1001'
    assert inv['model_text_independently_verified'] is False
    with ops._connection() as con:
        assert con.execute('select count(*) from command_receipts').fetchone()[0] == 0


def test_quote_clock_rendering_does_not_change_evidence_fingerprint(environment):
    ops, _ = environment
    q1 = ops.fulfillment_quote('ORD-1001')
    q2 = copy.deepcopy(q1)
    q2['observed_at'] = '2099-01-01T00:00:00+00:00'
    q2['valid_until'] = '2099-01-02T00:00:00+00:00'
    for option in q2['options']:
        option['projected_ready_at'] = '2099-01-03T00:00:00+00:00'
        option['simulated_late_minutes'] = 999
        option['sla_risk'] = 'late'
    first = capture_evidence(ops, 'INC-1001', quote=q1)
    second = capture_evidence(ops, 'INC-1001', quote=q2)
    assert first['sha256'] == second['sha256']


def test_reopen_existing_task_database_preserves_record(environment):
    ops, tasks = environment
    created = ActionCoordinator(ops, tasks).propose('INC-1001')
    TaskStore(tasks.path).init()
    reopened = TaskStore(tasks.path)
    assert reopened.get_investigation(created['proposal']['investigation_id'])['evidence_sha256'] == created['proposal']['evidence_sha256']
    assert reopened.get(created['task_id'])['proposal']['investigation_id']
    with ops._connection() as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 2


def test_model_completion_can_be_referenced_to_create_task(environment):
    ops, tasks = environment
    captured = capture_evidence(ops, 'INC-1001')
    inv_id = tasks.create_investigation('INC-1001', captured, model_status='running')['investigation_id']
    finished = tasks.finish_investigation(inv_id, model_status='completed', summary='unverified analysis', tool_calls=3)
    assert finished['model_summary'] == 'unverified analysis'
    assert finished['model_text_independently_verified'] is False
    task = ActionCoordinator(ops, tasks).propose('INC-1001', investigation_id=inv_id)
    assert task['proposal']['investigation_id'] == inv_id
    assert task['proposal']['model_narrative_independently_verified'] is False
    with pytest.raises(StateConflict):
        tasks.finish_investigation(inv_id, model_status='completed')


def test_stale_facts_reject_linked_investigation_without_business_write(environment):
    ops, tasks = environment
    inv_id = tasks.create_investigation('INC-1001', capture_evidence(ops, 'INC-1001'))['investigation_id']
    with ops.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=on_hand+1 WHERE warehouse_id='WH-WEST' AND sku_id='SKU-A'")
    with pytest.raises(StateConflict):
        ActionCoordinator(ops, tasks).propose('INC-1001', investigation_id=inv_id)
    with ops._connection() as con:
        assert con.execute('select count(*) from command_receipts').fetchone()[0] == 0
    assert tasks.list() == []


def test_wrong_incident_and_unknown_id_rejected(environment):
    ops, tasks = environment
    inv_id = tasks.create_investigation('INC-1001', capture_evidence(ops, 'INC-1001'))['investigation_id']
    with pytest.raises(StateConflict):
        ActionCoordinator(ops, tasks).propose('INC-1002', investigation_id=inv_id)
    with pytest.raises(NotFound):
        tasks.get_investigation('../not-an-id')
    with pytest.raises(StateConflict):
        ActionCoordinator(ops, tasks).propose('INC-1001', investigation_id='INV-' + '0'*20)


def test_failed_or_running_model_run_is_not_approved_as_evidence(environment):
    ops, tasks = environment
    inv_id = tasks.create_investigation('INC-1001', capture_evidence(ops,'INC-1001'), model_status='running')['investigation_id']
    with pytest.raises(StateConflict):
        ActionCoordinator(ops, tasks).propose('INC-1001', investigation_id=inv_id)
    tasks.finish_investigation(inv_id, model_status='failed')
    with pytest.raises(StateConflict):
        ActionCoordinator(ops, tasks).propose('INC-1001', investigation_id=inv_id)


def test_token_gate_and_evidence_read_api(environment, monkeypatch):
    ops, tasks = environment
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    try:
        client = TestClient(app)
        assert client.post('/api/ops/agent/tasks', json={'incident_id': 'INC-1001'}).status_code == 403
        app.dependency_overrides[api.require_write_token] = lambda: None
        created = client.post('/api/ops/agent/tasks', json={'incident_id': 'INC-1001'})
        assert created.status_code == 200, created.text
        inv_id = created.json()['proposal']['investigation_id']
        record = client.get('/api/ops/agent/investigations/' + inv_id)
        assert record.status_code == 200
        assert record.json()['model_text_independently_verified'] is False
    finally:
        app.dependency_overrides.clear()


def test_stream_persists_model_result_without_model_write_access(environment, monkeypatch):
    ops, tasks = environment
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    app.dependency_overrides[api.require_write_token] = lambda: None
    def fake_stream(_ops, incident_id):
        assert incident_id == 'INC-1001'
        yield 'tool_call', {'name':'inspect_incident'}
        yield 'tool_result', {'name':'inspect_incident','message':'{}'}
        yield 'done', {'answer':'mock analysis; unverified', 'tool_calls': 2}
    monkeypatch.setattr(investigation_agent, 'investigate_stream', fake_stream)
    try:
        client = TestClient(app)
        with client.stream('POST', '/api/ops/agent/investigate/stream', json={'incident_id':'INC-1001'}) as res:
            assert res.status_code == 200
            frames = list(res.iter_lines())
        payloads = [json.loads(line[6:]) for line in frames if line.startswith('data: ')]
        inv_id = payloads[0]['investigation_id']
        record = tasks.get_investigation(inv_id)
        assert record['model_status'] == 'completed'
        assert record['model_summary'] == 'mock analysis; unverified'
        assert ActionCoordinator(ops, tasks).propose('INC-1001', investigation_id=inv_id)
        with ops._connection() as con:
            assert con.execute('select count(*) from command_receipts').fetchone()[0] == 0
    finally:
        app.dependency_overrides.clear()


def test_failed_stream_persists_failed_status(environment, monkeypatch):
    ops, tasks = environment
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    app.dependency_overrides[api.require_write_token] = lambda: None
    monkeypatch.setattr(investigation_agent, 'investigate_stream', lambda *_: iter([('error',{'message':'failed'})]))
    try:
        client = TestClient(app)
        res = client.post('/api/ops/agent/investigate/stream',json={'incident_id':'INC-1001'})
        assert res.status_code == 200
        identifier = next(json.loads(x[6:])['investigation_id'] for x in res.text.splitlines() if x.startswith('data: ') and 'investigation_id' in x)
        assert tasks.get_investigation(identifier)['model_status'] == 'failed'
    finally:
        app.dependency_overrides.clear()
