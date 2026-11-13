"""Deterministic business commands. Model text can never bypass these invariants."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .store import OpsStore


class OpsError(Exception):
    status_code = 422


class NotFound(OpsError):
    status_code = 404


class StateConflict(OpsError):
    status_code = 409


class RuleViolation(OpsError):
    status_code = 422


def _row(con: sqlite3.Connection, sql: str, params: tuple, resource: str) -> dict:
    value = con.execute(sql, params).fetchone()
    if value is None:
        raise NotFound(f'{resource} not found')
    return dict(value)


class OpsService:
    def __init__(self, store: OpsStore):
        self.store = store

    def _command(self, operation: str, key: str, payload: dict,
                 apply: Callable[[sqlite3.Connection], tuple[dict, str, dict, dict]]) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9._:-]{8,100}', key):
            raise RuleViolation('Invalid idempotency key (8-100 safe characters)')
        canonical = json.dumps({'operation': operation, 'payload': payload},
                               sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        with self.store.transaction() as con:
            previous = con.execute(
                'SELECT request_digest,response_json FROM command_receipts WHERE idempotency_key=?',
                (key,),
            ).fetchone()
            if previous is not None:
                if previous['request_digest'] != digest:
                    raise StateConflict('Idempotency key already used with different arguments')
                return {**json.loads(previous['response_json']), 'replayed': True}
            result, subject, before, after = apply(con)
            # Mutating a business object also updates deterministic anomaly signals
            # in the SAME transaction. Failure rolls back the command and audit.
            from .detection import DetectionEngine, DetectionError
            detector = DetectionEngine(self.store)
            clock = datetime.now(timezone.utc)
            order_id = payload.get('order_id')
            if order_id is None and 'incident_id' in payload:
                associated = con.execute('SELECT order_id FROM incidents WHERE incident_id=?',
                                         (payload['incident_id'],)).fetchone()
                order_id = associated['order_id'] if associated else None
            if order_id:
                detector.sync_order(con, order_id, at=clock)
                # Stock changes affect ALL unreserved orders sharing this SKU,
                # not just the order that initiated the command.
                if operation in ('reserve', 'release', 'dispatch'):
                    sku = con.execute('SELECT sku_id FROM orders WHERE order_id=?',
                                      (order_id,)).fetchone()['sku_id']
                    peers = [r['order_id'] for r in con.execute('''
                        SELECT order_id FROM orders WHERE sku_id=? AND status='pending'
                          AND order_id!=? ORDER BY order_id LIMIT 501
                    ''', (sku, order_id))]
                    if len(peers) > 500:
                        raise DetectionError('Too many affected orders; command rolled back')
                    for peer_id in peers:
                        detector.sync_order(con, peer_id, at=clock)
            con.execute('''
                INSERT INTO command_receipts(idempotency_key,operation,request_digest,response_json)
                VALUES (?,?,?,?)
            ''', (key, operation, digest, json.dumps(result, ensure_ascii=False, sort_keys=True)))
            con.execute('''
                INSERT INTO audit_log(idempotency_key,operation,actor,subject_id,before_json,after_json)
                VALUES (?,?,?,?,?,?)
            ''', (key, operation, 'local-demo-operator', subject,
                  json.dumps(before, ensure_ascii=False, sort_keys=True),
                  json.dumps(after, ensure_ascii=False, sort_keys=True)))
            return {**result, 'replayed': False}

    def reserve(self, order_id: str, warehouse_id: str, key: str,
                *, expected_snapshot: str | None = None) -> dict:
        """Legacy reserve is preserved; conditional reserves also check the plan snapshot."""
        if expected_snapshot is not None and not re.fullmatch(r'[0-9a-f]{64}', expected_snapshot):
            raise RuleViolation('Invalid planning snapshot fingerprint')
        def apply(con):
            if expected_snapshot is not None:
                # BEGIN IMMEDIATE was already acquired by _command(). This check
                # and inventory mutation therefore share one writer transaction.
                from .planning import quote_order
                current = quote_order(con, order_id)
                if current['snapshot_fingerprint'] != expected_snapshot:
                    raise StateConflict('Plan expired or business state changed; request a new quote')
                selected = next((o for o in current['options']
                                 if o['warehouse_id'] == warehouse_id), None)
                if selected is None or not selected['eligible']:
                    raise StateConflict('Selected plan is no longer executable')
            order = _row(con, 'SELECT * FROM orders WHERE order_id=?', (order_id,), 'Order')
            blocked = con.execute("SELECT 1 FROM incidents WHERE order_id=? AND state IN ('escalated','resolved')",
                                  (order_id,)).fetchone()
            if blocked:
                raise StateConflict('Closed or escalated incident requires human reassessment')
            if order['status'] != 'pending':
                raise StateConflict('Only pending orders may reserve inventory')
            wh = _row(con, 'SELECT * FROM warehouses WHERE warehouse_id=?',
                      (warehouse_id,), 'Warehouse')
            if not wh['active']:
                raise RuleViolation('Warehouse is inactive')
            inv = _row(con, 'SELECT * FROM inventory WHERE warehouse_id=? AND sku_id=?',
                       (warehouse_id, order['sku_id']), 'Inventory')
            if inv['on_hand'] - inv['reserved'] < order['quantity']:
                raise RuleViolation('Insufficient available inventory; no changes committed')
            changed = con.execute('''UPDATE inventory SET reserved=reserved+?
                WHERE warehouse_id=? AND sku_id=? AND on_hand-reserved>=?''',
                (order['quantity'], warehouse_id, order['sku_id'], order['quantity']))
            if changed.rowcount != 1:
                raise StateConflict('Inventory changed concurrently')
            con.execute("UPDATE orders SET status='reserved',warehouse_id=? WHERE order_id=?",
                        (warehouse_id, order_id))
            con.execute("UPDATE incidents SET state='investigating',updated_at=CURRENT_TIMESTAMP "
                        "WHERE order_id=? AND state='open'", (order_id,))
            result = {'order_id': order_id, 'warehouse_id': warehouse_id,
                      'status': 'reserved', 'quantity': order['quantity']}
            return result, order_id, {'order_status': order['status'], 'reserved': inv['reserved']}, {
                'order_status': 'reserved', 'reserved': inv['reserved'] + order['quantity']}
        payload = {'order_id': order_id, 'warehouse_id': warehouse_id}
        if expected_snapshot is not None:
            payload['expected_snapshot'] = expected_snapshot
        return self._command('reserve', key, payload, apply)

    def release(self, order_id: str, key: str) -> dict:
        def apply(con):
            order = _row(con, 'SELECT * FROM orders WHERE order_id=?', (order_id,), 'Order')
            if order['status'] != 'reserved':
                raise StateConflict('Only reserved, unshipped orders may release inventory')
            changed = con.execute('''UPDATE inventory SET reserved=reserved-?
                WHERE warehouse_id=? AND sku_id=? AND reserved>=?''',
                (order['quantity'], order['warehouse_id'], order['sku_id'], order['quantity']))
            if changed.rowcount != 1:
                raise StateConflict('Inventory reservation is inconsistent')
            con.execute("UPDATE orders SET status='pending',warehouse_id=NULL WHERE order_id=?", (order_id,))
            con.execute("UPDATE incidents SET state='open',updated_at=CURRENT_TIMESTAMP "
                        "WHERE order_id=? AND state='investigating'", (order_id,))
            return {'order_id': order_id, 'status': 'pending'}, order_id, {
                'order_status': 'reserved'}, {'order_status': 'pending'}
        return self._command('release', key, {'order_id': order_id}, apply)

    def dispatch(self, order_id: str, carrier: str, key: str) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9_-]{2,40}', carrier):
            raise RuleViolation('Invalid simulated carrier code')
        def apply(con):
            order = _row(con, 'SELECT * FROM orders WHERE order_id=?', (order_id,), 'Order')
            if order['status'] != 'reserved':
                raise StateConflict('Only reserved orders may be dispatched')
            if con.execute("SELECT 1 FROM incidents WHERE order_id=? AND state='escalated'",
                           (order_id,)).fetchone():
                raise StateConflict('Escalated incident requires human reassessment')
            changed = con.execute('''UPDATE inventory
                SET reserved=reserved-?, on_hand=on_hand-?
                WHERE warehouse_id=? AND sku_id=? AND reserved>=? AND on_hand>=?''',
                (order['quantity'], order['quantity'], order['warehouse_id'],
                 order['sku_id'], order['quantity'], order['quantity']))
            if changed.rowcount != 1:
                raise StateConflict('Inventory cannot be dispatched')
            con.execute("UPDATE orders SET status='shipped' WHERE order_id=?", (order_id,))
            shipment_id = f'SHP-{order_id}'
            con.execute('INSERT INTO shipments(shipment_id,order_id,carrier,status) VALUES (?,?,?,?)',
                        (shipment_id, order_id, carrier, 'created'))
            return {'order_id': order_id, 'shipment_id': shipment_id, 'status': 'shipped',
                    'shipment_status': 'created'}, order_id, {'order_status': 'reserved'}, {
                'order_status': 'shipped', 'shipment_status': 'created'}
        return self._command('dispatch', key, {'order_id': order_id, 'carrier': carrier}, apply)

    def advance_shipment(self, order_id: str, to_status: str, key: str) -> dict:
        if to_status not in ('in_transit', 'delivered'):
            raise RuleViolation('Only in_transit or delivered are supported')
        def apply(con):
            shipment = _row(con, 'SELECT * FROM shipments WHERE order_id=?',
                            (order_id,), 'Shipment')
            if con.execute("SELECT 1 FROM incidents WHERE order_id=? AND state='escalated'",
                           (order_id,)).fetchone():
                raise StateConflict('Escalated incident requires human reassessment')
            allowed = {'created': 'in_transit', 'in_transit': 'delivered'}
            if allowed.get(shipment['status']) != to_status:
                raise StateConflict('Invalid shipment status transition')
            con.execute('UPDATE shipments SET status=?,updated_at=CURRENT_TIMESTAMP WHERE order_id=?',
                        (to_status, order_id))
            if to_status == 'delivered':
                changed = con.execute("UPDATE orders SET status='delivered' WHERE order_id=? AND status='shipped'",
                                      (order_id,))
                if changed.rowcount != 1:
                    raise StateConflict('Order is not in a shippable delivery state')
            return {'order_id': order_id, 'shipment_status': to_status}, order_id, {
                'shipment_status': shipment['status']}, {'shipment_status': to_status}
        return self._command('advance_shipment', key, {
            'order_id': order_id, 'to_status': to_status}, apply)

    def verify_incident(self, incident_id: str, key: str) -> dict:
        def apply(con):
            inc = _row(con, 'SELECT * FROM incidents WHERE incident_id=?',
                       (incident_id,), 'Incident')
            if inc['state'] not in ('open', 'investigating'):
                raise StateConflict('Incident is not awaiting verification')
            order = _row(con, 'SELECT * FROM orders WHERE order_id=?',
                         (inc['order_id'],), 'Order')
            shipment = con.execute('SELECT status FROM shipments WHERE order_id=?',
                                   (inc['order_id'],)).fetchone()
            if order['status'] != 'delivered' or shipment is None or shipment['status'] != 'delivered':
                raise RuleViolation('Cannot resolve: delivery is not independently verified')
            con.execute("UPDATE incidents SET state='resolved',resolution_note=?,"
                        "updated_at=CURRENT_TIMESTAMP WHERE incident_id=?",
                        ('Delivery confirmed from order and shipment states', incident_id))
            return {'incident_id': incident_id, 'state': 'resolved', 'verified': True}, incident_id, {
                'state': inc['state']}, {'state': 'resolved'}
        return self._command('verify_incident', key, {'incident_id': incident_id}, apply)

    def escalate(self, incident_id: str, reason: str, key: str) -> dict:
        reason = reason.strip()
        if not 5 <= len(reason) <= 300:
            raise RuleViolation('Escalation reason must be 5-300 characters')
        def apply(con):
            inc = _row(con, 'SELECT * FROM incidents WHERE incident_id=?',
                       (incident_id,), 'Incident')
            if inc['state'] not in ('open', 'investigating'):
                raise StateConflict('Only active incidents may be escalated')
            con.execute("UPDATE incidents SET state='escalated',resolution_note=?,"
                        "updated_at=CURRENT_TIMESTAMP WHERE incident_id=?", (reason, incident_id))
            return {'incident_id': incident_id, 'state': 'escalated'}, incident_id, {
                'state': inc['state']}, {'state': 'escalated', 'reason': reason}
        return self._command('escalate', key, {'incident_id': incident_id, 'reason': reason}, apply)
