"""Output helpers shared by CLI and evaluation. Never save endpoint credentials."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


def bibtex(result: dict) -> str:
    """Export only retrieved metadata; missing bibliographic fields stay absent."""
    escapes = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}', '%': r'\%',
               '&': r'\&', '_': r'\_', '#': r'\#', '$': r'\$', '^': r'\^{}', '~': r'\~{}'}

    def tex(value):
        return ''.join(escapes.get(ch, ch) for ch in ' '.join(str(value).split()))

    entries, seen = [], set()
    for paper in result.get('papers') or []:
        title = str(paper.get('title') or '').strip()
        identity = str(paper.get('id') or paper.get('url') or title)
        if not title or identity in seen:
            continue
        seen.add(identity)
        fields = [('title', '{' + tex(title) + '}')]
        authors = [str(author.get('name') or '') if isinstance(author, dict) else str(author)
                   for author in paper.get('authors') or []]
        authors = [author.strip() for author in authors if author.strip()]
        if authors:
            fields.append(('author', ' and '.join(tex(author) for author in authors)))
        year = paper.get('year')
        if year is not None and re.fullmatch(r'(?:19|20)\d{2}', str(year)):
            fields.append(('year', str(year)))
        url = str(paper.get('url') or '')
        if url.startswith(('https://', 'http://')):
            fields.append(('url', tex(url)))
        doi = str(paper.get('doi') or '')
        doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi, flags=re.I)
        if re.fullmatch(r'10\.\d{4,9}/\S+', doi):
            fields.append(('doi', tex(doi)))
        arxiv = str(paper.get('arxiv_id') or '')
        if not arxiv and identity.startswith('arxiv:'):
            arxiv = identity[6:]
        if arxiv:
            fields.extend([('eprint', tex(arxiv)), ('archivePrefix', 'arXiv')])
        venue = str(paper.get('venue') or '')
        if venue and 'arxiv' not in venue.casefold():
            fields.append(('howpublished', tex(venue)))
        key = 'sg_' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]
        entries.append('@misc{' + key + ',\n' + ',\n'.join(
            '  ' + name + ' = {' + value + '}' for name, value in fields) + '\n}')
    note = '% ScholarGuide: retrieved metadata; abstract-based recommendations.\n'
    return note + '\n\n'.join(entries) + ('\n' if entries else '')


def markdown(result: dict) -> str:
    def safe(text):
        # Model/source text remains plain text in Markdown viewers as in DOM.
        value = ' '.join(str(text).split()).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return re.sub(r'([\\`*_{}\[\]#!|>])', r'\\\1', value)
    def safe_url(value):
        value = str(value or '')
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                    or parsed.username or parsed.password
                    or any(char.isspace() or ord(char) < 32 for char in value)):
                return ''
        except ValueError:
            return ''
        return value

    req = result.get('request') or {}
    lines = ['# ScholarGuide', '', f"Mode: **{result['mode']}** · Method: **{result['method']}**",
             f"Status: **{result['status']}** · Stop: {safe(result['stop_reason'])}", '',
             f"Topic: {safe(req.get('topic', result.get('user_input', '')))}",
             f"Reading goal: {safe(req.get('reading_goal') or 'general')} · Requested: {req.get('target_count', 5)} · Returned: {len(result.get('papers') or [])}",
             f"Category: {req.get('category', 'unknown')} · Minimum: {req.get('minimum_category_count', 0)}",
             f"Venue: {safe(req.get('venue') or 'any')} · Years: {req.get('year_from') or 'any'}-{req.get('year_to') or 'any'}", '',
             '> Recommendations use titles and abstracts. Model judgements are not human evaluation scores.',
             '> Evidence verified means the quoted text occurs in the abstract; it does not prove the interpretation.', '']
    if req.get('purpose'):
        lines += ['Purpose: ' + safe(req['purpose']), '']
    if any(req.get(key) for key in ('accepted_only', 'presentation_types')) or req.get('publication_type', 'any') != 'any':
        lines += [f"Publication: {safe(req.get('publication_type', 'any'))} · Acceptance required: {bool(req.get('accepted_only'))} · Presentation: {safe(', '.join(req.get('presentation_types') or []) or 'any')}", '']
    if result.get('stop_reason') == 'user_cancelled':
        lines += ['> Stopped at your request. Completed, verified recommendations are preserved; an in-flight request may still have incurred usage.', '']
    if result['mode'] == 'demo':
        lines += ['> **OFFLINE FIXTURE DEMO:** recorded paper metadata and scripted model decisions; not live inference or benchmark evidence.', '']
    for i, p in enumerate(result.get('papers', []), 1):
        url = safe_url(p.get('url'))
        lines.extend([f"## {i}. {safe(p['title'])}", '', url, '',
                      ('Authors: ' + safe(', '.join(str(a) for a in p.get('authors') or []))) if p.get('authors') else 'Authors: unavailable',
                      f"Year: {p.get('year') or 'unknown'} · ID: {safe(p['id'])}",
                      f"Category match (model): {p.get('category_status', 'unknown')}", '',
                      safe(p.get('reason', '')), '', '> ' + safe(p.get('evidence_quote', '')).replace('\n', '\n> '), ''])
        publication = p.get('publication') or {}
        if publication:
            lines += [f"Publication metadata: {safe(p.get('venue') or 'unknown')} · {safe(publication.get('status') or 'unknown')} · {safe(publication.get('presentation') or 'unknown')}", '']
            for evidence in publication.get('evidence') or []:
                evidence_url = safe_url(evidence.get('url'))
                if evidence_url:
                    lines += ['Publication evidence: ' + safe(evidence_url), '']
    landscape = result.get('research_coverage')
    if isinstance(landscape, dict):
        lines += ['## 研究路线覆盖 / Research coverage', '',
                  'Status: ' + safe(landscape.get('status') or 'unavailable') +
                  ' · Scope: ' + safe(landscape.get('scope') or 'unknown'), '',
                  '> 覆盖依据：模型按摘要片段分类；引文匹配不证明语义成立，也不代表穷尽研究方向或达到质量认证。', '']
        covered = set(landscape.get('covered') or [])
        missing = set(landscape.get('missing') or [])
        for facet in landscape.get('facets') or []:
            identity = facet.get('id') or ''
            state = 'covered' if identity in covered else 'missing' if identity in missing else 'unknown'
            lines += ['- ' + safe(facet.get('label') or identity) + ' [' + state + '] · query: ' +
                      safe(facet.get('query') or '')]
        lines += ['', 'Covered: ' + safe(', '.join(landscape.get('covered') or []) or 'none'),
                  'Missing: ' + safe(', '.join(landscape.get('missing') or []) or 'none'), '']
        if landscape.get('reason'):
            lines += ['Coverage note: ' + safe(landscape['reason']), '']
    brief = result.get('research_brief')
    if isinstance(brief, dict):
        lines += ['## 研究综合 / Research brief', '',
                  'Status: ' + safe(brief.get('status', 'unknown')), '',
                  '> 证据范围：论文摘要。引文匹配仅证明文字存在，不证明解释成立、实验结论或研究新颖性。', '']
        def claim_lines(claim):
            if not isinstance(claim, dict) or not claim.get('text'):
                return ['摘要未明确说明。', '']
            output = [safe(claim['text']), '']
            for evidence in claim.get('evidence') or []:
                output += ['> [' + safe(evidence.get('paper_id', '')) + '] ' + safe(evidence.get('quote', '')).replace('\n', '\n> '), '']
            return output
        for note in brief.get('paper_notes') or []:
            lines += ['### ' + safe(note.get('paper_id', '')), '']
            for field, label in (('contribution', '贡献 · 摘要整理'), ('method', '方法 · 摘要整理'),
                                 ('author_limitation', '作者明确报告的局限'), ('model_inference', '模型推论')):
                lines += ['**' + label + '**', ''] + claim_lines(note.get(field))
        for theme in brief.get('themes') or []:
            lines += ['### 技术脉络 · 模型归纳：' + safe(theme.get('title', '')), ''] + claim_lines(theme)
        for question in brief.get('open_questions') or []:
            lines += ['开放问题 · 模型推论：' + safe(question.get('question', '')), '']
        lines += ['### 待验证的研究假设', '', '> 以下是拟议实验，不是论文已经完成的实验或已证实的新方法。', '']
        for index, item in enumerate(brief.get('research_hypotheses') or [], 1):
            lines += [f"#### {index}. {safe(item.get('hypothesis', ''))}", '']
            for field, label in (('rationale', '动机'), ('experiment', '建议实验'), ('baseline', '对照'), ('measure', '衡量方式'), ('falsifier', '否证条件')):
                lines += [label + '：' + safe(item.get(field, '')), '']
            lines += claim_lines({'text': '动机依据', 'evidence': item.get('evidence') or []})
        lines += ['### 建议阅读顺序', '']
        lines += [f"{index}. {safe(item.get('paper_id', ''))} — {safe(item.get('reason', ''))}"
                  for index, item in enumerate(brief.get('reading_order') or [], 1)]
        if brief.get('gaps'):
            lines += ['', '证据缺口：' + safe(json.dumps(brief['gaps'], ensure_ascii=False))]
        lines += ['']
    if result.get('gaps'):
        lines += ['## Unmet requirements', '']
        for gap in result['gaps']:
            if gap['requirement'] == 'constraints':
                lines.append(f"- constraints ({gap.get('value', '')}): {gap['found']}/{gap['required']}; missing {gap['missing']}.")
            elif gap.get('requirement') == 'research_coverage':
                labels = gap.get('missing_facets') or []
                lines.append('- Research coverage: ' + safe(gap.get('value') or 'unverified')
                             + ('; missing ' + safe(', '.join(labels)) if labels else '')
                             + ('; ' + safe(gap['reason']) if gap.get('reason') else ''))
            else:
                lines.append(f"- {gap['requirement']}: {gap['found']}/{gap['required']}; missing {gap['missing']}.")
        lines.append('')
    if result.get('warnings'):
        lines += ['## Warnings', ''] + ['- ' + safe(x) for x in result['warnings']] + ['']
    lines += ['## Execution trace', '']
    for index, step in enumerate(result.get('trace', []), start=1):
        detail = {k: v for k, v in step.items() if k not in ('step', 'action')}
        lines.append(f"{step.get('seq') or step.get('step') or index}. **{step.get('action', 'progress')}**: {safe(json.dumps(detail, ensure_ascii=False))}")
    lines += ['', '## Resources', '', '```json', json.dumps(result.get('metrics', {}), indent=2), '```', '']
    return '\n'.join(lines)


def save_run(result: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    def replace_text(path, text):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                             prefix='.' + path.name + '-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    # result.json is the completion marker read by Jobs/Activity. Publish it
    # last, after a complete report exists; readers never observe half a JSON.
    replace_text(directory / 'report.md', markdown(result))
    replace_text(directory / 'result.json', json.dumps(result, ensure_ascii=False, indent=2) + '\n')
