"""Read-only SQLite access with a strict statement authorizer and bounded results."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .data import DB_PATH, ensure_demo_db

ALLOWED_TABLES = {"companies", "monthly_financials"}
ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
MAX_ROWS = 100


def _authorize(action: int, name: str | None, arg2: str | None,
               _database: str | None, _source: str | None) -> int:
    if action not in ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and name not in ALLOWED_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    ensure_demo_db(path)
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=4)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")
    db.set_authorizer(_authorize)
    calls = 0
    def progress() -> int:
        nonlocal calls
        calls += 1
        return 1 if calls > 500 else 0
    db.set_progress_handler(progress, 1000)
    return db


def run_readonly_query(sql: str, path: Path = DB_PATH) -> dict[str, Any]:
    # A single terminal semicolon is normal SQL syntax, not a second statement.
    # Strip only that terminator; any remaining semicolons or comment markers
    # are still rejected. The SQLite read-only connection and authorizer remain
    # the final safeguards against writes and unauthorized database access.
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    if (len(sql) > 2500 or not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE)
            or re.search(r";|--|/\*|\*/", sql)):
        raise ValueError("仅允许单条只读 SELECT / WITH 查询；可带一个结尾分号，禁止注释、多语句和写入操作")
    with _connect(path) as db:
        try:
            cursor = db.execute(sql)
            fetched = cursor.fetchmany(MAX_ROWS + 1)
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"SQL 查询失败：{exc}") from exc
    return {"rows": [dict(r) for r in fetched[:MAX_ROWS]],
            "row_count": min(len(fetched), MAX_ROWS),
            "truncated": len(fetched) > MAX_ROWS,
            "columns": [d[0] for d in (cursor.description or [])]}


def schema_text() -> str:
    return """Synthetic demo database, currency units CNY 10k (万元):
companies(company_id TEXT PK, company_name TEXT, industry TEXT)
monthly_financials(company_id TEXT, year INTEGER [2023-2025], month INTEGER [1-12],
revenue REAL, cost REAL, expenses REAL, net_profit REAL)
JOIN companies ON company_id. Relationship: net_profit = revenue - cost - expenses.
Profit margin = SUM(net_profit)/SUM(revenue)*100, NOT AVG(monthly margins).
2024 vs 2025 margin change should be expressed in percentage points.
All data is fictional and not suitable for real investment decisions.
Only SELECT / WITH statements; maximum 100 returned rows."""


def overview(path: Path = DB_PATH) -> dict[str, Any]:
    with _connect(path) as db:
        companies = db.execute("SELECT count(*) FROM companies").fetchone()[0]
        yearly = db.execute("""
            SELECT year, ROUND(SUM(revenue),2) revenue,
            ROUND(SUM(net_profit),2) profit,
            ROUND(100.0*SUM(net_profit)/SUM(revenue),2) margin
            FROM monthly_financials GROUP BY year ORDER BY year
        """).fetchall()
        ranking = db.execute("""
            SELECT c.company_name,
              ROUND(100.0*SUM(CASE WHEN f.year=2025 THEN f.net_profit ELSE 0 END)/
                SUM(CASE WHEN f.year=2025 THEN f.revenue ELSE 0 END),2) margin_2025,
              ROUND(100.0*SUM(CASE WHEN f.year=2024 THEN f.net_profit ELSE 0 END)/
                SUM(CASE WHEN f.year=2024 THEN f.revenue ELSE 0 END),2) margin_2024
            FROM monthly_financials f JOIN companies c ON f.company_id=c.company_id
            WHERE f.year IN (2024,2025) GROUP BY c.company_id
            ORDER BY margin_2025-margin_2024 ASC
        """).fetchall()
    return {"companies": companies, "records": companies*36,
            "years": [dict(r) for r in yearly],
            "ranking": [{"company":r["company_name"], "current":r["margin_2025"],
                         "previous":r["margin_2024"],
                         "delta":round(r["margin_2025"]-r["margin_2024"],2)}
                        for r in ranking],
            "synthetic": True}
