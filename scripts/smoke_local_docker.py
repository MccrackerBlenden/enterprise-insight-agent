#!/usr/bin/env python3
"""Free, read-only HTTP smoke check for the V0.5-A local Docker entry point.

IMPORTANT: This script never calls /api/run/stream and never spends model tokens.
It is not a replacement for a real browser/LLM end-to-end acceptance run.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def fetch(base: str, endpoint: str, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(base.rstrip("/") + endpoint, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.status, res.read(512_000)


def verify(base: str, timeout: float = 8.0) -> tuple[list[str], list[str]]:
    checks: list[str] = []
    warnings: list[str] = []
    status, homepage = fetch(base, "/", timeout)
    assert status == 200 and b"<html" in homepage.lower(), "Web homepage missing"
    checks.append("Nginx homepage: HTTP 200 + HTML")

    status, body = fetch(base, "/api/health", timeout)
    health: dict[str, Any] = json.loads(body)
    assert status == 200 and health.get("status") == "ok", "API is not healthy"
    assert health.get("synthetic_data") is True, "Unexpected data source flag"
    assert isinstance(health.get("model_ready"), bool), "Missing model readiness flag"
    checks.append("/api/health: healthy synthetic API")
    if not health["model_ready"]:
        warnings.append("Model is not configured; paid Agent task cannot run yet")

    status, body = fetch(base, "/api/overview", timeout)
    data: dict[str, Any] = json.loads(body)
    assert status == 200 and data.get("synthetic") is True, "Overview is not synthetic"
    assert data.get("companies") == 12 and data.get("records") == 432, "Demo dataset dimensions changed"
    assert [r.get("year") for r in data.get("years", [])] == [2023, 2024, 2025], "Year range changed"
    assert len(data.get("ranking", [])) == 12, "Expected 12-company overview ranking"
    checks.append("/api/overview: 12 synthetic companies, 432 months, 2023–2025")
    return checks, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=8)
    args = parser.parse_args(argv)
    if not args.base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        parser.error("Only localhost is accepted; this is not a public-deployment probe")
    try:
        checks, warnings = verify(args.base_url, args.timeout)
    except (urllib.error.URLError, ValueError, KeyError, TypeError, AssertionError) as exc:
        print(f"FAIL local Docker smoke: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for check in checks:
        print("PASS", check)
    for warning in warnings:
        print("WARN", warning)
    print("PASS: local read-only smoke. NOT validated: real Agent/SSE, charts, report, restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
