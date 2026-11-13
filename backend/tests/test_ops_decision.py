"""A8: verified choice records, operator alternatives and safe execution."""
from __future__ import annotations

import pytest
import json
from fastapi.testclient import TestClient

from app.main import app
from app.ops import api
from app.ops.action_tasks import ActionCoordinator, TaskStore
from app.ops.decision import decision_packet
from app.ops.evidence import capture_evidence
from app.ops.service import RuleViolation, StateConflict
from app.ops.store import OpsStore


@pytest.fixture
def domain(tmp_path):
    ops = OpsStore(tmp_path / 'ops.sqlite')
    ops.init()
    # Stabilize quote ranking across wall clocks: both warehouses are on time,
    # so the lower fictional cost warehouse is the policy default.
    with ops.transaction() as con:
        con.execute("UPDATE orders SET promised_at='2030-01-01T00:00:00+00:00' WHERE order_id='ORD-1002'")
    tasks = TaskStore(tmp_path / 'tasks.sqlite')
    tasks.init()
    return ops, tasks


def count(ops, table):
    assert table in ('command_receipts', 'audit_log')
    with ops._connection() as con:
        return con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def test_decision_packet_is_reproducible_and_no_side_effects(domain):
    ops, tasks = domain
    a = ActionCoordinator(ops, tasks).propose('INC-1002')
    proposal = a['proposal']
    decision = proposal['decision']
    assert decision['selected_warehouse_id'] == 'WH-WEST'
    assert decision['selection_source'] == 'deterministic_policy'
    assert decision['evidence_sha256'] == proposal['evidence_sha256']
    assert decision['quote_fingerprint'] == proposal['quote']['snapshot_fingerprint']
    assert len(decision['decision_sha256']) == 64
    recreated = decision_packet(proposal['quote'], proposal['evidence_sha256'])
    assert recreated == decision
    assert count(ops, 'command_receipts') == 0
    assert a['events'][0]['detail']['decision_sha256'] == decision['decision_sha256']


def test_operator_can_choose_eligible_alternative_and_execute(domain):
    ops, tasks = domain
    actor = ActionCoordinator(ops, tasks)
    a = actor.propose('INC-1002', warehouse_id='WH-EAST',
                      selection_reason='Operator prioritizes shorter simulated handling time')
    d = a['proposal']['decision']
    assert d['recommended_warehouse_id'] == 'WH-WEST'
    assert d['selected_warehouse_id'] == a['warehouse_id'] == 'WH-EAST'
    assert d['selection_source'] == 'operator_override'
    assert 'handling' in d['operator_reason']
    assert tasks.review(a['task_id'], True)['status'] == 'approved'
    done = actor.run(a['task_id'])
    assert done['status'] == 'succeeded'
    assert done['result']['verified'] is True
    assert done['result']['decision_sha256'] == d['decision_sha256']
    assert done['result']['evidence_sha256'] == a['proposal']['evidence_sha256']
    assert done['result']['selection_source'] == 'operator_override'
    assert done['result']['warehouse_id'] == 'WH-EAST'
    assert count(ops, 'command_receipts') == 1
    assert count(ops, 'audit_log') == 1


def test_unavailable_or_unknown_warehouse_rejected_without_task(domain):
    ops, tasks = domain
    actor = ActionCoordinator(ops, tasks)
    for candidate in ('WH-EAST', 'WH-UNKNOWN'):
        with pytest.raises(RuleViolation):
            actor.propose('INC-1001', warehouse_id=candidate,
                          selection_reason='Operator request to change fulfillment warehouse')
    assert tasks.list() == []
    assert count(ops, 'command_receipts') == 0


def test_override_requires_meaningful_reason_and_rejects_spurious_reason(domain):
    ops, tasks = domain
    actor = ActionCoordinator(ops, tasks)
    for reason in ('', 'short', '  ', 'x' * 301):
        with pytest.raises(RuleViolation):
            actor.propose('INC-1002', warehouse_id='WH-EAST', selection_reason=reason)
    with pytest.raises(RuleViolation):
        actor.propose('INC-1002', warehouse_id='WH-WEST',
                      selection_reason='Reason provided without changing default')
    assert tasks.list() == []


def test_blocked_case_remains_blocked(domain):
    ops, tasks = domain
    a = ActionCoordinator(ops, tasks).propose('INC-1003')
    assert a['status'] == 'blocked'
    assert a['proposal']['decision']['requires_approval'] is False
    assert a['proposal']['decision']['selected_warehouse_id'] is None
    with pytest.raises(RuleViolation):
        ActionCoordinator(ops, tasks).propose('INC-1003', warehouse_id='WH-EAST',
                                               selection_reason='Trying to bypass insufficient stock')


def test_stale_override_is_rejected_before_business_write(domain):
    ops, tasks = domain
    actor = ActionCoordinator(ops, tasks)
    a = actor.propose('INC-1002', warehouse_id='WH-EAST',
                      selection_reason='Operator chooses the faster simulated handling warehouse')
    tasks.review(a['task_id'], True)
    with ops.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=on_hand-1 WHERE warehouse_id='WH-EAST' AND sku_id='SKU-B'")
    ended = actor.run(a['task_id'])
    assert ended['status'] == 'stale'
    assert count(ops, 'command_receipts') == 0
    assert ops.get_incident('INC-1002')['order_status'] == 'pending'


def test_preview_and_operator_post_are_guarded(domain, monkeypatch):
    ops, tasks = domain
    app.dependency_overrides[api.get_store] = lambda: ops
    app.dependency_overrides[api.get_task_store] = lambda: tasks
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    try:
        client = TestClient(app)
        preview = client.get('/api/ops/agent/incidents/INC-1002/decision-preview')
        assert preview.status_code == 200, preview.text
        assert preview.json()['selection_source'] == 'deterministic_policy'
        assert client.get('/api/ops/agent/incidents/INC-9999/decision-preview').status_code == 404
        assert client.post('/api/ops/agent/tasks', json={'incident_id':'INC-1002',
                          'warehouse_id':'WH-EAST','selection_reason':'Operator selects faster simulated warehouse'}).status_code == 403
        app.dependency_overrides[api.require_write_token] = lambda: None
        bad = client.post('/api/ops/agent/tasks', json={'incident_id':'INC-1002',
                          'warehouse_id':'WH-EAST','selection_reason':'short'})
        assert bad.status_code == 422
        good = client.post('/api/ops/agent/tasks', json={'incident_id':'INC-1002',
                          'warehouse_id':'WH-EAST',
                          'selection_reason':'Operator selects faster simulated warehouse'})
        assert good.status_code == 200, good.text
        assert good.json()['proposal']['decision']['selection_source'] == 'operator_override'
        assert count(ops,'command_receipts') == 0
    finally:
        app.dependency_overrides.clear()


def test_no_model_provenance_as_authorization(domain):
    ops, tasks = domain
    c = capture_evidence(ops,'INC-1002')
    inv = tasks.create_investigation('INC-1002',c,model_status='running')
    with pytest.raises(StateConflict):
        ActionCoordinator(ops,tasks).propose('INC-1002', investigation_id=inv['investigation_id'],
          warehouse_id='WH-EAST',selection_reason='Model recommended this but it is not approved')
    assert tasks.list() == []


def test_pre_a8_task_remains_executable_without_fake_provenance(domain):
    ops, tasks = domain
    actor = ActionCoordinator(ops, tasks)
    task = actor.propose('INC-1001')
    tid = task['task_id']
    # Emulate a durable A7 record written before the new decision ledger existed.
    with tasks.transaction() as con:
        proposal = task['proposal'].copy()
        proposal.pop('decision')
        con.execute('UPDATE agent_tasks SET proposal_json=? WHERE task_id=?',
                    (json.dumps(proposal), tid))
    tasks.review(tid, True)
    done = actor.run(tid)
    assert done['status'] == 'succeeded'
    assert done['result']['verified'] is True
    assert 'decision_sha256' not in done['result']
    assert count(ops, 'command_receipts') == 1


def test_modified_decision_ledger_is_rejected_without_side_effect(domain):
    ops, tasks = domain
    actor = ActionCoordinator(ops, tasks)
    t = actor.propose('INC-1001')
    tid = t['task_id']
    tasks.review(tid, True)
    with tasks.transaction() as con:
        altered = t['proposal'].copy()
        altered['decision'] = altered['decision'].copy()
        altered['decision']['rationale'] = 'TAMPERED decision'
        con.execute('UPDATE agent_tasks SET proposal_json=? WHERE task_id=?',
                    (json.dumps(altered), tid))
    with pytest.raises(StateConflict, match='Decision ledger'):
        actor.run(tid)
    assert tasks.get(tid)['status'] == 'approved'
    assert count(ops, 'command_receipts') == 0
    assert count(ops, 'audit_log') == 0


def test_decision_and_task_warehouse_mismatch_rejected(domain):
    ops, tasks = domain
    actor = ActionCoordinator(ops, tasks)
    t = actor.propose('INC-1002')
    tid = t['task_id']
    tasks.review(tid, True)
    with tasks.transaction() as con:
        con.execute("UPDATE agent_tasks SET warehouse_id='WH-EAST' WHERE task_id=?", (tid,))
    with pytest.raises(StateConflict, match='Decision ledger'):
        actor.run(tid)
    assert count(ops, 'command_receipts') == 0
