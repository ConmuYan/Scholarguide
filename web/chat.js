"use strict";

const el = (id) => document.getElementById(id);
const t = (key, params) => window.SGI18n.t(key, params);
// Only mark application-owned text, never user or service content.
const i18nNodes = new Set();
function trackI18n(node) {
  if (node) i18nNodes.add(node);
  return node;
}
function uiText(node, key) {
  node.dataset.i18n = key;
  node._sgI18nText = key;
  trackI18n(node);
  node.textContent = t(key);
}
function uiFormat(node, key, params) {
  node._sgI18nFormat = {key, params};
  trackI18n(node);
  node.textContent = t(key, params);
}
function uiAttribute(node, name, key) {
  node.setAttribute('data-i18n-' + name, key);
  node._sgI18nAttribute = {name, key};
  trackI18n(node);
  node.setAttribute(name, t(key));
}
function uiAttributeFormat(node, name, key, params) {
  node._sgI18nAttribute = {name, key, params};
  trackI18n(node);
  node.setAttribute(name, t(key, params));
}
const state = { sessionId: null, busy: false, turns: 0, timer: null, started: 0,
  lastEventSeq: 0, canSend: true, canRetry: true, archived: false,
  feedbackLeft: null, feedbackCap: null, capabilities: null,
  watchVersion: 0, pendingSessionId: null, recoveryUncertainSessionId: null, configReady: null, modeChosen: false,
  composing: false, draftContext: null, task: null, savedItems: [], cancelRequested: false,
  liveTrace: [], title: '', titleIsCustom: false, titleSaving: false, loadingSession: false, sessionReadFailed: false };

const STATUS_LABELS = {complete: "已完成", partial: "部分完成", no_results: "暂无结果",
  awaiting_user: "等待回答", failed: "运行失败", unsupported: "需要调整要求",
  running: "运行中", cancelled: "已停止", active: "待继续"};
const EXAMPLES = {
  evaluation: {mode: "live", message: "推荐 5 篇 RAG 论文，至少 2 篇提出评测方法或 benchmark，用于设计我的实验。"},
  medical: {mode: "live", message: "推荐 5 篇医疗 RAG 研究论文，重点关注评测方法，帮助我确定实验方向。"},
  demo: {mode: "demo", message: "Recommend five RAG papers including one evaluation paper."},
};

function draftKey(context = state.draftContext) {
  return "scholarguide-chat-draft:" + (context || "new");
}

function saveDraft() {
  try {
    const value = el("message").value;
    if (value) localStorage.setItem(draftKey(), JSON.stringify({message: value,
      mode: el("mode").value, source: el("source").value,
      scenario: el("scenario").value}));
    else localStorage.removeItem(draftKey());
  } catch (_) {}
}

function resizeComposer() {
  const input = el("message");
  input.rows = 2;
  while (input.rows < 8 && input.scrollHeight > input.clientHeight + 2) input.rows += 1;
  el("message-count").textContent = input.value.length + " / 2000";
}

function setDraft(value, focus = false) {
  el("message").value = value || "";
  resizeComposer();
  saveDraft();
  if (focus && !el("message").disabled) el("message").focus();
}

function switchDraft(context, preserve = true) {
  if (preserve) saveDraft();
  state.draftContext = context || null;
  let value = "";
  try { value = localStorage.getItem(draftKey()) || ""; } catch (_) {}
  try {
    const draft = JSON.parse(value);
    if (draft && typeof draft.message === "string") {
      value = draft.message;
      if (["live", "demo"].includes(draft.mode)) {
        el("mode").value = draft.mode;
        state.modeChosen = true;
      }
      if (draft.source) el("source").value = draft.source;
      if (["followup", "ask"].includes(draft.scenario)) el("scenario").value = draft.scenario;
    }
  } catch (_) { /* drafts saved by an earlier version contain plain text */ }
  el("message").value = value.slice(0, 2000);
  resizeComposer();
}

function updateModeContext() {
  const demo = el("mode").value === "demo";
  el("scenario-field").hidden = state.loadingSession || !demo;
  el("source-field").hidden = state.loadingSession || demo;
  el("chat-setup").hidden = state.loadingSession || state.configReady !== false;
  uiText(el("chat-mode-note"), state.loadingSession
    ? (state.sessionReadFailed ? "未能读取所选会话的模式；请重连或选择其他记录。" : "正在读取所选会话的模式与任务…") : demo
    ? "离线 Demo · 固定 RAG 样例，不调用模型。仅支持排除第一篇；其他反馈走预设流程，不保证实际改变清单。研究自己的主题请使用 Live。"
    : state.configReady === true
      ? "Live · 使用已配置模型真实检索，可能产生 API 费用。发送前可检查主题、来源与要求。"
      : state.configReady === false
        ? "Live 尚未配置模型。请先打开 Models 设置，或主动选择离线 Demo。"
        : "正在检查模型配置；也可以主动选择离线 Demo。");
  renderTaskContext();
  updateComposer();
}

function useExample(name) {
  if (state.busy || state.sessionId) return;
  const example = EXAMPLES[name];
  if (!example || !confirmDraftReplacement(example.message)) return;
  state.modeChosen = true;
  el("mode").value = example.mode;
  if (example.mode === "demo") {
    el("scenario").value = "followup";
    el("source").value = "arxiv";
  }
  updateModeContext();
  setDraft(example.message, true);
  setStatus("示例已填入草稿；检查后点击发送。", "");
}

function renderTaskContext() {
  if (state.loadingSession) return;
  const task = state.task;
  el("task-context-mode").removeAttribute("data-i18n");
  el("task-context-mode").textContent = (el("mode").value === "demo" ? t("离线 Demo") : "Live") +
    (state.sessionId && el("mode").value !== "demo" ? " · " + sourceLabel(el("source").value) : "");
  el("task-context-empty").hidden = Boolean(task && Object.keys(task).length);
  const grid = el("task-context-grid");
  grid.replaceChildren();
  grid.hidden = !task || !Object.keys(task).length;
  if (grid.hidden) return;
  const goalNames = {orientation: "入门阅读", comparison: "方法对比", frontier: "探索研究方向", general: "通用阅读"};
  const categoryNames = {evaluation: "评测 / Benchmark", survey: "综述", research: "研究论文"};
  const rows = [["主题", task.topic], ["目的", task.purpose || (goalNames[task.reading_goal] ? t(goalNames[task.reading_goal]) : null)],
    ["目标", task.target_count ? t("{count} 篇", {count: task.target_count}) : null],
    ["类别", task.category && task.category !== "none" ?
      t("至少 {count} 篇{category}", {count: task.minimum_category_count || 0,
        category: categoryNames[task.category] ? t(categoryNames[task.category]) : task.category}) : null],
    ["会议", task.venue], ["年份", task.year_from || task.year_to ?
      t("{from}–{to}", {from: task.year_from || t("不限"), to: task.year_to || t("不限")}) : null],
    ["已排除", (task.excluded_paper_ids || []).join("、")],
    ["待调整", (task.unsupported || []).join("；")]];
  for (const [label, value] of rows) {
    if (!value) continue;
    const term = document.createElement("dt"); term.textContent = t(label);
    const detail = document.createElement("dd"); detail.textContent = String(value);
    grid.append(term, detail);
  }
}

const reconnect = document.createElement("button");
reconnect.id = "chat-reconnect";
reconnect.type = "button";
reconnect.className = "btn-text";
uiText(reconnect, "重新连接");
reconnect.hidden = true;
reconnect.addEventListener("click", () => {
  if (!state.busy && state.pendingSessionId === state.sessionId) resumeSession(state.sessionId);
});
el("status").after(reconnect);

function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function setModelPill(provider, model, protocol, profileId, ready = true) {
  const node = el("chat-model-label");
  node.replaceChildren();
  if (!ready) {
    uiText(node, "未配置 API");
    node.classList.add("off");
    uiAttribute(node, "title", "真实检索需要完成本机配置；离线演示仍可使用。");
    return;
  }
  const copy = document.createElement("span");
  copy.className = "model-copy";
  const providerNode = document.createElement("span");
  providerNode.className = "model-provider";
  providerNode.textContent = provider || "Provider";
  const modelNode = document.createElement("span");
  modelNode.className = "model-name";
  modelNode.textContent = model || "model";
  copy.append(providerNode, modelNode);
  node.append(copy);
  node.classList.remove("off");
  node.title = [provider, model, protocol, profileId].filter(Boolean).join(" · ");
  uiAttributeFormat(node, "aria-label", "当前模型：{models}", {models: [provider, model].filter(Boolean).join(", ")});
}

async function loadActiveConfig() {
  try {
    const config = await get("/api/config");
    state.configReady = Boolean(config.ready);
    const defaultSource = config.research_default_source || config.source;
    if (!state.sessionId && !state.modeChosen && [...el("source").options].some(option => option.value === defaultSource)) {
      el("source").value = defaultSource;
    }
    if (!state.sessionId && !state.modeChosen) el("mode").value = config.ready ? "live" : "demo";
    updateModeContext();
    if (!config.ready) {
      setModelPill(null, null, null, null, false);
      return;
    }
    setModelPill(config.provider, config.model, config.protocol, config.profile_id, true);
  } catch (_) {
    state.configReady = false;
    updateModeContext();
    setModelPill(null, null, null, null, false);
  }
}

function updateDiagnosisAvailability() {
  for (const button of document.querySelectorAll("button.diagnosis-action")) {
    const budgetBlocked = button.dataset.newSession !== "true" && !state.canRetry;
    button.disabled = state.busy || Boolean(state.pendingSessionId) || budgetBlocked;
    button.title = budgetBlocked ? t("反馈预算已用尽；请新建会话或归档当前会话。")
      : (button.dataset.actionTitle || "");
  }
  for (const button of document.querySelectorAll(".turn-retry")) {
    button.disabled = state.busy || Boolean(state.pendingSessionId) || !state.canRetry;
  }
  for (const button of document.querySelectorAll(".followup-chip")) {
    button.disabled = state.busy || Boolean(state.pendingSessionId) || !state.canSend;
  }
}

function applyCapabilities(snapshot) {
  syncSessionTitle(snapshot);
  const caps = (snapshot && snapshot.capabilities) || {};
  const archived = Boolean(snapshot && snapshot.archived);
  state.capabilities = caps;
  state.archived = archived;
  state.canSend = !archived && caps.can_send !== false;
  state.canRetry = !archived && caps.can_retry !== false;
  state.feedbackLeft = snapshot && snapshot.feedback_phases_left != null
    ? Number(snapshot.feedback_phases_left) : null;
  state.feedbackCap = snapshot && snapshot.feedback_phases_cap != null
    ? Number(snapshot.feedback_phases_cap) : null;
  const budget = el("budget-status");
  const hasSession = Boolean(state.sessionId);
  budget.hidden = !hasSession;
  budget.className = "chat-budget";
  if (!hasSession) {
    budget.textContent = "";
  } else if (archived) {
    budget.textContent = t("当前会话已归档；可以恢复查看，但发送和诊断重试已禁用。");
    budget.classList.add("archived");
  } else if (state.feedbackLeft === 0 && !caps.awaiting_user) {
    budget.textContent = t("反馈预算已用尽（0/{cap}）。当前结果保留；请新建会话或归档当前会话。", {cap: state.feedbackCap ?? 2});
    budget.classList.add("exhausted");
  } else if (state.feedbackLeft != null) {
    budget.textContent = caps.awaiting_user
      ? t("等待澄清回答；反馈预算还剩 {left}/{cap} 轮。", {left: state.feedbackLeft, cap: state.feedbackCap ?? 2})
      : t("反馈预算：还剩 {left}/{cap} 轮。", {left: state.feedbackLeft, cap: state.feedbackCap ?? 2});
  } else {
    budget.textContent = "";
  }
  el("archive-session").hidden = !hasSession || archived;
  el("restore-session").hidden = !hasSession || !archived;
  updateComposer();
}

async function refreshSessionCapabilities() {
  if (!state.sessionId) {
    applyCapabilities(null);
    return;
  }
  try {
    const sessionId = state.sessionId;
    const version = state.watchVersion;
    const snapshot = await get("/api/chat/sessions/" + sessionId);
    if (state.sessionId === sessionId && state.watchVersion === version) applyCapabilities(snapshot);
  } catch (_) { /* keep last known capability; server errors are shown separately */ }
}

function setStatusKey(key, params, kind) {
  const node = el("status");
  node.removeAttribute("data-i18n");
  node._sgI18nFormat = {key, params};
  trackI18n(node);
  node.textContent = t(key, params);
  node.dataset.kind = kind || "";
}

function setStatus(text, kind) {
  const node = el("status");
  node.removeAttribute("data-i18n");
  delete node._sgI18nFormat;
  node.textContent = text;
  node.dataset.kind = kind || "";
}

function updateComposer() {
  const pending = Boolean(state.pendingSessionId);
  const setupNeeded = !state.sessionId && el("mode").value === "live" && state.configReady !== true;
  const disabled = state.busy || pending || !state.canSend || setupNeeded;
  el("send").disabled = disabled;
  el("message").disabled = state.archived || (!state.canSend && !state.busy && !pending);
  el("composer-keyboard-help").textContent = t(state.busy || pending
    ? "可以预写下一条反馈 · 当前轮结束后手动发送 · 草稿会保留"
    : "Enter 发送 · Shift+Enter 换行 · 中文输入确认不会发送");
  el("rename-session").disabled = state.busy || pending || state.titleSaving;
  el("save-session-title").disabled = state.busy || pending || state.titleSaving;
  el("new-session").disabled = state.busy;
  for (const id of ["mode", "scenario", "source"]) {
    el(id).disabled = state.busy || Boolean(state.sessionId);
  }
  el("send").textContent = t(state.busy ? "运行中…" : pending ? "等待重连" : setupNeeded ? "配置模型后发送" : state.canSend ? "发送" : "已耗尽");
  const stoppable = state.busy && Boolean(state.sessionId) && Boolean(state.pendingSessionId);
  el("stop-turn").hidden = !stoppable;
  el("stop-turn").disabled = state.cancelRequested;
  el("stop-turn").textContent = t(state.cancelRequested ? "正在停止…" : "停止本轮");
  updateDiagnosisAvailability();
}

function setBusy(busy) {
  state.busy = busy;
  updateComposer();
  if (busy) {
    document.querySelector('.result-arrival')?.remove();
    state.liveTrace = [];
    el("chat-progress").replaceChildren(window.ScholarGuideResearch.progress([], {chat: true}));
    state.started = Date.now();
    el("running").hidden = false;
    el("empty-chat").hidden = true;
    el("running-time").textContent = "0s";
    uiText(el("running-text"), "正在理解需求；检索和评估进度会显示在这里。");
    el("running-trace-list").replaceChildren();
    uiText(el("running-trace-count"), "0 个事件");
    el("running-trace").hidden = true;
    if (!state.timer) {
      state.timer = setInterval(() => {
        el("running-time").textContent = Math.floor((Date.now() - state.started) / 1000) + "s";
      }, 1000);
    }
  } else {
    el("running").hidden = true;
    el("running-trace").hidden = true;
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    updateComposer();
  }
}

async function post(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(180000),
    });
  } catch (_) {
    throw new Error(t("无法连接本地服务，请确认终端仍在运行。"));
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "请求失败（" + response.status + "）");
    error.status = response.status;
    error.code = data.error_code || data.code || "";
    throw error;
  }
  return data;
}

async function stopTurn() {
  if (!state.busy || !state.pendingSessionId || state.cancelRequested) return;
  const sessionId = state.sessionId;
  const version = state.watchVersion;
  state.cancelRequested = true;
  updateComposer();
  setStatusKey("正在请求停止；已检索的候选和证据会保留。", {}, "");
  try {
    await post("/api/chat/sessions/" + sessionId + "/cancel", {});
    assertCurrentWatch(sessionId, version);
    setStatusKey("停止请求已收到；正在等待当前步骤结束并保存已有结果。", {}, "");
  } catch (error) {
    if (state.watchVersion !== version || state.sessionId !== sessionId) return;
    state.cancelRequested = false;
    updateComposer();
    setStatusKey("未能确认停止：{error} 进度跟踪仍会继续。", {error: error.message}, "error");
  }
}

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

function assertCurrentWatch(sessionId, version) {
  if (state.sessionId === sessionId && state.watchVersion === version) return;
  const error = new Error(t("会话已切换。"));
  error.code = "watch_superseded";
  throw error;
}

async function readTurn(sessionId, version, after) {
  for (let attempt = 0; ; attempt += 1) {
    assertCurrentWatch(sessionId, version);
    try {
      const [feed, snapshot] = await Promise.all([
        get('/api/chat/sessions/' + sessionId + '/events?after=' + after),
        get('/api/chat/sessions/' + sessionId),
      ]);
      assertCurrentWatch(sessionId, version);
      if (attempt) setStatusKey("已重新连接，继续跟踪这一轮…", {}, "running");
      return { feed, snapshot };
    } catch (error) {
      assertCurrentWatch(sessionId, version);
      if (error.status === 404) throw error;
      const transient = !error.status || error.status === 429 || error.status >= 500;
      if (!transient || attempt >= 3) {
        error.code = "poll_disconnected";
        throw error;
      }
      setStatusKey("连接中断，正在重连（{attempt}/3）… 已提交消息不会再次发送。", {attempt: attempt + 1}, "reconnecting");
      await sleep(500 * (attempt + 1));
    }
  }
}

function pauseWatch(error, sessionId, version) {
  assertCurrentWatch(sessionId, version);
  state.pendingSessionId = sessionId;
  reconnect.hidden = false;
  setStatusKey("{error} 自动重连已停止；点击“重新连接”继续查看，不会重新发送消息。", {error: error.message}, "error");
}

function finishWatch() {
  state.pendingSessionId = null;
  state.recoveryUncertainSessionId = null;
  state.cancelRequested = false;
  reconnect.hidden = true;
}

async function pollTurn(sessionId, version) {
  const stream = window.ScholarGuideTrace.subscribe('/api/chat/sessions/' + sessionId + '/events', {
    after: state.lastEventSeq || 0,
    onEvent(event) { if (state.sessionId === sessionId && state.watchVersion === version) appendLiveTrace(event); },
    onError() { /* JSON snapshot reads remain the reconnect fallback. */ },
  });
  try {
  let after = state.lastEventSeq || 0;
  const started = Date.now();
  while (Date.now() - started < 900000) {
    const { feed, snapshot } = await readTurn(sessionId, version, after);
    if (snapshot.cancel_requested || snapshot.cancellation_state === "stopping") {
      state.cancelRequested = true;
      updateComposer();
    }
    for (const event of feed.events || []) {
      after = Math.max(after, event.seq || 0);
      state.lastEventSeq = after;
      appendLiveTrace(event);
      el('running-text').removeAttribute('data-i18n');
      if (event.action === 'agent_node') {
        const node = (event.payload && event.payload.node) || 'agent';
        const detail = event.payload && (event.payload.action || event.payload.query || event.payload.stop);
        const nodes = {prepare:'整理任务与已存证据', decide:'规划下一步', search:'检索论文', assess:'核对摘要与类别', deliver:'整理研究结果', stop:'保存当前进展'};
        el('running-text').textContent = (nodes[node] || '正在处理研究任务') + (detail ? ' · ' + detail : '');
      } else if (event.action === 'decide') {
        el('running-text').textContent = '正在根据已有证据规划下一步';
      } else if (event.action === 'search') {
        el('running-text').textContent = '正在检索 · ' + ((event.payload && event.payload.query) || '论文');
      } else if (event.action === 'assess') {
        el('running-text').textContent = '正在核对摘要、相关性与推荐条件';
      }
    }
    if (snapshot.status !== 'running') {
      if (!Object.hasOwn(STATUS_LABELS, snapshot.status) && !['finished', 'archived'].includes(snapshot.status)) {
        const error = new Error('无法确认本轮状态，请重新连接。');
        error.code = 'poll_disconnected';
        throw error;
      }
      if (snapshot.error) throw new Error(snapshot.error);
      const last = (snapshot.history || []).slice(-1)[0];
      if (!last) throw new Error('Turn finished without a result.');
      return last;
    }
    await sleep(450);
  }
  const error = new Error('本次进度连接已持续 15 分钟，可重新连接查看已提交任务。');
  error.code = "poll_disconnected";
  throw error;
  } finally { stream.close(); }
}

async function get(path) {
  let response;
  try {
    response = await fetch(path, { signal: AbortSignal.timeout(15000) });
  } catch (_) {
    throw new Error(t("无法连接本地服务。"));
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "请求失败（" + response.status + "）");
    error.status = response.status;
    error.code = data.error_code || data.code || "";
    throw error;
  }
  return data;
}

function confirmDraftAction(changesSession = false) {
  if (!el("message").value) return true;
  const location = changesSession ? "草稿保留在当前会话，不会转移到新会话。" : "草稿会保留。";
  return window.confirm(t("当前有未发送的草稿。继续将立即执行此操作，可能调用远端服务并产生费用；{location}是否继续？", {location: t(location)}));
}

function confirmDraftReplacement(next) {
  const current = el("message").value;
  const isUnmodifiedExample = Object.values(EXAMPLES).some(example => example.message === current);
  return !current || current === next || isUnmodifiedExample ||
    window.confirm(t("当前有未发送的草稿。填入示例将替换当前草稿，是否继续？"));
}

function submitSuggestedMessage(message) {
  if (state.busy || state.pendingSessionId || !state.canRetry || !state.canSend) return;
  if (el("message").value) {
    if (confirmDraftAction()) submitMessage(null, message);
  } else {
    el("message").value = message;
    el("composer").requestSubmit();
  }
}

async function copyText(text, button) {
  const original = button.textContent;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else {
      const focused = document.activeElement;
      const area = document.createElement("textarea");
      area.value = text; area.style.position = "fixed"; area.style.opacity = "0";
      try {
        document.body.append(area); area.select();
        if (!document.execCommand("copy")) throw new Error("copy failed");
      } finally {
        area.remove();
        if (focused && typeof focused.focus === "function") focused.focus({preventScroll: true});
      }
    }
    uiText(button, "已复制");
  } catch (_) { uiText(button, "复制失败"); }
  setTimeout(() => { button.textContent = original; }, 1200);
}

function safeLink(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch (_) { return null; }
}

const SOURCE_LABELS = {arxiv: "arXiv", openalex: "OpenAlex", semantic_scholar: "Semantic Scholar",
  dblp: "DBLP", openreview: "OpenReview", acl_anthology: "ACL Anthology", deepseek: "DeepSeek", lune: "Lune Research"};
const sourceLabel = (value) => SOURCE_LABELS[value] || value;

const TRACE_LABELS = {research_plan: "规划研究路线", research_coverage: "核对路线覆盖",
  provider: "模型配置", task_initial: "建立任务", task_update: "更新任务", search_imported: "导入 Search",
  reuse: "复用证据", intent: "阅读目标", agent_node: "Agent 节点",
  decision: "Agent 决策", invalid_action: "拒绝动作", search: "检索论文",
  assess: "评估候选", delivery_check: "交付检查", delivered: "交付结果",
  ask: "请求澄清", unsupported: "不支持需求", unsatisfiable: "无法满足",
  search_failed: "检索失败", error: "错误", reopen: "重开任务", resume: "继续会话",
};

function traceDetail(event) {
  const p = event.payload || {};
  switch (event.action) {
    case "research_plan":
      return t("{count} 条计划路线 · 摘要级分类", {count: (p.plan?.facets || p.facets || []).length});
    case "research_coverage":
      return t("{count} 篇候选已归类 · 继续核对推荐条件", {count: (p.coverage?.assignments || p.assignments || []).length});
    case "provider":
      return [p.profile_name || p.profile_id, p.model, p.protocol].filter(Boolean).join(" · ");
    case "task_initial":
    case "task_update":
    case "resume":
      return p.message || t("任务已更新");
    case "search_imported":
      return (p.request && p.request.topic) || p.message || t("从 Search 导入");
    case "reuse":
      return t("复用 {reused} 篇 · stale {stale} 篇 · rate {rate}", {reused: (p.reused || []).length, stale: (p.stale || []).length, rate: p.reuse_rate ?? "n/a"});
    case "intent":
      return [p.goal ? t("目标 {goal}", {goal: p.goal}) : "", p.mix || "", p.policy || ""].filter(Boolean).join(" · ");
    case "agent_node":
      return [p.node, p.action, p.accepted === false ? "rejected" : ""].filter(Boolean).join(" · ");
    case "decision":
      return [p.decision, p.reason].filter(Boolean).join(" · ");
    case "invalid_action":
      return [`${p.attempted || "?"} → ${p.fallback || "?"}`, p.error].filter(Boolean).join(" · ");
    case "search":
      return [p.query, p.returned != null ? t("返回 {count} 篇", {count: p.returned}) : "",
              p.new_candidates != null ? t("新增 {count} 篇", {count: p.new_candidates}) : ""].filter(Boolean).join(" · ");
    case "assess":
      return [t("检查 {count} 篇", {count: p.assessed ?? "?"}), p.verified != null ? t("引文匹配 {count} 篇", {count: p.verified}) : ""]
        .filter(Boolean).join(" · ");
    case "delivery_check":
      return [p.ok === false ? t("未通过") : t("通过"), p.reason].filter(Boolean).join(" · ");
    case "delivered":
      return t("{count} 篇论文", {count: (p.papers || []).length});
    case "ask":
      return p.question || t("等待用户补充");
    case "error":
    case "search_failed":
      return p.error || p.message || t("运行出现问题");
    default:
      return "";
  }
}

function traceRow(event) {
  const li = document.createElement("li");
  li.className = "chat-trace-item" + (["error", "search_failed"].includes(event.action) ? " trace-error" : "");
  const seq = document.createElement("span");
  seq.className = "chat-trace-seq";
  seq.textContent = String(event.seq || "").padStart(2, "0");
  const body = document.createElement("div");
  body.className = "chat-trace-copy";
  const title = document.createElement("strong");
  title.textContent = t(TRACE_LABELS[event.action] || event.action);
  const detailText = traceDetail(event);
  const detail = document.createElement("small");
  detail.textContent = detailText || t("已记录");
  detail.title = detailText || event.action;
  body.append(title, detail);
  li.append(seq, body);
  return li;
}

function appendLiveTrace(event) {
  if (!event || !event.action) return;
  state.liveTrace = window.ScholarGuideTrace.upsert(state.liveTrace, event);
  const timeline = window.ScholarGuideTrace.render(state.liveTrace);
  const list = el("running-trace-list");
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 32;
  list.replaceChildren(...timeline.children);
  el("chat-progress").replaceChildren(window.ScholarGuideResearch.progress(state.liveTrace, {chat: true}));
  el("running-trace-count").removeAttribute("data-i18n");
  el("running-trace-count").textContent = t("{count} 个真实事件", {count: state.liveTrace.length});
  el("running-trace").hidden = false;
  if (nearBottom) list.scrollTop = list.scrollHeight;
}

async function loadTurnTrace(details, revision) {
  if (details.dataset.loaded) return;
  details.dataset.loaded = "1";
  const list = details.querySelector(".chat-trace-list");
  const meta = details.querySelector(".turn-trace-meta");
  if (!state.sessionId) {
    meta.removeAttribute("data-i18n");
    meta.textContent = t("会话尚未建立");
    return;
  }
  try {
    const feed = await get("/api/chat/sessions/" + state.sessionId + "/events");
    const events = (feed.events || []).filter((event) =>
      revision == null || Number(event.revision) === Number(revision));
    list.replaceChildren();
    const timeline = window.ScholarGuideTrace.render(events);
    list.replaceChildren(...timeline.children);
    if (!events.length) {
      const empty = document.createElement("li");
      empty.className = "chat-trace-empty";
      empty.textContent = t("本轮没有可展示的持久化事件；完整信息见 Activity。");
      list.append(empty);
    }
    meta.removeAttribute("data-i18n");
    meta.textContent = t("{count} 个事件", {count: events.length});
  } catch (error) {
    delete details.dataset.loaded;
    list.replaceChildren();
    const empty = document.createElement("li");
    empty.className = "chat-trace-empty trace-error";
    empty.textContent = t("轨迹加载失败：{error}", {error: error.message});
    list.append(empty);
    meta.removeAttribute("data-i18n");
    meta.textContent = t("加载失败");
  }
}

function provenanceRow(paper) {
  const row = document.createElement("div");
  row.className = "paper-provenance";
  const sources = (paper.sources && paper.sources.length ? paper.sources : [paper.source]).filter(Boolean);
  for (const source of sources) {
    const badge = document.createElement("span");
    badge.className = "source-badge"; badge.textContent = sourceLabel(source);
    row.append(badge);
  }
  if (paper.metadata_only) {
    const badge = document.createElement("span");
    badge.className = "badge no"; uiText(badge, "仅元数据"); row.append(badge);
  }
  if (paper.provenance_conflict) {
    const badge = document.createElement("span");
    badge.className = "badge warn"; uiText(badge, "来源摘要不一致"); row.append(badge);
  }
  for (const [label, href] of [["OA 全文", safeLink(paper.open_access_url)],
                               ["DOI", paper.doi ? safeLink("https://doi.org/" + paper.doi) : null]]) {
    if (!href) continue;
    const link = document.createElement("a");
    link.className = "paper-link"; link.href = href; link.target = "_blank";
    link.rel = "noopener noreferrer"; uiText(link, label); row.append(link);
  }
  return row.children.length ? row : null;
}

function citationText(paper) {
  return window.ScholarGuideResearch.citation(paper);
}

function paperRow(paper, index) {
  const item = document.createElement("li");
  item.className = "turn-paper";
  item.append(Object.assign(document.createElement("span"), {
    className: "p-n", textContent: String(index + 1).padStart(2, "0"),
  }));
  const body = document.createElement("div");
  const title = document.createElement("h4");
  const url = safeLink(paper.url);
  if (url) {
    const link = document.createElement("a");
    link.href = url; link.textContent = paper.title || paper.id;
    link.target = "_blank"; link.rel = "noopener noreferrer";
    title.append(link);
  } else title.textContent = paper.title || paper.id;
  body.append(title);
  const publication = window.ScholarGuideResearch?.publication(paper);
  if (publication) body.append(publication);

  const meta = document.createElement("p");
  meta.className = "p-meta";
  meta.textContent = [
    Array.isArray(paper.authors) && paper.authors.length ? paper.authors.slice(0, 2).join(", ") + (paper.authors.length > 2 ? " 等" : "") : "",
    paper.year ? String(paper.year) : "",
    paper.venue || (paper.sources || []).map(sourceLabel).join(" / "),
    paper.id,
  ].filter(Boolean).join(" · ");
  body.append(meta);
  const provenance = provenanceRow(paper);
  if (provenance) body.append(provenance);

  if (paper.reason) {
    const reason = document.createElement("p");
    reason.className = "p-reason";
    reason.textContent = paper.reason;
    body.append(reason);
  }

  if (paper.evidence_quote) {
    const block = document.createElement("div");
    block.className = "evidence-block";
    const quote = document.createElement("blockquote");
    quote.textContent = paper.evidence_quote;
    const long = paper.evidence_quote.length > 160;
    if (long) quote.classList.add("is-clamped");
    block.append(quote);
    if (long) {
      const toggle = document.createElement("button");
      toggle.type = "button"; toggle.className = "btn-text quote-toggle"; uiText(toggle, "展开引文");
      toggle.setAttribute("aria-expanded", "false");
      toggle.addEventListener("click", () => {
        const clamped = quote.classList.toggle("is-clamped");
        uiText(toggle, clamped ? "展开引文" : "收起引文");
        toggle.setAttribute("aria-expanded", String(!clamped));
      });
      block.append(toggle);
    }
    const quoteCopy = document.createElement("button");
    quoteCopy.type = "button"; quoteCopy.className = "btn-text";
    quoteCopy.dataset.copyQuote = ""; uiText(quoteCopy, "复制引文");
    quoteCopy.addEventListener("click", () => copyText(paper.evidence_quote, quoteCopy));
    block.append(quoteCopy);
    body.append(block);
  } else {
    const missing = document.createElement("p");
    missing.className = "evidence-missing";
    uiText(missing, "缺少可核验引文：这篇论文未通过摘要引文检查，未计入推荐依据。可调整主题后重试。");
    body.append(missing);
  }

  const actions = document.createElement("div");
  actions.className = "paper-actions";
  if (paper.paper_type && paper.paper_type !== "unknown") {
    actions.append(Object.assign(document.createElement("span"), {
      className: "badge type",
      textContent: ({survey: "综述", benchmark: "Benchmark", research: "研究论文"})[paper.paper_type] || paper.paper_type,
    }));
  }
  const status = paper.category_status;
  actions.append(Object.assign(document.createElement("span"), {
    className: "badge " + (status === "yes" ? "yes" : "no"),
    textContent: t(status === "yes" ? "类别匹配" : status === "unknown" ? "类别待确认" : "其他相关"),
  }));
  actions.append(Object.assign(document.createElement("span"), {
    className: "badge " + (paper.evidence_verified ? "verified" : "no"),
    textContent: t(paper.evidence_verified ? "引文已校验" : "引文未校验"),
  }));
  const copy = document.createElement("button");
  copy.type = "button"; copy.className = "btn-text"; uiText(copy, "复制引用");
  copy.dataset.copyCitation = "";
  copy.addEventListener("click", () => copyText(citationText(paper), copy));
  actions.append(copy);
  body.append(actions, window.ScholarGuideResearch.paperDetails(paper));
  item.append(body);
  return item;
}

function historyItem(turn, message, status, paperCount) {
  const li = document.createElement("li");
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.turn = String(turn);
  button.setAttribute("aria-label", t("第 {turn} 轮：{label}", {turn, label: message || t("查看结果")}));
  for (const old of el("history").querySelectorAll("[aria-current]")) old.removeAttribute("aria-current");
  button.setAttribute("aria-current", "step");
  button.addEventListener("click", () => {
    const target = document.getElementById("turn-" + turn);
    if (target) {
      for (const old of el("history").querySelectorAll("[aria-current]")) old.removeAttribute("aria-current");
      button.setAttribute("aria-current", "step");
      target.focus({preventScroll: true});
      target.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
    }
  });
  const n = document.createElement("span");
  n.className = "h-n"; n.textContent = String(turn).padStart(2, "0");
  const middle = document.createElement("span");
  const title = document.createElement("span");
  title.className = "h-title"; title.textContent = message || t("（无消息）");
  const meta = document.createElement("span");
  meta.className = "h-meta";
  meta.textContent = t("{status}{count}", {status: t(STATUS_LABELS[status] || status), count: paperCount ? " · " + t("{count} 篇", {count: paperCount}) : ""});
  middle.append(title, meta);
  button.append(n, middle);
  li.append(button);
  el("history").prepend(li);
}

function renderResult(result, options = {}) {
  if (!result) return;
  if (result.turn && document.getElementById('turn-' + result.turn)) return;
  if (result.request && typeof result.request === "object") {
    state.task = {...result.request, unsupported: result.unsupported || result.request.unsupported || []};
  }
  renderTaskContext();
  state.turns += 1;
  const turn = result.turn || state.turns;
  const article = document.createElement("article");
  article.className = "turn";
  article.id = "turn-" + turn;
  article.tabIndex = -1;
  article.setAttribute("aria-labelledby", "turn-title-" + turn);

  const head = document.createElement("header");
  head.className = "turn-head";
  const n = document.createElement("span");
  n.className = "turn-n"; uiFormat(n, "第 {turn} 轮", {turn: String(turn).padStart(2, "0")});
  n.id = "turn-title-" + turn;
  const status = document.createElement("span");
  status.className = "turn-state " + (result.status || "");
  const stopped = result.stop_reason === "user_cancelled" || result.status === "cancelled";
  const statusKey = stopped ? "已停止" : STATUS_LABELS[result.status];
  const updateStatusLabel = () => { status.textContent = [statusKey ? t(statusKey) : result.status, result.revision ? "rev " + result.revision : ""].filter(Boolean).join(" · "); };
  status._sgI18nUpdate = updateStatusLabel;
  updateStatusLabel();
  status.title = [result.status, result.stop_reason].filter(Boolean).join(" · ");
  head.append(n, status);
  article.append(head);
  const delivered = (result.papers || []).length;
  const target = Number(result.request?.target_count) || 0;
  if (result.status !== "awaiting_user") {
    const summary = document.createElement("p");
    summary.className = "turn-summary";
    const updateSummary = () => {
      if (stopped) uiFormat(summary, "已停止本轮；已保留 {count} 篇可交付论文及现有证据。", {count: delivered});
      else if (delivered) uiFormat(summary, "已交付 {count}{target} 篇论文 · 推荐依据见每篇摘要引文。", {count: delivered, target: target ? "/" + target : ""});
      else uiText(summary, result.status === "cancelled" ? "本轮已停止；现有证据保留在会话中。" : "本轮没有可交付的论文；可在下方查看原因。");
    };
    summary._sgI18nUpdate = updateSummary;
    trackI18n(summary);
    updateSummary();
    article.append(summary);
  }

  if (result.message) {
    const message = document.createElement("p");
    message.className = "turn-message";
    const label = document.createElement("b");
    label.textContent = result.imported_from === "search" ? "Search" : "你";
    message.append(label, document.createTextNode(result.message));
    article.append(message);
  }
  if (result.question) {
    const question = document.createElement("p");
    question.className = "turn-question";
    uiFormat(question, "需要澄清：{question}", {question: result.question});
    article.append(question);
  }
  if (result.status === "failed") {
    const retry = document.createElement("button");
    retry.type = "button"; retry.className = "btn btn-inline turn-retry"; uiText(retry, "重试本轮");
    retry.disabled = !state.canRetry;
    retry.addEventListener("click", () => {
      if (state.busy || !state.canRetry) return;
      submitSuggestedMessage(result.message || state.lastMessage || "");
    });
    article.append(retry);
  }
  const noteText = {
    partial: "部分完成：仍有缺口，已交付当前可用清单。",
    unsupported: "这次反馈超出可表示范围（例如同时要求两个类别配额），本轮不交付清单；可调整配额或用途后重试。",
    no_results: "本轮没有可交付的论文；可以简化主题或调整配额后再试。",
    failed: "本轮运行失败；输入保留在会话中，可直接重试。",
    awaiting_user: "等待你的回答；回答后会在同一会话继续。",
  }[result.status];
  const diagnosis = result.diagnosis || {};
  if (diagnosis.code && diagnosis.code !== "ok" && diagnosis.summary) {
    const note = document.createElement("div");
    note.className = "turn-note diagnosis";
    const title = document.createElement("b");
    title.className = "diagnosis-title";
    uiText(title, "本轮为什么没有（足够）结果");
    const summary = document.createElement("p");
    summary.className = "diagnosis-summary";
    summary.textContent = diagnosis.summary;
    note.append(title, summary);
    const health = diagnosis.source_health || {};
    const healthItems = Object.entries(health).map(([name, state]) => {
      const codes = Array.isArray(state.codes) ? state.codes.join(' / ') : (state.last_code || '');
      return codes ? `${name}: ${codes}` : '';
    }).filter(Boolean);
    if (healthItems.length) {
      const sourceLine = document.createElement("p");
      sourceLine.className = "diagnosis-source";
    const updateSource = () => uiFormat(sourceLine, "来源状态：{health}", {health: healthItems.join(" · ")});
      sourceLine._sgI18nUpdate = updateSource;
      trackI18n(sourceLine);
      updateSource();
      note.append(sourceLine);
    }
    if (diagnosis.near_misses && diagnosis.near_misses.length) {
      const near = document.createElement("p");
      near.className = "diagnosis-near";
      const updateNear = () => uiFormat(near, "已检索到的相关候选（未交付）：{items}", {items: diagnosis.near_misses.map(item => item.title).filter(Boolean).join(" · ")});
      near._sgI18nUpdate = updateNear;
      trackI18n(near);
      updateNear();
      note.append(near);
    }
    const actions = Array.isArray(diagnosis.actions) ? diagnosis.actions : [];
    if (actions.length) {
      const row = document.createElement("div");
      row.className = "diagnosis-actions";
      const label = document.createElement("span");
      label.className = "diagnosis-label";
      uiText(label, "建议");
      row.append(label);
      for (const action of actions.slice(0, 6)) {
        if (action.retry && action.message) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "btn-text diagnosis-action";
          button.textContent = action.label;
          button.title = action.description || action.message;
          button.dataset.actionTitle = button.title;
          button.disabled = !state.canRetry;
          button.addEventListener("click", () => {
            if (state.busy || state.pendingSessionId || !state.canRetry) return;
            if (["relax_year", "relax_venue", "lower_target"].includes(action.code) &&
                action.patch && Object.keys(action.patch).length) {
              if (confirmDraftAction()) runRecovery(action);
              return;
            }
            submitSuggestedMessage(action.message);
          });
          row.append(button);
        } else if (action.code === "new_session") {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "btn-text diagnosis-action";
          button.textContent = action.label;
          button.title = action.description || action.message || "";
          button.dataset.actionTitle = button.title;
          button.dataset.newSession = "true";
          button.addEventListener("click", () => {
            if (state.busy || state.pendingSessionId) return;
            if (confirmDraftAction(true)) forkSource(el("fork-source").value || "openreview");
          });
          row.append(button);
        } else {
          const suggestion = document.createElement("span");
          suggestion.className = "diagnosis-action muted";
          suggestion.textContent = action.label;
          suggestion.title = action.description || "";
          row.append(suggestion);
        }
      }
      note.append(row);
    }
    article.append(note);
  } else if (noteText) {
    const note = document.createElement("p");
    note.className = "turn-note";
    uiText(note, noteText);
    article.append(note);
  }
  let visiblePapers = result.papers || [];
  if (result.papers && result.papers.length) {
    const original = result.papers.slice();
    const sessionId = state.sessionId;
    const version = state.watchVersion;
    let generation = 0, selected = false, pending = false;
    const controls = document.createElement('div');
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'btn btn-inline';
    uiText(button, '按近期偏好查看');
    uiAttribute(button, "title", "仅调整当前结果的显示顺序，不改变检索或发送给模型");
    button.setAttribute('aria-pressed', 'false');
    const feedback = document.createElement('span');
    feedback.setAttribute('role', 'status');
    controls.append(button, feedback);
    const list = document.createElement('ol');
    list.className = 'turn-list';
    let nav = null;
    function rebuild(papers) {
      visiblePapers = papers;
      list.replaceChildren(...papers.map((paper, i) => paperRow(paper, i)));
      if (nav) nav.remove();
      nav = window.ScholarGuideResearch.readingNavigation(papers, [...list.children], 'turn-' + turn);
      if (nav) list.before(nav);
    }
    function current(token) {
      return generation === token && state.sessionId === sessionId && state.watchVersion === version &&
        article.isConnected && article === el('turns').querySelector('.turn:last-of-type');
    }
    button.addEventListener('click', async () => {
      const token = ++generation;
      if (selected || pending) {
        selected = false; pending = false;
        rebuild(original);
        uiText(button, "按近期偏好查看"); button.setAttribute('aria-pressed', 'false');
        uiText(feedback, "已恢复原始顺序。");
        return;
      }
      if (!current(token)) {
        uiText(feedback, "只能调整当前最新一轮的显示顺序。");
        return;
      }
      pending = true;
      uiText(button, '取消排序');
      uiText(feedback, '正在检查偏好…');
      try {
        const data = await post('/api/chat/sessions/' + encodeURIComponent(sessionId) +
          '/turns/' + encodeURIComponent(turn) + '/presentation', {selected: true});
        if (!current(token)) return;
        pending = false;
        const byId = new Map(original.map(paper => [paper.id, paper]));
        const ids = data.paper_ids;
        if (data.status !== 'applied' || !Array.isArray(ids) || ids.length !== original.length ||
            byId.size !== original.length || new Set(ids).size !== ids.length || ids.some(id => !byId.has(id))) {
          uiText(button, '按近期偏好查看');
          uiText(feedback, '无法应用偏好，保留原始顺序。');
          return;
        }
        rebuild(ids.map(id => byId.get(id)));
        selected = true; uiText(button, '恢复原始顺序'); button.setAttribute('aria-pressed', 'true');
        uiText(feedback, '仅改变显示顺序，不改变筛选条件；撤回无法撤销已见结果。');
      } catch (_) {
        if (!current(token)) return;
        pending = false; uiText(button, '按近期偏好查看');
        uiText(feedback, '无法应用偏好，保留原始顺序。');
      }
    });
    article.append(controls, list);
    rebuild(original);
  }
  if (result.status !== "complete" && Array.isArray(result.candidates)) {
    const deliveredIds = new Set((result.papers || []).map(p => p.id || p.paper_id));
    const candidates = result.candidates.filter(p => p && p.title &&
      !deliveredIds.has(p.id || p.paper_id)).slice(0, 20);
    if (candidates.length) {
      const details = document.createElement("details");
      details.className = "candidate-discovery turn-candidates";
      const summary = document.createElement("summary");
      summary.textContent = t("已保留 {count} 篇未交付候选", {count: candidates.length});
      const list = document.createElement("ul");
      for (const candidate of candidates) {
        const item = document.createElement("li");
        const url = safeLink(candidate.url);
        if (url) {
          const link = document.createElement("a");
          link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
          link.textContent = candidate.title;
          item.append(link);
        } else item.textContent = candidate.title;
        list.append(item);
      }
      details.append(summary, list);
      article.append(details);
    }
  }
  if (result.gaps && result.gaps.length) {
    const gaps = document.createElement("p");
    gaps.className = "turn-foot";
    gaps.textContent = t("缺口：{items}", {items: result.gaps.map(gap => window.ScholarGuideResearch.requirement(gap, result.research_coverage)).join("；")});
    article.append(gaps);
  }
  const foot = document.createElement("p");
  foot.className = "turn-foot";
  const money = result.cost || (result.metrics && result.metrics.cost) || {};
  const value = (v) => (v === null || v === undefined || v === "" ? "unknown" : v);
  const cost = money.total != null
    ? t("{amount} {currency} 估算", {amount: Number(money.total).toFixed(4), currency: money.currency || ""})
    : t("费用未知");
  const costDetail = money && Object.keys(money).length
    ? `model ${value(money.model)} ${money.currency || ""} · tools ${value(money.tools)} ${money.tools_currency || "CNY"} · tokens ${value(money.input_tokens)} in / ${value(money.output_tokens)} out · prices ${value(money.input_price)} / ${value(money.output_price)} per 1M`
    : "";
  const reuse = typeof result.reuse === "number"
    ? { reuse_rate: result.reuse } : (result.reuse || {});
  const reused = Array.isArray(reuse.reused) ? reuse.reused.length : 0;
  const stale = Array.isArray(reuse.stale) ? reuse.stale.length : 0;
  const rate = reuse.reuse_rate != null ? Number(reuse.reuse_rate).toFixed(2) : "n/a";
  foot.textContent = t("费用 {cost} · 复用 {reused}{total}（{rate}）", {cost, reused, total: reused + stale ? "/" + (reused + stale) : "", rate});
  article.append(foot);
  if (costDetail) {
    const detail = document.createElement("p");
    detail.className = "turn-cost-detail";
    detail.textContent = costDetail;
    article.append(detail);
  }
  if (state.sessionId) {
    const exports = document.createElement("div");
    exports.className = "turn-exports";
    for (const [file, label] of [["report.md", "下载本轮报告"], ["references.bib", "BibTeX"], ["result.json", "JSON"]]) {
      const link = document.createElement("a");
      link.className = "btn-text";
      link.href = `/api/chat/sessions/${encodeURIComponent(state.sessionId)}/turns/${turn}/${file}`;
      if (label === "下载本轮报告") uiText(link, label); else link.textContent = label;
    uiAttributeFormat(link, "aria-label", "第 {turn} 轮：{label}", {turn, label: t(label)});
      exports.append(link);
    }
    article.append(exports);
  }
  if (delivered && result.status !== "awaiting_user") {
    const followups = document.createElement("div");
    followups.className = "followup-actions";
    const label = document.createElement("span");
    label.className = "followup-label";
    const demo = el("mode").value === "demo";
    uiText(label, demo ? "演示反馈：可排除第一篇（填入草稿后发送）" : "继续调整（填入草稿后发送）");
    followups.append(label);
    const first = result.papers[0];
    const choices = demo ? [] : [["更适合入门", "希望更适合刚开始研究这个方向的学生，请保留当前主题和硬性要求，调整推荐。"],
      ["聚焦评测方法", "我更关注这些论文中的评测方法，请保留当前主题和硬性要求，调整推荐。"]];
    if (first.id) choices.unshift(["排除第一篇", () => {
      const visible = visiblePapers[0];
      return visible && visible.id
        ? `排除论文 ${visible.id}（${visible.title || "第一篇"}），尽量保留其余推荐。` : "";
    }]);
    for (const [title, message] of choices) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "chip followup-chip";
      uiText(button, title);
      button.addEventListener("click", () => {
        if (state.busy || state.pendingSessionId || !state.canSend) return;
        const draft = typeof message === 'function' ? message() : message;
        if (!draft || !confirmDraftReplacement(draft)) return;
        setDraft(draft, true);
        setStatusKey("反馈已填入草稿；检查后发送，会使用一轮反馈预算。", {}, "");
      });
      followups.append(button);
    }
    article.append(followups);
  }

  const trace = document.createElement("details");
  trace.className = "turn-trace";
  trace.dataset.revision = result.revision ?? "";
  const summary = document.createElement("summary");
  const traceTitle = document.createElement("span");
  traceTitle.className = "turn-trace-title";
  uiText(traceTitle, "Agent 轨迹");
  const traceMeta = document.createElement("span");
  traceMeta.className = "turn-trace-meta";
  uiText(traceMeta, state.sessionId ? "展开加载" : "无会话");
  summary.append(traceTitle, traceMeta);
  const traceList = document.createElement("ol");
  traceList.className = "chat-trace-list";
  const traceFoot = document.createElement("p");
  traceFoot.className = "turn-trace-foot";
  const activityLink = document.createElement("a");
  activityLink.className = "btn-text";
  activityLink.href = "/activity" + (state.sessionId ? "?session=" + encodeURIComponent(state.sessionId) : "");
  uiText(activityLink, "在 Activity 查看完整审计 →");
  traceFoot.append(activityLink);
  trace.append(summary, traceList, traceFoot);
  const latest = turn === state.turns;
  if (latest) {
    for (const old of document.querySelectorAll(".turn-trace[open]")) old.open = false;
  }
  article.append(trace);

  const routeCoverage = window.ScholarGuideResearch?.coverage(result.research_coverage, {papers: result.papers, headingLevel: 3});
  if (routeCoverage) article.insertBefore(routeCoverage, trace);
  if (result.research_brief && window.ScholarGuideResearch) {
    article.insertBefore(window.ScholarGuideResearch.render(result.research_brief, {papers: result.papers, headingLevel: 3}), trace);
  }
  el("turns").append(article);
  historyItem(turn, result.message || result.question, result.status, (result.papers || []).length);
  el("empty-chat").hidden = true;
  el("question").hidden = !result.question;
  if (result.question) el("question-text").textContent = result.question;
  const awaiting = result.status === "awaiting_user";
  uiAttribute(el("message"), "placeholder", awaiting
    ? "回答上面的澄清问题，或补充你的新要求…"
    : "Describe the papers you need, then keep refining…");
  setStatusKey("会话 {session} · {status}", {session: (state.sessionId || "").slice(0, 8), status: result.status},
    awaiting ? "awaiting_user" : result.status);
  if (state.sessionId) el("session-id").textContent = state.sessionId;
  trace.addEventListener("toggle", () => {
    if (trace.open) loadTurnTrace(trace, result.revision);
  });
  if (options.announce !== false) window.ScholarGuideResearch.resultNotice(article,
    awaiting ? t("需要补充研究需求") : t("第 {turn} 轮已结束 · {count} 篇可阅读", {turn, count: (result.papers || []).length}));
}

function syncSessionLinks() {
  const link = el("open-activity");
  link.hidden = !state.sessionId;
  if (state.sessionId) link.href = "/activity?session=" + encodeURIComponent(state.sessionId);
  if (state.sessionId) {
    try { localStorage.setItem("scholarguide-chat-session", state.sessionId); } catch (_) {}
    history.replaceState(null, '', '/chat?session=' + encodeURIComponent(state.sessionId));
  }
}

function syncSessionTitle(snapshot) {
  if (snapshot) {
    state.title = snapshot.title || snapshot.display_title || snapshot.current_task?.topic || state.title;
    state.titleIsCustom = snapshot.title_is_custom === true;
  }
  const title = state.title || state.task?.topic;
  if (title) {
    el("session-title").removeAttribute("data-i18n");
    delete el("session-title")._sgI18nText;
    delete el("session-title")._sgI18nFormat;
    i18nNodes.delete(el("session-title"));
    el("session-title").textContent = title;
  } else uiText(el("session-title"), state.sessionId ? "当前研究会话" : "尚未开始研究");
  el("rename-session").hidden = !state.sessionId;
}

function closeTitleEditor() {
  el("session-title-form").hidden = true;
  el("session-title-status").textContent = "";
}

el("rename-session").addEventListener("click", () => {
  if (!state.sessionId || state.busy || state.pendingSessionId) return;
  el("session-title-form").hidden = false;
  el("session-title-input").value = state.title;
  el("session-title-input").focus();
  el("session-title-input").select();
});
el("cancel-session-title").addEventListener("click", () => {
  closeTitleEditor(); el("rename-session").focus();
});
el("session-title-input").addEventListener("keydown", event => {
  if (event.key === "Escape" && !event.isComposing) { closeTitleEditor(); el("rename-session").focus(); }
});
el("session-title-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!state.sessionId || state.busy || state.pendingSessionId || state.titleSaving) return;
  const sessionId = state.sessionId, version = state.watchVersion;
  const title = el("session-title-input").value.trim();
  state.titleSaving = true; updateComposer();
  el("session-title-status").textContent = t("正在保存…");
  try {
    const snapshot = await post('/api/chat/sessions/' + encodeURIComponent(sessionId) + '/rename', {title});
    assertCurrentWatch(sessionId, version);
    syncSessionTitle(snapshot); closeTitleEditor();
    el("rename-session").focus(); await loadSaved();
  } catch (error) {
    if (state.sessionId === sessionId && state.watchVersion === version) el("session-title-status").textContent = error.message;
  } finally { state.titleSaving = false; updateComposer(); }
});

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date).replaceAll("/", "-");
}

const deletingSessions = new Set();
const sessionRunning = item => ["running", "queued", "stopping", "resuming"].includes(item?.status) ||
  Boolean(item?.running || item?.capabilities?.running);

async function deleteSavedSession(item) {
  if (!item || deletingSessions.has(item.session_id)) return;
  if (sessionRunning(item)) {
    setStatusKey("运行中的会话不能删除；请先停止并等待结束。", {}, "error");
    return;
  }
  if (!window.confirm(t("删除这条本机会话及其本地历史？不会删除远端数据、备份或已发出的请求。"))) return;
  const version = state.watchVersion, selected = state.sessionId;
  const current = () => state.watchVersion === version && state.sessionId === selected;
  deletingSessions.add(item.session_id);
  renderSaved(state.savedItems);
  let deleted = false;
  try {
    const response = await fetch("/api/chat/sessions/" + encodeURIComponent(item.session_id), {
      method: "DELETE", headers: {"Content-Type": "application/json"},
      signal: AbortSignal.timeout(15000),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || t("删除失败。"));
    deleted = true;
    const wasCurrent = current();
    if (wasCurrent && state.sessionId === item.session_id) resetSession();
    const afterVersion = state.watchVersion, afterSession = state.sessionId;
    const refreshed = await loadSaved();
    if (wasCurrent && state.watchVersion === afterVersion && state.sessionId === afterSession) {
      setStatusKey(refreshed ? "本机会话已删除；远端数据和备份未受影响。" :
        "本机会话已删除，但列表刷新失败；请稍后刷新。", {}, refreshed ? "ok" : "error");
    }
  } catch (error) {
    if (current()) setStatus(error.message || t("删除失败；本地历史未确认删除。"), "error");
  } finally {
    deletingSessions.delete(item.session_id);
    if (deleted) state.savedItems = state.savedItems.filter(saved => saved.session_id !== item.session_id);
    renderSaved(state.savedItems);
  }
}

function renderSaved(items) {
  state.savedItems = items || [];
  const host = el("saved-sessions");
  host.replaceChildren();
  const query = el("session-search").value.trim().toLocaleLowerCase();
  const filtered = state.savedItems.filter(item => [item.title, item.display_title, item.current_task?.topic, item.message,
    item.session_id, item.source, item.mode].filter(Boolean).join(" ").toLocaleLowerCase().includes(query));
  uiFormat(el("session-search-count"), query ? "{shown} / {total} 个已加载会话符合筛选" : "{shown} / {total} 个已加载会话", {shown: filtered.length, total: state.savedItems.length});
  for (const item of filtered) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button"; button.className = "saved-item";
    if (item.session_id === state.sessionId) {
      button.classList.add("active");
      button.setAttribute("aria-current", "true");
    }
    const dot = document.createElement("span");
    dot.className = "act-dot " + (item.status || "");
    const body = document.createElement("span");
    const title = document.createElement("span");
    title.className = "h-title";
    title.textContent = (item.title || item.display_title || item.current_task?.topic || item.message || t("（无消息）")).slice(0, 80);
    title.title = item.title || item.display_title || item.message || '';
    const meta = document.createElement("span");
    meta.className = "h-meta";
    meta.textContent = [item.archived ? t("已归档") : "",
      item.origin === "search" ? "Search" : "Chat",
      STATUS_LABELS[item.status] ? t(STATUS_LABELS[item.status]) : item.status, item.mode === "demo" ? t("离线 Demo") : "Live", t("{count} 轮", {count: item.turns}), item.source,
      item.updated_at ? formatTime(item.updated_at) : ""].filter(Boolean).join(" · ");
    if (item.archived) { button.classList.add("archived"); uiAttribute(title, "title", "已归档"); }
    body.append(title, meta);
    if (item.current_task?.topic && item.current_task.topic !== title.textContent) {
      const topic = document.createElement("span"); topic.className = "saved-topic";
      topic.textContent = item.current_task.topic; body.append(topic);
    }
    if (item.paper_count != null || item.coverage_status) {
      const coverage = document.createElement("span"); coverage.className = "h-meta saved-coverage";
      const missing = Array.isArray(item.missing_facets) ? item.missing_facets.length : 0;
      coverage.textContent = [item.paper_count != null ? t("{count} 篇论文", {count: item.paper_count}) : '',
        missing ? t("{count} 条路线仍有缺口", {count: missing}) : item.coverage_status === 'ready' ? t('规划路线已有论文关联') : ''].filter(Boolean).join(' · ');
      if (coverage.textContent) body.append(coverage);
    }
    button.append(dot, body);
    button.addEventListener("click", () => resumeSession(item.session_id));
    const row = document.createElement("div");
    row.className = "saved-item-row";
    row.append(button);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn-text saved-delete";
    uiText(remove, "删除");
    uiAttribute(remove, "title", "删除本机会话历史");
    remove.dataset.sessionId = item.session_id;
    remove.disabled = sessionRunning(item) || deletingSessions.has(item.session_id);
    remove.addEventListener("click", event => {
      event.stopPropagation();
      deleteSavedSession(item);
    });
    row.append(remove);
    li.append(row);
    host.append(li);
  }
  if (!host.children.length) {
    const empty = document.createElement("li");
    empty.className = "saved-empty"; uiText(empty, query ? "没有匹配的会话，试试其他主题或 ID。" : "还没有已保存会话。");
    host.append(empty);
  }
}

async function loadSaved() {
  const request = state.savedRequest = (state.savedRequest || 0) + 1;
  try {
    const data = await get("/api/chat/sessions");
    if (state.savedRequest !== request) return false;
    renderSaved(data.sessions || []);
    return true;
  } catch (_) { return false; /* keep current session usable */ }
}

async function resumeSession(sessionId) {
  if (!sessionId || state.busy) return;
  const version = ++state.watchVersion;
  const changingSession = state.sessionId !== sessionId;
  let loaded = false;
  if (changingSession) {
    state.loadingSession = true; state.sessionReadFailed = false;
    closeTitleEditor(); state.title = ''; state.titleIsCustom = false;
    el("turns").replaceChildren();
    el("history").replaceChildren();
    state.task = null;
    el("question").hidden = true; el("question-text").textContent = "";
    el("budget-status").hidden = true;
    el("archive-session").hidden = true; el("restore-session").hidden = true;
    el("rename-session").hidden = true;
    uiText(el("session-title"), "正在读取所选会话…");
    uiText(el("task-context-mode"), "读取中…");
    el("task-context-grid").replaceChildren(); el("task-context-grid").hidden = true;
    el("task-context-empty").hidden = false;
    uiText(el("task-context-empty"), "正在读取所选会话的任务。");
  }
  switchDraft(sessionId);
  state.sessionId = sessionId;
  renderSaved(state.savedItems);
  state.modeChosen = true;
  state.pendingSessionId = sessionId;
  reconnect.hidden = true;
  if (changingSession) updateModeContext();
  el("session-id").textContent = sessionId;
  syncSessionLinks();
  setBusy(true);
  setStatusKey("正在恢复会话…", {}, "");
  try {
    const { snapshot: snap, feed: traceFeed } = await readTurn(sessionId, version, 0);
    loaded = true;
    state.loadingSession = false; state.sessionReadFailed = false;
    uiText(el("task-context-empty"), "发送后，这里会显示 Agent 理解的主题与筛选要求，便于你及时纠正。");
    state.turns = 0;
    state.lastEventSeq = (traceFeed.events || []).reduce(
      (last, event) => Math.max(last, event.seq || 0), 0);
    el("turns").replaceChildren();
    el("history").replaceChildren();
    el("question").hidden = true;
    // Render history with the restored session mode, not the previous draft's.
    if (snap.mode) el("mode").value = snap.mode;
    (snap.history || []).forEach(result => renderResult(result, {announce: false}));
    state.lastMessage = ((snap.history || []).slice(-1)[0] || {}).message || "";
    el("session-id").textContent = sessionId;
    el("source").value = snap.source || "arxiv";
    el("source").disabled = true;
    if (snap.current_task) state.task = snap.current_task;
    state.cancelRequested = Boolean(snap.cancel_requested);
    updateModeContext();
    applyCapabilities(snap);
    setStatusKey("会话 {session} · {status}", {session: sessionId.slice(0, 8), status: snap.status || "active"},
      snap.status === "awaiting_user" ? "awaiting_user" : (snap.status || ""));
    syncSessionLinks();
    if (state.recoveryUncertainSessionId === sessionId && snap.status !== "running") {
      throw new Error(t("无法确认恢复请求是否已执行；请继续重连，勿重复提交。"));
    }
    if (snap.status === "running") {
      state.recoveryUncertainSessionId = null;
      const committed = Math.max(0, ...(snap.history || []).map(row => Number(row.revision) || 0));
      for (const event of traceFeed.events || []) if (Number(event.revision) > committed) appendLiveTrace(event);
      setStatusKey("正在继续跟踪这一轮…", {}, "");
      renderResult(await pollTurn(sessionId, version));
    } else if (snap.error) {
      throw new Error(snap.error);
    } else if (!Object.hasOwn(STATUS_LABELS, snap.status) && !['finished', 'archived'].includes(snap.status)) {
      throw new Error(t("无法确认会话状态，请重新连接。"));
    }
    finishWatch();
    await refreshSessionCapabilities();
    await loadActiveConfig();
    await loadSaved();
  } catch (error) {
    if (state.watchVersion !== version || state.sessionId !== sessionId) return;
    if (state.loadingSession && !loaded) {
      state.sessionReadFailed = true;
      updateModeContext();
      uiText(el("session-title"), "会话尚未读取");
      uiText(el("task-context-mode"), "未读取");
      uiText(el("task-context-empty"), "未能读取这条会话；可以重连或选择其他记录。");
    }
    if (error.status === 404) {
      dropMissingSession(t("历史会话已不存在（可能已被清理或服务已重启），已切换为新会话。直接输入请求即可。"));
    } else {
      pauseWatch(error, sessionId, version);
    }
  } finally {
    if (state.watchVersion === version) setBusy(false);
  }
}

function resetSession() {
  state.watchVersion += 1;
  state.loadingSession = false; state.sessionReadFailed = false;
  finishWatch();
  state.modeChosen = false;
  switchDraft(null);
  state.sessionId = null;
  state.turns = 0;
  state.lastEventSeq = 0;
  state.canSend = true;
  state.canRetry = true;
  state.archived = false;
  state.feedbackLeft = null;
  state.feedbackCap = null;
  state.capabilities = null;
  state.task = null;
      el("task-context-empty").textContent = "发送后，这里会显示 Agent 理解的主题与筛选要求，便于你及时纠正。";
  state.title = ''; state.titleIsCustom = false; closeTitleEditor();
  history.replaceState(null, '', '/chat');
  setBusy(false);
  applyCapabilities(null);
  el("turns").replaceChildren();
  el("history").replaceChildren();
  el("question").hidden = true;
  el("empty-chat").hidden = false;
  el("session-id").textContent = "—";
  el("source").disabled = false;
  el("open-activity").hidden = true;
  if (state.configReady != null && !state.modeChosen) el("mode").value = state.configReady ? "live" : "demo";
  updateModeContext();
  try { localStorage.removeItem("scholarguide-chat-session"); } catch (_) {}
  setStatusKey("新会话：输入请求后创建。", {}, "");
  loadSaved();
}

function dropMissingSession(message) {
  resetSession();
  setStatus(message, "error");
}

async function runRecovery(action) {
  if (!state.sessionId || state.busy || state.pendingSessionId || !state.canRetry) return;
  const sessionId = state.sessionId, version = ++state.watchVersion;
  let responseReceived = false;
  state.pendingSessionId = sessionId;
  setBusy(true);
  setStatusKey("正在应用恢复动作：{action}…", {action: action.label}, "");
  try {
    const payload = await post("/api/chat/sessions/" + sessionId + "/recover",
                               {action: action.code, stream: true});
    assertCurrentWatch(sessionId, version);
    responseReceived = Boolean(payload.running);
    const result = payload.running ? await pollTurn(sessionId, version) : payload.result;
    assertCurrentWatch(sessionId, version);
    if (!result || !['complete', 'partial', 'no_results', 'awaiting_user', 'failed', 'unsupported', 'cancelled'].includes(result.status)) {
      throw new Error(t("无法确认恢复动作的终态。"));
    }
    renderResult(result);
    finishWatch();
    await refreshSessionCapabilities();
    await loadActiveConfig();
  } catch (error) {
    if (state.watchVersion !== version || state.sessionId !== sessionId) return;
    if (error.code === "feedback_budget_exhausted" || error.code === "session_archived") {
      finishWatch();
      setStatus(error.code === "feedback_budget_exhausted" ? "反馈预算已用尽；请新建会话或归档当前会话。" : "当前会话已归档；请先恢复后再继续。", "exhausted");
      await refreshSessionCapabilities();
    } else {
      if (!responseReceived) state.recoveryUncertainSessionId = sessionId;
      pauseWatch(error, sessionId, version);
    }
  } finally {
    if (state.watchVersion === version) setBusy(false);
  }
}

async function forkSource(source) {
  if (!state.sessionId || state.busy) return;
  setBusy(true);
  setStatusKey("正在新建会话并切换 source 为 {source}…", {source}, "");
  try {
    const payload = await post("/api/chat/sessions/" + state.sessionId + "/fork",
                               {source, stream: true});
    if (payload.session_id) {
      window.location.href = "/chat?session=" + payload.session_id;
      return;
    }
    setStatusKey("未能创建 source fallback 会话。", {}, "error");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function setArchived(archived) {
  if (!state.sessionId || state.busy) return;
  try {
    const snapshot = await post("/api/chat/sessions/" + state.sessionId +
      (archived ? "/archive" : "/restore"), {});
    applyCapabilities(snapshot);
    setStatusKey(archived ? "会话已归档；可随时恢复。" : "会话已恢复。", {}, archived ? "exhausted" : "");
    await loadSaved();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

el("archive-session").addEventListener("click", () => setArchived(true));
el("restore-session").addEventListener("click", () => setArchived(false));

async function submitMessage(event, suggestedMessage = null) {
  if (event) event.preventDefault();
  if (state.busy || state.pendingSessionId || !state.canSend) return;
  if (!state.sessionId && el("mode").value === "live" && state.configReady !== true) {
    setStatusKey("请先在 Models 配置模型，或主动选择离线 Demo。", {}, "error");
    return;
  }
  const message = (suggestedMessage === null ? el("message").value : suggestedMessage).trim();
  state.lastMessage = message;
  if (!message) return;
  saveDraft();
  state.modeChosen = true;
  const version = ++state.watchVersion;
  setBusy(true);
  setStatusKey("正在运行这一轮…", {}, "");
  try {
    let payload;
    if (!state.sessionId) {
      payload = await post("/api/chat/sessions", {
        message,
        mode: el("mode").value,
        scenario: el("scenario").value,
        source: el("source").value,
        stream: true,
      });
      if (state.watchVersion !== version) return;
      state.sessionId = payload.session_id;
      state.draftContext = state.sessionId;
      saveDraft();
      try { localStorage.removeItem(draftKey(null)); } catch (_) {}
      el("session-id").textContent = state.sessionId;
      el("source").disabled = true;
      syncSessionLinks();
      loadSaved();
    } else {
      payload = await post("/api/chat/sessions/" + state.sessionId + "/messages",
                           { message, stream: true });
    }
    if (state.watchVersion !== version) return;
    if (suggestedMessage === null && el("message").value.trim() === message) setDraft("");
    if (!payload.result) state.pendingSessionId = state.sessionId;
    updateComposer();
    const result = payload.result || await pollTurn(state.sessionId, version);
    assertCurrentWatch(result.session_id || state.sessionId, version);
    finishWatch();
    if (result.status === "failed" && !el("message").value.trim()) setDraft(message);
    renderResult(result);
    await refreshSessionCapabilities();
    await loadActiveConfig();
  } catch (error) {
    if (state.watchVersion !== version) return;
    if (error.code === "poll_disconnected") {
      pauseWatch(error, state.sessionId, version);
      return;
    }
    finishWatch();
    if (error.code === "feedback_budget_exhausted") {
      setStatusKey("反馈预算已用尽；当前结果保留，请新建会话或归档当前会话。", {}, "exhausted");
      await refreshSessionCapabilities();
    } else if (error.code === "session_archived") {
      setStatusKey("当前会话已归档；请先恢复后再继续。", {}, "exhausted");
      await refreshSessionCapabilities();
    } else if (error.status === 404) {
      dropMissingSession(t("原会话已不存在（可能已被清理），已切换为新会话；请重新发送这条消息。"));
    } else {
      setStatus(error.message, "error");
      el("empty-chat").hidden = state.turns > 0;
      // A failed turn must leave the request editable: restore it unless the
      // user already pre-wrote a different draft while the turn was running.
      if (!el("message").value.trim()) setDraft(message);
    }
  } finally {
    if (state.watchVersion === version) setBusy(false);
  }
}
el("composer").addEventListener("submit", submitMessage);

el("message").addEventListener("input", () => { resizeComposer(); saveDraft(); });
el("message").addEventListener("compositionstart", () => { state.composing = true; });
el("message").addEventListener("compositionend", () => { state.composing = false; resizeComposer(); saveDraft(); });
el("message").addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || state.composing || event.keyCode === 229) return;
  event.preventDefault();
  if (!el("send").disabled) el("composer").requestSubmit();
});
el("mode").addEventListener("change", () => { state.modeChosen = true; updateModeContext(); saveDraft(); });
el("source").addEventListener("change", () => { state.modeChosen = true; renderTaskContext(); saveDraft(); });
el("scenario").addEventListener("change", saveDraft);
el("choose-demo").addEventListener("click", () => useExample("demo"));
for (const button of document.querySelectorAll("[data-example]")) {
  button.addEventListener("click", () => useExample(button.dataset.example));
}
el("session-search").addEventListener("input", () => renderSaved(state.savedItems));
el("stop-turn").addEventListener("click", stopTurn);
window.addEventListener("beforeunload", saveDraft);
// Update marked nodes in place: no requests, result reconstruction or focus changes.
window.addEventListener("sg:language-change", () => {
  updateComposer();
  renderTaskContext();
  for (const node of i18nNodes) {
    if (!node.isConnected) continue;
    if (node._sgI18nText) node.textContent = t(node._sgI18nText);
    if (node._sgI18nFormat) node.textContent = t(node._sgI18nFormat.key, node._sgI18nFormat.params);
    if (node._sgI18nAttribute) {
      const {name, key, params} = node._sgI18nAttribute;
      node.setAttribute(name, t(key, params));
    }
    if (node._sgI18nUpdate) node._sgI18nUpdate();
  }
});
switchDraft(null, false);
updateModeContext();

el("new-session").addEventListener("click", () => {
  if (!state.busy) resetSession();
});

// Demo deep link: /chat?demo=followup or /chat?demo=ask
const initialDemo = new URLSearchParams(location.search).get("demo");
const initialSession = new URLSearchParams(location.search).get("session");
(function autoDemo() {
  const demo = initialDemo;
  if (!demo || state.sessionId || initialSession) return;
  if (el("message").value) {
    const url = new URL(location.href);
    url.searchParams.delete("demo");
    history.replaceState(null, '', url.pathname + url.search + url.hash);
    setStatusKey("已保留未发送的草稿；Demo 未自动运行。请检查草稿后手动发送。", {}, "");
    return;
  }
  const scenario = demo === "ask" ? "ask" : "followup";
  state.modeChosen = true;
  el("mode").value = "demo";
  el("source").value = "arxiv";
  el("scenario").value = scenario;
  setDraft(scenario === "ask"
    ? "Find RAG reading-list papers; I am not sure whether for a survey."
    : "Recommend five RAG papers including one evaluation paper.");
  updateModeContext();
  el("composer").requestSubmit();
})();

el("refresh-sessions").addEventListener("click", loadSaved);
if (initialSession) {
  resumeSession(initialSession);
} else if (!initialDemo) {
  let stored = null;
  try { stored = localStorage.getItem("scholarguide-chat-session"); } catch (_) {}
  if (stored) resumeSession(stored); else loadSaved();
}

el("copy-session").addEventListener("click", () => {
  if (!state.sessionId) {
    el("copy-session").textContent = t("暂无");
    setTimeout(() => { el("copy-session").textContent = t("复制"); }, 1200);
    return;
  }
  copyText(state.sessionId, el("copy-session"));
});

loadActiveConfig();
window.addEventListener("focus", loadActiveConfig);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadActiveConfig();
});
