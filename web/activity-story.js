"use strict";

/*
 * Activity trace model.
 *
 * The raw stream contains both lifecycle halves (tool_start / tool_end) and
 * legacy summary events. This module joins those halves by span_id so the
 * reader sees one real operation, with its source, duration and terminal
 * state, instead of two unrelated "tool_start" nodes.
 */

const storyT = (key, params) => window.SGI18n.t(key, params);
const TRACE_LABELS = {
  task_initial: "初始化任务", task_update: "更新任务", plan: "解析需求",
  intent: "阅读目标策略", decision: "动作决策", invalid_action: "拒绝动作",
  reuse: "复用检查", research_plan: "规划研究路线", research_coverage: "核对路线覆盖",
  agent_node: "Agent 节点", search: "检索", search_result: "候选结果",
  assess: "评估候选", check: "覆盖检查", delivery_check: "交付检查",
  delivered: "交付结果", stop: "结束本轮", cancelled: "停止本轮",
  refine: "生成补搜查询", error: "错误", search_imported: "导入 Search 轨迹",
  ask: "请求澄清", provider: "模型配置", synthesis: "研究综合",
  source_wait: "来源排队", source_ready: "来源就绪",
};
const TYPE_LABELS = {
  proposal: "PROPOSAL", contract: "CONTRACT", node: "NODE", tool: "TOOL",
  gate: "GATE", error: "ERROR", import: "IMPORT",
};
const STAGE_LABELS = {
  "model.update": "理解需求", "model.action": "选择下一步",
  "model.assess": "评估候选", "model.research_plan": "规划研究路线",
  "model.synthesis": "整理研究综合", "model.probe": "连接测试",
};
const NODE_LABELS = {
  planner: "准备状态", decide: "选择动作", retrieve: "执行检索",
  assess: "评估候选", critic: "检查缺口", verifier: "核对交付",
  finalize: "收尾", prepare: "准备上下文",
};
const GROUPS = [
  ["understand", "需求与任务", "用户输入、解析与任务状态"],
  ["runtime", "Agent 行动", "决策、门控与复用"],
  ["tools", "来源与工具", "真实模型、HTTP 与检索调用"],
  ["import", "导入记录", "Search 原始轨迹"],
  ["deliver", "交付与结果", "证据检查与最终清单"],
];

function translatedLabels(labels) {
  return Object.fromEntries(Object.entries(labels).map(([key, value]) => [key, storyT(value)]));
}
let TRACE_TEXT = translatedLabels(TRACE_LABELS);
let TYPE_TEXT = translatedLabels(TYPE_LABELS);
let STAGE_TEXT = translatedLabels(STAGE_LABELS);
let NODE_TEXT = translatedLabels(NODE_LABELS);
let GROUP_TEXT = GROUPS.map(([key, label, note]) => [key, storyT(label), storyT(note)]);
function refreshStoryText() {
  TRACE_TEXT = translatedLabels(TRACE_LABELS);
  TYPE_TEXT = translatedLabels(TYPE_LABELS);
  STAGE_TEXT = translatedLabels(STAGE_LABELS);
  NODE_TEXT = translatedLabels(NODE_LABELS);
  GROUP_TEXT = GROUPS.map(([key, label, note]) => [key, storyT(label), storyT(note)]);
}

function storyEvents(detail) {
  if (detail.events) {
    return detail.events.map(event => ({
      ...event,
      payload: event.payload && typeof event.payload === "object" ? event.payload : {},
    }));
  }
  const trace = (detail.result && detail.result.trace) || [];
  return trace.map((event, index) => ({
    seq: event.seq ?? event.step ?? index + 1,
    revision: (detail.result && detail.result.revision) ?? 1,
    action: event.action,
    payload: event.payload && typeof event.payload === "object" ? event.payload : event,
    created_at: event.at || event.created_at || (detail.result && detail.result.started_at),
    source: "run",
  }));
}
function lastTraceEvent(events, action) {
  for (let i = events.length - 1; i >= 0; i -= 1) if (events[i].action === action) return events[i];
  return null;
}
function displaySource(name) {
  const labels = { lune: "Lune", arxiv: "arXiv", openalex: "OpenAlex", semantic_scholar: "Semantic Scholar",
    dblp: "DBLP", openreview: "OpenReview", acl_anthology: "ACL Anthology", deepseek: "DeepSeek" };
  return labels[name] || (typeof sourceLabel === "function" ? sourceLabel(name) : (name || ""));
}
function durationText(value) {
  if (value == null || Number.isNaN(Number(value))) return "";
  const ms = Number(value);
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`;
}
function clock(value) {
  return value ? (typeof formatClock === "function" ? formatClock(value) : String(value).slice(11, 19)) : "";
}
function truncate(value, limit = 260) {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? "");
  return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
}
function isToolLifecycle(event) {
  return ["tool_start", "tool_end", "tool_error"].includes(event.action);
}
function operationLabel(payload) {
  const tool = String(payload.tool || payload.name || "");
  const category = String(payload.category || "");
  const source = String(payload.source || "");
  if (category === "model" || tool.startsWith("model.")) {
    return `${storyT("模型调用 · {stage}", {stage: STAGE_TEXT[tool] || tool.replace(/^model\\./, "") || storyT("生成")})}`;
  }
  if (tool === "search") return storyT("检索 · {source}", {source: displaySource(source) || storyT("来源")});
  if (tool === "http.request") return storyT("HTTP 请求 · {source}", {source: displaySource(source) || storyT("来源")});
  if (tool === "assess") return storyT("评估候选");
  if (tool === "assess_batch") return storyT("批量评估候选");
  if (source) return storyT("{tool} · {source}", {tool: tool || storyT("工具"), source: displaySource(source)});
  return tool ? tool.replace(/_/g, " ") : storyT("工具调用");
}
function operationStatus(operation) {
  if (operation.running) return "running";
  if (operation.error) return "error";
  return "done";
}
function operationSummary(operation) {
  const payload = operation.payload || {};
  if (operation.running) return storyT("执行中；页面随真实事件更新。");
  if (operation.error) {
    return ["失败", payload.error_code || payload.status, truncate(payload.error || payload.reason || "", 260)]
      .filter(Boolean).join(" · ");
  }
  const parts = [];
  if (payload.status) parts.push(payload.status);
  if (payload.duration_ms != null) parts.push(durationText(payload.duration_ms));
  if (payload.returned != null) parts.push(storyT("返回 {count}", {count: payload.returned}));
  if (payload.http_status != null) parts.push(storyT("HTTP {status}", {status: payload.http_status}));
  return parts.join(" · ") || storyT("已完成");
}
function mergeOperations(events) {
  const groups = new Map();
  const others = [];
  for (const event of events) {
    if (!isToolLifecycle(event)) {
      if (["source_wait", "source_ready"].includes(event.action)) {
        groups.set(`wait:${event.seq}:${event.action}`, {
          start: event, end: null, payload: { ...event.payload, status: event.action === "source_ready" ? "ready" : "waiting" },
        });
      } else others.push(event);
      continue;
    }
    const payload = event.payload || {};
    const key = payload.span_id || `${payload.tool || "tool"}:${payload.source || ""}:${payload.attempt || event.seq}`;
    const current = groups.get(key) || { start: null, end: null, payload: {} };
    if (event.action === "tool_start") {
      current.start = event;
      current.payload = { ...current.payload, ...payload };
    } else {
      current.end = event;
      current.payload = { ...current.payload, ...payload };
    }
    groups.set(key, current);
  }
  const operations = [...groups.values()].map(operation => {
    const start = operation.start || operation.end;
    const terminal = operation.end || (operation.start && operation.start.action === "tool_error" ? operation.start : null);
    const payload = { ...(operation.payload || {}) };
    if (terminal) {
      payload.status = payload.status || (terminal.action === "tool_error" ? "error" : "ok");
      if (terminal.payload?.duration_ms != null) payload.duration_ms = terminal.payload.duration_ms;
      if (terminal.payload?.error) payload.error = terminal.payload.error;
      if (terminal.payload?.error_code) payload.error_code = terminal.payload.error_code;
    }
    return {
      event: {
        seq: start?.seq, revision: start?.revision, action: "tool_operation",
        created_at: start?.created_at, payload,
      },
      payload,
      running: !terminal && payload.status !== "completed",
      error: payload.status === "error" || start?.action === "tool_error",
      start, end: terminal,
    };
  }).sort((a, b) => Number(a.event.seq || 0) - Number(b.event.seq || 0));
  return { operations, others };
}
function eventType(event) {
  const action = event.action;
  if (["task_initial", "task_update", "plan", "research_plan", "intent"].includes(action)) return "proposal";
  if (["decision", "invalid_action", "reuse"].includes(action)) return "contract";
  if (action === "agent_node") return event.payload?.node === "decide" ? "contract" : "node";
  if (["search", "search_result", "assess", "source_wait", "source_ready"].includes(action)) return "tool";
  if (["check", "delivery_check", "delivered", "stop", "cancelled"].includes(action)) return "gate";
  if (action === "error") return "error";
  if (action === "search_imported") return "import";
  return "node";
}
function eventGroup(event) {
  if (event.action === "search_imported") return "import";
  if (["task_initial", "task_update", "plan", "intent", "research_plan"].includes(event.action)) return "understand";
  if (["delivery_check", "delivered", "stop", "cancelled", "check"].includes(event.action)) return "deliver";
  if (event.action === "error") return "deliver";
  return "runtime";
}
function eventTitle(event) {
  if (event.action === "tool_operation") return operationLabel(event.payload);
  if (event.action === "agent_node") {
    const node = event.payload?.node || "";
    return storyT("节点 · {node}", {node: NODE_TEXT[node] || node || storyT("运行")});
  }
  if (event.action === "decision") return storyT("动作 · {decision}", {decision: event.payload?.decision || storyT("未记录")});
  if (event.action === "reuse") return storyT("复用检查");
  return TRACE_TEXT[event.action] || event.action;
}
function storySummary(event) {
  if (event.action === "tool_operation") return operationSummary(event._operation || { running: false, payload: event.payload, error: false });
  const payload = event.payload || {};
  switch (event.action) {
    case "task_update": return [payload.message, payload.summary].filter(Boolean).join(" · ") || storyT("更新 {count} 个字段", {count: Object.keys(payload.patch || {}).length});
    case "task_initial": return storyT("已建立结构化任务状态");
    case "plan": return payload.query || payload.request?.topic || storyT("已解析需求");
    case "intent": return [payload.goal, payload.mix].filter(Boolean).join(" · ");
    case "research_plan": return storyT("{count} 条研究路线", {count: (payload.plan?.facets || payload.facets || []).length});
    case "decision": return [payload.decision, payload.reason, payload.accepted === false ? "被门控" : ""].filter(Boolean).join(" · ");
    case "invalid_action": return [payload.attempted, "→", payload.fallback, payload.error].filter(Boolean).join(" ");
    case "reuse": return storyT("复用 {reused} 篇 · 失效 {stale} 篇", {reused: (payload.reused || []).length, stale: (payload.stale || []).length});
    case "agent_node": return [payload.node, payload.action, payload.stop, payload.error].filter(Boolean).join(" · ") || storyT("运行节点");
    case "search": return (payload.query || storyT("检索")) + (payload.returned != null ? storyT(" · 返回 {count} 篇", {count: payload.returned}) : "");
    case "search_result": return storyT("返回 {returned} 篇 · 新增 {added} 篇", {returned: payload.returned ?? "?", added: payload.new_candidates ?? "?"});
    case "assess": return storyT("检查 {count} 篇", {count: payload.assessed ?? payload.count ?? "?"});
    case "check": return storyT("{count} 篇 · {gaps} 项缺口", {count: payload.selected ?? "?", gaps: (payload.gaps || []).length});
    case "delivery_check": return [storyT(payload.ok ? "交付检查通过" : "交付被拒绝"), payload.reason].filter(Boolean).join(" · ");
    case "delivered": return storyT("交付 {count} 篇", {count: (payload.papers || []).length});
    case "stop": return [payload.status, payload.reason].filter(Boolean).join(" · ");
    case "cancelled": return payload.message || storyT("已按用户请求停止");
    case "error": return payload.error || payload.message || storyT("运行错误");
    case "search_imported": {
      const trace = payload.trace || payload.events || payload.result?.trace || [];
      if (!Array.isArray(trace)) return storyT("导入 0 条事件");
      const starts = trace.filter(item => item.action === "tool_start" && item.payload?.category === "source").length;
      const searches = trace.filter(item => item.action === "search").length;
      const assessments = trace.filter(item => item.action === "assess").length;
      return storyT("导入 {events} 条事件 · {searches} 轮检索", {events: trace.length, searches: starts || searches}) + (assessments ? storyT(" · {count} 次评估", {count: assessments}) : "");
    }
    default: return payload.summary || payload.message || payload.reason || storyT("已记录");
  }
}
function traceFacts(event) {
  const payload = event.payload || {};
  const facts = [];
  const add = (label, value) => {
    if (value === undefined || value === null || value === "") return;
    facts.push([storyT(label), Array.isArray(value) || typeof value === "object" ? truncate(JSON.stringify(value), 360) : truncate(value)]);
  };
  add("工具", payload.tool);
  add("来源", payload.source && displaySource(payload.source));
  add("类别", payload.category);
  add("状态", payload.status);
  add("耗时", payload.duration_ms != null ? durationText(payload.duration_ms) : "");
  add("尝试", payload.attempt);
  add("HTTP", payload.http_status);
  add("返回", payload.returned);
  add("模型", payload.model);
  add("原因", payload.reason);
  add("错误", payload.error);
  add("节点", payload.node);
  add("查询", payload.query);
  add("决策", payload.decision);
  if (payload.patch && Object.keys(payload.patch).length) add("字段变更", Object.keys(payload.patch).join(", "));
  if (payload.legal_actions?.length) add("合法动作", payload.legal_actions.join(" / "));
  if (payload.gaps?.length) add("缺口", `${payload.gaps.length} 项`);
  if (payload.plan) add("计划", payload.plan.scope || payload.plan.status);
  if (payload.usage) add("用量", payload.usage);
  if (payload.trace && Array.isArray(payload.trace)) add("导入事件", String(payload.trace.length));
  if (payload.result?.trace && Array.isArray(payload.result.trace)) add("导入事件", String(payload.result.trace.length));
  return facts;
}
function importedTrace(event) {
  const payload = event?.payload || {};
  const trace = payload.trace || payload.events || payload.result?.trace || [];
  return Array.isArray(trace) ? trace : [];
}
function buildModel(detail) {
  const base = storyEvents(detail);
  const imported = lastTraceEvent(base, "search_imported");
  const importedEvents = importedTrace(imported);
  const importedToolStarts = importedEvents.filter(item => item.action === "tool_start");
  const importedSource = importedToolStarts.some(item => item.payload?.category === "source")
    ? importedToolStarts.filter(item => item.payload?.category === "source").length
    : importedEvents.filter(item => item.action === "search").length;
  const importedModels = importedToolStarts.filter(item => item.payload?.category === "model").length;
  const importedErrors = importedEvents.filter(item => item.action === "tool_error" || item.payload?.status === "error").length;
  const importedTools = importedToolStarts.length || importedEvents.filter(item => ["search", "assess"].includes(item.action)).length;
  const merged = mergeOperations(base);
  const spans = [];
  for (const event of merged.others) {
    spans.push({
      event, index: spans.length, type: eventType(event),
      title: eventTitle(event), summary: storySummary(event), time: clock(event.created_at), source: event.source,
    });
  }
  for (const operation of merged.operations) {
    const event = operation.event;
    event._operation = { running: operation.running, payload: operation.payload, error: operation.error };
    spans.push({
      event, index: spans.length, type: operation.error ? "error" : "tool",
      title: operationLabel(operation.payload),
      summary: operationSummary(operation),
      time: clock(operation.start?.created_at),
      source: operation.payload.source,
    });
  }
  spans.sort((a, b) => Number(a.event.seq || 0) - Number(b.event.seq || 0));
  const groups = Object.fromEntries(GROUPS.map(([key]) => [key, []]));
  for (const span of spans) {
    const key = span.type === "import" ? "import" : (span.type === "tool" || span.type === "error" ? "tools" : eventGroup(span.event));
    groups[key] = groups[key] || [];
    groups[key].push(span);
  }
  return { spans, groups, imported, importedEvents, importedSource, importedModels, importedErrors, importedTools,
           operations: merged.operations };
}
function nodeState(model) {
  const has = predicate => model.spans.some(predicate);
  return [
    ["输入", true],
    ["任务", has(span => ["task_initial", "task_update", "plan", "intent"].includes(span.event.action))],
    ["规划", has(span => ["decision", "research_plan", "agent_node"].includes(span.event.action))],
    ["来源", has(span => span.type === "tool" && (span.event.payload?.source || span.event.payload?.tool === "search" || span.event.action === "search")) || model.importedSource > 0],
    ["评估", has(span => span.event.action === "assess" || span.event.payload?.tool === "model.assess")],
    ["交付", has(span => ["delivery_check", "delivered", "stop", "cancelled"].includes(span.event.action))],
  ];
}
function traceChip(text, kind = "") {
  return element("span", text, "act-trace-chip" + (kind ? " " + kind : ""));
}
function selectSpan(span, button) {
  const box = el("activity-span-inspector");
  box.replaceChildren();
  document.querySelectorAll(".trace-row.active").forEach(node => node.classList.remove("active"));
  button.classList.add("active");
  const head = element("div", "", "act-span-head");
  head.append(element("span", TYPE_LABELS[span.type] || span.type, "act-trace-type " + span.type),
              element("span", span.time || "—", "act-span-time"));
  box.append(head, TRACE_LABELS[span.event.action] ? ownedElement("h4", TRACE_LABELS[span.event.action]) : element("h4", span.title), element("p", span.summary, "act-span-summary"));
  const facts = traceFacts(span.event);
  if (facts.length) {
    const dl = element("dl", "", "act-span-facts");
    for (const [key, value] of facts) dl.append(ownedElement("dt", key), element("dd", value));
    box.append(dl);
  } else {
    box.append(ownedElement("p", "这个事件没有公开的结构化字段。", "act-span-empty"));
  }
  const raw = document.createElement("details");
  raw.className = "act-span-raw";
  raw.append(ownedElement("summary", "查看原始事件"));
  raw.append(element("pre", JSON.stringify(span.event.payload || {}, null, 2)));
  box.append(raw);
}
function renderStory(detail) {
  refreshStoryText();
  const host = el("activity-story");
  const model = buildModel(detail);
  if (!model.spans.length) { host.hidden = true; return; }
  host.hidden = false;
  const result = detail.result || (detail.history || []).slice(-1)[0] || {};
  const runtimeKey = detail.kind === "run" ? "单次检索 · {mode}"
    : (model.imported ? "会话 · 继承 Search" : "会话 · 有状态执行");
  const runtimeParams = {mode: (result.mode || "run").toUpperCase()};
  ownedTemplate(el("activity-story-runtime"), runtimeKey, runtimeParams);
  const modelCalls = model.spans.filter(span => span.event.payload?.category === "model" || String(span.event.payload?.tool || "").startsWith("model.")).length + model.importedModels;
  const sourceCalls = model.spans.filter(span => span.event.payload?.category === "source").length + model.importedSource;
  const errors = model.spans.filter(span => span.type === "error" || span.event.payload?.status === "error").length + model.importedErrors;
  const tools = model.spans.filter(span => span.type === "tool").length + model.importedTools;
  const summary = el("activity-trace-summary");
  summary.replaceChildren(
    ownedTemplate(traceChip("", "runtime"), runtimeKey, runtimeParams),
    ownedTemplate(traceChip(""), "{count} 个工具操作", {count: tools}),
    ownedTemplate(traceChip("", modelCalls ? "tool" : ""), "{count} 次模型调用", {count: modelCalls}),
    ownedTemplate(traceChip("", sourceCalls ? "tool" : ""), "{count} 次来源请求", {count: sourceCalls}),
    ownedTemplate(traceChip("", errors ? "error" : ""), "{count} 个错误", {count: errors}),
  );
  const graph = el("activity-graph");
  graph.replaceChildren(...nodeState(model).map(([label, observed]) =>
    ownedElement("span", label, "act-graph-node" + (observed ? " observed" : ""))));
  const tree = el("activity-trace-tree");
  const inspector = el("activity-span-inspector");
  const recordKey = JSON.stringify([state.selectedKind, state.selected]);
  const sameRecord = tree.dataset.recordKey === recordKey;
  const selectedKey = sameRecord ? tree.querySelector('.trace-row.active')?.dataset.spanKey : null;
  const focusedKey = sameRecord && tree.contains(document.activeElement) ? document.activeElement.dataset.spanKey : null;
  const raw = sameRecord ? inspector.querySelector('.act-span-raw') : null;
  const rawOpen = Boolean(raw?.open);
  const rawFocused = raw?.querySelector('summary') === document.activeElement;
  tree.dataset.recordKey = recordKey;
  tree.replaceChildren();
  const root = element("li", "", "act-trace-root");
  root.append(element("span", "RUN", "act-trace-type import"),
              ownedElement("b", detail.kind === "run" ? "检索执行" : "会话执行"),
              element("span", detail.kind === "run" ? (result.method || "A") : `${(detail.session && detail.session.turns) || 1} 轮`,
                      "act-trace-root-meta"));
  tree.append(root);
  for (const [key, label, note] of GROUPS) {
    const items = model.groups[key] || [];
    if (!items.length) continue;
    const group = element("li", "", "act-trace-group");
    group.append(ownedElement("span", label, "act-trace-group-label"),
                 ownedElement("span", note, "act-trace-group-note"),
                 element("span", String(items.length), "act-trace-group-count"));
    tree.append(group);
    for (const span of items) {
      const li = element("li", "", "act-trace-item");
      const button = element("button", "", "trace-row " + span.type);
      button.type = "button";
      button.dataset.action = span.event.action;
      button.dataset.spanKey = JSON.stringify([span.event.seq ?? span.index, span.event.action, span.index]);
      button.append(
        element("span", String(span.event.seq ?? span.index + 1).padStart(2, "0"), "trace-seq"),
        element("span", TYPE_LABELS[span.type] || span.type, "act-trace-type " + span.type),
        element("span", "", "trace-copy"),
        element("span", span.time || "", "trace-time"),
        ownedElement("span", span.type === "error" || span.event.payload?.status === "error" ? "出现错误"
          : span.type === "tool" ? (span.event._operation?.running ? "进行中" : "完成") : "已记录",
          "trace-status " + (span.type === "error" ? "error" : span.type === "tool" ? "ok" : "")),
      );
      const copy = button.querySelector(".trace-copy");
      copy.append(TRACE_LABELS[span.event.action] ? ownedElement("b", TRACE_LABELS[span.event.action]) : element("b", span.title));
      const small = element("small", span.summary);
      copy.append(small);
      button.addEventListener("click", () => selectSpan(span, button));
      li.append(button); tree.append(li);
    }
  }
  const rows = [...tree.querySelectorAll('.trace-row')];
  const preferred = rows.find(row => row.dataset.spanKey === selectedKey) ||
    tree.querySelector('.trace-row[data-action="tool_operation"]') ||
    tree.querySelector('.trace-row[data-action="decision"]') ||
    tree.querySelector(".trace-row");
  if (preferred) {
    preferred.click();
    if (preferred.dataset.spanKey === selectedKey && rawOpen) {
      const replacement = inspector.querySelector('.act-span-raw');
      if (replacement) {
        replacement.open = true;
        if (rawFocused) replacement.querySelector('summary').focus({preventScroll: true});
      }
    }
    if (focusedKey) rows.find(row => row.dataset.spanKey === focusedKey)?.focus({preventScroll: true});
  }
  owned(el("activity-story-foot"), "工具开始与结束按 span_id 合并为一次操作。模型调用显示实际阶段，来源请求显示来源与耗时；展开原始事件可核对完整载荷。");
}
