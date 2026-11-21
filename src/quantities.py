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


# The only accepted stop signal is a structured claim: a closed kind plus a
# verbatim quote. Nothing here reads the wording, so no paraphrase can be
# reclassified as "already satisfied" and no real restriction can be deleted by
# the program. A previous design tried to decide in Python whether a clause was
# already met; every wording change ("no scores, only surveys", "Only papers
# with DOI", "只收录用的论文") bypassed it, so that decision now belongs to the
# schema: either the planner names a kind that the product cannot verify, or the
# array is empty and the search proceeds without further interpretation.
BLOCKING_KINDS = (
    'code_release', 'dataset', 'award', 'author_or_affiliation',
    'citation_threshold', 'length_or_pages', 'language', 'venue_subset',
    'year_subset', 'publication_type', 'presentation_type', 'version_or_status',
    'topic_filter', 'output_format', 'other',
)
BLOCKING_QUOTE_LIMIT = 300
# One description per kind, so the prompt text, the enum and the docs cannot
# drift apart: the schema lists the enum, the prompt lists the same help.
BLOCKING_KIND_HELP = {
    'code_release': 'released source code or a repository link',
    'dataset': 'a public dataset or benchmark artefact',
    'award': 'best-paper or other award status',
    'author_or_affiliation': 'a named author, group, institution or industry lab',
    'citation_threshold': 'a citation count limit',
    'length_or_pages': 'page or length limits',
    'language': 'paper language',
    'venue_subset': 'a narrower venue subset than the venue field expresses (workshop, sub-track)',
    'year_subset': ('a year selection one inclusive range cannot express '
                    '(e.g. "2024 or 2026" only, or excluding a year inside the range)'),
    'publication_type': 'a publication type the publication_type field cannot express',
    'presentation_type': 'a presentation class outside oral/spotlight/poster',
    'version_or_status': 'preprint vs journal version, review or withdrawal status',
    'topic_filter': ('an extra content filter on top of the topic '
                     '(e.g. excluding surveys, only papers with DOI, 只收录…的论文)'),
    'output_format': 'a layout other than the delivered JSON (e.g. CSV, spreadsheet, LaTeX table)',
    'other': 'none of the above fits but the restriction still changes which papers qualify',
}


def blocking_kind_help() -> str:
    """The kind list as prompt text, generated from the enum itself."""
    assert tuple(BLOCKING_KIND_HELP) == BLOCKING_KINDS
    return '; '.join(f'{kind} ({BLOCKING_KIND_HELP[kind]})' for kind in BLOCKING_KINDS)


def blocking_quotes(items, source_text, *, field='blocking_requirements') -> list:
    """Validate the structured stop contract shared by both entry points.

    Returns the verbatim quotes in order. The same array stops retrieval before
    any search (one-shot workflow) or before the next turn (stateful session), so
    one message cannot be judged differently by the two entry points.
    """
    if items is None:
        # A missing field is not "no restrictions": the array is required so
        # an omitted answer cannot silently pass as empty.
        raise GuideError(f'{field} is required; use [] only when nothing blocks.')
    if not isinstance(items, list) or isinstance(items, (str, bytes)):
        raise GuideError(f'{field} must be an array of {{kind, quote}} objects.')
    text = str(source_text or '').casefold()
    quotes = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {'kind', 'quote'}:
            raise GuideError(f'{field} entries must contain exactly kind and quote.')
        if item['kind'] not in BLOCKING_KINDS:
            raise GuideError(f'{field} kind must be one of: ' + ', '.join(BLOCKING_KINDS))
        quote = item['quote']
        if not isinstance(quote, str) or not quote.strip():
            raise GuideError(f'{field} quote must be a non-empty string.')
        if len(quote) > BLOCKING_QUOTE_LIMIT:
            raise GuideError(f'{field} quote must be at most {BLOCKING_QUOTE_LIMIT} characters.')
        if quote.casefold() not in text:
            raise GuideError(f'{field} quote must be an exact excerpt of the user input.')
        if quote not in quotes:
            quotes.append(quote)
    return quotes


def redundant_scope_requirements(plan, items) -> tuple:
    """Absorb scope entries that restate a value the plan already carries.

    The planner sometimes reports the venue list or the years it already set, or
    reports them *instead of* setting the field. Both are handled structurally,
    without interpreting prose:

    * a ``venue_subset`` whose quote names only venues from the curated catalogue
      fills ``venue`` when it is empty, and is redundant when it equals the
      applied set (or names no venue at all);
    * a ``year_subset`` whose quote names years fills ``year_from``/``year_to``
      when they are unset and form one contiguous range, and is redundant when it
      equals the applied range (or names no year at all).

    A genuinely narrower request (only CVPR while six are applied, 2025 inside
    2024-2026) is kept and still blocks. Only explicit values are compared.
    """
    kept, dropped = [], []
    for item in items or []:
        if not isinstance(item, dict):
            kept.append(item)  # malformed entries are rejected by the validator
            continue
        kind = item.get('kind')
        quote = str(item.get('quote') or '')
        if kind == 'venue_subset':
            if _absorb_venue(plan, quote):
                dropped.append(quote)
                continue
            if _redundant_venue(plan, quote):
                dropped.append(quote)
                continue
        elif kind == 'year_subset':
            if _absorb_years(plan, quote):
                dropped.append(quote)
                continue
            if _redundant_years(plan, quote):
                dropped.append(quote)
                continue
        kept.append(item)
    return kept, dropped


def _absorb_venue(plan, quote: str) -> bool:
    """Fill an empty venue field from a quote that names only known venues."""
    if str(plan.get('venue') or '').strip():
        return False
    import re as _re
    from research_policy import curated_venue_catalog
    names = [v['name'] for v in curated_venue_catalog()['venues']] + ['ACM MM', 'SIGGRAPH']
    # Longest names first so "ACM MM" is not split into two tokens.
    found = []
    for name in sorted(set(names), key=len, reverse=True):
        match = _re.search(r'(?<![A-Za-z0-9])' + _re.escape(name) + r'(?![A-Za-z0-9])', quote, _re.I)
        if match:
            found.append((match.start(), name))
    if not found:
        return False
    ordered = []
    for _, name in sorted(found):
        if name.upper() not in {item.upper() for item in ordered}:
            ordered.append(name)
    plan['venue'] = ', '.join(ordered)
    return True


def _absorb_years(plan, quote: str) -> bool:
    """Fill an unset year range from a quote that names one contiguous range."""
    if plan.get('year_from') is not None or plan.get('year_to') is not None:
        return False
    text = str(quote)
    years = sorted({int(value) for value in re.findall(r'\b(?:19|20)\d{2}\b', text)})
    if not years or years[0] < 1900 or years[-1] > 2100:
        return False
    if len(years) > 1 and years[-1] - years[0] + 1 != len(years):
        span = re.search(r'((?:19|20)\d{2})\s*(?:-|–|—|~|to|至|到)\s*((?:19|20)\d{2})', text)
        if not span:
            return False
        low, high = int(span.group(1)), int(span.group(2))
        if high < low:
            return False
        years = list(range(low, high + 1))
    plan['year_from'], plan['year_to'] = years[0], years[-1]
    return True


def _applied_venues(plan) -> dict:
    from common import requested_venues
    from research_policy import venue_identity
    value = str(plan.get('venue') or '').strip()
    return {name: venue_identity(name) for name in requested_venues(value) if venue_identity(name)}


def _redundant_venue(plan, quote: str) -> bool:
    applied = _applied_venues(plan)
    if not applied:
        return False
    folded = str(quote).casefold()
    named = {identity for name, identity in applied.items() if name.casefold() in folded}
    if not named:
        return True  # refers to the provided whitelist without naming a venue
    return named == set(applied.values())


def _redundant_years(plan, quote: str) -> bool:
    year_from, year_to = plan.get('year_from'), plan.get('year_to')
    if not isinstance(year_from, int) or not isinstance(year_to, int) or year_from > year_to:
        return False
    text = str(quote)
    mentioned = {int(value) for value in re.findall(r'\b(?:19|20)\d{2}\b', text)}
    if not mentioned:
        return True  # refers to the provided year scope without naming a year
    if mentioned == set(range(year_from, year_to + 1)):
        return True
    # "2024-2026" names only the endpoints of the same inclusive range.
    span = re.search(r'((?:19|20)\d{2})\s*(?:-|–|—|~|to|至|到)\s*((?:19|20)\d{2})', text)
    if span:
        low, high = int(span.group(1)), int(span.group(2))
        if low == year_from and high == year_to:
            return True
    return False


def blocking_requirement_schema() -> dict:
    """One schema fragment for the planner and the task updater."""
    return {'type': 'array', 'items': {
        'type': 'object',
        'properties': {
            'kind': {'type': 'string', 'enum': list(BLOCKING_KINDS)},
            'quote': {'type': 'string', 'maxLength': BLOCKING_QUOTE_LIMIT},
        },
        'required': ['kind', 'quote'],
        'additionalProperties': False,
    }}


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
