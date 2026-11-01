"use strict";

// Keys are the original Simplified Chinese strings; unregistered keys fall back to them.
(function i18n() {
  const KEY = "scholarguide-language";
  const languages = ["zh-CN", "zh-Hant", "en"];
  const dictionary = Object.create(null);
  let lang = "zh-CN";
  try {
    const saved = localStorage.getItem(KEY);
    if (languages.includes(saved)) lang = saved;
  } catch (_) {}

  function register(entries) {
    if (!entries || typeof entries !== "object" || Array.isArray(entries)) return;
    Object.keys(entries).forEach(key => {
      const value = entries[key];
      if (!value || typeof value !== "object" || Array.isArray(value)) return;
      const item = dictionary[key] || (dictionary[key] = Object.create(null));
      ["en", "zh-Hant"].forEach(code => {
        if (typeof value[code] === "string") item[code] = value[code];
      });
    });
  }
  register({
    "语言": {en: "Language", "zh-Hant": "語言"},
    "浅色": {en: "Light", "zh-Hant": "淺色"},
    "深色": {en: "Dark", "zh-Hant": "深色"},
    "跟随系统": {en: "System", "zh-Hant": "跟隨系統"},
    "主题：{mode}（点击切换）": {en: "Theme: {mode} (click to change)", "zh-Hant": "主題：{mode}（點擊切換）"},
    "主题：{mode}": {en: "Theme: {mode}", "zh-Hant": "主題：{mode}"},
    "打开导航": {en: "Open navigation", "zh-Hant": "開啟導覽"},
    "关闭导航": {en: "Close navigation", "zh-Hant": "關閉導覽"},
    "语言：{current}；切换为{next}": {en: "Language: {current}; switch to {next}", "zh-Hant": "語言：{current}；切換為{next}"}
  });
  function t(key, params = {}) {
    const entry = dictionary[key];
    const text = lang !== "zh-CN" && entry && typeof entry[lang] === "string" ? entry[lang] : key;
    return String(text).replace(/\{([^{}]+)\}/g, (match, name) =>
      params != null && Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match);
  }
  const attributes = ["placeholder", "title", "aria-label", "alt", "content"];
  const selector = ["[data-i18n]", ...attributes.map(name => `[data-i18n-${name}]`)].join(",");
  function apply(root = document) {
    if (!root) return;
    function translate(element) {
      if (element.hasAttribute("data-i18n") && !/^(INPUT|TEXTAREA|SELECT)$/.test(element.tagName) &&
          element.childNodes.length === 1 && element.firstChild.nodeType === 3) {
        let params = {};
        try { params = JSON.parse(element.getAttribute('data-i18n-params') || '{}'); } catch (_) {}
        element.firstChild.nodeValue = t(element.getAttribute("data-i18n"), params);
      }
      attributes.forEach(name => {
        const marker = "data-i18n-" + name;
        if (element.hasAttribute(marker)) element.setAttribute(name, t(element.getAttribute(marker)));
      });
    }
    if (root.nodeType === 1 && root.matches(selector)) translate(root);
    if (root.querySelectorAll) root.querySelectorAll(selector).forEach(translate);
  }
  function refresh() {
    document.documentElement.lang = lang;
    const button = document.getElementById("sg-language-toggle");
    if (button) {
      const index = languages.indexOf(lang);
      const names = {"zh-CN": "简体中文", "zh-Hant": "繁體中文", en: "English"};
      const label = t("语言：{current}；切换为{next}", {current: names[lang], next: names[languages[(index + 1) % languages.length]]});
      button.dataset.language = lang;
      button.setAttribute("aria-label", label);
      button.title = label;
      const badge = button.querySelector('.sg-language-badge');
      badge.textContent = {"zh-CN": "简", "zh-Hant": "繁", en: "EN"}[lang];
      badge.lang = lang;
    }
    apply(document);
  }
  function change(next, persist) {
    if (!languages.includes(next)) return false;
    const changed = next !== lang;
    lang = next;
    if (persist) { try { localStorage.setItem(KEY, lang); } catch (_) {} }
    refresh();
    if (changed) window.dispatchEvent(new CustomEvent("sg:language-change", {detail: {lang}}));
    return true;
  }
  function bind(node, key, params = {}) {
    if (!node || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName) || node.children.length) return node;
    node.dataset.i18n = key;
    node.dataset.i18nParams = JSON.stringify(params);
    node.textContent = t(key, params);
    return node;
  }
  window.SGI18n = {t, register, get: () => lang, set: value => change(value, true), apply, bind};
  window.addEventListener("storage", event => {
    if (event.key === KEY || event.key === null) {
      // A removal or clear resets the preference; invalid values are ignored.
      if (event.key === null || event.newValue === null) change("zh-CN", false);
      else change(event.newValue, false);
    }
  });
  // Deferred scripts run after HTML parsing and before later deferred scripts.
  const page = document.getElementById("sg-page-i18n");
  if (page && page.type === "application/json") {
    try { register(JSON.parse(page.textContent)); } catch (_) {}
  }
  function init() {
    const button = document.getElementById("theme-toggle");
    if (button && !document.getElementById("sg-language-toggle")) {
      const toggle = document.createElement("button");
      toggle.id = "sg-language-toggle";
      toggle.type = "button";
      toggle.className = "sg-language-toggle " + (button.classList.contains('lp-theme') ? 'lp-theme' : 'icon-btn');
      const ns = 'http://www.w3.org/2000/svg';
      const icon = document.createElementNS(ns, 'svg');
      icon.setAttribute('viewBox', '0 0 24 24');
      icon.setAttribute('fill', 'none');
      icon.setAttribute('aria-hidden', 'true');
      for (const d of ['M4 5h12M10 3v2M6 5c.5 5 3 8 7 10M14 5c-.5 5-3 8-8 11', 'm14 20 4-10 4 10M15.5 17h5']) {
        const path = document.createElementNS(ns, 'path');
        path.setAttribute('d', d); path.setAttribute('stroke', 'currentColor');
        path.setAttribute('stroke-width', '1.5'); path.setAttribute('stroke-linecap', 'round');
        path.setAttribute('stroke-linejoin', 'round'); icon.appendChild(path);
      }
      const badge = document.createElement('span');
      badge.className = 'sg-language-badge'; badge.setAttribute('aria-hidden', 'true');
      toggle.append(icon, badge);
      toggle.addEventListener("click", () => change(languages[(languages.indexOf(lang) + 1) % languages.length], true));
      button.insertAdjacentElement("afterend", toggle);
    }
    refresh();
  }
  document.documentElement.lang = lang;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();

(function theme() {
  const KEY = "scholarguide-theme";
  const root = document.documentElement;
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const modes = ["light", "dark", "system"];
  const labels = { light: "浅色", dark: "深色", system: "跟随系统" };
  let currentMode;

  function stored() {
    try {
      const value = localStorage.getItem(KEY);
      return modes.includes(value) ? value : "system";
    } catch (_) { return "system"; }
  }

  function resolved(mode) {
    if (mode === "system") return media && media.matches ? "dark" : "light";
    return mode;
  }

  function paint(mode) {
    const actual = resolved(mode);
    root.dataset.theme = actual;
    root.dataset.themeMode = mode;
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = actual === "dark" ? "☾" : "☀";
      const name = window.SGI18n.t(labels[mode]);
      button.setAttribute("aria-label", window.SGI18n.t("主题：{mode}（点击切换）", {mode: name}));
      button.title = window.SGI18n.t("主题：{mode}", {mode: name});
    }
  }

  function set(mode) {
    currentMode = mode;
    try { localStorage.setItem(KEY, mode); } catch (_) {}
    paint(mode);
  }

  function init() {
    currentMode = stored();
    paint(currentMode);
    window.addEventListener("sg:language-change", () => paint(currentMode));
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.addEventListener("click", () => {
        const current = currentMode;
        set(modes[(modes.indexOf(current) + 1) % modes.length]);
      });
    }
    if (media) {
      const onChange = () => { if (currentMode === "system") paint("system"); };
      if (media.addEventListener) media.addEventListener("change", onChange);
      else if (media.addListener) media.addListener(onChange);
    }
    const menu = document.getElementById("menu-toggle");
    const sidebar = document.getElementById("sidebar");
    if (menu && sidebar) {
      const mobile = window.matchMedia("(max-width: 920px)");
      menu.setAttribute("aria-controls", sidebar.id);

      function syncDrawer(returnFocus = false) {
        const open = mobile.matches && sidebar.classList.contains("open");
        sidebar.inert = mobile.matches && !open;
        if (sidebar.inert) sidebar.setAttribute("aria-hidden", "true");
        else sidebar.removeAttribute("aria-hidden");
        menu.setAttribute("aria-expanded", String(open));
        menu.setAttribute("aria-label", window.SGI18n.t(open ? "关闭导航" : "打开导航"));
        if (mobile.matches && !open && (returnFocus || sidebar.contains(document.activeElement))) {
          menu.focus();
        }
      }

      function closeDrawer() {
        if (!mobile.matches) return;
        sidebar.classList.remove("open");
        sidebar.style.visibility = '';
        sidebar.style.transform = '';
        syncDrawer(true);
      }

      window.addEventListener("sg:language-change", () => syncDrawer());
      menu.addEventListener("click", () => {
        if (!mobile.matches) return;
        const open = sidebar.classList.toggle("open");
        syncDrawer(!open);
        if (open) {
          sidebar.style.visibility = 'visible';
          sidebar.style.transform = 'none';
          const link = sidebar.querySelector("a");
          if (link) link.focus();
          requestAnimationFrame(() => {
            if (link && sidebar.classList.contains('open')) link.focus();
          });
        }
      });
      sidebar.querySelectorAll("a").forEach(link => link.addEventListener("click", closeDrawer));
      document.addEventListener("keydown", event => {
        if (event.key === "Escape" && mobile.matches && sidebar.classList.contains("open")) {
          event.preventDefault();
          closeDrawer();
        }
      });
      document.addEventListener("pointerdown", event => {
        if (sidebar.classList.contains("open") && !sidebar.contains(event.target) && !menu.contains(event.target)) {
          closeDrawer();
        }
      });
      const onResize = () => {
        sidebar.classList.remove("open");
        syncDrawer();
      };
      if (mobile.addEventListener) mobile.addEventListener("change", onResize);
      else mobile.addListener(onResize);
      syncDrawer();
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();

// Motion observes presentation events only. It never submits or reloads work.
(function visualFeedback() {
  function initFeedback() {
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const running = new Set();
    function animate(node, frames, options = {}) {
      if (!node || preference.matches || typeof node.animate !== 'function') return;
      const effect = node.animate(frames, {duration: 220, easing: 'cubic-bezier(.2,.8,.2,1)', fill: 'backwards', ...options});
      running.add(effect);
      effect.finished.then(() => running.delete(effect), () => running.delete(effect));
    }
    function syncPreference() {
      document.documentElement.dataset.motion = preference.matches ? 'reduced' : 'full';
      if (preference.matches) {
        for (const effect of running) effect.cancel();
        running.clear();
      }
    }
    if (preference.addEventListener) preference.addEventListener('change', syncPreference);
    else if (preference.addListener) preference.addListener(syncPreference);
    syncPreference();
    const themeButton = document.getElementById('theme-toggle');
    themeButton?.addEventListener('click', () => animate(themeButton,
      [{transform: 'rotate(-16deg) scale(.9)'}, {transform: 'rotate(0) scale(1)'}]));
    window.addEventListener('sg:language-change', () => {
      const button = document.getElementById('sg-language-toggle');
      animate(button?.querySelector('svg'),
        [{opacity: .4, transform: 'translateY(2px)'}, {opacity: 1, transform: 'none'}]);
      animate(button?.querySelector('.sg-language-badge'),
        [{opacity: .3, transform: 'translateY(3px)'}, {opacity: 1, transform: 'none'}], {duration: 180});
    });
    if (!document.body.classList.contains('landing-page') || typeof IntersectionObserver !== 'function') return;
    // Below-fold sections enter once. Content is visible even without JS or IO.
    const observer = new IntersectionObserver(entries => {
      const visible = entries.filter(entry => entry.isIntersecting);
      visible.forEach((entry, index) => {
        observer.unobserve(entry.target);
        if (entry.target.contains(document.activeElement) || document.hidden) return;
        animate(entry.target,
          [{opacity: .25, transform: 'translateY(12px)'}, {opacity: 1, transform: 'none'}],
          {duration: 380, delay: Math.min(index, 3) * 35});
      });
    }, {threshold: .08, rootMargin: '0px 0px -24px 0px'});
    document.querySelectorAll('.lp-source-head, .lp-register-group, .lp-section-header, .lp-path li, .lp-evidence-points article, .lp-closing-inner')
      .forEach(node => {
        if (node.getBoundingClientRect().top >= window.innerHeight * .95) observer.observe(node);
      });
    window.addEventListener('pagehide', () => {
      observer.disconnect();
      for (const effect of running) effect.cancel();
      running.clear();
    }, {once: true});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initFeedback);
  else initFeedback();
})();
