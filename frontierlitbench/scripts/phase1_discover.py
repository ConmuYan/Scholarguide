"""Phase 1：候选发现（Candidate Pool）。

用 Lune ``search_papers`` 对 7 个子方向逐 query 做多角度检索（search_papers_many
的 limit 是总结果数而非每 query，故改用逐 query 以获得更大覆盖面）。候选池不按
引用数截断。每条候选记录发现来源（query + 工具）。

按 Lune 内部 paper_id 做初版去重（同 paper 合并子方向与 query 来源），正式三级
去重在 Phase 2。结果追加写 corpus/candidates_agents.jsonl。

运行：python3 frontierlitbench/scripts/phase1_discover.py
"""
from __future__ import annotations

import json
import os
import sys

import common_lib as lib

sys.path.insert(0, str(lib.ROOT))
try:
    from common import load_env
    load_env()
except Exception:
    pass

VENUES = list(lib.TRACK_C_VENUES)
YEAR_MIN = 2024
YEAR_MAX = 2026
LIMIT = 10  # 单 query 结果上限（search_papers 的 limit 上限为 10）

# 7 个子方向的检索 query（协议 Phase 1 明确要求覆盖）
SUB_AREAS = {
    'tool_use': [
        'tool use language model',
        'function calling large language model',
        'API calling LLM agent',
        'tool-augmented language model',
        'tool learning agent',
    ],
    'planning': [
        'multi-step planning agent',
        'task decomposition language model',
        'tree search reasoning LLM',
        'graph search agent',
        'hierarchical planning agent',
    ],
    'memory': [
        'agent memory long-term',
        'long-term memory language model',
        'retrieval augmented agent',
        'memory management LLM agent',
        'working memory language model',
    ],
    'self_correction': [
        'self-correction language model',
        'reflection language model',
        'self-refine LLM',
        'verification agent',
        'self-critique reasoning',
    ],
    'multi_agent': [
        'multi-agent collaboration',
        'multi-agent debate',
        'role-playing agent',
        'multi-agent system LLM',
        'agent society cooperation',
    ],
    'agent_embodied': [
        'web agent',
        'GUI agent',
        'code agent',
        'software engineering agent',
        'computer use agent',
    ],
    'agent_training': [
        'agent tuning',
        'trajectory reinforcement learning',
        'LLM agent training',
        'agentic fine-tuning',
        'interactive learning agent',
    ],
}


def _credential_kind() -> str:
    try:
        import mcp_lune
        token = mcp_lune.access_token()
    except Exception:
        token = ''
    if token:
        return 'oauth'
    if os.getenv('LUNE_API_KEY'):
        return 'env-key'
    return ''


def _search_one(client, query, venues, year_min, year_max):
    """单 query search_papers，返回 papers 列表。"""
    import mcp_client
    import mcp_lune
    arguments = {
        'query': query,
        'venues': venues,
        'year_min': year_min,
        'year_max': year_max,
        'limit': LIMIT,
        'detail': True,
    }
    raw = client.call_tool('search_papers', arguments)
    payload = mcp_client.structured(raw)
    return mcp_lune.papers_from(payload if isinstance(payload, dict) else {})


def _build_candidate(item, sub_area, query):
    """把 Lune 返回条目转成候选记录（保留原始关键字段）。"""
    return {
        'lune_paper_id': str(item.get('paper_id') or ''),
        'title': str(item.get('title') or '').strip(),
        'authors': [str(a) for a in item.get('authors') or []],
        'year': item.get('year'),
        'conference': str(item.get('conference') or ''),
        'doi': str(item.get('doi') or ''),
        'arxiv_id': str(item.get('arxiv_id') or ''),
        'abstract': str(item.get('abstract') or ''),
        'sub_areas': [sub_area],
        'matched_queries': [query],
        'source': 'lune',
        'source_tool': 'search_papers',
        'retrieved_at': lib.utc_now_iso(),
    }


def _merge(pool, record):
    """按 lune_paper_id 合并候选（去重并合并来源）。"""
    key = record['lune_paper_id']
    if not key:
        return
    if key not in pool:
        pool[key] = record
        return
    existing = pool[key]
    if record['sub_areas'][0] not in existing['sub_areas']:
        existing['sub_areas'].append(record['sub_areas'][0])
    for query in record['matched_queries']:
        if query not in existing['matched_queries']:
            existing['matched_queries'].append(query)


def _coverage(pool):
    by_year, by_venue, by_sub = {}, {}, {}
    for record in pool.values():
        year = record.get('year')
        venue = record.get('conference')
        by_year[year] = by_year.get(year, 0) + 1
        by_venue[venue] = by_venue.get(venue, 0) + 1
        for sub in record.get('sub_areas', []):
            by_sub[sub] = by_sub.get(sub, 0) + 1
    return by_year, by_venue, by_sub


def main() -> int:
    lib.ensure_dirs()
    if not _credential_kind():
        print('错误：未检测到 Lune 凭据。请先设置 LUNE_API_KEY。')
        return 2

    import mcp_lune
    provider = mcp_lune.LuneProvider(max_requests=200)
    client = provider.client()

    pool = {}
    failures = []
    calls = 0
    for sub_area, queries in SUB_AREAS.items():
        for query in queries:
            calls += 1
            try:
                papers = _search_one(client, query, VENUES, YEAR_MIN, YEAR_MAX)
            except Exception as exc:
                failures.append({'sub_area': sub_area, 'query': query,
                                 'error': repr(exc)})
                continue
            for item in papers:
                _merge(pool, _build_candidate(item, sub_area, query))
        print('子方向 %-15s 完成，候选累计 %d' % (sub_area, len(pool)))

    candidates_path = lib.CORPUS_DIR / 'candidates_agents.jsonl'
    # 覆盖写：先清空，避免多次运行残留重复记录
    candidates_path.write_text('', encoding='utf-8')
    for record in pool.values():
        lib.append_jsonl(candidates_path, record)

    by_year, by_venue, by_sub = _coverage(pool)
    lib.log_review('phase1_discover', total=len(pool), calls=calls,
                   failures=failures, by_year=by_year, by_venue=by_venue)

    print('Phase 1 完成')
    print('候选总数（Lune ID 去重后）：%d（共 %d 次调用）' % (len(pool), calls))
    print('年份分布：%s' % json.dumps(by_year, ensure_ascii=False, sort_keys=True))
    print('会议分布：%s' % json.dumps(by_venue, ensure_ascii=False, sort_keys=True))
    print('子方向分布：%s' % json.dumps(by_sub, ensure_ascii=False, sort_keys=True))
    print('失败 query 数：%d' % len(failures))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
