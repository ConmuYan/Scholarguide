"""Phase 2：去重、核验与归属。

三级去重顺序：DOI → arXiv ID → 规范化标题 + 年份 + 第一作者。预印本与会议版
合并为一条记录，保留所有来源链接。生成 paper_id（doi:/arxiv:/url:，一经生成不再
变动）。基于标题/摘要做 primary_track 初步判断与边界/benchmark 标记。

完整的外部核验（真实存在、作者顺序、正式会议年份）在 Phase 3 结合 OpenAlex 完成。

运行：python3 frontierlitbench/scripts/phase2_dedup_verify.py
"""
from __future__ import annotations

import json
import re

import common_lib as lib

# 边界信号（仅匹配标题）：可能属于 Track B（world model/机器人）或 Track A（视频）
_BOUNDARY_SIGNALS = (
    'embodied', 'robot', 'robotic', 'manipulation', 'world model',
    'autonomous driving', 'drone', 'aerial', 'video generation',
    'video editing', 'image generation', 'text-to-video', 'diffusion',
)
_BENCHMARK_SIGNALS = ('benchmark', 'dataset', 'testbed', 'eval suite',
                      'datasets for', 'evaluation benchmark')


def _first_author(authors):
    return authors[0] if authors else ''


def _real_doi(doi):
    """返回非 arXiv 的真实 DOI；arxiv DOI 视为 arXiv ID 来源。"""
    text = lib.normalize_doi(doi)
    if not text:
        return ''
    if text.startswith('10.48550/arxiv') or 'arxiv' in text:
        return ''
    return text


def _extract_arxiv(item):
    """从 arxiv_id 或 arxiv DOI 提取规范化 arXiv ID。"""
    arxiv = lib.normalize_arxiv(item.get('arxiv_id'))
    if arxiv:
        return arxiv
    doi = lib.normalize_doi(item.get('doi'))
    match = re.search(r'arxiv[/.](\d{4}\.\d{4,5})', doi or '')
    if match:
        return match.group(1)
    return ''


def _merge(existing, item):
    """把候选 item 合并进 existing 记录。"""
    for doi in item.get('dois', []):
        if doi and doi not in existing['dois']:
            existing['dois'].append(doi)
    for arxiv in item.get('arxiv_ids', []):
        if arxiv and arxiv not in existing['arxiv_ids']:
            existing['arxiv_ids'].append(arxiv)
    for sub in item.get('sub_areas', []):
        if sub not in existing['sub_areas']:
            existing['sub_areas'].append(sub)
    for query in item.get('matched_queries', []):
        if query not in existing['matched_queries']:
            existing['matched_queries'].append(query)
    if item.get('lune_paper_id') not in existing['lune_paper_ids']:
        existing['lune_paper_ids'].append(item['lune_paper_id'])
    existing['candidate_count'] += 1


def _build_group(item):
    """从首条候选构建一个去重组。"""
    doi = _real_doi(item.get('doi'))
    arxiv = _extract_arxiv(item)
    year = item.get('year')
    return {
        'real_doi': doi,
        'arxiv_id': arxiv,
        'title': str(item.get('title') or '').strip(),
        'norm_title': lib.normalize_title(item.get('title')),
        'authors': [str(a) for a in item.get('authors') or []],
        'first_author': _first_author(item.get('authors') or []),
        'year': year,
        'venue': str(item.get('conference') or ''),
        'abstract': str(item.get('abstract') or ''),
        'dois': [doi] if doi else [],
        'arxiv_ids': [arxiv] if arxiv else [],
        'sub_areas': list(item.get('sub_areas') or []),
        'matched_queries': list(item.get('matched_queries') or []),
        'lune_paper_ids': [str(item.get('lune_paper_id') or '')],
        'candidate_count': 1,
    }


def _same_group(group, item):
    """判断候选 item 是否与 group 是同一论文（三级去重 key）。"""
    doi = _real_doi(item.get('doi'))
    if doi and group['real_doi'] and doi == group['real_doi']:
        return True
    arxiv = _extract_arxiv(item)
    if arxiv and group['arxiv_id'] and arxiv == group['arxiv_id']:
        return True
    norm_title = lib.normalize_title(item.get('title'))
    first = _first_author(item.get('authors') or [])
    if (norm_title and norm_title == group['norm_title']
            and item.get('year') == group['year']
            and first and first == group['first_author']):
        return True
    return False


def _make_paper_id(group):
    """生成 paper_id：真实 DOI 优先，arXiv 次之，URL 兜底。"""
    doi = group['real_doi']
    if doi:
        return 'doi:' + doi
    arxiv = group['arxiv_id']
    if arxiv:
        return 'arxiv:' + arxiv
    # 兜底：用 Lune paper_id 构造 url（无权威 URL 时保持可追溯）
    return 'url:lune/' + group['lune_paper_ids'][0]


def _classify(group):
    """primary_track 初步判断 + 边界/benchmark/需裁决标记（仅标题匹配）。"""
    title = group['title'].lower()
    boundary = any(signal in title for signal in _BOUNDARY_SIGNALS)
    benchmark = any(signal in title for signal in _BENCHMARK_SIGNALS)

    flags = {
        'primary_track': 'C',
        'boundary_case': bool(boundary),
        'needs_lead_decision': False,
        'sub_area': '',
        'track_notes': [],
    }
    if benchmark:
        flags['sub_area'] = 'benchmark'
        flags['needs_lead_decision'] = True
        flags['track_notes'].append('benchmark_or_dataset_paper')
    if boundary:
        flags['boundary_case'] = True
        flags['track_notes'].append('possible_track_A_or_B_boundary')
    return flags


def _verify_fields(group):
    """字段完整性核验（本地可做部分）。外部核验在 Phase 3。"""
    issues = []
    if not group['title']:
        issues.append('missing_title')
    if not group['authors']:
        issues.append('missing_authors')
    if group['year'] not in lib.YEARS:
        issues.append('invalid_year')
    if group['venue'] not in lib.TRACK_C_VENUES:
        issues.append('venue_not_in_whitelist')
    if not group['abstract']:
        issues.append('missing_abstract')
    return issues


def main() -> int:
    lib.ensure_dirs()
    candidates = lib.read_jsonl(lib.CORPUS_DIR / 'candidates_agents.jsonl')
    if not candidates:
        print('错误：候选文件为空，请先运行 phase1_discover.py。')
        return 1

    groups = []
    for item in candidates:
        matched = None
        for group in groups:
            if _same_group(group, item):
                matched = group
                break
        if matched is None:
            groups.append(_build_group(item))
        else:
            _merge(matched, item)

    # 生成 paper_id、归属、核验状态
    output = []
    for group in groups:
        paper_id = _make_paper_id(group)
        flags = _classify(group)
        issues = _verify_fields(group)
        record = {
            'paper_id': paper_id,
            'title': group['title'],
            'authors': group['authors'],
            'year': group['year'],
            'venue': group['venue'],
            'abstract': group['abstract'],
            'dois': group['dois'],
            'arxiv_ids': group['arxiv_ids'],
            'sub_areas': group['sub_areas'],
            'matched_queries': group['matched_queries'],
            'lune_paper_ids': group['lune_paper_ids'],
            'candidate_count': group['candidate_count'],
            **flags,
            'field_issues': issues,
            'verified': not issues,
            'citation_status': 'pending',
            'repo_status': 'pending',
            'institution_status': 'pending',
        }
        output.append(record)

    # paper_id 唯一性校验
    ids = [r['paper_id'] for r in output]
    dup_ids = [pid for pid in set(ids) if ids.count(pid) > 1]
    if dup_ids:
        lib.log_review('phase2_duplicate_paper_id', duplicates=dup_ids)

    path = lib.CORPUS_DIR / 'dedup_candidates.jsonl'
    path.write_text('', encoding='utf-8')
    for record in output:
        lib.append_jsonl(path, record)

    boundary = sum(1 for r in output if r['boundary_case'])
    lead = sum(1 for r in output if r['needs_lead_decision'])
    unverified = sum(1 for r in output if not r['verified'])
    lib.log_review('phase2_dedup_verify', candidates=len(candidates),
                   deduped=len(output), merged=len(candidates) - len(output),
                   boundary=boundary, lead_decision=lead,
                   field_unverified=unverified)

    print('Phase 2 完成')
    print('候选：%d -> 去重后：%d（合并 %d 条）' % (
        len(candidates), len(output), len(candidates) - len(output)))
    print('boundary_case：%d | needs_lead_decision：%d | 字段不完整：%d' % (
        boundary, lead, unverified))
    print('去重结果：%s' % path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
