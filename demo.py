"""Offline fixtures for teaching/tests. No simulated result is presented as live."""
from dataclasses import replace
import json
import re

from common import Budget, Config, Paper, ROOT
from workflow import run


class DemoSource:
    mode = 'demo'

    def __init__(self, scenario='followup'):
        self.scenario = scenario
        self.calls = 0
        self.papers = [Paper(**p) for p in json.loads((ROOT / 'examples/papers.json').read_text(encoding='utf-8'))['papers']]

    def search(self, query, limit=10):
        self.calls += 1
        if self.scenario == 'satisfied':
            selected = self.papers
        elif self.scenario == 'insufficient':
            selected = self.papers[:2]
        else:
            selected = self.papers[:5] if self.calls == 1 else self.papers[4:]
        return [replace(p, from_query=[query]) for p in selected[:limit]]


class DemoLLM:
    mode = 'demo'
    events = []

    def generate(self, stage, system, data, schema, validate):
        if stage == 'plan':
            result = {'topic': 'retrieval augmented generation', 'category': 'evaluation',
                      'minimum_category_count': 2, 'target_count': 5, 'unsupported': [],
                      'query': 'retrieval augmented generation'}
        elif stage == 'refine':
            result = {'query': 'retrieval augmented generation evaluation benchmark'}
        else:
            rows = []
            for p in data['papers']:
                # Illustrative fixture labels, not a general keyword classifier or human gold data.
                is_eval = p['paper_id'] in ('arxiv:2309.15217', 'arxiv:2311.09476')
                rows.append({'paper_id': p['paper_id'], 'relevance': 2,
                             'category_status': 'yes' if is_eval else 'no',
                             'evidence_quote': p['abstract'].split('. ')[0] + ('.' if '. ' in p['abstract'] else ''),
                             'reason': 'Fixture explanation: this abstract discusses retrieval-augmented NLP.'})
            result = {'assessments': rows}
        validate(result)
        return result


def demo_run(scenario='followup', method='A', progress=None, config=None, budget=None):
    # Demo never calls the model, but carrying the active provider/profile in the
    # metadata keeps the topbar and the trace from disagreeing about identity.
    return run('Offline fixture: five RAG papers including two evaluation papers.',
               config or Config(), method, source=DemoSource(scenario),
               llm=DemoLLM(), budget=budget or Budget(30), progress=progress)


class ChatDemoLLM(DemoLLM):
    """Deterministic multi-turn fixtures for the v2 chat path (offline)."""

    def __init__(self, ask_first: bool = False):
        self.ask_first = ask_first
        self.asked = False

    def generate(self, stage, system, data, schema, validate):
        if stage == 'update':
            patch = {}
            task = data.get('task') or {}
            message = str(data.get('message') or '')
            if not task.get('topic'):
                patch['topic'] = 'retrieval augmented generation'
            # Only support the explicit first-paper action and exact IDs from
            # the last delivered list. This is an offline fixture, not a
            # general natural-language parser; never invent an exclusion.
            delivered = data.get('last_recommendations') or []
            text = message.strip()
            excluded = []
            if re.match(r'^(?:请\s*)?(?:排除|去掉|移除)\s*(?:第一篇|第1篇)(?:[，。,.！!；;\s]|$)', text):
                excluded = [str(delivered[0].get('paper_id') or '')] if delivered else []
            else:
                for paper in delivered:
                    identity = str(paper.get('paper_id') or '')
                    if identity and re.match(r'^(?:请\s*)?排除论文\s*' + re.escape(identity)
                                             + r'(?=[\s（(，,。.;；]|$)', text, re.I):
                        excluded.append(identity)
            excluded = [identity for identity in excluded if identity]
            if excluded:
                patch['excluded_paper_ids'] = list(dict.fromkeys(
                    [*(task.get('excluded_paper_ids') or []), *excluded]))
            if 'survey' in message.lower():
                patch['category'] = 'survey'
                patch['minimum_category_count'] = 1
            unresolved = []
            if self.ask_first and not self.asked:
                unresolved = [{'field': 'purpose',
                               'question': 'Is this reading list for a survey or for experiments?',
                               'blocking': True}]
            result = {'patch': patch, 'summary': 'demo task update', 'unresolved': unresolved}
            validate(result)
            return result
        if stage == 'action':
            allowed = data['allowed']
            pending = (data.get('observation') or {}).get('pending_ids') or []
            if self.ask_first and not self.asked and 'ASK' in allowed:
                self.asked = True
                result = {'action': 'ASK', 'affected_field': 'purpose',
                          'question': 'Is this reading list for a survey or for experiments?',
                          'reason': 'the purpose changes the query and the quota'}
            elif 'ASSESS' in allowed and pending:
                result = {'action': 'ASSESS', 'paper_ids': pending[:10],
                          'reason': 'judge the new candidates'}
            elif 'SEARCH' in allowed:
                result = {'action': 'SEARCH', 'query': 'retrieval augmented generation',
                          'reason': 'no candidates yet'}
            else:
                result = {'action': 'FINISH', 'reason': 'requirements met or budget spent'}
            validate(result)
            return result
        return super().generate(stage, system, data, schema, validate)
