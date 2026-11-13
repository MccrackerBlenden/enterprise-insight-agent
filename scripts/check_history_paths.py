#!/usr/bin/env python3
"""Check high-risk pathnames in *reachable Git history*; does NOT scan historical blob contents.

Outputs filenames only, never token values. If it flags a path, treat publishing
original history as blocked until you review all relevant commits. This is not a
substitute for a dedicated history secret scanner or license review.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from check_release_safety import blocked_path


def main() -> int:
    result = subprocess.run(['git', '-C', str(ROOT), 'rev-list', '--objects', '--all'], capture_output=True, text=True)
    if result.returncode:
        print('BLOCKED: no readable Git history available for scan')
        return 2
    high_risk = set()
    objects=0
    for line in result.stdout.splitlines():
        objects += 1
        _, sep, path = line.partition(' ')
        if sep and blocked_path(path):
            high_risk.add(path)
    if objects == 0:
        print('INFO: no reachable commits yet; this new repository has no history to inspect.')
        print('WARNING: original WSL Git history was not supplied or inspected.')
        return 0
    if high_risk:
        print(f'BLOCKED: {len(high_risk)} potentially sensitive historical file path(s) (not contents):')
        for p in sorted(high_risk): print(' -', p)
        print('Do not push this history without a manual historical-content review; consider a fresh public snapshot.')
        return 1
    print(f'PASS: checked high-risk pathnames in {objects} reachable Git objects.')
    print('WARNING: historical file contents/secrets NOT scanned; follow with dedicated history scanning and review.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
