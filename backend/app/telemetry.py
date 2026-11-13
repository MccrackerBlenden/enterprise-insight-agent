"""Run-scoped, privacy-minimal LangChain telemetry.

No prompts, model messages, SQL text, API keys or raw tool outputs are retained.
Amounts are *estimates* only when explicit local tariff settings are provided.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from threading import Lock
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler


def field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def token_counts(raw: Any) -> dict[str, int] | None:
    """Normalize provider or LangChain usage fields; never interpret absent as zero."""
    if not raw:
        return None
    input_value = field(raw, 'input_tokens')
    output_value = field(raw, 'output_tokens')
    if input_value is None:
        input_value = field(raw, 'prompt_tokens')
    if output_value is None:
        output_value = field(raw, 'completion_tokens')
    if not (isinstance(input_value, int) and isinstance(output_value, int)):
        return None
    if input_value < 0 or output_value < 0:
        return None
    total = field(raw, 'total_tokens')
    if not isinstance(total, int) or total < 0:
        total = input_value + output_value
    return {'input_tokens': input_value, 'output_tokens': output_value, 'total_tokens': total}


def response_tokens(response: Any) -> dict[str, int] | None:
    """Prefer AIMessage.usage_metadata, then provider response metadata."""
    for batch in field(response, 'generations', []) or []:
        for generation in batch:
            message = field(generation, 'message')
            if message is None:
                continue
            usage = token_counts(field(message, 'usage_metadata'))
            if usage is not None:
                return usage
            metadata = field(message, 'response_metadata', {}) or {}
            usage = token_counts(field(metadata, 'token_usage'))
            if usage is not None:
                return usage
    output = field(response, 'llm_output', {}) or {}
    return token_counts(field(output, 'token_usage') or field(output, 'usage'))


def _optional_estimate_cny(usage: dict[str, Any]) -> tuple[float | None, str | None]:
    """Manual flat-rate estimate; never assert this is the provider's invoice."""
    if not usage.get('complete'):
        return None, None
    input_str = os.getenv('EIA_PRICE_INPUT_CNY_PER_1M', '').strip()
    output_str = os.getenv('EIA_PRICE_OUTPUT_CNY_PER_1M', '').strip()
    if not input_str or not output_str:
        return None, None
    try:
        input_rate, output_rate = Decimal(input_str), Decimal(output_str)
        if not input_rate.is_finite() or not output_rate.is_finite() or input_rate < 0 or output_rate < 0:
            return None, None
        cost = (Decimal(usage['input_tokens']) * input_rate
                + Decimal(usage['output_tokens']) * output_rate) / Decimal(1_000_000)
        return float(cost.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)), 'manual_flat_rate_estimate'
    except (InvalidOperation, ValueError):
        return None, None


class RunTelemetry(BaseCallbackHandler):
    """Track callbacks per invocation, including partial usage and failures."""

    def __init__(self, model_name: str):
        super().__init__()
        self.model_name = model_name
        self.started_at = time.perf_counter()
        self.lock = Lock()
        self.model_started: dict[str, float] = {}
        self.tool_started: dict[str, tuple[str, float]] = {}
        self.model_calls = 0
        self.model_failures = 0
        self.tool_calls = 0
        self.tool_failures = 0
        self.tool_records: list[dict[str, Any]] = []
        self.model_durations_ms: list[float] = []
        self.usage_reported_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0

    def _model_start(self, run_id: Any) -> None:
        key = str(run_id)
        with self.lock:
            if key not in self.model_started:
                self.model_started[key] = time.perf_counter()
                self.model_calls += 1

    def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: Any, **kwargs: Any) -> None:
        self._model_start(run_id)

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: Any, **kwargs: Any) -> None:
        self._model_start(run_id)

    def _model_end(self, run_id: Any, tokens: dict[str, int] | None, *, failed: bool = False) -> None:
        key = str(run_id)
        with self.lock:
            start = self.model_started.pop(key, None)
            if start is not None:
                self.model_durations_ms.append(round((time.perf_counter() - start) * 1000, 2))
            if failed:
                self.model_failures += 1
            if tokens is not None:
                self.usage_reported_calls += 1
                self.input_tokens += tokens['input_tokens']
                self.output_tokens += tokens['output_tokens']
                self.total_tokens += tokens['total_tokens']

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        self._model_end(run_id, response_tokens(response))

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self._model_end(run_id, None, failed=True)

    def on_tool_start(self, serialized: Any, input_str: str, *, run_id: Any, **kwargs: Any) -> None:
        # Never store `input_str` or raw arguments.
        name = str(field(serialized, 'name', 'tool'))[:80]
        with self.lock:
            self.tool_calls += 1
            self.tool_started[str(run_id)] = (name, time.perf_counter())

    def _tool_end(self, run_id: Any, *, error: bool = False) -> None:
        with self.lock:
            name, start = self.tool_started.pop(str(run_id), ('tool', time.perf_counter()))
            if error:
                self.tool_failures += 1
            self.tool_records.append({
                'name': name,
                'duration_ms': round((time.perf_counter() - start) * 1000, 2),
                'callback_error': error,
            })

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        # Some existing tools encode business errors as JSON text. We intentionally
        # do not parse or retain output: the evaluator still checks SSE tool_result.
        self._tool_end(run_id)

    def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self._tool_end(run_id, error=True)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            calls = self.model_calls
            seen = self.usage_reported_calls
            usage = (None if not seen else {
                'input_tokens': self.input_tokens,
                'output_tokens': self.output_tokens,
                'total_tokens': self.total_tokens,
                'reported_calls': seen,
                'model_calls': calls,
                'complete': seen == calls and self.model_failures == 0,
            })
            tools = list(self.tool_records)
            pending = len(self.tool_started)
            model_ms = list(self.model_durations_ms)
            failures = self.model_failures
            tool_calls = self.tool_calls
            tool_failures = self.tool_failures
        estimated, pricing = _optional_estimate_cny(usage or {})
        return {
            'schema_version': 1,
            'model_name': self.model_name,
            'model_calls': calls,
            'model_failures': failures,
            'model_durations_ms': model_ms,
            'tool_calls': tool_calls,
            'tool_callback_failures': tool_failures,
            'tools': tools,
            'pending_tool_callbacks': pending,
            'elapsed_ms': round((time.perf_counter() - self.started_at) * 1000, 2),
            'token_usage': usage,
            'cost_cny_estimate': estimated,
            'cost_basis': pricing or 'unknown_not_billed_cost',
        }
