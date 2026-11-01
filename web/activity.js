"use strict";

const el = (id) => document.getElementById(id);
const t = (key, params) => window.SGI18n.t(key, params);
function owned(node, key) { node.dataset.i18n = key; node.textContent = t(key); return node; }
function ownedTemplate(node, key, params) {
  node.dataset.i18nTemplate = key;
  node.dataset.i18nParams = JSON.stringify(params);
  node.textContent = t(key, params);
  return node;
}
function external(node, text) { delete node.dataset.i18n; node.textContent = text; }
function ownedElement(tag, key, className) { return owned(element(tag, '', className), key); }
const state = { kind: "sessions", index: null, selected: null, selectedKind: null, detailVersion: 0, pendingSession: null, pendingRun: null, query: "", mode: "all", follow: null, actionFilter: "all" };
// A running record is followed, not snapshotted: the reader may have arrived
// here mid-run from the workspace, and a frozen page would look stuck.
const FOLLOW_MS = 1600;
const RUNNING = new Set(["running", "queued", "stopping", "resuming"]);
const LABELS = {research_plan: "规划研究路线", research_coverage: "核对路线覆盖",
  task_initial: "初始化任务", provider: "模型配置", search_imported: "导入搜索轨迹", task_update: "更新任务", intent: "阅读目标策略", decision: "动作决策", resume: "恢复会话", reopen: "重开会话",
  search: "检索", assess: "评估", reuse: "复用检查", ask: "澄清",
  delivery_check: "交付检查", delivered: "交付", delivery_refused: "拒绝交付", finish: "选择结束",
  invalid_action: "非法动作", error: "错误", unsupported: "范围外", unsatisfiable: "约束冲突",
  plan: "解析需求", refine: "生成补搜", search_failed: "检索失败", agent_node: "Agent 节点",
  search_result: "整理候选", check: "核对推荐", stop: "结束任务", model: "模型调用",
  tool_start: "工具开始", tool_end: "工具完成", tool_error: "工具出错",
  source_wait: "来源排队", source_ready: "来源就绪", cancelled: "本轮停止",
};
const ACTIVITY_GROUPS = {
  task: ["task_initial", "provider", "search_imported", "task_update", "intent", "decision", "resume", "reopen"],
  search: ["search", "search_failed", "search_result", "plan", "refine", "agent_node", "research_plan"],
  tool: ["tool_start", "tool_end", "tool_error", "source_wait", "source_ready"],
  assess: ["assess", "research_coverage"],
  reuse: ["reuse"],
  delivery: ["delivery_check", "delivered", "delivery_refused", "check", "stop", "cancelled"],
  ask: ["ask"],
  error: ["invalid_action", "error", "unsupported", "unsatisfiable"],
};
const ACTIVITY_GROUP_LABELS = { task: "任务", search: "检索", tool: "工具", assess: "评估", reuse: "复用",
  delivery: "交付", ask: "澄清", error: "异常" };
const STATUS_TEXT = { complete: "完成", finished: "完成", no_results: "无结果",
  error: "错误", failed: "失败", cancelled: "已停止", running: "运行中",
  queued: "排队中", partial: "部分完成", unknown: "未知" };
const MODE_TEXT = { live: "实时", demo: "演示", offline: "离线", multi: "多源" };
function humanStatus(value) { return STATUS_TEXT[value] ? t(STATUS_TEXT[value]) : (value || t("未知")); }
function humanMode(value) { return MODE_TEXT[value] ? t(MODE_TEXT[value]) : (value || ""); }
// Titles are user/server content, not UI labels; preserve them verbatim.
function humanTitle(value) {
  return String(value || "");
}
const SOURCE_LABELS = {arxiv: "arXiv", openalex: "OpenAlex", semantic_scholar: "Semantic Scholar",
  dblp: "DBLP", openreview: "OpenReview", acl_anthology: "ACL Anthology", deepseek: "DeepSeek", lune: "Lune Research"};
const RUN_LABELS = {lune: "Lune · MCP search_papers"};
const sourceLabel = (value) => RUN_LABELS[value] || SOURCE_LABELS[value] || value;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function get(url) {
  let response;
  try {
    response = await fetch(url, { signal: AbortSignal.timeout(15000) });
  } catch (cause) {
    const error = new Error("无法连接本地服务，请确认终端仍在运行。");
    // A caller has to be able to tell "not there yet" from "cannot reach it".
    error.status = (cause && cause.status) || 0;
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "请求失败（" + response.status + "）");
    error.status = response.status;
    throw error;
  }
  return data;
}

function shortId(id) {
  return (id || "").slice(0, 8);
}

const timeFormat = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const clockFormat = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 19);
  return timeFormat.format(date).replaceAll("/", "-");
}

function formatClock(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(11, 19);
  return clockFormat.format(date);
}

function formatDuration(value) {
  if (value == null || Number.isNaN(Number(value))) return "";
  const ms = Number(value);
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`;
}

function listItem(item) {
  const li = document.createElement("li");
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.recordId = item.id;
  button.dataset.recordKind = item.kind;
  button.className = "act-item" + (state.selected === item.id && state.selectedKind === item.kind ? " active" : "");
  const dot = element("span", "", "act-dot " + (item.status || ""));
  const body = element("span", "", "act-item-body");
  const title = element("span", "", "act-item-title");
  title.textContent = humanTitle(item.title || (item.kind === "session"
    ? t("会话 {id}", {id: shortId(item.id)})
    : (item.topic || t("运行 {id}", {id: shortId(item.id)}))));
  const meta = element("span", "", "act-item-meta");
  meta.textContent = item.kind === "session"
    ? [humanStatus(item.status), t("{count} 轮", {count: item.turns}), t("{count} 事件", {count: item.events}), humanMode(item.mode),
       item.source ? (SOURCE_LABELS[item.source] || item.source) : "", t("第 {count} 版", {count: item.revision})].filter(Boolean).join(" · ")
    : [humanStatus(item.status), humanMode(item.mode), item.method, t("{count} 篇", {count: item.papers}),
       item.elapsed != null ? Number(item.elapsed).toFixed(1) + "s" : ""].filter(Boolean).join(" · ");
  const time = element("span", "", "act-item-time");
  time.textContent = formatTime(item.updated_at || item.started_at);
  body.append(title, meta, time);
  button.append(dot, body);
  button.addEventListener("click", () => select(item, {userInitiated: true}));
  li.append(button);
  return li;
}

function renderList(autoSelect = true) {
  const all = (state.kind === "sessions" ? state.index.sessions : state.index.runs) || [];
  const query = state.query.trim().toLowerCase();
  let items = state.mode === "all" ? all : all.filter(item => (item.mode || "") === state.mode);
  if (query) items = items.filter(item => `${item.title || ''} ${item.message || ''} ${item.id}`.toLowerCase().includes(query));
  const list = el("activity-list");
  const focused = document.activeElement;
  const focusedId = list.contains(focused) ? focused.dataset.recordId : null;
  const focusedKind = focusedId ? focused.dataset.recordKind : null;
  list.replaceChildren(...items.map(listItem));
  if (focusedId) {
    const replacement = [...list.querySelectorAll('.act-item')].find(button =>
      button.dataset.recordId === focusedId && button.dataset.recordKind === focusedKind);
    if (replacement) replacement.focus({preventScroll: true});
  }
  const filtered = state.index.filtered || {};
  const total = state.kind === "sessions"
    ? (filtered.sessions != null ? filtered.sessions : all.length)
    : (filtered.runs != null ? filtered.runs : all.length);
  el("activity-range").textContent = query ? t("{count}/{total} 条匹配", {count: items.length, total}) : t("显示 {count}/{total} 条", {count: items.length, total});
  el("activity-empty").hidden = items.length > 0;
  owned(el("activity-empty"), query || state.mode !== "all"
    ? "没有符合当前筛选的记录。请尝试清除搜索或切换模式。"
    : state.kind === "sessions"
      ? "还没有会话记录。先到 Chat 发起一轮对话。"
      : "还没有单次运行记录。先到 Search 发起一次检索。");
  el("tab-sessions").classList.toggle("active", state.kind === "sessions");
  el("tab-runs").classList.toggle("active", state.kind === "runs");
  el("tab-sessions").setAttribute("aria-selected", String(state.kind === "sessions"));
  el("tab-runs").setAttribute("aria-selected", String(state.kind === "runs"));
  el("tab-sessions").tabIndex = state.kind === "sessions" ? 0 : -1;
  el("tab-runs").tabIndex = state.kind === "runs" ? 0 : -1;
  el("activity-panel").setAttribute("aria-labelledby", state.kind === "sessions" ? "tab-sessions" : "tab-runs");
  if (!autoSelect) return;
  const wantedRun = state.pendingRun;
  state.pendingRun = null;
  const targetRun = wantedRun && items.find(item => item.id === wantedRun);
  if (targetRun) {
    select(targetRun);
    return;
  }
  const wanted = state.pendingSession;
  state.pendingSession = null;
  const target = wanted && items.find(item => item.id === wanted);
  if (target) {
    select(target);
    return;
  }
  const first = items[0];
  if (first && (!state.selected || !items.some(item => item.id === state.selected))) {
    select(first);
  } else if (!first) {
    stopFollow();
    state.detailVersion += 1;
    state.selected = null;
    state.selectedKind = null;
    owned(el("activity-placeholder"), "没有符合当前筛选的记录。");
    el("activity-placeholder").hidden = false;
    el("activity-detail").hidden = true;
  }
}

function detailHeader(kind, title, status, costText) {
  owned(el("activity-kind"), kind === "session" ? "会话" : "单次检索");
  el("activity-title").textContent = humanTitle(title);
  const badge = el("activity-status");
  if (STATUS_TEXT[status]) owned(badge, STATUS_TEXT[status]); else if (!status) owned(badge, "未知"); else external(badge, status);
  badge.className = "badge " + (status === "complete" || status === "finished" ? "yes"
    : status === "failed" ? "no" : "");
  el("activity-cost").textContent = costText || "";
}

function renderTask(task, extra) {
  const host = el("activity-task");
  host.replaceChildren();
  const pairs = [];
  if (task.topic) pairs.push(["主题", task.topic]);
  if (task.purpose) pairs.push(["用途", task.purpose]);
  if (task.category) pairs.push(["类别", task.category +
    (task.minimum_category_count ? " ≥ " + task.minimum_category_count : "")]);
  if (task.target_count) pairs.push(["K", String(task.target_count)]);
  if ((task.excluded_paper_ids || []).length) pairs.push(["排除", task.excluded_paper_ids.join(", ")]);
  if ((task.unsupported || []).length) pairs.push(["超范围", task.unsupported.join("；")]);
  if (extra) pairs.push(...extra);
  for (const [key, value] of pairs) {
    const item = element("span", "", "act-pair");
    item.append(ownedElement("b", key), document.createTextNode(value));
    host.append(item);
  }
  host.hidden = pairs.length === 0;
}

function eventSummary(event) {
  const payload = event.payload || {};
  switch (event.action) {
    case "research_plan":
      return `${(payload.plan?.facets || payload.facets || []).length} 条计划路线 · 摘要级分类`;
    case "research_coverage":
      return `${(payload.coverage?.assignments || payload.assignments || []).length} 篇候选已归类 · 继续核对推荐条件`;
    case "agent_node": {
      const legal = payload.legal_actions || [];
      const detail = payload.action || payload.node || (legal.length ? "allowed " + legal.join("/") : "");
      return [payload.node, detail, payload.error, payload.stop].filter(Boolean).join(" · ");
    }
    case "tool_start":
    case "tool_end":
    case "tool_error": {
      const tool = payload.tool || payload.name || "工具";
      const source = payload.source ? " · " + sourceLabel(payload.source) : "";
      if (event.action === "tool_start") return `${tool}${source} · 已开始`;
      if (event.action === "tool_error") return `${tool}${source} · ${payload.error_code || "失败"}${payload.error ? " · " + payload.error : ""}`;
      return [tool + source, payload.status || "完成", payload.duration_ms != null ? payload.duration_ms + "ms" : "",
              payload.returned != null ? "返回 " + payload.returned : ""].filter(Boolean).join(" · ");
    }
    case "source_wait":
      return `${sourceLabel(payload.source || "")} 排队中 · 上限 ${payload.max_inflight ?? "?"}`;
    case "source_ready":
      return `${sourceLabel(payload.source || "")} 获得并发槽 · ${payload.duration_ms != null ? payload.duration_ms + "ms" : "继续"}`;
    case "search": {
      const router = payload.router || {};
      const runs = (router.batches || []).reduce((sum, batch) => sum + (batch.runs || []).length, 0);
      const cost = router.cost != null ? ` · cost ${Number(router.cost).toFixed(1)}` : "";
      return (payload.query || "") + (payload.returned != null
        ? ` → ${payload.returned} 篇，新增 ${payload.new ?? 0}` : "") +
        (runs ? ` · ${runs} 个源请求${cost}` : "");
    }
    case "assess":
      return `${payload.assessed ?? "?"} 篇，验证 ${payload.verified ?? "?"}`;
    case "search_result":
      return `返回 ${payload.returned ?? "?"} 篇 · 新增 ${payload.new_candidates ?? "?"} · 候选 ${payload.unique_candidates ?? "?"}`;
    case "check":
      return `${payload.selected ?? "?"} 篇 · ${(payload.gaps || []).length} 项缺口`;
    case "stop":
      return `${payload.status || ""} · ${payload.reason || ""}`;
    case "reuse": {
      const stale = payload.stale || [];
      const reasons = {};
      stale.forEach(item => { reasons[item.reason] = (reasons[item.reason] || 0) + 1; });
      return `复用 ${(payload.reused || []).length}，失效 ${stale.length}` +
        (Object.keys(reasons).length
          ? " · " + Object.entries(reasons).map(([k, v]) => k + ":" + v).join(" ")
          : "");
    }
    case "delivery_check":
      return (payload.ok ? "通过" : "拒绝") + (payload.reason ? " · " + payload.reason : "") +
        ` · stale ${payload.stale ?? 0}`;
    case "delivered":
      return `${(payload.papers || []).length} 篇`;
    case "finish":
      return payload.reason || "交付当前清单";
    case "ask":
      return payload.question || "";
    case "decision":
      return `${payload.decision || ""} · ${payload.reason || ""}`;
    case "intent":
      return [payload.goal, payload.policy, payload.mix].filter(Boolean).join(" · ");
    case "task_update":
      return [payload.summary, payload.patch && Object.keys(payload.patch).join(",")]
        .filter(Boolean).join(" · ");
    case "task_initial":
      return "初始结构化请求";
    case "provider":
      return [payload.profile_name || payload.profile_id, payload.model, payload.protocol].filter(Boolean).join(" · ");
    case "search_imported":
      return "来自 Search 的原始 trace · " + ((payload.trace || []).length) + " 步";
    case "invalid_action":
      return `${payload.attempted || ""} → ${payload.fallback || ""} (${payload.error || ""})`;
    case "error":
      return payload.error || "";
    case "resume":
    case "reopen":
      return payload.message || "";
    case "unsupported":
      return (payload.requirements || []).join("；");
    case "plan":
    case "refine":
      return payload.query || "";
    case "cancelled":
      return payload.message || "已按请求停止";
    default:
      return Object.keys(payload).slice(0, 4).join(", ");
  }
}

function eventRow(event, index) {
  const li = document.createElement("li");
  li.className = "act-event";
  li.dataset.action = event.action;
  li.dataset.eventKey = JSON.stringify([event.seq ?? index, event.action, index]);
  const n = element("span", String(event.seq ?? index + 1).padStart(2, "0"), "act-seq");
  const body = element("div", "", "act-event-body");
  const head = element("div", "", "act-event-head");
  head.append(
    element("span", LABELS[event.action] || event.action, "act-action " + (event.action || "")),
    element("span", "第 " + (event.revision ?? "—") + " 版", "act-rev"),
    element("span", formatClock(event.created_at || event.at), "act-time"),
  );
  body.append(head, element("p", eventSummary(event), "act-summary"));
  if (["tool_start", "tool_end", "tool_error"].includes(event.action) ||
      ["source_wait", "source_ready"].includes(event.action)) {
    const box = element("div", "", "act-sources");
    const payload = event.payload || {};
    if (payload.tool) box.append(element("span", payload.tool, "act-source"));
    if (payload.source) box.append(element("span", sourceLabel(payload.source), "act-source"));
    if (payload.duration_ms != null) box.append(element("span", formatDuration(payload.duration_ms), "act-source"));
    if (payload.http_status != null) box.append(element("span", "HTTP " + payload.http_status, "act-source"));
    if (payload.returned != null) box.append(element("span", "返回 " + payload.returned, "act-source"));
    if (payload.error_code) box.append(element("span", payload.error_code, "act-source error"));
    if (box.children.length) body.append(box);
  }
  const router = (event.payload || {}).router;
  if (router && (router.batches || []).length) {
    const box = element("div", "", "act-sources");
    for (const batch of router.batches) {
      for (const run of batch.runs || []) {
        const chip = element("span", `${sourceLabel(run.name)} ${run.status}`,
          "act-source " + (run.status || ""));
        if (run.elapsed_ms != null) chip.append(document.createTextNode(` · ${run.elapsed_ms}ms`));
        if ((run.name === 'lune' && !['cache', 'cached', 'skipped'].includes(run.status)) ||
            (run.cost_cny === null && !['cache', 'cached', 'skipped'].includes(run.status))) {
          chip.append(document.createTextNode(' · 费用未知'));
        } else if (run.cost_cny) {
          chip.append(document.createTextNode(` · ¥${Number(run.cost_cny).toFixed(3)}`));
        }
        box.append(chip);
      }
    }
    if (router.cost != null) box.append(element("span", `cost ${Number(router.cost).toFixed(1)}`, "act-source cost"));
    body.append(box);
  }
  const details = document.createElement("details");
  details.className = "act-raw";
  details.append(ownedElement("summary", "原始事件 JSON"));
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(event.payload ?? event, null, 2);
  details.append(pre);
  body.append(details);
  li.append(n, body);
  return li;
}

function renderFilters(actions) {
  const host = el("activity-filters");
  const focused = document.activeElement;
  const focusedGroup = host.contains(focused) ? focused.dataset.group : null;
  host.replaceChildren();
  const present = new Set(actions);
  const choices = [["all", "全部"]].concat(Object.keys(ACTIVITY_GROUPS).map(key => [key, ACTIVITY_GROUP_LABELS[key]]));
  for (const [key, label] of choices) {
    if (key !== "all" && !ACTIVITY_GROUPS[key].some(action => present.has(action))) continue;
    const button = ownedElement("button", label, "chip act-filter" + (key === state.actionFilter ? " active" : ""));
    button.type = "button";
    button.dataset.group = key;
    button.setAttribute("aria-pressed", String(key === state.actionFilter));
    button.addEventListener("click", () => {
      state.actionFilter = key;
      host.querySelectorAll(".act-filter").forEach(node => {
        node.classList.toggle("active", node === button);
        node.setAttribute("aria-pressed", String(node === button));
      });
      const timeline = el("activity-timeline");
      for (const row of timeline.children) {
        row.hidden = key !== "all" && !ACTIVITY_GROUPS[key].includes(row.dataset.action);
      }
      el("activity-count").textContent =
        t("{count} 条事件", {count: [...timeline.children].filter(row => !row.hidden).length});
    });
    host.append(button);
  }
  if (focusedGroup) {
    const replacement = [...host.querySelectorAll('.act-filter')].find(button => button.dataset.group === focusedGroup);
    (replacement || host.querySelector('.act-filter'))?.focus({preventScroll: true});
  }
  applyActionFilter();
}

// Preserve a reader's open event and keyboard location when a followed record
// receives more events; do not carry those UI states across different records.
function renderTimeline(events) {
  const timeline = el("activity-timeline");
  const recordKey = JSON.stringify([state.selectedKind, state.selected]);
  const sameRecord = timeline.dataset.recordKey === recordKey;
  const open = new Set();
  let focusedKey = null;
  if (sameRecord) {
    for (const row of timeline.children) {
      const details = row.querySelector('.act-raw');
      if (details?.open) open.add(row.dataset.eventKey);
      if (details?.querySelector('summary') === document.activeElement) focusedKey = row.dataset.eventKey;
    }
  }
  const rows = events.map(eventRow);
  timeline.dataset.recordKey = recordKey;
  timeline.replaceChildren(...rows);
  for (const row of rows) {
    const details = row.querySelector('.act-raw');
    if (open.has(row.dataset.eventKey)) details.open = true;
    if (focusedKey === row.dataset.eventKey) details.querySelector('summary').focus({preventScroll: true});
  }
}

function applyActionFilter() {
  const key = state.actionFilter;
  const timeline = el("activity-timeline");
  if (!timeline) return;
  for (const row of timeline.children) {
    row.hidden = key !== "all" && !(ACTIVITY_GROUPS[key] || []).includes(row.dataset.action);
  }
  el("activity-count").textContent =
    t("{count} 条事件", {count: [...timeline.children].filter(row => !row.hidden).length});
}

function renderCostBreakdown(detail) {
  const host = el("activity-cost-breakdown");
  const cost = (detail.result && detail.result.cost) || detail.spend
    || ((detail.history || []).slice(-1)[0] || {}).cost;
  if (!cost) { host.hidden = true; host.replaceChildren(); return; }
  const value = v => (v === null || v === undefined || v === "" ? "unknown" : v);
  host.hidden = false;
  host.replaceChildren(
    ownedElement("b", "成本估算"),
    element("span", `${value(cost.total)} ${cost.currency || ""} total${cost.estimate ? "（估算）" : ""}`),
    element("span", `模型 ${value(cost.model)} ${cost.currency || ""} · 工具 ${value(cost.tools)} ${cost.tools_currency || "CNY"}`),
    element("span", `${value(cost.input_tokens)} 输入 / ${value(cost.output_tokens)} 输出 · 单价 ${value(cost.input_price)} / ${value(cost.output_price)} 每 1M`),
  );
}

const deletingRecords = new Set();
const recordKey = (kind, id) => JSON.stringify([kind, id]);

async function deleteDetail(detail) {
  if (!detail || !state.selected || detail.id !== state.selected || detail.kind !== state.selectedKind) return;
  const key = recordKey(detail.kind, detail.id);
  if (deletingRecords.has(key)) return;
  if (isFollowing(detail)) {
    owned(el('activity-placeholder'), '运行中的记录不能删除；请先停止并等待结束。');
    el('activity-placeholder').hidden = false;
    return;
  }
  if (!window.confirm(t('删除这条本地记录？不会删除远端数据、备份或已发出的请求。'))) return;
  const selectedId = state.selected;
  const selectedKind = state.selectedKind;
  const endpoint = detail.kind === 'session'
    ? '/api/chat/sessions/' + encodeURIComponent(selectedId)
    : '/api/jobs/' + encodeURIComponent(selectedId);
  const version = state.detailVersion;
  const current = () => state.detailVersion === version && state.selected === selectedId && state.selectedKind === selectedKind;
  deletingRecords.add(key);
  configureDelete(detail);
  try {
    const response = await fetch(endpoint, {method: 'DELETE', headers: {'Content-Type': 'application/json'},
      signal: AbortSignal.timeout(15000)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || t('删除失败。'));
    const url = new URL(location.href);
    const param = selectedKind === 'session' ? 'session' : 'run';
    if (url.searchParams.get(param) === selectedId) {
      url.searchParams.delete(param);
      history.replaceState(history.state, '', url.pathname + url.search + url.hash);
    }
    if (selectedKind === 'session' && state.pendingSession === selectedId) state.pendingSession = null;
    if (selectedKind === 'run' && state.pendingRun === selectedId) state.pendingRun = null;
    const wasCurrent = current();
    if (wasCurrent) {
      stopFollow(); state.selected = null; state.selectedKind = null; state.detailVersion += 1;
      el('activity-detail').hidden = true; el('activity-placeholder').hidden = false;
      owned(el('activity-placeholder'), '本地记录已删除。');
    }
    const afterVersion = state.detailVersion;
    const refreshed = await refreshIndex();
    if (wasCurrent && state.detailVersion === afterVersion && !state.selected && !refreshed) {
      owned(el('activity-placeholder'), '本地记录已删除，但列表刷新失败；请稍后刷新。');
    }
  } catch (error) {
    if (current()) {
      el('activity-placeholder').hidden = false;
      external(el('activity-placeholder'), error.message || t('删除失败；本地记录状态未知。'));
    }
  } finally {
    deletingRecords.delete(key);
    const shown = el('activity-delete')._detail;
    if (shown && state.selected === shown.id && state.selectedKind === shown.kind) configureDelete(shown);
  }
}

function configureDelete(detail) {
  const button = el('activity-delete');
  button.hidden = false;
  button._detail = detail;
  button.disabled = isFollowing(detail) || deletingRecords.has(recordKey(detail.kind, detail.id));
  owned(button, detail.kind === 'session' ? '删除本机会话' : '删除本地运行');
  button.onclick = () => deleteDetail(detail);
}

function renderSession(detail) {
  const continueLink = el("activity-continue");
  continueLink.href = "/chat?session=" + encodeURIComponent(detail.id);
  continueLink.hidden = false;
  const spend = detail.spend || (detail.history || []).slice(-1)[0]?.cost || {};
  const spendText = spend.total != null
    ? `${Number(spend.total).toFixed(4)} ${spend.currency || ""} 估算`
    : "成本未知";
  detailHeader("session", detail.session.title || ("会话 " + shortId(detail.id)),
    detail.session.status,
    `${spendText} · 账本 ${Number(detail.cost.settled).toFixed(2)}`);
  renderTask(detail.task || {});
  renderCostBreakdown(detail);
  renderStory(detail);
  const historyHost = el("activity-history");
  historyHost.replaceChildren();
  for (const turn of detail.history || []) {
    const row = element("div", "", "act-turn");
    row.append(element("b", "第 " + String(turn.turn).padStart(2, "0") + " 轮"),
               element("span", turn.status || "", "badge"),
               element("span", (turn.papers || 0) + " 篇", "act-turn-meta"),
               element("p", turn.message || "（无消息）", "act-turn-message"));
    historyHost.append(row);
  }
  historyHost.hidden = !(detail.history || []).length;
  renderFilters(detail.events.map(event => event.action));
  renderTimeline(detail.events);
  applyActionFilter();
  el("activity-count").textContent = t("{count} 条事件", {count: detail.events.length});
  configureDelete(detail);
  el("activity-detail").hidden = false;
  el("activity-placeholder").hidden = true;
}

function renderRun(detail) {
  el("activity-continue").hidden = true;
  el("activity-history").hidden = true;
  el("activity-history").replaceChildren();
  const result = detail.result || liveResult(detail);
  const metrics = result.metrics || {};
  const spend = result.cost || metrics.cost || {};
  const spendText = spend.total != null
    ? `${Number(spend.total).toFixed(4)} ${spend.currency || ""} 估算`
    : "成本未知";
  detailHeader("run", result.display_title || (result.request && result.request.topic) || "运行 " + shortId(detail.id),
    result.status, `${spendText} · 账本 ${metrics.cost_units ? Number(metrics.cost_units.settled).toFixed(2) : "—"}`);
  renderTask(result.request || {}, [
    ["模式", result.mode || ""], ["方法", result.method || ""],
    ["论文", String((result.papers || []).length)],
    ["耗时", metrics.elapsed_seconds != null ? Number(metrics.elapsed_seconds).toFixed(1) + "s" : "—"],
  ]);
  renderCostBreakdown(detail);
  renderStory(detail);
  const trace = (result.trace || []).map((event, index) => ({
    seq: event.step ?? index + 1, revision: result.revision ?? 1,
    action: event.action, payload: event.payload || event, created_at: event.at || event.created_at || result.started_at,
  }));
  renderFilters(trace.map(event => event.action));
  renderTimeline(trace);
  applyActionFilter();
  el("activity-count").textContent = t("{count} 条事件", {count: trace.length});
  configureDelete(detail);
  el("activity-detail").hidden = false;
  el("activity-placeholder").hidden = true;
}

// A job that is still running has no result.json yet. Its snapshot carries the
// trace and the request, so the page can follow it without inventing anything.
// `detail.live` is the raw /api/jobs snapshot.
function liveResult(detail) {
  const job = detail.live || {};
  return {
    status: job.state || 'unknown',
    running: true,
    request: job.request || {},
    papers: job.result && Array.isArray(job.result.papers) ? job.result.papers : [],
    trace: job.trace || [],
    metrics: (job.result && job.result.metrics) || {},
    cost: (job.result && job.result.cost) || null,
    mode: job.mode || '',
    method: job.result ? job.result.method || '' : '',
    display_title: job.title || '',
    started_at: job.created_at || '',
  };
}

function liveDetail(job, id) {
  return { kind: 'run', id: id, live: job, result: null };
}

function followPill(on, text) {
  const pill = el("activity-follow");
  if (!pill) return;
  pill.hidden = !on;
  if (on && text) owned(el("activity-follow-text"), text);
}

function stopFollow() {
  if (state.follow && state.follow.timer) clearTimeout(state.follow.timer);
  state.follow = null;
  followPill(false);
}

function rememberScroll() {
  const timeline = el("activity-timeline");
  return { window: Math.round(window.scrollY), timeline: timeline ? timeline.scrollTop : 0 };
}

function restoreScroll(saved) {
  const timeline = el("activity-timeline");
  if (timeline && saved.timeline) timeline.scrollTop = saved.timeline;
  if (saved.window) window.scrollTo(0, saved.window);
}

// Fetch one record. A single run that is still in flight has no result file,
// so fall back to the live job snapshot instead of reporting "not found".
async function fetchDetail(item) {
  if (item.kind === "session") {
    return { detail: await get("/api/activity/sessions/" + encodeURIComponent(item.id)) };
  }
  try {
    return { detail: await get("/api/activity/runs/" + encodeURIComponent(item.id)) };
  } catch (error) {
    if (error.status !== 404) throw error;
    const job = await get("/api/jobs/" + encodeURIComponent(item.id));
    return { detail: liveDetail(job, item.id), job };
  }
}

function statusOf(detail) {
  if (detail.kind === "session") return (detail.session || {}).status || "";
  // A live job carries `state`; a finished record carries `result.status`.
  if (detail.live) return detail.live.state || "";
  return ((detail.result || {}).status) || "";
}

function isFollowing(detail) {
  return RUNNING.has(String(statusOf(detail)));
}

async function select(item, {userInitiated = false} = {}) {
  stopFollow();
  state.selected = item.id;
  state.selectedKind = item.kind;
  const version = ++state.detailVersion;
  renderList(false);
  // Clear the old detail while loading, so selected and displayed records agree.
  el("activity-detail").hidden = true;
  el("activity-placeholder").hidden = false;
  owned(el("activity-placeholder"), "正在读取这条记录…");
  el("activity-detail").setAttribute("aria-busy", "true");
  const current = () => state.detailVersion === version && state.selected === item.id && state.selectedKind === item.kind;
  try {
    const { detail } = await fetchDetail(item);
    if (!current()) return;
    if (item.kind === "session") renderSession(detail); else renderRun(detail);
    const focus = document.activeElement;
    if (userInitiated && window.innerWidth <= 740 && focus &&
        focus.dataset.recordId === item.id && focus.dataset.recordKind === item.kind) {
      el("activity-title").focus({preventScroll: true});
      el("activity-detail").scrollIntoView({ block: "start", behavior: "smooth" });
    }
    if (isFollowing(detail)) startFollow(item, detail);
  } catch (error) {
    if (!current()) return;
    el("activity-placeholder").hidden = false;
    el("activity-detail").hidden = true;
    external(el("activity-placeholder"), error.message);
  } finally {
    if (current()) el("activity-detail").setAttribute("aria-busy", "false");
  }
}

// Poll the selected record while it runs, and only redraw when something moved.
function startFollow(item, detail) {
  const key = item.kind + ":" + item.id;
  state.follow = { key, timer: null, signature: signatureOf(detail), ticks: 0 };
  followPill(true, item.kind === "run" ? "正在跟踪这次运行" : "正在跟踪这一轮");
  scheduleFollow(item);
}

function signatureOf(detail) {
  if (detail.kind === "session") {
    const events = (detail.events || []).length;
    const status = (detail.session || {}).status || "";
    const turns = (detail.history || []).length;
    return `${status}|${events}|${turns}|${(detail.session || {}).revision || 0}`;
  }
  if (detail.live) {
    const job = detail.live;
    return `${job.state || ""}|${(job.trace || []).length}`;
  }
  const result = detail.result || {};
  return `${result.status || ""}|${(result.trace || []).length}`;
}

function scheduleFollow(item) {
  if (!state.follow) return;
  state.follow.timer = setTimeout(() => followTick(item), FOLLOW_MS);
}

async function followTick(item) {
  if (!state.follow || state.follow.key !== item.kind + ":" + item.id) return;
  if (document.hidden) { scheduleFollow(item); return; }
  const version = state.detailVersion;
  const saved = rememberScroll();
  try {
    const { detail } = await fetchDetail(item);
    if (!state.follow || state.detailVersion !== version) return;
    state.follow.ticks += 1;
    const signature = signatureOf(detail);
    if (signature !== state.follow.signature) {
      state.follow.signature = signature;
      if (item.kind === "session") renderSession(detail); else renderRun(detail);
      restoreScroll(saved);
      refreshIndex();
    }
    if (!isFollowing(detail)) {
      stopFollow();
      await refreshIndex();
      return;
    }
    scheduleFollow(item);
  } catch (error) {
    // A transient read failure must not kill the follow loop silently; keep
    // trying, but stop following once the record is genuinely gone.
    if (!state.follow) return;
    if (error.status === 404) { stopFollow(); return; }
    scheduleFollow(item);
  }
}

async function refreshIndex() {
  const query = state.query;
  const generation = searchGeneration;
  try {
    const index = await get("/api/activity" + (query ? "?q=" + encodeURIComponent(query) : ""));
    if (generation !== searchGeneration || query !== state.query) return false;
    state.index = index;
    renderStats();
    renderList(false);
    return true;
  } catch (_) { return false; /* the detail stays readable */ }
}

function renderStats() {
  const sessions = state.index.sessions || [];
  const runs = state.index.runs || [];
  const totals = state.index.totals || {};
  const events = totals.events != null ? totals.events
    : sessions.reduce((sum, item) => sum + (item.events || 0), 0);
  const done = totals.completed != null ? totals.completed
    : sessions.filter(item => item.status === "finished").length
      + runs.filter(item => item.status === "complete").length;
  const total = totals.total != null ? totals.total : sessions.length + runs.length;
  el("stat-sessions").textContent = String(totals.sessions != null ? totals.sessions : sessions.length);
  el("stat-runs").textContent = String(totals.runs != null ? totals.runs : runs.length);
  el("stat-events").textContent = String(events);
  el("stat-complete").textContent = total ? Math.round(done / total * 100) + "%" : "—";
}

async function load() {
  const generation = searchGeneration;
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    el("activity-tz").textContent = t("时间显示：本地时间（{zone}）· 数据源为 UTC 时间戳", {zone});
    const index = await get("/api/activity");
    if (generation !== searchGeneration) return;
    state.index = index;
    if (!(state.index.sessions || []).length && (state.index.runs || []).length) state.kind = "runs";
    const params = new URLSearchParams(location.search);
    state.pendingSession = params.get("session");
    state.pendingRun = params.get("run");
    if (state.pendingRun) state.kind = "runs";
    renderStats();
    const runId = state.pendingRun;
    if (runId && !(state.index.runs || []).some(item => item.id === runId)) {
      // The job may still be running, which means no result.json and no index
      // row yet: select it anyway, fetchDetail() falls back to the live job.
      state.pendingRun = null;
      renderList(false);
      await select({id: runId, kind: "run"});
      return;
    }
    const sessionId = state.pendingSession;
    if (sessionId && !(state.index.sessions || []).some(item => item.id === sessionId)) {
      state.pendingSession = null;
      renderList(false);
      await select({id: sessionId, kind: "session"});
      return;
    }
    renderList();
  } catch (error) {
    if (generation !== searchGeneration) return;
    el("activity-empty").hidden = false;
    external(el("activity-empty"), error.message);
  }
}

let searchTimer = null;
let searchGeneration = 0;
el("activity-search").addEventListener("input", event => {
  state.query = event.target.value || "";
  const generation = ++searchGeneration;
  stopFollow();
  state.detailVersion += 1; state.selected = null; state.selectedKind = null;
  el("activity-list").replaceChildren();
  el("activity-empty").hidden = false; owned(el("activity-empty"), "正在筛选记录…");
  el("activity-detail").hidden = true; el("activity-placeholder").hidden = false;
  owned(el("activity-placeholder"), "正在筛选记录…");
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    try {
      const index = await get("/api/activity?q=" + encodeURIComponent(state.query));
      if (generation !== searchGeneration) return;
      state.index = index;
      renderStats();
      renderList();
    } catch (error) {
      if (generation !== searchGeneration) return;
      el("activity-empty").hidden = false;
      external(el("activity-empty"), error.message);
    }
  }, 220);
});
el("activity-mode").addEventListener("change", event => {
  state.mode = event.target.value || "all";
  if (state.index) renderList();
});
el("tab-sessions").addEventListener("click", () => {
  state.kind = "sessions"; state.selected = null; stopFollow(); if (state.index) renderList();
});
el("tab-runs").addEventListener("click", () => {
  state.kind = "runs"; state.selected = null; stopFollow(); if (state.index) renderList();
});
for (const tab of [el("tab-sessions"), el("tab-runs")]) {
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === "Home" ? el("tab-sessions")
      : event.key === "End" ? el("tab-runs")
      : tab.id === "tab-sessions" ? el("tab-runs") : el("tab-sessions");
    target.click();
    target.focus();
  });
}
window.addEventListener("beforeunload", stopFollow);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.follow && state.selected) {
    // Resume immediately instead of waiting out the remaining delay.
    if (state.follow.timer) clearTimeout(state.follow.timer);
    followTick({ id: state.selected, kind: state.selectedKind });
  }
});

window.addEventListener("sg:language-change", () => {
  if (state.index) renderList(false);
  document.querySelectorAll('[data-i18n-template]').forEach(node => {
    node.textContent = t(node.dataset.i18nTemplate, JSON.parse(node.dataset.i18nParams));
  });
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  el("activity-tz").textContent = t("时间显示：本地时间（{zone}）· 数据源为 UTC 时间戳", {zone});
  applyActionFilter();
});

load();
