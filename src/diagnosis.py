"""Evidence-based failure diagnosis for Search and Chat results.

This module is deliberately pure and provider-agnostic.  It explains *why* a run
missed its target and proposes explicit, user-confirmed recovery actions.  It does
not silently relax constraints: the caller/UI decides which action to take.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from recovery import recovery_patch


SOURCE_NAMES = ('OpenAlex', 'DBLP', 'Semantic Scholar', 'OpenReview', 'ACL Anthology', 'arXiv')


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _source_errors(trace: Iterable[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """Collect provider-level errors with stable codes from traces.

    ``SourceRun`` now carries ``kind/code``; old traces only have free text, so
    fall back to keyword classification without exposing a traceback.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in trace or ():
        if not isinstance(event, Mapping) or event.get('action') not in ('search', 'search_failed'):
            continue
        messages: list[tuple[str, str]] = []
        error = event.get('error')
        if error:
            messages.extend((part.strip(), '') for part in str(error).split(';') if part.strip())
        router = event.get('router') or {}
        for batch in router.get('batches', []) if isinstance(router, Mapping) else []:
            for run in batch.get('runs', []) if isinstance(batch, Mapping) else []:
                if run.get('status') == 'error' and run.get('error'):
                    messages.append((str(run['error']).strip(),
                                     str(run.get('code') or run.get('kind') or '')))
        for message, raw_code in messages:
            provider = next((name for name in SOURCE_NAMES
                             if name.casefold() in message.casefold()), 'retrieval')
            code = raw_code or _classify(message)
            key = (provider.casefold(), code, message.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append({'provider': provider, 'code': code,
                        'error': message[:500]})
    return out[:12]


def _classify(error: str) -> str:
    from source import classify_source_error
    transport_code = classify_source_error(error)
    if transport_code in ('dns_error', 'connection_error', 'transport'):
        return transport_code
    text = error.casefold()
    if '429' in text or 'rate limit' in text or 'too many requests' in text:
        return 'rate_limited'
    if 'challenge' in text or 'bot protection' in text or 'captcha' in text:
        return 'bot_challenge'
    if 'timeout' in text or 'timed out' in text or 'exceeded' in text:
        return 'timeout'
    if 'could not resolve venue' in text or 'invalid venue' in text:
        return 'venue_resolve'
    if '401' in text or '403' in text or 'unauthorized' in text or 'missing key' in text:
        return 'auth'
    if 'api key' in text or 'credentials' in text or 'cooldown' in text:
        return 'unavailable'
    if 'invalid' in text or 'parse' in text or 'json' in text:
        return 'invalid_response'
    if '404' in text or 'not found' in text:
        return 'not_found'
    return 'source_error'


def _constraint_text(request: Mapping[str, Any]) -> str:
    bits = []
    venue = str(request.get('venue') or '').strip()
    if venue:
        bits.append(f'venue={venue}')
    year_from = request.get('year_from')
    year_to = request.get('year_to')
    if year_from or year_to:
        bits.append(f'years={year_from or "any"}-{year_to or "any"}')
    return ' · '.join(bits)


def _gap(gaps: Iterable[Mapping[str, Any]], requirement: str) -> Mapping[str, Any] | None:
    return next((gap for gap in gaps if gap.get('requirement') == requirement), None)


def _action(code: str, label: str, description: str, message: str, *,
            retry: bool = True, patch: Mapping[str, Any] | None = None) -> dict:
    return {'code': code, 'label': label, 'description': description,
            'message': message, 'retry': retry, 'patch': dict(patch or {})}


def diagnose(*, request: Mapping[str, Any] | Any | None, gaps: Iterable[Mapping[str, Any]] | None,
             trace: Iterable[Mapping[str, Any]] | None, candidates: Iterable[Mapping[str, Any]] | None = None,
             selected: Iterable[Mapping[str, Any]] | None = None,
             source_health: Mapping[str, Mapping[str, Any]] | None = None,
             stop_reason: str = '', limits: Mapping[str, Any] | None = None) -> dict:
    """Return a compact diagnosis + recovery options.

    ``code == 'ok'`` means no recovery is needed.  The failure taxonomy is
    intentionally small and stable: constraints, quota/mix, candidate supply,
    budget, source health, or a generic unmet requirement.
    """
    request = request or {}
    gaps = list(gaps or [])
    if stop_reason == 'user_cancelled':
        return {'code': 'user_cancelled',
                'summary': '已按你的要求停止。已完成且通过证据检查的结果已保留；可修改需求后继续。',
                'actions': [], 'source_errors': [], 'source_health': {},
                'counts': {'target': int(_get(request, 'target_count', 5) or 5),
                           'found': len(list(selected or []))},
                'near_misses': [], 'details': {'stop_reason': stop_reason}}
    source_errors = _source_errors(trace)
    health: dict[str, dict] = {}
    for name, state in (source_health or {}).items():
        if isinstance(state, Mapping):
            health[name] = dict(state)
    specific = {item['provider'] for item in source_errors if item['provider'] != 'retrieval'}
    for item in source_errors:
        if item['provider'] == 'retrieval' and specific:
            continue
        state = health.setdefault(item['provider'], {'name': item['provider'],
                                                      'codes': [], 'last_error': ''})
        codes = state.setdefault('codes', [])
        if item['code'] not in codes:
            codes.append(item['code'])
        state['last_error'] = item['error']
    target = int(_get(request, 'target_count', 5) or 5)
    paper_gap = _gap(gaps, 'paper_count')
    found = int(paper_gap.get('found', 0)) if paper_gap else target - int(
        sum(int(gap.get('missing') or 0) for gap in gaps if gap.get('requirement') == 'paper_count'))
    found = max(0, min(target, found))
    constraints = _constraint_text(request)
    selected_ids = {str(item.get('id') or item.get('paper_id') or '')
                    for item in (selected or []) if isinstance(item, Mapping)}
    near = []
    for paper in list(candidates or []):
        if not isinstance(paper, Mapping):
            continue
        paper_id = str(paper.get('id') or paper.get('paper_id') or '')
        if paper_id and paper_id in selected_ids:
            continue
        near.append({'id': paper_id, 'title': paper.get('title') or ''})
        if len(near) >= 3:
            break
    if not gaps:
        return {'code': 'ok', 'summary': '', 'actions': [], 'source_errors': [],
                'source_health': health,
                'counts': {'target': target, 'found': found}, 'near_misses': [],
                'details': {'stop_reason': stop_reason}}

    constraint_gap = _gap(gaps, 'constraints')
    quota_gaps = [gap for gap in gaps if gap.get('requirement') in ('research', 'survey', 'evaluation',
                                                                   'benchmark', 'mix')]
    if constraint_gap:
        code = 'constraints_unmet'
        reason = f'当前约束（{constraint_gap.get("value") or constraints or "venue/year"}）没有足够命中'
    elif quota_gaps:
        code = 'quota_unmet'
        reason = '检索到的候选不足，或类别/阅读目标配额无法满足'
    elif found == 0 and source_errors:
        code = 'source_degraded'
        reason = '检索源不可用，当前没有形成可用候选'
    elif 'budget' in str(stop_reason).casefold() or 'exhaust' in str(stop_reason).casefold():
        code = 'budget_exhausted'
        reason = '本轮搜索/评估预算已用尽'
    elif found == 0:
        code = 'insufficient_candidates'
        reason = '没有找到与主题和约束足够相关的候选'
    else:
        code = 'requirements_unmet'
        reason = '候选已找到，但未满足全部交付要求'

    summary = f'目标 {target} 篇，已交付 {found} 篇；{reason}。'
    if source_errors:
        names = [item['provider'] for item in source_errors if item['provider'] != 'retrieval']
        detail = '、'.join(dict.fromkeys(names)) or '检索超时'
        codes = sorted({item['code'] for item in source_errors
                        if item['code'] and item['code'] != 'source_error'})
        summary += f' 检索侧异常：{detail}'
        if codes:
            summary += f'（{", ".join(codes)}）'
        summary += '。'
    if paper_gap:
        summary += f' 数量缺口 {paper_gap.get("missing", target - found)} 篇。'
    if stop_reason and stop_reason not in ('requirements_met',):
        summary += f' 本轮停止原因：{stop_reason}。'

    actions: list[dict] = []
    venue = str(_get(request, 'venue', '') or '').strip()
    year_from = _get(request, 'year_from')
    year_to = _get(request, 'year_to')
    if year_from or year_to:
        actions.append(_action(
            'relax_year', '不限年份', '保留 venue 和主题，只放宽年份',
            '放宽年份限制，不限发表年份，保留当前主题和会议条件，重试。',
            patch=recovery_patch('relax_year', request)))
    if venue:
        actions.append(_action(
            'relax_venue', '不限 venue', '保留主题和年份，只放宽会议/期刊',
            '放宽会议/期刊限制，保留当前主题和年份，重试。',
            patch=recovery_patch('relax_venue', request)))
    if target > 3:
        lowered = int(recovery_patch('lower_target', request, found=found).get('target_count', target))
        if lowered < target:
            actions.append(_action(
                'lower_target', f'目标降到 {lowered} 篇', '先交付可用论文，再逐步补足',
                f'把目标降到 {lowered} 篇，优先交付当前已有且满足主题的结果，并明确剩余缺口。',
                patch=recovery_patch('lower_target', request, found=found)))
    elif found and target == 3:
        actions.append(_action(
            'accept_partial', f'接受当前 {found} 篇', '保留当前 partial 结果，不产生额外模型调用',
            '保留当前结果和缺口，不再重试；可新建会话追加条件。', retry=False))
    if source_errors:
        actions.append(_action(
            'retry_source', '检查检索源', 'OpenAlex 的 venue 解析或 DBLP 反爬可能阻止召回',
            '检索源出现异常，请稍后重试或换用 OpenReview/ACL 专用检索。', retry=False))
        actions.append(_action(
            'new_session', '新建会话换源', '保留当前请求，重新选择可用 source',
            '新建一个会话，并选择 OpenReview / ACL / 单源检索后重试。',
            retry=False, patch={'source': 'openreview'}))
    if not actions:
        actions.append(_action(
            'clarify', '补充条件', '缩小或改写主题，避免过度约束',
            '请补充更具体的主题、可接受的会议/年份，或允许我放宽一个条件后重试。', retry=False))

    return {
        'code': code,
        'summary': summary,
        'actions': actions,
        'source_errors': source_errors,
        'source_health': health,
        'counts': {'target': target, 'found': found},
        'near_misses': near,
        'details': {
            'stop_reason': stop_reason,
            'constraints': constraints,
            'gaps': gaps,
            'limits': dict(limits or {}),
        },
    }
