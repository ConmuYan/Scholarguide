"use strict";

// A deterministic, local product film. No media, requests, or model work.
(function researchFilm() {
  const figure = document.getElementById("research-film");
  if (!figure) return;
  const film = figure.querySelector(".sg-film");
  const viewport = document.getElementById("film-viewport");
  const play = document.getElementById("film-play");
  const app = document.getElementById("film-app");
  const cursor = document.getElementById("film-cursor");
  if (!viewport || !play || !app) return;
  const DURATION = 30000;
  const SPEED = 1.5;
  // These are illustrative strings only, never values in a user's workspace draft.
  const query = "推荐 5 篇 RAG 评测论文，比较评测方法与开放问题。";
  const followup = "只保留可比较的评测方法，指出各自的适用边界。";
  const t = key => window.SGI18n ? window.SGI18n.t(key) : key;
  const chapters = [
    {id: "compose", start: 0}, {id: "results", start: 8000},
    {id: "synthesis", start: 15500}, {id: "chat", start: 23000},
  ];
  const panels = chapters.map(chapter => document.getElementById("film-" + chapter.id));
  const reveals = [...figure.querySelectorAll("[data-reveal]")];
  const revealAt = {sources: 8400, "paper-a": 9500, "paper-b": 10100,
    quote: 11100, synthesis: 16300, hypothesis: 18100,
    "chat-message": 26300, "chat-answer": 27700};
  const sources = ["film-source-openreview", "film-source-openalex", "film-source-s2"].map(id => document.getElementById(id));
  const input = document.getElementById("film-input");
  const chatInput = document.getElementById("film-chat-input");
  const status = document.getElementById("film-agent-status");
  const caption = document.getElementById("film-caption-text");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  let position = reduced.matches ? 20000 : 0;
  let intent = !reduced.matches;
  let inView = !("IntersectionObserver" in window);
  let raf = null;
  let lastTimestamp = null;
  let scene = "";
  let cycle = 0;
  let geometry = new Map();

  function text(element, value) {
    if (element && element.textContent !== value) element.textContent = value;
  }
  function attribute(element, key, value) {
    if (element && element.getAttribute(key) !== String(value)) element.setAttribute(key, String(value));
  }
  function typed(value, start, duration) {
    const letters = Array.from(value);
    return letters.slice(0, Math.floor(letters.length * Math.min(1, Math.max(0, (position - start) / duration)))).join("");
  }
  function eligible() { return intent && inView && !document.hidden; }
  function controls() {
    const playing = eligible();
    attribute(figure, "data-playing", playing);
    attribute(film, "data-playing", playing);
    attribute(play, "data-state", playing ? "playing" : "paused");
    attribute(play, "aria-pressed", playing);
    attribute(play, "aria-label", t(playing ? "暂停操作演示" : "继续播放操作演示"));
  }
  // Read target geometry on scene/viewport changes, rather than each frame.
  function point(target, x = .75, y = .6) {
    const key = target + ":" + x + ":" + y;
    if (geometry.has(key)) return geometry.get(key);
    const parent = viewport.getBoundingClientRect();
    const element = target && document.getElementById(target);
    const box = element && !element.closest("[hidden]") ? element.getBoundingClientRect() : null;
    const value = box && box.width ? {x: box.left - parent.left + box.width * x,
      y: box.top - parent.top + box.height * y} : {x: parent.width * x, y: parent.height * y};
    geometry.set(key, value);
    return value;
  }
  const paths = {
    compose: [[0, null, .93, .85], [900, "film-input", .72, .58],
      [4600, "film-input", .72, .58], [6200, "film-search-button", .7, .6],
      [7900, "film-search-button", .7, .6]],
    results: [[8000, null, .84, .26], [10200, "film-paper-a", .78, .45],
      [11300, "film-paper-a", .78, .45], [12600, "film-quote", .83, .65],
      [15499, "film-quote", .83, .65]],
    synthesis: [[15500, null, .84, .25], [18200, "film-hypothesis", .8, .65],
      [22999, "film-hypothesis", .8, .65]],
    chat: [[23000, "film-chat-input", .77, .52], [25200, "film-chat-input", .77, .52],
      [26000, "film-chat-send", .5, .5], [29999, "film-chat-send", .5, .5]],
  };
  function pointer() {
    if (!cursor) return;
    const path = paths[scene];
    let left = path[0], right = path[path.length - 1];
    for (let index = 1; index < path.length; index++) {
      if (position <= path[index][0]) { left = path[index - 1]; right = path[index]; break; }
    }
    const first = point(left[1], left[2], left[3]);
    const last = point(right[1], right[2], right[3]);
    const part = Math.max(0, Math.min(1, (position - left[0]) / Math.max(1, right[0] - left[0])));
    const smooth = part * part * (3 - 2 * part);
    cursor.style.transform = "translate3d(" + (first.x + (last.x - first.x) * smooth).toFixed(2) +
      "px," + (first.y + (last.y - first.y) * smooth).toFixed(2) + "px,0)";
    attribute(cursor, "data-visible", !reduced.matches && position > 450 && position < 29600);
    attribute(cursor, "data-clicking", [900, 6800, 11000, 19000, 26100].some(at => Math.abs(position - at) < 180));
  }
  function narrative() {
    if (position < 4800) return ["typing", "理解研究目的", "从一个具体问题开始，明确你准备怎样使用这些论文。"];
    if (position < 8000) return ["scope", "准备定向检索", "阅读目的与关注内容，成为下一步检索的条件。"];
    if (position < 11100) return ["discovery", "检索与出版信息核对", "按任务选择信源，再对照正式出版记录与摘要内容。"];
    if (position < 15500) return ["quote", "展开摘要依据", "展开推荐依据，检查这篇论文为何适合当前问题。"];
    if (position < 18100) return ["brief", "整理贡献与技术路线", "把已选论文连起来，形成可以继续阅读的研究脉络。"];
    if (position < 23000) return ["hypothesis", "拟议实验 · 尚未验证", "区分摘要事实、模型归纳和待检验的下一步设想。"];
    if (position < 26300) return ["followup", "继续调整研究范围", "带着新的限制继续追问，沿用已有证据。"];
    return ["refine", "保留条件与证据缺口", "把方向收敛到一个可继续比较、验证的问题。"];
  }
  function render() {
    const chapter = [...chapters].reverse().find(item => position >= item.start);
    if (scene !== chapter.id) {
      scene = chapter.id;
      geometry.clear();
      panels.forEach((panel, index) => { if (panel) panel.hidden = chapters[index].id !== scene; });
      attribute(figure, "data-scene", scene);
      attribute(film, "data-scene", scene);
    }
    text(input, typed(t(query), 900, 3500));
    text(chatInput, position >= 26300 ? "" : typed(t(followup), 23200, 2400));
    attribute(chatInput, "data-placeholder", t("继续追问，收敛研究方向…"));
    const counts = {};
    reveals.forEach(element => {
      const key = element.dataset.reveal;
      const index = counts[key] || 0;
      counts[key] = index + 1;
      const visible = position >= (revealAt[key] === undefined ? 0 : revealAt[key]) + index * 220;
      attribute(element, "data-visible", visible);
      attribute(element, "aria-hidden", !visible);
    });
    sources.forEach((element, index) => {
      const start = 8400 + index * 400;
      attribute(element, "data-status", position < start ? "waiting" : position < start + 1200 ? "searching" : "done");
    });
    const story = narrative();
    attribute(figure, "data-frame", story[0]);
    attribute(film, "data-frame", story[0]);
    text(status, t(story[1]));
    text(caption, t(story[2]));
    attribute(figure, "data-time", Math.round(position));
    attribute(figure, "data-cycle", cycle);
    attribute(figure, "data-speed", SPEED);
    // The chrome remains still while the illustrated workspace gently dissolves
    // into the next cycle. Reduced motion uses discrete, fully readable frames.
    app.style.opacity = String(reduced.matches ? 1 : Math.max(0, Math.min(1,
      position / 500, (DURATION - position) / 700)));
    pointer();
    controls();
  }
  function tick(timestamp) {
    raf = null;
    if (!eligible()) { sync(); return; }
    if (lastTimestamp !== null) position += Math.min(100, timestamp - lastTimestamp) * SPEED;
    lastTimestamp = timestamp;
    if (position >= DURATION) {
      position %= DURATION;
      cycle += 1;
      geometry.clear();
    }
    render();
    if (eligible()) raf = requestAnimationFrame(tick);
    else lastTimestamp = null;
  }
  function sync() {
    if (eligible()) {
      if (raf === null) {
        lastTimestamp = null;
        attribute(figure, "data-seeking", false);
        raf = requestAnimationFrame(tick);
      }
    } else {
      if (raf !== null) cancelAnimationFrame(raf);
      raf = null;
      lastTimestamp = null;
    }
    controls();
  }
  function seek(value) {
    const target = Number(value);
    if (!Number.isFinite(target)) return;
    intent = false;
    position = Math.max(0, Math.min(DURATION, target));
    geometry.clear();
    attribute(figure, "data-seeking", true);
    sync();
    render();
  }
  play.addEventListener("click", () => {
    if (eligible()) intent = false;
    else {
      intent = true;
    }
    sync();
  });
  // Optional frame control for export/review tooling, without shipping a seek UI.
  figure.addEventListener("scholarguide:film-frame", event => {
    if (event.detail && Number.isFinite(event.detail.time)) seek(event.detail.time);
  });
  // Repaint the current position without changing playback intent or scheduling.
  window.addEventListener("sg:language-change", () => { geometry.clear(); render(); });
  document.addEventListener("visibilitychange", sync);
  const onReducedMotion = () => { if (reduced.matches) intent = false; render(); sync(); };
  if (reduced.addEventListener) reduced.addEventListener("change", onReducedMotion);
  else reduced.addListener(onReducedMotion);
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver(entries => {
      inView = entries[0].isIntersecting && entries[0].intersectionRatio >= .15;
      sync();
    }, {threshold: [0, .15]});
    observer.observe(viewport);
  }
  const resize = () => { geometry.clear(); render(); };
  if ("ResizeObserver" in window) new ResizeObserver(resize).observe(viewport);
  else window.addEventListener("resize", resize);
  attribute(figure, "data-enhanced", true);
  const controlBar = document.getElementById("film-controls");
  if (controlBar) controlBar.hidden = false;
  render();
  sync();
})();
