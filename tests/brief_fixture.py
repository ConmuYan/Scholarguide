"""Test-only adapter from legacy semantic fixtures to the v3 ID-only wire."""
from copy import deepcopy


def packet_papers(data):
    """Rehydrate old test fixtures from the exact snippets supplied to the model."""
    bank = data.get('evidence_bank') or []
    return [{**paper, 'abstract': ' '.join(item['quote'] for item in bank
                                         if item['paper_id'] == paper['paper_id'])}
            for paper in data['papers']]


def wire_output(raw, data):
    if 'evidence' not in raw:
        return deepcopy(raw)
    value = deepcopy(raw)
    bank = data['evidence_bank']
    aliases = {}
    pool = []
    for old in value.pop('evidence'):
        quote = old.get('quote')
        matches = [item for item in bank if item['paper_id'] == old.get('paper_id')
                   and isinstance(quote, str) and len(' '.join(quote.split())) >= 12
                   and (quote == item['quote'] or item['quote'] in quote)]
        key = matches[0]['evidence_id'] if matches else 'INVALID_FIXTURE_' + old.get('evidence_id', '')
        aliases[old.get('evidence_id')] = key
        pool.append(key)
    def refs(item):
        if isinstance(item, dict):
            for key, value in item.items():
                if key == 'evidence_ids' and isinstance(value, list):
                    item[key] = [aliases.get(identity, identity) for identity in value]
                else:
                    refs(value)
        elif isinstance(item, list):
            for value in item:
                refs(value)
    refs(value)
    value['evidence_ids'] = pool
    return value
