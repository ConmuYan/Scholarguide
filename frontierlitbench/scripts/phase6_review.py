"""Phase 6：人工复核包 + 覆盖统计 + 敏感性 + 清单。

产出：
- review/sample_for_human_review.csv（≥25% 抽样，固定随机种子）
- review/open_issues.md（boundary_case + needs_lead_decision 汇总）
- reports/coverage_and_missingness_agents.csv（年份×Venue×子方向 + 缺失统计）
- reports/sensitivity_agents.md（敏感性分析，只报告不改主榜）
- manifests/corpus_sha256_agents.json（交付物 SHA-256）

运行：python3 frontierlitbench/scripts/phase6_review.py
"""
from __future__ import annotations

import csv
import random
from collections import Counter

import common_lib as lib

SAMPLE_SEED = 42


def _gold_100pct_check(gold):
    """Gold 关键书目身份 100% 程序化校验。"""
    issues = []
    seen_ids = set()
    for record in gold:
        pid = record.get('paper_id')
        if not pid:
            issues.append('missing_paper_id')
        if pid in seen_ids:
            issues.append('duplicate_paper_id:%s' % pid)
        seen_ids.add(pid)
        for field in ('title', 'year', 'venue'):
            if record.get(field) in (None, ''):
                issues.append('missing_%s:%s' % (field, pid))
    return issues


def _sample_csv(gold, path):
    """抽取 ≥25% 样本生成人工复核清单。"""
    random.seed(SAMPLE_SEED)
    sample = random.sample(gold, max(1, int(len(gold) * 0.25))) if gold else []
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['paper_id', 'title', 'venue', 'year', 'tier', 'Q',
                         'evidence_links'])
        for record in sample:
            links = '; '.join(record.get('dois', []) + record.get('arxiv_ids', []))
            writer.writerow([record.get('paper_id'), record.get('title'),
                             record.get('venue'), record.get('year'),
                             record.get('tier'), record.get('Q'), links])
    return len(sample)


def _open_issues(features, path):
    """boundary_case 与 needs_lead_decision 汇总。"""
    boundary = [r for r in features if r.get('boundary_case')]
    lead = [r for r in features if r.get('needs_lead_decision')]
    lines = ['# Open Issues（Track C Agents）', '',
             '## boundary_case（需两名标注者复核 Track 归属）',
             '数量：%d' % len(boundary), '']
    for r in boundary:
        lines.append('- %s | %s %s | %s | notes=%s' % (
            r.get('paper_id'), r.get('venue'), r.get('year'),
            r.get('title'), ','.join(r.get('track_notes', []))))
    lines += ['', '## needs_lead_decision（待负责人裁决）',
              '数量：%d' % len(lead), '']
    for r in lead:
        lines.append('- %s | %s %s | %s | sub_area=%s' % (
            r.get('paper_id'), r.get('venue'), r.get('year'),
            r.get('title'), r.get('sub_area', '')))
    lines += ['', '## 其他待决事项', '',
              '- G 维度（GitHub 官方仓库）全部 unverified，需人工核验后重跑 '
              'phase3_github.py 与 phase4_score_tier.py。', '']
    path.write_text('\n'.join(lines), encoding='utf-8')


def _coverage_csv(features, path):
    """年份×Venue×子方向分布 + 三维度缺失/unverified 统计。"""
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['venue', 'year', 'sub_area', 'count',
                         'citation_verified', 'institution_verified',
                         'github_verified'])
        for r in features:
            venue = r.get('venue')
            year = r.get('year')
            for sub in r.get('sub_areas', []) or ['']:
                writer.writerow([
                    venue, year, sub, 1,
                    1 if r.get('citation_status') == 'verified' else 0,
                    1 if r.get('institution_status') == 'verified' else 0,
                    1 if r.get('repo_status') in ('official', 'none_available',
                                                  'not_applicable') else 0,
                ])


def _sensitivity_md(gold, path):
    """敏感性分析：只报告，不改主榜。"""
    lines = ['# 敏感性分析（Track C Agents）', '',
             '主榜 Gold 数量：%d' % len(gold), '']
    if not gold:
        lines += ['由于 G 维度（GitHub）全部 unverified，准入 Gold 为 0，',
                  '敏感性分析无有效样本。待 G 维度人工核验后可重跑本阶段。', '']
    else:
        lines += ['- (a) 去掉 A 后，C 与 G 按 0.55/0.30 重新归一化再分档；',
                  '- (b) 只用 C 排序。',
                  '与主档位相比等级变化的论文数将在此列出。', '']
    path.write_text('\n'.join(lines), encoding='utf-8')


def _manifest(path):
    """对全部交付物计算 SHA-256（排除脚本与缓存目录）。"""
    files = sorted([p for p in lib.FB_ROOT.rglob('*')
                    if p.is_file()
                    and 'scripts' not in p.parts
                    and 'cache' not in p.parts
                    and '__pycache__' not in p.parts
                    and p.name not in ('review_log_agents.jsonl', '.DS_Store')])
    manifest = {'generated_at': lib.utc_now_iso(), 'files': {}}
    for f in files:
        manifest['files'][str(f.relative_to(lib.FB_ROOT))] = lib.sha256_file(f)
    lib.write_json(path, manifest)
    return len(files)


def main() -> int:
    lib.ensure_dirs()
    gold = lib.read_jsonl(lib.CORPUS_DIR / 'gold_agents.jsonl')
    features = lib.read_jsonl(lib.CORPUS_DIR / 'features_agents.jsonl')

    issues = _gold_100pct_check(gold)
    sample_n = _sample_csv(gold, lib.REVIEW_DIR / 'sample_for_human_review.csv')
    _open_issues(features, lib.REVIEW_DIR / 'open_issues.md')
    _coverage_csv(features, lib.REPORTS_DIR / 'coverage_and_missingness_agents.csv')
    _sensitivity_md(gold, lib.REPORTS_DIR / 'sensitivity_agents.md')
    file_n = _manifest(lib.MANIFESTS_DIR / 'corpus_sha256_agents.json')

    lib.log_review('phase6_review', gold=len(gold), sample=sample_n,
                   gold_check_issues=len(issues), manifest_files=file_n)

    print('Phase 6 完成')
    print('Gold：%d | 抽样：%d | 书目身份问题：%d | 清单文件：%d' % (
        len(gold), sample_n, len(issues), file_n))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
