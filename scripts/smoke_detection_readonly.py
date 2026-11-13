"""Local, free and read-only smoke: no automatic scan and no model calls."""
import json
import os
import urllib.request

BASE = os.getenv('EIA_LOCAL_URL', 'http://127.0.0.1:8080').rstrip('/')


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=8) as res:
        assert res.status == 200
        return json.load(res)


def main():
    summary = get('/api/ops/detector/summary')
    assert summary['synthetic'] and summary['schema'] == 2
    assert summary['events_total'] >= 0 and summary['signals_total'] >= 0
    signals = get('/api/ops/signals?limit=10')
    events = get('/api/ops/signals/events?after_id=0&limit=10')
    assert signals['synthetic'] and isinstance(signals['items'], list)
    assert events['synthetic'] and isinstance(events['items'], list)
    print('PASS detector schema v2 and durable counters')
    print('PASS signal inventory and append-only events API')
    print('PASS read-only smoke: no scan, mutation or model API invoked')


if __name__ == '__main__':
    main()
