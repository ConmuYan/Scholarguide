const $ = (id) => document.getElementById(id);
const setStatus = (id, text, kind) => {
  const node = $(id);
  node.textContent = text || '';
  node.className = 'status' + (kind ? ' ' + kind : '');
};

async function call(path, options = {}) {
  const response = await fetch(path, {
    headers: {'Content-Type': 'application/json'},
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
  return data;
}

$('begin').addEventListener('click', async () => {
  setStatus('begin-status', '正在获取…');
  try {
    const data = await call('/api/providers/oauth/openai/begin', {method: 'POST', body: '{}'});
    $('auth-url').value = data.url;
    $('link-row').hidden = false;
    const open = $('open');
    open.disabled = false;
    open.onclick = () => window.open(data.url, '_blank', 'noopener');
    setStatus('begin-status', '已生成，点“在新标签打开”完成授权。', 'ok');
  } catch (error) {
    setStatus('begin-status', '失败：' + error.message, 'err');
  }
});

$('complete').addEventListener('click', async () => {
  const callback = $('callback').value.trim();
  if (!callback) { setStatus('complete-status', '请先粘贴回跳地址。', 'err'); return; }
  setStatus('complete-status', '正在换取令牌…');
  try {
    const data = await call('/api/providers/oauth/openai/complete', {
      method: 'POST', body: JSON.stringify({callback}),
    });
    setStatus('complete-status', '登录成功：' + (data.subject || '已连接') + '。', 'ok');
    const next = $('next-step');
    if (next) next.hidden = false;
    setTimeout(() => window.location.assign('/settings'), 1200);
  } catch (error) {
    setStatus('complete-status', '失败：' + error.message, 'err');
  }
});
