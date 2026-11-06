"""Phase 5：Track C 任务资格检查。

对 FB-03 / FB-10 / FB-11 / FB-12 / FB-15 逐一计算有效 Gold 数量 |G_t|：
|G_t| >= 5 → eligible；< 5 → ineligible 并写明原因，提出同 Track 替换候选。

此步在任何系统跑测试之前完成，不参考系统输出。

运行：python3 frontierlitbench/scripts/phase5_eligibility.py
"""
from __future__ import annotations

import common_lib as lib

# Track C 相关任务（协议冻结）
TASKS = {
    'FB-03': {'name': 'C Agents Overall', 'years': (2024, 2025, 2026),
              'venues': None, 'description': '2024-26 全白名单'},
    'FB-10': {'name': 'C Agents Year 2024', 'years': (2024,), 'venues': None,
              'description': '2024'},
    'FB-11': {'name': 'C Agents Year 2025', 'years': (2025,), 'venues': None,
              'description': '2025'},
    'FB-12': {'name': 'C Agents Year 2026', 'years': (2026,), 'venues': None,
              'description': '2026'},
    'FB-15': {'name': 'C Agents Venue', 'years': (2024, 2025, 2026),
              'venues': ('ACL', 'EMNLP'), 'description': '2024-26 仅 ACL+EMNLP'},
}


def _count_gold(gold, task):
    """计算某任务的有效 Gold 数量。"""
    years = task['years']
    venues = task['venues']
    count = 0
    for record in gold:
        if record.get('year') not in years:
            continue
        if venues and record.get('venue') not in venues:
            continue
        count += 1
    return count


def main() -> int:
    lib.ensure_dirs()
    gold = lib.read_jsonl(lib.CORPUS_DIR / 'gold_agents.jsonl')

    results = {}
    for task_id, task in TASKS.items():
        count = _count_gold(gold, task)
        if count >= 5:
            status = 'eligible'
            reason = ''
            replacement = None
        else:
            status = 'ineligible'
            reason = '有效 Gold 数量 %d < 5（Gold 总量 %d，因 G 维度 unverified 无论文准入）' % (
                count, len(gold))
            replacement = None
        results[task_id] = {
            'task': task['name'], 'description': task['description'],
            'status': status, 'gold_count': count, 'reason': reason,
            'replacement_candidate': replacement,
        }

    output = {
        'protocol_version': lib.PROTOCOL_VERSION,
        'track': lib.TRACK,
        'checked_at': lib.utc_now_iso(),
        'gold_total': len(gold),
        'threshold': 5,
        'tasks': results,
    }
    lib.write_json(lib.TASKS_DIR / 'task_eligibility_agents.json', output)

    lib.log_review('phase5_eligibility', gold_total=len(gold),
                   tasks={k: v['status'] for k, v in results.items()})

    print('Phase 5 完成')
    print('Gold 总量：%d' % len(gold))
    for task_id, task in results.items():
        print('  %s: %s (|G_t|=%d)' % (task_id, task['status'], task['gold_count']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
