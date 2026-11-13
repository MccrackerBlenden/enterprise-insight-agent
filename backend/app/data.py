"""Deterministic synthetic demo database with fictional companies and a fixed seed."""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "demo.sqlite"
SEED = 20261111

COMPANIES = [
    ("C01", "星澜科技", "科技服务"), ("C02", "云岚数据", "科技服务"),
    ("C03", "知序智能", "科技服务"), ("C04", "澄远软件", "科技服务"),
    ("C05", "森禾制造", "先进制造"), ("C06", "铂川精工", "先进制造"),
    ("C07", "凌峰材料", "先进制造"), ("C08", "启元工业", "先进制造"),
    ("C09", "晨汐零售", "消费零售"), ("C10", "白屿商贸", "消费零售"),
    ("C11", "和风生活", "消费零售"), ("C12", "拾光品牌", "消费零售"),
]


def create_demo_db(path: Path = DB_PATH) -> Path:
    """Recreate demo DB from a fixed seed; all names and figures are fictional."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rng = random.Random(SEED)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE companies (
              company_id TEXT PRIMARY KEY,
              company_name TEXT NOT NULL,
              industry TEXT NOT NULL
            );
            CREATE TABLE monthly_financials (
              company_id TEXT NOT NULL,
              year INTEGER NOT NULL,
              month INTEGER NOT NULL,
              revenue REAL NOT NULL,
              cost REAL NOT NULL,
              expenses REAL NOT NULL,
              net_profit REAL NOT NULL,
              PRIMARY KEY(company_id,year,month),
              FOREIGN KEY(company_id) REFERENCES companies(company_id)
            );
            CREATE INDEX idx_financial_year ON monthly_financials(year,company_id);
        """)
        db.executemany("INSERT INTO companies VALUES (?,?,?)", COMPANIES)
        rows = []
        for i, (company_id, _name, _industry) in enumerate(COMPANIES):
            base = 650 + 115 * (i % 4) + 58 * (i // 4)
            for year in (2023, 2024, 2025):
                for month in range(1, 13):
                    season = 1 + 0.085 * ((month - 6) / 6)
                    revenue = round(base * season * (1 + 0.075 * (year - 2023)) * rng.uniform(.96, 1.04), 2)
                    cost_ratio = .50 + (i % 4) * .017 + rng.uniform(-.010, .010)
                    expenses_ratio = .245 + (i % 3) * .014 + rng.uniform(-.007, .007)
                    # Three fictional companies deliberately become cost-stressed in 2025.
                    if year == 2025 and i in (1, 4, 8):
                        cost_ratio += (.055, .07, .085)[(1, 4, 8).index(i)]
                        expenses_ratio += .012
                    cost = round(revenue * cost_ratio, 2)
                    expenses = round(revenue * expenses_ratio, 2)
                    profit = round(revenue - cost - expenses, 2)
                    rows.append((company_id, year, month, revenue, cost, expenses, profit))
        db.executemany("INSERT INTO monthly_financials VALUES (?,?,?,?,?,?,?)", rows)
        db.commit()
    return path


def ensure_demo_db(path: Path = DB_PATH) -> Path:
    return path if path.exists() else create_demo_db(path)
