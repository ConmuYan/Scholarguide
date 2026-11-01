"use strict";

// Shared by Search and Chat. All generated text remains text, never markup.
(function researchBriefRenderer() {
  const STATUS = {ready: "研究综合已生成", partial: "研究综合 · 证据仍有缺口",
    skipped: "本轮未生成研究综合", failed: "研究综合暂未完成"};
  const REASONS = {no_selected_papers: "当前没有可供综合的已选论文。",
    no_usable_abstracts: "已选论文没有可使用的摘要，无法据此生成研究综合。",
    no_usable_evidence_spans: "摘要中没有可用的证据片段，无法据此生成研究综合。",
    user_cancelled: "已按你的要求停止，现有论文推荐保留。",
    budget_exhausted: "本轮剩余预算不足，现有论文推荐保留。",
    model_or_validation_error: "模型调用或综合输出校验未完成，现有论文推荐保留。"};
  const FIELDS = {contribution: "贡献 · 摘要整理", method: "技术方法 · 摘要整理",
    author_limitation: "作者明确报告的局限", model_inference: "可能的含义 · 模型推论"};

  const t = (key, params) => window.SGI18n?.t(key, params) ?? key.replace(/\{(\w+)\}/g, (_, name) => params?.[name] ?? `{${name}}`);
  function ui(tag, key, className, params) {
    const element = node(tag, t(key, params), className);
    element.dataset.sgResearchTranslation = "";
    element._sgResearchTranslation = () => { element.textContent = t(key, params); };
    return element;
  }
  if (typeof window.addEventListener === 'function') window.addEventListener("sg:language-change", () => {
    document.querySelectorAll("[data-sg-research-translation], [data-sg-research-aria]")
      .forEach(element => { element._sgResearchTranslation?.(); element._sgResearchAriaTranslation?.(); });
  });
  function text(value) { return typeof value === "string" ? value : ""; }
  function list(value) { return Array.isArray(value) ? value : []; }
  function node(tag, value, className) {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = text(value);
    if (className) element.className = className;
    return element;
  }
  function safeUrl(value) {
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) ? url.href : null;
    } catch (_) { return null; }
  }
  function paperLink(identity, papers) {
    const paper = papers.get(identity);
    const title = paper ? (text(paper.title) || identity) : identity;
    const href = paper && safeUrl(paper.url);
    if (!href) return title ? node("span", title) : ui("span", "未知论文");
    const link = node("a", title, "research-paper-link");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }
  function evidenceFor(value, papers) {
    return list(value).filter(ref => ref && papers.has(ref.paper_id) &&
      typeof ref.quote === "string" && ref.quote.trim());
  }
  function evidenceDetails(value, papers) {
    const refs = evidenceFor(value, papers);
    const details = node("details", undefined, "research-evidence");
    const count = new Set(refs.map(ref => ref.paper_id)).size;
    details.append(count ? ui("summary", "查看摘要依据（{count} 篇）", undefined, {count}) : ui("summary", "当前证据无法关联到清单"));
    const items = node("ul");
    for (const ref of refs) {
      const item = node("li");
      item.append(paperLink(ref.paper_id, papers), node("blockquote", ref.quote));
      item.append(ui("p", "摘要文字匹配；并非解释成立或实验可复现的证明。", "research-evidence-note"));
      items.append(item);
    }
    details.append(items);
    return details;
  }
  function claimRow(label, claim, papers, heading) {
    const row = node("div", undefined, "research-claim");
    row.append(ui("h" + heading, label));
    if (!claim || !text(claim.text)) {
      row.append(ui("p", "摘要未明确说明；不能据此断言论文没有这项内容。", "research-unknown"));
    } else if (!evidenceFor(claim.evidence, papers).length) {
      row.append(ui("p", "证据无法关联到当前论文清单，暂不展示这一解释。", "research-unknown"));
    } else {
      row.append(node("p", claim.text), evidenceDetails(claim.evidence, papers));
    }
    return row;
  }
  function gapText(gap, papers) {
    if (!gap || typeof gap !== "object") return "";
    if (gap.reason === "insufficient_grounded_proposals") {
      return t("可依据当前证据提出的假设：{found}/{requested}；未用无依据的设想补足数量。", {found: Number(gap.found) || 0, requested: Number(gap.requested) || 3});
    }
    if (gap.reason === "fewer_than_requested") return t("论文数量少于请求，综合只覆盖实际交付的清单。");
    if (gap.reason === "conflicting_source_abstracts") return t("来源摘要存在不一致，相关解释需要进一步核对。");
    if (gap.reason === "abstract_input_truncated") return t("部分摘要超出本轮输入上限，仅使用已提供的摘要片段。");
    const label = FIELDS[gap.section];
    const paper = papers.get(gap.paper_id);
    if (label && paper) return t("{paper}：{label}尚未由摘要建立。", {paper: text(paper.title) || gap.paper_id, label: t(label)});
    if (gap.section === "reading_order") return t("部分已选论文尚未有可用的阅读顺序建议。");
    if (gap.section === "themes") return t("当前证据不足以归纳跨论文共同主题。");
    if (gap.section === "open_questions") return t("当前证据不足以提出有依据的开放问题。");
    if (gap.section === "paper_notes") return t("部分论文缺少可用的逐篇研究整理。");
    return t("部分研究内容缺少摘要依据，未作补充推断。");
  }

  function render(brief, options = {}) {
    if (Array.isArray(options)) options = {papers: options};
    const papers = new Map(list(options.papers).filter(paper => paper &&
      typeof (paper.id || paper.paper_id) === "string").map(paper => [paper.id || paper.paper_id, paper]));
    const section = node("section", undefined, "research-brief");
    section.dataset.sgResearchAria = "";
    section._sgResearchAriaTranslation = () => section.setAttribute("aria-label", t("研究综合与待验证设想"));
    section._sgResearchAriaTranslation();
    const value = brief && typeof brief === "object" ? brief : {};
    const heading = [2, 3, 4].includes(options.headingLevel) ? options.headingLevel : 2;
    const groupHeading = Math.min(heading + 1, 6);
    const paperHeading = Math.min(heading + 2, 6);
    const claimHeading = Math.min(heading + 3, 6);
    section.append(ui("h" + heading, STATUS[value.status] || "研究综合"));
    section.append(ui("p", "证据范围：仅已选论文的摘要。作者报告、模型归纳与待验证设想分别展示；引文匹配只证明文字存在，不证明解释或实验结论。", "research-scope"));
    if (!["ready", "partial"].includes(value.status)) {
      section.append(ui("p", REASONS[value.reason] || "当前没有可展示的研究综合；论文推荐与已有证据不受影响。", "research-notice"));
      return section;
    }
    if (value.source_level !== "abstract_only") {
      section.append(ui("p", "当前综合的证据范围无法确认，暂不展示内容。", "research-notice"));
      return section;
    }

    const notes = node("div", undefined, "research-paper-notes");
    notes.append(ui("h" + groupHeading, "逐篇贡献、方法与证据边界"));
    for (const item of list(value.paper_notes)) {
      if (!item || !papers.has(item.paper_id)) continue;
      const article = node("article", undefined, "research-paper-note");
      const title = node("h" + paperHeading);
      title.append(paperLink(item.paper_id, papers));
      article.append(title);
      for (const key of ["contribution", "method", "author_limitation"]) {
        article.append(claimRow(FIELDS[key], item[key], papers, claimHeading));
      }
      if (item.model_inference) article.append(claimRow(FIELDS.model_inference, item.model_inference, papers, claimHeading));
      notes.append(article);
    }
    section.append(notes);

    if (list(value.themes).length) {
      const themes = node("div", undefined, "research-themes");
      themes.append(ui("h" + groupHeading, "技术路线与跨论文联系 · 模型归纳"));
      for (const theme of value.themes) {
        if (!theme || new Set(evidenceFor(theme.evidence, papers).map(ref => ref.paper_id)).size < 2) continue;
        const article = node("article");
        article.append(node("h" + paperHeading, theme.title), node("p", theme.text), evidenceDetails(theme.evidence, papers));
        themes.append(article);
      }
      section.append(themes);
    }
    if (list(value.open_questions).length) {
      const questions = node("div", undefined, "research-questions");
      questions.append(ui("h" + groupHeading, "可继续探索的问题 · 模型建议"));
      questions.append(ui("p", "这些问题由当前清单引出，不代表已检索完整领域或证明问题尚无人解决。"));
      const items = node("ul");
      for (const question of value.open_questions) {
        if (!question || !evidenceFor(question.evidence, papers).length) continue;
        const item = node("li");
        item.append(node("p", question.question), evidenceDetails(question.evidence, papers));
        items.append(item);
      }
      questions.append(items);
      section.append(questions);
    }
    const hypotheses = node("div", undefined, "research-hypotheses");
    hypotheses.append(ui("h" + groupHeading, "待检验研究假设 · 尚未验证"));
    hypotheses.append(ui("p", "以下实验、对照与衡量方式均为拟议建议，尚未执行；数据、代码和算力可得性需要另行检查。引用支持动机，不证明假设有效或新颖。", "research-scope"));
    const plans = node("ol");
    for (const hypothesis of list(value.research_hypotheses)) {
      if (!hypothesis || !evidenceFor(hypothesis.evidence, papers).length) continue;
      const item = node("li", undefined, "research-hypothesis");
      item.append(node("h4", hypothesis.hypothesis));
      const fields = node("dl");
      for (const [key, label] of [["rationale", "动机 · 模型推论"], ["experiment", "拟议实验"],
        ["baseline", "建议对照"], ["measure", "建议衡量方式"], ["falsifier", "何种结果会否定假设"]]) {
        fields.append(ui("dt", label), node("dd", hypothesis[key]));
      }
      item.append(fields, evidenceDetails(hypothesis.evidence, papers));
      plans.append(item);
    }
    hypotheses.append(plans);
    if (!plans.children.length) hypotheses.append(ui("p", "当前摘要证据不足以提出可追溯的实验假设。", "research-unknown"));
    section.append(hypotheses);

    const reading = node("div", undefined, "research-reading-order");
    reading.append(ui("h" + groupHeading, "建议阅读顺序 · 模型建议"));
    const order = node("ol");
    const seen = new Set();
    for (const entry of list(value.reading_order)) {
      if (!entry || !papers.has(entry.paper_id) || seen.has(entry.paper_id)) continue;
      seen.add(entry.paper_id);
      const item = node("li");
      item.append(paperLink(entry.paper_id, papers), node("p", entry.reason));
      order.append(item);
    }
    reading.append(order);
    if (!order.children.length) reading.append(ui("p", "当前没有可用的阅读顺序建议。", "research-unknown"));
    section.append(reading);
    if (list(value.gaps).length) {
      const gaps = node("details", undefined, "research-gaps");
      gaps.append(ui("summary", "查看证据缺口（{count}）", undefined, {count: value.gaps.length}));
      const items = node("ul");
      for (const gap of value.gaps) {
        const item = node("li");
        item.dataset.sgResearchTranslation = "";
        item._sgResearchTranslation = () => { item.textContent = gapText(gap, papers); };
        item._sgResearchTranslation(); items.append(item);
      }
      gaps.append(items);
      section.append(gaps);
    }
    return section;
  }

  function publication(paper) {
    const data = paper?.publication;
    if (!data || typeof data !== "object" || !Object.keys(data).length) return null;
    const row = node("p", undefined, "publication-info");
    const types = {conference:"会议", journal:"期刊", preprint:"预印本"};
    const states = {accepted:"已录用", published:"已发表", submitted:"投稿中", rejected:"未录用", withdrawn:"已撤稿", retracted:"已撤稿", desk_rejected:"未送审"};
    const parts = [types[data.type], data.verified ? states[data.status] : "出版状态待核对"];
    const presentation = data.verified && ["oral", "spotlight", "poster"].includes(data.presentation) ? data.presentation : null;
    const status = node("span");
    status.dataset.sgResearchTranslation = "";
    status._sgResearchTranslation = () => { status.textContent = [...parts.filter(Boolean).map(part => t(part)), presentation].filter(Boolean).join(" · "); };
    status._sgResearchTranslation(); row.append(status);
    const evidence = list(data.evidence).find(item => safeUrl(item?.url));
    if (evidence) {
      const link = ui("a", "出版依据");
      link.href = safeUrl(evidence.url); link.target = "_blank"; link.rel = "noopener noreferrer";
      row.append(document.createTextNode(" · "), link);
    }
    return row;
  }
  function requirement(gap, coverage) {
    if (gap.requirement === "research_coverage") {
      const labels = new Map(list(coverage?.facets).map(facet => [facet.id, facet.label]));
      const missing = list(gap.missing_facets).map(id => labels.get(id)).filter(Boolean);
      return missing.length ? t("技术路线仍有缺口：{facets}", {facets: missing.join("、")})
        : gap.missing_facets?.length ? t("技术路线仍缺 {count} 条", {count: gap.missing_facets.length}) : t("技术路线覆盖暂时无法核对");
    }
    const labels = {paper_count:"推荐数量", evaluation:"评测 / Benchmark", survey:"综述", research:"研究论文"};
    const count = gap.required != null ? `${gap.found ?? 0}/${gap.required}` : "";
    if (["mix", "constraints"].includes(gap.requirement)) return count ? t("{label} · 已交付 {count}", {label: text(gap.value), count}) : text(gap.value);
    const label = labels[gap.requirement] ? t(labels[gap.requirement]) : text(gap.value) || t("推荐条件");
    return count ? t("{label} {count} 篇", {label, count}) : t("{label}仍有缺口", {label});
  }

  function coverage(value, options = {}) {
    if (!value || typeof value !== "object") return null;
    const facets = list(value.facets).filter(facet => facet && text(facet.id) && text(facet.label));
    if (!facets.length) return null;
    const covered = new Set(list(value.covered));
    const missing = new Set(list(value.missing));
    const papers = new Map(list(options.papers).map(paper => [paper.id, paper]));
    const labels = new Map(facets.map(facet => [facet.id, facet.label]));
    const section = node("section", undefined, "research-coverage");
    section.append(ui(options.headingLevel === 3 ? "h3" : "h2", "技术路线覆盖"));
    section.append(ui("p", "基于摘要的模型分类，用于检查阅读面的覆盖；不代表领域穷尽或论文结论已验证。", "field-help"));
    const rows = node("ul", undefined, "coverage-facets");
    for (const facet of facets) {
      const state = covered.has(facet.id) ? "已关联论文" : missing.has(facet.id) ? "仍有缺口" : "待核对";
      rows.append(ui("li", "{label} · {state}", "badge " + (covered.has(facet.id) ? "yes" : "warn"), {label: facet.label, get state() { return t(state); }}));
    }
    section.append(rows);
    const assignments = list(value.assignments).filter(item => item && papers.has(item.paper_id) && text(item.evidence_quote));
    if (assignments.length) {
      const details = node("details", undefined, "research-evidence");
      details.append(ui("summary", "查看路线分类与摘要依据"));
      const items = node("ul");
      for (const assignment of assignments) {
        const item = node("li");
        const matched = list(assignment.facet_ids).map(id => labels.get(id)).filter(Boolean);
        item.append(paperLink(assignment.paper_id, papers), node("p", matched.join(" · "), "field-help"),
          node("blockquote", assignment.evidence_quote), ui("p", "引文为摘要文字匹配；路线归类仍是模型判断。", "field-help"));
        items.append(item);
      }
      details.append(items); section.append(details);
    }
    return section;
  }

  const SOURCE_LABELS = {arxiv: "arXiv", openalex: "OpenAlex", semantic_scholar: "Semantic Scholar",
    dblp: "DBLP", openreview: "OpenReview", acl_anthology: "ACL Anthology", deepseek: "DeepSeek", lune: "Lune Research"};
  function doiUrl(value) {
    const doi = text(value).trim().replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "").replace(/^doi:\s*/i, "");
    return /^10\.\d{4,9}\/[^\s<>]+$/i.test(doi) ? safeUrl("https://doi.org/" + doi) : null;
  }
  function citation(paper) {
    const authors = list(paper.authors).map(text).filter(Boolean).join(", ");
    const venue = text(paper.conference) || text(paper.venue);
    const tail = [paper.year, venue].filter(Boolean).join(", ");
    const locator = doiUrl(paper.doi) || safeUrl(paper.url) || safeUrl(paper.open_access_url);
    return `${text(paper.title) || text(paper.id) || "未知论文"}${authors ? " — " + authors : ""}${tail ? " (" + tail + ")" : ""}${locator ? "\n" + locator : ""}`;
  }
  function paperDetails(paper) {
    const details = node("details", undefined, "paper-details");
    details.append(ui("summary", "查看摘要与文献信息"));
    details.append(text(paper.abstract) ? node("p", paper.abstract, "paper-abstract") : ui("p", "当前来源未提供摘要。", "paper-abstract"));
    const authors = list(paper.authors).map(text).filter(Boolean);
    details.append(authors.length ? ui("p", "作者：{authors}", "field-help", {authors: authors.join(", ")}) : ui("p", "作者：来源未提供", "field-help"));
    if (paper.doi) {
      const row = node("p", undefined, "field-help");
      row.append(document.createTextNode("DOI："));
      const href = doiUrl(paper.doi);
      if (href) {
        const link = node("a", text(paper.doi));
        link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer";
        row.append(link);
      } else row.append(document.createTextNode(text(paper.doi)));
      details.append(row);
    }
    if (paper.abstract_source) details.append(ui("p", "摘要来源：{source}", "field-help", {source: SOURCE_LABELS[paper.abstract_source] || paper.abstract_source}));
    details.append(ui("p", "论文 ID：{id}", "field-help", {get id() { return text(paper.id) || t("未知"); }}));
    return details;
  }
  function jumpTo(target) {
    if (!target) return;
    target.focus({preventScroll: true});
    target.scrollIntoView({block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"});
    document.querySelector(".result-arrival")?.remove();
  }

  // Use only emitted counts. A quote match is not a recommendation decision.
  function progress(events, options = {}) {
    const rows = list(events);
    let candidates = null, assessed = null, verified = null, eligible = null;
    const number = value => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
    for (const event of rows) {
      const p = event.payload || event;
      if (["search", "search_result"].includes(event.action)) {
        if (number(p.unique_candidates) !== null) candidates = p.unique_candidates;
        else if (number(p.new) !== null || number(p.new_candidates) !== null) {
          candidates = (candidates || 0) + (number(p.new) ?? number(p.new_candidates));
        }
      }
      if (event.action === "assess") {
        if (number(p.assessed) !== null) assessed = (assessed || 0) + p.assessed;
        if (number(p.verified) !== null) verified = (verified || 0) + p.verified;
        if (number(p.eligible) !== null) eligible = p.eligible;
      }
    }
    const host = node("div", undefined, "run-progress");
    const counts = [[candidates, options.chat ? "本轮新增候选" : "候选论文"], [assessed, "次摘要核对"],
      eligible !== null ? [eligible, "当前可推荐"] : [verified, "次引文匹配"]].filter(([value]) => value !== null);
    for (const [value, label] of counts) {
      const item = node("span", undefined, "run-fact");
      item.append(node("b", String(value)), ui("span", label)); host.append(item);
    }
    if (!counts.length) host.append(ui("span", "模型与来源返回后，这里会逐步更新实际进展。", "field-help"));
    return host;
  }

  function readingNavigation(papers, targets, prefix) {
    if (papers.length < 2) return null;
    const nav = node("nav", undefined, "reading-nav");
    nav.id = prefix + "-reading"; nav.tabIndex = -1;
    nav.dataset.sgResearchAria = "";
    nav._sgResearchAriaTranslation = () => nav.setAttribute("aria-label", t("论文阅读目录"));
    nav._sgResearchAriaTranslation();
    nav.append(ui("p", "跳转阅读 · {count} 篇", "reading-nav-label", {count: papers.length}));
    const items = node("ol");
    papers.forEach((paper, i) => {
      const target = targets[i];
      if (!target) return;
      target.id = prefix + "-paper-" + (i + 1); target.tabIndex = -1;
      const item = node("li"), link = node("a");
      link.href = "#" + target.id;
      link.append(node("span", String(i + 1).padStart(2, "0"), "reading-nav-index"),
        node("span", text(paper.title) || text(paper.id), "reading-nav-title"));
      link.addEventListener("click", event => { event.preventDefault(); jumpTo(target); });
      item.append(link); items.append(item);
      const back = ui("button", "返回论文目录 ↑", "btn-text reading-back");
      back.type = "button";
      back.addEventListener("click", () => jumpTo(nav));
      (target.querySelector(".paper-body") || target.lastElementChild || target).append(back);
    });
    nav.append(items);
    return nav;
  }

  function resultNotice(target, message) {
    document.querySelector(".result-arrival")?.remove();
    const notice = node("div", undefined, "result-arrival");
    notice.setAttribute("role", "status");
    notice.append(node("span", message));
    const button = ui("button", "查看结果 ↓", "btn-text");
    button.type = "button";
    button.addEventListener("click", () => jumpTo(target));
    const close = node("button", "×", "icon-btn");
    close.type = "button";
    close.dataset.sgResearchAria = "";
    close._sgResearchAriaTranslation = () => close.setAttribute("aria-label", t("关闭结果提示"));
    close._sgResearchAriaTranslation();
    close.addEventListener("click", () => notice.remove());
    notice.append(button, close); document.body.append(notice);
  }

  window.ScholarGuideResearch = Object.freeze({render, publication, citation, paperDetails, coverage, requirement,
    progress, readingNavigation, resultNotice});
})();
