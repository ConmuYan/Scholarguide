"""Phase 3（A 维度）：机构评分。

基于 OpenAlex authorships（第一作者 + 明确通讯作者）的机构，匹配 CSRankings
NLP+AI+ML 排名快照，给出 institution_score：
前 25 = 1.00，26-50 = 0.80，51-100 = 0.60，其他已核验高校 = 0.40。
通讯作者未披露时只用第一作者，corresponding_status="not_identified"。

结果写入 features_agents.jsonl（在原记录上追加 A 维度字段）。

运行：python3 frontierlitbench/scripts/phase3_institution.py
"""
from __future__ import annotations

import re

import common_lib as lib


def _norm_institution(name):
    """规范化机构名：小写、去标点与常见变体（用于匹配 CSRankings 简称）。"""
    text = str(name or '').lower()
    text = re.sub(r'[^a-z0-9]+', '', text)
    # 常见缩写展开，便于匹配
    text = text.replace('universityof', 'univ')
    text = text.replace('univ', 'univ')
    return text


def _rank_map(rubric):
    """构建 {规范化机构名: (rank, tier_score)} 映射。"""
    mapping = {}
    for bucket, score in (('top25', 1.00), ('top50', 0.80), ('top100', 0.60)):
        for item in rubric.get(bucket, []):
            mapping[_norm_institution(item['institution'])] = (
                item['rank'], score, item['institution'])
    return mapping


def _match_rank(name, rank_map):
    """机构名匹配 CSRankings 排名，返回 (score, matched_name, rank)。"""
    norm = _norm_institution(name)
    if not norm:
        return None, None, None
    if norm in rank_map:
        rank, score, matched = rank_map[norm]
        return score, matched, rank
    # 子串匹配（模糊：机构名互为子串）
    for key, (rank, score, matched) in rank_map.items():
        if norm in key or key in norm:
            return score, matched, rank
    # 未进入前 100，但仍是已核验高校 → 0.40
    return 0.40, name, None


def _score_record(record, rank_map):
    """计算单篇论文的 A 维度机构分。"""
    authorships = record.get('authorships', [])
    if not authorships:
        return {
            'institution_status': 'unverified',
            'institution_unverified_reason': 'no_author_institutions',
            'institution_score': None,
            'corresponding_status': 'not_identified',
        }

    first_insts = authorships[0].get('institutions', [])
    corresponding = [a for a in authorships if a.get('is_corresponding')]
    corr_insts = []
    for auth in corresponding:
        for inst in auth.get('institutions', []):
            if inst not in corr_insts:
                corr_insts.append(inst)

    first_best = None
    first_evidence = None
    for inst in first_insts:
        score, matched, rank = _match_rank(inst, rank_map)
        if score is not None and (first_best is None or score > first_best):
            first_best = score
            first_evidence = {'institution': inst, 'matched': matched, 'rank': rank}

    corr_best = None
    corr_evidence = None
    for inst in corr_insts:
        score, matched, rank = _match_rank(inst, rank_map)
        if score is not None and (corr_best is None or score > corr_best):
            corr_best = score
            corr_evidence = {'institution': inst, 'matched': matched, 'rank': rank}

    corresponding_status = 'identified' if corresponding else 'not_identified'

    if first_best is not None and corr_best is not None:
        final = round((first_best + corr_best) / 2, 4)
        status = 'verified'
    elif first_best is not None:
        final = first_best
        status = 'verified'
        corresponding_status = 'not_identified'
    else:
        final = None
        status = 'unverified'

    return {
        'institution_status': status,
        'institution_score': final,
        'corresponding_status': corresponding_status,
        'first_author_evidence': first_evidence,
        'corresponding_evidence': corr_evidence,
        'institution_reviewer': 'agent-draft',
    }


def main() -> int:
    lib.ensure_dirs()
    records = lib.read_jsonl(lib.CORPUS_DIR / 'features_agents.jsonl')
    if not records:
        print('错误：features 为空，请先运行 phase3_features.py。')
        return 1

    rubric = lib.read_json(lib.CONFIG_DIR / 'institution_rubric_agents_draft.json')
    if not rubric:
        print('错误：rubric 缺失，请先运行 phase3_csrankings.py。')
        return 1
    rank_map = _rank_map(rubric)

    path = lib.CORPUS_DIR / 'features_agents.jsonl'
    path.write_text('', encoding='utf-8')
    verified = 0
    for record in records:
        score = _score_record(record, rank_map)
        merged = {**record, **score}
        if score['institution_status'] == 'verified':
            verified += 1
        lib.append_jsonl(path, merged)

    lib.log_review('phase3_institution', total=len(records), verified=verified,
                   unverified=len(records) - verified)

    print('Phase 3（A 机构）完成')
    print('总数：%d | 机构 verified：%d | unverified：%d' % (
        len(records), verified, len(records) - verified))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
