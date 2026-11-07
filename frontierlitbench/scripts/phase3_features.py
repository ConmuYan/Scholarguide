"""Phase 3：质量特征原始值采集。

- C（引用）：OpenAlex ``cited_by_count``（唯一主源），同时做书目身份核验。
- G（GitHub）：官方仓库识别与 stargazers_count（先采集，识别策略见函数）。
- A（机构）：从 OpenAlex authorships 提取作者机构，CSRankings 评分在 Phase 4 前完成。

同一采集窗口内获取（as_of 记录）。找不到/多匹配标记 unverified 并写原因。

运行：python3 frontierlitbench/scripts/phase3_features.py
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

import common_lib as lib

OPENALEX = 'https://api.openalex.org'
MAILTO = 'frontierlitbench@example.com'
BATCH_SIZE = 25


def _query_doi(record):
    """构造 OpenAlex 查询 DOI：真实 DOI 优先，arxiv DOI 次之。"""
    for doi in record.get('dois', []):
        if doi and not doi.startswith('10.48550/arxiv'):
            return doi
    for arxiv in record.get('arxiv_ids', []):
        if arxiv:
            return '10.48550/arxiv.' + arxiv
    return ''


def _openalex_by_dois(dois):
    """单条按 DOI 查询 OpenAlex，返回 {查询DOI: work}（精确对应，避免批量
    查询时 arxiv DOI 命中真实 DOI 导致的 key 错位）。"""
    result = {}
    for doi in dois:
        url = OPENALEX + '/works/doi:' + quote(doi) + '?mailto=' + MAILTO
        work = lib.http_get_json(url, timeout=20)
        if isinstance(work, dict) and work.get('id'):
            result[doi] = work
        time.sleep(0.15)  # 礼貌限速
    return result


def _openalex_by_title(record):
    """无 DOI 时用标题搜索 OpenAlex（可能多匹配，标 unverified 风险）。"""
    title = re.sub(r'[^\w\s]', ' ', record['title'])
    title = ' '.join(title.split()[:12])
    url = (OPENALEX + '/works?search=' + quote(title)
           + '&filter=publication_year:%s&per-page=3&mailto=%s'
           % (record['year'], MAILTO))
    payload = lib.http_get_json(url, timeout=30)
    if not payload:
        return None, []
    results = payload.get('results', [])
    if not results:
        return None, []
    return results, results


def _verify_record(record, work):
    """用 OpenAlex 元数据核验 Lune 记录的书目身份。"""
    checks = {}
    oa_title = str(work.get('title') or '')
    checks['title_verified'] = bool(
        oa_title and lib.normalize_title(oa_title) == lib.normalize_title(
            record['title']))
    oa_year = work.get('publication_year')
    checks['year_verified'] = bool(oa_year and oa_year == record['year'])
    oa_authors = [a.get('author', {}).get('display_name', '')
                  for a in work.get('authorships', [])]
    checks['authors_present'] = bool(oa_authors)
    return checks, oa_title, oa_authors


def _extract_institutions(work):
    """从 OpenAlex authorships 提取第一作者与通讯作者的机构。"""
    authorships = work.get('authorships', [])
    out = []
    for auth in authorships:
        insts = [i.get('display_name') for i in auth.get('institutions', [])]
        out.append({
            'author': auth.get('author', {}).get('display_name', ''),
            'institutions': insts,
            'is_corresponding': bool(auth.get('is_corresponding')),
        })
    return out


def _collect_citations(records):
    """C 维度：OpenAlex 引用采集 + 元数据核验。"""
    as_of = lib.utc_now_iso()
    doi_map = {}
    no_doi = []
    for record in records:
        doi = _query_doi(record)
        if doi:
            doi_map[lib.normalize_doi(doi)] = record
        else:
            no_doi.append(record)

    works = _openalex_by_dois(list(doi_map.keys()))
    enriched = {}
    for norm_doi, record in doi_map.items():
        work = works.get(norm_doi)
        if work:
            enriched[record['paper_id']] = _build_citation(record, work, as_of)
        else:
            enriched[record['paper_id']] = _unverified_citation(
                record, as_of, 'openalex_no_match')

    # 无 DOI 的用标题搜索
    for record in no_doi:
        results, _ = _openalex_by_title(record)
        if not results:
            enriched[record['paper_id']] = _unverified_citation(
                record, as_of, 'openalex_no_match')
            continue
        # 多匹配或单匹配，记录但不自动合并
        work = results[0]
        if len(results) > 1:
            enriched[record['paper_id']] = _unverified_citation(
                record, as_of, 'openalex_multiple_match')
        else:
            enriched[record['paper_id']] = _build_citation(record, work, as_of)

    return enriched


def _build_citation(record, work, as_of):
    checks, oa_title, oa_authors = _verify_record(record, work)
    return {
        'citation_count': work.get('cited_by_count'),
        'citation_source': 'openalex',
        'citation_as_of': as_of,
        'openalex_id': (work.get('id') or '').replace('https://openalex.org/', ''),
        'citation_status': 'verified',
        'title_verified': checks['title_verified'],
        'year_verified': checks['year_verified'],
        'openalex_title': oa_title,
        'openalex_authors': oa_authors,
        'authorships': _extract_institutions(work),
    }


def _unverified_citation(record, as_of, reason):
    return {
        'citation_count': None,
        'citation_source': 'openalex',
        'citation_as_of': as_of,
        'openalex_id': '',
        'citation_status': 'unverified',
        'unverified_reason': reason,
        'title_verified': False,
        'year_verified': False,
        'openalex_title': '',
        'openalex_authors': [],
        'authorships': [],
    }


def main() -> int:
    lib.ensure_dirs()
    records = lib.read_jsonl(lib.CORPUS_DIR / 'dedup_candidates.jsonl')
    if not records:
        print('错误：去重候选为空，请先运行 phase2_dedup_verify.py。')
        return 1

    enriched = _collect_citations(records)

    path = lib.CORPUS_DIR / 'features_agents.jsonl'
    path.write_text('', encoding='utf-8')
    verified = 0
    for record in records:
        feature = enriched.get(record['paper_id'], {})
        merged = {**record, **feature}
        if feature.get('citation_status') == 'verified':
            verified += 1
        lib.append_jsonl(path, merged)

    unverified = len(records) - verified
    lib.log_review('phase3_citations', total=len(records), verified=verified,
                   unverified=unverified)

    print('Phase 3（C 引用）完成')
    print('总数：%d | 引用 verified：%d | unverified：%d' % (
        len(records), verified, unverified))
    print('特征文件：%s' % path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
