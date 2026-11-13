"""Offline domain + API regression: no model calls, no destructive resets."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ops.api import get_store
from app.ops.service import OpsService, NotFound, RuleViolation, StateConflict
from app.ops.store import OpsStore


@pytest.fixture
def domain(tmp_path):
    db = OpsStore(tmp_path / 'ops.sqlite')
    db.init()
    return db, OpsService(db)


def key(n: int):
    return f'op-test-{n:04d}'


def sql(store, query, *args):
    with store._connection() as con:
        return con.execute(query, args).fetchone()[0]


def test_seed_is_deterministic_and_independent(domain):
    db, _ = domain
    assert db.summary()['orders'] == 3
    assert len(db.list_incidents()) == 3
    assert db.get_incident('INC-1001')['inventory'][0]['available'] == 2
    assert db.get_incident('INC-1003')['quantity'] == 20
    assert db.path.name == 'ops.sqlite'


def test_new_store_never_resets_business_changes(domain):
    db, svc = domain
    svc.reserve('ORD-1001', 'WH-WEST', key(1))
    OpsStore(db.path).init()
    assert db.get_incident('INC-1001')['order_status'] == 'reserved'
    assert sql(db, 'SELECT COUNT(*) FROM command_receipts') == 1


def test_reservation_fails_on_insufficient_stock_and_is_atomic(domain):
    db, svc = domain
    with pytest.raises(RuleViolation):
        svc.reserve('ORD-1001', 'WH-EAST', key(1))
    assert db.get_incident('INC-1001')['order_status'] == 'pending'
    assert sql(db, 'SELECT reserved FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-EAST', 'SKU-A') == 0
    assert sql(db, 'SELECT COUNT(*) FROM audit_log') == 0


def test_reservation_updates_order_inventory_and_incident(domain):
    db, svc = domain
    result = svc.reserve('ORD-1001', 'WH-WEST', key(1))
    assert result['status'] == 'reserved'
    assert db.get_incident('INC-1001')['state'] == 'investigating'
    assert sql(db, 'SELECT reserved FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-WEST', 'SKU-A') == 3


def test_same_request_is_idempotent_even_after_reopen(domain):
    db, svc = domain
    first = svc.reserve('ORD-1001', 'WH-WEST', key(1))
    second = OpsService(OpsStore(db.path)).reserve('ORD-1001', 'WH-WEST', key(1))
    assert first['replayed'] is False and second['replayed'] is True
    assert sql(db, 'SELECT COUNT(*) FROM audit_log') == 1
    assert sql(db, 'SELECT reserved FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-WEST', 'SKU-A') == 3


def test_duplicate_idempotency_key_with_different_args_is_rejected(domain):
    _, svc = domain
    svc.reserve('ORD-1001', 'WH-WEST', key(1))
    with pytest.raises(StateConflict):
        svc.reserve('ORD-1002', 'WH-EAST', key(1))


def test_second_command_cannot_reserve_same_order(domain):
    db, svc = domain
    svc.reserve('ORD-1001', 'WH-WEST', key(1))
    with pytest.raises(StateConflict):
        svc.reserve('ORD-1001', 'WH-WEST', key(2))
    assert sql(db, 'SELECT COUNT(*) FROM command_receipts') == 1


def test_release_compensates_reservation_before_dispatch(domain):
    db, svc = domain
    svc.reserve('ORD-1001', 'WH-WEST', key(1))
    result = svc.release('ORD-1001', key(2))
    assert result['status'] == 'pending'
    assert db.get_incident('INC-1001')['state'] == 'open'
    assert sql(db, 'SELECT reserved FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-WEST', 'SKU-A') == 0
    assert sql(db, 'SELECT on_hand FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-WEST', 'SKU-A') == 10


def test_dispatch_is_atomic_and_shipment_is_singleton(domain):
    db, svc = domain
    svc.reserve('ORD-1001', 'WH-WEST', key(1))
    svc.dispatch('ORD-1001', 'sim_carrier', key(2))
    assert sql(db, 'SELECT on_hand FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-WEST', 'SKU-A') == 7
    assert sql(db, 'SELECT reserved FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-WEST', 'SKU-A') == 0
    assert sql(db, 'SELECT COUNT(*) FROM shipments WHERE order_id=?', 'ORD-1001') == 1
    with pytest.raises(StateConflict):
        svc.dispatch('ORD-1001', 'sim_carrier', key(3))
    with pytest.raises(StateConflict):
        svc.release('ORD-1001', key(4))


def test_dispatch_without_reservation_cannot_ship(domain):
    db, svc = domain
    with pytest.raises(StateConflict):
        svc.dispatch('ORD-1001', 'sim_carrier', key(1))
    assert sql(db, 'SELECT COUNT(*) FROM shipments') == 0


def test_shipment_must_progress_in_order(domain):
    db, svc = domain
    svc.reserve('ORD-1001', 'WH-WEST', key(1))
    svc.dispatch('ORD-1001', 'sim_carrier', key(2))
    with pytest.raises(StateConflict):
        svc.advance_shipment('ORD-1001', 'delivered', key(3))
    assert db.get_incident('INC-1001')['shipment_status'] == 'created'
    svc.advance_shipment('ORD-1001', 'in_transit', key(4))
    svc.advance_shipment('ORD-1001', 'delivered', key(5))
    assert db.get_incident('INC-1001')['order_status'] == 'delivered'
    with pytest.raises(StateConflict):
        svc.advance_shipment('ORD-1001', 'in_transit', key(6))


def test_incident_cannot_resolve_without_independent_evidence(domain):
    db, svc = domain
    with pytest.raises(RuleViolation):
        svc.verify_incident('INC-1001', key(1))
    svc.reserve('ORD-1001', 'WH-WEST', key(2))
    svc.dispatch('ORD-1001', 'sim_carrier', key(3))
    with pytest.raises(RuleViolation):
        svc.verify_incident('INC-1001', key(4))
    assert db.get_incident('INC-1001')['state'] == 'investigating'


def test_end_to_end_synthetic_incident_resolution(domain):
    db, svc = domain
    svc.reserve('ORD-1001', 'WH-WEST', key(1))
    svc.dispatch('ORD-1001', 'sim_carrier', key(2))
    svc.advance_shipment('ORD-1001', 'in_transit', key(3))
    svc.advance_shipment('ORD-1001', 'delivered', key(4))
    assert svc.verify_incident('INC-1001', key(5))['verified'] is True
    detail = db.get_incident('INC-1001')
    assert detail['state'] == 'resolved'
    assert detail['shipment_status'] == 'delivered'
    assert [x['operation'] for x in detail['audit']] == [
        'reserve', 'dispatch', 'advance_shipment', 'advance_shipment', 'verify_incident']
    assert db.summary()['incidents']['resolved'] == 1
    with pytest.raises(StateConflict):
        svc.verify_incident('INC-1001', key(6))


def test_escalate_unsatisfiable_incident(domain):
    db, svc = domain
    with pytest.raises(RuleViolation):
        svc.reserve('ORD-1003', 'WH-WEST', key(1))
    assert svc.escalate('INC-1003', 'All warehouses insufficient', key(2))['state'] == 'escalated'
    assert db.get_incident('INC-1003')['state'] == 'escalated'
    with pytest.raises(StateConflict):
        svc.escalate('INC-1003', 'Retry escalation', key(3))


def test_escalation_blocks_automated_fulfillment(domain):
    db, svc = domain
    svc.escalate('INC-1001', 'Hold this order for human reassessment', key(1))
    with pytest.raises(StateConflict):
        svc.reserve('ORD-1001', 'WH-WEST', key(2))
    assert db.get_incident('INC-1001')['order_status'] == 'pending'


def test_unknown_or_inactive_warehouse_rejected(domain):
    db, svc = domain
    with pytest.raises(NotFound):
        svc.reserve('ORD-1001', 'WH-NONE', key(1))
    with db.transaction() as con:
        con.execute("UPDATE warehouses SET active=0 WHERE warehouse_id='WH-WEST'")
    with pytest.raises(RuleViolation):
        svc.reserve('ORD-1001', 'WH-WEST', key(2))


def test_schema_enforces_nonnegative_and_foreign_keys(domain):
    db, _ = domain
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as con:
            con.execute("UPDATE inventory SET on_hand=-1 WHERE warehouse_id='WH-EAST'")
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as con:
            con.execute("INSERT INTO shipments(shipment_id,order_id,carrier) VALUES('S-3','MISSING','x')")


def test_command_rolls_back_if_audit_insert_fails(domain):
    db, svc = domain
    with db.transaction() as con:
        con.execute("CREATE TRIGGER block_audit BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'audit blocked'); END")
    with pytest.raises(sqlite3.IntegrityError):
        svc.reserve('ORD-1001', 'WH-WEST', key(1))
    assert db.get_incident('INC-1001')['order_status'] == 'pending'
    assert sql(db, 'SELECT reserved FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-WEST', 'SKU-A') == 0
    assert sql(db, 'SELECT COUNT(*) FROM command_receipts') == 0


def test_concurrent_reservations_do_not_oversell(domain):
    db, _ = domain
    with db.transaction() as con:
        con.executemany('INSERT INTO orders(order_id,sku_id,quantity,status,warehouse_id,promised_at) '
                        'VALUES (?, ?, 5, ?, NULL, ?)', [
                            ('ORD-2001', 'SKU-B', 'pending', '2026-11-14'),
                            ('ORD-2002', 'SKU-B', 'pending', '2026-11-14'),
                        ])
    def request(i):
        try:
            OpsService(OpsStore(db.path)).reserve(f'ORD-200{i}', 'WH-EAST', key(i))
            return 'success'
        except RuleViolation:
            return 'out_of_stock'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, (1, 2)))
    assert sorted(results) == ['out_of_stock', 'success']
    assert sql(db, 'SELECT reserved FROM inventory WHERE warehouse_id=? AND sku_id=?',
               'WH-EAST', 'SKU-B') == 5


def test_missing_idempotency_key_rejected(domain):
    _, svc = domain
    for bad in ('', '123', 'foo ; DROP TABLE orders'):
        with pytest.raises(RuleViolation):
            svc.reserve('ORD-1001', 'WH-WEST', bad)


@pytest.fixture
def client(domain):
    db, _ = domain
    app.dependency_overrides[get_store] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_api_read_endpoints(client):
    assert client.get('/api/ops/summary').json()['orders'] == 3
    assert len(client.get('/api/ops/incidents').json()['items']) == 3
    assert client.get('/api/ops/incidents/INC-1001').json()['inventory']
    assert client.get('/api/ops/incidents/UNKNOWN').status_code == 404
    assert client.get('/api/overview').status_code == 200
    assert client.get('/api/health').status_code == 200


def test_write_endpoints_disabled_without_token(client, monkeypatch):
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    response = client.post('/api/ops/commands/reserve', json={
        'order_id': 'ORD-1001', 'warehouse_id': 'WH-WEST'}, headers={
            'X-Idempotency-Key': key(1)})
    assert response.status_code == 403
    assert client.get('/api/ops/incidents/INC-1001').json()['order_status'] == 'pending'


def test_api_authorized_command_and_replay(client, monkeypatch):
    token = 'local-test-only-secret-token-0123456789'
    monkeypatch.setenv('EIA_OPS_WRITE_TOKEN', token)
    headers = {'X-Ops-Demo-Token': token, 'X-Idempotency-Key': key(1)}
    payload = {'order_id': 'ORD-1001', 'warehouse_id': 'WH-WEST'}
    first = client.post('/api/ops/commands/reserve', json=payload, headers=headers)
    repeat = client.post('/api/ops/commands/reserve', json=payload, headers=headers)
    assert first.status_code == 200 and repeat.status_code == 200
    assert first.json()['replayed'] is False and repeat.json()['replayed'] is True
    assert client.get('/api/ops/incidents/INC-1001').json()['order_status'] == 'reserved'


def test_api_requires_real_token_and_strict_payload(client, monkeypatch):
    token = 'local-test-only-secret-token-0123456789'
    monkeypatch.setenv('EIA_OPS_WRITE_TOKEN', token)
    payload = {'order_id': 'ORD-1001', 'warehouse_id': 'WH-WEST'}
    assert client.post('/api/ops/commands/reserve', json=payload, headers={
        'X-Ops-Demo-Token': 'wrong', 'X-Idempotency-Key': key(1)}).status_code == 403
    assert client.post('/api/ops/commands/reserve', json={**payload, 'secret': 'extra'}, headers={
        'X-Ops-Demo-Token': token, 'X-Idempotency-Key': key(1)}).status_code == 422
    assert client.get('/api/ops/incidents/INC-1001').json()['order_status'] == 'pending'
