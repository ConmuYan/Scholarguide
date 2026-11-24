"use strict";

const settingsT = (key, params) => window.SGI18n.t(key, params);

const el = (id) => document.getElementById(id);
const state = { active: "", profiles: [], selected: null, busy: false, dirty: false };
const DRAFT_KEY = "scholarguide-provider-drafts-v1";
const DRAFT_FIELDS = {name:"provider-name", protocol:"provider-protocol", base_url:"provider-url",
  model:"provider-model", input_price:"provider-input-price", output_price:"provider-output-price",
  max_output_tokens:"provider-max-tokens", currency:"provider-currency"};
const keyDrafts = Object.create(null);
let drafts = {};
try { drafts = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "{}"); } catch (_) {}
if (!drafts || typeof drafts !== "object" || Array.isArray(drafts)) drafts = {};

function draftId() { return state.selected || "new"; }
function persistDrafts() {
  try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify(drafts)); } catch (_) {}
}
function syncControls() {
  const readOnly = state.selected === "env";
  el("provider-form").setAttribute("aria-busy", String(state.busy));
  el("provider-form").querySelectorAll("input,select").forEach(node => {
    node.disabled = state.busy || readOnly;
  });
  el("save-provider").disabled = state.busy || readOnly;
  el("test-provider").disabled = state.busy;
  el("delete-provider").disabled = state.busy || !state.selected || readOnly;
  el("activate-provider").disabled = state.busy || !state.selected || state.selected === state.active;
  el("new-provider").disabled = state.busy;
  el("toggle-provider-key").disabled = state.busy || readOnly;
  el("discard-provider-draft").disabled = state.busy;
  el("save-provider").textContent = readOnly ? settingsT("环境配置为只读") : settingsT("保存配置");
  el("provider-list").querySelectorAll("button").forEach(node => { node.disabled = state.busy; });
  syncOAuthControls();
}
function setBusy(busy) { state.busy = busy; syncControls(); }
function rememberDraft() {
  if (state.busy || state.selected === "env") return;
  // A probe or save describes the previous values, not the edited draft.
  el("test-result").hidden = true;
  el("provider-ready-actions").hidden = true;
  setStatus("");
  const draft = {};
  for (const [field, id] of Object.entries(DRAFT_FIELDS)) draft[field] = el(id).value;
  draft.activate = el("provider-activate").checked;
  drafts[draftId()] = draft;
  keyDrafts[draftId()] = el("provider-key").value;
  state.dirty = true;
  persistDrafts();
  el("provider-draft-status").hidden = false;
  el("provider-draft-status").dataset.i18n = "配置草稿已保留在此标签页；密钥不会写入浏览器存储，刷新后需重新输入。";
  el("provider-draft-status").textContent = settingsT(el("provider-draft-status").dataset.i18n);
  el("discard-provider-draft").hidden = false;
}
function clearDraft(id) {
  delete drafts[id]; delete keyDrafts[id]; persistDrafts();
}

function setStatus(text, kind = "") {
  delete el("settings-status").dataset.i18n;
  const node = el("settings-status");
  node.textContent = text;
  node.dataset.kind = kind;
}

function localizedStatus(key, kind = "") {
  setStatus(settingsT(key), kind);
  el("settings-status").dataset.i18n = key;
}

async function api(path, options = {}, timeoutMs = 35000) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (_) {
    throw new Error(settingsT("无法连接本地服务。"));
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || settingsT("请求失败（{status}）", {status: response.status}));
    error.status = response.status;
    throw error;
  }
  return data;
}

function text(value) {
  return value === null || value === undefined || value === "" ? settingsT("未知") : String(value);
}

function badge(textValue, kind = "") {
  const node = document.createElement("span");
  node.className = "badge " + kind;
  node.textContent = textValue;
  return node;
}

/* ---------- ChatGPT sign-in (OpenAI OAuth) ----------
   The browser only polls fixed status text; tokens stay in the OS keychain and
   are never requested, rendered or stored by this page. */
const OAUTH_ID = "openai-oauth";
const OAUTH_POLL_MS = 1500;
const OAUTH_WAIT_MS = 10 * 60 * 1000;
const OAUTH_MESSAGES = {
  "Waiting for the browser sign-in.": "正在等待浏览器完成授权。",
  "ChatGPT sign-in completed. The active model was not changed.": "ChatGPT 授权完成，当前模型未改变。",
  "ChatGPT sign-in did not complete.": "ChatGPT 登录未完成。",
  "OpenAI authorization was declined.": "OpenAI 授权被拒绝。",
  "A ChatGPT sign-in is already in progress.": "已有登录正在进行，请等待这次授权结束。",
  "Authorization succeeded, but the model profile could not be saved; retry setup.": "授权成功，但模型配置未能保存，请重试。",
  "Authorization timed out; try again.": "登录等待超时，请重试。",
  "The reserved openai-oauth profile conflicts with an existing profile; resolve it first.": "保留的 openai-oauth 配置与已有配置冲突；请先处理该配置。",
  "Sign-in accepts only model and use_system_proxy.": "登录请求只接受模型标识与代理选项。",
  "Model must be 1–200 characters.": "模型标识需为 1–200 个字符。",
  "OpenAI login timed out.": "OpenAI 登录超时，请重试。",
  "Could not open a browser for OpenAI login.": "无法打开浏览器完成 OpenAI 登录。",
  "A supported OS keyring is required for OpenAI OAuth; no token was saved.": "需要受支持的系统钥匙串才能保存 OpenAI 令牌。",
  "OpenAI OAuth credentials are unavailable or refresh could not be confirmed; please log in again.": "OpenAI 授权不可用或刷新未确认；请重新登录。",
  "OpenAI identity verification failed; please retry login.": "OpenAI 身份验证失败，请重新登录。",
  "OpenAI OAuth request failed; please retry login.": "OpenAI 登录请求失败，请重试。",
  "OpenAI returned invalid OAuth credentials.": "OpenAI 返回了无效的授权凭据。",
  "OpenAI did not grant the required ChatGPT plan permission.": "OpenAI 未授予所需的 ChatGPT 套餐权限。",
};
// Variables such as the identity-verification category or a deadline message are
// normalized to one localized sentence instead of leaking English text.
const OAUTH_MESSAGE_PREFIXES = [
  ["OpenAI identity verification failed", "OpenAI 身份验证失败，请重新登录。"],
  ["OpenAI OAuth", "OpenAI 登录超时，请重试。"],
];
const oauthDiagnostics = {busy: false, result: null, error: ""};
const oauth = { state: "idle", message: "", profile: null, hasProfile: false, loadError: false,
  localError: "", polling: false, timer: null, token: 0, startedAt: 0, seenRunning: false, slugEdited: false,
  listState: "idle" };

function oauthMessage(message) {
  const raw = String(message || "").trim();
  if (OAUTH_MESSAGES[raw]) return settingsT(OAUTH_MESSAGES[raw]);
  for (const [prefix, key] of OAUTH_MESSAGE_PREFIXES) {
    if (raw.startsWith(prefix)) return settingsT(key);
  }
  return raw;
}

function oauthLive(key, kind = "", params = null) {
  const node = el("oauth-status");
  if (!node) return;
  node.dataset.kind = kind;
  if (params) {
    delete node.dataset.i18n;
    node.textContent = settingsT(key, params);
  } else {
    node.dataset.i18n = key;
    node.textContent = settingsT(key);
  }
}

function syncOAuthControls() {
  if (!el("oauth-login")) return;
  const running = oauth.state === "running";
  el("oauth-model").disabled = state.busy || running || oauthDiagnostics.busy;
  el("oauth-proxy").disabled = state.busy || running || oauthDiagnostics.busy;
  const test = el("oauth-test");
  if (test) {
    test.disabled = state.busy || running || oauthDiagnostics.busy || !oauth.hasProfile;
    test.setAttribute("aria-busy", String(oauthDiagnostics.busy));
    test.textContent = settingsT(oauthDiagnostics.busy ? "正在测试…" : "测试连接与模型");
  }
  const button = el("oauth-login");
  button.disabled = state.busy || running || oauthDiagnostics.busy;
  button.setAttribute("aria-busy", String(running));
  const key = running ? "等待授权…" : "登录 ChatGPT";
  button.dataset.i18n = key;
  button.textContent = settingsT(key);
}

function oauthDiagnosticNode(tag, content, className = "") {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

function renderOAuthDiagnostics() {
  const host = el("oauth-diagnostics");
  if (!host) return;
  host.replaceChildren();
  host.hidden = !oauthDiagnostics.busy && !oauthDiagnostics.result && !oauthDiagnostics.error;
  host.setAttribute("aria-busy", String(oauthDiagnostics.busy));
  if (oauthDiagnostics.busy) {
    host.append(oauthDiagnosticNode("p", settingsT("正在读取模型目录并发送小型测试请求…"), "oauth-test-pending"));
    return;
  }
  if (oauthDiagnostics.error) {
    host.append(oauthDiagnosticNode("p", settingsT("测试未完成，请重试或重新登录。"), "oauth-test-error"));
    return;
  }
  const result = oauthDiagnostics.result;
  if (!result) return;
  const probe = result.probe || {};
  const catalog = result.catalog || {};
  const heading = oauthDiagnosticNode("div", "", "oauth-test-heading");
  heading.append(oauthDiagnosticNode("strong", settingsT(probe.ok ? "模型可用" : "模型测试失败")));
  heading.append(oauthDiagnosticNode("span", probe.model || "", "oauth-tested-model"));
  heading.dataset.kind = probe.ok ? "ok" : "error";
  host.append(heading);
  const summary = oauthDiagnosticNode("dl", "", "oauth-test-summary");
  const metric = (label, value) => {
    const pair = document.createElement("div");
    pair.append(oauthDiagnosticNode("dt", settingsT(label)), oauthDiagnosticNode("dd", value));
    summary.append(pair);
  };
  metric("响应时间", Number.isFinite(probe.latency_ms) ? `${(probe.latency_ms / 1000).toFixed(2)} s` : settingsT("未知"));
  metric("测试输入 token", Number.isSafeInteger(probe.usage?.input_tokens) ? String(probe.usage.input_tokens) : settingsT("未知"));
  metric("测试输出 token", Number.isSafeInteger(probe.usage?.output_tokens) ? String(probe.usage.output_tokens) : settingsT("未知"));
  metric("HTTP 状态", probe.http_status != null ? String(probe.http_status) : settingsT("未知"));
  host.append(summary);
  if (!probe.ok) host.append(oauthDiagnosticNode("p", settingsT("当前模型未通过测试；可选择目录中的其它模型重试，或重新登录。"), "oauth-test-error"));
  const models = Array.isArray(result.models) ? result.models : [];
  if (catalog.ok && models.length) {
    const details = document.createElement("details");
    details.className = "oauth-model-catalog";
    const toggle = oauthDiagnosticNode("summary", settingsT("可用模型（{count}）", {count: models.length}));
    const choices = oauthDiagnosticNode("div", "", "oauth-model-choices");
    for (const model of models) {
      const choice = oauthDiagnosticNode("button", model.display_name || model.slug, "oauth-model-choice");
      choice.type = "button";
      choice.title = model.slug;
      choice.setAttribute("aria-pressed", String(model.slug === probe.model));
      choice.addEventListener("click", () => {
        el("oauth-model").value = model.slug;
        oauth.slugEdited = true;
        for (const peer of choices.children) peer.setAttribute("aria-pressed", String(peer === choice));
        el("oauth-model").focus();
      });
      choices.append(choice);
    }
    details.append(toggle, choices);
    host.append(details);
  } else {
    host.append(oauthDiagnosticNode("p", settingsT(catalog.ok ? "该账户未返回可展示模型。" : "模型目录读取失败；模型测试结果单独显示。"), "field-help"));
  }
  host.append(oauthDiagnosticNode("p", settingsT("以上仅为本次测试用量；账户套餐总用量请在 ChatGPT 查看。"), "field-help"));
}

async function testOAuthAccount() {
  if (oauthDiagnostics.busy || state.busy || oauth.state === "running" || !oauth.hasProfile) return;
  oauthDiagnostics.busy = true;
  oauthDiagnostics.error = "";
  oauthDiagnostics.result = null;
  syncOAuthControls();
  renderOAuthDiagnostics();
  try {
    const model = el("oauth-model").value.trim() || oauth.profile?.model;
    // The diagnostic has a 45 s server budget; allow its terminal reply to arrive.
    const result = await api("/api/providers/oauth/openai/test", {method: "POST", body: JSON.stringify(model ? {model} : {})}, 55000);
    oauthDiagnostics.result = result;
    const options = el("oauth-model-options");
    if (options) {
      options.replaceChildren();
      for (const model of result.models || []) {
        const option = document.createElement("option");
        option.value = model.slug;
        option.label = model.display_name || model.slug;
        options.append(option);
      }
    }
    // A failed reauthorization does not revoke a working saved credential.
    // Only an actual successful probe clears that stale sign-in error.
    if (result.probe?.ok && oauth.state === "failed") {
      oauth.state = "idle";
      oauth.localError = "";
      oauth.message = "";
    }
  } catch (error) {
    oauthDiagnostics.error = "test_failed";
  } finally {
    oauthDiagnostics.busy = false;
    renderOAuth();
    renderOAuthDiagnostics();
  }
}

function renderOAuthConnected(profile) {
  const node = el("oauth-connected");
  if (!node) return;
  node.replaceChildren();
  if (!profile) { node.hidden = true; return; }
  node.hidden = false;
  const model = document.createElement("span");
  model.className = "oauth-connected-model";
  model.textContent = settingsT("已连接模型：{model}", {model: profile.model || settingsT("未知")});
  const note = document.createElement("span");
  note.className = "oauth-connected-note";
  note.textContent = settingsT("已通过 ChatGPT 授权，令牌保存在系统钥匙串。");
  node.append(model, note);
}

function renderOAuth() {
  const card = el("settings-oauth");
  if (!card) return;
  const running = oauth.state === "running";
  const failed = oauth.state === "failed";
  const connected = !running && !failed && Boolean(oauth.profile);
  const view = running ? "running" : failed ? "failed" : connected ? "connected" : "idle";
  card.dataset.oauthState = view;
  const badgeNode = el("oauth-badge");
  const badgeKey = view === "running" ? "正在等待授权" : view === "connected" ? "已连接"
    : view === "failed" ? "登录失败" : "未连接";
  badgeNode.dataset.i18n = badgeKey;
  badgeNode.dataset.kind = view;
  badgeNode.textContent = settingsT(badgeKey);
  const model = el("oauth-model");
  if (!oauth.slugEdited && oauth.profile && oauth.profile.model && !model.value.trim()) {
    model.value = oauth.profile.model;
  }
  syncOAuthControls();
  if (running) oauthLive("正在等待浏览器完成授权…", "running");
  else if (connected) oauthLive("ChatGPT 账号已连接。", "ok");
  else if (failed) oauthLocalOrServerError();
  else if (oauth.loadError) oauthLive("无法读取登录状态；请刷新页面后重试。", "error");
  else oauthLive("尚未连接 ChatGPT 账号。", "");
  renderOAuthConnected(connected ? oauth.profile : null);
  if (oauth.listState !== view) {
    oauth.listState = view;
    if (state.profiles.length) renderList();
  }
}

function oauthLocalOrServerError() {
  if (oauth.localError) oauthLive(oauth.localError, "error");
  else oauthLive("登录未完成：{message}", "error",
    {message: oauthMessage(oauth.message) || settingsT("未知")});
}

function applyOAuthStatus(data) {
  if (!data || typeof data !== "object") return;
  oauth.loadError = false;
  oauth.localError = "";
  oauth.state = ["idle", "running", "succeeded", "failed"].includes(data.state) ? data.state : "failed";
  oauth.message = typeof data.message === "string" ? data.message : "";
  const profile = data.profile && typeof data.profile === "object" ? data.profile : null;
  oauth.profile = data.has_profile === false ? null : profile;
  oauth.hasProfile = Boolean(data.has_profile || oauth.profile);
  if (!oauth.hasProfile) oauth.profile = null;
  if (oauth.state === "running") oauth.seenRunning = true;
  renderOAuth();
}

function stopOAuthPolling() {
  oauth.polling = false;
  if (oauth.timer !== null) { clearTimeout(oauth.timer); oauth.timer = null; }
}

async function afterOAuthSettled(data) {
  if (data && data.state === "succeeded") {
    try {
      await load();
    } catch (error) {
      setStatus(error.message, "error");
    }
  }
  renderOAuth();
  const card = el("settings-oauth");
  const wasOurs = card.contains(document.activeElement) || document.activeElement === document.body;
  if (!wasOurs) return;
  if (data && data.state === "failed") {
    el("oauth-login").focus({ preventScroll: true });
    return;
  }
  const target = el("oauth-connected").hidden ? el("oauth-status") : el("oauth-connected");
  target.setAttribute("tabindex", "-1");
  target.focus({ preventScroll: true });
}

function startOAuthPolling() {
  if (oauth.polling) return;
  oauth.polling = true;
  oauth.startedAt = Date.now();
  const token = ++oauth.token;
  const schedule = () => {
    if (!oauth.polling || token !== oauth.token) return;
    oauth.timer = setTimeout(check, OAUTH_POLL_MS);
  };
  const check = async () => {
    if (!oauth.polling || token !== oauth.token) return;
    oauth.timer = null;
    if (Date.now() - oauth.startedAt > OAUTH_WAIT_MS) {
      stopOAuthPolling();
      oauth.state = "failed";
      oauth.message = "Authorization timed out; try again.";
      renderOAuth();
      return;
    }
    let data = null;
    try {
      data = await api("/api/providers/oauth/openai");
    } catch (_) {
      schedule();
      return;
    }
    if (!oauth.polling || token !== oauth.token) return;
    // A poll can arrive before the background thread reports "running"; the
    // idle state is not a terminal outcome while a sign-in was just started.
    if (data && data.state === "idle" && !oauth.seenRunning) { schedule(); return; }
    applyOAuthStatus(data || {});
    if (data && data.running) { schedule(); return; }
    stopOAuthPolling();
    await afterOAuthSettled(data || {});
  };
  schedule();
}

async function loadOAuth() {
  try {
    const data = await api("/api/providers/oauth/openai");
    oauth.seenRunning = false;
    applyOAuthStatus(data || {});
    if (data && data.running) { oauth.seenRunning = true; startOAuthPolling(); }
    return data;
  } catch (error) {
    oauth.state = "idle";
    oauth.message = "";
    oauth.profile = null;
    oauth.hasProfile = false;
    oauth.loadError = true;
    renderOAuth();
    return null;
  }
}

async function oauthLogin(event) {
  if (event) event.preventDefault();
  if (oauth.state === "running" || state.busy || oauthDiagnostics.busy) return;
  // Hosted instances cannot complete a loopback callback, so sign-in continues on
  // the dedicated page, which also lists the account's models before saving.
  window.location.assign("/oauth");
  return;
  oauthDiagnostics.result = null;
  oauthDiagnostics.error = "";
  renderOAuthDiagnostics();
  const body = {model: el("oauth-model").value.trim(),
    use_system_proxy: el("oauth-proxy").checked};
  if (!body.model && oauth.profile && oauth.profile.model) body.model = oauth.profile.model;
  if (!body.model) {
    oauth.state = "failed";
    oauth.message = "";
    oauth.loadError = false;
    oauth.localError = "请输入模型标识（slug）后重试。";
    renderOAuth();
    el("oauth-model").focus();
    return;
  }
  oauth.loadError = false;
  oauth.localError = "";
  oauth.state = "running";
  oauth.message = "";
  renderOAuth();
  try {
    const data = await api("/api/providers/oauth/openai/login", {method: "POST", body: JSON.stringify(body)});
    applyOAuthStatus(data || {});
    if (el("settings-oauth").dataset.oauthState === "running") startOAuthPolling();
  } catch (error) {
    if (error.status === 409) {
      // Another sign-in is running: keep the waiting state and wait for it.
      oauth.state = "running";
      oauth.message = error.message;
      renderOAuth();
      oauth.seenRunning = true;
      startOAuthPolling();
      return;
    }
    stopOAuthPolling();
    oauth.state = "failed";
    oauth.message = error.message;
    oauth.localError = "";
    renderOAuth();
  }
}

function profileById(id) {
  return state.profiles.find(profile => profile.id === id);
}

function editorBadgeText(profile) {
  if (!profile) return settingsT("自定义");
  if (profile.source === "env") return settingsT("环境 · 只读");
  if (profile.id === OAUTH_ID) return settingsT("ChatGPT 登录");
  return profile.protocol;
}

function fillForm(profile) {
  setStatus("");
  state.selected = profile ? profile.id : null;
  el("provider-id").value = profile ? profile.id : "";
  el("provider-name").value = profile ? profile.name : "";
  el("provider-protocol").value = profile ? profile.protocol : "chat_completions";
  el("provider-url").value = profile ? profile.base_url : "";
  el("provider-model").value = profile ? profile.model : "";
  el("provider-key").value = "";
  el("provider-key").type = "password";
  el("toggle-provider-key").textContent = settingsT("显示");
  el("toggle-provider-key").setAttribute("aria-pressed", "false");
  el("provider-key").placeholder = profile && profile.has_key
    ? settingsT("已存储密钥 · {key}（留空以保留）", {key: profile.masked_key})
    : settingsT("接口不需要密钥时请留空");
  el("key-hint").textContent = profile && profile.has_key
    ? settingsT("密钥存储在服务端；不会在此显示。")
    : settingsT("此配置未存储密钥。");
  el("provider-input-price").value = profile && profile.input_price != null ? profile.input_price : "";
  el("provider-output-price").value = profile && profile.output_price != null ? profile.output_price : "";
  el("provider-max-tokens").value = profile ? profile.max_output_tokens || 3000 : 3000;
  el("provider-currency").value = profile ? profile.currency : "CNY";
  el("provider-activate").checked = !profile || profile.id === state.active;
  el("editor-title").textContent = profile ? profile.name : settingsT("新建供应商");
  el("editor-badge").textContent = editorBadgeText(profile);
  const readOnly = profile && profile.source === "env";
  el("delete-provider").disabled = !profile || readOnly;
  el("activate-provider").disabled = !profile || profile.id === state.active;
  el("save-provider").disabled = !!readOnly;
  el("save-provider").textContent = readOnly ? settingsT("环境配置为只读") : settingsT("保存配置");
  el("readonly-note").hidden = !readOnly;
  el("test-result").hidden = true;
  el("provider-ready-actions").hidden = true;
  const draft = !readOnly && drafts[draftId()];
  if (draft && typeof draft === "object") {
    for (const [field, id] of Object.entries(DRAFT_FIELDS)) {
      if (typeof draft[field] === "string" || typeof draft[field] === "number") el(id).value = draft[field];
    }
    el("provider-activate").checked = Boolean(draft.activate);
    el("provider-key").value = keyDrafts[draftId()] || "";
  }
  state.dirty = Boolean(draft);
  el("provider-draft-status").hidden = !draft;
  el("discard-provider-draft").hidden = !draft;
  el("provider-draft-status").dataset.i18n = "已恢复未保存的配置草稿。密钥仅暂存在本页，刷新后需重新输入。";
  el("provider-draft-status").textContent = settingsT(el("provider-draft-status").dataset.i18n);
  syncControls();
}

function renderList() {
  const host = el("provider-list");
  const focusedId = host.contains(document.activeElement) ? document.activeElement.dataset.profileId : null;
  host.replaceChildren();
  for (const profile of state.profiles) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.profileId = profile.id;
    button.className = "provider-item" + (profile.id === state.selected ? " active" : "") +
      (profile.id === state.active ? " is-active" : "");
    button.setAttribute("aria-pressed", String(profile.id === state.selected));
    button.disabled = state.busy;
    const head = document.createElement("span");
    head.className = "provider-item-head";
    const isOAuth = profile.id === OAUTH_ID;
    if (isOAuth) {
      const mark = document.createElement("span");
      mark.className = "provider-mark";
      mark.setAttribute("aria-hidden", "true");
      const logo = document.createElement("img");
      logo.src = "/assets/logos/chatgpt-logo.svg";
      logo.alt = "";
      logo.width = 16;
      logo.height = 16;
      mark.append(logo);
      head.append(mark);
    }
    head.append(document.createTextNode(profile.name));
    if (isOAuth) head.append(badge(settingsT("ChatGPT 登录"), "verified oauth-brand"));
    if (profile.id === state.active) head.append(badge(settingsT("当前"), "yes"));
    if (profile.source === "env") head.append(badge("env"));
    if (isOAuth && oauth.state === "running") head.append(badge(settingsT("正在登录"), "no"));
    if (isOAuth && oauth.state === "failed") head.append(badge(settingsT("登录失败"), "warn"));
    const meta = document.createElement("span");
    meta.className = "provider-item-meta";
    meta.textContent = [isOAuth ? "ChatGPT OAuth" : profile.protocol, profile.model,
      isOAuth ? settingsT("钥匙串令牌") : profile.has_key ? settingsT("已存储密钥") : settingsT("无密钥"),
      profile.input_price != null || profile.output_price != null
        ? `${text(profile.input_price)}/${text(profile.output_price)} ${profile.currency}/1M`
        : settingsT("价格未知")].join(" · ");
    button.append(head, meta);
    button.addEventListener("click", () => {
      if (state.busy) return;
      fillForm(profile);
      renderList();
    });
    li.append(button);
    host.append(li);
  }
  if (!host.children.length) {
    const li = document.createElement("li");
    li.className = "settings-empty";
    li.textContent = settingsT("暂无供应商配置。");
    host.append(li);
  }
  if (focusedId !== null) {
    const replacement = [...host.querySelectorAll("button")].find(button => button.dataset.profileId === focusedId);
    (replacement || el("new-provider")).focus({ preventScroll: true });
  }
  const active = profileById(state.active) || state.profiles[0];
  const label = el("active-model-label");
  label.replaceChildren();
  if (active) {
    const provider = document.createElement("span");
    provider.className = "model-provider";
    provider.textContent = active.name;
    const model = document.createElement("span");
    model.className = "model-name";
    model.textContent = active.model;
    label.title = `${active.name} · ${active.model}`;
    const copy = document.createElement("span");
    copy.className = "model-copy";
    copy.append(provider, model);
    label.append(copy);
  } else {
    label.textContent = settingsT("未配置模型");
  }
}

function payload() {
  const value = (id) => el(id).value.trim();
  return {
    id: value("provider-id") || undefined,
    name: value("provider-name"),
    protocol: value("provider-protocol"),
    base_url: value("provider-url"),
    model: value("provider-model"),
    api_key: el("provider-key").value,
    input_price: value("provider-input-price"),
    output_price: value("provider-output-price"),
    max_output_tokens: value("provider-max-tokens") || "3000",
    currency: value("provider-currency") || "CNY",
    activate: el("provider-activate").checked,
  };
}

async function load() {
  const data = await api("/api/providers");
  state.active = data.active;
  state.profiles = data.profiles || [];
  let selected = profileById(state.selected) || profileById(state.active) || state.profiles[0];
  if (state.selected === null && drafts.new) selected = null;
  fillForm(selected || null);
  renderList();
}

async function save(event) {
  event.preventDefault();
  if (state.busy) return;
  if (state.selected === "env") {
    localizedStatus("Environment / default 是只读配置；请新建自定义配置。", "error");
    return;
  }
  if (!el("provider-form").reportValidity()) return;
  const body = payload();
  const pendingDraft = draftId();
  setBusy(true);
  localizedStatus("正在保存配置…");
  try {
    if (state.selected === "env" && !body.id) throw new Error(settingsT("请创建自定义配置 ID。"));
    if (state.selected === "env") body.id = undefined;
    const result = await api("/api/providers", { method: "POST", body: JSON.stringify(body) });
    clearDraft(pendingDraft);
    state.selected = result.profile.id;
    await load();
    fillForm(profileById(result.profile.id));
    renderList();
    el("provider-ready-actions").hidden = result.active !== result.profile.id;
    localizedStatus(result.active === result.profile.id
      ? "已保存并设为当前模型；可以开始检索或继续对话。"
      : "配置已保存，点击启用后供后续任务使用。", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function testConnection() {
  if (state.busy) return;
  if (!el("provider-form").reportValidity()) return;
  const body = payload();
  setBusy(true);
  localizedStatus("正在服务端测试接口，发送小型结构化请求，可能产生少量费用。", "");
  el("test-result").hidden = true;
  try {
    const result = await api("/api/providers/test", { method: "POST", body: JSON.stringify(body) });
    lastTestResult = result;
    renderTestResult(result);
    if (result.message) setStatus(result.message, result.ok ? "ok" : "error");
    else localizedStatus(result.ok ? "接口可访问。" : "接口测试失败。", result.ok ? "ok" : "error");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    setBusy(false);
  }
}

let lastTestResult = null;
function renderTestResult(result) {
    const box = el("test-result");
    box.replaceChildren();
    box.hidden = false;
    const head = document.createElement("p");
    head.className = "test-head " + (result.ok ? "ok" : "error");
    head.textContent = result.ok ? settingsT("接口可访问。") : settingsT("接口测试失败。");
    box.append(head);
    const facts = document.createElement("p");
    facts.className = "test-facts";
    facts.textContent = [
      settingsT("状态") + " " + text(result.status),
      settingsT("协议") + " " + text(result.protocol),
      result.http_status != null ? "HTTP " + result.http_status : "",
      result.latency_ms != null ? result.latency_ms + "ms" : "",
      result.model ? settingsT("模型") + " " + result.model : "",
    ].filter(Boolean).join(" · ");
    box.append(facts);
    if (result.error) {
      const error = document.createElement("p");
      error.className = "test-error";
      error.textContent = result.error;
      box.append(error);
      const help = document.createElement("p");
      help.className = "test-facts";
      help.textContent = ({auth:settingsT("检查 API key 和该模型的访问权限。"), model_or_endpoint:settingsT("检查模型名称、接口地址及所选协议。"),
        timeout:settingsT("请求超时；配置和草稿已保留，可检查地址后再试。"), connection:settingsT("无法连接该地址；检查本机服务或网络。"),
        protocol:settingsT("接口已响应，但结构化输出格式不兼容；检查协议或换一个模型。")})[result.status] || settingsT("配置仍保留，检查接口信息后可再次测试。");
      box.append(help);
    }
    if (result.usage && (result.usage.input_tokens != null || result.usage.output_tokens != null)) {
      const usage = document.createElement("p");
      usage.className = "test-facts";
      usage.textContent = settingsT("用量") + " " + text(result.usage.input_tokens) + " " + settingsT("输入") + " / "
        + text(result.usage.output_tokens) + " " + settingsT("输出")
        + (result.cost ? " · " + settingsT("测试费用") + " " + text(result.cost.total) + " " + result.cost.currency : "");
      box.append(usage);
    }
}

async function activate() {
  if (!state.selected || state.busy) return;
  setBusy(true);
  try {
    await api("/api/providers/activate", { method: "POST", body: JSON.stringify({ id: state.selected }) });
    await load();
    localizedStatus("当前供应商已更改，新任务将使用它。", "ok");
    el("provider-ready-actions").hidden = false;
  } catch (error) {
    setStatus(error.message, "error");
  } finally { setBusy(false); }
}

async function remove() {
  if (!state.selected || state.selected === "env" || state.busy) return;
  if (!window.confirm(settingsT("确认删除此供应商配置？API 密钥将从本机删除。"))) return;
  const removed = state.selected;
  setBusy(true);
  try {
    await api("/api/providers/" + encodeURIComponent(state.selected), { method: "DELETE" });
    clearDraft(removed);
    state.selected = null;
    await load();
    localizedStatus("配置已删除。", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally { setBusy(false); }
}

el("provider-form").addEventListener("submit", save);
el("test-provider").addEventListener("click", testConnection);
el("activate-provider").addEventListener("click", activate);
el("delete-provider").addEventListener("click", remove);
el("new-provider").addEventListener("click", () => {
  if (state.busy) return;
  fillForm(null);
  renderList();
  el("provider-name").focus();
});
el("provider-form").addEventListener("input", rememberDraft);
el("provider-form").addEventListener("change", rememberDraft);
el("discard-provider-draft").addEventListener("click", () => {
  if (state.busy) return;
  clearDraft(draftId()); fillForm(profileById(state.selected) || null);
});
el("toggle-provider-key").addEventListener("click", () => {
  const show = el("provider-key").type === "password";
  el("provider-key").type = show ? "text" : "password";
  el("toggle-provider-key").textContent = show ? settingsT("隐藏") : settingsT("显示");
  el("toggle-provider-key").setAttribute("aria-pressed", String(show));
});
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !event.isComposing && !state.busy
      && !el("save-provider").disabled && !el("settings-oauth").contains(document.activeElement)) {
    event.preventDefault(); el("provider-form").requestSubmit();
  }
});
window.addEventListener("sg:language-change", () => {
  syncControls();
  renderList();
  renderOAuth();
  renderOAuthDiagnostics();
  if (lastTestResult && !el("test-result").hidden) renderTestResult(lastTestResult);
  el("toggle-provider-key").textContent = settingsT(el("provider-key").type === "password" ? "显示" : "隐藏");
  const profile = profileById(state.selected);
  el("editor-title").textContent = profile ? profile.name : settingsT("新建供应商");
  el("editor-badge").textContent = editorBadgeText(profile);
  el("provider-key").placeholder = profile && profile.has_key ? settingsT("已存储密钥 · {key}（留空以保留）", {key: profile.masked_key}) : settingsT("接口不需要密钥时请留空");
  el("key-hint").textContent = settingsT(profile && profile.has_key ? "密钥存储在服务端；不会在此显示。" : "此配置未存储密钥。");
});

async function loadOAuthCatalogue() {
  const select = el("oauth-model-select");
  if (!select) return;
  try {
    const data = await api("/api/providers/oauth/openai/catalog");
    const models = data.models || [];
    select.innerHTML = '<option value="">预置模型…</option>' + models.map(
      (m) => `<option value="${m.id}">${m.label}（${m.id}）</option>`).join("");
  } catch (_) {
    select.innerHTML = '<option value="">预置模型不可用</option>';
  }
}

async function saveOAuthModel(event) {
  if (event) event.preventDefault();
  const select = el("oauth-model-select");
  const id = ((el("oauth-model").value || (select ? select.value : "")) || "").trim();
  if (!id) {
    setStatus("请选择预置模型，或填写模型 ID。", "error");
    return;
  }
  try {
    await api("/api/providers", {method: "POST", body: JSON.stringify({
      id: "openai-oauth", name: "OpenAI / ChatGPT OAuth", protocol: "responses",
      base_url: "https://api.openai.com/v1", model: id, source: "custom", activate: true})});
    await load();
    setStatus("已保存并启用：" + id, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
}

if (el("oauth-form")) {
  el("oauth-form").addEventListener("submit", oauthLogin);
  el("oauth-test").addEventListener("click", testOAuthAccount);
  el("oauth-model").addEventListener("input", () => { oauth.slugEdited = true; });
  if (el("oauth-model-select")) {
    el("oauth-model-select").addEventListener("change", () => {
      const value = el("oauth-model-select").value;
      if (value) el("oauth-model").value = value;
    });
    loadOAuthCatalogue();
  }
  if (el("oauth-save")) el("oauth-save").addEventListener("click", saveOAuthModel);
}
load().catch(error => setStatus(error.message, "error"));
loadOAuth();
