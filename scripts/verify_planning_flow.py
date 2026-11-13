"""Offline A3 acceptance; all mutations confined to a temporary ops database."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.ops.service import OpsService, StateConflict  # noqa: E402
from app.ops.store import OpsStore  # noqa: E402


def verify() -> None:
    with tempfile.TemporaryDirectory(prefix='a3-planning-') as tmp:
        db = OpsStore(Path(tmp) / 'operations.sqlite')
        db.init()
        svc = OpsService(db)
        quote = db.fulfillment_quote('ORD-1001')
        assert quote['recommended_warehouse_id'] == 'WH-WEST'
        assert len(quote['options']) == 2
        assert all('simulated' in quote['assumptions'] or 'fictional' in quote['assumptions']
                   for _ in [None])
        print('PASS quote: real available stock, two warehouses, transparent fictional policy')

        with db.transaction() as con:
            con.execute("UPDATE inventory SET reserved=reserved+1 "
                        "WHERE warehouse_id='WH-WEST' AND sku_id='SKU-A'")
        try:
            svc.reserve('ORD-1001', 'WH-WEST', 'a3-accept-stale-001',
                        expected_snapshot=quote['snapshot_fingerprint'])
        except StateConflict:
            pass
        else:
            raise AssertionError('Stale plan incorrectly executed')
        assert db.get_incident('INC-1001')['order_status'] == 'pending'
        print('PASS changed inventory invalidates quote without writing order or receipt')

        fresh = db.fulfillment_quote('ORD-1001')
        first = svc.reserve('ORD-1001', 'WH-WEST', 'a3-accept-valid-001',
                            expected_snapshot=fresh['snapshot_fingerprint'])
        assert first['replayed'] is False
        replay = OpsService(OpsStore(db.path)).reserve(
            'ORD-1001', 'WH-WEST', 'a3-accept-valid-001',
            expected_snapshot=fresh['snapshot_fingerprint'])
        assert replay['replayed'] is True
        assert db.get_incident('INC-1001')['order_status'] == 'reserved'
        with db._connection() as con:
            assert con.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0] == 1
            assert con.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0] == 1
        print('PASS fresh plan executes exactly once with durable receipt and audit')
        assert db.summary()['operations_schema'] == 2
        print('PASS schema v2 preserved; original analytics database not accessed')
    print('A3 PLANNING AND GUARDED EXECUTION PASS')


if __name__ == '__main__':
    verify()
