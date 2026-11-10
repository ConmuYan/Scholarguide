"""Phase 4：准入与评分、S/A/B 分档。

准入 Gold 条件：C、G、A 三维度都 verified（repo_status ∈ official /
none_available / not_applicable）。不满足的留在候选池并记录卡点。

- C：按 Track C × 年份分层，x=log(1+citations) 的中位秩百分位。
- G：同年份 official 子集内，Stars 的中位秩百分位；none_available=0.0，
  not_applicable=0.5。
- A：institution_score（Phase 3 已算）。
- Q = 0.55·C + 0.30·G + 0.15·A。
- 分档：Q 降序、同分 paper_id 升序；N_S=round_half_up(0.20N)、
  N_A=round_half_up(0.40N)、N_B=N-N_S-N_A。

运行：python3 frontierlitbench/scripts/phase4_score_tier.py
"""
from __future__ import annotations

import json
import math

import common_lib as lib

_REPO_VERIFIED = ('official', 'none_available', 'not_applicable')


def _admitted(record):
    """准入判断 + 未准入卡点。"""
    if record.get('track_review') == 'exclude':
        return False, 'outside_track_c'
    if record.get('repo_attribution_concern'):
        return False, 'github_stars_not_paper_specific'
    if record.get('citation_status') != 'verified':
        return False, 'citation_unverified'
    if record.get('institution_status') != 'verified':
        return False, 'institution_unverified'
    if record.get('repo_status') not in _REPO_VERIFIED:
        return False, 'github_unverified'
    return True, ''


def _midrank(values, x):
    """中位秩百分位 P(x)= (#{x_j<x} + 0.5·#{x_j=x}) / |R|。"""
    if not values:
        return None
    n = len(values)
    less = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return (less + 0.5 * equal) / n


def _compute_c(gold_records):
    """C 维度：按年份分层的 log(1+citations) 中位秩百分位。"""
    by_year = {}
    for record in gold_records:
        year = record.get('year')
        by_year.setdefault(year, []).append(
            math.log(1 + (record.get('citation_count') or 0)))
    c_scores = {}
    for record in gold_records:
        year = record.get('year')
        x = math.log(1 + (record.get('citation_count') or 0))
        c_scores[record['paper_id']] = _midrank(by_year.get(year, []), x)
    return c_scores


def _compute_g(gold_records):
    """G 维度：同年份 official 子集 Stars 中位秩；none/not_applicable 固定分。"""
    by_year = {}
    for record in gold_records:
        if record.get('repo_status') == 'official':
            year = record.get('year')
            by_year.setdefault(year, []).append(record.get('github_stars') or 0)
    g_scores = {}
    for record in gold_records:
        status = record.get('repo_status')
        if status == 'none_available':
            g_scores[record['paper_id']] = 0.0
        elif status == 'not_applicable':
            g_scores[record['paper_id']] = 0.5
        elif status == 'official':
            year = record.get('year')
            stars = record.get('github_stars') or 0
            values = by_year.get(year, [])
            g_scores[record['paper_id']] = _midrank(values, stars)
        else:
            g_scores[record['paper_id']] = None
    return g_scores


def _tier(sorted_records):
    """按 Q 降序 + paper_id 决胜分 S/A/B 档。"""
    n = len(sorted_records)
    n_s = lib.round_half_up(lib.TIER_S_RATIO * n)
    n_a = lib.round_half_up(lib.TIER_A_RATIO * n)
    n_b = n - n_s - n_a
    tiers = {}
    for i, record in enumerate(sorted_records):
        if i < n_s:
            tiers[record['paper_id']] = 'S'
        elif i < n_s + n_a:
            tiers[record['paper_id']] = 'A'
        else:
            tiers[record['paper_id']] = 'B'
    return tiers, n_s, n_a, n_b


def main() -> int:
    lib.ensure_dirs()
    records = lib.read_jsonl(lib.CORPUS_DIR / 'features_agents.jsonl')
    if not records:
        print('错误：features 为空。')
        return 1

    # 准入
    gold = []
    rejected = []
    for record in records:
        ok, reason = _admitted(record)
        if ok:
            gold.append(record)
        else:
            rejected.append({'paper_id': record.get('paper_id'),
                             'title': record.get('title'),
                             'reject_reason': reason})

    c_scores = _compute_c(gold)
    g_scores = _compute_g(gold)

    for record in gold:
        pid = record['paper_id']
        c = c_scores.get(pid)
        g = g_scores.get(pid)
        a = record.get('institution_score')
        q = (lib.W_C * c + lib.W_G * g + lib.W_A * a
             if None not in (c, g, a) else None)
        record['C'] = c
        record['G'] = g
        record['A'] = a
        record['Q'] = round(q, 6) if q is not None else None

    # 分档：Q 降序，同分 paper_id 升序
    gold.sort(key=lambda r: (-(r.get('Q') if r.get('Q') is not None else -1),
                             r['paper_id']))
    tiers, n_s, n_a, n_b = _tier(gold)
    for record in gold:
        record['tier'] = tiers[record['paper_id']]

    # 写 gold_agents.jsonl
    gold_path = lib.CORPUS_DIR / 'gold_agents.jsonl'
    gold_path.write_text('', encoding='utf-8')
    for record in gold:
        lib.append_jsonl(gold_path, record)

    # 写 impact_annotations
    annot_path = lib.LABELS_DIR / 'impact_annotations_agents.jsonl'
    annot_path.write_text('', encoding='utf-8')
    for record in gold:
        lib.append_jsonl(annot_path, {
            'paper_id': record['paper_id'], 'title': record['title'],
            'tier': record['tier'], 'Q': record['Q'],
            'C': record['C'], 'G': record['G'], 'A': record['A'],
        })

    # 档位阈值
    thresholds = {'N': len(gold), 'N_S': n_s, 'N_A': n_a, 'N_B': n_b,
                  'tier_boundaries': {}}
    for tier in ('S', 'A', 'B'):
        tier_records = [r for r in gold if r['tier'] == tier]
        if tier_records:
            thresholds['tier_boundaries'][tier] = {
                'min_Q': min(r['Q'] for r in tier_records),
                'max_Q': max(r['Q'] for r in tier_records),
            }
    lib.write_json(lib.LABELS_DIR / 'tier_thresholds_agents.json', thresholds)

    lib.log_review('phase4_score_tier', total=len(records), admitted=len(gold),
                   rejected=len(rejected), n_s=n_s, n_a=n_a, n_b=n_b,
                   reject_reasons=_reject_dist(rejected))

    print('Phase 4 完成')
    print('候选：%d | 准入 Gold：%d | 未准入：%d' % (
        len(records), len(gold), len(rejected)))
    print('档位：N_S=%d N_A=%d N_B=%d' % (n_s, n_a, n_b))
    if rejected:
        from collections import Counter
        print('未准入原因：%s' % dict(Counter(r['reject_reason'] for r in rejected)))
    return 0


def _reject_dist(rejected):
    from collections import Counter
    return dict(Counter(r['reject_reason'] for r in rejected))


if __name__ == '__main__':
    raise SystemExit(main())
