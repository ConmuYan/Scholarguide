'use strict';

(function () {
  const root = window;
  const listeners = new WeakMap();
  const t = (key, params) => root.SGI18n?.t(key, params) ?? key.replace(/\{(\w+)\}/g, (_, name) => params?.[name] ?? `{${name}}`);
  // Bind only application-owned strings; payload labels and details stay untouched.
  function bind(node, key, params) {
    const update = () => { node.textContent = t(key, params); };
    update();
    node.dataset.sgTraceTranslation = '';
    node._sgTraceTranslation = update;
  }
  window.addEventListener('sg:language-change', () => {
    document.querySelectorAll('[data-sg-trace-translation], [data-sg-trace-aria]').forEach(node => {
      node._sgTraceTranslation?.(); node._sgTraceAriaTranslation?.();
    });
  });
  const text = value => value == null ? '' : String(value);
  const payloadOf = event => event && event.payload && typeof event.payload === 'object' ? event.payload : (event || {});
  const keyOf = event => text(event && (event.event_id || event.span_id || event.id || `${event.seq || ''}:${event.action || ''}:${event.at || event.created_at || ''}`));
  const sourceOf = event => {
    const payload = payloadOf(event);
    return payload.source || payload.tool || payload.provider || payload.name || event.source || '';
  };
  const durationOf = event => {
    const payload = payloadOf(event);
    const value = payload.duration_ms ?? event.duration_ms;
    return Number.isFinite(Number(value)) ? `${Number(value)}ms` : '';
  };
  const labels = {tool_start:'正在调用工具',tool_end:'工具执行完成',tool_error:'工具返回错误',provider:'读取模型配置',task_initial:'建立研究任务',task_update:'更新研究需求',research_plan:'规划研究路线',research_coverage:'检查研究覆盖',search:'检索论文',assess:'核对论文摘要',reuse:'检查已有证据',assessment_reuse:'复用有效判断',decision:'选择下一步',agent_node:'推进研究步骤',delivery_check:'核对交付条件',delivered:'交付推荐结果',ask:'等待澄清',stop:'本轮结束',synthesis:'整理研究综合'};
  const stageLabels = {update:'理解需求',plan:'制定查询',action:'选择下一步',assess:'评估候选',research_plan:'规划研究路线',research_review:'检查路线覆盖',synthesis:'整理研究综合'};
  const labelOf = event => {
    const payload = payloadOf(event);
    if (payload.category === 'model') {
      const label = t(stageLabels[String(payload.tool || '').replace('model.','')] || '调用研究模型');
      return event.action === 'tool_start' ? t('正在{label}', {label}) : label;
    }
    if (payload.category === 'source' && (event.action || '').startsWith('tool_')) return `${t(event.action === 'tool_start' ? '正在检索' : event.action === 'tool_error' ? '来源请求失败' : '来源返回结果')}${payload.source ? ' · ' + payload.source : ''}`;
    if (event.action === 'synthesis') return t('正在整理研究脉络');
    return labels[event.action] ? t(labels[event.action]) : payload.label || payload.title || event.action || t('运行步骤');
  };
  const isError = event => /error|failed|failure/i.test(text(event && event.action) + ' ' + text(payloadOf(event).status));
  const isDone = event => /end|done|complete|completed|finish|finished|success/i.test(text(event && event.action) + ' ' + text(payloadOf(event).status));

  function normalize(value) {
    if (!value || typeof value !== 'object') return null;
    const event = value.event && typeof value.event === 'object' ? value.event : value;
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    return {
      ...event,
      seq: event.seq ?? event.id ?? null,
      event_id: event.event_id || event.id || null,
      at: event.at || event.created_at || new Date().toISOString(),
      payload,
      operation_id: event.operation_id || null,
      session_id: event.session_id || null,
      turn: event.turn ?? null,
    };
  }

  function parseSse(block) {
    const lines = block.split(/\r?\n/);
    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
    if (!data) return null;
    try { return JSON.parse(data); } catch (_) { return null; }
  }

  function subscribe(url, options = {}) {
    const after = options.after == null ? null : options.after;
    let closed = false;
    let source = null;
    let controller = null;
    let fallbackStarted = false;
    let cursor = Number(after) || 0;
    const emit = raw => {
      const event = normalize(raw);
      if (!event || closed) return;
      if (event.action === 'done' || event.event === 'done') {
        options.onDone?.(event);
        return;
      }
      if (event.seq != null && Number(event.seq) <= cursor) return;
      cursor = Math.max(cursor, Number(event.seq) || 0);
      options.onEvent?.(event);
    };
    const close = () => {
      closed = true;
      source?.close();
      controller?.abort();
    };
    const fallback = async () => {
      if (fallbackStarted || closed) return;
      fallbackStarted = true;
      const join = url.includes('?') ? '&' : '?';
      const query = `${join}stream=0${after != null ? `&after=${encodeURIComponent(after)}` : ''}`;
      controller = new AbortController();
      try {
        while (!closed) {
          const response = await fetch(url + `${join}stream=0&after=${cursor}`, {headers: {Accept: 'application/json'}, signal: controller.signal});
          const value = await response.json();
          if (!response.ok) throw new Error(value.error?.message || value.error || `请求失败（${response.status}）`);
          const events = Array.isArray(value) ? value : value.events || value.trace || [];
          events.forEach(emit);
          await new Promise(resolve => setTimeout(resolve, 1000));
        }
      } catch (error) {
        if (!closed && error.name !== 'AbortError') options.onError?.(error);
      }
    };
    if (typeof root.EventSource === 'function') {
      const join = url.includes('?') ? '&' : '?';
      source = new root.EventSource(url + `${join}stream=1${after != null ? `&after=${encodeURIComponent(after)}` : ''}`);
      source.addEventListener('trace', event => { try { emit(JSON.parse(event.data)); } catch (_) {} });
      source.addEventListener('done', event => {
        try { options.onDone?.(normalize(JSON.parse(event.data))); } catch (_) { options.onDone?.(); }
        close();
      });
      source.onerror = () => { source?.close(); fallback(); };
    } else fallback();
    return {close};
  }

  function render(events, options = {}) {
    const rows = Array.isArray(events) ? events : [];
    const host = document.createElement('ol');
    host.className = options.className || 'sg-trace-timeline';
    const updateAria = () => host.setAttribute('aria-label', options.ariaLabel || t('运行进度'));
    updateAria();
    host.dataset.sgTraceAria = '';
    host._sgTraceAriaTranslation = updateAria;
    const spans = new Map();
    rows.forEach(event => {
      const payload = payloadOf(event);
      const id = payload.span_id || event.span_id || keyOf(event);
      const prior = spans.get(id);
      if (event.action === 'tool_start' || event.action === 'model_start' || payload.status === 'running') {
        spans.set(id, {...(prior || {}), ...event});
      } else if (prior && (event.action === 'tool_end' || event.action === 'tool_error' || event.action === 'model_end' || isDone(event))) {
        spans.set(id, {...prior, ...event, payload: {...payloadOf(prior), ...payload}});
      } else spans.set(id, event);
    });
    Array.from(spans.values()).forEach((event, index) => {
      const payload = payloadOf(event);
      const li = document.createElement('li');
      const running = !isDone(event) && !isError(event) && (event.action || '').match(/start|running|progress/);
      li.className = `sg-trace-item chat-trace-item${running ? ' is-running' : ''}${isError(event) ? ' is-error' : ''}`;
      if (running) li.setAttribute('aria-current', 'step');
      const summary = document.createElement('div');
      summary.className = 'sg-trace-summary';
      const title = document.createElement('strong');
      const updateTitle = () => { title.textContent = options.label?.(event) || labelOf(event); };
      updateTitle();
      title.dataset.sgTraceTranslation = '';
      title._sgTraceTranslation = updateTitle;
      const meta = document.createElement('span');
      meta.className = 'sg-trace-meta';
      meta.textContent = [sourceOf(event), durationOf(event), event.at ? new Date(event.at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : ''].filter(Boolean).join(' · ');
      summary.append(title, meta);
      const publicSummary = payload.error || payload.reason || payload.query || (payload.returned != null ? t('返回 {count} 条记录', {count: payload.returned}) : '');
      const badge = document.createElement('span'); badge.className = 'sg-trace-status';
      bind(badge, running ? '执行中' : isError(event) ? '需注意' : isDone(event) ? '已完成' : '已记录');
      summary.append(badge);
      li.append(summary);
      if (publicSummary) {
        const note = document.createElement('p'); note.className = 'sg-trace-note';
        if (!payload.error && !payload.reason && !payload.query && payload.returned != null) bind(note, '返回 {count} 条记录', {count: payload.returned});
        else note.textContent = publicSummary;
        li.append(note);
      }
      const detail = document.createElement('details');
      detail.className = 'sg-trace-details';
      detail.open = false;
      const detailSummary = document.createElement('summary');
      bind(detailSummary, running ? '当前阶段详情' : '查看事件详情');
      const pre = document.createElement('pre');
      pre.textContent = JSON.stringify(payload, null, 2);
      detail.append(detailSummary, pre);
      li.append(detail);
      host.append(li);
    });
    if (!host.children.length) {
      const empty = document.createElement('li');
      empty.className = 'sg-trace-empty';
      if (options.empty) empty.textContent = options.empty;
      else bind(empty, '等待实际运行事件。');
      host.append(empty);
    }
    return host;
  }

  function upsert(events, event) {
    const normalized = normalize(event);
    if (!normalized) return events || [];
    const list = Array.isArray(events) ? events.slice() : [];
    const key = keyOf(normalized);
    const index = list.findIndex(item => keyOf(item) === key || (normalized.payload?.span_id && normalized.payload.span_id === payloadOf(item).span_id && isDone(normalized)));
    if (index >= 0) list[index] = {...list[index], ...normalized, payload: {...payloadOf(list[index]), ...normalized.payload}};
    else list.push(normalized);
    return list.sort((a, b) => Number(a.seq ?? 0) - Number(b.seq ?? 0));
  }

  function quantity(value) {
    const numbers = {one:1,two:2,three:3,four:4,five:5,six:6,seven:7,eight:8,nine:9,ten:10,一:1,二:2,两:2,三:3,四:4,五:5,六:6,七:7,八:8,九:9,十:10};
    const word = '(?:\\d{1,3}|[一二两三四五六七八九十]{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)';
    const range = new RegExp('(?<!\\d)(' + word + ')\\s*(?:-|–|~|到|至|to)\\s*(' + word + ')\\s*(?:篇|papers?\\b)', 'i').exec(value || '');
    const parse = raw => Number(raw) || numbers[raw.toLowerCase()] || (raw.includes('十') ? (numbers[raw.split('十')[0]] || 1)*10+(numbers[raw.split('十')[1]] || 0) : NaN);
    if (range) { const low=parse(range[1]),high=parse(range[2]);return {count:Math.min(high,Math.max(low,5)),explicit:true,interval:true,valid:low>=1&&low<=high&&high<=10}; }
    const expression = new RegExp('(?<![\\d第])(' + word + ')\\s*(?:篇|(?:[A-Za-z]+\\s+){0,5}papers?\\b)', 'i');
    const match = expression.exec(value || '');
    const count = match ? parse(match[1]) : 5;
    return {count, explicit:Boolean(match), valid:count >= 1 && count <= 10};
  }
  root.ScholarGuideTrace = {normalize, subscribe, render, upsert, payloadOf, label:labelOf, quantity};
  async function sources(selects) {
    try {
      const response = await fetch('/api/sources');
      if (!response.ok) return;
      const value = await response.json();
      for (const select of selects.filter(Boolean)) {
        const selected = select.value;
        const options = [new Option(t('自动规划 · 可用学术来源'), 'multi')];
        options[0].dataset.sgTraceTranslation = '';
        options[0]._sgTraceTranslation = () => { options[0].textContent = t('自动规划 · 可用学术来源'); };
        for (const row of value.sources || []) {
          if (!row.enabled) continue;
          const option = new Option(row.label + (row.configured ? '' : ' · ' + t('待配置')), row.name);
          if (!row.configured) {
            const update = () => { option.textContent = row.label + ' · ' + t('待配置'); };
            option.dataset.sgTraceTranslation = '';
            option._sgTraceTranslation = update;
          }
          option.disabled = !row.configured;
          options.push(option);
        }
        select.replaceChildren(...options);
        select.value = options.some(option => option.value === selected) ? selected : 'multi';
      }
    } catch (_) { /* Keep the explicitly labeled static list if configuration is unreachable. */ }
  }
  root.ScholarGuideTrace.sources = sources;
  document.addEventListener('DOMContentLoaded', () => sources([document.getElementById('source'), document.getElementById('fork-source')]));
})();
