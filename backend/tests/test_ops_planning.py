"""A3: real DB feasibility, explicit fictional scoring, atomic conditional execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ops.api import get_store
from app.ops.planning import POLICY_VERSION, quote_order
from app.ops.service import NotFound, OpsService, RuleViolation, StateConflict
from app.ops.store import OpsStore

UTC = timezone.utc
AT = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    store = OpsStore(tmp_path / 'operations.sqlite')
    store.init()
    return store


def quote(db, order='ORD-1001', at=AT):
    with db._connection() as con:
        return quote_order(con, order, at=at)


def count(db, table):
    assert table in ('command_receipts', 'audit_log', 'detection_signals', 'detection_events')
    with db._connection() as con:
        return con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def test_quote_is_readonly_and_includes_full_comparison(db):
    result = quote(db)
    assert result['policy_version'] == POLICY_VERSION
    assert result['synthetic'] is True
    assert result['recommended_warehouse_id'] == 'WH-WEST'
    assert len(result['options']) == 2
    east = next(o for o in result['options'] if o['warehouse_id'] == 'WH-EAST')
    west = next(o for o in result['options'] if o['warehouse_id'] == 'WH-WEST')
    assert east['blockers'] == ['insufficient_stock']
    assert west['eligible'] is True and west['available'] == 10
    assert west['simulated_cost_points'] == 9
    assert count(db, 'command_receipts') == count(db, 'audit_log') == 0


def test_quote_does_not_treat_split_warehouse_stock_as_fulfillable(db):
    result = quote(db, 'ORD-1003')
    assert result['recommended_warehouse_id'] is None
    assert all(not o['eligible'] for o in result['options'])
    assert all('insufficient_stock' in o['blockers'] for o in result['options'])


def test_quote_missing_order(db):
    with pytest.raises(NotFound):
        quote(db, 'ORD-UNKNOWN')


def test_quote_sort_prefers_low_lateness_before_cost(db):
    with db.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=20 WHERE warehouse_id='WH-EAST' AND sku_id='SKU-A'")
    result = quote(db, at=AT + timedelta(hours=20))
    assert all(o['eligible'] for o in result['options'])
    assert result['recommended_warehouse_id'] == 'WH-EAST'
    assert result['options'][0]['simulated_handling_hours'] == 8
    assert result['options'][1]['simulated_cost_points'] == 9


def test_quote_respects_simulated_cost_when_lateness_equal(db):
    with db.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=20 WHERE warehouse_id='WH-EAST' AND sku_id='SKU-A'")
    result = quote(db)
    assert result['recommended_warehouse_id'] == 'WH-WEST'  # both on time: cost wins


def test_quote_blocks_inactive_warehouse(db):
    with db.transaction() as con:
        con.execute("UPDATE warehouses SET active=0 WHERE warehouse_id='WH-WEST'")
    result = quote(db)
    assert result['recommended_warehouse_id'] is None
    west = next(o for o in result['options'] if o['warehouse_id'] == 'WH-WEST')
    assert 'warehouse_inactive' in west['blockers']


def test_quote_blocks_escalated_incident(db):
    OpsService(db).escalate('INC-1001', 'Human hold pending review', 'a3-escalate-test01')
    result = quote(db)
    assert result['recommended_warehouse_id'] is None
    assert all('incident_requires_reassessment' in o['blockers'] for o in result['options'])


def test_quote_blocks_order_already_reserved(db):
    OpsService(db).reserve('ORD-1001', 'WH-WEST', 'a3-old-reserve01')
    result = db.fulfillment_quote('ORD-1001')
    assert all('order_not_pending' in o['blockers'] for o in result['options'])
    assert result['recommended_warehouse_id'] is None


def test_timestamp_timezone_validation(db):
    with pytest.raises(ValueError):
        quote(db, at=datetime(2026, 11, 12))
    result = quote(db, at=AT.astimezone(timezone(timedelta(hours=8))))
    assert result['observed_at'] == AT.isoformat()


def test_fingerprint_deterministic_in_time_bucket(db):
    a = quote(db, at=AT)['snapshot_fingerprint']
    b = quote(db, at=AT + timedelta(seconds=200))['snapshot_fingerprint']
    c = quote(db, at=AT + timedelta(seconds=301))['snapshot_fingerprint']
    assert a == b and a != c


def test_quote_precondition_executes_and_audits(db):
    current = db.fulfillment_quote('ORD-1001')
    out = OpsService(db).reserve('ORD-1001', 'WH-WEST', 'a3-confirm-001',
                                 expected_snapshot=current['snapshot_fingerprint'])
    assert out['status'] == 'reserved' and out['replayed'] is False
    assert db.get_incident('INC-1001')['order_status'] == 'reserved'
    assert count(db, 'command_receipts') == count(db, 'audit_log') == 1


def test_quote_precondition_replay_survives_changed_state(db):
    current = db.fulfillment_quote('ORD-1001')
    svc = OpsService(db)
    first = svc.reserve('ORD-1001', 'WH-WEST', 'a3-repeat-001',
                        expected_snapshot=current['snapshot_fingerprint'])
    replay = OpsService(OpsStore(db.path)).reserve('ORD-1001', 'WH-WEST', 'a3-repeat-001',
                 expected_snapshot=current['snapshot_fingerprint'])
    assert first['replayed'] is False and replay['replayed'] is True
    assert count(db, 'command_receipts') == 1


def test_stale_quote_rejected_atomically(db):
    current = db.fulfillment_quote('ORD-1001')
    with db.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=9 WHERE warehouse_id='WH-WEST' AND sku_id='SKU-A'")
    with pytest.raises(StateConflict, match='Plan expired|state changed'):
        OpsService(db).reserve('ORD-1001', 'WH-WEST', 'a3-stale-001',
                                expected_snapshot=current['snapshot_fingerprint'])
    assert db.get_incident('INC-1001')['order_status'] == 'pending'
    assert count(db, 'command_receipts') == 0
    assert count(db, 'audit_log') == 0


def test_fingerprint_catches_incident_escalation(db):
    current = db.fulfillment_quote('ORD-1001')
    OpsService(db).escalate('INC-1001', 'Human escalation needed', 'a3-hold-001')
    with pytest.raises(StateConflict):
        OpsService(db).reserve('ORD-1001', 'WH-WEST', 'a3-hold-reserve01',
                                expected_snapshot=current['snapshot_fingerprint'])
    assert db.get_incident('INC-1001')['order_status'] == 'pending'


def test_fingerprint_catches_warehouse_deactivation(db):
    current = db.fulfillment_quote('ORD-1001')
    with db.transaction() as con:
        con.execute("UPDATE warehouses SET active=0 WHERE warehouse_id='WH-WEST'")
    with pytest.raises(StateConflict):
        OpsService(db).reserve('ORD-1001', 'WH-WEST', 'a3-inactive-001',
                               expected_snapshot=current['snapshot_fingerprint'])
    assert count(db, 'command_receipts') == 0


def test_cannot_select_unavailable_option(db):
    current = db.fulfillment_quote('ORD-1001')
    with pytest.raises(StateConflict, match='no longer executable'):
        OpsService(db).reserve('ORD-1001', 'WH-EAST', 'a3-blocked-001',
                               expected_snapshot=current['snapshot_fingerprint'])
    assert count(db, 'command_receipts') == 0


def test_invalid_quote_fingerprint_rejected(db):
    with pytest.raises(RuleViolation):
        OpsService(db).reserve('ORD-1001', 'WH-WEST', 'a3-bad-001',
                               expected_snapshot='foo')


def test_existing_reserve_api_remains_compatible(db):
    result = OpsService(db).reserve('ORD-1001', 'WH-WEST', 'a3-legacy-001')
    assert result['status'] == 'reserved'


def test_idempotency_key_collision_cannot_upgrade_legacy_command(db):
    svc = OpsService(db)
    svc.reserve('ORD-1001', 'WH-WEST', 'a3-collision01')
    with pytest.raises(StateConflict):
        svc.reserve('ORD-1001', 'WH-WEST', 'a3-collision01', expected_snapshot='0'*64)


def test_concurrent_quote_execution_one_commit(db):
    fp = db.fulfillment_quote('ORD-1001')['snapshot_fingerprint']
    def run(key):
        try:
            OpsService(OpsStore(db.path)).reserve('ORD-1001', 'WH-WEST', key,
                           expected_snapshot=fp)
            return 'ok'
        except StateConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = sorted(ex.map(run, ('a3-concur-one', 'a3-concur-two')))
    assert results == ['conflict', 'ok']
    assert count(db, 'command_receipts') == 1
    assert db.get_incident('INC-1001')['order_status'] == 'reserved'


def test_api_read_and_default_write_denial(db, monkeypatch):
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    app.dependency_overrides[get_store] = lambda: db
    try:
        with TestClient(app) as client:
            quote_result = client.get('/api/ops/orders/ORD-1001/fulfillment-options')
            assert quote_result.status_code == 200
            payload = {'order_id': 'ORD-1001', 'warehouse_id': 'WH-WEST',
                       'snapshot_fingerprint': quote_result.json()['snapshot_fingerprint']}
            out = client.post('/api/ops/commands/reserve-from-plan', json=payload,
                              headers={'X-Idempotency-Key': 'a3-api-block-01'})
            assert out.status_code == 403
            assert client.get('/api/ops/orders/UNKNOWN/fulfillment-options').status_code == 404
    finally:
        app.dependency_overrides.clear()
    assert count(db, 'command_receipts') == 0


def test_api_authorized_quote_reserve_and_replay(db, monkeypatch):
    token = 'a3-local-only-very-long-demo-token'
    monkeypatch.setenv('EIA_OPS_WRITE_TOKEN', token)
    app.dependency_overrides[get_store] = lambda: db
    try:
        with TestClient(app) as client:
            q = client.get('/api/ops/orders/ORD-1001/fulfillment-options').json()
            body = {'order_id': 'ORD-1001', 'warehouse_id': 'WH-WEST',
                    'snapshot_fingerprint': q['snapshot_fingerprint']}
            hdr = {'X-Ops-Demo-Token': token, 'X-Idempotency-Key': 'a3-api-exec-01'}
            first = client.post('/api/ops/commands/reserve-from-plan', json=body, headers=hdr)
            assert first.status_code == 200, first.text
            again = client.post('/api/ops/commands/reserve-from-plan', json=body, headers=hdr)
            assert again.status_code == 200 and again.json()['replayed'] is True
            invalid = client.post('/api/ops/commands/reserve-from-plan', json={**body, 'extra': 'x'}, headers=hdr)
            assert invalid.status_code == 422
    finally:
        app.dependency_overrides.clear()
    assert count(db, 'command_receipts') == 1
