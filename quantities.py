"""Quantity instructions shared by CLI, Search and Chat, independent of models.

Numbers require an explicit paper unit, so years and ordinal exclusions are not
mistaken for requested list sizes. Category additions preserve the existing total.
"""
import re

from common import DEFAULT_PAPERS, MAX_PAPERS, GuideError

WORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
         'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11,
         'twelve': 12, 'fifteen': 15, 'twenty': 20, 'dozen': 12,
         '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
         '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
NUM = r'(?:\d+|twenty|fifteen|twelve|eleven|dozen|three|seven|eight|four|five|nine|one|two|six|ten|[零一二两三四五六七八九十百千万]+)'
PATTERN = re.compile(r'(?<![\d\w])(?P<a>' + NUM + r')\s*(?:(?:-|–|—|~|～|到|至|\bto\b)\s*(?P<b>' + NUM + r')\s*)?(?:(?:[A-Za-z][A-Za-z0-9-]*\s+){0,6})?(?:papers?|articles?|studies)\b|(?P<ca>' + NUM + r')\s*(?:(?:-|–|—|~|～|到|至)\s*(?P<cb>' + NUM + r')\s*)?篇', re.I)


def number(value):
    value = value.lower()
    if value.isdigit():
        return int(value)
    if value in WORDS:
        return WORDS[value]
    if any(unit in value for unit in '百千万'):
        return MAX_PAPERS + 1  # Every such explicit count exceeds this product cap.
    if value == '零':
        return 0
    if '十' in value:
        left, right = value.split('十', 1)
        return WORDS.get(left, 1) * 10 + WORDS.get(right, 0)
    raise GuideError('无法识别论文数量。')


def quantity_constraints(text, *, default=False):
    if not isinstance(text, str):
        return {'target_count': DEFAULT_PAPERS} if default else {}
    for match in PATTERN.finditer(text):
        before = text[max(0, match.start() - 55):match.start()].lower()
        after = text[match.end():match.end() + 35].lower()
        body = match.group(0).lower()
        if re.search(r'\b(?:hundred|thousand|million)\b', body):
            raise GuideError('每次推荐支持 1–10 篇；请在上限内调整数量。')
        if before.endswith('第'):
            continue  # 第一篇/第三篇 identifies an existing recommendation, not list size.
        # "including two benchmark papers" specifies a category quota, not K.
        category = bool(re.search(r'evaluation|benchmark|survey|评测|综述|研究论文', body + after))
        if (re.search(r'(?:including|include|add|其中|包括|包含|增加|加入)\s*$', before)
                or (category and re.search(r'(?:至少|at\s+least)\s*$', before))):
            continue
        if re.search(r'(?:remove|drop|exclude|删掉|排除|去掉|移除)\s*(?:the\s*)?$', before):
            continue
        first = number(match.group('a') or match.group('ca'))
        if 1900 <= first <= 2100 and match.group('a') and not re.search(r'(?:recommend|give|list|provide|find|推荐|给出|列出|查找)\s*$', before):
            continue  # "CVPR 2025 papers" expresses a publication year.
        second = match.group('b') or match.group('cb')
        if second:
            upper = number(second)
            lower = first
        elif re.search(r'(?:至少|不少于|at\s+least)\s*$', before):
            lower, upper = first, MAX_PAPERS
        elif re.search(r'(?:最多|不超过|至多|at\s+most|up\s+to)\s*$', before):
            lower, upper = 1, first
        else:
            lower = upper = first
        if not 1 <= lower <= upper <= MAX_PAPERS:
            raise GuideError('每次推荐支持 1–10 篇；你指定的数量超出上限或区间无效，请在 1–10 篇内选择。')
        target = lower if lower == upper else min(upper, max(lower, DEFAULT_PAPERS))
        return {'target_count': target, 'minimum_count': lower, 'maximum_count': upper}
    return {'target_count': DEFAULT_PAPERS, 'minimum_count': None, 'maximum_count': None} if default else {}


def apply_constraints(task, constraints=None, *, message='', initial=False, goal_hint=None):
    """Merge explicit user quantity/API filters before task validation or execution."""
    from dataclasses import asdict
    from common import Request
    merged = dict(task)
    count = quantity_constraints(message, default=initial and 'target_count' not in merged)
    merged.update(count)
    overrides = dict(constraints or {})
    if 'target_count' in overrides and not {'minimum_count', 'maximum_count'} & set(overrides):
        overrides.update(minimum_count=None, maximum_count=None)
    merged.update(overrides)
    if goal_hint in ('orientation', 'comparison', 'frontier', 'general'):
        merged['reading_goal'] = goal_hint
    if not str(merged.get('topic') or '').strip():
        checked = Request.from_dict({**merged, 'topic': 'awaiting clarification'})
        for key in ('target_count', 'minimum_count', 'maximum_count'):
            merged[key] = getattr(checked, key)
        return merged
    request = Request.from_dict(merged)
    merged.update(asdict(request))
    return merged


def calibrate_user_requirements(proposal, message, previous=None):
    """Keep model inferences from becoming unrequested acceptance conditions."""
    result, previous = dict(proposal), previous or {}
    message = message or ''
    requested = bool(re.search(
        r'(?<![A-Za-z])(?:accept(?:ed|ance)?|publish(?:ed|ing)?|peer[- ]reviewed|oral|spotlight|poster)(?![A-Za-z])'
        r'|录用|发表|正式|顶会|顶刊|(?:only|仅|只).{0,12}(?:top|conference|journal)', message, re.I))
    if result.get('accepted_only') is True and not previous.get('accepted_only') and not requested:
        result['accepted_only'] = False
    kinds = result.get('presentation_types')
    if kinds and not previous.get('presentation_types'):
        result['presentation_types'] = [kind for kind in kinds
            if re.search(r'(?<![A-Za-z])' + re.escape(kind) + r'(?![A-Za-z])', message, re.I)]
    return result
