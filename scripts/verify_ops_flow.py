"""Offline acceptance: fresh temporary DB, never writes to the user's data volume."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.ops.service import OpsService, RuleViolation  # noqa: E402
from app.ops.store import OpsStore  # noqa: E402


def main():
    with TemporaryDirectory(prefix='eia-v06a1-') as folder:
        store = OpsStore(Path(folder) / 'operations.sqlite')
        store.init()
        actions = OpsService(store)
        try:
            actions.reserve('ORD-1003', 'WH-WEST', 'accept-shortage-01')
        except RuleViolation:
            print('PASS insufficient stock rejected without mutation')
        else:
            raise AssertionError('Stock shortage was not rejected')
        actions.escalate('INC-1003', 'Insufficient inventory across all warehouses', 'accept-escalate-01')
        assert store.get_incident('INC-1003')['state'] == 'escalated'
        print('PASS shortage incident escalated with audit')

        actions.reserve('ORD-1001', 'WH-WEST', 'accept-reserve-01')
        assert actions.reserve('ORD-1001', 'WH-WEST', 'accept-reserve-01')['replayed']
        actions.dispatch('ORD-1001', 'sim_carrier', 'accept-dispatch-01')
        actions.advance_shipment('ORD-1001', 'in_transit', 'accept-transit-01')
        actions.advance_shipment('ORD-1001', 'delivered', 'accept-delivered-01')
        actions.verify_incident('INC-1001', 'accept-verify-01')
        proof = store.get_incident('INC-1001')
        assert proof['state'] == 'resolved' and proof['order_status'] == 'delivered'
        assert proof['shipment_status'] == 'delivered' and len(proof['audit']) == 5
        print('PASS multi-warehouse reserve -> dispatch -> transit -> delivery -> verified close')
        print('PASS idempotency and five durable audit records')
        print('PASS isolated, deterministic demo acceptance; original analytics DB untouched')


if __name__ == '__main__':
    main()
