"""Deterministic reading-goal policy for survey / benchmark / research mixes.

The LLM only proposes a goal and per-paper types. This module owns the quota
numbers, fallback and ordering; no model output can silently change the budget.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

PAPER_TYPES = ('survey', 'benchmark', 'research')
GOALS = ('auto', 'orientation', 'comparison', 'frontier', 'general')

TYPE_LABELS = {'survey': '综述', 'benchmark': '评测/Benchmark', 'research': '研究论文'}
GOAL_LABELS = {
    'auto': '自动判断',
    'orientation': '新手入门',
    'comparison': '方法对比',
    'frontier': '前沿新 idea',
    'general': '通用推荐',
}

# (survey, benchmark, research) at K=3/4/5. K=5 is the product default.
MIX = {
    'orientation': {3: (1, 0, 2), 4: (2, 0, 2), 5: (2, 0, 3)},
    'comparison': {3: (0, 2, 1), 4: (0, 2, 2), 5: (0, 3, 2)},
    'frontier': {3: (0, 0, 3), 4: (0, 0, 4), 5: (1, 0, 4)},
    'general': {3: (1, 1, 1), 4: (1, 1, 2), 5: (1, 1, 3)},
}


def resolve_goal(goal: str, topic: str = '') -> str:
    goal = (goal or '').strip().lower()
    if goal in ('orientation', 'comparison', 'frontier', 'general'):
        return goal
    # Auto/empty is a request to infer; the planner normally supplies a concrete
    # profile. If it did not, use a balanced general mix rather than guessing.
    return 'general'


def allocate(target_count: int, goal: str) -> "OrderedDict[str, int]":
    from common import MIN_PAPERS, MAX_PAPERS, GuideError
    if type(target_count) is not int or not MIN_PAPERS <= target_count <= MAX_PAPERS:
        raise GuideError('推荐数量必须在 1–10 篇之间。')
    target = target_count
    profile = resolve_goal(goal)
    if target in MIX[profile]:
        survey, benchmark, research = MIX[profile][target]
    else:
        weights = MIX[profile][5]
        counts = [target * value // 5 for value in weights]
        for index in sorted(range(3), key=lambda i: (-(target * weights[i] % 5), -weights[i], i))[:target - sum(counts)]:
            counts[index] += 1
        survey, benchmark, research = counts
    return OrderedDict((name, count) for name, count in
                       (('survey', survey), ('benchmark', benchmark), ('research', research))
                       if count > 0)


def mix_text(mix) -> str:
    return ' · '.join(f'{TYPE_LABELS.get(k, k)} {v}' for k, v in mix.items())


def category_to_type(category: str) -> str:
    return {'evaluation': 'benchmark', 'survey': 'survey', 'research': 'research'}.get(category, '')


def type_from_assessment(row: dict, category: str = 'none') -> str:
    value = str(row.get('paper_type') or '').lower()
    if value in PAPER_TYPES:
        return value
    if value == 'unknown':
        return ''
    # Backward compatibility: old assessor only returned category_status.
    status = row.get('category_status')
    if status == 'yes':
        return category_to_type(category)
    return ''


@dataclass(frozen=True)
class IntentPolicy:
    goal: str
    target: int

    @property
    def mix(self):
        return allocate(self.target, self.goal)

    @property
    def label(self):
        return GOAL_LABELS.get(self.goal, self.goal)
