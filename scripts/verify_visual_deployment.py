#!/usr/bin/env python3
"""Prove HTTP on target port serves the intended branded Docker image, not old UI."""
import argparse, hashlib, json, urllib.request, urllib.error
from pathlib import Path
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--base-url', default='http://127.0.0.1:18080')
args=parser.parse_args()
assert args.base_url.startswith(('http://127.0.0.1:','http://localhost:')), 'Only local verification allowed'
base=args.base_url.rstrip('/')
def get(path):
    with urllib.request.urlopen(base+path,timeout=15) as r:
        assert r.status==200, f'{path} HTTP {r.status}'
        return r.read()
manifest=json.loads((root/'frontend/public/release.json').read_text())
try:
    served=json.loads(get('/release.json'))
    assert served['release_id']==manifest['release_id'],f'Wrong version: {served}'
    print('PASS: browser HTTP serves expected revision:',served['release_id'])
    icon=get('/eia-brand-icon.png')
    local=(root/'frontend/public/eia-brand-icon.png').read_bytes()
    assert hashlib.sha256(icon).digest()==hashlib.sha256(local).digest(),'HTTP Logo differs from supplied original'
    print('PASS: browser HTTP Logo matches supplied asset (SHA-256)')
    html=get('/')
    assert b'<html' in html.lower(), 'Homepage is not HTML'
    print('PASS: Homepage HTML loaded from expected port')
except (AssertionError,KeyError,ValueError,urllib.error.URLError) as e:
    raise SystemExit('FAIL: visual deployment mismatch: '+str(e))
print('PASS: VERIFIED PORTFOLIO BUILD (Docker-served branding; not a screenshot or usability audit)')
