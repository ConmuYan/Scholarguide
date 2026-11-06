"""Phase 3（G 维度）v2：官方 GitHub 仓库识别与 Stars。

v1 命中率低的主要原因
--------------------
1. Bug：_extract_available_url 的 URL 字符类排除了 '.'，
   "https://github.com/x/y" 会被截成 "https://github"，第 2 级几乎不可能命中。
2. 只看 OpenAlex 摘要。OpenAlex 摘要经常缺失，或把 URL 拆坏（"github. com/x"），
   而大多数论文的代码链接写在 arXiv comment、首页脚注或项目主页里。
3. 兜底搜索只用「标题缩写 + 两个词」并按 stars 排序，匹配规则又要求仓库名约等于缩写；
   没有缩写的论文直接放弃，而 "Toolformer: ..." 这类冒号前的单词名也提取不到。
4. 未认证调用限额 60 次/小时，而且连非搜索调用也 sleep 6.1s。

v2 证据层级（按可靠性降序，命中 official 即停）
--------------------------------------------
L1 paper_text   OpenAlex 摘要 + arXiv 摘要 + arXiv comment 中的 GitHub 链接（论文本身）
L2 hf_papers    Hugging Face Papers API 的 githubRepo（只有作者/提交者/管理员能设置）
L3 project_page 论文或 HF 给出的项目主页 HTML 中的 GitHub 链接
L4 fulltext     arXiv HTML 全文 / PDF 前 3 页（--fulltext 开启），只接受带「代码」语境的链接
L5 search       GitHub 搜索："<arXiv ID>" in:readme、"<标题>" in:readme、缩写 in:name

判定规则（对应协议 §4.2：官方仓库需能从论文、项目主页或作者可验证链接确认）
- L1/L4/L3（论文或项目主页中的链接）：仓库存在即 official。
- L2/L5 以及「由 xxx.github.io 推断出的仓库」：必须先做 README 反向链接校验
  （README、description 或 homepage 中出现 arXiv ID 或论文标题）。
    · 反向链接 + 未被排除 + （仓库属于某位作者 或 README 自称 official）→ official
      （L2 的 githubRepo 本身来自作者侧，有反向链接即可）
    · 只有反向链接 → 候选，保持 unverified，进入人工复核队列
- 排除：fork、awesome / paper-list 类仓库、README 含 unofficial / reproduction 等字样。
- 不自动产生 none_available：「找不到」不等于「没有官方仓库」，需要人工确认。
- 已由人工设定的 none_available / not_applicable / official 一律不覆盖。

环境变量
  GITHUB_TOKEN     强烈建议设置（core 限额 5000 次/小时，search 30 次/分钟）
  OPENALEX_MAILTO  可选，进入 OpenAlex polite pool

运行
  python3 frontierlitbench/scripts/phase3_github.py [--limit N] [--scope all]
          [--fulltext] [--force] [--no-cache]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import common_lib as lib

REVIEWER = 'agent-draft-v2'
UA = 'FrontierLitBench-TrackC/2.0 (academic benchmark construction)'
GH_API = 'https://api.github.com'
ARXIV_API = 'https://export.arxiv.org/api/query'
HF_API = 'https://huggingface.co/api/papers/'
OPENALEX_API = 'https://api.openalex.org/works/'

BASE_DIR = lib.CORPUS_DIR.parent
CACHE_DIR = BASE_DIR / 'cache' / 'phase3_github'
FEATURES = lib.CORPUS_DIR / 'features_agents.jsonl'
REVIEW_CSV = BASE_DIR / 'review' / 'github_review_queue_agents.csv'
COVERAGE_CSV = BASE_DIR / 'reports' / 'github_coverage_agents.csv'

# ---------------------------------------------------------------- 词表

_GENERIC = {'gpt', 'gpt4', 'gpt4v', 'gpt4o', 'gpt35', 'chatgpt', 'swe', 'llm',
            'llms', 'api', 'apis', 'rag', 'agent', 'agents', 'ai', 'nlp', 'rl',
            'rlhf', 'cot', 'mllm', 'mllms', 'vlm', 'vlms', 'lm', 'lms', 'bench',
            'benchmark', 'sota', 'qa', 'llama', 'llama2', 'llama3', 'mcts',
            'multiagent', 'treeofthoughts', 'chainofthought', 'web', 'gui', 'os'}

_CODE_CTX = ('code', 'available', 'release', 'open-source', 'open source',
             'opensource', 'implementation', 'repository', 'repo', 'github',
             'project page', 'publicly', 'data and', 'resources')
_OWN_CTX = ('our code', 'our implementation', 'we release', 'we open-source',
            'we open source', 'we make', 'is available at', 'are available at',
            'will be released', 'released at', 'can be found at',
            'is publicly available', 'are publicly available', 'code:', 'code at')
_THIRD_CTX = ('based on', 'built on', 'built upon', 'build on', 'build upon',
              'building on', 'we use', 'we adopt', 'adapted from', 'borrowed from',
              'baseline', 'prior work', 'e.g.', 'such as', 'following',
              'implemented by', 'provided by', 'we compare', 'existing')
# 链接之后出现即视为第三方（"... github.com/x/y as a baseline"）
_THIRD_AFTER = ('as a baseline', 'as baseline', 'as the baseline', 'as our base',
                'as the backbone', 'framework to')

_EXCLUDE_TEXT = ('unofficial', 'reproduction', 'reproduce the results of',
                 'reimplementation', 're-implementation', 'reimplement',
                 'third-party', 'third party implementation', 'fork of',
                 'paper list', 'reading list', 'curated list')
_EXCLUDE_NAME = ('awesome', 'paper-list', 'paperlist', 'reading-list',
                 'readinglist', 'papers-list')

_GH_RESERVED = {'features', 'about', 'topics', 'orgs', 'sponsors', 'marketplace',
                'collections', 'settings', 'login', 'apps', 'search', 'pricing',
                'site', 'contact', 'explore', 'trending', 'join', 'blog',
                'enterprise', 'security', 'customer-stories', 'readme'}

# 项目主页模板仓库（页脚的 "website adapted from ..."），不是论文代码
_TEMPLATE_REPOS = {'nerfies/nerfies.github.io',
                   'eliahuhorwitz/academic-project-page-template',
                   'google/nerfies'}

# 单篇论文常放在这些大仓库的子目录里，Stars 主要属于平台 → repo_attribution_concern
_KNOWN_MONOREPOS = {'google-research/google-research', 'microsoft/unilm',
                    'microsoft/lmops', 'facebookresearch/fairseq',
                    'huggingface/transformers', 'langchain-ai/langchain',
                    'run-llama/llama_index', 'openai/evals',
                    'salesforce/lavis', 'allenai/allennlp', 'thudm/chatglm-6b',
                    'microsoft/autogen', 'microsoft/promptflow'}

# ---------------------------------------------------------------- 正则

_GH_OWNER = r'[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}'
_GH_URL_RE = re.compile(
    r'(?:https?://)?(?:www\.)?github\.com/(' + _GH_OWNER + r')/'
    r'([A-Za-z0-9_.\-]+)((?:/[^\s<>"\'(){}\[\]]*)?)', re.I)
_URL_RE = re.compile(r'https?://[^\s<>"\'(){}\[\]]+', re.I)
_GHIO_RE = re.compile(r'^https?://([A-Za-z0-9-]+)\.github\.io(/[^?#]*)?', re.I)
_ARXIV_ID_RE = re.compile(
    r'(?:arxiv\.org/(?:abs|pdf|html)/|arxiv[:./ ])\s*(\d{4}\.\d{4,5})(?:v\d+)?',
    re.I)
_ARXIV_BARE = re.compile(r'^(\d{4}\.\d{4,5})(?:v\d+)?$')
_ATOM = {'a': 'http://www.w3.org/2005/Atom',
         'arxiv': 'http://arxiv.org/schemas/atom'}


# ---------------------------------------------------------------- HTTP

class Http:
    """带磁盘缓存、分桶节流、GitHub 限额感知与重试的 GET 客户端。"""

    def __init__(self, cache_dir: Path, gh_token: str | None, use_cache=True):
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.gh_token = gh_token
        self.use_cache = use_cache
        self.calls = Counter()
        self._last: dict[str, float] = {}
        self.intervals = {
            'gh_search': 2.2 if gh_token else 6.5,
            'gh_core': 0.1 if gh_token else 1.0,
            'arxiv': 3.2, 'hf': 0.4, 'web': 1.0, 'openalex': 0.12,
        }

    def _throttle(self, bucket):
        wait = self._last.get(bucket, 0.0) + self.intervals[bucket] - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[bucket] = time.monotonic()

    def _cache_path(self, url, accept):
        key = hashlib.sha1((url + '|' + (accept or '')).encode()).hexdigest()
        return self.cache_dir / (key[:2]) / (key + '.json')

    def _store(self, path, status, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'status': status, 'body': body},
                                   ensure_ascii=False), encoding='utf-8')

    @staticmethod
    def _gh_backoff(err):
        h = err.headers
        try:
            body = err.read().decode('utf-8', 'replace').lower()
        except Exception:
            body = ''
        ra = h.get('Retry-After')
        if ra and ra.isdigit():
            return int(ra) + 1
        if h.get('X-RateLimit-Remaining') == '0':
            reset = h.get('X-RateLimit-Reset')
            if reset and reset.isdigit():
                return min(max(int(reset) - time.time(), 0) + 2, 3700)
        if 'rate limit' in body:
            return 65
        return None  # 真正的 403（无权限等），不重试

    def get(self, url, bucket, *, accept=None, as_json=True, max_bytes=2_500_000):
        """返回 (status, body)。404/410 也缓存（负缓存）；网络失败 status=-1。"""
        path = self._cache_path(url, accept)
        if self.use_cache and path.exists():
            try:
                cached = json.loads(path.read_text(encoding='utf-8'))
                return cached['status'], cached['body']
            except (ValueError, KeyError):
                pass
        headers = {'User-Agent': UA}
        if accept:
            headers['Accept'] = accept
        if bucket.startswith('gh') and self.gh_token:
            headers['Authorization'] = 'Bearer ' + self.gh_token
            headers['X-GitHub-Api-Version'] = '2022-11-28'
        for attempt in range(4):
            self._throttle(bucket)
            self.calls[bucket] += 1
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status = resp.status
                    text = resp.read(max_bytes).decode('utf-8', errors='replace')
            except urllib.error.HTTPError as err:
                status = err.code
                if status in (403, 429) and bucket.startswith('gh'):
                    wait = self._gh_backoff(err)
                    if wait is not None and attempt < 3:
                        print('    GitHub 限额，等待 %ds ...' % wait, file=sys.stderr)
                        time.sleep(wait)
                        continue
                if status in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(5 * 2 ** attempt)
                    continue
                if status in (404, 410):
                    self._store(path, status, None)
                return status, None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt < 3:
                    time.sleep(3 * 2 ** attempt)
                    continue
                return -1, None
            body = text
            if as_json:
                try:
                    body = json.loads(text)
                except ValueError:
                    body = None
            self._store(path, status, body)
            return status, body
        return -1, None

    def get_bytes(self, url, bucket, max_bytes=25_000_000):
        key = hashlib.sha1(url.encode()).hexdigest()
        path = self.cache_dir / 'bin' / (key + '.bin')
        if self.use_cache and path.exists():
            return path.read_bytes()
        for attempt in range(3):
            self._throttle(bucket)
            self.calls[bucket] += 1
            try:
                req = urllib.request.Request(url, headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read(max_bytes)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return data
            except Exception:
                time.sleep(5 * 2 ** attempt)
        return None


# ---------------------------------------------------------------- 文本工具

def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def norm_words(s):
    s = unicodedata.normalize('NFKC', s or '').lower()
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', s)).strip()


_TITLE_STOP = {'the', 'a', 'an', 'and', 'or', 'of', 'for', 'in', 'on', 'to',
               'with', 'via', 'from', 'by', 'is', 'are', 'can', 'as', 'at'}


def title_tokens(title):
    return {w for w in norm_words(title).split() if w not in _TITLE_STOP}


def repair_urls(text):
    """修复 PDF/OpenAlex 抽取造成的断裂 URL。"""
    t = unicodedata.normalize('NFKC', text or '').replace('\u200b', '')
    t = re.sub(r'(https?:)\s*/\s*/\s*', r'\1//', t, flags=re.I)
    t = re.sub(r'github\s*\.\s*(com|io)\b', r'github.\1', t, flags=re.I)
    t = re.sub(r'(github\.com)\s*/\s*', r'\1/', t, flags=re.I)
    t = re.sub(r'(github\.com/[A-Za-z0-9-]+)(?:\s+/\s*|/\s+)(?=[A-Za-z0-9_.-])',
               r'\1/', t, flags=re.I)
    # 断行连字符："github.com/owner/repo-\nname"
    t = re.sub(r'(github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]*-)\s*\n\s*([A-Za-z0-9_.-]+)',
               r'\1\2', t, flags=re.I)
    return t


def clean_repo(repo):
    repo = (repo or '').rstrip('.,;:)\'"-')
    repo = re.sub(r'\.git$', '', repo, flags=re.I)
    return '' if repo in ('', '.', '..') else repo


def shortnames(title):
    """论文名候选：冒号前短语、CamelCase、全大写、带数字、带连字符的专名。"""
    t = unicodedata.normalize('NFKC', title or '')
    cands = []
    m = re.match(r'\s*([^:：]{2,40})[:：]', t)
    if m and len(m.group(1).split()) <= 3:
        cands.append(m.group(1).strip())
    cands += re.findall(r'\b[A-Za-z]*[a-z][A-Z][A-Za-z0-9]*\b', t)
    # 只收「全大写前缀」的连字符名（SWE-agent、WebArena-Lite），不收 Self-Improve 这类普通词组
    cands += re.findall(r'\b[A-Z]{2,}[A-Za-z0-9]*-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b', t)
    cands += re.findall(r'\b[A-Z]{2,}[A-Za-z0-9]*\b', t)
    cands += re.findall(r'\b[A-Za-z]+[0-9]+[A-Za-z0-9]*\b', t)
    out, seen = [], set()
    for c in cands:
        n = norm(c)
        if len(n) >= 3 and n not in _GENERIC and n not in seen:
            seen.add(n)
            out.append(c)
    return out


def html_to_text(raw):
    """去标签但保留 <a> 的锚文本和 href，使链接带有语境。"""
    t = re.sub(r'<(script|style|noscript)\b.*?</\1\s*>', ' ', raw or '', flags=re.I | re.S)

    def _a(m):
        inner = re.sub(r'<[^>]+>', ' ', m.group(2))
        return ' %s %s ' % (inner, html.unescape(m.group(1)))

    t = re.sub(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a\s*>',
               _a, t, flags=re.I | re.S)
    t = re.sub(r'<[^>]+>', ' ', t)
    return re.sub(r'[ \t\r\f\v]+', ' ', html.unescape(t))


# ---------------------------------------------------------------- 链接抽取与打分

def find_github_links(text, source):
    t = repair_urls(text)
    out = []
    for m in _GH_URL_RE.finditer(t):
        owner, repo = m.group(1), clean_repo(m.group(2))
        if not repo or owner.lower() in _GH_RESERVED:
            continue
        full = '%s/%s' % (owner, repo)
        if full.lower() in _TEMPLATE_REPOS or repo.lower().endswith('.github.io'):
            continue
        out.append({'full': full, 'sub': (m.group(3) or '').rstrip('.,;:)'),
                    'source': source, 'pos': m.start(), 'len': max(len(t), 1),
                    'ctx': t[max(0, m.start() - 160):m.start()].lower(),
                    'after': t[m.end():m.end() + 60].lower()})
    return out


def find_project_pages(text):
    t = repair_urls(text)
    pages = []
    for m in _URL_RE.finditer(t):
        url = m.group(0).rstrip('.,;:)')
        host = urllib.parse.urlparse(url).netloc.lower()
        ctx = t[max(0, m.start() - 80):m.start()].lower()
        if host.endswith('github.com') or any(
                h in host for h in ('arxiv.org', 'doi.org', 'openreview.net',
                                    'aclanthology.org', 'huggingface.co')):
            continue
        if host.endswith('github.io') or any(
                k in ctx for k in ('project page', 'website', 'homepage', 'demo',
                                   'project site', 'available at')):
            if url not in pages:
                pages.append(url)
    return pages


def score_link(link, names):
    s = 0
    near = link['ctx'][-110:]
    if any(k in near for k in _CODE_CTX):
        s += 2
    if any(k in near for k in _OWN_CTX):
        s += 2
    sentence = re.split(r'[.;!?]\s', near)[-1]  # 只看链接所在的这一句
    if any(k in sentence[-80:] for k in _THIRD_CTX) or any(
            k in link.get('after', '') for k in _THIRD_AFTER):
        s -= 4
    repo_n = norm(link['full'].split('/')[1])
    for n in names:
        if n and (repo_n == n or (len(n) >= 4 and (n in repo_n or repo_n in n))):
            s += 3
            break
    if link['source'] in ('arxiv_comment', 'openalex_abstract', 'arxiv_abstract'):
        s += 1
    if link['pos'] / link['len'] < 0.25:
        s += 1
    return s


def rank_links(links, names):
    best = {}
    for ln in links:
        ln['score'] = score_link(ln, names)
        k = ln['full'].lower()
        if k not in best or ln['score'] > best[k]['score']:
            best[k] = ln
    return sorted(best.values(), key=lambda x: (-x['score'], x['pos']))


# ---------------------------------------------------------------- arXiv ID 解析

def author_names(rec):
    out = []
    for a in rec.get('authors') or []:
        n = (a.get('name') or a.get('display_name')) if isinstance(a, dict) else str(a)
        if n:
            out.append(n)
    return out


def arxiv_from_record(rec):
    ids = rec.get('identifiers') or {}
    cands = [ids.get('arxiv_id'), rec.get('arxiv_id'), rec.get('paper_id'),
             ids.get('doi'), rec.get('doi'), ids.get('url'), rec.get('url')]
    for key in ('arxiv_ids', 'dois', 'source_links', 'links', 'urls'):
        val = rec.get(key)
        if isinstance(val, list):
            cands += [str(v) for v in val]
    for c in cands:
        c = str(c or '').strip()
        if c.lower().startswith('arxiv:'):
            c = c[6:]
        m = _ARXIV_BARE.match(c) or _ARXIV_ID_RE.search(c)
        if m:
            return m.group(1)
    return None


def arxiv_from_openalex(http, rec):
    oid = str(rec.get('openalex_id') or '').rstrip('/').split('/')[-1]
    if not oid.startswith('W'):
        return None
    url = OPENALEX_API + oid + '?select=ids,locations'
    mail = os.environ.get('OPENALEX_MAILTO')
    if mail:
        url += '&mailto=' + urllib.parse.quote(mail)
    st, body = http.get(url, 'openalex')
    if st != 200 or not isinstance(body, dict):
        return None
    texts = [str(v) for v in (body.get('ids') or {}).values()]
    for loc in body.get('locations') or []:
        texts += [str(loc.get('landing_page_url') or ''), str(loc.get('pdf_url') or '')]
    for t in texts:
        m = _ARXIV_ID_RE.search(t)
        if m:
            return m.group(1)
    return None


def parse_arxiv_feed(xml_text):
    out = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for e in root.findall('a:entry', _ATOM):
        m = re.search(r'(\d{4}\.\d{4,5})', e.findtext('a:id', '', _ATOM))
        if not m:
            continue
        out[m.group(1)] = {
            'title': re.sub(r'\s+', ' ', e.findtext('a:title', '', _ATOM)).strip(),
            'summary': e.findtext('a:summary', '', _ATOM) or '',
            'comment': e.findtext('arxiv:comment', '', _ATOM) or '',
            'authors': [a.findtext('a:name', '', _ATOM)
                        for a in e.findall('a:author', _ATOM)],
        }
    return out


def title_similar(a, b):
    na, nb = norm_words(a), norm_words(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.93


def arxiv_by_title(http, title, authors):
    q = re.sub(r'[^A-Za-z0-9 ]+', ' ', title or '')
    q = re.sub(r'\s+', ' ', q).strip()
    if len(q) < 10:
        return None, None
    url = '%s?search_query=%s&max_results=5' % (
        ARXIV_API, urllib.parse.quote('ti:"%s"' % q))
    st, body = http.get(url, 'arxiv', as_json=False)
    if st != 200 or not body:
        return None, None
    first_sur = norm_words(authors[0]).split()[-1] if authors else ''
    for aid, meta in parse_arxiv_feed(body).items():
        if not title_similar(title, meta['title']):
            continue
        if first_sur and not any(first_sur in norm_words(a) for a in meta['authors']):
            continue
        return aid, meta
    return None, None


def fetch_arxiv_meta(http, aids, meta_cache):
    todo = [a for a in aids if a and a not in meta_cache]
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        url = '%s?id_list=%s&max_results=%d' % (ARXIV_API, ','.join(batch), len(batch))
        st, body = http.get(url, 'arxiv', as_json=False)
        if st == 200 and body:
            meta_cache.update(parse_arxiv_feed(body))


# ---------------------------------------------------------------- 仓库校验

def repo_info(http, full):
    st, info = http.get('%s/repos/%s' % (GH_API, full), 'gh_core')
    if st == 200 and isinstance(info, dict) and info.get('full_name'):
        return info
    return None


def backlink_kind(blob, title, aid):
    if aid and re.search(re.escape(aid) + r'(?!\d)', blob):
        return 'arxiv_id'
    nt, nb = norm_words(title), norm_words(blob)
    if len(nt) >= 15 and (' %s ' % nt) in (' %s ' % nb):
        return 'title_exact'
    tt = title_tokens(title)
    if len(tt) >= 4:
        for line in blob.splitlines():
            lt = set(norm_words(line).split())
            if len(lt) <= 3 * len(tt) + 12 and len(tt & lt) / len(tt) >= 0.85:
                return 'title_fuzzy'
    return None


def owner_matches(http, owner, authors):
    login = owner.get('login') or ''
    names = [login]
    if owner.get('type') == 'User':
        st, u = http.get('%s/users/%s' % (GH_API, login), 'gh_core')
        if st == 200 and isinstance(u, dict) and u.get('name'):
            names.append(u['name'])
    for a in authors:
        parts = norm_words(a).split()
        if len(parts) < 2:
            continue
        given, sur = parts[0], parts[-1]
        full = ' '.join(parts)
        for n in names:
            nw, comp = norm_words(n), norm(n)
            if nw == full or nw == '%s %s' % (sur, given):
                return True
            if given + sur in comp or sur + given in comp:
                return True
            if len(sur) >= 4 and (comp.startswith(given[0] + sur) or comp == sur
                                  or comp.startswith(sur + given[0])):
                return True
    return False


def exclusion_reason(info, readme):
    name = (info.get('name') or '').lower()
    if info.get('fork'):
        return 'fork'
    if any(k in name for k in _EXCLUDE_NAME):
        return 'list_repo_name'
    head = ((info.get('description') or '') + ' ' + readme[:4000]).lower()
    for k in _EXCLUDE_TEXT:
        if k in head:
            return 'text:' + k
    if len(set(re.findall(r'\d{4}\.\d{4,5}', readme))) > 25:
        return 'many_arxiv_ids'
    return None


def inspect_repo(http, full, title, aid, authors, info=None):
    info = info or repo_info(http, full)
    if not info:
        return None
    full = info['full_name']
    st, readme = http.get('%s/repos/%s/readme' % (GH_API, full), 'gh_core',
                          accept='application/vnd.github.raw', as_json=False)
    readme = (readme or '')[:300_000] if st == 200 else ''
    blob = '\n'.join([info.get('description') or '', info.get('homepage') or '', readme])
    phrase = re.search(r'(?<!un)official\s+(?:code|implementation|repo|repository|'
                       r'pytorch|release|codebase)', blob[:6000], re.I)
    return {
        'full': full, 'url': info.get('html_url'), 'info': info,
        'stars': info.get('stargazers_count'),
        'backlink': backlink_kind(blob, title, aid),
        'excluded': exclusion_reason(info, readme),
        'owner_match': owner_matches(http, info.get('owner') or {}, authors),
        'official_phrase': bool(phrase),
    }


def attribution_concern(full, sub):
    sub_parts = [p for p in (sub or '').split('/') if p]
    if len(sub_parts) >= 3 and sub_parts[0] in ('tree', 'blob'):
        return True  # 指向子目录：Stars 属于整个仓库
    return full.lower() in _KNOWN_MONOREPOS


# ---------------------------------------------------------------- 单篇解析

def official(full, sub, info, method, evidence, evidence_url, alternates=()):
    return {
        'repo_status': 'official',
        'github_url': info.get('html_url') or 'https://github.com/' + full,
        'github_stars': info.get('stargazers_count'),
        'stars_as_of': lib.utc_now_iso(),
        'repo_attribution_concern': attribution_concern(info.get('full_name', full), sub),
        'github_method': method,
        'github_evidence': evidence,
        'github_evidence_url': evidence_url,
        'github_alternates': list(alternates),
        'github_candidates': [],
        'github_unverified_reason': None,
        'github_reviewer': REVIEWER,
    }


def unverified(reason, candidates=(), notes=()):
    return {
        'repo_status': 'unverified',
        'github_url': None, 'github_stars': None, 'stars_as_of': None,
        'repo_attribution_concern': None,
        'github_method': None, 'github_evidence': None,
        'github_evidence_url': None, 'github_alternates': [],
        'github_candidates': list(candidates),
        'github_unverified_reason': reason + ('; ' + '; '.join(notes) if notes else ''),
        'github_reviewer': REVIEWER,
    }


def cand_entry(insp, method):
    return {'url': insp['url'], 'stars': insp['stars'], 'method': method,
            'backlink': insp['backlink'], 'owner_match': insp['owner_match'],
            'official_phrase': insp['official_phrase'], 'excluded': insp['excluded']}


class Resolver:
    def __init__(self, http, arxiv_meta, fulltext, max_verify):
        self.http = http
        self.arxiv_meta = arxiv_meta
        self.fulltext = fulltext
        self.max_verify = max_verify

    def _paper_side(self, links, method, evidence_url, notes):
        """论文/项目主页里的链接：仓库存在即 official。"""
        for ln in links:
            info = repo_info(self.http, ln['full'])
            if info:
                alts = ['https://github.com/' + x['full'] for x in links if x is not ln][:5]
                return official(ln['full'], ln['sub'], info, method,
                                '%s:%s' % (ln['source'], ln['full']), evidence_url, alts)
            notes.append('dead_link:' + ln['full'])
        return None

    def resolve(self, rec, aid):
        title = rec.get('title') or ''
        names = [norm(s) for s in shortnames(title)]
        meta = self.arxiv_meta.get(aid) if aid else None
        authors = author_names(rec) or (meta['authors'] if meta else [])
        abs_url = 'https://arxiv.org/abs/' + aid if aid else None
        notes, candidates, pages, seen_verify = [], [], [], set()

        def verify(full, method, info=None):
            key = full.lower()
            if key in seen_verify:
                return None
            seen_verify.add(key)
            insp = inspect_repo(self.http, full, title, aid, authors, info)
            if insp and insp['backlink'] and not insp['excluded']:
                candidates.append(cand_entry(insp, method))
            return insp

        # L1 论文文本
        texts = [('openalex_abstract', rec.get('abstract') or '')]
        if meta:
            texts += [('arxiv_abstract', meta['summary']), ('arxiv_comment', meta['comment'])]
        links = []
        for src, txt in texts:
            links += find_github_links(txt, src)
            pages += [p for p in find_project_pages(txt) if p not in pages]
        ranked = [ln for ln in rank_links(links, names) if ln['score'] >= 1]
        res = self._paper_side(ranked, 'L1_paper_text', abs_url, notes)
        if res:
            return res

        # L2 Hugging Face Papers
        if aid:
            st, hf = self.http.get(HF_API + aid, 'hf')
            if st == 200 and isinstance(hf, dict):
                pp = hf.get('projectPage')
                if pp and pp not in pages:
                    pages.append(pp)
                m = _GH_URL_RE.search(hf.get('githubRepo') or '')
                if m and clean_repo(m.group(2)):
                    full = '%s/%s' % (m.group(1), clean_repo(m.group(2)))
                    insp = verify(full, 'L2_hf_papers')
                    if insp and not insp['excluded'] and (
                            insp['backlink'] or insp['owner_match']):
                        return official(insp['full'], m.group(3), insp['info'],
                                        'L2_hf_papers',
                                        'hf_githubRepo+%s' % (insp['backlink'] or 'owner_match'),
                                        'https://huggingface.co/papers/' + aid)

        # L3 项目主页
        for page in pages[:3]:
            st, raw = self.http.get(page, 'web', accept='text/html', as_json=False)
            if st == 200 and raw:
                plinks = [ln for ln in rank_links(
                    find_github_links(html_to_text(raw), 'project_page'), names)
                    if ln['score'] >= 0]
                res = self._paper_side(plinks, 'L3_project_page', page, notes)
                if res:
                    return res
            gm = _GHIO_RE.match(page)
            if gm:  # owner.github.io/repo/ → 推断 owner/repo，必须反向链接
                seg = [p for p in (gm.group(2) or '').split('/') if p]
                if seg:
                    insp = verify('%s/%s' % (gm.group(1), seg[0]), 'L3_ghio_guess')
                    if insp and insp['backlink'] and not insp['excluded']:
                        return official(insp['full'], '', insp['info'], 'L3_ghio_guess',
                                        'ghio_guess+' + insp['backlink'], page)

        # L4 全文
        if self.fulltext and aid:
            res = self._fulltext(aid, names, notes)
            if res:
                return res

        # L5 GitHub 搜索 + README 校验
        res = self._search(title, aid, names, verify)
        if res:
            return res

        candidates.sort(key=lambda c: (-int(c['owner_match']) - int(c['official_phrase']),
                                       -(c['stars'] or 0)))
        if candidates:
            return unverified('candidate_needs_review', candidates[:5], notes)
        if not aid:
            notes.append('no_arxiv_id')
        return unverified('no_official_repo_found', (), notes)

    def _fulltext(self, aid, names, notes):
        st, raw = self.http.get('https://arxiv.org/html/' + aid, 'arxiv',
                                accept='text/html', as_json=False)
        text, src = '', ''
        if st == 200 and raw and 'ltx_document' in raw:
            text, src = html_to_text(raw), 'arxiv_html'
        else:
            text, src = self._pdf_text(aid), 'arxiv_pdf'
        if not text:
            return None
        ranked = [ln for ln in rank_links(find_github_links(text, src), names)
                  if ln['score'] >= 4 or (ln['score'] >= 2 and ln['pos'] / ln['len'] < 0.25)]
        return self._paper_side(ranked, 'L4_fulltext',
                                'https://arxiv.org/abs/' + aid, notes)

    def _pdf_text(self, aid):
        try:
            from pypdf import PdfReader
        except ImportError:
            return ''
        data = self.http.get_bytes('https://arxiv.org/pdf/' + aid, 'arxiv')
        if not data:
            return ''
        try:
            reader = PdfReader(io.BytesIO(data))
            parts = []
            for page in reader.pages[:3]:
                parts.append(page.extract_text() or '')
                for annot in page.get('/Annots') or []:
                    obj = annot.get_object()
                    uri = (obj.get('/A') or {}).get('/URI')
                    if uri and 'github' in str(uri):
                        parts.append(' code %s ' % uri)  # 首页超链接注释
            return '\n'.join(parts)
        except Exception:
            return ''

    def _search(self, title, aid, names, verify):
        queries = []
        if aid:
            queries.append('"%s" in:readme' % aid)
        tq = re.sub(r'["\']', ' ', title)
        tq = ' '.join(tq.split()[:12])
        if len(tq) >= 15:
            queries.append('"%s" in:readme,description' % tq)
        for s in shortnames(title)[:2]:
            queries.append('%s in:name' % s)
        pool, order = {}, []
        for q in queries:
            url = '%s/search/repositories?q=%s&per_page=10' % (GH_API, urllib.parse.quote(q))
            st, body = self.http.get(url, 'gh_search')
            for item in (body or {}).get('items', []) if st == 200 else []:
                k = (item.get('full_name') or '').lower()
                if k and k not in pool and not item.get('fork'):
                    pool[k] = item
                    order.append(k)

        def prior(k):  # 仓库名与论文名吻合的优先校验
            repo_n = norm(k.split('/')[-1])
            hit = any(n and (repo_n == n or (len(n) >= 4 and n in repo_n)) for n in names)
            return (0 if hit else 1, order.index(k))

        best = None
        for k in sorted(order, key=prior)[:self.max_verify]:
            insp = verify(pool[k]['full_name'], 'L5_search', info=None)
            if not insp or not insp['backlink'] or insp['excluded']:
                continue
            if insp['owner_match'] or insp['official_phrase']:
                if best is None or (insp['stars'] or 0) > (best['stars'] or 0):
                    best = insp
        if best:
            sig = '+'.join(x for x, ok in (('owner_match', best['owner_match']),
                                           ('official_phrase', best['official_phrase'])) if ok)
            return official(best['full'], '', best['info'], 'L5_search',
                            'search+%s+%s' % (best['backlink'], sig), best['url'])
        return None


# ---------------------------------------------------------------- 主流程

def needs_processing(rec, force):
    st = rec.get('repo_status')
    reviewer = str(rec.get('github_reviewer') or '')
    human = bool(st) and st != 'unverified' and not reviewer.startswith('agent')
    if human or st in ('none_available', 'not_applicable'):
        return False  # 人工结论永不覆盖
    if force:
        return True
    return not (reviewer == REVIEWER and st == 'official')


def write_atomic(path, records):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, path)


def save(records, results):
    merged = []
    for r in records:
        if r['paper_id'] in results:
            merged.append({**r, **results[r['paper_id']]})
        elif not r.get('repo_status'):
            merged.append({**r, **unverified('github_not_processed')})
        else:
            merged.append(r)  # 保留已有结果，不再像 v1 那样覆盖成 unverified
    write_atomic(FEATURES, merged)
    return merged


def write_reports(merged, processed_ids):
    REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with REVIEW_CSV.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['paper_id', 'year', 'title', 'repo_status', 'reason_or_method',
                    'github_url', 'stars', 'attribution_concern', 'candidates'])
        for r in merged:
            if r['paper_id'] not in processed_ids:
                continue
            st = r.get('repo_status')
            weak = st == 'official' and r.get('github_method') in ('L2_hf_papers',
                                                                   'L3_ghio_guess', 'L5_search')
            if st == 'unverified' or weak or r.get('repo_attribution_concern'):
                cands = ' | '.join('%s (★%s, %s, backlink=%s, owner=%s)' % (
                    c['url'], c['stars'], c['method'], c['backlink'], c['owner_match'])
                    for c in r.get('github_candidates') or [])
                w.writerow([r['paper_id'], r.get('year'), r.get('title'), st,
                            r.get('github_method') or r.get('github_unverified_reason'),
                            r.get('github_url'), r.get('github_stars'),
                            r.get('repo_attribution_concern'), cands])
    by_year = {}
    for r in merged:
        if r['paper_id'] not in processed_ids:
            continue
        row = by_year.setdefault(r.get('year'), Counter())
        row['n'] += 1
        row[r.get('repo_status') or 'none'] += 1
        row['method:' + str(r.get('github_method'))] += 1
        if r.get('github_candidates'):
            row['has_candidates'] += 1
    keys = sorted({k for c in by_year.values() for k in c})
    with COVERAGE_CSV.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['year'] + keys)
        for y in sorted(by_year, key=str):
            w.writerow([y] + [by_year[y].get(k, 0) for k in keys])


def main() -> int:
    # 加载项目根 .env（GITHUB_TOKEN / OPENALEX_MAILTO），不覆盖已设置的环境变量
    try:
        sys.path.insert(0, str(lib.ROOT))
        from common import load_env
        load_env()
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少篇（0=全部）')
    ap.add_argument('--scope', choices=['citation_verified', 'all'],
                    default='citation_verified')
    ap.add_argument('--fulltext', action='store_true',
                    help='启用 L4（arXiv HTML，失败时用 pypdf 读 PDF 前 3 页）')
    ap.add_argument('--force', action='store_true', help='重跑 v2 已判定 official 的论文')
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--max-verify', type=int, default=6,
                    help='每篇最多对多少个搜索结果做 README 校验')
    args = ap.parse_args()

    lib.ensure_dirs()
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not token:
        print('警告：未设置 GITHUB_TOKEN，限额为 60 次/小时，会频繁等待重置。',
              file=sys.stderr)
    http = Http(CACHE_DIR, token, use_cache=not args.no_cache)

    records = lib.read_jsonl(FEATURES)
    target = [r for r in records
              if (args.scope == 'all' or r.get('citation_status') == 'verified')
              and needs_processing(r, args.force)]
    target.sort(key=lambda r: -(r.get('citation_count') or 0))
    if args.limit > 0:
        target = target[:args.limit]
    print('待处理 %d 篇（共 %d 篇）' % (len(target), len(records)))

    # 预处理：解析 arXiv ID，并批量取 arXiv 元数据（摘要 + comment）
    meta_path = CACHE_DIR / 'arxiv_meta.json'
    try:
        arxiv_meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        arxiv_meta = {}
    aid_of = {}
    for r in target:
        aid = arxiv_from_record(r) or arxiv_from_openalex(http, r)
        if not aid:
            aid, meta = arxiv_by_title(http, r.get('title'), author_names(r))
            if aid and meta:
                arxiv_meta[aid] = meta
        aid_of[r['paper_id']] = aid
    fetch_arxiv_meta(http, list(aid_of.values()), arxiv_meta)
    meta_path.write_text(json.dumps(arxiv_meta, ensure_ascii=False), encoding='utf-8')
    print('arXiv ID 解析：%d/%d' % (sum(1 for a in aid_of.values() if a), len(target)))

    resolver = Resolver(http, arxiv_meta, args.fulltext, args.max_verify)
    results = {}
    for i, r in enumerate(target, 1):
        aid = aid_of[r['paper_id']]
        try:
            res = resolver.resolve(r, aid)
        except Exception as exc:  # 单篇失败不影响整体
            lib.log_review('github_resolve_error', paper_id=r['paper_id'], error=repr(exc))
            res = unverified('resolver_error')
        res['arxiv_id_resolved'] = aid
        results[r['paper_id']] = res
        tag = res['github_method'] or res['github_unverified_reason']
        print('  [%d/%d] %-48s %s %s' % (i, len(target), (r.get('title') or '')[:48],
                                         res['repo_status'], tag))
        if i % 20 == 0:
            save(records, results)

    merged = save(records, results)
    write_reports(merged, set(results))

    stats = Counter(v['repo_status'] for v in results.values())
    methods = Counter(v['github_method'] for v in results.values() if v['github_method'])
    n_cand = sum(1 for v in results.values() if v['github_candidates'])
    lib.log_review('phase3_github_v2', processed=len(results), official=stats['official'],
                   unverified=stats['unverified'], with_candidates=n_cand,
                   methods=dict(methods), http_calls=dict(http.calls))
    print('\nPhase 3（G GitHub）v2 完成')
    print('处理 %d | official %d (%.1f%%) | unverified %d（其中有候选待复核 %d）' % (
        len(results), stats['official'], 100 * stats['official'] / max(len(results), 1),
        stats['unverified'], n_cand))
    print('命中层级：' + ', '.join('%s=%d' % kv for kv in sorted(methods.items())))
    print('HTTP 调用：' + ', '.join('%s=%d' % kv for kv in sorted(http.calls.items())))
    print('复核队列：%s\n覆盖率报表：%s' % (REVIEW_CSV, COVERAGE_CSV))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())