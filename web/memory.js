"use strict";

const memoryT = (key, params) => window.SGI18n.t(key, params);

(() => {
  const endpoint = "/api/v1/memory/preferences";
  const forms = [...document.querySelectorAll(".memory-item")];
  const status = document.getElementById("memory-status");
  let records = null;
  let storeId = null;
  let busy = false;
  let sequence = 0;
  let expiryTimer;

  function message(text, error = false) {
    status.dataset.messageKey = text;
    status.textContent = memoryT(text);
    status.dataset.kind = error ? "error" : "";
  }
  function effective(record) {
    return record && ["active", "granted", "authorized"].includes(record.state) &&
      record.consent === true && typeof record.expires_at === "number" && record.expires_at * 1000 > Date.now();
  }
  function render() {
    clearTimeout(expiryTimer);
    let nextExpiry = Infinity;
    for (const form of forms) {
      const record = records && records[form.dataset.key];
      let description = memoryT("状态未知");
      if (records) {
        if (!record) description = memoryT("未授权") + " · " + memoryT("修订号") + " 0 · " + memoryT("失效时间") + "：" + memoryT("无");
        else {
          const expiry = typeof record.expires_at === "number" && Number.isFinite(record.expires_at)
            ? new Date(record.expires_at * 1000) : null;
          const validExpiry = expiry && !Number.isNaN(expiry.getTime());
          let label = ({active:"已授权", granted:"已授权", authorized:"已授权", expired:"已过期", revoked:"已撤回", deleted:"已删除"})[record.state] || "状态未知";
          if (["active", "granted", "authorized"].includes(record.state)) {
            if (record.consent !== true || !validExpiry) label = "未授权";
            else if (!effective(record)) label = "已过期";
            else nextExpiry = Math.min(nextExpiry, expiry.getTime());
          }
          description = memoryT(label) + " · " + memoryT("修订号") + " " + record.revision + " · " + memoryT("失效时间") + "：" + (validExpiry ? expiry.toLocaleString() : memoryT("无"));
          if (effective(record)) {
            const values = {orientation:"入门了解", comparison:"比较方法", frontier:"追踪前沿", general:"一般阅读", true:"是", false:"否"};
            const value = values[String(record.value)];
            if (value) description += " · " + memoryT("当前值") + "：" + memoryT(value);
          }
        }
      }
      form.querySelector(".memory-current").textContent = description;
      form.querySelectorAll("input, select, button").forEach(node => { node.disabled = busy || !records; });
      form.querySelector('[data-action="revoke"]').disabled = busy || !records || !effective(record);
      form.querySelector('[data-action="delete"]').disabled = busy || !records || !record || record.state === "deleted";
      form.setAttribute("aria-busy", String(busy));
    }
    if (Number.isFinite(nextExpiry)) expiryTimer = setTimeout(render, Math.min(2147483647, Math.max(1, nextExpiry - Date.now() + 1)));
  }
  async function refresh() {
    const token = ++sequence;
    records = null;
    storeId = null;
    render();
    try {
      const response = await fetch(endpoint, {cache:"no-store"});
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (token !== sequence) return false;
      if (data.api_version !== "v1" || !data.preferences || typeof data.preferences !== "object" || Array.isArray(data.preferences) || typeof data.store_id !== "string" || !/^[0-9a-f]{64}$/.test(data.store_id)) throw new Error();
      for (const key of ["reading_goal", "prefer_recent"]) {
        const record = data.preferences[key];
        if (record && (!Number.isSafeInteger(record.revision) || record.revision < 0)) throw new Error();
      }
      records = data.preferences;
      storeId = data.store_id;
      return true;
    } catch (_) {
      if (token === sequence) message("无法读取本机偏好，状态未知；请刷新页面后重试。", true);
      return false;
    } finally {
      if (token === sequence) render();
    }
  }
  async function mutate(form, action, body) {
    if (busy || !records) return;
    busy = true;
    ++sequence;
    render();
    message("正在更新本机偏好...");
    let conflict = false;
    try {
      const response = await fetch(endpoint + "/" + form.dataset.key + (action === "grant" ? "" : "/" + action), {
        method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({...body, store_id:storeId})
      });
      conflict = response.status === 409;
      if (!response.ok && !conflict) throw new Error();
      const loaded = await refresh();
      if (loaded) {
        if (conflict) message("版本冲突：已刷新当前状态，未重放操作；请检查后重新操作。", true);
        else {
          form.elements.consent.checked = false;
          message("本机偏好已更新；近期偏好可由你主动用于当前结果的显示排序。");
        }
      }
    } catch (_) {
      records = null;
      message("操作结果未知，未自动重试；请刷新页面查看当前状态。", true);
    } finally {
      busy = false;
      render();
    }
  }
  for (const form of forms) {
    form.addEventListener("keydown", event => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") event.stopPropagation();
    });
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (busy || !records || !form.reportValidity()) return;
      const raw = form.elements.value.value;
      const allowed = form.dataset.key === "reading_goal" ? ["orientation", "comparison", "frontier", "general"] : ["true", "false"];
      const expiry = Math.floor(new Date(form.elements.expiry.value).getTime() / 1000);
      if (!allowed.includes(raw) || !form.elements.consent.checked || !Number.isSafeInteger(expiry) || expiry <= Date.now() / 1000) {
        message("请选择有效偏好、明确同意并输入未来的失效时间。", true);
        return;
      }
      const value = form.dataset.key === "prefer_recent" ? raw === "true" : raw;
      mutate(form, "grant", {value, consent:true, expires_at:expiry, expected_revision:records[form.dataset.key]?.revision ?? 0});
    });
    for (const button of form.querySelectorAll("[data-action]")) button.addEventListener("click", () => {
      if (busy || !records || button.disabled) return;
      const action = button.dataset.action;
      const prompt = action === "revoke" ? "确认撤回此项授权？旧历史不受影响。" : "确认删除此项本机偏好？不会删除历史或远端数据。";
      if (!window.confirm(memoryT(prompt))) return;
      mutate(form, action, {expected_revision:records[form.dataset.key]?.revision ?? 0});
    });
  }
  window.addEventListener("sg:language-change", () => {
    render();
    if (status.dataset.messageKey) status.textContent = memoryT(status.dataset.messageKey);
  });
  render();
  refresh().then(ok => { if (ok) message("已读取本机偏好；不会自动授权。"); });
})();
