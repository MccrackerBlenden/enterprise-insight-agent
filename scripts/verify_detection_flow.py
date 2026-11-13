"""Deterministic A2 acceptance on an isolated temporary database."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.ops.detection import DetectionEngine  # noqa: E402
from app.ops.service import OpsService  # noqa: E402
from app.ops.store import OpsStore  # noqa: E402


def main():
    with TemporaryDirectory(prefix='eia-v06a2-') as directory:
        db = OpsStore(Path(directory) / 'ops.sqlite')
        db.init()
        detector = DetectionEngine(db)
        clock = datetime(2026, 11, 12, tzinfo=timezone.utc)
        first = detector.scan(at=clock)
        assert any(x['rule_code'] == 'inventory_shortage' and x['order_id'] == 'ORD-1003'
                   for x in first['changes'])
        assert detector.scan(at=clock)['changes_total'] == 0
        print('PASS dynamic shortage detection; duplicate scans produce zero new events')

        # Prepare a delayed order in the temporary DB; simulated time is explicit.
        late = detector.scan(at=datetime(2026, 11, 14, tzinfo=timezone.utc))
        assert any(x['rule_code'] == 'promise_overdue' for x in late['changes'])
        assert not detector.scan(at=datetime(2026, 11, 14, tzinfo=timezone.utc))['changes']
        print('PASS overdue risk sourced from promised_at and order state')

        # Actual fulfillment writes trigger further signal evaluation in their own
        # transactions; the original A1 audit and idempotency remain intact.
        operations = OpsService(db)
        operations.reserve('ORD-1001', 'WH-WEST', 'verify-a2-reserve-01')
        operations.dispatch('ORD-1001', 'sim_carrier', 'verify-a2-dispatch-01')
        with db._connection() as con:
            raw = con.execute("SELECT updated_at FROM shipments WHERE order_id='ORD-1001'").fetchone()[0]
        last = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        detector.scan(at=last + timedelta(hours=7))
        signal = next(x for x in db.list_signals() if x['order_id'] == 'ORD-1001'
                      and x['rule_code'] == 'shipment_stalled')
        assert signal['status'] == 'active'
        print('PASS shipped-but-stalled detection with linked incident')

        operations.advance_shipment('ORD-1001', 'in_transit', 'verify-a2-transit-01')
        operations.advance_shipment('ORD-1001', 'delivered', 'verify-a2-delivered-01')
        assert db.get_signal(signal['signal_id'])['status'] == 'cleared'
        operations.verify_incident('INC-1001', 'verify-a2-close-01')
        assert db.get_incident('INC-1001')['state'] == 'resolved'
        print('PASS business command clears stale signal; A1 verified closure unchanged')

        restored = OpsStore(db.path)
        restored.init()
        assert restored.detector_summary()['events_total'] == db.detector_summary()['events_total']
        print('PASS signal history survives reinitialization without resetting business data')
        print('A2 OFFLINE ACCEPTANCE PASS; original analytics database untouched')


if __name__ == '__main__':
    main()
