"""Pure-Python heuristics for gracefully ending an exploratory agent run.

Only real LangChain ToolMessage results count as valid evidence, and both SQL
queries and deterministic metric analyses can provide that evidence.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


def _text(content: Any) -> str:
    return content if isinstance(content, str) else ""


def inspect_history(messages: Sequence[Any]) -> dict[str, int]:
    stats = {"model_rounds": 0, "schema_calls": 0, "sql_attempts": 0, "sql_success": 0}
    for message in messages:
        kind = getattr(message, "type", None)
        if kind == "ai":
            stats["model_rounds"] += 1
        if kind != "tool":
            continue
        name = getattr(message, "name", None)
        if name == "get_schema":
            stats["schema_calls"] += 1
        elif name in ("query_database", "analyze_metrics"):
            key = "sql" if name == "query_database" else "metric"
            if key == "metric":
                stats.setdefault("metric_success", 0)
            stats[f"{key}_attempts"] = stats.get(f"{key}_attempts", 0) + 1
            try:
                payload = json.loads(_text(getattr(message, "content", "")))
            except (TypeError, ValueError):
                continue
            valid = (isinstance(payload, dict) and not payload.get("error") and
                     (isinstance(payload.get("rows"), list) if key == "sql"
                      else payload.get("kind") == "metric_analysis" and
                           payload.get("audit", {}).get("status") == "passed" and
                           bool(payload.get("rows"))))
            if valid:
                stats[f"{key}_success"] = stats.get(f"{key}_success", 0) + 1
    return stats


def needs_final_answer(stats: dict[str, int]) -> bool:
    """Limit repeated queries while allowing up to two meaningful evidence calls."""
    return (stats.get("sql_success", 0) + stats.get("metric_success", 0) >= 2
            or stats.get("sql_attempts", 0) + stats.get("metric_attempts", 0) >= 3
            or stats["model_rounds"] >= 4)
