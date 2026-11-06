"""导出 Gold 完整榜单为 Markdown + CSV（按 Q 降序、同分 paper_id 升序）。

运行：python3 frontierlitbench/scripts/export_leaderboard.py
"""
from __future__ import annotations

import csv

import common_lib as lib


def load_gold():
    gold = lib.read_jsonl(lib.CORPUS_DIR / 'gold_agents.jsonl')
    gold.sort(key=lambda r: (-(r.get('Q') or 0), r.get('paper_id') or ''))
    return gold


def write_csv(gold, path):
    with path.open('w', encoding='utf-8', newline='') as handle:
        w = csv.writer(handle)
        w.writerow(['rank', 'tier', 'venue', 'year', 'paper_id', 'title', 'Q',
                    'C', 'G', 'A', 'citation_count', 'github_url',
                    'github_stars', 'github_method'])
        for i, r in enumerate(gold, 1):
            w.writerow([i, r.get('tier'), r.get('venue'), r.get('year'),
                        r.get('paper_id'), r.get('title'),
                        round(r.get('Q') or 0, 6), round(r.get('C') or 0, 4),
                        round(r.get('G') or 0, 4), round(r.get('A') or 0, 4),
                        r.get('citation_count'), r.get('github_url'),
                        r.get('github_stars'), r.get('github_method')])


def write_md(gold, path):
    lines = ['# FrontierLitBench v1.0 Track C Gold 榜单', '',
             '- Gold 篇数：%d（S=%d / A=%d / B=%d）' % (
                 len(gold),
                 sum(1 for r in gold if r['tier'] == 'S'),
                 sum(1 for r in gold if r['tier'] == 'A'),
                 sum(1 for r in gold if r['tier'] == 'B')),
             '- 排序：Q 降序，同分按 paper_id 升序', '',
             '| 排名 | 档位 | 会议 | 年份 | Q | C | G | A | 标题 |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for i, r in enumerate(gold, 1):
        lines.append('| %d | %s | %s | %s | %.3f | %.2f | %.2f | %.2f | %s |' % (
            i, r.get('tier'), r.get('venue'), r.get('year'),
            r.get('Q') or 0, r.get('C') or 0, r.get('G') or 0, r.get('A') or 0,
            (r.get('title') or '').replace('|', '\\|')))
    lines += ['', '## 说明', '',
              '- Q = 0.55·C + 0.30·G + 0.15·A（中位秩百分位）',
              '- C：Track C × 年份分层的引用百分位（OpenAlex）',
              '- G：同年份 official 仓库的 Stars 百分位（GitHub）',
              '- A：机构百分位（CSRankings NLP+AI+ML 2021-2025）', '']
    path.write_text('\n'.join(lines), encoding='utf-8')


def main() -> int:
    lib.ensure_dirs()
    gold = load_gold()
    write_csv(gold, lib.REPORTS_DIR / 'gold_leaderboard_agents.csv')
    write_md(gold, lib.REPORTS_DIR / 'gold_leaderboard_agents.md')
    print('已导出 %d 篇榜单' % len(gold))
    print('  MD  : %s' % (lib.REPORTS_DIR / 'gold_leaderboard_agents.md'))
    print('  CSV : %s' % (lib.REPORTS_DIR / 'gold_leaderboard_agents.csv'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
