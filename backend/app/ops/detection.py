"""A2: deterministic fulfillment anomaly rules and durable signal transitions.

The detector can *observe* business state and open/link incidents. It cannot
reserve inventory, change shipments, close incidents or bypass authorization.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .store import OpsStore

UTC = timezone.utc
RULES = ('inventory_shortage', 'promise_overdue', 'shipment_stalled')


class DetectionError(ValueError):
    status_code = 422


def _instant(value: datetime | None = None) -> datetime:
    value = value or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise DetectionError('Detection timestamp must include timezone')
    return value.astimezone(UTC)


def _db_time(text: str) -> datetime:
    """Historical SQLite timestamps and date-only demo values are UTC."""
    value = datetime.fromisoformat(text)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _serialize(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class DetectionEngine:
    """Scan snapshots or observe one order inside an existing command transaction."""

    def __init__(self, store: OpsStore):
        self.store = store

    def scan(self, *, at: datetime | None = None, max_orders: int = 500) -> dict:
        timestamp = _instant(at)
        if not 1 <= max_orders <= 500:
            raise DetectionError('max_orders must be 1..500')
        # The whole scan is atomic; a partial scan is never reported as complete.
        with self.store.transaction() as con:
            ids = [row['order_id'] for row in con.execute(
                'SELECT order_id FROM orders ORDER BY order_id LIMIT ?', (max_orders + 1,)
            )]
            if len(ids) > max_orders:
                raise DetectionError('Order count exceeds scan limit; no changes written')
            changes = []
            for order_id in ids:
                changes.extend(self.sync_order(con, order_id, at=timestamp))
            return {
                'synthetic': True,
                'scanned_orders': len(ids),
                'changes': changes,
                'changes_total': len(changes),
                'observed_at': timestamp.isoformat(),
            }

    def sync_order(self, con: sqlite3.Connection, order_id: str,
                   *, at: datetime | None = None) -> list[dict]:
        timestamp = _instant(at)
        order = con.execute('SELECT * FROM orders WHERE order_id=?', (order_id,)).fetchone()
        if order is None:
            raise DetectionError('Order does not exist')
        shipment = con.execute('SELECT * FROM shipments WHERE order_id=?', (order_id,)).fetchone()
        warehouses = con.execute('''
            SELECT v.warehouse_id, v.on_hand-v.reserved AS available
            FROM inventory v JOIN warehouses w ON w.warehouse_id=v.warehouse_id
            WHERE v.sku_id=? AND w.active=1 ORDER BY v.warehouse_id
        ''', (order['sku_id'],)).fetchall()
        candidates = self._evaluate(order, shipment, warehouses, timestamp)
        changes: list[dict] = []
        for rule in RULES:
            desired = candidates.get(rule)
            prior = con.execute('''
                SELECT * FROM detection_signals WHERE order_id=? AND rule_code=?
            ''', (order_id, rule)).fetchone()
            if desired is None and (prior is None or prior['status'] == 'cleared'):
                continue
            if desired is None:
                status, severity, evidence = 'cleared', prior['severity'], {
                    'cleared_reason': 'rule_no_longer_matches',
                    'previous_evidence': json.loads(prior['evidence_json']),
                }
                transition = 'cleared'
            else:
                severity, evidence = desired
                status = 'active'
                normalized = _serialize(evidence)
                if (prior is not None and prior['status'] == 'active'
                        and prior['severity'] == severity and prior['evidence_json'] == normalized):
                    continue
                transition = ('detected' if prior is None else
                              'reopened' if prior['status'] == 'cleared' else 'updated')

            if prior is None:
                signal_id = 'SIG-' + hashlib.sha256(f'{order_id}:{rule}'.encode()).hexdigest()[:20].upper()
                revision = 1
                incident_id = self._link_incident(con, order_id, rule, severity, revision,
                                                  reuse_terminal=True)
                con.execute('''
                    INSERT INTO detection_signals(
                        signal_id,order_id,rule_code,incident_id,status,severity,
                        evidence_json,revision,first_detected_at,last_transition_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ''', (signal_id, order_id, rule, incident_id, status, severity,
                      _serialize(evidence), revision, timestamp.isoformat(), timestamp.isoformat()))
            else:
                signal_id = prior['signal_id']
                revision = prior['revision'] + 1
                incident_id = prior['incident_id']
                if transition == 'reopened':
                    incident_id = self._link_incident(con, order_id, rule, severity, revision)
                con.execute('''
                    UPDATE detection_signals SET incident_id=?,status=?,severity=?,
                      evidence_json=?,revision=?,last_transition_at=? WHERE signal_id=?
                ''', (incident_id, status, severity, _serialize(evidence), revision,
                      timestamp.isoformat(), signal_id))
            con.execute('''
                INSERT INTO detection_events(
                    signal_id,transition,revision,severity,evidence_json,observed_at
                ) VALUES (?,?,?,?,?,?)
            ''', (signal_id, transition, revision, severity, _serialize(evidence), timestamp.isoformat()))
            changes.append({
                'signal_id': signal_id, 'order_id': order_id, 'rule_code': rule,
                'incident_id': incident_id, 'transition': transition,
                'status': status, 'severity': severity, 'revision': revision,
            })
        return changes

    @staticmethod
    def _link_incident(con: sqlite3.Connection, order_id: str, rule: str,
                       severity: str, revision: int, *, reuse_terminal: bool = False) -> str:
        existing = con.execute('''
            SELECT incident_id FROM incidents WHERE order_id=? AND state IN ('open','investigating')
            ORDER BY created_at,incident_id LIMIT 1
        ''', (order_id,)).fetchone()
        if existing is not None:
            return existing['incident_id']
        if reuse_terminal:
            # First observation of an already-escalated seed must not bypass
            # a human hold by creating a new open incident for the same order.
            terminal = con.execute('''
                SELECT incident_id FROM incidents WHERE order_id=?
                ORDER BY created_at DESC,incident_id DESC LIMIT 1
            ''', (order_id,)).fetchone()
            if terminal is not None:
                return terminal['incident_id']
        incident_id = 'INC-AUTO-' + hashlib.sha256(
            f'{order_id}:{rule}:{revision}'.encode()).hexdigest()[:20].upper()
        con.execute('''
            INSERT INTO incidents(incident_id,order_id,kind,severity,state,summary)
            VALUES (?,?,'fulfillment_risk',?,'open',?)
        ''', (incident_id, order_id, severity, f'Auto-detected {rule} for {order_id}'))
        return incident_id

    @staticmethod
    def _evaluate(order: sqlite3.Row, shipment: sqlite3.Row | None,
                  warehouses: list[sqlite3.Row], at: datetime) -> dict[str, tuple[str, dict]]:
        issues: dict[str, tuple[str, dict]] = {}
        status = order['status']
        if status == 'pending':
            best = max((w['available'] for w in warehouses), default=0)
            if best < order['quantity']:
                issues['inventory_shortage'] = (
                    'high' if best == 0 else 'medium', {
                        'sku_id': order['sku_id'], 'quantity': order['quantity'],
                        'max_single_warehouse_available': best,
                        'eligible_warehouses': len(warehouses),
                    }
                )
        if status not in ('delivered', 'cancelled'):
            promised = _db_time(order['promised_at'])
            if at > promised:
                overdue = at - promised
                issues['promise_overdue'] = (
                    'high' if overdue >= timedelta(hours=24) else 'medium', {
                        'promised_at': promised.isoformat(), 'order_status': status,
                        'overdue_band': '24h_plus' if overdue >= timedelta(hours=24) else 'under_24h',
                    }
                )
        if status == 'shipped' and shipment is not None and shipment['status'] in ('created','in_transit'):
            threshold = timedelta(hours=6 if shipment['status'] == 'created' else 24)
            last_update = _db_time(shipment['updated_at'])
            elapsed = at - last_update
            if elapsed >= threshold:
                issues['shipment_stalled'] = (
                    'high' if elapsed >= 2 * threshold else 'medium', {
                        'shipment_id': shipment['shipment_id'],
                        'shipment_status': shipment['status'],
                        'last_updated_at': last_update.isoformat(),
                        'stalled_band': 'double_threshold' if elapsed >= 2 * threshold else 'threshold',
                    }
                )
        return issues
