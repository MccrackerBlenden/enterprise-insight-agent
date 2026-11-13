"""A3 deterministic fulfillment quotes, using ONLY synthetic warehouse policies.

Quotes are not reservations. No live carrier prices, delivery routes or customer
addresses are available. Feasibility comes from the actual operations database;
all scoring inputs are explicitly documented *simulated assumptions*.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .service import NotFound

UTC = timezone.utc
POLICY_VERSION = 'synthetic-handling-v1'
QUOTE_BUCKET_SECONDS = 300
# Synthetic policy points, NOT monetary amounts or external SLA promises.
# Both entries are deliberately public, reviewable and independent of the LLM.
POLICY = {
    'WH-EAST': {'handling_hours': 8, 'cost_points_per_unit': 5},
    'WH-WEST': {'handling_hours': 18, 'cost_points_per_unit': 3},
}


def _clock(at: datetime | None) -> datetime:
    at = at or datetime.now(UTC)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('Planning timestamp must be timezone-aware')
    return at.astimezone(UTC)


def _time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def quote_order(con: sqlite3.Connection, order_id: str, *, at: datetime | None = None) -> dict:
    """Compute a short-lived optimistic quote without mutating any tables.

    An operation must recompute this quote inside its BEGIN IMMEDIATE transaction
    and compare its fingerprint; stale quotes do not authorize stale inventory.
    """
    now = _clock(at)
    order = con.execute('SELECT * FROM orders WHERE order_id=?', (order_id,)).fetchone()
    if order is None:
        raise NotFound('Order not found')
    order = dict(order)
    warehouses = [dict(row) for row in con.execute('''
        SELECT w.warehouse_id, w.region, w.active,
               v.on_hand, v.reserved
        FROM warehouses w LEFT JOIN inventory v
          ON w.warehouse_id=v.warehouse_id AND v.sku_id=?
        ORDER BY w.warehouse_id
    ''', (order['sku_id'],))]
    incidents = [dict(row) for row in con.execute('''
        SELECT incident_id,state FROM incidents WHERE order_id=? ORDER BY incident_id
    ''', (order_id,))]
    time_bucket = int(now.timestamp()) // QUOTE_BUCKET_SECONDS
    state = {'order': order, 'warehouses': warehouses, 'incidents': incidents,
             'policy_version': POLICY_VERSION, 'time_bucket': time_bucket}
    fingerprint = hashlib.sha256(_canonical(state).encode('utf-8')).hexdigest()
    active_holds = [i['state'] for i in incidents if i['state'] in ('escalated', 'resolved')]
    promised = _time(order['promised_at'])
    options = []
    for wh in warehouses:
        warehouse_id = wh['warehouse_id']
        policy = POLICY.get(warehouse_id)
        available = (wh['on_hand'] - wh['reserved']) if wh['on_hand'] is not None else 0
        reasons = []
        if order['status'] != 'pending':
            reasons.append('order_not_pending')
        if active_holds:
            reasons.append('incident_requires_reassessment')
        if not wh['active']:
            reasons.append('warehouse_inactive')
        if wh['on_hand'] is None:
            reasons.append('inventory_missing')
        elif available < order['quantity']:
            reasons.append('insufficient_stock')
        if policy is None:
            reasons.append('no_simulated_policy')
        eligible = len(reasons) == 0
        # These are simulated comparative indicators, not a carrier's ETA or price.
        hours = policy['handling_hours'] if policy else None
        projected = now + timedelta(hours=hours) if hours is not None else None
        minutes_late = (max(0, int((projected - promised).total_seconds() // 60))
                        if projected else None)
        cost = policy['cost_points_per_unit'] * order['quantity'] if policy else None
        options.append({
            'warehouse_id': warehouse_id, 'region': wh['region'],
            'available': available, 'required': order['quantity'],
            'eligible': eligible, 'blockers': reasons,
            'simulated_handling_hours': hours,
            'simulated_cost_points': cost,
            'projected_ready_at': projected.isoformat() if projected else None,
            'simulated_late_minutes': minutes_late,
            'sla_risk': ('late' if minutes_late else 'on_time') if projected else 'unknown',
        })
    # Objective is lexicographic: feasible -> simulated lateness -> cost points.
    # Never mistake arbitrary cost points for actual shipping prices.
    options.sort(key=lambda o: (
        not o['eligible'],
        o['simulated_late_minutes'] if o['simulated_late_minutes'] is not None else 10**9,
        o['simulated_cost_points'] if o['simulated_cost_points'] is not None else 10**9,
        o['warehouse_id'],
    ))
    valid_until = datetime.fromtimestamp((time_bucket + 1) * QUOTE_BUCKET_SECONDS, tz=UTC)
    return {
        'synthetic': True, 'order_id': order_id,
        'order_status': order['status'], 'sku_id': order['sku_id'],
        'quantity': order['quantity'], 'promised_at': promised.isoformat(),
        'policy_version': POLICY_VERSION,
        'assumptions': 'fictional per-warehouse handling hours and dimensionless cost points; no customer address, carrier SLA or live routing',
        'ranking': ['eligible', 'simulated_late_minutes', 'simulated_cost_points', 'warehouse_id'],
        'observed_at': now.isoformat(), 'valid_until': valid_until.isoformat(),
        'snapshot_fingerprint': fingerprint, 'options': options,
        'recommended_warehouse_id': next((o['warehouse_id'] for o in options if o['eligible']), None),
    }
