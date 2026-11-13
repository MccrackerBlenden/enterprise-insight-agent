#!/usr/bin/env python3
"""Conservative, local pre-push guard; never prints any credential contents.

Checks tracked files by default. It does NOT scan Git history or prove a repo safe:
run a dedicated history/secret scanner and review artifacts before publication.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

BLOCKED_COMPONENTS = {"node_modules", ".venv", "__pycache__", ".git", ".pytest_cache"}
BLOCKED_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".zip", ".log"}
SECRET_PATTERNS = [
    ("provider/API token pattern", re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("private key header", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("credential assignment", re.compile(
        rb"(?im)^[ \t]*(?:export[ \t]+)?(?:OPENAI_API_KEY|DASHSCOPE_API_KEY|GITHUB_TOKEN|AWS_SECRET_ACCESS_KEY)[ \t]*[:=][ \t]*['\"]?([^\s'\"#]+)"
    )),
]
SAMPLE_VALUES = {b"YOUR_KEY", b"YOUR_API_KEY", b"REPLACE_ME", b"<YOUR_API_KEY>", b"xxx", b"example"}


def blocked_path(name: str) -> bool:
    path = Path(name)
    parts = path.parts
    if set(parts) & BLOCKED_COMPONENTS:
        return True
    if any(p.startswith(".env") and p != ".env.example" for p in parts):
        return True
    if path.name in {"id_rsa", "id_ed25519", "credentials.json", ".npmrc"}:
        return True
    if path.suffix.lower() in BLOCKED_EXTENSIONS or path.name.endswith((".sqlite-wal", ".sqlite-shm", ".db-wal", ".db-shm")):
        return True
    if len(parts) >= 3 and parts[:3] == ("backend", "evals", "results") and path.name != ".gitignore":
        return True
    if "raw" in parts and "data" in parts:
        return True
    return False


def secret_hits(content: bytes) -> list[str]:
    hits: list[str] = []
    for label, expression in SECRET_PATTERNS:
        for match in expression.finditer(content):
            if label == "credential assignment" and match.group(1).strip() in SAMPLE_VALUES:
                continue
            line = content.count(b"\n", 0, match.start()) + 1
            hits.append(f"line {line}: {label}")
    return hits


def tracked_files(root: Path) -> list[str]:
    proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                          capture_output=True, check=True)
    return sorted(x.decode("utf-8", errors="replace") for x in proc.stdout.split(b"\x00") if x)


def scan(root: Path, files: list[str], require_locks: bool = False) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    for name in files:
        file = root / name
        if blocked_path(name):
            failures.append(f"Blocked path: {name}")
            continue
        if not file.is_file():
            failures.append(f"Tracked file missing from checkout: {name}")
            continue
        if file.stat().st_size > 3_000_000:
            warnings.append(f"Review large tracked file manually: {name}")
            continue
        for hit in secret_hits(file.read_bytes()):
            failures.append(f"Potential secret: {name} ({hit}; value suppressed)")
    for lock in ("backend/uv.lock", "frontend/package-lock.json"):
        if lock not in files:
            message = f"Required lockfile absent from tracked set: {lock}"
            (failures if require_locks else warnings).append(message)
    return failures, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--require-locks", action="store_true", help="Fail without both committed lockfiles")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        files = tracked_files(root)
    except (OSError, subprocess.CalledProcessError):
        print("FAIL: repository needs .git history to inspect tracked files", file=sys.stderr)
        return 2
    failures, warnings = scan(root, files, args.require_locks)
    print(f"Checked {len(files)} tracked paths (current checkout only; NOT historical commits).")
    for warning in warnings:
        print("WARN", warning)
    for failure in failures:
        print("FAIL", failure)
    if failures:
        print("BLOCKED: do not publish; review without pasting any secret values.")
        return 1
    print("PASS: tracked-file heuristic checks only; history + license + data review still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
