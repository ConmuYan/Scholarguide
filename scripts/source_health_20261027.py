"""Live source health check for the current configuration. No model calls.

Each provider gets one small public query. The report separates:
  * ok        — reached, returned records, abstract usable
  * empty     — reached, but nothing came back
  * error     — network/auth/protocol/bot-challenge failure
  * skipped   — no credential for this configuration

Writes runs/source-check-20261027/report.{json,md}. Keys are never printed.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import Budget, Config, source_names
from source import AcademicSource, PROVIDERS

OUT = ROOT / 'runs' / 'source-check-20261027'
QUERY = 'retrieval augmented generation evaluation'
BUILTIN = ('arxiv', 'openalex', 'semantic_scholar', 'dblp', 'openreview',
           'acl_anthology', 'deepseek')


def probe(source, name):
    started = time.monotonic()
    source.begin_turn()
    try:
        items = source._search_one(name, QUERY, 2)
        status = 'ok' if items else 'empty'
        error = ''
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic surface
        items, status, error = [], 'error', f'{type(exc).__name__}: {exc}'
    key = source._provider_key(name)
    return {
        'provider': name,
        'label': PROVIDERS[name].label if name in PROVIDERS else name,
        'status': status,
        'returned': len(items),
        'first_title': items[0].title[:100] if items else '',
        'first_has_abstract': bool(items and items[0].abstract),
        'first_doi': items[0].doi if items else '',
        'elapsed_ms': int((time.monotonic() - started) * 1000),
        'http_calls': source.budget.search_http_calls,
        'error': error[:300],
        'key_configured': bool(key) if name in ('semantic_scholar', 'deepseek', 'openalex') else None,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    env = Config.from_env()
    try:
        from providers import ProviderStore
        store = ProviderStore()
        active = store.active()
        config = store.config(env)
    except Exception:  # noqa: BLE001 - profile store is optional
        active, config = None, env
    config.source_timeout = min(config.source_timeout, 12)
    config.cache = False
    rows = []
    for name in BUILTIN:
        src = AcademicSource(config, Budget(120))
        rows.append(probe(src, name))
    # DeepSeek Search is the only source whose credential depends on the profile:
    # a non-DeepSeek active profile drops the model-key fallback.
    deepseek = next(r for r in rows if r['provider'] == 'deepseek')
    if not deepseek['key_configured'] and env.api_key and 'api.deepseek.com' in env.base_url:
        if not getattr(config, 'deepseek_search_key', ''):
            env.source_timeout = min(env.source_timeout, 12)
            env.cache = False
            alt = probe(AcademicSource(env, Budget(120)), 'deepseek')
            alt['note'] = 'tested with the env profile credential; skipped by the active profile'
            deepseek['note'] = alt.get('note')
            deepseek['env_fallback'] = alt

    # Lune (MCP): only present after install(); request budget is its own ledger.
    lune = None
    try:
        import mcp_lune
        provider = mcp_lune.install()
        before = provider.usage()
        src = AcademicSource(config, Budget(120))
        started = time.monotonic()
        try:
            items = src._search_one('lune', QUERY, 2)
            status, error = ('ok' if items else 'empty'), ''
        except Exception as exc:  # noqa: BLE001
            items, status, error = [], 'error', f'{type(exc).__name__}: {exc}'
        after = provider.usage()
        lune = {'provider': 'lune', 'label': 'Lune Research (MCP)', 'status': status,
                'returned': len(items),
                'first_title': items[0].title[:100] if items else '',
                'first_has_abstract': bool(items and items[0].abstract),
                'elapsed_ms': int((time.monotonic() - started) * 1000),
                'http_calls': src.budget.search_http_calls,
                'error': error[:300], 'key_configured': True,
                'budget_before': {'requests': before['requests'], 'remaining': before['remaining']},
                'budget_after': {'requests': after['requests'], 'remaining': after['remaining']}}
    except Exception as exc:  # noqa: BLE001 - adapter missing is itself a finding
        lune = {'provider': 'lune', 'label': 'Lune Research (MCP)', 'status': 'error',
                'returned': 0, 'error': f'{type(exc).__name__}: {exc}'[:300]}

    # Router preview: which sources the product would actually call for two tasks.
    routing = []
    try:
        from dataclasses import replace
        from retrieval import RetrievalRouter
        preview_config = replace(config, source='multi', research_mode=True)
        router = RetrievalRouter(preview_config, AcademicSource(preview_config, Budget(30)))
        for task, query in (
            ({'topic': '3D generation', 'category': 'none', 'target_count': 5},
             '3D generation text-to-3D'),
            ({'topic': 'RAG evaluation', 'category': 'evaluation', 'target_count': 5,
              'venue': 'NeurIPS'}, 'retrieval augmented generation benchmark'),
            ({'topic': 'RAG survey', 'category': 'survey', 'target_count': 5,
              'venue': 'ACL'}, 'retrieval augmented generation survey'),
        ):
            plan = router.plan(task, query)
            routing.append({'task': task.get('topic') + (' @ ' + task['venue'] if task.get('venue') else ''),
                            'steps': [{'provider': s.get('provider'),
                                       'purpose': s.get('purpose') or s.get('intent') or ''}
                                      for s in plan.steps],
                            'fallback': [s.get('provider') for s in plan.fallback_steps],
                            'deepseek_allowed': plan.deepseek_allowed,
                            'notes': plan.notes})
    except Exception as exc:  # noqa: BLE001
        routing.append({'error': f'{type(exc).__name__}: {exc}'})

    payload = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'not_a_benchmark': 'Live connectivity only; one query per source; no quality labels.',
        'model_calls': 0,
        'active_profile': active.public() if active else None,
        'configured_sources': source_names(config.source),
        'providers': rows + ([lune] if lune else []),
        'routing_preview': routing,
    }
    (OUT / 'report.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                                     encoding='utf-8')

    lines = ['# Source health check', '',
             f"checked_at: `{payload['checked_at']}`",
             f"active profile: `{(active.public() if active else {}).get('name')}` "
             f"({(active.public() if active else {}).get('model')})",
             f"configured: `{config.source}`", '',
             '| source | status | returned | abstract | ms | http | detail |',
             '|---|---|---|---|---|---|---|']
    for row in payload['providers']:
        detail = row.get('error') or ''
        fallback = row.get('env_fallback')
        if fallback:
            detail = (f"active profile: no key | env profile: {fallback['status']} "
                      f"({fallback['returned']} results, {fallback['elapsed_ms']} ms)")
        lines.append(f"| {row['label']} | {row['status']} | {row['returned']} | "
                     f"{'yes' if row.get('first_has_abstract') else 'no'} | "
                     f"{row.get('elapsed_ms', '')} | {row.get('http_calls', '')} | {str(detail)[:160]} |")
    lines += ['', '## Routing preview', '']
    for item in routing:
        if 'steps' in item:
            chain = ' → '.join(s['provider'] for s in item['steps']) or '(none)'
            fallback = ', '.join(item['fallback']) or 'none'
            lines.append(f"- {item['task']}: {chain} | fallback: {fallback}"
                         f" | deepseek allowed: {item['deepseek_allowed']}")
            for note in item['notes']:
                lines.append(f"  - note: {note}")
        else:
            lines.append(f"- error: {item['error']}")
    needs = [row for row in payload['providers']
             if row['status'] in ('error', 'empty')
             or (row.get('elapsed_ms') or 0) > 5000
             or (row['provider'] in ('semantic_scholar', 'deepseek')
                 and row.get('key_configured') is False)]
    lines += ['', '## Needs attention', '']
    lines += ([f"- {row['label']}: {row['status']} — "
                f"{str(row.get('error') or row.get('note') or 'slow/keyless')[:160]}"
               for row in needs] or ['- none'])
    (OUT / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
