"""A4: durable approval-gated tasks and read-only investigation boundary."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ops import api
from app.ops.action_tasks import TaskStore, ActionCoordinator
from app.ops.service import StateConflict
from app.ops.store import OpsStore
from app.ops.investigation_agent import investigate_stream


@pytest.fixture
def domain(tmp_path):
    ops = OpsStore(tmp_path / 'operations.sqlite')
    ops.init()
    tasks = TaskStore(tmp_path / 'action_tasks.sqlite')
    tasks.init()
    return ops, tasks, ActionCoordinator(ops, tasks)


def rowcount(ops, table):
    assert table in ('command_receipts', 'audit_log', 'orders', 'inventory')
    with ops._connection() as con:
        return con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def test_proposal_is_durable_and_requires_approval(domain):
    ops, tasks, actor = domain
    created = actor.propose('INC-1001')
    assert created['status'] == 'pending_approval'
    assert created['warehouse_id'] == 'WH-WEST'
    assert created['proposal']['quote']['synthetic'] is True
    assert 'NOT dispatch' in created['proposal']['scope']
    assert tasks.get(created['task_id'])['status'] == 'pending_approval'
    assert rowcount(ops, 'command_receipts') == 0
    with pytest.raises(StateConflict):
        actor.run(created['task_id'])


def test_review_approve_then_execute_independent_verification(domain):
    ops, tasks, actor = domain
    created = actor.propose('INC-1001')
    task_id = created['task_id']
    assert tasks.review(task_id, True)['status'] == 'approved'
    result = actor.run(task_id)
    assert result['status'] == 'succeeded'
    assert result['result']['verified'] is True
    assert result['result']['actual_order_status'] == 'reserved'
    assert 'NOT resolved' in result['result']['scope']
    assert ops.get_incident('INC-1001')['state'] == 'investigating'
    assert rowcount(ops, 'command_receipts') == rowcount(ops, 'audit_log') == 1
    assert [x['kind'] for x in result['events']] == [
        'proposal_created', 'approved', 'execution_claimed', 'succeeded']


def test_repeated_approval_is_noop(domain):
    _, tasks, actor = domain
    tid = actor.propose('INC-1001')['task_id']
    tasks.review(tid, True)
    tasks.review(tid, True)
    assert [e['kind'] for e in tasks.get(tid)['events']].count('approved') == 1


def test_reject_and_no_business_mutation(domain):
    ops, tasks, actor = domain
    tid = actor.propose('INC-1001')['task_id']
    assert tasks.review(tid, False)['status'] == 'rejected'
    with pytest.raises(StateConflict):
        actor.run(tid)
    with pytest.raises(StateConflict):
        tasks.review(tid, True)
    assert rowcount(ops, 'command_receipts') == 0


def test_quote_stale_blocks_writes(domain):
    ops, tasks, actor = domain
    tid = actor.propose('INC-1001')['task_id']
    tasks.review(tid, True)
    with ops.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=8 WHERE warehouse_id='WH-WEST' AND sku_id='SKU-A'")
    result = actor.run(tid)
    assert result['status'] == 'stale'
    assert result['failure_code'] == 'StateConflict'
    assert rowcount(ops, 'command_receipts') == 0
    assert ops.get_incident('INC-1001')['order_status'] == 'pending'


def test_no_feasible_warehouse_task_is_blocked(domain):
    ops, tasks, actor = domain
    result = actor.propose('INC-1003')
    assert result['status'] == 'blocked'
    assert result['warehouse_id'] is None
    with pytest.raises(StateConflict):
        tasks.review(result['task_id'], True)
    assert rowcount(ops, 'command_receipts') == 0


def test_unknown_incident_refused(domain):
    _, _, actor = domain
    from app.ops.service import NotFound
    with pytest.raises(NotFound):
        actor.propose('INC-9999')


def test_restart_task_store_persists_journal(domain):
    ops, tasks, actor = domain
    tid = actor.propose('INC-1001')['task_id']
    tasks.review(tid, True)
    TaskStore(tasks.path).init()
    reopened = TaskStore(tasks.path)
    assert reopened.get(tid)['status'] == 'approved'
    assert len(reopened.get(tid)['events']) == 2
    assert ActionCoordinator(ops, reopened).run(tid)['status'] == 'succeeded'


def test_crash_after_business_commit_recovers_without_double_write(domain, monkeypatch):
    ops, tasks, actor = domain
    tid = actor.propose('INC-1001')['task_id']
    tasks.review(tid, True)
    original = tasks.finish
    def fail_once(*args, **kwargs):
        raise RuntimeError('simulated crash after durable business commit')
    monkeypatch.setattr(tasks, 'finish', fail_once)
    with pytest.raises(RuntimeError):
        actor.run(tid)
    assert tasks.get(tid)['status'] == 'executing'
    assert rowcount(ops, 'command_receipts') == 1
    monkeypatch.setattr(tasks, 'finish', original)
    recovered = ActionCoordinator(ops, TaskStore(tasks.path)).run(tid, recover=True)
    assert recovered['status'] == 'succeeded'
    assert recovered['result']['command_replayed'] is True
    assert rowcount(ops, 'command_receipts') == rowcount(ops, 'audit_log') == 1


def test_cannot_recover_approval_or_terminal_task(domain):
    _, tasks, actor = domain
    tid = actor.propose('INC-1001')['task_id']
    with pytest.raises(StateConflict):
        actor.run(tid, recover=True)
    tasks.review(tid, True)
    actor.run(tid)
    with pytest.raises(StateConflict):
        actor.run(tid, recover=True)


def test_two_requests_cannot_claim_same_task(domain):
    ops, tasks, actor = domain
    tid = actor.propose('INC-1001')['task_id']
    tasks.review(tid, True)
    def run(_):
        try:
            return ActionCoordinator(OpsStore(ops.path), TaskStore(tasks.path)).run(tid)['status']
        except StateConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(run, range(2)))
    assert results == ['conflict', 'succeeded']
    assert rowcount(ops, 'command_receipts') == 1


def test_list_limit_and_task_id_validation(domain):
    _, tasks, actor = domain
    actor.propose('INC-1001')
    assert len(tasks.list()) == 1
    with pytest.raises(Exception):
        tasks.list(0)
    with pytest.raises(Exception):
        tasks.get('../etc/passwd')


def test_api_requires_token_and_does_not_modify_business_without_review(domain, monkeypatch):
    ops, tasks, actor = domain
    monkeypatch.setattr(api, 'require_write_token', api.require_write_token)
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    app.dependency_overrides[api.require_write_token] = lambda: None
    try:
        client = TestClient(app)
        created = client.post('/api/ops/agent/tasks', json={'incident_id': 'INC-1001'})
        assert created.status_code == 200
        tid = created.json()['task_id']
        assert client.get('/api/ops/agent/tasks').json()['items'][0]['task_id'] == tid
        assert client.post(f'/api/ops/agent/tasks/{tid}/execute').status_code == 409
        assert client.post(f'/api/ops/agent/tasks/{tid}/review', json={'approved': True}).status_code == 200
        assert client.post(f'/api/ops/agent/tasks/{tid}/execute').json()['result']['verified'] is True
    finally:
        app.dependency_overrides.clear()


def test_api_write_gate_default_denied(domain, monkeypatch):
    ops, tasks, _ = domain
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    try:
        client = TestClient(app)
        assert client.post('/api/ops/agent/tasks', json={'incident_id': 'INC-1001'}).status_code == 403
        assert rowcount(ops, 'command_receipts') == 0
    finally:
        app.dependency_overrides.clear()


def test_investigation_without_model_never_fakes_results(domain, monkeypatch):
    from app.ops import investigation_agent
    monkeypatch.setattr(investigation_agent, 'is_ready', lambda: False)
    ops, _, _ = domain
    assert [kind for kind, _ in investigate_stream(ops, 'INC-1001')] == ['error']
    assert [kind for kind, _ in investigate_stream(ops, 'INC-UNKNOWN')] == ['error']
    assert rowcount(ops, 'command_receipts') == 0


def test_business_schema_stays_v2(domain):
    ops, tasks, _ = domain
    with ops._connection() as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 2
    with tasks.transaction() as con:
        assert con.execute('SELECT COUNT(*) FROM agent_task_events').fetchone()[0] == 0
