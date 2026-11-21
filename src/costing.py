"""Monetary cost estimates for model usage and tool provenance.

This is deliberately an estimate, not a provider invoice. Prices come from the
active provider profile and are expressed per one million tokens. Missing usage
or missing prices remain ``None`` — the UI must show "unknown", never zero.
"""
from __future__ import annotations

import math
from typing import Any, Iterable


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def paid_cost_may_be_unknown(source: Any, error: BaseException) -> bool:
    """A failed paid call is unknown unless non-dispatch is explicitly known."""
    providers = getattr(source, 'providers', ()) or ()
    return (getattr(error, 'remote_outcome', None) != 'not_started'
            and any(name in ('lune', 'deepseek') for name in providers))


def source_runs_cost(runs: Iterable[Any]) -> float | None:
    """Return a complete CNY estimate, never a sum that hides unknown charges."""
    total = 0.0
    for run in runs:
        get = run.get if isinstance(run, dict) else lambda key, default=None: getattr(run, key, default)
        status, name = get('status'), get('name')
        if status in ('cache', 'cached', 'skipped'):
            continue
        # Legacy Lune rows often contain zero despite unknown provider billing.
        if name == 'lune':
            return None
        value = _number(get('cost_cny'))
        # Older DeepSeek empty/error runs also recorded zero without billing
        # evidence; a confirmed not-started attempt is the only exception.
        if (name == 'deepseek' and value == 0 and status in ('ok', 'empty', 'error', 'partial')
                and get('remote_outcome') != 'not_started'):
            return None
        if value is None:
            return None
        total += value
    return round(total, 6)


def tool_cost(trace: Iterable[dict[str, Any]] | None) -> float | None:
    """Sum known tool estimates; return None if any incurred cost is unknown."""
    total = 0.0
    for event in trace or ():
        payload = event.get('payload') if isinstance(event, dict) else None
        item = payload or event
        router = item.get('router') if isinstance(item, dict) else None
        if router:
            if router.get('tool_cost_unknown') is True:
                return None
            for batch in router.get('batches') or []:
                value = source_runs_cost(batch.get('runs') or [])
                if value is None:
                    return None
                total += value
        elif isinstance(item, dict) and 'cost_cny' in item:
            value = _number(item.get('cost_cny'))
            if value is None:
                return None
            total += value
    return round(total, 6)


def pricing(config: Any) -> dict[str, Any]:
    return {
        'input_price': _number(getattr(config, 'input_price', None)),
        'output_price': _number(getattr(config, 'output_price', None)),
        'currency': str(getattr(config, 'currency', 'CNY') or 'CNY'),
        'unit': 'per_1m_tokens',
        'estimate': True,
        'profile_id': str(getattr(config, 'profile_id', 'env') or 'env'),
        'protocol': str(getattr(config, 'protocol', 'chat_completions') or 'chat_completions'),
        'model_name': str(getattr(config, 'model', '') or ''),
    }


def cost_breakdown(config: Any, metrics: dict[str, Any] | None,
                   trace: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    metrics = metrics or {}
    prices = pricing(config)
    input_tokens = metrics.get('input_tokens')
    output_tokens = metrics.get('output_tokens')
    complete = metrics.get('usage_complete')
    # Older imported results may not have the marker; preserve their token data.
    if complete is False:
        input_tokens = output_tokens = None
    input_tokens = int(input_tokens) if type(input_tokens) is int else None
    output_tokens = int(output_tokens) if type(output_tokens) is int else None
    model = None
    if (input_tokens is not None and output_tokens is not None
            and prices['input_price'] is not None and prices['output_price'] is not None):
        model = round((input_tokens / 1_000_000) * prices['input_price']
                      + (output_tokens / 1_000_000) * prices['output_price'], 6)
    tools = tool_cost(trace)
    # Tool estimates are in CNY. Without an explicit exchange rate they cannot
    # be added to a non-CNY model estimate, except when the tool total is zero.
    comparable = tools is not None and (prices['currency'].upper() == 'CNY' or tools == 0)
    known = model is not None and comparable
    return {
        **prices,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'model': model,
        'tools': tools,
        'tools_currency': 'CNY',
        'total': round(model + tools, 6) if known else None,
        'known': known,
    }


def apply_cost(result: dict[str, Any], config: Any,
               trace: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Attach an estimate to a workflow/chat result and its metrics."""
    metrics = result.setdefault('metrics', {})
    cost = cost_breakdown(config, metrics, trace if trace is not None else
                          (result.get('trace') or []))
    metrics['cost'] = cost
    result['cost'] = cost
    return result
