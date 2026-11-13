"""Deterministic audited comparison for the 2024–2025 margin decline task.

Independent of model narrative; figures are computed from the synthetic SQLite
source and never supplied by an LLM. This is intentionally a scoped V0.2-A
feature, not a general-purpose natural language chart generator.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .data import DB_PATH
from .db import _connect

SQL_EVIDENCE = """SELECT c.company_name, f.year,
       COUNT(*) AS months,
       SUM(f.revenue) AS revenue,
       SUM(f.cost) AS cost,
       SUM(f.expenses) AS expenses,
       SUM(f.net_profit) AS net_profit
FROM monthly_financials AS f
JOIN companies AS c ON c.company_id = f.company_id
WHERE f.year IN (2024, 2025)
GROUP BY c.company_id, c.company_name, f.year
ORDER BY c.company_name, f.year"""


def applies_to(question: str) -> bool:
    """Only the exact supported two-year decline comparison gets a verified panel.

    Other tasks still use the existing open-ended Agent and are never given a
    misleading fixed chart. More intents can be added with separate contracts.
    """
    normalized = re.sub(r"\s+", "", question)
    return all(token in normalized for token in ("2024", "2025", "利润率")) and ("下降" in normalized or "下滑" in normalized)


def _display(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("分析结果中存在无效数值")
    result = round(value, 2)
    return 0.0 if result == 0 else result


def build_verified_decline(path: Path = DB_PATH, limit: int = 3) -> dict[str, Any]:
    """Return a chart/report evidence contract with explicit method and provenance."""
    if not 1 <= limit <= 12:
        raise ValueError("比较企业数量必须在 1–12 之间")
    with _connect(path) as db:
        rows = [dict(r) for r in db.execute(SQL_EVIDENCE).fetchall()]

    by_company: dict[str, dict[int, dict[str, Any]]] = {}
    for row in rows:
        by_company.setdefault(str(row["company_name"]), {})[int(row["year"])] = row

    candidates: list[dict[str, Any]] = []
    max_residual = 0.0
    for company, years in by_company.items():
        if 2024 not in years or 2025 not in years:
            continue
        before, after = years[2024], years[2025]
        if before["months"] != 12 or after["months"] != 12:
            continue
        if before["revenue"] <= 0 or after["revenue"] <= 0:
            continue
        for sample in (before, after):
            if abs(sample["net_profit"] - (sample["revenue"] - sample["cost"] - sample["expenses"])) > 0.02:
                raise ValueError("合成数据勾稽校验失败，无法生成核验报告")

        margin_before = 100 * before["net_profit"] / before["revenue"]
        margin_after = 100 * after["net_profit"] / after["revenue"]
        cost_shift = 100 * (after["cost"] / after["revenue"] - before["cost"] / before["revenue"])
        expense_shift = 100 * (after["expenses"] / after["revenue"] - before["expenses"] / before["revenue"])
        delta = margin_after - margin_before
        residual = delta + cost_shift + expense_shift
        max_residual = max(max_residual, abs(residual))
        if abs(residual) > 1e-7:
            raise ValueError("利润率来源分解校验失败")
        candidates.append({
            "company": company,
            "margin_2024": _display(margin_before),
            "margin_2025": _display(margin_after),
            "delta_pp": _display(delta),
            "cost_rate_change_pp": _display(cost_shift),
            "expense_rate_change_pp": _display(expense_shift),
            "revenue_growth_pct": _display(100 * (after["revenue"] / before["revenue"] - 1)),
            "cost_growth_pct": _display(100 * (after["cost"] / before["cost"] - 1)) if before["cost"] else None,
            "expense_growth_pct": _display(100 * (after["expenses"] / before["expenses"] - 1)) if before["expenses"] else None,
            "months_2024": before["months"],
            "months_2025": after["months"],
            "_delta": delta,
        })
    candidates.sort(key=lambda item: (item["_delta"], item["company"]))
    declines = [row for row in candidates if row["_delta"] < 0][:limit]
    for row in declines:
        row.pop("_delta")

    return {
        "kind": "margin_decline_2024_2025",
        "title": "2024–2025 年净利润率下降核验",
        "source": "固定随机种子生成的本地合成数据库（非真实企业）",
        "unit": "人民币万元；比例单位为百分比 / 百分点",
        "method": "年度净利润率 = SUM(net_profit) / SUM(revenue) × 100；变化 = 2025 利润率 − 2024 利润率（百分点）。成本率及费用率均先按年度汇总后除以营收。",
        "interpretation": "利润率变化 = −成本率变化 −费用率变化；这是会计恒等式分解，不证明经营事件的因果关系。",
        "query_sql": SQL_EVIDENCE,
        "compared_companies": len(candidates),
        "companies": declines,
        "audit": {
            "status": "passed" if len(candidates) == 12 and max_residual <= 1e-7 else "partial",
            "max_identity_residual_pp": max_residual,
            "fully_compared": len(candidates),
        },
    }
