'use strict';
const $ = id => document.getElementById(id);
const t = (key, params) => window.SGI18n?.t(key, params) ?? key.replace(/\{(\w+)\}/g, (_, name) => params?.[name] ?? `{${name}}`);
const tr = (key, ...values) => t(key).replace(/\{(\d+)\}/g, (match, index) => Number(index) < values.length ? String(values[Number(index)]) : match);
function setLiveText(node, key, ...values) {
  delete node.dataset.i18n;
  node.dataset.i18nLive = key;
  node.dataset.i18nArgs = JSON.stringify(values);
  node.textContent = tr(key, ...values);
}
function refreshLiveText() {
  document.querySelectorAll('[data-i18n-live]').forEach(node => {
    let values = [];
    try { values = JSON.parse(node.dataset.i18nArgs || '[]'); } catch (_) {}
    node.textContent = tr(node.dataset.i18nLive, ...values);
  });
}
const state = {mode: 'live', natural: false, job: null, polling: false, busy: false, started: 0, timer: null, errors: 0, traceCount: 0, sourceFilter: 'all', sourceChosen: false, lastResult: null, lastPayload: null, config: null, configError: '', configLoading: false, configLoaded: false, draftRestored: false, stopRequested: false, focusResult: false, recovery: null};
const names = {none: '不额外指定类别', evaluation: '评测 / Benchmark', survey: '综述 / Survey', research: '研究论文'};
const GOAL_LABELS = {auto: '自动判断', orientation: '新手入门', comparison: '方法对比',
  frontier: '前沿 idea', general: '通用'};
const TYPE_LABELS = {survey: '综述', benchmark: 'Benchmark', research: '研究论文', unknown: '类型未知'};
const SOURCE_LABELS = {multi: '多源', arxiv: 'arXiv', openalex: 'OpenAlex', semantic_scholar: 'Semantic Scholar',
  dblp: 'DBLP', openreview: 'OpenReview', acl_anthology: 'ACL Anthology', deepseek: 'DeepSeek', lune: "Lune Research"};
const sourceLabel = value => SOURCE_LABELS[value] ? t(SOURCE_LABELS[value]) : value;
const categoryName = value => names[value] ? t(names[value]) : value;
const goalName = value => GOAL_LABELS[value] ? t(GOAL_LABELS[value]) : value;
const typeName = value => TYPE_LABELS[value] ? t(TYPE_LABELS[value]) : value;
const stageOf = {provider: 'plan', intent: 'plan', plan: 'plan', task_initial: 'plan',
  task_update: 'plan', research_plan: 'plan', search: 'search', search_result: 'search',
  search_failed: 'search', search_partial: 'search', refine: 'search', assess: 'assess',
  assessment_reuse: 'assess', assessment_sufficient: 'assess', research_coverage: 'assess',
  check: 'assess', delivery_check: 'assess', synthesis: 'done', delivered: 'done'};
const stageNames = {plan: '理解与规划', search: '检索论文', assess: '核对证据', done: '整理与交付'};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
const liveComputations = new Map();
function liveComputed(node, render) {
  delete node.dataset.i18n; delete node.dataset.i18nLive;
  liveComputations.set(node, render);
  render();
}
function liveElement(tag, key, className, ...values) {
  const node = element(tag, undefined, className);
  setLiveText(node, key, ...values);
  return node;
}
function showError(message, retry = true) {
  $('form-error').textContent = t(message);
  $('form-error-row').hidden = !message;
  $('retry-search').hidden = !message || !retry;
}
const INTERRUPTED_JOBS_KEY = 'scholarguide-interrupted-searches';
function interruptedJobs() {
  try {
    const jobs = JSON.parse(sessionStorage.getItem(INTERRUPTED_JOBS_KEY) || '[]');
    return Array.isArray(jobs) ? jobs.filter(job => /^[a-f0-9]{32}$/.test(job.id) && ['live', 'demo'].includes(job.mode)) : [];
  } catch (_) { return []; }
}
function saveInterruptedJobs(jobs) {
  try { sessionStorage.setItem(INTERRUPTED_JOBS_KEY, JSON.stringify(jobs)); } catch (_) {}
  $('detached-recovery-note').hidden = jobs.length === 0;
}
function rememberInterrupted(job) {
  if (!job?.id || !/^[a-f0-9]{32}$/.test(job.id)) return;
  saveInterruptedJobs([...interruptedJobs().filter(item => item.id !== job.id), job]);
}
function forgetInterrupted(id) {
  saveInterruptedJobs(interruptedJobs().filter(item => item.id !== id));
}
function emptyContext(reason) {
  const demo = state.mode === 'demo';
  setLiveText($('empty-title'), reason === 'changed' ? '设置已更改，等待重新运行。'
    : demo ? '选择一个离线演示场景。' : '准备在线搜索。');
  setLiveText($('empty-description'), demo
    ? '选择演示场景并点击运行。之前的结果仍保存在 Activity；切换不会自动执行。'
    : '填写研究需求后点击发现论文。之前的结果仍保存在 Activity；切换不会自动发起在线调用。');
  $('empty-demo-action').hidden = demo;
  $('detached-recovery-note').hidden = interruptedJobs().length === 0;
}
function clearSearchView(reason = 'changed') {
  if (state.busy) return;
  if (state.job || state.lastResult || state.recovery) {
    if (state.recovery && state.job) rememberInterrupted({id: state.job, mode: state.mode, started: state.started});
    state.stream?.close(); state.stream = null; state.streamJob = null; state.streamEvents = [];
    state.job = null; state.lastResult = null; state.lastPayload = null; state.previousResult = null;
    state.recovery = null; state.sourceFilter = 'all'; state.traceCount = 0; state.focusResult = false;
    forgetJob();
    $('paper-list').replaceChildren(); $('source-filter').replaceChildren();
    $('trace-list').replaceChildren();
    liveComputations.clear();
    document.querySelector('#result-section > .reading-nav')?.remove();
    document.querySelector('#result-section > .research-brief')?.remove();
    document.querySelector('#result-section > .research-coverage')?.remove();
    setLiveText($('trace-count'), '空闲');
    $('trace-placeholder').hidden = false; $('search-progress').replaceChildren();
    setLiveText($('result-count'), '—');
    $('open-activity-link').href = '/activity';
    document.querySelector('.result-arrival')?.remove();
    updateStages([]);
    $('running-state').hidden = true; $('result-section').hidden = true; $('downloads').hidden = true;
    $('job-recovery').hidden = true; $('reconnect').hidden = true;
    $('empty-state').hidden = false;
  }
  emptyContext(reason);
}
function setMode(mode) {
  if (state.busy) return;
  const previous = state.mode;
  // Preserve the original mode for a detached interrupted task.
  if (previous !== mode) clearSearchView('mode');
  state.mode = mode;
  if (previous !== mode) emptyContext('mode');
  ['live', 'demo'].forEach(value => {
    $(value + '-tab').classList.toggle('active', value === mode);
    $(value + '-tab').setAttribute('aria-pressed', String(value === mode));
    $(value + '-fields').hidden = value !== mode;
  });
  $('live-settings').hidden = mode === 'demo';
  $(mode === 'demo' ? 'demo-primary-slot' : 'search-primary-slot').append($('submit-button').closest('.search-actions'));
  setLiveText($('submit-text'), mode === 'demo' ? '运行演示' : '发现论文');
  syncForm();
  saveDraft();
  showError('');
}
function setBusy(busy) {
  state.busy = busy;
  $('form-fields').disabled = busy;
  $('live-tab').disabled = busy;
  $('demo-tab').disabled = busy;
  $('try-demo').disabled = busy;
  $('readiness-demo').disabled = busy;
  $('toggle-input').disabled = busy;
  $('result-section').setAttribute('aria-busy', String(busy));
  $('cancel-search').disabled = !state.job || state.stopRequested || !busy;
  setLiveText($('submit-text'), busy ? '正在发现…' : state.mode === 'demo' ? '运行演示' : '发现论文');
  if (busy) setLiveText($('result-count'), '· 检索中');
  if (busy && !state.timer) {
    state.timer = setInterval(() => {
      $('elapsed').textContent = Math.floor((Date.now() - state.started) / 1000) + 's';
    }, 1000);
  } else if (!busy && state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  syncForm();
}
// The workflow may revisit earlier phases. A highlight represents the most
// recently observed recognizable event, never a percentage or proof that the
// earlier phases completed. Unknown events leave the last observed phase intact.
function eventStage(event) {
  if (!event || typeof event !== 'object') return null;
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const action = event.action || '';
  if (stageOf[action]) return stageOf[action];
  if (action === 'agent_node') {
    return {prepare: 'plan', planner: 'plan', retrieve: 'search', search: 'search',
      assess: 'assess', verifier: 'assess', finalize: 'done', deliver: 'done'}[payload.node || event.node] || null;
  }
  if (['tool_start', 'tool_end', 'tool_error'].includes(action)) {
    const category = payload.category || event.category || '';
    const tool = String(payload.tool || event.tool || '');
    if (category === 'source' || ['search', 'http.request'].includes(tool)) return 'search';
    if (category === 'model' || tool.startsWith('model.')) {
      return {'model.update': 'plan', 'model.plan': 'plan', 'model.research_plan': 'plan',
        'model.assess': 'assess', 'model.research_review': 'assess', 'model.synthesis': 'done'}[tool] || null;
    }
  }
  return null;
}
function updateStages(events) {
  const seen = new Set();
  let latest = null;
  for (const event of Array.isArray(events) ? events : []) {
    const stage = eventStage(event);
    if (stage) { seen.add(stage); latest = stage; }
  }
  document.querySelectorAll('#stagebar .stage').forEach(node => {
    const key = node.dataset.stage;
    node.classList.toggle('active', key === latest);
    node.classList.toggle('seen', seen.has(key) && key !== latest);
    const status = node.querySelector('.stage-state');
    const label = node.querySelector('.stage-label, :scope > span');
    const labelText = t(stageNames[key]);
    if (label && label.textContent !== labelText) label.textContent = labelText;
    const text = t(key === latest ? '最近阶段' : seen.has(key) ? '已记录' : '尚未记录');
    if (status && status.textContent !== text) status.textContent = text;
  });
  const caption = latest ? t('最近可识别阶段：{stage}。阶段可能循环，不代表完成比例。', {stage: t(stageNames[latest])})
    : t('等待真实运行事件；阶段可能循环，不代表完成比例。');
  if ($('stage-caption').textContent !== caption) $('stage-caption').textContent = caption;
}
async function api(url, options = {}) {
  let response;
  try { response = await fetch(url, {...options, signal: AbortSignal.timeout(15000)}); }
  catch (_) { throw new Error(t('暂时无法连接本地服务。请确认启动服务的终端仍在运行。')); }
  let value;
  try { value = await response.json(); } catch (_) { throw new Error(t('服务返回了无法读取的响应。')); }
  if (!response.ok) {
    const error = new Error(value.error || t('请求未能完成。'));
    error.status = response.status;
    throw error;
  }
  return value;
}
function inputPayload() {
  const shared = {mode: state.mode, method: $('method').value};
  if (state.mode === 'demo') return {...shared, scenario: $('scenario').value};
  let request = $('request').value.trim();
  let constraints;
  let researchPreferences;
  if (!state.natural) {
    const topic = $('topic').value.trim();
    if (!topic) throw inputError('请先填写研究主题，或选择一个示例。', 'topic');
    const quantity = window.ScholarGuideTrace.quantity(topic);
    if (!quantity.valid) throw inputError('每次推荐支持 1–10 篇；请在需求中调整篇数。', 'topic');
    const target = quantity.count;
    const category = $('category').value;
    const minimum = category === 'none' ? 0 : Number($('minimum').value);
    if (category !== 'none' && (!Number.isInteger(minimum) || minimum < 1 || minimum > target)) {
      throw inputError('类别最低篇数不能超过推荐总数，请调整数量或类别篇数。', 'minimum');
    }
    request = topic;
    if (category !== 'none') {
      const categoryText = {evaluation: '评测方法或 benchmark', survey: '相关研究的综述（survey）',
        research: '研究论文（research paper）'}[category] || category;
      request += `其中至少 ${minimum} 篇主要讨论${categoryText}。`;
    }
    const goal = $('reading-goal').value;
    if (goal && goal !== 'auto') request += ` 阅读目标：${GOAL_LABELS[goal]}。`;
    const venue = $('venue').value.trim();
    if (venue) request += ` 发表在 ${venue}。`;
    const from = $('year-from').value.trim();
    const to = $('year-to').value.trim();
    for (const [value, field] of [[from, 'year-from'], [to, 'year-to']]) {
      if (value && (!/^\d{4}$/.test(value) || Number(value) < 1900 || Number(value) > 2100)) {
        throw inputError('年份需填写 1900–2100 之间的整数。', field);
      }
      if ($(field).validity.badInput) throw inputError('请输入有效的年份。', field);
    }
    if (from && to && Number(from) > Number(to)) throw inputError('开始年份不能晚于结束年份。', 'year-from');
    if (from && to) request += ` 发表年份为 ${from}-${to}。`;
    else if (from) request += ` 发表年份不早于 ${from} 年。`;
    else if (to) request += ` 发表年份不晚于 ${to} 年。`;
    constraints = {...(quantity.explicit && !quantity.interval ? {target_count:target} : {}), category, minimum_category_count: minimum,
      venue, year_from: from ? Number(from) : null, year_to: to ? Number(to) : null};
    const presentations = ['oral', 'spotlight', 'poster'].filter(value => $('presentation-' + value).checked);
    const scope = $('venue-scope').value;
    const kind = $('publication-type').value;
    const accepted = $('accepted-only').checked || presentations.length > 0;
    if (scope !== 'any' || kind !== 'any' || accepted || presentations.length) {
      researchPreferences = {venue_scope: scope, publication_type: kind,
        accepted_only: accepted, presentation_types: presentations};
      if (scope === 'preferred') request += ' 优先考虑本系统优选AI/CS研究池中的venue。';
      if (scope === 'preferred_only') request += ' 仅考虑本系统优选AI/CS研究池中的venue。';
      if (kind !== 'any') request += ` 出版类型仅限${({conference: '会议论文', journal: '期刊论文', preprint: '预印本'})[kind]}。`;
      if (accepted) request += ' 需要可核验的录用或发表依据。';
      if (presentations.length) request += ` OpenReview展示类别为${presentations.join('或')}。`;
    }
  }
  if (!request) throw inputError('请填写研究需求，说明主题和你希望读到什么。', 'request');
  return {...shared, request, source: $('source').value, cache: $('cache').checked,
          reading_goal: $('reading-goal').value,
          research_mode: true,
          ...(constraints ? {request_constraints: constraints} : {}),
          ...(researchPreferences ? {research_preferences: researchPreferences} : {})};
}
function focusField(id) {
  const field = $(id);
  for (let parent = field?.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS') parent.open = true;
  }
  field?.focus();
}
function inputError(message, field) {
  const error = new Error(t(message));
  error.field = field;
  return error;
}
function syncForm() {
  const demo = state.mode === 'demo';
  document.querySelectorAll('.guided-option').forEach(node => { node.hidden = state.natural; });
  const hasQuota = !state.natural && $('category').value !== 'none';
  $('minimum-field').hidden = !hasQuota;
  $('minimum').disabled = !hasQuota;
  $('quota-help').hidden = !hasQuota;
  ['topic', 'request', 'minimum', 'year-from', 'year-to'].forEach(id => $(id).removeAttribute('aria-invalid'));
  let payload, error;
  try { payload = inputPayload(); } catch (value) { error = value; }
  const hasInput = $('topic').value.trim() || $('request').value.trim() || $('year-from').value || $('year-to').value;
  $('input-feedback').textContent = !demo && error && hasInput ? error.message : '';
  if (!demo && error && hasInput && error.field) $(error.field).setAttribute('aria-invalid', 'true');
  $('request-preview').hidden = demo || !payload;
  const goal = $('reading-goal').value;
  const goalDescription = goal === 'auto'
    ? t('自动判断阅读目的；按实际需求搭配论文类型。')
    : tr('已手选「{0}」，将覆盖需求文本中的阅读目的。', goalName(goal));
  setLiveText($('goal-help'), '{0} 阅读目的影响排序；你指定的类别最低篇数是硬要求。', goalDescription);
  setLiveText($('topic-help'), state.natural
    ? '数量、类别和范围以完整需求文本为准；原表单筛选未叠加。Ctrl / ⌘ + Enter 开始。'
    : '中英文均可，篇数直接写在需求里，支持 1–10 篇，未指定默认 5 篇。Ctrl / ⌘ + Enter 开始。');
  $('request-preview-text').textContent = payload ? `${payload.request}\n\n${goalDescription}\n${tr('来源：{0} · {1}', sourceLabel($('source').value), t($('method').value === 'S' ? '只检索一轮' : '有缺口再补搜，最多两轮'))}` : '';
  const venue = $('venue').value.trim();
  const from = $('year-from').value;
  const to = $('year-to').value;
  const years = !demo && error && ['year-from', 'year-to'].includes(error.field)
    ? t('年份条件无效，请修正')
    : from && to ? tr('{0}–{1} 年', from, to) : from ? tr('{0} 年及以后', from) : to ? tr('{0} 年及以前', to) : t('不限年份');
  if (state.natural) setLiveText($('search-scope-summary'), '数量、类别与检索范围按完整需求解析，原表单筛选未叠加。');
  else {
    const count = window.ScholarGuideTrace.quantity($('topic').value).count;
    const quota = hasQuota ? tr(' · 其中至少 {0} 篇{1}', $('minimum').value, categoryName($('category').value)) : '';
    setLiveText($('search-scope-summary'), '推荐 {0} 篇（由需求解析，上限 10） · {1} · {2}{3}', count, venue || t('不限会议或期刊'), years, quota);
  }
  const ready = Boolean(state.config?.ready);
  $('submit-button').disabled = state.busy || (!demo && (!ready || Boolean(error)));
  $('readiness-actions').hidden = demo || ready || state.busy;
  $('retry-config').disabled = state.configLoading;
  setLiveText($('retry-config'), state.configLoading ? '正在检查…' : '重新检查配置');
  $('search-readiness').classList.toggle('is-ready', demo || ready);
  if (demo) setLiveText($('search-readiness'), '离线演示已就绪 · 固定 RAG 样例，无需配置模型');
  else if (state.configError) setLiveText($('search-readiness'), state.configError);
  else if (!state.config) setLiveText($('search-readiness'), '正在检查模型配置…');
  else if (ready) setLiveText($('search-readiness'), '{0} · {1} · 配置已就绪', state.config.provider, state.config.model);
  else if (state.config.message) $('search-readiness').textContent = state.config.message;
  else setLiveText($('search-readiness'), '先在 Models 中配置一个模型，或使用离线演示。');
  if (demo) setLiveText($('mode-note'), '演示不调用模型或学术 API；展示的判断由固定脚本产生。');
  else setLiveText($('mode-note'), '在线搜索可能产生 API 费用 · {0} · 摘要级推荐', t($('method').value === 'S' ? '只检索一轮' : '必要时补搜，最多两轮'));
  const selectedPreferences = [
    ...(!state.natural ? [
      $('category').value !== 'none', Boolean(venue), Boolean(from || to),
      $('venue-scope').value !== 'any', $('publication-type').value !== 'any', $('accepted-only').checked,
      ...['oral', 'spotlight', 'poster'].map(value => $('presentation-' + value).checked),
    ] : []),
    $('method').value !== 'A', goal !== 'auto', $('source').value !== 'multi', !$('cache').checked,
  ].filter(Boolean).length;
  setLiveText($('advanced-label'), state.natural ? '调整阅读目的与检索偏好' : '调整数量、范围与检索偏好');
  setLiveText($('advanced-count'), selectedPreferences ? '{0} 项已调整' : '可选', selectedPreferences);
  const publicationSummary = [
    $('venue-scope').value === 'preferred' ? t('优选研究池优先') : $('venue-scope').value === 'preferred_only' ? t('仅限优选研究池') : '',
    ({conference:t('会议论文'),journal:t('期刊论文'),preprint:t('预印本')})[$('publication-type').value],
    $('accepted-only').checked ? t('需录用/发表依据') : '',
    ...['oral', 'spotlight', 'poster'].filter(value => $('presentation-' + value).checked),
  ].filter(Boolean);
  if (!state.natural && publicationSummary.length) $('search-scope-summary').textContent += ' · ' + publicationSummary.join(' · ');
  const source = $('source').value;
  const sourceHelp = source === 'dblp' ? 'DBLP 主要提供元数据；缺少摘要的独立记录无法生成有证据的推荐。'
    : source === 'deepseek' ? '网页发现结果可能缺少摘要；可选多源搜索以补充学术记录。'
    : source === 'multi' ? '多个来源参与检索，覆盖更广，也可能增加等待时间。会议与年份要求仍需元数据支持。'
    : source === 'arxiv' ? 'arXiv 提供预印本摘要；arXiv 收录本身不代表会议录用。'
    : '根据来源返回的标题、摘要与元数据进行推荐；缺失的信息会明确标注。';
  setLiveText($('source-help'), sourceHelp);
}
function saveDraft() {
  if (state.busy) return;
  const values = {};
  for (const id of ['topic', 'request', 'category', 'minimum', 'method', 'reading-goal', 'venue', 'year-from', 'year-to', 'source', 'scenario', 'venue-scope', 'publication-type']) values[id] = $(id).value;
  const researchChecks = {};
  for (const id of ['accepted-only', 'presentation-oral', 'presentation-spotlight', 'presentation-poster']) researchChecks[id] = $(id).checked;
  try { sessionStorage.setItem('scholarguide-search-draft', JSON.stringify({values, natural: state.natural, mode: state.mode, cache: $('cache').checked, researchChecks, sourceChosen: state.sourceChosen, advancedOpen: $('advanced-settings').open})); } catch (_) {}
}
function setInputMode(natural, focus = true) {
  state.natural = natural;
  $('guided-fields').hidden = natural;
  $('natural-fields').hidden = !natural;
  $('toggle-input').textContent = t(natural ? '改用主题与类别' : '改用完整需求');
  $('toggle-input').setAttribute('aria-expanded', String(natural));
  $('topic-label').htmlFor = natural ? 'request' : 'topic';
  $('topic-label').textContent = t(natural ? '完整研究需求' : '研究主题');
  syncForm();
  if (focus) $(natural ? 'request' : 'topic').focus();
}
function traceDetail(event) {
  if (event.action === 'synthesis') return t(event.status === 'preparing'
    ? '只基于已核验的论文整理方法脉络与开放问题。'
    : '研究整理已完成；没有依据的内容会保留为缺口。');
  if (event.action === 'research_plan') return event.facets?.length
    ? tr('计划比较 {0} 条研究路线 · 摘要级分类', event.facets.length) : t('按当前主题确定检索范围');
  if (event.action === 'research_coverage') return tr('已记录 {0} 篇候选的路线归类 · 继续核对推荐条件', (event.assignments || []).length);
  if (event.action === 'provider') {
    return tr('profile {0} · model {1} · {2}', event.profile_name || event.profile_id || '—', event.model || '—', event.protocol || '—');
  }
  if (event.action === 'intent') {
    return tr('目标 {0} · policy {1} · {2}', goalName(event.goal) || event.goal || '—', event.policy || '—', event.mix || '—');
  }
  if (event.query) {
    if (event.router && event.router.batches) {
      const runs = event.router.batches.reduce((sum, batch) => sum + (batch.runs || []).length, 0);
      const providers = (event.router.plan && event.router.plan.providers || []).map(sourceLabel);
      return tr('{0} · {1} · {2} 个源请求', event.query, providers.join(' / '), runs);
    }
    return event.query;
  }
  if (event.action === 'search_result') return tr('返回 {0} 篇 · 新增 {1} 篇 · 共 {2} 篇候选', event.returned, event.new_candidates, event.unique_candidates);
  if (event.action === 'assess') return tr('本批检查 {0} 篇 · 已有 {1} 篇可推荐', event.assessed, event.eligible);
  if (event.action === 'check') return event.gaps?.length
    ? event.gaps.map(g => window.ScholarGuideResearch.requirement(g)).join('；')
    : t('当前清单满足已解析的数量与类别要求');
  if (event.action === 'stop') return t(({
    complete: '按当前模型判断满足需求',
    partial: '返回已有结果与剩余缺口',
    no_results: '没有找到足够依据的论文',
    unsupported: '有暂不支持的需求',
    failed: '运行未完成'
  })[event.status] || event.status);
  return event.error || '';
}
function renderTrace(trace, done = false) {
  $('trace-placeholder').hidden = trace.length > 0;
  $('trace-count').textContent = t('{count} 个真实事件', {count: trace.length});
  const timeline = window.ScholarGuideTrace.render(trace);
  $('trace-list').replaceChildren(...timeline.children);
  state.traceCount = trace.length;
  $('search-progress').replaceChildren(window.ScholarGuideResearch.progress(trace));
  updateStages(trace);
  const last = trace.at(-1);
  if (last) {
    setLiveText($('running-title'), done ? '本次执行已结束' : window.ScholarGuideTrace.label(last));
    const detail = traceDetail(last) || (last.payload?.source ? tr('来源：{0}', sourceLabel(last.payload.source)) : '实际执行过程会持续更新。');
    setLiveText($('running-detail'), detail);
  }
}
function safeLink(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; }
  catch (_) { return null; }
}
async function copyText(text, button) {
  const originalKey = button.dataset.i18n || button.textContent;
  const focused = document.activeElement;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else {
      const area = document.createElement('textarea');
      area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.append(area); area.select();
      const copied = document.execCommand('copy'); area.remove();
      if (focused?.isConnected) focused.focus();
      if (!copied) throw new Error('clipboard unavailable');
    }
    setLiveText(button, '已复制');
    setLiveText($('copy-status'), '已复制到剪贴板：{0}', t(originalKey));
  } catch (_) {
    setLiveText(button, '复制失败');
    setLiveText($('copy-status'), '无法使用剪贴板，可展开摘要后手动选择文字，或下载完整报告。');
  }
  setTimeout(() => { setLiveText(button, originalKey); }, 1200);
}
function citationText(paper) {
  return window.ScholarGuideResearch.citation(paper);
}
function readingListText(result) {
  const papers = result.papers || [];
  return [result.mode === 'demo' ? 'ScholarGuide · 离线演示清单（预设判断）' : 'ScholarGuide · 推荐清单',
    '研究需求：' + (result.request?.topic || result.user_input || ''),
    '推荐依据为摘要级模型判断；引文校验仅表示文字出现在摘要中。', '',
    ...papers.map((paper, index) => `${index + 1}. ${citationText(paper, result.request || {})}\n${paper.reason || ''}${paper.evidence_quote ? '\n摘要引文：“' + paper.evidence_quote + '”' : ''}`),
  ].join('\n\n');
}
function provenanceRow(paper) {
  const row = element('div', undefined, 'paper-provenance');
  const sources = (paper.sources && paper.sources.length ? paper.sources : [paper.source]).filter(Boolean);
  for (const source of sources) row.append(element('span', sourceLabel(source), 'source-badge'));
  if (paper.metadata_only) row.append(liveElement('span', '仅元数据', 'badge no'));
  if (paper.provenance_conflict) row.append(liveElement('span', '来源摘要不一致', 'badge warn'));
  const oa = safeLink(paper.open_access_url);
  if (oa) {
    const link = liveElement('a', 'OA 全文', 'paper-link');
    link.href = oa; link.target = '_blank'; link.rel = 'noopener noreferrer';
    row.append(link);
  }
  if (paper.doi) {
    const doi = safeLink('https://doi.org/' + paper.doi);
    if (doi) {
      const link = element('a', 'DOI', 'paper-link');
      link.href = doi; link.target = '_blank'; link.rel = 'noopener noreferrer';
      row.append(link);
    }
  }
  return row.children.length ? row : null;
}

function paperCard(paper, index, request) {
  const article = element('article', undefined, 'paper');
  article.dataset.paperId = paper.id || '';
  article.setAttribute('aria-label', `第 ${index + 1} 篇：${paper.title || paper.id}`);
  article.dataset.sources = (paper.sources && paper.sources.length
    ? paper.sources : [paper.source]).filter(Boolean).join(' ');
  article.append(element('span', String(index + 1).padStart(2, '0'), 'paper-index'));
  const body = element('div', undefined, 'paper-body');
  const heading = element('h3');
  const url = safeLink(paper.url);
  if (url) {
    const link = element('a', paper.title || paper.id);
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    heading.append(link);
  } else heading.textContent = paper.title || paper.id;
  body.append(heading);

  const meta = element('p', undefined, 'paper-meta');
  const bits = [];
  if (paper.authors?.length) bits.push(paper.authors.slice(0, 2).join(', ') + (paper.authors.length > 2 ? ' 等' : ''));
  if (paper.year) bits.push(String(paper.year));
  if (paper.conference || paper.venue) bits.push(paper.conference || paper.venue);
  bits.push(paper.id);
  bits.forEach((bit, i) => {
    if (i) meta.append(element('span', '·', 'sep'));
    meta.append(document.createTextNode(bit));
  });
  body.append(meta);
  const publication = window.ScholarGuideResearch?.publication(paper);
  if (publication) body.append(publication);
  const provenance = provenanceRow(paper);
  if (provenance) body.append(provenance);
  const reason = element('p', undefined, 'paper-reason');
  reason.append(liveElement('span', '为什么推荐 · ', 'reason-label'), document.createTextNode(paper.reason || t('暂无推荐理由。')));
  body.append(reason);

  if (paper.evidence_quote) {
    const block = element('div', undefined, 'evidence-block');
    const quote = element('blockquote', paper.evidence_quote);
    const long = paper.evidence_quote.length > 180;
    if (long) quote.classList.add('is-clamped');
    block.append(quote);
    if (long) {
      const toggle = liveElement('button', '展开引文', 'btn-text quote-toggle');
      toggle.dataset.i18n = '展开引文';
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', 'false');
      toggle.addEventListener('click', () => {
        const clamped = quote.classList.toggle('is-clamped');
        setLiveText(toggle, clamped ? '展开引文' : '收起引文');
        toggle.setAttribute('aria-expanded', String(!clamped));
      });
      block.append(toggle);
    }
    const quoteCopy = liveElement('button', '复制引文', 'btn-text');
    quoteCopy.dataset.i18n = '复制引文';
    quoteCopy.type = 'button';
    quoteCopy.dataset.copyQuote = '';
    quoteCopy.addEventListener('click', () => copyText(paper.evidence_quote, quoteCopy));
    block.append(quoteCopy);
    body.append(block);
  } else {
    body.append(liveElement('p', '缺少可核验引文：这篇论文未通过摘要引文检查，未计入推荐依据。可调整主题或回到检索重试。', 'evidence-missing'));
  }

  const actionsRow = element('div', undefined, 'paper-actions');
  if (paper.paper_type && paper.paper_type !== 'unknown') {
    actionsRow.append(TYPE_LABELS[paper.paper_type] ? liveElement('span', TYPE_LABELS[paper.paper_type], 'badge type') : element('span', paper.paper_type, 'badge type'));
  }
  if (request.category && request.category !== 'none') {
    const status = paper.category_status;
    const badge = element('span', undefined, 'badge ' + (status === 'yes' ? 'yes' : 'no'));
    liveComputed(badge, () => {
      badge.textContent = status === 'yes' ? `${categoryName(request.category)} · ${t('模型判断')}` : t(status === 'unknown' ? '类别待确认' : '其他相关论文');
    });
    actionsRow.append(badge);
  }
  actionsRow.append(liveElement('span', paper.evidence_verified ? '引文已校验' : '引文未校验',
    'badge ' + (paper.evidence_verified ? 'verified' : 'no')));
  const copy = liveElement('button', '复制引用', 'btn-text');
  copy.dataset.i18n = '复制引用';
  copy.type = 'button';
  copy.dataset.copyCitation = '';
  copy.addEventListener('click', () => copyText(citationText(paper, request), copy));
  actionsRow.append(copy);
  body.append(actionsRow);
  body.append(window.ScholarGuideResearch.paperDetails(paper));
  article.append(body);
  return article;
}
function sourceList(paper) {
  return (paper.sources && paper.sources.length ? paper.sources : [paper.source]).filter(Boolean);
}

function renderCoverage(result) {
  const host = $('coverage-status');
  liveComputations.set(host, () => renderCoverage(result));
  const papers = result.papers || [];
  if (!papers.length) { host.hidden = true; return; }
  const request = result.request || {};
  const target = request.target_count || 5;
  let text = tr('已交付 {0}/{1}', papers.length, target);
  if (request.category && request.category !== 'none') {
    const count = papers.filter(p => p.category_status === 'yes').length;
    text += tr(' · {0} {1}/{2}', categoryName(request.category), count, request.minimum_category_count || 0);
  }
  const intent = (result.trace || []).find(event => event.action === 'intent');
  if (request.reading_goal) {
    text += tr(' · 目标 {0}', goalName(request.reading_goal) || request.reading_goal);
    if (intent?.mix) text += ` · ${intent.mix}`;
  }
  if ((result.gaps || []).length) text += tr(' · 仍有 {0} 项缺口', (result.gaps || []).length);
  host.textContent = text;
  host.className = 'coverage-status' + ((result.gaps || []).length ? ' warn' : '');
  host.hidden = false;
  if (intent) host.title = tr('目标策略：{0}', intent.policy || '—');
}

function applySourceFilter() {
  const result = state.lastResult;
  if (!result) return;
  const all = result.papers || [];
  const filtered = state.sourceFilter === 'all'
    ? all : all.filter(p => sourceList(p).includes(state.sourceFilter));
  setLiveText($('result-count'), filtered.length === all.length
    ? '· {0} 篇' : '· {0}/{1} 篇', ...(filtered.length === all.length ? [all.length] : [filtered.length, all.length]));
  const list = $('paper-list');
  list.replaceChildren();
  const request = result.request || {};
  filtered.forEach(paper => list.append(paperCard(paper, all.indexOf(paper), request)));
  document.querySelector('#result-section > .reading-nav')?.remove();
  const readingNav = window.ScholarGuideResearch.readingNavigation(filtered, [...list.children], 'search');
  if (readingNav) list.before(readingNav);
  if (!filtered.length) list.append(liveElement('p', '该来源下没有论文；切换“全部来源”查看其余结果。', 'notice amber'));
  for (const button of document.querySelectorAll('#source-filter .chip')) {
    button.classList.toggle('active', button.dataset.source === state.sourceFilter);
    button.setAttribute('aria-pressed', String(button.dataset.source === state.sourceFilter));
  }
  setLiveText($('filter-feedback'), state.sourceFilter === 'all'
    ? '显示全部来源：{0}/{1} 篇 · 排序编号保持原清单顺序。复制与下载始终包含完整的 {1} 篇。'
    : '当前显示来源「{0}」：{1}/{2} 篇 · 排序编号保持原清单顺序。复制与下载始终包含完整的 {2} 篇。',
    ...(state.sourceFilter === 'all' ? [filtered.length, all.length] : [sourceLabel(state.sourceFilter), filtered.length, all.length]));
  $('filter-feedback').hidden = all.length === 0;
}

function renderSourceFilter(result) {
  const host = $('source-filter');
  host.replaceChildren();
  const sources = new Set();
  for (const paper of result.papers || []) sourceList(paper).forEach(s => sources.add(s));
  if (sources.size < 2) { host.hidden = true; return; }
  const add = (value, label) => {
    const button = value === 'all'
      ? liveElement('button', '全部来源 ({0})', 'chip', (result.papers || []).length)
      : element('button', label, 'chip');
    button.type = 'button';
    button.dataset.source = value;
    button.setAttribute('aria-pressed', String(value === state.sourceFilter));
    button.classList.toggle('active', value === state.sourceFilter);
    button.addEventListener('click', () => { state.sourceFilter = value; applySourceFilter(); });
    host.append(button);
  };
  add('all', tr('全部来源 ({0})', (result.papers || []).length));
  [...sources].sort().forEach(source => add(source, `${sourceLabel(source)} (${(result.papers || []).filter(p => sourceList(p).includes(source)).length})`));
  host.hidden = false;
}

function setModelPill(node, provider, model) {
  node.replaceChildren();
  const copy = element('span', '', 'model-copy');
  copy.append(element('span', provider || 'Provider', 'model-provider'),
              element('span', model || 'model', 'model-name'));
  node.title = [provider, model].filter(Boolean).join(' · ');
  node.append(copy);
}

function costBreakdownText(cost) {
  if (!cost) return "";
  const value = (v) => (v === null || v === undefined || v === "" ? "unknown" : v);
  return [
    `${value(cost.total)} ${cost.currency || ""} total${cost.estimate ? " (est.)" : ""}`,
    `model ${value(cost.model)} ${cost.currency || ""}`,
    `tools ${value(cost.tools)} ${cost.tools_currency || "CNY"}`,
    `tokens ${value(cost.input_tokens)} in / ${value(cost.output_tokens)} out`,
    `prices ${value(cost.input_price)} / ${value(cost.output_price)} per 1M`,
  ].join(" · ");
}

function renderResult(result) {
  state.lastResult = result;
  state.sourceFilter = 'all';
  $('running-state').hidden = true;
  $('empty-state').hidden = true;
  $('result-section').hidden = false;
  document.querySelector('#result-section > .research-brief')?.remove();
  document.querySelector('#result-section > .research-coverage')?.remove();
  const coverage = window.ScholarGuideResearch?.coverage(result.research_coverage, {papers: result.papers, headingLevel: 2});
  if (coverage) $('result-section').append(coverage);
  if (result.research_brief && window.ScholarGuideResearch) {
    $('result-section').append(window.ScholarGuideResearch.render(result.research_brief, {papers: result.papers, headingLevel: 2}));
  }
  $('downloads').hidden = !state.job;
  $('download-md').href = `/api/jobs/${state.job}/report.md`;
  $('download-json').href = `/api/jobs/${state.job}/result.json`;
  $('download-bib').href = `/api/jobs/${state.job}/references.bib`;
  const chatLink = $('open-chat-result');
  if (result.chat_session_id) {
    chatLink.href = '/chat?session=' + encodeURIComponent(result.chat_session_id);
    $('continue-chat').href = chatLink.href;
    $('task-continuity').hidden = false;
  } else {
    $('task-continuity').hidden = true;
  }
  chatLink.hidden = true;
  const papers = result.papers || [];
  setLiveText($('result-count'), '· {0} 篇', papers.length);
  const isDemo = result.mode === 'demo';
  setLiveText($('task-continuity').querySelector('p'), isDemo
    ? '可在 Chat 中体验排除第一篇；其他反馈使用预设流程，不保证改变清单。'
    : '在同一任务中排除读过的论文、调整目的，或围绕现有候选继续补搜。');
  const summary = {
    complete: isDemo ? '演示已完成，清单按预设判断生成。' : '本次检索已完成，以下清单满足模型判定的数量与类别要求。',
    partial: '已返回可用论文，部分条件仍未满足。',
    no_results: '本轮没有找到可推荐的论文。可以简化主题后重试。',
    unsupported: '这次需求包含暂不支持的条件，请调整后重试。',
    failed: '本次运行未完成，具体原因见下方。',
    cancelled: '搜索已停止，已获取的结果保留在下方。'
  };
  $('outcome').className = 'notice' + (result.status === 'complete' && !isDemo ? '' : ' amber');
  const outcomeKey = result.cancel_requested || ['cancelled', 'user_cancelled'].includes(result.stop_reason)
    ? '已按你的要求停止搜索，保留当前可用结果。' : summary[result.status];
  delete $('outcome').dataset.i18nLive;
  if (outcomeKey) {
    $('outcome').replaceChildren(liveElement('span', outcomeKey));
    if (isDemo) $('outcome').prepend(liveElement('span', '离线演示 · 固定元数据与预设判断，无 API 调用。'));
  } else {
    delete $('outcome').dataset.i18nLive;
    $('outcome').textContent = result.status || '';
  }
  const diagnosis = result.diagnosis || {};
  const diagnosisHost = $('diagnosis-section');
  if (diagnosis.code && diagnosis.code !== 'ok' && diagnosis.summary) {
    const actions = (diagnosis.actions || []).slice(0, 4).map(a => a.label).filter(Boolean);
    const health = Object.entries(diagnosis.source_health || {}).map(([name, state]) => {
      const codes = Array.isArray(state.codes) ? state.codes.join(' / ') : (state.last_code || '');
      return codes ? `${name}: ${codes}` : '';
    }).filter(Boolean);
    const near = (diagnosis.near_misses || []).map(item => item.title).filter(Boolean).slice(0, 3);
    liveComputed(diagnosisHost, () => {
      diagnosisHost.textContent = t('诊断：') + diagnosis.summary +
        (health.length ? ' Source health: ' + health.join(' · ') + '。' : '') +
        (near.length ? t(' 未交付候选：') + near.join(' · ') + '。' : '') +
        (actions.length ? t(' 建议：') + actions.join(' / ') + '。' : '');
    });
    diagnosisHost.hidden = false;
  } else {
    diagnosisHost.hidden = true;
    diagnosisHost.textContent = '';
  }

  const metrics = $('metrics');
  metrics.replaceChildren();
  const m = result.metrics || {};
  const mode = isDemo ? '离线演示' : result.mode === 'live_model_cached_search' ? '使用搜索缓存' : '在线检索';
  const cost = result.cost || m.cost || {};
  const costValue = cost.total != null ? `${Number(cost.total).toFixed(4)} ${cost.currency || ''} · 估算` : 'unknown';
  [[m.search_rounds, '轮搜索'], [m.elapsed_seconds != null ? Number(m.elapsed_seconds).toFixed(1) : '—', '秒'],
   [m.llm_calls ?? '—', '次模型调用'], [costValue, '费用'], [mode, '']].forEach(([value, label]) => {
    const item = element('span', undefined, 'metric');
    item.append(value === mode ? liveElement('b', mode) : element('b', value), liveElement('span', label));
    metrics.append(item);
  });
  const costLine = $('cost-details');
  setLiveText(costLine, '成本 · {0}', costBreakdownText(result.cost || (result.metrics || {}).cost) || 'unknown');
  costLine.hidden = !(result.cost || (result.metrics || {}).cost);
  const request = result.request || {};
  $('result-request').replaceChildren();
  if (request.topic) {
    const topic = $('result-request');
    const constraints = [];
    if (request.reading_goal) constraints.push(GOAL_LABELS[request.reading_goal] || request.reading_goal);
    if (request.venue) constraints.push(request.venue);
    if (request.year_from || request.year_to) {
      constraints.push(`${request.year_from || 'any'}-${request.year_to || 'any'}`);
    }
    topic.append(liveElement('span', '主题 '), element('span', request.topic));
    if (constraints.length) topic.append(document.createTextNode(' · ' + constraints.join(' · ')));
  }

  const gaps = result.gaps || [];
  renderCoverage(result);
  renderSourceFilter(result);
  $('gap-section').hidden = gaps.length === 0;
  liveComputed($('gap-section'), () => {
    $('gap-section').textContent = gaps.length
      ? t('剩余缺口：') + gaps.map(g => window.ScholarGuideResearch.requirement(g, result.research_coverage)).join('；') : '';
  });
  const warnings = [...(result.warnings || [])];
  if (result.status === 'failed') warnings.push(result.stop_reason);
  $('warning-section').hidden = warnings.length === 0;
  $('warning-section').textContent = warnings.join('；');

  applySourceFilter();
  if (!papers.length) {
    $('paper-list').replaceChildren(liveElement('p', '没有可展示的推荐。请查看缺口与执行轨迹，再调整主题。', 'notice amber'));
  }
  if (result.status !== 'complete' && Array.isArray(result.candidates)) {
    const delivered = new Set(papers.map(paper => paper.id || paper.paper_id));
    const candidates = result.candidates.filter(paper => paper && paper.title &&
      !delivered.has(paper.id || paper.paper_id)).slice(0, 20);
    if (candidates.length) {
      const details = element('details', undefined, 'candidate-discovery');
      details.append(liveElement('summary', '已保留 {0} 篇未交付候选', undefined, candidates.length));
      const list = element('ul');
      for (const candidate of candidates) {
        const item = element('li');
        const url = safeLink(candidate.url);
        if (url) {
          const link = element('a', candidate.title);
          link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
          item.append(link);
        } else item.textContent = candidate.title;
        list.append(item);
      }
      details.append(list);
      $('paper-list').append(details);
    }
  }
  if (state.focusResult) {
    window.ScholarGuideResearch.resultNotice($('results-heading'), `本次搜索已结束 · ${papers.length} 篇可阅读`);
    state.focusResult = false;
  }
}
function persistJob(phase = 'running') {
  const mode = phase === 'done' && state.lastResult ? (state.lastResult.mode === 'demo' ? 'demo' : 'live') : state.mode;
  try { sessionStorage.setItem('scholarguide-job', JSON.stringify({id: state.job, started: state.started, mode, phase})); } catch (_) {}
}
function forgetJob() {
  try { sessionStorage.removeItem('scholarguide-job'); } catch (_) {}
}
// Snapshot and event-stream views can arrive in either order. Job events have
// stable sequence numbers; merge them without letting an older snapshot erase
// newer observed events. They are all observations, not inferred progress.
function mergeSearchTrace(existing, incoming) {
  const rows = new Map();
  for (const batch of [existing || [], incoming || []]) {
    batch.forEach((event, index) => {
      if (!event || typeof event !== 'object') return;
      // Older persisted traces may have neither seq nor step; the event API
      // uses their one-based list position as the cursor as well.
      const seq = Number(event.seq ?? event.step ?? index + 1);
      if (Number.isSafeInteger(seq) && seq > 0) rows.set(seq, event);
    });
  }
  return [...rows.entries()].sort((a, b) => a[0] - b[0]).map(([, event]) => event);
}
async function poll() {
  if (state.polling || !state.job) return;
  const jobId = state.job;
  if (state.streamJob !== jobId) {
    state.stream?.close();
    state.stream = null;
    state.streamJob = jobId;
    state.streamEvents = [];
  }
  state.polling = true;
  try {
    const job = await api('/api/jobs/' + jobId);
    if (state.job !== jobId) return;
    state.errors = 0;
    $('reconnect').hidden = true;
    state.stopRequested = Boolean(job.cancel_requested) || ['requested', 'stopping'].includes(job.cancellation_state);
    $('cancel-search').disabled = state.stopRequested || job.state !== 'running';
    setLiveText($('cancel-search'), state.stopRequested ? '正在停止…' : '停止搜索');
    const snapshotTrace = Array.isArray(job.trace) ? job.trace : [];
    // Older records without event identifiers have an ambiguous incremental
    // cursor on the event endpoint; use their complete snapshots only.
    const legacyTrace = snapshotTrace.some(event => event && event.seq == null && event.step == null);
    if (legacyTrace) {
      state.stream?.close();
      state.stream = null;
      state.streamEvents = mergeSearchTrace([], snapshotTrace);
    } else {
      state.streamEvents = mergeSearchTrace(state.streamEvents, snapshotTrace);
    }
    renderTrace(state.streamEvents, job.state !== 'running');
    if (job.state === 'running' && !legacyTrace && !state.stream) {
      state.stream = window.ScholarGuideTrace.subscribe('/api/jobs/' + jobId + '/events', {
        onEvent(event) { if (state.job !== jobId || !state.busy) return; state.streamEvents = mergeSearchTrace(state.streamEvents, [event]); renderTrace(state.streamEvents); },
        onError() { /* Snapshot polling remains a read-only fallback. */ },
      });
    }
    if (state.stopRequested) setLiveText($('running-detail'), '已请求停止。正在处理的调用结束后，会保存已有结果。');
    if (job.state !== 'running') { state.stream?.close(); state.stream = null; }
    if (job.state === 'needs_retry') {
      forgetInterrupted(jobId);
      setBusy(false);
      $('running-state').hidden = true;
      if (job.result) renderResult(job.result);
      else { $('empty-state').hidden = true; $('result-section').hidden = true; }
      state.recovery = job.recovery || {};
      $('job-recovery-message').textContent = state.recovery.message || '服务重启后发现未完成搜索。确认后可继续；当前不会自动调用模型或来源。';
      const candidates = job.result?.candidates?.length || 0;
      $('job-recovery-progress').textContent = `已记录 ${state.recovery.completed_calls || 0} 次调用${candidates ? ` · 保留 ${candidates} 篇候选` : ''}${state.recovery.uncertain_calls ? ` · ${state.recovery.uncertain_calls} 次在途调用结果未知` : ''}。`;
      $('job-recovery').hidden = false;
      $('resume-search').hidden = !state.recovery.can_resume;
      $('resume-search').disabled = false;
      persistJob('needs_retry');
    } else if (job.state === 'done') {
      forgetInterrupted(jobId);
      $('job-recovery').hidden = true; state.recovery = null;
      setBusy(false); renderResult(job.result); persistJob('done');
    } else if (job.state === 'error') {
      setBusy(false); $('running-state').hidden = true;
      restorePreviousResult(); showError(job.error); if (!state.lastResult) forgetJob();
    } else {
      setTimeout(poll, 700);
    }
  } catch (error) {
    if (state.job !== jobId) return;
    if (error.status === 404) {
      forgetInterrupted(jobId);
      setBusy(false); $('running-state').hidden = true;
      restorePreviousResult(); showError(error.message); if (!state.lastResult) forgetJob();
    } else {
      $('reconnect').hidden = false;
      $('reconnect-text').textContent = error.message + ' 页面重连不会重复提交任务。';
      if (++state.errors < 5) setTimeout(poll, 1800);
    }
  } finally { state.polling = false; }
}
function restorePreviousResult() {
  if (state.previousResult) {
    const previous = state.previousResult;
    state.stream?.close(); state.stream = null; state.streamJob = null;
    state.job = previous.job;
    state.started = previous.started;
    state.streamEvents = previous.trace;
    renderTrace(previous.trace, true);
    $('open-activity-link').href = '/activity?run=' + encodeURIComponent(previous.job);
    renderResult(previous.result);
    state.previousResult = null;
    persistJob('done');
  } else {
    $('empty-state').hidden = Boolean(state.lastResult);
    $('result-section').hidden = !state.lastResult;
    $('downloads').hidden = !state.lastResult;
  }
}
$('search-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.busy) return;
  let payload;
  try { payload = inputPayload(); } catch (error) {
    showError(error.message, false); if (error.field) focusField(error.field); return;
  }
  if (state.mode === 'live' && !state.config?.ready) {
    showError('在线搜索需要先完成模型配置。可以打开 Models，或切换离线演示。', false); return;
  }
  showError('');
  if (state.recovery && state.job) rememberInterrupted({id: state.job, mode: state.mode, started: state.started});
  $('job-recovery').hidden = true; state.recovery = null;
  saveDraft();
  state.previousResult = state.lastResult && state.job ? {job: state.job, result: state.lastResult,
    trace: (state.streamEvents || []).slice(), started: state.started} : null;
  state.job = null;
  state.focusResult = true;
  state.stopRequested = false;
  state.lastPayload = payload;
  state.started = Date.now(); state.traceCount = 0; state.errors = 0;
  document.querySelector('.result-arrival')?.remove();
  $('search-progress').replaceChildren(window.ScholarGuideResearch.progress([]));
  $('trace-list').replaceChildren(); setLiveText($('trace-count'), '准备中');
  $('trace-placeholder').hidden = true;
  $('empty-state').hidden = true; $('result-section').hidden = true; $('downloads').hidden = true;
  $('running-state').hidden = false; $('reconnect').hidden = true; $('elapsed').textContent = '0s';
  setLiveText($('running-title'), state.mode === 'demo' ? '正在运行演示' : '正在准备检索');
  setLiveText($('running-detail'), '请稍候，可在执行轨迹中查看进度。');
  updateStages([]);
  setBusy(true);
  try {
    const job = await api('/api/jobs', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    state.job = job.id; persistJob();
    $('cancel-search').disabled = false;
    setLiveText($('cancel-search'), '停止搜索');
    const activityLink = $('open-activity-link');
    if (activityLink) activityLink.href = '/activity?run=' + state.job;
    poll();
  } catch (error) {
    setBusy(false); $('running-state').hidden = true;
    restorePreviousResult(); showError(error.message);
  }
});
$('open-interrupted-search').addEventListener('click', () => {
  if (state.busy) return;
  const job = interruptedJobs().at(-1);
  if (!job) return;
  setMode(job.mode);
  state.job = job.id; state.started = job.started || Date.now(); state.errors = 0;
  $('empty-state').hidden = true; $('running-state').hidden = false;
  setLiveText($('running-title'), '正在读取中断任务');
  setLiveText($('running-detail'), '只读取已保存的进展；继续执行需要另行确认。');
  $('open-activity-link').href = '/activity?run=' + encodeURIComponent(job.id);
  setBusy(true); persistJob(); poll();
});
$('live-tab').addEventListener('click', () => setMode('live'));
$('demo-tab').addEventListener('click', () => setMode('demo'));
$('try-demo').addEventListener('click', () => { setMode('demo'); $('search-form').requestSubmit(); });
$('readiness-demo').addEventListener('click', () => { setMode('demo'); $('scenario').focus(); });
$('copy-reading-list').addEventListener('click', () => {
  if (state.lastResult) copyText(readingListText(state.lastResult), $('copy-reading-list'));
});
$('cancel-search').addEventListener('click', async () => {
  if (!state.job || !state.busy || state.stopRequested) return;
  state.stopRequested = true;
  $('cancel-search').disabled = true;
  setLiveText($('cancel-search'), '正在停止…');
  setLiveText($('running-detail'), '已请求停止。正在处理的调用结束后，会保存已有结果。');
  try {
    await api('/api/jobs/' + state.job + '/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    poll();
  } catch (error) {
    state.stopRequested = false;
    $('cancel-search').disabled = false;
    setLiveText($('cancel-search'), '重试停止');
    setLiveText($('running-detail'), '停止请求未能送达：{0} 当前搜索仍可能在运行。', error.message);
  }
});
$('resume-search').addEventListener('click', async () => {
  if (state.busy || !state.job || !state.recovery?.can_resume) return;
  $('resume-search').disabled = true;
  showError('');
  try {
    const resumed = await api('/api/jobs/' + encodeURIComponent(state.job) + '/resume', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    state.job = resumed.id || state.job;
    $('job-recovery').hidden = true;
    $('running-state').hidden = false;
    setLiveText($('running-title'), '正在继续原任务');
    setLiveText($('running-detail'), '已完成的调用将从记录复用，继续未完成的阶段。');
    setBusy(true); persistJob(); poll();
  } catch (error) {
    $('resume-search').disabled = false;
    setLiveText($('job-recovery-message'), '恢复未能启动：{0} 已保存结果仍保留，可以重试。', error.message);
  }
});
$('edit-interrupted-search').addEventListener('click', () => {
  if (state.busy) return;
  $(state.natural ? 'request' : 'topic').focus();
  $('search-heading').scrollIntoView({block: 'start', behavior: 'smooth'});
});
$('retry-poll').addEventListener('click', () => { state.errors = 0; poll(); });
$('retry-search').addEventListener('click', () => { if (!state.busy) $('search-form').requestSubmit(); });
$('toggle-input').addEventListener('click', () => {
  if (!state.natural && $('topic').value.trim()) {
    try { $('request').value = inputPayload().request; }
    catch (error) {
      showError(error.message, false);
      if (error.field) focusField(error.field);
      return;
    }
  }
  clearSearchView();
  setInputMode(!state.natural);
  saveDraft();
});
const SEARCH_EXAMPLES = {
  evaluation: {goal: 'comparison', request: '推荐 5 篇 RAG 论文，其中至少 2 篇主要提出评测方法或 benchmark。我准备设计实验，希望比较不同评测方案。'},
  orientation: {goal: 'orientation', request: '我刚开始研究大语言模型的工具使用，推荐 5 篇适合建立整体理解的论文，其中至少 1 篇是综述。'},
  frontier: {goal: 'frontier', request: '推荐 5 篇关于 3D 场景重建的研究论文。我希望了解最近的方法与开放问题，寻找值得探索的研究方向。'},
};
for (const example of document.querySelectorAll('[data-example]')) {
  example.addEventListener('click', () => {
    const value = SEARCH_EXAMPLES[example.dataset.example];
    if (!value || state.busy) return;
    clearSearchView();
    $('request').value = value.request;
    $('reading-goal').value = value.goal;
    setInputMode(true);
    showError(''); saveDraft();
  });
}
$('search-form').addEventListener('input', () => { if (state.job || state.lastResult || state.recovery) clearSearchView(); syncForm(); saveDraft(); showError(''); });
$('venue-catalog').addEventListener('toggle', async () => {
  if (!$('venue-catalog').open || $('catalog-list').children.length) return;
  setLiveText($('catalog-notice'), '正在读取研究池…');
  try {
    const catalog = await api('/api/research/catalog');
    $('catalog-notice').textContent = `版本：${catalog.version}。本系统选择的研究发现池，不是客观排名，也不代表池内每份提交都已录用。`;
    for (const venue of catalog.venues || []) $('catalog-list').append(element('li', `${venue.name} · ${venue.type === 'journal' ? '期刊' : '会议'}${venue.issn?.length ? ' · ISSN ' + venue.issn.join(' / ') : ''}`));
  } catch (error) { $('catalog-notice').textContent = error.message; }
});
$('search-form').addEventListener('change', event => {
  if (state.job || state.lastResult || state.recovery) clearSearchView();
  if (event.target.id === 'source') state.sourceChosen = true;
  syncForm(); saveDraft();
});
$('advanced-settings').addEventListener('toggle', saveDraft);
for (const id of ['topic', 'request']) $(id).addEventListener('keydown', event => {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault(); if (!state.busy) $('search-form').requestSubmit();
  }
});
// Demo deep link: /search?demo=followup|satisfied|insufficient
function autoDemo() {
  const demo = new URLSearchParams(location.search).get('demo');
  if (!demo) return;
  const allowed = ['followup', 'satisfied', 'insufficient'];
  if (allowed.includes(demo)) {
    $('scenario').value = demo;
    setMode('demo');
    $('search-form').requestSubmit();
  }
}

let configRequest = null;
async function refreshConfig() {
  if (state.busy) return;
  if (configRequest) return configRequest;
  state.configLoading = true;
  syncForm();
  configRequest = (async () => {
    try {
      const config = await api('/api/config');
      state.config = config;
      state.configError = '';
      if (config.ready) setModelPill($('model-label'), config.provider, config.model);
      else setLiveText($('model-label'), '尚未配置 API');
      $('model-label').classList.toggle('off', !config.ready);
      const defaultSource = config.research_default_source || config.source;
      if (!state.configLoaded && !state.draftRestored && !state.sourceChosen &&
          [...$('source').options].some(option => option.value === defaultSource)) $('source').value = defaultSource;
      state.configLoaded = true;
    } catch (_) {
      state.config = null;
      state.configError = '暂时无法读取模型配置。可重新检查，或先体验离线演示；你的需求已保留。';
      setLiveText($('model-label'), '本地服务未连接');
      $('model-label').classList.add('off');
    } finally {
      state.configLoading = false;
      configRequest = null;
      syncForm();
    }
  })();
  return configRequest;
}
function translateSearchOptions() {
  document.querySelectorAll('option[data-i18n]').forEach(option => { option.textContent = t(option.dataset.i18n); });
}
translateSearchOptions();
window.addEventListener('sg:language-change', () => {
  translateSearchOptions();
  // Update in place: do not rerender results, alter the draft, focus or start I/O.
  $('submit-text').textContent = t(state.busy ? '正在发现…' : state.mode === 'demo' ? '运行演示' : '发现论文');
  $('toggle-input').textContent = t(state.natural ? '改用主题与类别' : '改用完整需求');
  $('topic-label').textContent = t(state.natural ? '完整研究需求' : '研究主题');
  $('retry-config').textContent = t(state.configLoading ? '正在检查…' : '重新检查配置');
  // Recompute validation in place; this does not submit, change inputs, or perform I/O.
  syncForm();
  document.querySelectorAll('#paper-list [data-i18n]').forEach(node => {
    node.textContent = t(node.dataset.i18n);
  });
  if (state.streamEvents?.length) {
    $('trace-count').textContent = t('{count} 个真实事件', {count: state.streamEvents.length});
  }
  updateStages(state.streamEvents || []);
  const latest = state.streamEvents?.at(-1);
  if (state.busy && latest) {
    setLiveText($('running-title'), window.ScholarGuideTrace.label(latest));
    const detail = state.stopRequested ? t('已请求停止。正在处理的调用结束后，会保存已有结果。')
      : traceDetail(latest) || (latest.payload?.source ? tr('来源：{0}', sourceLabel(latest.payload.source)) : t('实际执行过程会持续更新。'));
    setLiveText($('running-detail'), detail);
  }
  refreshLiveText();
  for (const [node, render] of liveComputations) {
    if (!node.isConnected) liveComputations.delete(node);
    else render();
  }
});
$('retry-config').addEventListener('click', refreshConfig);
window.addEventListener('focus', refreshConfig);
window.addEventListener('pageshow', event => { if (event.persisted) refreshConfig(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshConfig(); });

(async () => {
  let restoredDraft = false;
  try {
    const draft = JSON.parse(sessionStorage.getItem('scholarguide-search-draft'));
    if (draft?.values && typeof draft.values === 'object') {
      for (const id of ['topic', 'request', 'category', 'minimum', 'method', 'reading-goal', 'venue', 'year-from', 'year-to', 'source', 'scenario', 'venue-scope', 'publication-type']) {
        const value = draft.values[id];
        if (typeof value === 'string') {
          const node = $(id);
          if (node.tagName !== 'SELECT' || [...node.options].some(option => option.value === value)) node.value = value;
        }
      }
      $('cache').checked = draft.cache !== false;
      for (const id of ['accepted-only', 'presentation-oral', 'presentation-spotlight', 'presentation-poster']) $(id).checked = draft.researchChecks?.[id] === true;
      state.sourceChosen = draft.sourceChosen === true;
      $('advanced-settings').open = draft.advancedOpen === true;
      setInputMode(draft.natural === true, false);
      setMode(draft.mode === 'demo' ? 'demo' : 'live');
      restoredDraft = true;
    }
  } catch (_) {}
  syncForm();
  state.draftRestored = restoredDraft;
  await refreshConfig();
  let restoredJob = false;
  const demo = new URLSearchParams(location.search).get('demo');
  const explicitDemo = ['followup', 'satisfied', 'insufficient'].includes(demo) &&
    (performance.getEntriesByType('navigation')[0]?.type || 'navigate') === 'navigate';
  try {
    const saved = JSON.parse(sessionStorage.getItem('scholarguide-job'));
    if (saved?.id && /^[a-f0-9]{32}$/.test(saved.id)) {
      const savedMode = saved.mode === 'demo' ? 'demo' : 'live';
      const detached = saved.phase === 'needs_retry' && (explicitDemo || (restoredDraft && savedMode !== state.mode));
      const stale = saved.phase === 'done' && (explicitDemo || (restoredDraft && savedMode !== state.mode));
      if (detached || stale) {
        if (detached) rememberInterrupted({id: saved.id, mode: savedMode, started: saved.started});
        forgetJob();
        emptyContext('mode');
      } else {
        if (saved.phase !== 'done' || !restoredDraft) setMode(savedMode);
        state.job = saved.id; state.started = saved.started || Date.now();
        restoredJob = true;
        const activityLink = $('open-activity-link');
        if (activityLink) activityLink.href = '/activity?run=' + state.job;
        $('empty-state').hidden = true; $('running-state').hidden = false; setBusy(true); poll();
      }
    }
  } catch (_) {}
  $('detached-recovery-note').hidden = interruptedJobs().length === 0;
  if (!restoredJob && explicitDemo) autoDemo();
})();
