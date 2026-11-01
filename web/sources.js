"use strict";

const el = id => document.getElementById(id);
const state = {
  sources: [], routing: [], cooling: [], filter: "", selected: null, skills: [], catalog: null,
  lastTest: {}, probeVersion: 0, probeRequests: {}, busy: false, editorOpen: false, editorDirty: false, editorSaving: false,
  luneQuota: null, quotaVersion: 0, quotaBusy: false,
};

// Translate only frontend-owned copy; backend values remain untouched.
const t = text => window.SGI18n.t(text);
const tr = (text, ...args) => t(text).replace(/\{(\d+)\}/g, (match, index) => index < args.length ? String(args[index]) : match);

const SOURCE_COPY = {
  arxiv: "预印本摘要与 PDF。适合发现最新工作，但不代表会议录用。",
  openalex: "学术元数据与出版信息，可用于会议、期刊和年份核对。",
  semantic_scholar: "独立学术索引，提供摘要、出版信息与开放获取链接。",
  dblp: "书目元数据为主，通常不含摘要。访问受上游防护影响时会明确失败。",
  openreview: "ICLR / NeurIPS / ICML 等会议的官方评审与录用记录。",
  acl_anthology: "ACL / EMNLP / NAACL 等 NLP 会议的官方论文页。",
  deepseek: "网页发现来源，只提供 snippet；不会自动升级为摘要或全文证据。",
  lune: "A/A* 会议论文库与 MCP 工具；书目信息不等于录用或展示类别证明。",
};
const CAP_LABELS = {
  abstract: "摘要", publication: "出版信息", presentation: "展示类别",
  fulltext: "全文", related: "相关论文", citations: "引用", skills: "MCP 工具",
};
const FILTER_LABELS = {
  venue: "会议", year: "年份", publication_type: "出版类型",
  accepted_only: "录用状态", presentation: "展示类别",
};
const STATUS_LABELS = { ok: "可用", empty: "可用 · 空结果", error: "有错误", cache: "缓存命中", unknown: "未检测" };
const CODE_LABELS = {
  bot_challenge: "网站要求浏览器验证", rate_limited: "请求过多，已进入冷却",
  timeout: "请求超时", dns_error: "DNS 解析失败", connection_error: "连接失败",
  transport: "网络传输失败", auth: "认证失败", quota_exhausted: "额度不足",
  quota_no_snapshot: "尚未确认额度快照", quota_snapshot_expired: "额度快照已过期",
  quota_pending_reserved: "存在未完成的额度预留", quota_snapshot_corrupt: "额度账本损坏",
  quota_ledger_unavailable: "额度账本不可用", quota_store_closed: "额度账本已关闭",
  invalid_response: "响应格式异常", source_error: "来源请求失败",
};
const CODE_DETAILS = {
  bot_challenge: "上游站点返回了反机器人验证页；这是外部限制，换网络出口或停用该来源。再次测试会解除当前冷却。",
  rate_limited: "匿名或当前额度触发限流；配置来源 API key 可降低概率，冷却结束后会再试。",
  timeout: "请求超时；上游可能已经产生调用，不会伪造成功。",
  dns_error: "本机无法解析上游域名，请检查网络或代理。",
  connection_error: "连接上游失败，请检查网络或代理。",
  transport: "网络传输中断，未拿到完整响应。",
  auth: "来源凭据无效或已过期，请在配置面板更新。",
  quota_exhausted: "本机确认的额度已用尽；账户实际余额需另行核对。",
  quota_no_snapshot: "未确认账户剩余额度，已在发送前暂停；核对其他客户端后确认快照。",
  quota_snapshot_expired: "本机快照已过期，须重新核对账户额度；不会自动假设重置。",
  quota_pending_reserved: "仍有未完成的预留；仅在确认所属进程已结束时可标记未知。",
  quota_snapshot_corrupt: "本机额度账本无法读取；请求已暂停，请保留账本以便排查。",
  quota_ledger_unavailable: "本机额度账本暂不可用，请求已暂停。",
  quota_store_closed: "本机额度账本已关闭，请求已暂停。" ,
  invalid_response: "上游返回了无法解析的内容，未计为成功。",
};
const HEALTH_ICONS = { ok: "✓", empty: "○", error: "!", cache: "↺", unknown: "?" };

function formatTime(value) {
  if (!value) return t("未知");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}
function duration(ms) {
  if (ms == null || Number.isNaN(Number(ms))) return t("未知");
  const value = Number(ms);
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value}ms`;
}
function healthCode(source) {
  return (source.health || {}).last_code || (source.health || {}).code || "";
}
function statusOf(source) {
  if (!source.enabled) return "off";
  if (!source.configured) return "setup";
  const health = source.health || {};
  if (health.status === "error" || health.last_status === "error") return "error";
  if (["ok", "empty", "cache"].includes(health.status || health.last_status)) return "ok";
  return "unknown";
}
function statusLabel(source) {
  return { off: t("已停用"), setup: t("待配置"), error: t("需处理"), ok: t("正常"), unknown: t("未检测") }[statusOf(source)];
}
function healthText(source) {
  const health = source.health || {};
  const status = health.status || health.last_status;
  if (!status) return t("尚未测试");
  const base = STATUS_LABELS[status] ? t(STATUS_LABELS[status]) : status;
  const code = healthCode(source);
  return code && status === "error" ? `${base} · ${(CODE_LABELS[code] ? t(CODE_LABELS[code]) : code)}` : base;
}
function observedText(source) {
  const health = source.health || {};
  if (!health.last_at) return t("尚未观测");
  return `${healthText(source)} · ${formatTime(health.last_at)}`;
}
function setupText(source) {
  if (!source.configured) return source.setup_hint || t("请在此页配置来源凭据。");
  if (source.key_recommended && !source.auth_configured) return t("匿名访问可用，但可能被限流；建议配置来源 API key。");
  if (source.auth_configured) return t("来源凭据已配置。");
  return "";
}
function quotaText(source) {
  if (source.name === "lune") {
    const local = state.luneQuota;
    if (!local?.snapshot_present) return t("待确认");
    if (local.expired) return t("快照已过期");
    return tr(t("本机快照 {0} 次"), local.local_available);
  }
  const quota = source.quota || {};
  if (quota.remaining != null) return tr(t("剩余 {0}"), quota.remaining);
  if (quota.used != null && quota.limit != null) return `${quota.used} / ${quota.limit}`;
  if (quota.requests != null) return tr(t("已用 {0}"), quota.requests);
  return "供应商未返回";
}
function concurrencyText(source) {
  const value = source.concurrency || {};
  if (value.in_flight != null && value.limit != null) return tr(t("{0} / {1} 在途"), value.in_flight, value.limit);
  return t("未知");
}
function capabilities(source) {
  const value = source.capabilities || {};
  const result = [];
  for (const [key, label] of Object.entries(CAP_LABELS)) {
    if (value[key] === true) result.push({ id: `cap:${key}`, filterKey: label, label: t(label), kind: "native" });
  }
  const filters = value.filters || {};
  for (const [key, mode] of Object.entries(filters)) {
    if (mode === "native") result.push({ id: `filter:${key}:${mode}`, filterKey: FILTER_LABELS[key] || key, label: tr("{0}原生", FILTER_LABELS[key] ? t(FILTER_LABELS[key]) : key), kind: "native" });
    else if (mode === "client") result.push({ id: `filter:${key}:${mode}`, filterKey: FILTER_LABELS[key] || key, label: tr("{0}本地筛选", FILTER_LABELS[key] ? t(FILTER_LABELS[key]) : key), kind: "" });
  }
  return result;
}
function allCapabilityKeys(source) {
  return capabilities(source).map(item => item.id);
}
async function api(path, options = {}, timeout = 90000) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) }, signal: AbortSignal.timeout(timeout) });
  } catch (error) {
    if (error?.name === "TimeoutError") throw new Error(t("请求超时。"));
    throw new Error(t("无法连接本地服务。"));
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.error?.message || data.error || data.message || tr(t("请求失败（{0}）"), response.status);
    const failure = new Error(message);
    if (data.error?.remote_outcome === "completed" && data.error?.retry_safe === false &&
        data.partial_result && typeof data.partial_result === "object") {
      failure.partialResult = data.partial_result;
    }
    throw failure;
  }
  return data;
}
function setStatus(node, message, kind = "") {
  if (!node) return;
  delete node.dataset.i18n; delete node.dataset.i18nParams;
  node.textContent = message;
  node.dataset.kind = kind;
}
function setStatusKey(node, key, kind = '') {
  setStatus(node, '', kind);
  window.SGI18n.bind(node, key);
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function uiElement(tag, key, className, params = {}) {
  return window.SGI18n.bind(element(tag, '', className), key, params);
}
function setOwnedCopy(node, key, params = {}) {
  node.replaceChildren();
  return window.SGI18n.bind(node, key, params);
}
function chip(text, kind = "") {
  return element("span", text, "cap-chip" + (kind ? " " + kind : ""));
}
function setLastTest(name, value) {
  state.lastTest[name] = value;
}
function renderSummary() {
  const host = el("source-summary");
  const total = state.sources.length;
  const ready = state.sources.filter(source => statusOf(source) === "ok").length;
  const attention = state.sources.filter(source => ["error", "setup"].includes(statusOf(source))).length;
  const inflight = state.sources.reduce((sum, source) => sum + Number(source.concurrency?.in_flight || 0), 0);
  const cells = [
    [t("来源"), total, t("已登记来源")],
    [t("可用"), ready, t("最近一次有成功或空结果")],
    [t("需处理"), attention, t("凭据缺失或最近失败")],
    [t("在途"), inflight, t("当前实际请求")],
  ];
  host.replaceChildren(...cells.map(([label, value, note]) => {
    const cell = element("div", "", "summary-cell");
    cell.append(element("span", label), element("b", String(value)), element("small", note));
    return cell;
  }));
  const route = state.routing.length ? state.routing.map(name => sourceLabel(name)).join(" → ") : t("暂无可用来源");
  const cooling = state.cooling.length ? tr(t(" 冷却中：{0}。"), state.cooling.map(name => sourceLabel(name)).join("、")) : "";
  el("routing-note").textContent = tr(t("multi 当前顺序：{0}。冷却、停用或未配置的来源会被跳过；显式选择单源时不会自动扩展。{1}"), route, cooling);
}
function sourceLabel(name) {
  return (state.sources.find(source => source.name === name) || {}).label || name;
}
function renderFilters() {
  const host = el("capability-filter");
  const focused = host.contains(document.activeElement) ? document.activeElement.dataset.filterId : null;
  const present = new Map(state.sources.flatMap(capabilities).map(item => [item.id, item.label]));
  const chosen = state.filter;
  const options = [["", t("全部")], ...present];
  host.replaceChildren(...options.map(([value, label]) => {
    const button = element("button", label, "filter-chip" + (value === chosen ? " active" : ""));
    button.type = "button";
    button.dataset.filter = label;
    button.dataset.filterId = value;
    button.setAttribute("aria-pressed", String(value === chosen));
    button.addEventListener("click", () => {
      state.filter = value;
      renderFilters(); renderSources();
    });
    return button;
  }));
  if (focused !== null) {
    const replacement = [...host.querySelectorAll("button")].find(button => button.dataset.filterId === focused);
    (replacement || host.querySelector("button"))?.focus({ preventScroll: true });
  }
}
function renderSources() {
  const host = el("sources-list");
  const visible = state.sources.filter(source => !state.filter || allCapabilityKeys(source).includes(state.filter));
  const filterLabel = state.sources.flatMap(capabilities).find(item => item.id === state.filter)?.label || state.filter;
  el("source-count").textContent = tr(t("{0} / {1} 个来源{2}"), visible.length, state.sources.length, state.filter ? " · " + filterLabel : "");
  if (!visible.length) {
    host.replaceChildren(element("p", state.sources.length ? t("没有符合此能力筛选的来源。") : t("服务端尚未返回来源配置。"), "empty-state"));
    return;
  }
  host.replaceChildren(...visible.map(renderSource));
}
function renderSource(source) {
  const article = element("article", "", "source-row");
  article.dataset.name = source.name;
  const status = statusOf(source);
  article.dataset.state = status;
  const top = element("div", "", "source-card-top");
  top.append(element("span", "", "source-dot"));
  const title = element("div", "", "source-title");
  title.append(element("h3", source.label || source.name), element("p", source.name || ""));
  const badge = element("span", `${HEALTH_ICONS[status] || "?"} ${statusLabel(source)}`, "source-state");
  top.append(title, badge);
  const description = element("p", SOURCE_COPY[source.name] ? t(SOURCE_COPY[source.name]) : t("来源说明未知。"), "source-description");
  const caps = element("div", "", "source-capabilities");
  const capabilityItems = capabilities(source);
  if (capabilityItems.length) capabilityItems.forEach(item => caps.append(chip(item.label, item.kind)));
  else caps.append(chip(t("能力未知")));
  const stats = element("dl", "", "source-stats");
  const fact = (label, value) => {
    const box = element("div");
    box.append(element("dt", label), element("dd", value));
    return box;
  };
  stats.append(
    fact(t("最近观测"), observedText(source)),
    fact(t("响应"), duration(source.health?.elapsed_ms)),
    fact(t("并发"), concurrencyText(source)),
    fact(t("来源额度"), quotaText(source)),
    fact(t("优先级"), String(source.priority ?? t("未知"))),
    fact(t("凭据"), source.auth_configured ? t("已配置") : source.requires_key ? t("需要独立 key") : t("无需")),
  );
  article.append(top, description, caps, stats);
  const setup = setupText(source);
  if (setup) article.append(element("p", setup, "source-hint"));
  if (status === "error") {
    const code = healthCode(source);
    const detail = source.name === "arxiv" && code === "rate_limited"
      ? t("arXiv API 返回 429 / Rate exceeded。它不是配置错误；通常需要等待 15–60 分钟、切换网络出口，或在 Sources 里点“测试连接”重新探测。")
      : (CODE_DETAILS[code] ? t(CODE_DETAILS[code]) : null) || source.health?.error || source.health?.last_error || t("最近调用失败。");
    article.append(element("p", detail, "source-alert"));
  }
  const action = element("div", "", "source-actions");
  const test = uiElement("button", "测试连接", "btn-text");
  test.type = "button";
  test.addEventListener("click", () => testSource(source, test, resultHost));
  const edit = uiElement("button", "配置", "btn-text");
  edit.type = "button";
  edit.dataset.configureSource = "true";
  edit.addEventListener("click", () => openEditor(source.name, edit));
  const switchLabel = element("label", "", "source-switch");
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = source.enabled === true;
  toggle.disabled = state.busy;
  toggle.setAttribute("aria-label", tr(t("启用 {0}"), source.label || source.name));
  toggle.addEventListener("change", () => toggleSource(source, toggle));
  switchLabel.append(toggle, document.createTextNode(source.enabled ? t("已启用") : t("已停用")));
  action.append(test, edit, switchLabel);
  const resultHost = element("div", "", "test-result");
  const previous = state.lastTest[source.name];
  if (previous) {
    resultHost.classList.add(previous.ok ? "ok" : "error");
    resultHost.textContent = previous.text;
  } else resultHost.hidden = true;
  article.append(action, resultHost);
  return article;
}
async function loadSources() {
  const data = await api("/api/sources");
  state.sources = Array.isArray(data.sources) ? data.sources : [];
  state.routing = Array.isArray(data.routing) ? data.routing : [];
  state.cooling = Array.isArray(data.cooling_down) ? data.cooling_down : [];
  renderSummary(); renderFilters(); renderSources();
  el("lune-tools").hidden = !state.sources.some(source => source.name === "lune");
  populateNameSelect();
}
async function testSource(source, button, host) {
  const query = el("probe-query").value.trim();
  const version = state.probeVersion;
  const request = (state.probeRequests[source.name] || 0) + 1;
  state.probeRequests[source.name] = request;
  const current = () => version === state.probeVersion && state.probeRequests[source.name] === request;
  if (!query || query.length > 128) {
    setLastTest(source.name, { ok: false, text: t("测试查询需为 1–128 字符。") });
    renderSources();
    return;
  }
  button.disabled = true;
  button.textContent = t("测试中…");
  host.hidden = false;
  host.className = "test-result";
  host.textContent = t("真实请求进行中，最长约 90 秒…");
  try {
    const data = await api(`/api/sources/${encodeURIComponent(source.name)}/test`, {
      method: "POST", body: JSON.stringify({ query }),
    }, 90000);
    const ok = data.ok === true;
    const detail = data.health || {};
    const text = ok
      ? tr(t("真实调用成功 · {0} · {1} 条结果"), duration(data.duration_ms), detail.returned ?? 0)
      : tr(t("真实调用未成功 · {0}"), (CODE_LABELS[detail.last_code] ? t(CODE_LABELS[detail.last_code]) : detail.last_code) || t("检查失败"));
    if (current()) setLastTest(source.name, { ok, text });
    await loadSources();
  } catch (error) {
    if (current()) {
      setLastTest(source.name, { ok: false, text: tr(t("测试失败：{0}"), error.message) });
      renderSources();
    }
  } finally {
    button.disabled = false;
    button.textContent = t("测试连接");
  }
}
async function toggleSource(source, input) {
  input.disabled = true;
  try {
    await api("/api/sources", { method: "POST", body: JSON.stringify({ name: source.name, enabled: input.checked }) });
    await loadSources();
  } catch (error) {
    input.checked = source.enabled === true;
    setStatus(el("sources-status"), error.message, "error");
  } finally {
    input.disabled = false;
  }
}
function populateNameSelect() {
  const select = el("source-name");
  const current = select.value || state.selected;
  const names = state.sources.map(source => [source.name, source.label || source.name]);
  select.replaceChildren(...names.map(([value, label]) => new Option(label, value)));
  if (current && names.some(([value]) => value === current)) select.value = current;
  updateEditorHints();
}
let editorOpener = null;
let editorOpenerSource = null;
function openEditor(name = null, opener = null) {
  if (state.editorSaving) return;
  if (state.editorOpen && state.editorDirty && !window.confirm(t("当前来源有未保存的修改。切换将放弃这些修改，是否继续？"))) {
    if (state.selected) el("source-name").value = state.selected;
    return;
  }
  const source = state.sources.find(row => row.name === name) || (!name ? state.sources[0] : null);
  if (opener) {
    editorOpener = opener;
    editorOpenerSource = opener.closest(".source-row")?.dataset.name || null;
  }
  state.editorOpen = true;
  state.editorDirty = false;
  state.selected = source?.name || null;
  el("source-editor-panel").hidden = false;
  el("editor-title").textContent = source ? tr(t("配置 · {0}"), source.label || source.name) : t("配置来源");
  populateNameSelect();
  if (source) el("source-name").value = source.name;
  const current = source || {};
  el("source-priority").value = current.priority ?? "100";
  el("source-enabled").checked = current.enabled !== false;
  el("source-key").value = "";
  el("source-endpoint").value = current.endpoint || "";
  updateEditorHints();
  el("source-form-status").textContent = "";
  el("source-name").focus();
}
function updateEditorHints() {
  const name = el("source-name").value;
  const source = state.sources.find(row => row.name === name) || {};
  const keyField = el("source-key-requirement");
  keyField.textContent = source.requires_key ? t("必需") : source.key_recommended ? t("推荐") : t("可选");
  el("source-key-hint").textContent = source.auth_configured
    ? t("密钥已配置；留空保留现有密钥。")
    : source.requires_key
      ? t("该来源需要独立 key；配置后才会进入默认多源。")
      : source.key_recommended
        ? t("未配置也能匿名访问，但更容易触发 429；建议配置。")
        : t("此来源通常不需要密钥。");
  el("endpoint-field").hidden = !["lune", "deepseek"].includes(name);
}
function closeEditor() {
  if (state.editorSaving) return;
  state.editorOpen = false;
  state.editorDirty = false;
  state.selected = null;
  el("source-key").value = "";
  el("source-editor-panel").hidden = true;
  el("source-form-status").textContent = "";
  const replacement = editorOpenerSource && [...el("sources-list").querySelectorAll(".source-row")]
    .find(row => row.dataset.name === editorOpenerSource)?.querySelector("[data-configure-source]");
  const target = editorOpener?.isConnected ? editorOpener : replacement || el("new-source");
  editorOpener = null;
  editorOpenerSource = null;
  target.focus();
}
function setEditorSaving(saving) {
  state.editorSaving = saving;
  el("source-form").querySelectorAll("input, select, button").forEach(node => { node.disabled = saving; });
  el("close-editor").disabled = saving;
}
function bodyForSave() {
  const name = el("source-name").value;
  const body = { name, enabled: el("source-enabled").checked, priority: Number(el("source-priority").value) };
  if (!Number.isInteger(body.priority) || body.priority < 0 || body.priority > 100) throw new Error(t("优先级需要是 0–100 的整数。"));
  const key = el("source-key").value;
  if (key.trim()) body.api_key = key.trim();
  if (!el("endpoint-field").hidden && el("source-endpoint").value.trim()) body.endpoint = el("source-endpoint").value.trim();
  return body;
}
function empty(host, message) {
  host.replaceChildren(element("p", message, "empty-state"));
}
function catalogRows(data) {
  const result = data.result || data.catalog || data;
  return Array.isArray(result.conferences) ? result.conferences : [];
}
async function loadCatalog() {
  const host = el("catalog-content");
  empty(host, t("正在读取 Lune 会议目录…"));
  try {
    const data = await api("/api/sources/lune/catalog", {}, 90000);
    state.catalog = data;
    const rows = catalogRows(data);
    host.replaceChildren();
    if (!rows.length) { empty(host, t("目录接口已响应，但没有可识别的会议记录。")); return; }
    const summary = element("div", "", "catalog-summary");
    const papers = rows.reduce((sum, row) => sum + Number(row.paper_count || 0), 0);
    summary.append(chip(tr(t("{0} 个会议"), rows.length)), chip(tr(t("{0} 篇索引论文"), papers.toLocaleString())));
    host.append(summary);
    const groups = new Map();
    rows.forEach(row => {
      const key = row.category || t("其他");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    });
    for (const [category, items] of groups) {
      const group = element("div", "", "catalog-group");
      group.append(element("h3", category));
      const grid = element("div", "", "catalog-grid");
      items.sort((a, b) => String(a.short_name).localeCompare(String(b.short_name))).forEach(row => {
        const item = element("div", "", "catalog-item");
        item.append(element("b", row.short_name || row.full_name || t("未命名会议")),
                    element("small", `${row.full_name || ""}${row.years?.length ? " · " + row.years.slice(0, 3).join(", ") : ""}${row.paper_count != null ? " · " + tr("{0} 篇", Number(row.paper_count).toLocaleString()) : ""}`));
        grid.append(item);
      });
      group.append(grid); host.append(group);
    }
  } catch (error) {
    if (error.partialResult) {
      renderOperationOutput(host, error.partialResult);
      host.prepend(element("p", t("远端已完成，本地审计未确认；不要自动重试。"), "source-hint"));
    } else {
      empty(host, tr(t("目录读取失败：{0}"), error.message));
    }
  }
}
function skillName(skill, index) {
  return skill.name || skill.id || `skill-${index + 1}`;
}
function skillParams(skill) {
  const raw = skill.parameters || skill.params || skill.input_schema?.properties || {};
  const parameters = {};
  for (const [key, value] of Object.entries(raw)) {
    if (typeof value === "string") {
      const type = ["limit", "offset", "year"].includes(value) ? "integer" : value === "texts" ? "array" : "string";
      parameters[key] = { type, required: Array.isArray(skill.required) && skill.required.includes(key) };
    } else if (value && typeof value === "object") {
      parameters[key] = value;
    }
  }
  return parameters;
}
function renderSkills() {
  const host = el("skills-list");
  if (!state.skills.length) { empty(host, t("服务端未返回可用工具。工具状态未知，不代表来源不可用。")); return; }
  host.replaceChildren(...state.skills.map((raw, index) => {
    const skill = raw && typeof raw === "object" ? raw : {};
    const name = skillName(skill, index);
    const details = document.createElement("details");
    details.className = "skill-operation";
    const summary = document.createElement("summary");
    summary.addEventListener("click", () => {
      if (!details.open) for (const other of host.querySelectorAll(':scope > details.skill-operation')) {
        if (other !== details) other.open = false;
      }
    });
    const toolKeys = {search_papers: '检索论文', search_related_papers: '寻找相似论文', get_paper_fulltext: '读取论文全文', get_paper_citations: '追踪引用', list_conferences: '查看会议覆盖'};
    summary.append(toolKeys[name] ? uiElement('span', toolKeys[name]) : element('span', name), element('span', name));
    details.append(summary, element("p", skill.description || t("按需调用 Lune 的 MCP 工具。"), ""));
    const form = document.createElement("form");
    form.className = "operation-form";
    const params = skillParams(skill);
    for (const [key, param] of Object.entries(params)) {
      const label = element("label", "", "field");
      const caption = element("span", `${param.title || key}${param.required ? t(" · 必填") : ""}`);
      const input = document.createElement("input");
      input.name = key; input.maxLength = 4000; input.required = Boolean(param.required);
      input.placeholder = param.description || (key === "paper_id" ? t("例如 lune UUID 或 DOI") : "");
      if (param.type === "integer" || param.type === "number") { input.type = "number"; input.min = "1"; input.max = key === "year" ? "2100" : "50"; }
      if (!param.description && key === 'paper_id') input.dataset.i18nPlaceholder = '例如 lune UUID 或 DOI';
      if (param.type === "array") { input.placeholder = t("多个值用逗号分隔"); input.dataset.i18nPlaceholder = '多个值用逗号分隔'; }
      label.append(caption, input); form.append(label);
    }
    const actions = element("div", "", "operation-actions");
    const submit = uiElement("button", "执行", "btn btn-inline");
    submit.type = "submit";
    actions.append(submit); form.append(actions);
    const output = element("div", "", "operation-output");
    output.hidden = true;
    output.dataset.generation = "0";
    const invalidate = () => {
      output.dataset.generation = String(Number(output.dataset.generation) + 1);
      if (!output.hidden) setOwnedCopy(output, '参数已更改，上次结果不再显示；请重新执行。');
    };
    form.addEventListener("input", invalidate);
    form.addEventListener("change", invalidate);
    form.addEventListener("submit", event => runSkill(event, name, form, output, submit));
    details.append(form, output); return details;
  }));
}
async function runSkill(event, name, form, output, submit) {
  event.preventDefault();
  if (submit.disabled) return;
  const generation = output.dataset.generation;
  const skill = state.skills.find(row => skillName(row) === name) || {};
  const params = skillParams(skill);
  const args = {};
  new FormData(form).forEach((value, key) => {
    const text = String(value).trim();
    if (!text) return;
    args[key] = params[key]?.type === "integer" ? Number(text) : params[key]?.type === "array" ? text.split(",").map(item => item.trim()).filter(Boolean) : text;
  });
  submit.disabled = true; setOwnedCopy(submit, '执行中…');
  output.hidden = false; setOwnedCopy(output, '真实 MCP 调用进行中，最长约 90 秒…');
  try {
    const data = await api(`/api/sources/lune/skills/${encodeURIComponent(name)}`, { method: "POST", body: JSON.stringify({ arguments: args }) }, 90000);
    if (output.dataset.generation === generation) renderOperationOutput(output, data);
  } catch (error) {
    if (output.dataset.generation === generation) {
      if (error.partialResult) {
        renderOperationOutput(output, error.partialResult);
        output.prepend(element("p", t("远端已完成，本地审计未确认；不要自动重试。"), "source-hint"));
      } else {
        setOwnedCopy(output, '操作失败：{0}', {0: error.message});
      }
    }
  } finally { submit.disabled = false; setOwnedCopy(submit, '执行'); }
}
function renderOperationOutput(host, data) {
  delete host.dataset.i18n; delete host.dataset.i18nParams;
  const result = data.result || data;
  host.replaceChildren();
  if (Array.isArray(result.papers) || Array.isArray(result.results)) {
    const papers = result.papers || result.results;
    const list = element("ul", "", "result-list");
    papers.slice(0, 8).filter(paper => paper && typeof paper === "object").forEach(paper => {
      const item = document.createElement("li");
      item.append(element("b", paper.title || paper.paper_id || t("未命名论文")),
                  element("small", [paper.year, paper.venue, paper.doi].filter(Boolean).join(" · ") || paper.paper_id || ""));
      list.append(item);
    });
    host.append(element("p", tr(t("返回 {0} 篇"), papers.length), "source-hint"), list);
  } else if (typeof result.content === "string") {
    const preview = element("p", result.content.slice(0, 1600), "fulltext-preview");
    host.append(uiElement("p", "全文预览（前 1600 字符）", "source-hint"), preview);
    const details = document.createElement("details");
    details.append(uiElement("summary", "展开完整返回"), element("pre", JSON.stringify(data, null, 2), "operation-output"));
    host.append(details);
  } else {
    host.append(uiElement("p", "操作完成。完整响应如下，可展开查看。", "source-hint"));
    const pre = element("pre", JSON.stringify(data, null, 2));
    host.append(pre);
  }
}
async function loadSkills() {
  try {
    const data = await api("/api/sources/lune/skills");
    const raw = data.skills || data;
    state.skills = Array.isArray(raw) ? raw.filter(item => item && typeof item === "object") : [];
    renderSkills();
  } catch (error) {
    empty(el("skills-list"), tr(t("工具读取失败：{0}"), error.message));
  }
}
function renderLuneQuota(quota) {
  state.luneQuota = quota;
  if (state.sources.length) renderSources();
  const status = el("lune-quota-status");
  if (!quota?.snapshot_present) {
    setStatusKey(status, "尚未确认额度快照，Lune 请求已暂停。", "error");
  } else if (quota.expired) {
    setStatusKey(status, "额度快照已过期，Lune 请求已暂停。", "error");
  } else {
    setStatus(status, tr(t("本机可用 {0} 次 · 快照有效至 {1}"), quota.local_available, formatTime(quota.expires_at * 1000)), "ok");
  }
  const host = el("lune-quota-pending");
  const pending = Array.isArray(quota?.pending) ? quota.pending : [];
  host.replaceChildren(...pending.map(item => {
    const row = element("div", "", "lune-quota-pending-row");
    row.append(element("span", tr(t("未确认结果 · {0} · {1}"), item.method, formatTime(item.created_at * 1000))));
    const button = uiElement("button", "标记为未知", "btn-text");
    button.type = "button";
    button.disabled = state.quotaBusy;
    button.addEventListener("click", async () => {
      if (state.quotaBusy || !window.confirm(t("请确认该请求已不再运行。标记为未知不会返还额度，是否继续？"))) return;
      state.quotaBusy = true;
      button.disabled = true;
      const version = ++state.quotaVersion;
      let updated = null;
      try {
        updated = await api("/api/sources/lune/quota/reconcile", {
          method: "POST", body: JSON.stringify({ id: item.id, acknowledged: true }),
        });
      } catch (error) {
        if (version === state.quotaVersion) setStatus(status, error.message, "error");
      } finally {
        state.quotaBusy = false;
        if (version === state.quotaVersion && updated) renderLuneQuota(updated);
        else button.disabled = false;
      }
    });
    row.append(button);
    return row;
  }));
}
async function loadLuneQuota() {
  const version = ++state.quotaVersion;
  try {
    const quota = await api("/api/sources/lune/quota");
    if (version === state.quotaVersion) renderLuneQuota(quota);
  } catch (error) {
    if (version === state.quotaVersion) setStatus(el("lune-quota-status"), tr(t("读取额度失败：{0}"), error.message), "error");
    throw error;
  }
}
el("lune-quota-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.quotaBusy) return;
  const remainingText = el("lune-quota-remaining").value;
  const remaining = Number(remainingText);
  const expiry = new Date(el("lune-quota-expiry").value).getTime() / 1000;
  if (!remainingText || !Number.isInteger(remaining) || remaining < 0 || remaining > 300 ||
      !Number.isFinite(expiry) || expiry <= Date.now() / 1000 || !el("lune-quota-ack").checked) {
    setStatusKey(el("lune-quota-status"), "请核对剩余次数、有效时间和确认选项。", "error");
    return;
  }
  const button = event.currentTarget.querySelector('button[type="submit"]');
  state.quotaBusy = true;
  button.disabled = true;
  const version = ++state.quotaVersion;
  try {
    const quota = await api("/api/sources/lune/quota", {
      method: "POST", body: JSON.stringify({ remaining, expires_at: expiry, acknowledged: true }),
    });
    if (version === state.quotaVersion) {
      el("lune-quota-ack").checked = false;
      renderLuneQuota(quota);
    }
  } catch (error) {
    if (version === state.quotaVersion) setStatus(el("lune-quota-status"), tr(t("确认失败：{0}"), error.message), "error");
  } finally {
    state.quotaBusy = false;
    button.disabled = false;
  }
});
async function init() {
  const status = el("sources-status");
  setStatusKey(status, "正在读取来源配置…");
  const results = await Promise.allSettled([loadSources(), loadSkills(), loadLuneQuota()]);
  const failed = results.filter(result => result.status === "rejected");
  if (failed.length) setStatus(status, tr(t("部分读取失败：{0}"), failed.map(result => result.reason.message).join("；")), "error");
  else setStatusKey(status, "来源状态与工具清单已读取。测试和目录请求由你主动发起。", "ok");
}
el("refresh-sources").addEventListener("click", async () => {
  setStatusKey(el("sources-status"), "正在刷新来源状态…");
  try { await Promise.all([loadSources(), loadLuneQuota()]); setStatusKey(el("sources-status"), "来源状态已刷新。", "ok"); }
  catch (error) { setStatus(el("sources-status"), error.message, "error"); }
});
el("probe-query").addEventListener("input", () => {
  state.probeVersion += 1; state.lastTest = {};
  document.querySelectorAll(".test-result").forEach(node => { node.hidden = true; node.textContent = ""; });
});
el("test-visible").addEventListener("click", async () => {
  const version = state.probeVersion;
  const visible = state.sources.filter(source => !state.filter || allCapabilityKeys(source).includes(state.filter));
  for (const source of visible) {
    if (version !== state.probeVersion) break;
    const card = document.querySelector(`.source-row[data-name="${CSS.escape(source.name)}"]`);
    const button = card?.querySelector(".source-actions .btn-text");
    if (button) await testSource(source, button, card.querySelector(".test-result"));
  }
});
el("new-source").addEventListener("click", event => openEditor(null, event.currentTarget));
el("close-editor").addEventListener("click", closeEditor);
el("cancel-edit").addEventListener("click", closeEditor);
el("source-name").addEventListener("change", () => openEditor(el("source-name").value));
el("source-form").addEventListener("input", event => {
  if (event.target.id !== "source-name") state.editorDirty = true;
});
el("source-form").addEventListener("change", event => {
  if (event.target.id !== "source-name") state.editorDirty = true;
});
el("source-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.editorSaving) return;
  let body;
  try { body = bodyForSave(); }
  catch (error) { setStatus(el("source-form-status"), error.message, "error"); return; }
  setEditorSaving(true);
  try {
    await api("/api/sources", { method: "POST", body: JSON.stringify(body) });
    el("source-key").value = "";
    setStatusKey(el("source-form-status"), "配置已保存。", "ok");
    await loadSources();
    state.editorDirty = false;
    setEditorSaving(false);
    closeEditor();
  } catch (error) {
    setStatus(el("source-form-status"), tr(t("保存失败：{0}"), error.message), "error");
  } finally { setEditorSaving(false); }
});
el("reload-catalog").addEventListener("click", loadCatalog);
// Apply a freshly rendered view's text to the existing source row. This is
// intentionally scoped to a row rendered from the same source snapshot; input
// values, pending buttons, listeners, focus and server-owned data stay intact.
function syncSourceCopy(current, translated) {
  if (!current || !translated || current.nodeType !== translated.nodeType) return;
  if (current.nodeType === Node.TEXT_NODE) {
    if (current.nodeValue !== translated.nodeValue) current.nodeValue = translated.nodeValue;
    return;
  }
  if (current.nodeType !== Node.ELEMENT_NODE || current.tagName !== translated.tagName) return;
  if (current.matches('input, select, textarea, button:disabled, .test-result')) return;
  for (const attr of ['aria-label', 'title']) {
    if (translated.hasAttribute(attr)) current.setAttribute(attr, translated.getAttribute(attr));
  }
  if (current.childNodes.length !== translated.childNodes.length) return;
  [...current.childNodes].forEach((node, index) => syncSourceCopy(node, translated.childNodes[index]));
}
window.addEventListener("sg:language-change", () => {
  renderSummary();
  // Stable filter IDs preserve the selected capability across languages.
  const labels = new Map(state.sources.flatMap(capabilities).map(item => [item.id, item.label]));
  for (const button of el('capability-filter').querySelectorAll('button')) {
    const label = button.dataset.filterId ? labels.get(button.dataset.filterId) : t('全部');
    if (label) { button.textContent = label; button.dataset.filter = label; }
  }
  for (const row of el('sources-list').querySelectorAll('.source-row')) {
    const source = state.sources.find(item => item.name === row.dataset.name);
    if (source) syncSourceCopy(row, renderSource(source));
  }
  const visible = state.sources.filter(source => !state.filter || allCapabilityKeys(source).includes(state.filter));
  const filterLabel = labels.get(state.filter) || state.filter;
  el('source-count').textContent = tr('{0} / {1} 个来源{2}', visible.length, state.sources.length, state.filter ? ' · ' + filterLabel : '');
  if (state.editorOpen) {
    updateEditorHints();
    const source = state.sources.find(item => item.name === state.selected);
    el('editor-title').textContent = source ? tr('配置 · {0}', source.label || source.name) : t('配置来源');
  }
});
init();
