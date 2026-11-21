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
    setStatus('complete-status', '登录成功：' + (data.subject || '已连接'), 'ok');
    $('model-section').hidden = false;
    await loadModels();
  } catch (error) {
    setStatus('complete-status', '失败：' + error.message, 'err');
  }
});

async function loadModels() {
  $('model').innerHTML = '<option>正在读取…</option>';
  $('model-manual').hidden = true;
  try {
    const data = await call('/api/providers/oauth/openai/models');
    const models = data.models || [];
    if (!models.length) {
      $('model').innerHTML = '';
      $('model-manual').hidden = false;
      setStatus('model-status',
        (data.note ? '拉取失败：' + data.note + '。' : '该账号没有可用模型。') + '请手动填写模型标识。', 'err');
      return;
    }
    $('model').innerHTML = models.map((m) => `<option value="${m}">${m}</option>`).join('');
    setStatus('model-status', `共 ${models.length} 个可用模型。`, 'ok');
  } catch (error) {
    $('model').innerHTML = '';
    $('model-manual').hidden = false;
    setStatus('model-status', '拉取失败：' + error.message + '；可手动填写模型标识。', 'err');
  }
}
$('reload').addEventListener('click', loadModels);

$('save').addEventListener('click', async () => {
  const model = ($('model').value || $('model-manual').value || '').trim();
  if (!model) { setStatus('model-status', '请先选择模型。', 'err'); return; }
  setStatus('model-status', '正在保存…');
  try {
    await call('/api/providers', {
      method: 'POST',
      body: JSON.stringify({
        id: 'openai-oauth', name: 'OpenAI / ChatGPT OAuth', protocol: 'responses',
        base_url: 'https://api.openai.com/v1', model, source: 'custom', activate: true,
      }),
    });
    setStatus('model-status', '已保存并启用：' + model, 'ok');
  } catch (error) {
    setStatus('model-status', '保存失败：' + error.message, 'err');
  }
});

const back = document.createElement('p');
back.className = 'hint';
back.innerHTML = '完成后可返回 <a href="/settings">设置</a> 查看当前服务。';
document.querySelector('main').appendChild(back);
