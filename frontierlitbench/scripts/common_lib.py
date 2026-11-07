"""FrontierLitBench Track C 数据构建共享工具库。

纯标准库实现，可独立重跑。集中提供路径、JSONL 读写、审计日志、
paper_id 生成、round_half_up、标题/URL 规范化、HTTP 请求等公共逻辑。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# --- 路径常量 -----------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]      # 项目根目录
FB_ROOT = ROOT / 'frontierlitbench'             # 本任务产物根目录

CONFIG_DIR = FB_ROOT / 'config'
SNAPSHOTS_DIR = FB_ROOT / 'snapshots'
CORPUS_DIR = FB_ROOT / 'corpus'
LABELS_DIR = FB_ROOT / 'labels'
TASKS_DIR = FB_ROOT / 'tasks'
REPORTS_DIR = FB_ROOT / 'reports'
REVIEW_DIR = FB_ROOT / 'review'
MANIFESTS_DIR = FB_ROOT / 'manifests'
SCRIPTS_DIR = FB_ROOT / 'scripts'

ALL_DIRS = (CONFIG_DIR, SNAPSHOTS_DIR, CORPUS_DIR, LABELS_DIR, TASKS_DIR,
            REPORTS_DIR, REVIEW_DIR, MANIFESTS_DIR, SCRIPTS_DIR)

# Track C 白名单与年份（协议冻结，不得修改）
TRACK_C_VENUES = ('ICLR', 'ICML', 'NeurIPS', 'ACL', 'EMNLP', 'AAAI')
YEARS = (2024, 2025, 2026)

# 任务 FB-15 需要的 ACL + EMNLP 组合
ACL_EMNLP = ('ACL', 'EMNLP')

# --- 常量 ---------------------------------------------------------------
PROTOCOL_VERSION = 'FB-1.0'
TRACK = 'C'
TRACK_LABEL = 'Agents'

# 权重与档位（协议冻结）
W_C = 0.55
W_G = 0.30
W_A = 0.15
TIER_S_RATIO = 0.20
TIER_A_RATIO = 0.40

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')


def ensure_dirs() -> None:
    """创建全部交付物子目录（幂等）。"""
    for directory in ALL_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch_now() -> int:
    return int(time.time())


def round_half_up(x: float) -> int:
    """四舍五入：floor(x + 0.5)。禁用 Python 内置 round（银行家舍入）。"""
    return math.floor(float(x) + 0.5)


def normalize_title(title: str) -> str:
    """去重用：小写并去除空白与常见标点，仅保留字母数字。"""
    text = str(title or '').lower()
    return re.sub(r'[^a-z0-9]+', '', text)


def normalize_arxiv(arxiv_id: str) -> str:
    """规范化 arXiv ID：去 URL 前缀、版本号。"""
    text = str(arxiv_id or '').strip()
    text = re.sub(r'^https?://arxiv\.org/(?:abs|pdf)/', '', text, flags=re.I)
    text = re.sub(r'^arxiv:\s*', '', text, flags=re.I)
    text = text.split('?', 1)[0]
    text = re.sub(r'\.pdf$', '', text, flags=re.I)
    text = re.sub(r'v\d+$', '', text)
    return text.strip()


def normalize_doi(doi: str) -> str:
    """规范化 DOI：去 URL 前缀、去尾部标点，统一小写。"""
    text = str(doi or '').strip()
    text = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', text, flags=re.I)
    text = re.sub(r'^doi:\s*', '', text, flags=re.I)
    text = text.rstrip('.,;')
    return text.lower()


def normalize_url(url: str) -> str:
    text = str(url or '').strip()
    text = re.sub(r'^https?://', '', text, flags=re.I)
    return text.rstrip('/').lower()


def make_paper_id(doi=None, arxiv_id=None, url=None) -> str:
    """paper_id 规则（协议 Phase 2）：doi: 优先，arxiv: 次之，url: 兜底。"""
    d = normalize_doi(doi) if doi else ''
    a = normalize_arxiv(arxiv_id) if arxiv_id else ''
    if d:
        return 'doi:' + d
    if a:
        return 'arxiv:' + a
    if url:
        return 'url:' + normalize_url(url)
    return ''


# --- 文件读写 -----------------------------------------------------------
def append_jsonl(path: Path, record: dict) -> None:
    """追加一条 JSONL 记录，父目录不存在时创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def read_jsonl(path: Path) -> list:
    """读取 JSONL，跳过空行与损坏行。"""
    if not path.is_file():
        return []
    records = []
    with path.open('r', encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None


# --- HTTP ---------------------------------------------------------------
def http_get(url: str, *, timeout=30, headers=None):
    """GET 请求，返回 (status, body_bytes)；失败返回 (None, None)。"""
    request = Request(url, headers={'User-Agent': UA, **(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except (HTTPError, URLError, OSError, ValueError):
        return None, None


def http_get_json(url: str, *, timeout=30, headers=None):
    status, body = http_get(url, timeout=timeout, headers=headers)
    if status is None or body is None:
        return None
    try:
        return json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None


# --- 审计日志 -----------------------------------------------------------
def log_review(event: str, **fields) -> dict:
    """向 review_log_agents.jsonl 追加一条审计记录。"""
    record = {'ts': utc_now_iso(), 'event': event, **fields}
    append_jsonl(LABELS_DIR / 'review_log_agents.jsonl', record)
    return record


# --- 摘要 / 校验 --------------------------------------------------------
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()
