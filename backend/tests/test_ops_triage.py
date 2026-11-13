"""A10: priority queue is a read-only projection of actual checkpoints."""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.ops import api
from app.ops.action_tasks import ActionCoordinator, TaskStore
from app.ops.fulfillment import FulfillmentCoordinator
from app.ops.playbook import OperationsPlaybook
from app.ops.store import OpsStore
from app.ops.triage import build_triage


@pytest.fixture
def domain(tmp_path):
    ops = OpsStore(tmp_path / 'ops.sqlite'); ops.init()
    tasks = TaskStore(tmp_path / 'tasks.sqlite'); tasks.init()
    return ops, tasks, ActionCoordinator(ops, tasks)


def test_unassigned_incidents_show_up_without_model_or_mutation(domain):
    ops, tasks, _ = domain
    result = build_triage(ops, tasks)
    assert result['total_incidents'] == 3
    assert result['recent_task_count'] == 0
    assert result['summary']['new'] == 3
    assert result['items'][0]['severity'] == 'high'
    assert all(item['kind'] == 'incident' for item in result['items'])
    with ops._connection() as con:
        assert con.execute('SELECT count(*) FROM command_receipts').fetchone()[0] == 0


def test_pending_approval_instead_of_duplicate_incident_row(domain):
    ops, tasks, coordinator = domain
    created = coordinator.propose('INC-1001')
    result = build_triage(ops, tasks)
    found = [r for r in result['items'] if r['incident_id'] == 'INC-1001']
    assert len(found) == 1
    assert found[0]['task_id'] == created['task_id']
    assert found[0]['gate'] == 'reservation_approval'
    assert found[0]['bucket'] == 'approval'
    assert result['summary']['approval'] == 1


def test_approved_task_is_ready_and_fulfillment_needs_separate_approval(domain):
    ops, tasks, coordinator = domain
    tid = coordinator.propose('INC-1001')['task_id']
    tasks.review(tid, True)
    assert next(x for x in build_triage(ops, tasks)['items'] if x['task_id'] == tid)['bucket'] == 'ready'
    OperationsPlaybook(ops, tasks).advance_until_gate(tid)
    selected = next(x for x in build_triage(ops, tasks)['items'] if x['task_id'] == tid)
    assert selected['gate'] == 'fulfillment_approval'
    assert selected['bucket'] == 'approval'
    assert selected['can_continue'] is False


def test_delivery_confirmation_is_human_gate(domain):
    ops, tasks, coordinator = domain
    tid = coordinator.propose('INC-1001')['task_id']
    tasks.review(tid, True)
    book = OperationsPlaybook(ops, tasks)
    book.advance_until_gate(tid)
    FulfillmentCoordinator(ops, tasks).review(tid, True)
    book.advance_until_gate(tid)
    entry = next(x for x in build_triage(ops, tasks)['items'] if x['task_id'] == tid)
    assert entry['gate'] == 'delivery_attestation'
    assert entry['bucket'] == 'approval'
    assert entry['can_continue'] is False


def test_rejected_task_is_history_but_incident_remains_unresolved(domain):
    ops, tasks, coordinator = domain
    tid = coordinator.propose('INC-1001')['task_id']
    tasks.review(tid, False)
    entries = [x for x in build_triage(ops, tasks)['items'] if x['incident_id'] == 'INC-1001']
    assert len(entries) == 2
    assert next(x for x in entries if x['kind'] == 'task')['bucket'] == 'archive'
    assert next(x for x in entries if x['kind'] == 'incident')['bucket'] == 'new'


def test_readonly_api_same_source_and_invalid_limit_422(domain):
    ops, tasks, coordinator = domain
    coordinator.propose('INC-1001')
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    try:
        client = TestClient(app)
        response = client.get('/api/ops/agent/triage')
        assert response.status_code == 200
        body = response.json()
        assert body['synthetic'] is True
        assert body['summary']['approval'] == 1
        assert len(body['tasks']) == 1
        assert client.get('/api/ops/agent/triage?limit=101').status_code == 422
        with ops._connection() as con:
            assert con.execute('SELECT count(*) FROM audit_log').fetchone()[0] == 0
            assert con.execute('SELECT count(*) FROM command_receipts').fetchone()[0] == 0
    finally:
        app.dependency_overrides.clear()
