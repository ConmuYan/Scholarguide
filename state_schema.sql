-- ScholarGuide v2 state schema (SQLite)
-- 状态：与 docs/ARCHITECTURE.md §4.E / §5、docs/DESIGN_INVARIANTS.md 对齐。
-- 原则：少量逻辑对象 + JSON 主体列；状态变更与事件、预算结算同事务提交。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 一个会话 = 一次任务的生命周期（可跨多个用户轮次与一次 ASK 等待）。
CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('active','awaiting_user','finished','failed')),
  -- 分配预算上限（秒/动作/LLM 请求/候选数），冻结后不因恢复而重置
  budget_json     TEXT NOT NULL DEFAULT '{}'
);

-- 任务版本历史。revision 只用于审计与回溯，不参与复用判定（见 I2）。
CREATE TABLE IF NOT EXISTS task_versions (
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  revision        INTEGER NOT NULL,
  task_json       TEXT NOT NULL,          -- TaskState（topic/purpose/K/category/excluded/...）
  updated_at      TEXT NOT NULL,
  source_message  TEXT,                   -- 触发本次变更的用户原文，保留可追溯性
  change_summary  TEXT,                   -- 程序生成的字段级 patch 摘要
  PRIMARY KEY (session_id, revision)
);

-- 会话当前任务指针（避免每次扫最大 revision）
CREATE TABLE IF NOT EXISTS session_task (
  session_id      TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
  revision        INTEGER NOT NULL,
  FOREIGN KEY (session_id, revision) REFERENCES task_versions(session_id, revision)
);

-- 论文原始证据。内容可变（如摘要修订），因此以 content_hash 标识一个快照。
CREATE TABLE IF NOT EXISTS papers (
  paper_id        TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  title           TEXT NOT NULL,
  abstract        TEXT NOT NULL,
  authors_json    TEXT NOT NULL DEFAULT '[]',
  url             TEXT,
  year            INTEGER,
  venue           TEXT,
  source          TEXT NOT NULL DEFAULT 'arxiv',
  fetched_at      TEXT NOT NULL,
  doi             TEXT NOT NULL DEFAULT '',
  arxiv_id        TEXT NOT NULL DEFAULT '',
  open_access_url TEXT NOT NULL DEFAULT '',
  abstract_source TEXT NOT NULL DEFAULT '',
  metadata_only   INTEGER NOT NULL DEFAULT 0,
  sources_json    TEXT NOT NULL DEFAULT '[]',
  authority       REAL NOT NULL DEFAULT 0,
  snippet         TEXT NOT NULL DEFAULT '',
  provenance_conflict INTEGER NOT NULL DEFAULT 0,
  publication_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (paper_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_papers_content ON papers(content_hash);

-- 一个判断由一个 (paper 快照, 语义键) 唯一确定。
-- computed_with 为四元组 JSON: {content_hash, semantic_key, model, prompt_version}
-- 其中 semantic_key = hash(topic, purpose, category, model, prompt_version)
CREATE TABLE IF NOT EXISTS assessments (
  paper_id             TEXT NOT NULL,
  content_hash         TEXT NOT NULL,
  semantic_key         TEXT NOT NULL,
  computed_with        TEXT NOT NULL,
  relevance            INTEGER NOT NULL CHECK (relevance IN (0,1,2)),
  category_status      TEXT NOT NULL CHECK (category_status IN ('yes','no','unknown')),
  paper_type           TEXT NOT NULL DEFAULT 'unknown',
  fit_score            INTEGER NOT NULL DEFAULT 50 CHECK (fit_score BETWEEN 0 AND 100),
  understanding_sentence TEXT,
  evidence_quote       TEXT NOT NULL DEFAULT '',
  evidence_verified    INTEGER NOT NULL DEFAULT 0 CHECK (evidence_verified IN (0,1)),
  unknown_reason       TEXT,
  model                TEXT,
  prompt_version       TEXT,
  computed_at          TEXT NOT NULL,
  PRIMARY KEY (paper_id, content_hash, semantic_key),
  FOREIGN KEY (paper_id, content_hash) REFERENCES papers(paper_id, content_hash) ON DELETE CASCADE,
  -- I1：相关即须有已验证引文
  CHECK (relevance = 0 OR evidence_verified = 1)
);
CREATE INDEX IF NOT EXISTS idx_assessments_semantic ON assessments(semantic_key);
CREATE INDEX IF NOT EXISTS idx_assessments_content ON assessments(content_hash);

-- 本会话中出现过的候选（含来源 query 与首次出现时间）
CREATE TABLE IF NOT EXISTS candidates (
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  paper_id        TEXT NOT NULL,
  content_hash    TEXT NOT NULL,
  first_query     TEXT,
  first_seen_at   TEXT NOT NULL,
  PRIMARY KEY (session_id, paper_id),
  FOREIGN KEY (paper_id, content_hash) REFERENCES papers(paper_id, content_hash)
);

-- 已执行的检索，用于去重、审计与评估（不替代事件流）
CREATE TABLE IF NOT EXISTS queries (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  revision        INTEGER NOT NULL,
  action_seq      INTEGER NOT NULL,
  query           TEXT NOT NULL,
  returned_count  INTEGER NOT NULL DEFAULT 0,
  new_count       INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queries_session ON queries(session_id, id);

-- 成本账本：每个动作一条，reserved/settled/refunded 满足 I5
CREATE TABLE IF NOT EXISTS budget_ledger (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  action_seq      INTEGER NOT NULL,
  action          TEXT NOT NULL,
  reserved        REAL NOT NULL DEFAULT 0,
  settled         REAL NOT NULL DEFAULT 0,
  refunded        REAL NOT NULL DEFAULT 0,
  detail_json     TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL,
  CHECK (settled >= 0 AND settled <= reserved),
  CHECK (abs(settled + refunded - reserved) < 1e-9)
);
CREATE INDEX IF NOT EXISTS idx_ledger_session ON budget_ledger(session_id, id);

-- 事件流：动作、观察、复用理由、非法动作、错误，全部 append-only
CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  seq             INTEGER NOT NULL,
  revision        INTEGER,
  action          TEXT NOT NULL,
  payload_json    TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL,
  UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
