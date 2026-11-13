"""A5: real synthetic command state, separate approvals, step checkpoints and recovery."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ops import api
from app.ops.action_tasks import ActionCoordinator, TaskStore
from app.ops.fulfillment import FulfillmentCoordinator
from app.ops.service import OpsService, RuleViolation, StateConflict
from app.ops.store import OpsStore


@pytest.fixture
def domain(tmp_path):
    ops = OpsStore(tmp_path / 'operations.sqlite')
    ops.init()
    tasks = TaskStore(tmp_path / 'action_tasks.sqlite')
    tasks.init()
    actor = ActionCoordinator(ops, tasks)
    task_id = actor.propose('INC-1001')['task_id']
    tasks.review(task_id, True)
    assert actor.run(task_id)['status'] == 'succeeded'
    return ops, tasks, FulfillmentCoordinator(ops, tasks), task_id


def receipt_count(ops):
    with ops._connection() as con:
        return con.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0]


def ready(domain):
    ops, tasks, coordinator, tid = domain
    assert coordinator.prepare(tid)['fulfillment']['status'] == 'pending_approval'
    assert coordinator.review(tid, True)['fulfillment']['status'] == 'approved'
    return coordinator, tid


def test_requires_reservation_and_separate_approval(tmp_path):
    ops = OpsStore(tmp_path / 'ops.sqlite'); ops.init()
    tasks = TaskStore(tmp_path / 'tasks.sqlite'); tasks.init()
    tid = ActionCoordinator(ops, tasks).propose('INC-1001')['task_id']
    worker = FulfillmentCoordinator(ops, tasks)
    with pytest.raises(StateConflict):
        worker.prepare(tid)
    tasks.review(tid, True)
    with pytest.raises(StateConflict):
        worker.prepare(tid)
    ActionCoordinator(ops, tasks).run(tid)
    worker.prepare(tid)
    with pytest.raises(StateConflict):
        worker.advance(tid)
    assert receipt_count(ops) == 1


def test_prepare_idempotent_and_refuses_invalid_review(domain):
    ops, tasks, worker, tid = domain
    first = worker.prepare(tid)
    assert worker.prepare(tid)['fulfillment'] == first['fulfillment']
    assert [e['kind'] for e in tasks.get(tid)['events']].count('fulfillment_proposed') == 1
    with pytest.raises(StateConflict):
        worker.advance(tid)
    assert worker.review(tid, False)['fulfillment']['status'] == 'rejected'
    with pytest.raises(StateConflict):
        worker.review(tid, True)
    assert receipt_count(ops) == 1


def test_full_synthetic_fulfillment_and_independent_verification(domain):
    ops, tasks, worker, tid = domain
    worker.prepare(tid)
    worker.review(tid, True)
    outcomes = []
    for _ in range(2):
        outcomes.append(worker.advance(tid))
    wf = outcomes[-1]['fulfillment']
    assert wf['status'] == 'awaiting_delivery_confirmation'
    assert wf['next_step'] == 2
    incident = ops.get_incident('INC-1001')
    assert incident['order_status'] == 'shipped'
    assert incident['shipment_status'] == 'in_transit'
    with pytest.raises(RuleViolation):
        worker.advance(tid)
    worker.advance(tid, confirm_delivery=True)
    finished = worker.advance(tid)
    assert finished['fulfillment']['status'] == 'completed'
    assert finished['fulfillment']['next_step'] == 4
    final = ops.get_incident('INC-1001')
    assert final['order_status'] == 'delivered'
    assert final['shipment_status'] == 'delivered'
    assert final['state'] == 'resolved'
    assert receipt_count(ops) == 5
    kinds = [e['kind'] for e in finished['events']]
    assert kinds.count('fulfillment_step_verified') == 4
    assert kinds.count('synthetic_delivery_attested') == 1
    with pytest.raises(StateConflict):
        worker.advance(tid)


def test_delivery_flag_does_not_authorize_early_dispatch(domain):
    ops, tasks, worker, tid = domain
    ready(domain)
    with pytest.raises(RuleViolation):
        worker.advance(tid, confirm_delivery=True)
    assert receipt_count(ops) == 1


def test_rejected_fulfillment_never_dispatches(domain):
    ops, _, worker, tid = domain
    worker.prepare(tid)
    worker.review(tid, False)
    with pytest.raises(StateConflict):
        worker.advance(tid)
    assert ops.get_incident('INC-1001')['order_status'] == 'reserved'
    assert receipt_count(ops) == 1


def test_crash_after_dispatch_commit_replays_exact_receipt(domain, monkeypatch):
    ops, tasks, worker, tid = domain
    ready(domain)
    original = worker._finish
    monkeypatch.setattr(worker, '_finish', lambda *args, **kw: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        worker.advance(tid)
    assert ops.get_incident('INC-1001')['order_status'] == 'shipped'
    assert receipt_count(ops) == 2
    monkeypatch.setattr(worker, '_finish', original)
    recovered = FulfillmentCoordinator(OpsStore(ops.path), TaskStore(tasks.path)).advance(tid, recover=True)
    assert recovered['fulfillment']['next_step'] == 1
    assert recovered['events'][-1]['detail']['evidence']['command_replayed'] is True
    assert receipt_count(ops) == 2


@pytest.mark.parametrize('step_no', [1, 2, 3])
def test_crash_after_subsequent_command_recovery(domain, monkeypatch, step_no):
    ops, tasks, worker, tid = domain
    ready(domain)
    for i in range(step_no):
        worker.advance(tid, confirm_delivery=(i == 2))
    original = worker._finish
    monkeypatch.setattr(worker, '_finish', lambda *args, **kw: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        worker.advance(tid, confirm_delivery=(step_no == 2))
    count = receipt_count(ops)
    monkeypatch.setattr(worker, '_finish', original)
    recovered = FulfillmentCoordinator(ops, TaskStore(tasks.path)).advance(tid, recover=True)
    assert recovered['fulfillment']['next_step'] == step_no + 1
    assert receipt_count(ops) == count


def test_recovery_requires_interrupted_step(domain):
    _, _, worker, tid = domain
    ready(domain)
    with pytest.raises(StateConflict):
        worker.advance(tid, recover=True)


def test_external_inventory_release_block_dispatch_without_fake_success(domain):
    ops, _, worker, tid = domain
    ready(domain)
    OpsService(ops).release('ORD-1001', 'external-release-101')
    blocked = worker.advance(tid)
    assert blocked['fulfillment']['status'] == 'blocked'
    assert blocked['fulfillment']['failure_code'] == 'StateConflict'
    assert ops.get_incident('INC-1001')['order_status'] == 'pending'
    with pytest.raises(StateConflict):
        worker.advance(tid)


def test_concurrent_dispatch_never_double_writes(domain, monkeypatch):
    ops, tasks, worker, tid = domain
    ready(domain)
    dispatch_inflight = Event()
    allow_dispatch = Event()
    original_step = FulfillmentCoordinator._business_step

    def hold_first_dispatch(self, task, wf, step):
        # Force genuine overlap while the first caller owns the durable step.
        # Without this barrier the second caller may legitimately start the
        # *next* step after the first finishes, yielding [1, 2].
        if step == 'dispatch':
            dispatch_inflight.set()
            if not allow_dispatch.wait(timeout=5):
                raise TimeoutError('Concurrent dispatch test synchronization timed out')
        return original_step(self, task, wf, step)

    monkeypatch.setattr(FulfillmentCoordinator, '_business_step', hold_first_dispatch)

    def execute():
        try:
            return FulfillmentCoordinator(OpsStore(ops.path), TaskStore(tasks.path)).advance(tid)['fulfillment']['next_step']
        except StateConflict:
            return -1

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute)
        try:
            assert dispatch_inflight.wait(timeout=5), 'First dispatch never entered the business step'
            second = pool.submit(execute)
            assert second.result(timeout=5) == -1
        finally:
            allow_dispatch.set()  # Do not strand the first worker on assertion failure.
        assert first.result(timeout=5) == 1

    assert receipt_count(ops) == 2  # One reservation + one dispatch, not two dispatches.
    with ops._connection() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM command_receipts WHERE operation='dispatch'"
        ).fetchone()[0] == 1
    assert ops.get_incident('INC-1001')['shipment_status'] == 'created'


def test_manual_handoff_keeps_reserved_inventory_without_consent(domain):
    ops, tasks, worker, tid = domain
    ready(domain)
    result = worker.handoff(tid, reason='Operator will assess the event', release_reservation=False)
    assert result['fulfillment']['status'] == 'handoff'
    assert ops.get_incident('INC-1001')['order_status'] == 'reserved'
    assert receipt_count(ops) == 1
    with pytest.raises(StateConflict):
        worker.advance(tid)


def test_explicit_release_compensation_before_dispatch(domain):
    ops, tasks, worker, tid = domain
    ready(domain)
    result = worker.handoff(tid, reason='Release to human queue', release_reservation=True)
    assert result['fulfillment']['status'] == 'handoff'
    assert ops.get_incident('INC-1001')['order_status'] == 'pending'
    assert ops.get_incident('INC-1001')['state'] == 'open'
    assert receipt_count(ops) == 2
    with pytest.raises(StateConflict):
        worker.advance(tid)


def test_never_release_inventory_after_dispatch(domain):
    ops, tasks, worker, tid = domain
    ready(domain)
    worker.advance(tid)
    with pytest.raises(StateConflict):
        worker.handoff(tid, reason='Manual intervention needed', release_reservation=True)
    result = worker.handoff(tid, reason='Manual intervention needed', release_reservation=False)
    assert result['fulfillment']['status'] == 'handoff'
    assert ops.get_incident('INC-1001')['order_status'] == 'shipped'


def test_compensation_crash_recovers_without_duplicate_release(domain, monkeypatch):
    ops, tasks, worker, tid = domain
    ready(domain)
    original = worker._release_claimed
    monkeypatch.setattr(worker, '_release_claimed', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        worker.handoff(tid, reason='Release after exception', release_reservation=True)
    assert tasks.get(tid)['fulfillment']['inflight_step'] == 'release'
    monkeypatch.setattr(worker, '_release_claimed', original)
    recovered = FulfillmentCoordinator(ops, TaskStore(tasks.path)).advance(tid, recover=True)
    assert recovered['fulfillment']['status'] == 'handoff'
    assert ops.get_incident('INC-1001')['order_status'] == 'pending'


def test_additive_table_preserves_existing_a4_tasks(domain):
    _, tasks, _, tid = domain
    with tasks.transaction() as con:
        con.execute('DROP TABLE fulfillment_workflows')
    TaskStore(tasks.path).init()
    original = TaskStore(tasks.path).get(tid)
    assert original['status'] == 'succeeded'
    assert original['fulfillment'] is None


def test_http_write_gate_and_validation(domain, monkeypatch):
    ops, tasks, worker, tid = domain
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    try:
        client = TestClient(app)
        path = f'/api/ops/agent/tasks/{tid}/fulfillment'
        assert client.post(path + '/prepare').status_code == 403
        assert client.get(f'/api/ops/agent/tasks/{tid}').status_code == 200
        app.dependency_overrides[api.require_write_token] = lambda: None
        assert client.post(path + '/prepare').status_code == 200
        assert client.post(path + '/advance', json={}).status_code == 409
        assert client.post(path + '/review', json={'approved': True}).status_code == 200
        assert client.post(path + '/advance', json={'confirm_synthetic_delivery': True}).status_code == 422
        assert client.post(path + '/advance', json={}).status_code == 200
        assert client.post(path + '/handoff', json={'reason': 'Manual handoff request',
                                                  'release_reservation': True}).status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_handoff_requires_reason_and_does_not_bypass_approval(domain):
    ops, tasks, worker, tid = domain
    worker.prepare(tid)
    with pytest.raises(RuleViolation):
        worker.handoff(tid, reason='No')
    result = worker.handoff(tid, reason='Declined, use manual queue', release_reservation=False)
    assert result['fulfillment']['status'] == 'handoff'
    assert ops.get_incident('INC-1001')['order_status'] == 'reserved'


def test_release_commit_then_task_checkpoint_crashes_recover_no_duplicate(domain, monkeypatch):
    ops, tasks, worker, tid = domain
    ready(domain)
    original = worker._business_step
    def crash_after_release_commit(task, wf, step):
        evidence = original(task, wf, step)
        if step == 'release':
            raise RuntimeError('crash after Ops release committed')
        return evidence
    monkeypatch.setattr(worker, '_business_step', crash_after_release_commit)
    with pytest.raises(RuntimeError):
        worker.handoff(tid, reason='Release after timeout', release_reservation=True)
    assert receipt_count(ops) == 2
    assert tasks.get(tid)['fulfillment']['inflight_step'] == 'release'
    recovered = FulfillmentCoordinator(ops, TaskStore(tasks.path)).advance(tid, recover=True)
    assert recovered['fulfillment']['status'] == 'handoff'
    assert receipt_count(ops) == 2
    assert recovered['events'][-1]['detail']['evidence']['command_replayed'] is True


def test_api_full_synthetic_flow_requires_separate_approvals(domain):
    ops, tasks, worker, tid = domain
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    app.dependency_overrides[api.require_write_token] = lambda: None
    try:
        client = TestClient(app)
        base = f'/api/ops/agent/tasks/{tid}/fulfillment'
        assert client.post(base + '/prepare').status_code == 200
        assert client.post(base + '/advance', json={}).status_code == 409
        assert client.post(base + '/review', json={'approved': True}).status_code == 200
        assert client.post(base + '/advance', json={}).json()['fulfillment']['next_step'] == 1
        assert client.post(base + '/advance', json={}).json()['fulfillment']['next_step'] == 2
        assert client.post(base + '/advance', json={}).status_code == 422
        assert client.post(base + '/advance', json={'confirm_synthetic_delivery': True}).status_code == 200
        result = client.post(base + '/advance', json={}).json()
        assert result['fulfillment']['status'] == 'completed'
        assert client.get(f'/api/ops/agent/tasks/{tid}').json()['fulfillment']['next_step'] == 4
        assert ops.get_incident('INC-1001')['state'] == 'resolved'
    finally:
        app.dependency_overrides.clear()
