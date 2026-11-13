"""Read-only HTTP smoke: does not call Agent, change data or require token."""
from __future__ import annotations

import json
from urllib.request import urlopen

BASE = 'http://127.0.0.1:8080'


def get(path: str) -> dict:
    with urlopen(BASE + path, timeout=10) as response:
        assert response.status == 200
        return json.load(response)


def main() -> None:
    data = get('/api/ops/orders/ORD-1001/fulfillment-options')
    assert data['synthetic'] is True
    assert data['order_id'] == 'ORD-1001'
    assert len(data['options']) == 2
    assert len(data['snapshot_fingerprint']) == 64
    assert data['policy_version'] == 'synthetic-handling-v1'
    assert data['recommended_warehouse_id'] == 'WH-WEST'
    print('PASS A3 fulfillment options: 2 warehouses, shortage excluded, fictional policy exposed')
    assert get('/api/ops/summary')['operations_schema'] == 2
    assert get('/api/health')['status'] == 'ok'
    print('PASS original ops + analytics endpoints available')
    print('PASS local read-only A3 smoke; no writes or model API')


if __name__ == '__main__':
    main()
