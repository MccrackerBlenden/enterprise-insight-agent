"""A9: untrusted model suggestions cannot bypass deterministic business gates."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ops import api, model_plan
from app.ops.action_tasks import ActionCoordinator, TaskStore
from app.ops.evidence import capture_evidence
from app.ops.service import RuleViolation, StateConflict
from app.ops.store import OpsStore


@pytest.fixture
def domain(tmp_path):
    ops = OpsStore(tmp_path/'ops.sqlite')
    ops.init()
    with ops.transaction() as con:
        con.execute("UPDATE orders SET promised_at='2030-01-01T00:00:00+00:00' WHERE order_id='ORD-1002'")
    tasks = TaskStore(tmp_path/'tasks.sqlite')
    tasks.init()
    return ops, tasks


def investigation(ops, tasks, incident_id='INC-1002'):
    evidence = capture_evidence(ops, incident_id)
    info = tasks.create_investigation(incident_id, evidence, model_status='running')
    tasks.finish_investigation(info['investigation_id'], model_status='completed',
                               summary='Model investigation summary not verified', tool_calls=2)
    return info['investigation_id']


def choice(warehouse_id='WH-WEST', *, action='reserve_inventory'):
    return {'next_action': action, 'warehouse_id': warehouse_id,
            'rationale': 'Current verified synthetic inventory permits this warehouse; request operator review.',
            'risks': ['Costs and times are simulated, not a carrier guarantee']}


def counts(ops):
    with ops._connection() as con:
        return [con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('command_receipts', 'audit_log')]


def test_model_plan_can_be_persisted_reopened_and_is_not_approval(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    seen = []
    plan = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda facts: (
        seen.append(facts['incident']['order_id']) or choice()))
    assert seen == ['ORD-1002']
    assert plan['plan']['warehouse_id'] == 'WH-WEST'
    assert plan['adopted_task_id'] is None
    assert plan['plan']['requires_operator_confirmation'] is True
    assert TaskStore(tasks.path).get_model_plan(plan['plan_id'])['plan'] == plan['plan']
    assert counts(ops) == [0, 0]
    assert tasks.list() == []


def test_explicit_operator_confirms_plan_then_existing_approval_still_required(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    record = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice())
    tid = ActionCoordinator(ops, tasks).propose_from_model_plan(
        record['plan_id'], confirmed_warehouse_id='WH-WEST')['task_id']
    task = tasks.get(tid)
    assert task['status'] == 'pending_approval'
    assert task['proposal']['model_plan_id'] == record['plan_id']
    assert any(event['kind'] == 'model_plan_adopted' for event in task['events'])
    assert counts(ops) == [0, 0]
    assert tasks.get_model_plan(record['plan_id'])['adopted_task_id'] == tid
    tasks.review(tid, True)
    ended = ActionCoordinator(ops, tasks).run(tid)
    assert ended['status'] == 'succeeded' and ended['result']['verified'] is True
    assert counts(ops) == [1, 1]


def test_model_alternative_requires_operator_reason(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    plan = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice('WH-EAST'))
    actor = ActionCoordinator(ops, tasks)
    with pytest.raises(RuleViolation):
        actor.propose_from_model_plan(plan['plan_id'], confirmed_warehouse_id='WH-EAST')
    with pytest.raises(RuleViolation):
        actor.propose_from_model_plan(plan['plan_id'], confirmed_warehouse_id='WH-WEST',
                                      selection_reason='Operator changed their mind')
    task = actor.propose_from_model_plan(plan['plan_id'], confirmed_warehouse_id='WH-EAST',
            selection_reason='Operator explicitly reviewed and accepted the alternative warehouse')
    assert task['proposal']['decision']['selection_source'] == 'operator_override'
    assert task['status'] == 'pending_approval'
    assert counts(ops) == [0, 0]


def test_same_plan_cannot_create_two_tasks(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    plan = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice())
    actor = ActionCoordinator(ops, tasks)
    actor.propose_from_model_plan(plan['plan_id'], confirmed_warehouse_id='WH-WEST')
    with pytest.raises(StateConflict, match='adopted'):
        actor.propose_from_model_plan(plan['plan_id'], confirmed_warehouse_id='WH-WEST')
    assert len(tasks.list()) == 1 and counts(ops) == [0, 0]


@pytest.mark.parametrize('bad', [
    choice('WH-NOPE'), choice('WH-EAST', action='escalate_human'),
    choice(None, action='reserve_inventory'),
    {**choice(), 'unsafe_command': 'delete_all_orders'},
    {**choice(), 'risks': 'none'},
    {**choice(), 'rationale': 'short'},
    'no json',
])
def test_invalid_model_proposals_do_not_create_tasks_or_plans(domain, bad):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    with pytest.raises(RuleViolation):
        model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: bad)
    assert tasks.list() == []
    assert counts(ops) == [0, 0]
    with tasks.transaction() as con:
        assert con.execute('SELECT COUNT(*) FROM agent_model_plans').fetchone()[0] == 0


def test_blocked_quote_must_escalate_and_cannot_be_adopted_as_reservation(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks, 'INC-1003')
    with pytest.raises(RuleViolation):
        model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice('WH-WEST'))
    plan = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice(None, action='escalate_human'))
    assert plan['plan']['next_action'] == 'escalate_human'
    with pytest.raises(RuleViolation):
        ActionCoordinator(ops, tasks).propose_from_model_plan(plan['plan_id'], confirmed_warehouse_id='WH-EAST')
    assert counts(ops) == [0, 0]


def test_stale_investigation_before_model_does_not_call_model(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    with ops.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=on_hand+1 WHERE warehouse_id='WH-WEST' AND sku_id='SKU-B'")
    def fail(_):
        raise AssertionError('Model should not be called')
    with pytest.raises(StateConflict):
        model_plan.generate_model_plan(ops, tasks, inv, model_call=fail)


def test_business_change_during_model_call_rejects_plan(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    def moving(_):
        with ops.transaction() as con:
            con.execute("UPDATE inventory SET on_hand=on_hand+1 WHERE warehouse_id='WH-WEST' AND sku_id='SKU-B'")
        return choice()
    with pytest.raises(StateConflict, match='during'):
        model_plan.generate_model_plan(ops, tasks, inv, model_call=moving)
    assert counts(ops) == [0, 0]


def test_business_change_after_proposal_blocks_operator_adoption(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    plan = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice())
    with ops.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=on_hand+1 WHERE warehouse_id='WH-WEST' AND sku_id='SKU-B'")
    with pytest.raises(StateConflict, match='stale'):
        ActionCoordinator(ops, tasks).propose_from_model_plan(plan['plan_id'], confirmed_warehouse_id='WH-WEST')
    assert tasks.list() == [] and counts(ops) == [0, 0]


def test_tampered_model_plan_rejected_before_business_execution(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    record = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice())
    task = ActionCoordinator(ops, tasks).propose_from_model_plan(
        record['plan_id'], confirmed_warehouse_id='WH-WEST')
    tasks.review(task['task_id'], True)
    with tasks.transaction() as con:
        tampered = record['plan'].copy()
        tampered['warehouse_id'] = 'WH-EAST'
        con.execute('UPDATE agent_model_plans SET plan_json=? WHERE plan_id=?',
                    (json.dumps(tampered), record['plan_id']))
    with pytest.raises(StateConflict):
        ActionCoordinator(ops, tasks).run(task['task_id'])
    assert counts(ops) == [0, 0]


def test_public_api_requires_token_for_paid_model_and_task_creation(domain, monkeypatch):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    called = []
    def fake_run(o, t, investigation_id):
        called.append(investigation_id)
        return model_plan.generate_model_plan(o, t, investigation_id, model_call=lambda _: choice())
    monkeypatch.setattr(api, 'generate_model_plan', fake_run)
    try:
        client = TestClient(app)
        url = '/api/ops/agent/investigations/' + inv + '/model-plan'
        assert client.post(url).status_code == 403
        assert called == []
        app.dependency_overrides[api.require_write_token] = lambda: None
        res = client.post(url)
        assert res.status_code == 200, res.text
        plan_id = res.json()['plan_id']
        assert client.get('/api/ops/agent/model-plans/' + plan_id).status_code == 200
        adopted = client.post('/api/ops/agent/model-plans/' + plan_id + '/create-task',
                              json={'confirmed_warehouse_id': 'WH-WEST'})
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()['status'] == 'pending_approval'
        assert counts(ops) == [0, 0]
    finally:
        app.dependency_overrides.clear()


def test_pre_a9_deterministic_tasks_still_work(domain):
    ops, tasks = domain
    task = ActionCoordinator(ops, tasks).propose('INC-1001')
    assert 'model_plan_id' not in task['proposal']
    tasks.review(task['task_id'], True)
    assert ActionCoordinator(ops, tasks).run(task['task_id'])['status'] == 'succeeded'


def test_quote_bucket_expiry_can_revalidate_without_paid_model_or_ops_writes(domain, monkeypatch):
    from datetime import datetime, timezone
    from app.ops import planning
    ops, tasks = domain
    monkeypatch.setattr(planning, '_clock', lambda at: datetime(2026, 11, 12, 1, 4, tzinfo=timezone.utc))
    inv = investigation(ops, tasks)
    old = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice())
    before = counts(ops)
    monkeypatch.setattr(planning, '_clock', lambda at: datetime(2026, 11, 12, 1, 6, tzinfo=timezone.utc))
    actor = ActionCoordinator(ops, tasks)
    with pytest.raises(StateConflict, match='stale'):
        actor.propose_from_model_plan(old['plan_id'], confirmed_warehouse_id='WH-WEST')
    fresh = actor.refresh_model_plan(old['plan_id'])
    assert fresh['plan_id'] != old['plan_id']
    assert fresh['plan']['revalidated_from_plan_id'] == old['plan_id']
    assert fresh['plan']['model_reused_without_new_call'] is True
    assert fresh['plan']['quote_fingerprint'] != old['plan']['quote_fingerprint']
    assert tasks.get_investigation(fresh['investigation_id'])['model_status'] == 'not_run'
    assert tasks.get_model_plan(old['plan_id'])['adopted_task_id'] is None
    assert tasks.latest_unadopted_model_plan('INC-1002')['plan_id'] == fresh['plan_id']
    assert counts(ops) == before
    assert actor.refresh_model_plan(fresh['plan_id'])['plan_id'] == fresh['plan_id']
    assert actor.refresh_model_plan(old['plan_id'])['plan_id'] == fresh['plan_id']
    task = actor.propose_from_model_plan(fresh['plan_id'], confirmed_warehouse_id='WH-WEST')
    assert task['status'] == 'pending_approval'
    assert counts(ops) == before
    assert tasks.latest_unadopted_model_plan('INC-1002')['plan_id'] == old['plan_id']
    with pytest.raises(StateConflict, match='already adopted'):
        actor.refresh_model_plan(fresh['plan_id'])


def test_free_refresh_refuses_changed_inventory_and_preserves_ledgers(domain, monkeypatch):
    from datetime import datetime, timezone
    from app.ops import planning
    ops, tasks = domain
    monkeypatch.setattr(planning, '_clock', lambda at: datetime(2026, 11, 12, 1, 4, tzinfo=timezone.utc))
    inv = investigation(ops, tasks)
    old = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice())
    monkeypatch.setattr(planning, '_clock', lambda at: datetime(2026, 11, 12, 1, 6, tzinfo=timezone.utc))
    with ops.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=on_hand+1 WHERE warehouse_id='WH-WEST' AND sku_id='SKU-B'")
    with pytest.raises(StateConflict, match='Business facts'):
        ActionCoordinator(ops, tasks).refresh_model_plan(old['plan_id'])
    assert counts(ops) == [0, 0]
    assert tasks.list() == []
    with tasks.transaction() as con:
        assert con.execute('SELECT COUNT(*) FROM agent_model_plans').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM agent_investigations').fetchone()[0] == 1


def test_free_refresh_http_requires_demo_token(domain, monkeypatch):
    from datetime import datetime, timezone
    from app.ops import planning
    ops, tasks = domain
    monkeypatch.setattr(planning, '_clock', lambda at: datetime(2026, 11, 12, 1, 4, tzinfo=timezone.utc))
    inv = investigation(ops, tasks)
    old = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice())
    monkeypatch.setattr(planning, '_clock', lambda at: datetime(2026, 11, 12, 1, 6, tzinfo=timezone.utc))
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    try:
        client = TestClient(app)
        url = f"/api/ops/agent/model-plans/{old['plan_id']}/refresh"
        assert client.post(url).status_code == 403
        app.dependency_overrides[api.require_write_token] = lambda: None
        response = client.post(url)
        assert response.status_code == 200, response.text
        assert response.json()['plan']['model_reused_without_new_call'] is True
        assert counts(ops) == [0, 0]
    finally:
        app.dependency_overrides.clear()


def test_restore_saved_plan_after_ui_reload_is_read_only(domain):
    ops, tasks = domain
    inv = investigation(ops, tasks)
    old = model_plan.generate_model_plan(ops, tasks, inv, model_call=lambda _: choice())
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    try:
        client = TestClient(app)
        r = client.get('/api/ops/agent/incidents/INC-1002/latest-model-plan')
        assert r.status_code == 200
        assert r.json()['item']['plan_id'] == old['plan_id']
        assert client.get('/api/ops/agent/incidents/INC-1001/latest-model-plan').json()['item'] is None
        assert counts(ops) == [0, 0]
    finally:
        app.dependency_overrides.clear()
