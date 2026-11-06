"""Phase 3（A 维度）：CSRankings 机构排名快照。

下载 CSRankings generated-author-info.csv，按「NLP + AI + ML」子领域组合、时间窗
2021-2025 聚合各机构的 adjustedcount，生成前 100 名排名。遵守再发布限制：只保存
排名结果与证据 URL，不整表转存（下载的原始 CSV 处理后删除）。

生成 config/institution_rubric_agents_draft.json（provisional，待负责人确认）。

运行：python3 frontierlitbench/scripts/phase3_csrankings.py
"""
from __future__ import annotations

import csv
import json
import time
from collections import defaultdict

import common_lib as lib

SOURCE_URL = 'https://csrankings.org/generated-author-info.csv'
YEAR_MIN = 2021
YEAR_MAX = 2025

# NLP + AI + ML 子领域对应的 CSRankings area 缩写（协议冻结口径）
NLP_AI_ML_AREAS = {
    'nlp', 'acl', 'emnlp', 'naacl',      # NLP
    'ai', 'aaai', 'ijcai',               # AI
    'iclr', 'icml', 'kdd', 'mlmining', 'nips',  # ML
}


def _download_raw(path):
    """下载 generated-author-info.csv 到本地临时路径。"""
    import urllib.request
    from urllib.request import Request
    request = Request(SOURCE_URL, headers={'User-Agent': lib.UA})
    with urllib.request.urlopen(request, timeout=120) as resp:
        with open(path, 'wb') as handle:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
    return path


def _aggregate(raw_path):
    """流式聚合：dept -> adjustedcount 总和（NLP+AI+ML，2021-2025）。"""
    totals = defaultdict(float)
    with open(raw_path, 'r', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            area = (row.get('area') or '').strip()
            year = (row.get('year') or '').strip()
            dept = (row.get('dept') or '').strip()
            if area not in NLP_AI_ML_AREAS:
                continue
            if not year.isdigit() or not (YEAR_MIN <= int(year) <= YEAR_MAX):
                continue
            if not dept:
                continue
            try:
                adjusted = float(row.get('adjustedcount') or 0)
            except ValueError:
                continue
            totals[dept] += adjusted
    return totals


def _rank(totals):
    """按 adjustedcount 降序排名，同分按机构名字典序。"""
    items = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked = []
    for idx, (dept, score) in enumerate(items, start=1):
        ranked.append({'rank': idx, 'institution': dept,
                       'adjustedcount': round(score, 4)})
    return ranked


def main() -> int:
    lib.ensure_dirs()
    raw_path = lib.SNAPSHOTS_DIR / 'csrankings_authorinfo_raw.csv'
    checked_at = lib.utc_now_iso()

    print('下载 CSRankings 数据（约 18MB）...')
    try:
        _download_raw(raw_path)
    except Exception as exc:
        print('错误：下载 CSRankings 数据失败：%r' % exc)
        return 1

    print('聚合 NLP+AI+ML 领域（%d-%d）...' % (YEAR_MIN, YEAR_MAX))
    totals = _aggregate(raw_path)

    # 删除原始 CSV（遵守再发布限制，不整表转存）
    try:
        raw_path.unlink()
    except OSError:
        pass

    ranked = _rank(totals)
    top100 = ranked[:100]

    rubric = {
        'protocol_version': lib.PROTOCOL_VERSION,
        'track': lib.TRACK,
        'dimension': 'A',
        'area_combination': 'NLP+AI+ML',
        'year_range': [YEAR_MIN, YEAR_MAX],
        'source': SOURCE_URL,
        'checked_at': checked_at,
        'provisional': True,
        'note': 'CSRankings 排名快照；待负责人确认后转正式 rubric。',
        'top25': top100[:25],
        'top50': top100[25:50],
        'top100': top100[50:100],
        'total_institutions': len(ranked),
    }
    rubric_path = lib.CONFIG_DIR / 'institution_rubric_agents_draft.json'
    lib.write_json(rubric_path, rubric)

    lib.log_review('phase3_csrankings', total=len(ranked), top100_saved=len(top100),
                   source=SOURCE_URL, checked_at=checked_at)

    print('CSRankings 排名完成')
    print('参与排名机构数：%d' % len(ranked))
    print('前 10 名：')
    for item in top100[:10]:
        print('  %2d. %-40s %.1f' % (item['rank'], item['institution'],
                                       item['adjustedcount']))
    print('rubric：%s' % rubric_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
