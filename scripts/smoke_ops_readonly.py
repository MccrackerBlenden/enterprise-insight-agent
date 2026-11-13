"""Free, read-only HTTP smoke of the isolated synthetic-operations domain."""
from __future__ import annotations

import json
import os
import urllib.request

BASE = os.getenv('EIA_LOCAL_URL', 'http://127.0.0.1:8080').rstrip('/')


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=8) as reply:
        if reply.status != 200:
            raise AssertionError(f'{path}: HTTP {reply.status}')
        return json.load(reply)


def main() -> None:
    health = get('/api/health')
    assert health['status'] == 'ok'
    summary = get('/api/ops/summary')
    assert summary['synthetic'] is True and summary['operations_schema'] == 2
    assert summary['orders'] >= 3
    incidents = get('/api/ops/incidents')
    assert incidents['synthetic'] is True and len(incidents['items']) >= 3
    detail = get('/api/ops/incidents/INC-1001')
    assert detail['synthetic'] and detail['order_id'] == 'ORD-1001'
    assert len(detail['inventory']) >= 2
    print('PASS legacy /api/health: available')
    print('PASS ops /summary: schema v2 and synthetic orders')
    print('PASS ops /incidents and /incidents/INC-1001: linked order + inventory')
    print('PASS local read-only operations smoke; no commands or model API invoked')


if __name__ == '__main__':
    main()
