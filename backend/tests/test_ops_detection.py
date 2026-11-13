"""A2 regression: detection is state-derived, transactional and idempotent."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ops.api import get_store
from app.ops.detection import DetectionEngine, DetectionError
from app.ops.service import OpsService
from app.ops.store import OpsStore

UTC = timezone.utc
BEFORE = datetime(2026, 11, 12, tzinfo=UTC)
LATE = datetime(2026, 11, 15, tzinfo=UTC)


@pytest.fixture
def domain(tmp_path):
    store = OpsStore(tmp_path / 'ops.sqlite')
    store.init()
    return store, DetectionEngine(store)


def count(db, table):
    assert table in {'detection_signals', 'detection_events', 'incidents', 'command_receipts', 'audit_log'}
    with db._connection() as con:
        return con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def find(store, order_id, rule):
    return next(s for s in store.list_signals() if s['order_id'] == order_id and s['rule_code'] == rule)


def test_migration_from_v1_preserves_a1_business_receipts(domain):
    db, _ = domain
    result = OpsService(db).reserve('ORD-1001', 'WH-WEST', 'migration-reserve-01')
    assert result['status'] == 'reserved'
    # Simulate an A1 file by removing A2-only tables and reverting schema version.
    with db._connection() as con:
        con.execute('DROP TABLE detection_events')
        con.execute('DROP TABLE detection_signals')
        con.execute('PRAGMA user_version = 1')
    db.init()
    with db._connection() as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 2
    assert OpsService(db).reserve('ORD-1001', 'WH-WEST', 'migration-reserve-01')['replayed']
    assert db.get_incident('INC-1001')['order_status'] == 'reserved'
    assert count(db, 'command_receipts') == 1
    db.init()
    assert count(db, 'command_receipts') == 1


def test_scan_shortage_observed_from_state_not_hardcoded_incident(domain):
    db, detector = domain
    assert db.detector_summary()['active_total'] == 0
    result = detector.scan(at=BEFORE)
    match = next(x for x in result['changes'] if x['rule_code'] == 'inventory_shortage')
    assert match['order_id'] == 'ORD-1003'
    assert match['incident_id'] == 'INC-1003'
    evidence = db.get_signal(match['signal_id'])['evidence']
    assert evidence['quantity'] == 20 and evidence['max_single_warehouse_available'] == 2
    assert result['scanned_orders'] == 3


def test_scan_idempotent_does_not_append_or_modify_business_audit(domain):
    db, detector = domain
    first = detector.scan(at=BEFORE)
    second = detector.scan(at=BEFORE + timedelta(hours=1))
    assert first['changes_total'] == 1
    assert second['changes_total'] == 0
    assert count(db, 'detection_events') == 1
    assert count(db, 'audit_log') == 0
    assert count(db, 'command_receipts') == 0


def test_overdue_severity_updates_only_at_threshold(domain):
    db, detector = domain
    clock = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)  # pre-promise for all seeded orders
    detector.scan(at=clock)
    assert not [x for x in db.list_signals() if x['rule_code'] == 'promise_overdue']
    medium = detector.scan(at=clock + timedelta(hours=2))
    assert any(x['rule_code'] == 'promise_overdue' and x['severity'] == 'medium'
               for x in medium['changes'])
    high = detector.scan(at=clock + timedelta(hours=27))
    assert any(x['rule_code'] == 'promise_overdue' and x['severity'] == 'high'
               for x in high['changes'])
    assert detector.scan(at=clock + timedelta(hours=28))['changes_total'] == 0


def test_utc_and_offset_dates_are_compared_correctly(domain):
    db, detector = domain
    at_promise = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
    detector.scan(at=at_promise)
    assert not any(x['rule_code'] == 'promise_overdue' and x['order_id'] == 'ORD-1001'
                   for x in db.list_signals())
    detector.scan(at=at_promise + timedelta(seconds=1))
    assert any(x['rule_code'] == 'promise_overdue' and x['order_id'] == 'ORD-1001'
               for x in db.list_signals())
    with pytest.raises(DetectionError):
        detector.scan(at=datetime(2026, 11, 14))


def test_inventory_shortage_is_per_warehouse_not_sum(domain):
    db, detector = domain
    with db.transaction() as con:
        con.execute('INSERT INTO orders VALUES (?,?,?,?,?,?)',
                    ('ORD-SPLIT', 'SKU-C', 3, 'pending', None, '2026-12-31T00:00:00+00:00'))
    detector.scan(at=BEFORE)
    signal = find(db, 'ORD-SPLIT', 'inventory_shortage')
    # 1+2 units in two separate warehouses cannot fulfill one order of 3.
    assert signal['evidence']['max_single_warehouse_available'] == 2
    assert signal['incident_id'].startswith('INC-AUTO-')
    assert db.get_incident(signal['incident_id'])['order_id'] == 'ORD-SPLIT'


def test_autocreated_incident_does_not_override_operator_terminal_state(domain):
    db, detector = domain
    with db.transaction() as con:
        con.execute('INSERT INTO orders VALUES (?,?,?,?,?,?)',
                    ('ORD-DYNAMIC', 'SKU-C', 50, 'pending', None, '2026-12-31T00:00:00+00:00'))
    detector.scan(at=BEFORE)
    original = find(db, 'ORD-DYNAMIC', 'inventory_shortage')
    incident_id = original['incident_id']
    OpsService(db).escalate(incident_id, 'Operator escalation - investigation required',
                            'a2-escalation-01')
    assert db.get_incident(incident_id)['state'] == 'escalated'
    with db.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=100 WHERE warehouse_id='WH-EAST' AND sku_id='SKU-C'")
    detector.scan(at=BEFORE + timedelta(hours=1))
    assert find(db, 'ORD-DYNAMIC', 'inventory_shortage')['status'] == 'cleared'
    assert db.get_incident(incident_id)['state'] == 'escalated'
    with db.transaction() as con:
        con.execute("UPDATE inventory SET on_hand=1 WHERE warehouse_id='WH-EAST' AND sku_id='SKU-C'")
    detector.scan(at=BEFORE + timedelta(hours=2))
    reopened = find(db, 'ORD-DYNAMIC', 'inventory_shortage')
    assert reopened['status'] == 'active' and reopened['revision'] == 3
    assert reopened['incident_id'] != incident_id
    assert db.get_incident(incident_id)['state'] == 'escalated'
    assert db.get_incident(reopened['incident_id'])['state'] == 'open'
    assert [e['transition'] for e in db.list_signal_events() if e['signal_id'] == reopened['signal_id']] == [
        'detected', 'cleared', 'reopened']


def test_stalled_shipment_detects_and_clears_on_business_commands(domain):
    db, detector = domain
    ops = OpsService(db)
    ops.reserve('ORD-1001', 'WH-WEST', 'a2-reserve-01')
    ops.dispatch('ORD-1001', 'demo', 'a2-dispatch-01')
    with db._connection() as con:
        updated = con.execute("SELECT updated_at FROM shipments WHERE order_id='ORD-1001'").fetchone()[0]
    at = datetime.fromisoformat(updated).replace(tzinfo=UTC)
    detector.scan(at=at + timedelta(hours=7))
    signal = find(db, 'ORD-1001', 'shipment_stalled')
    assert signal['severity'] == 'medium'
    detector.scan(at=at + timedelta(hours=13))
    assert find(db, 'ORD-1001', 'shipment_stalled')['severity'] == 'high'
    ops.advance_shipment('ORD-1001', 'in_transit', 'a2-transit-01')
    assert find(db, 'ORD-1001', 'shipment_stalled')['status'] == 'cleared'
    ops.advance_shipment('ORD-1001', 'delivered', 'a2-delivered-01')
    assert db.get_incident('INC-1001')['order_status'] == 'delivered'


def test_command_detection_failure_rolls_back_business_mutation(domain):
    db, _ = domain
    with db.transaction() as con:
        con.execute("UPDATE orders SET promised_at='2020-01-01T00:00:00+00:00' WHERE order_id='ORD-1001'")
        con.execute('''CREATE TRIGGER reject_detector BEFORE INSERT ON detection_events
                       BEGIN SELECT RAISE(ABORT, 'detector blocked'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        OpsService(db).reserve('ORD-1001', 'WH-WEST', 'a2-reject-01')
    assert db.get_incident('INC-1001')['order_status'] == 'pending'
    assert count(db, 'audit_log') == 0 and count(db, 'command_receipts') == 0
    assert count(db, 'detection_signals') == 0
    assert db.get_incident('INC-1001')['inventory'][-1]['reserved'] == 0


def test_scan_failure_rolls_back_all_signals(domain):
    db, detector = domain
    with db.transaction() as con:
        con.execute('''CREATE TRIGGER reject_detector BEFORE INSERT ON detection_events
                       BEGIN SELECT RAISE(ABORT, 'detector blocked'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        detector.scan(at=BEFORE)
    assert count(db, 'detection_signals') == 0
    assert count(db, 'incidents') == 3


def test_scan_limit_rejects_partial_write(domain):
    db, detector = domain
    with pytest.raises(DetectionError):
        detector.scan(at=BEFORE, max_orders=2)
    assert count(db, 'detection_signals') == 0
    with pytest.raises(DetectionError):
        detector.scan(at=BEFORE, max_orders=501)


def test_persisted_signals_events_survive_store_reopen(domain):
    db, detector = domain
    detector.scan(at=BEFORE)
    recreated = OpsStore(db.path)
    recreated.init()
    assert recreated.detector_summary()['events_total'] == 1
    assert recreated.list_signal_events()[0]['transition'] == 'detected'
    assert recreated.get_signal(db.list_signals()[0]['signal_id'])['status'] == 'active'


def test_read_api_and_filtering(domain):
    db, detector = domain
    detector.scan(at=BEFORE)
    app.dependency_overrides[get_store] = lambda: db
    try:
        with TestClient(app) as client:
            assert client.get('/api/ops/summary').json()['operations_schema'] == 2
            assert client.get('/api/ops/detector/summary').json()['events_total'] == 1
            sig = client.get('/api/ops/signals?status=active').json()['items'][0]
            assert sig['rule_code'] == 'inventory_shortage'
            assert client.get('/api/ops/signals/' + sig['signal_id']).json()['evidence']['quantity'] == 20
            ev = client.get('/api/ops/signals/events').json()['items']
            assert len(ev) == 1 and ev[0]['transition'] == 'detected'
            assert client.get('/api/ops/signals/events?after_id=' + str(ev[0]['event_id'])).json()['items'] == []
            assert client.get('/api/ops/signals?status=invalid').status_code == 422
            assert client.get('/api/ops/signals/events?limit=999').status_code == 422
            assert client.get('/api/ops/signals/UNKNOWN').status_code == 404
            assert client.get('/api/health').status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_scan_api_requires_token(domain, monkeypatch):
    db, _ = domain
    app.dependency_overrides[get_store] = lambda: db
    monkeypatch.delenv('EIA_OPS_WRITE_TOKEN', raising=False)
    try:
        with TestClient(app) as client:
            assert client.post('/api/ops/detector/scan').status_code == 403
            assert db.detector_summary()['events_total'] == 0
    finally:
        app.dependency_overrides.clear()


def test_scan_api_authorized_without_clock_override(domain, monkeypatch):
    db, _ = domain
    token = 'local-only-a2-test-token-12345678'
    monkeypatch.setenv('EIA_OPS_WRITE_TOKEN', token)
    app.dependency_overrides[get_store] = lambda: db
    try:
        with TestClient(app) as client:
            response = client.post('/api/ops/detector/scan', headers={'X-Ops-Demo-Token': token})
            assert response.status_code == 200
            assert response.json()['scanned_orders'] == 3
            again = client.post('/api/ops/detector/scan', headers={'X-Ops-Demo-Token': token})
            assert again.status_code == 200 and again.json()['changes_total'] == 0
    finally:
        app.dependency_overrides.clear()


def test_concurrent_scans_produce_no_duplicate_events(domain):
    db, _ = domain
    def run(_):
        return DetectionEngine(OpsStore(db.path)).scan(at=BEFORE)['changes_total']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (1, 2)))
    assert sorted(results) == [0, 1]
    assert count(db, 'detection_events') == 1


def test_stock_reservation_detects_shortage_on_another_order(domain):
    db, _ = domain
    with db.transaction() as con:
        con.execute('INSERT INTO orders VALUES (?,?,?,?,?,?)',
                    ('ORD-PEER', 'SKU-B', 7, 'pending', None, '2026-12-31T00:00:00+00:00'))
    # 8 units in WH-EAST, 3 units in WH-WEST. A2 scan sees no shortage for 7.
    detector = DetectionEngine(db)
    detector.scan(at=BEFORE)
    assert not [s for s in db.list_signals() if s['order_id'] == 'ORD-PEER']
    OpsService(db).reserve('ORD-1002', 'WH-EAST', 'a2-peer-reserve-01')
    shortage = find(db, 'ORD-PEER', 'inventory_shortage')
    assert shortage['status'] == 'active'
    assert shortage['evidence']['max_single_warehouse_available'] == 6
    OpsService(db).release('ORD-1002', 'a2-peer-release-01')
    assert db.get_signal(shortage['signal_id'])['status'] == 'cleared'
    assert db.list_signal_events()[-1]['evidence']['cleared_reason'] == 'rule_no_longer_matches'


def test_replayed_stock_command_does_not_repeat_peer_signal(domain):
    db, _ = domain
    with db.transaction() as con:
        con.execute('INSERT INTO orders VALUES (?,?,?,?,?,?)',
                    ('ORD-PEER', 'SKU-B', 7, 'pending', None, '2026-12-31T00:00:00+00:00'))
    ops = OpsService(db)
    ops.reserve('ORD-1002', 'WH-EAST', 'a2-replay-01')
    before = count(db, 'detection_events')
    assert ops.reserve('ORD-1002', 'WH-EAST', 'a2-replay-01')['replayed']
    assert count(db, 'detection_events') == before


def test_existing_escalated_incident_keeps_authority_on_first_detection(domain):
    db, _ = domain
    OpsService(db).escalate('INC-1003', 'Human hold - warehouse shortage', 'a2-hold-01')
    signal = find(db, 'ORD-1003', 'inventory_shortage')
    assert signal['incident_id'] == 'INC-1003'
    assert db.get_incident('INC-1003')['state'] == 'escalated'
    assert count(db, 'incidents') == 3
