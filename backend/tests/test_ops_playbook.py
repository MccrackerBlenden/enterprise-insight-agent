"""A6: playbook can only move across already-authorized, durable steps."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ops import api
from app.ops.action_tasks import ActionCoordinator, TaskStore
from app.ops.fulfillment import FulfillmentCoordinator
from app.ops.playbook import OperationsPlaybook
from app.ops.service import OpsService, RuleViolation, StateConflict
from app.ops.store import OpsStore


@pytest.fixture
def sandbox(tmp_path):
    ops = OpsStore(tmp_path / 'operations.sqlite'); ops.init()
    tasks = TaskStore(tmp_path / 'tasks.sqlite'); tasks.init()
    task = ActionCoordinator(ops, tasks).propose('INC-1001')
    return ops, tasks, task['task_id'], OperationsPlaybook(ops, tasks)


def receipt_count(ops):
    with ops._connection() as con:
        return con.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0]


def test_unapproved_reservation_cannot_be_autorun(sandbox):
    ops, tasks, tid, book = sandbox
    before = book.preview(tid)
    assert before['gate'] == 'reservation_approval'
    assert before['can_continue'] is False
    outcome = book.advance_until_gate(tid)
    assert outcome['playbook']['steps_executed'] == []
    assert tasks.get(tid)['status'] == 'pending_approval'
    assert receipt_count(ops) == 0


def test_authorized_reservation_stops_at_second_approval(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    outcome = book.advance_until_gate(tid)
    assert outcome['playbook']['steps_executed'] == ['reserve_inventory', 'prepare_fulfillment']
    assert outcome['playbook']['next']['gate'] == 'fulfillment_approval'
    assert outcome['fulfillment']['status'] == 'pending_approval'
    assert ops.get_incident('INC-1001')['order_status'] == 'reserved'
    assert receipt_count(ops) == 1
    assert book.advance_until_gate(tid)['playbook']['steps_executed'] == []
    assert receipt_count(ops) == 1


def test_approved_fulfillment_stops_before_delivery(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    book.advance_until_gate(tid)
    FulfillmentCoordinator(ops, tasks).review(tid, True)
    outcome = book.advance_until_gate(tid)
    assert outcome['playbook']['steps_executed'] == ['dispatch', 'in_transit']
    assert outcome['playbook']['next']['gate'] == 'delivery_attestation'
    assert outcome['fulfillment']['next_step'] == 2
    assert receipt_count(ops) == 3
    assert ops.get_incident('INC-1001')['shipment_status'] == 'in_transit'
    assert book.advance_until_gate(tid)['playbook']['steps_executed'] == []
    assert receipt_count(ops) == 3


def test_after_explicit_delivery_attestation_verifies_incident(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    book.advance_until_gate(tid)
    worker = FulfillmentCoordinator(ops, tasks)
    worker.review(tid, True)
    book.advance_until_gate(tid)
    worker.advance(tid, confirm_delivery=True)
    outcome = book.advance_until_gate(tid)
    assert outcome['playbook']['steps_executed'] == ['verify_incident']
    assert outcome['playbook']['next']['gate'] == 'completed'
    assert ops.get_incident('INC-1001')['state'] == 'resolved'
    assert receipt_count(ops) == 5
    assert book.advance_until_gate(tid)['playbook']['steps_executed'] == []
    assert receipt_count(ops) == 5


def test_rejected_reservation_stays_unmodified(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, False)
    assert book.preview(tid)['gate'] == 'manual_handoff'
    assert book.advance_until_gate(tid)['playbook']['steps_executed'] == []
    assert receipt_count(ops) == 0


def test_rejected_fulfillment_stays_unmodified(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    book.advance_until_gate(tid)
    FulfillmentCoordinator(ops, tasks).review(tid, False)
    assert book.preview(tid)['gate'] == 'manual_handoff'
    assert book.advance_until_gate(tid)['playbook']['steps_executed'] == []
    assert receipt_count(ops) == 1


def test_existing_external_change_marks_stale_not_success(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    OpsService(ops).reserve('ORD-1001', 'WH-WEST', 'external-prebook')
    result = book.advance_until_gate(tid)
    assert result['status'] == 'stale'
    assert result['playbook']['steps_executed'] == ['reserve_inventory']
    assert result['playbook']['next']['gate'] == 'manual_handoff'
    assert receipt_count(ops) == 1


def test_interrupted_reservation_needs_explicit_recovery(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    tasks.claim(tid)
    assert book.preview(tid)['gate'] == 'explicit_recovery'
    assert book.advance_until_gate(tid)['playbook']['steps_executed'] == []
    assert receipt_count(ops) == 0


def test_interrupted_fulfillment_requires_manual_recovery(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    book.advance_until_gate(tid)
    worker = FulfillmentCoordinator(ops, tasks)
    worker.review(tid, True)
    worker._claim(tid, recover=False, confirm_delivery=False)
    assert book.preview(tid)['gate'] == 'explicit_recovery'
    assert book.advance_until_gate(tid)['playbook']['steps_executed'] == []
    assert receipt_count(ops) == 1


def test_budget_bound_does_not_skip_approval(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    one = book.advance_until_gate(tid, max_actions=1)
    assert one['playbook']['steps_executed'] == ['reserve_inventory']
    assert one['playbook']['next']['next_action'] == 'prepare_fulfillment'
    two = book.advance_until_gate(tid, max_actions=1)
    assert two['playbook']['steps_executed'] == ['prepare_fulfillment']
    assert two['playbook']['next']['gate'] == 'fulfillment_approval'
    assert receipt_count(ops) == 1
    for bad in (0, -1, 5, True, 1.4):
        with pytest.raises(RuleViolation):
            book.advance_until_gate(tid, max_actions=bad)


def test_state_is_durable_across_new_coordinators(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    book.advance_until_gate(tid)
    next_runner = OperationsPlaybook(OpsStore(ops.path), TaskStore(tasks.path))
    assert next_runner.preview(tid)['gate'] == 'fulfillment_approval'
    FulfillmentCoordinator(ops, tasks).review(tid, True)
    assert next_runner.advance_until_gate(tid)['playbook']['next']['gate'] == 'delivery_attestation'


def test_two_requests_cannot_double_reserve(sandbox):
    ops, tasks, tid, book = sandbox
    tasks.review(tid, True)
    def run(_):
        try:
            OperationsPlaybook(OpsStore(ops.path), TaskStore(tasks.path)).advance_until_gate(tid)
            return 'ok'
        except StateConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        output = list(pool.map(run, [1, 2]))
    assert len(output) == 2
    assert receipt_count(ops) == 1
    assert tasks.get(tid)['fulfillment']['status'] == 'pending_approval'


def test_api_preview_is_readonly_and_writes_require_token(sandbox):
    ops, tasks, tid, book = sandbox
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    try:
        client = TestClient(app)
        endpoint = f'/api/ops/agent/tasks/{tid}/playbook'
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.json()['gate'] == 'reservation_approval'
        assert client.post(endpoint + '/advance').status_code == 403
        assert receipt_count(ops) == 0
        app.dependency_overrides[api.require_write_token] = lambda: None
        assert client.post(endpoint + '/advance').status_code == 200
        assert receipt_count(ops) == 0  # explicit reservation approval still required
        tasks.review(tid, True)
        assert client.post(endpoint + '/advance').json()['fulfillment']['status'] == 'pending_approval'
        assert receipt_count(ops) == 1
    finally:
        app.dependency_overrides.clear()
