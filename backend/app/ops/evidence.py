"""Immutable, source-backed case evidence for synthetic operations.

Model narratives never change verified facts or authorize commands. The SHA256 is
an integrity fingerprint of a canonical snapshot, not an authenticity signature.
"""
from __future__ import annotations

import hashlib
import json

from .service import NotFound
from .store import OpsStore


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def capture_evidence(ops: OpsStore, incident_id: str, *, incident: dict | None = None,
                     quote: dict | None = None) -> dict:
    incident = incident if incident is not None else ops.get_incident(incident_id)
    if incident is None:
        raise NotFound('Incident not found')
    quote = quote if quote is not None else ops.fulfillment_quote(incident['order_id'])
    signals = [
        {'signal_id': s['signal_id'], 'rule_code': s['rule_code'],
         'status': s['status'], 'revision': s['revision'], 'severity': s['severity']}
        for s in ops.list_signals(limit=500) if s['order_id'] == incident['order_id']
    ]
    # Ignore volatile clock rendering: the quote fingerprint already binds the
    # 5-minute validity bucket, business rows and policy version.
    stable_quote = {k: v for k, v in quote.items() if k not in ('observed_at', 'valid_until')}
    stable_quote['options'] = [
        {k: v for k, v in option.items()
         if k not in ('projected_ready_at', 'simulated_late_minutes', 'sla_risk')}
        for option in quote['options']
    ]
    bundle = {
        'synthetic': True,
        'sources': ['ops.incidents', 'ops.orders', 'ops.inventory',
                    'ops.shipments', 'ops.audit_log', 'ops.detection_signals',
                    'ops.fulfillment_quote'],
        'incident_id': incident_id,
        'incident': {
            'state': incident['state'], 'kind': incident['kind'],
            'severity': incident['severity'], 'order_id': incident['order_id'],
            'order_status': incident['order_status'],
            'sku_id': incident['sku_id'], 'quantity': incident['quantity'],
            'warehouse_id': incident['warehouse_id'],
            'shipment_status': incident.get('shipment_status'),
            'promised_at': incident.get('promised_at'),
        },
        'inventory': [dict(item) for item in incident['inventory']],
        'audit': [dict(item) for item in incident.get('audit', [])],
        'signals': sorted(signals, key=lambda s: s['signal_id']),
        'quote': stable_quote,
        'limitations': [
            'Model narrative is not independently fact-checked',
            'Cost points and handling hours are fictional policy assumptions',
            'Actual execution must revalidate inventory and order state',
        ],
    }
    return {'facts': bundle, 'sha256': hashlib.sha256(_canonical(bundle).encode()).hexdigest()}


def only_quote_window_changed(previous: dict, current: dict) -> bool:
    """Require all business facts to match, ignoring only quote time bucket."""
    from copy import deepcopy
    before, after = deepcopy(previous), deepcopy(current)
    try:
        old_token = before['quote'].pop('snapshot_fingerprint')
        new_token = after['quote'].pop('snapshot_fingerprint')
    except (KeyError, TypeError, AttributeError):
        return False
    return old_token != new_token and before == after
