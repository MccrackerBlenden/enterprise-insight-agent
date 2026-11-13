"""Safely bridge *verified* industry/year SQL evidence to a metric chart artifact.

No model call, arbitrary Python execution or unvalidated chart JSON is involved.
The bridge is deliberately conservative: unsupported/ambiguous questions and
incomplete, truncated or numerically inconsistent SQL responses return None.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .data import DB_PATH
from .metric_analysis import analyze_metric

# The vocabulary is intentionally bounded to metrics already supported by the
# deterministic engine; do not silently interpret ambiguous composite requests.
_METRIC_WORDS = re.compile(
    r"净利润率|营业成本率|期间费用率|利润率|成本率|费用率|营业收入|净利润|营收|收入"
)
_METRIC_NAMES = {
    "净利润率": "margin", "利润率": "margin",
    "营业成本率": "cost_rate", "成本率": "cost_rate",
    "期间费用率": "expense_rate", "费用率": "expense_rate",
    "营业收入": "revenue", "营收": "revenue", "收入": "revenue", "净利润": "net_profit",
}
_RANGE = re.compile(r"(?<!\d)(20\d{2})\s*年?\s*(?:至|到|[-–—~～])\s*(20\d{2})\s*年?")


def requested_industry_trend(question: str) -> tuple[str, int, int] | None:
    """Extract a single unambiguous, explicit industry/year trend request."""
    if "行业" not in question or not any(k in question for k in ("趋势", "逐年", "每年", "年度", "分年")):
        return None
    found = set(_METRIC_NAMES[m] for m in _METRIC_WORDS.findall(question))
    ranges = _RANGE.findall(question)
    if len(found) != 1 or len(ranges) != 1:
        return None
    start, end = map(int, ranges[0])
    if not (2023 <= start <= end <= 2025):
        return None
    return found.pop(), start, end


def _field(row: dict[str, Any], *names: str) -> Any:
    """Match the same practical SQL aliases accepted by the independent eval."""
    norm = {str(k).lower().replace("_", "").replace(" ", ""): v for k, v in row.items()}
    for name in names:
        key = name.lower().replace("_", "").replace(" ", "")
        if key in norm:
            return norm[key]
    return None


def _measure(row: dict[str, Any], metric: str) -> float | None:
    revenue = _field(row, "revenue", "total_revenue", "sum_revenue", "revenue_sum", "营业收入", "总营业收入")
    profit = _field(row, "net_profit", "total_net_profit", "sum_net_profit", "netprofit_sum", "净利润")
    cost = _field(row, "cost", "total_cost", "sum_cost", "营业成本")
    expenses = _field(row, "expenses", "total_expenses", "sum_expenses", "期间费用")
    try:
        if metric == "revenue":
            value = float(revenue)
        elif metric == "net_profit":
            value = float(profit)
        else:
            total = float(revenue)
            numerator = float({"margin": profit, "cost_rate": cost, "expense_rate": expenses}[metric])
            value = 100 * numerator / total
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def build_verified_sql_artifact(
    question: str, result: dict[str, Any], path: Path = DB_PATH
) -> dict[str, Any] | None:
    """Produce the existing metric_analysis contract only after strict comparison.

    The chart is rebuilt by our known-safe metric engine and not copied from
    model-written SQL output. Every expected (year, industry, measure) must be
    represented *exactly once* with a matching numeric result.
    """
    request = requested_industry_trend(question)
    if request is None or result.get("error") or result.get("truncated") is not False:
        return None
    actual_rows = result.get("rows")
    if not isinstance(actual_rows, list) or not actual_rows:
        return None
    metric, start, end = request
    try:
        canonical = analyze_metric(metric, "industry_year", start, end, path=path)
    except (ValueError, OSError):
        return None
    if (canonical.get("audit") or {}).get("status") != "passed":
        return None
    reference = {(r["label"], r["series"]): float(r["value"]) for r in canonical["rows"]}
    if len(actual_rows) != len(reference):
        return None
    seen = set()
    for row in actual_rows:
        if not isinstance(row, dict):
            return None
        yr = _field(row, "year", "f.year", "年份", "年度")
        industry = _field(row, "industry", "industry_name", "行业")
        try:
            key = (str(int(yr)), str(industry))
        except (TypeError, ValueError):
            return None
        if key not in reference or key in seen:
            return None
        value = _measure(row, metric)
        if value is None or abs(value - reference[key]) > max(0.035, abs(reference[key]) * 1e-7):
            return None
        seen.add(key)
    if seen != set(reference):
        return None
    # Preserve the canonical deterministic query_sql and report provenance.
    # This artifact means the SQL numeric evidence matched the canonical DB
    # result, NOT that the model's prose has been verified.
    canonical["evidence"] = {
        "route": "query_database",
        "sql_numeric_audit": "passed",
        "matched_rows": len(seen),
        "expected_rows": len(reference),
        "note": "SQL 数值逐组核验通过；图表由确定性指标引擎重新计算；AI 解释未自动核验",
    }
    return canonical
