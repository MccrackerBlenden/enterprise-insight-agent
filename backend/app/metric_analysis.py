"""Bounded, deterministic metric analysis for agent-selected business questions.

The model chooses metric/grouping/period; SQL aggregation, chart specification,
financial identities and provenance are computed and checked in Python.
No model-authored chart JavaScript or unrestricted SQL is accepted here.
"""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from .data import DB_PATH
from .db import _connect

METRICS: dict[str, tuple[str, str]] = {
    "revenue": ("营业收入", "万元"),
    "net_profit": ("净利润", "万元"),
    "margin": ("净利润率", "%"),
    "cost_rate": ("营业成本率", "%"),
    "expense_rate": ("期间费用率", "%"),
}
GROUPINGS = {
    "company": ("c.company_name", "c.company_id, c.company_name"),
    "industry": ("c.industry", "c.industry"),
    "year": ("CAST(f.year AS TEXT)", "f.year"),
    "industry_year": ("CAST(f.year AS TEXT)", "c.industry, f.year"),
}


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("无效的计算结果")
    rounded = round(value, 2)
    return 0.0 if rounded == 0 else rounded


def analyze_metric(
    metric: str,
    group_by: str,
    start_year: int = 2025,
    end_year: int = 2025,
    order: str = "desc",
    top_n: int = 12,
    path: Path = DB_PATH,
) -> dict[str, Any]:
    """Return a safe, traceable chart/table contract from the fictional DB."""
    if metric not in METRICS:
        raise ValueError("不支持的指标，可用指标：" + ", ".join(METRICS))
    if group_by not in GROUPINGS:
        raise ValueError("不支持的分组，可用分组：" + ", ".join(GROUPINGS))
    if not (2023 <= start_year <= end_year <= 2025):
        raise ValueError("时间范围只能在 2023–2025 年内")
    if order not in ("asc", "desc"):
        raise ValueError("排序仅支持 asc 或 desc")
    if not 1 <= top_n <= 12:
        raise ValueError("top_n 范围为 1–12")

    label_sql, group_sql = GROUPINGS[group_by]
    series_sql = "c.industry" if group_by == "industry_year" else "NULL"
    sql = f"""SELECT {label_sql} AS label, {series_sql} AS series,
    COUNT(*) AS months, COUNT(DISTINCT c.company_id) AS company_count,
    SUM(f.revenue) AS revenue, SUM(f.cost) AS cost,
    SUM(f.expenses) AS expenses, SUM(f.net_profit) AS net_profit
FROM monthly_financials f
JOIN companies c ON c.company_id = f.company_id
WHERE f.year BETWEEN ? AND ?
GROUP BY {group_sql}
ORDER BY label, series"""
    with _connect(path) as conn:
        raw = [dict(row) for row in conn.execute(sql, (start_year, end_year)).fetchall()]
    if not raw:
        raise ValueError("该时间范围内没有可分析的数据")

    years = end_year - start_year + 1
    rows: list[dict[str, Any]] = []
    total_residual = 0.0
    complete = True
    for row in raw:
        revenue, cost, expenses, profit = (float(row[k]) for k in ("revenue", "cost", "expenses", "net_profit"))
        if revenue <= 0:
            raise ValueError("营收非正，无法计算占比指标")
        residual = abs(profit - (revenue - cost - expenses))
        total_residual = max(total_residual, residual)
        if residual > .05:
            raise ValueError("财务勾稽关系不成立，拒绝生成图表")
        expected_months = 12 * int(row["company_count"]) * (1 if group_by in {"year", "industry_year"} else years)
        complete = complete and int(row["months"]) == expected_months
        values = {
            "revenue": revenue,
            "net_profit": profit,
            "margin": 100 * profit / revenue,
            "cost_rate": 100 * cost / revenue,
            "expense_rate": 100 * expenses / revenue,
        }
        rows.append({
            "label": str(row["label"]),
            "series": str(row["series"]) if row["series"] is not None else None,
            "value": _finite(values[metric]),
            "revenue": _finite(revenue),
            "net_profit": _finite(profit),
            "months": int(row["months"]),
            "company_count": int(row["company_count"]),
        })

    if group_by in ("company", "industry"):
        rows.sort(key=lambda item: (item["value"] if order == "asc" else -item["value"], item["label"]))
        rows = rows[:top_n]
    else:
        rows.sort(key=lambda item: (item["series"] or "", int(item["label"])))

    title, unit = METRICS[metric]
    if group_by == "industry_year":
        by_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_series[row["series"] or ""] .append({"label": row["label"], "value": row["value"]})
        chart_series = [{"name": k, "points": v} for k, v in sorted(by_series.items())]
        chart_kind = "line"
    else:
        chart_series = [{"name": title, "points": [{"label": r["label"], "value": r["value"]} for r in rows]}]
        chart_kind = "line" if group_by == "year" else "bar"

    return {
        "kind": "metric_analysis",
        "title": f"{title} · {start_year}{'–' + str(end_year) if start_year != end_year else ''} 年",
        "metric": metric,
        "metric_label": title,
        "group_by": group_by,
        "unit": unit,
        "period": {"start_year": start_year, "end_year": end_year},
        "rows": rows,
        "chart": {"kind": chart_kind, "series": chart_series},
        "audit": {
            "status": "passed" if complete else "partial",
            "records_covered": sum(r["months"] for r in rows),
            "max_identity_residual_10k_cny": _finite(total_residual),
        },
        "method": "金额指标取分组内年度/月度原始记录之和；利润率、成本率、费用率均采用分组后的比率（分子汇总后除以营收汇总），不对月度比例做算术平均。",
        "source": "本地固定种子合成数据库，企业及数字均为虚构",
        "query_sql": sql,
        "query_parameters": [start_year, end_year],
    }
