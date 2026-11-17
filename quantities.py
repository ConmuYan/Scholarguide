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


# A clause is only treated as already satisfied on positive evidence: the plan
# carries the scope, or the delivered result guarantees the presentation. Real
# eligibility constraints (code, awards, authors, thresholds) are never dropped.
_OUTPUT_GUARANTEE = re.compile(
    r'\b(?:rank(?:ed|ing)?|number(?:ed|ing)?|null|json|gaps?|labels?|scores?'
    r'|fabricat\w*|invent\w*|hallucinat\w*)\b', re.I)
_HARD_CONSTRAINT = re.compile(
    r'\b(?:source\s+code|code|repositor(?:y|ies)|github|dataset|benchmark\s+dataset'
    r'|best[\s-]+paper|award|author|affiliation|institution|impact\s+factor'
    r'|acceptance\s+rate|h-index)\b|\d+\s*(?:citations?|stars?|downloads?)'
    r'|代码|开源|仓库|数据集|最佳论文|获奖|作者|机构|引用数|下载量', re.I)
# A custom layout is never satisfied by the JSON result above.
_CUSTOM_FORMAT = re.compile(
    r'\b(?:csv|tsv|excel|xlsx|spreadsheet|markdown\s+table|yaml|xml|html|sql|latex|bibtex)\b'
    r'|表格|电子表格', re.I)
_NEGATION = re.compile(
    r'\b(?:no|not|never|without|exclude|avoid|omit|do\s+not|don\'t)\b|不要|无需|不含|不包括|不能包含|避免',
    re.I)
_LABEL_WORDS = re.compile(r'\b(?:labels?|scores?|benchmark\s+labels?)\b|标签|评分', re.I)
_SCOPE_WORDS = re.compile(
    r'\b(?:venues?|conferences?|whitelist|publication\s+years?|years?)\b|会议|年份|年限', re.I)
_GENERIC_SCOPE = re.compile(r'\b(?:provided|given|specified|eligible|whitelist)\b|给定|指定|所列|白名单', re.I)
# Excluding specific papers/venues/years is a real eligibility restriction; the
# product cannot express it, so such a clause is never treated as satisfied.
_EXCLUSION = re.compile(
    r'\b(?:excluding|exclude[sd]?|except|skip(?:ping)?|without|omit(?:ting)?|but\s+not'
    r'|not\s+including|\bnot\b)\b|排除|不含|不包括|跳过|除…?外', re.I)
_STOPWORDS = frozenset({'ONLY', 'AND', 'OR', 'JSON', 'DOI', 'URL', 'USE', 'NOT', 'ALL', 'ANY',
                        'FROM', 'WITH', 'THE', 'FOR', 'ONLY.'})


# Scope clauses are satisfied only on positive evidence. The text may name the
# applied conferences and years plus neutral scope wording; anything else (a
# negation, an exclusion, a filter, a different venue) is unknown content and
# keeps the clause unsupported. A blacklist of negative words would leak as soon
# as the wording changed, so the rule is an allow-list instead.
_SCOPE_ALLOWED = frozenset("""
only use using provided give given specified eligible whitelist
conference conferences venue venues publication publications year years
paper papers article articles from to and or between restrict restricted
limited limit within range inclusive the these those list as in of a an
that are is be published select selected consider considered include included
""".split())
_CJK_SCOPE_ALLOWED = frozenset('只收的在仅年限会议期刊论文发表收录使用范围到等和与及内')


def _scope_only(text: str, venue: str) -> bool:
    """True when nothing but the applied scope is being said."""
    from common import requested_venues
    residue = text
    for name in sorted(requested_venues(venue), key=len, reverse=True):
        residue = re.sub(re.escape(name), ' ', residue, flags=re.I)
    residue = re.sub('[0-9]{4}', ' ', residue)
    residue = re.sub('[^0-9A-Za-z一-鿿]+', ' ', residue)
    for token in residue.split():
        if token.isdigit():
            continue
        if token.isascii():
            if token.casefold() not in _SCOPE_ALLOWED:
                return False
        elif any(char not in _CJK_SCOPE_ALLOWED for char in token):
            return False
    return True


def clause_is_satisfied(plan: dict, clause) -> bool:
    """True only when the plan or the delivery contract already satisfies a clause.

    The planner may echo a supported instruction into ``unsupported`` (for example
    "number each paper" or "use the given venue list"). Stopping the search for
    those would reject requests the product does implement, so they are recognized
    here. Anything that could change which papers qualify stays unsupported.
    """
    text = ' '.join(str(clause or '').split())
    if not text or not isinstance(plan, dict):
        return False
    # Any eligibility, custom-layout or non-negated label requirement stays hard.
    if _HARD_CONSTRAINT.search(text) or _CUSTOM_FORMAT.search(text):
        return False
    if _LABEL_WORDS.search(text) and not _NEGATION.search(text):
        return False
    has_range = bool(re.search(r'(?:19|20)\d{2}\s*(?:-|–|—|~|to|至|到)\s*(?:19|20)\d{2}', text))
    mentioned = {int(value) for value in re.findall(r'\b(?:19|20)\d{2}\b', text)}
    venue = str(plan.get('venue') or '').strip()
    year_from, year_to = plan.get('year_from'), plan.get('year_to')
    bounded_years = isinstance(year_from, int) and isinstance(year_to, int)
    if _OUTPUT_GUARANTEE.search(text) and not _SCOPE_WORDS.search(text):
        if re.search(r'\b(?:citations?|influen\w*|impact)\b', text, re.I) \
                and plan.get('ranking') != 'citations':
            return False
        # A clause that also states a period must be satisfied by that period too.
        if mentioned:
            return bounded_years and min(mentioned) <= year_from and year_to <= max(mentioned)
        return True
    from common import requested_venues
    from research_policy import venue_identity
    applied = {venue_identity(name) for name in requested_venues(venue)}
    named = {match.group(0) for match in re.finditer(r'\b[A-Z][A-Za-z0-9&.-]{1,19}\b', text)
             if match.start() > 0 and match.group(0) not in _STOPWORDS}
    if not _SCOPE_WORDS.search(text) and not has_range and not named:
        return False
    if not _scope_only(text, venue):
        return False
    if named and not all(venue_identity(token) in applied for token in named):
        return False
    if mentioned:
        # The applied window must sit inside the requested one, never the reverse.
        return bounded_years and min(mentioned) <= year_from and year_to <= max(mentioned)
    if named:
        return True  # every named venue is already applied
    return bool(venue) and bounded_years and bool(_GENERIC_SCOPE.search(text))

INFLUENCE_CLAUSE = re.compile(
    r'^(?:rank|order)(?:\s+the\s+papers)?\s+by\s+(?:research\s+)?'
    r'(?:influence|citation(?:\s+count)?s?)$', re.I)
PAIRED_SIGNIFICANCE = re.compile(r'\bmost\s+influential\s+and\s+research-significant\b', re.I)
_PAIRED_LABELS = frozenset({'research-significant', 'most influential and research-significant'})


def calibrate_ranking_requirements(plan: dict, message: str, unsupported) -> list:
    """Recognize only ranking phrases the product can express, in one place.

    Both the one-shot workflow and the stateful session use this so the same
    message cannot be judged differently by the two entry points.
    """
    remaining = []
    for clause in unsupported or []:
        text = str(clause).strip(' .')
        if INFLUENCE_CLAUSE.fullmatch(text):
            plan['ranking'] = 'citations'
            continue
        if (PAIRED_SIGNIFICANCE.search(message or '')
                and text.casefold() in _PAIRED_LABELS):
            plan['ranking'] = 'citations'
            continue
        remaining.append(clause)
    return remaining


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
