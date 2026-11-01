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

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      signal: AbortSignal.timeout(35000),
    });
  } catch (_) {
    throw new Error(settingsT("无法连接本地服务。"));
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || settingsT("请求失败（{status}）", {status: response.status}));
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

function profileById(id) {
  return state.profiles.find(profile => profile.id === id);
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
  el("editor-badge").textContent = profile ? (profile.source === "env" ? settingsT("环境 · 只读") : profile.protocol) : settingsT("自定义");
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
    head.append(document.createTextNode(profile.name));
    if (profile.id === state.active) head.append(badge(settingsT("当前"), "yes"));
    if (profile.source === "env") head.append(badge("env"));
    const meta = document.createElement("span");
    meta.className = "provider-item-meta";
    meta.textContent = [profile.protocol, profile.model, profile.has_key ? settingsT("已存储密钥") : settingsT("无密钥"),
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
      && !el("save-provider").disabled) {
    event.preventDefault(); el("provider-form").requestSubmit();
  }
});
window.addEventListener("sg:language-change", () => {
  syncControls();
  renderList();
  if (lastTestResult && !el("test-result").hidden) renderTestResult(lastTestResult);
  el("toggle-provider-key").textContent = settingsT(el("provider-key").type === "password" ? "显示" : "隐藏");
  const profile = profileById(state.selected);
  el("editor-title").textContent = profile ? profile.name : settingsT("新建供应商");
  el("editor-badge").textContent = profile ? (profile.source === "env" ? settingsT("环境 · 只读") : profile.protocol) : settingsT("自定义");
  el("provider-key").placeholder = profile && profile.has_key ? settingsT("已存储密钥 · {key}（留空以保留）", {key: profile.masked_key}) : settingsT("接口不需要密钥时请留空");
  el("key-hint").textContent = settingsT(profile && profile.has_key ? "密钥存储在服务端；不会在此显示。" : "此配置未存储密钥。");
});
load().catch(error => setStatus(error.message, "error"));
