'use strict';
const $ = id => document.getElementById(id);
let settings = {}, bots = [], mcpConfig = {};
let noticeTimer, refreshTimer;
const bytes = v => `${(v / 1024 / 1024).toFixed(1)} MiB`;

function notify(message, error = false) {
  const n = $('notice');
  n.textContent = message;
  n.className = `visible${error ? ' error' : ''}`;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => n.className = '', 6000);
}

async function api(path, method = 'GET', body) {
  const response = await fetch('/api/admin/' + path, {
    method,
    headers: {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error?.message || '请求失败，请检查输入参数');
  return data;
}

async function action(button, fn) {
  button.disabled = true;
  try {
    await fn();
  } catch (e) {
    notify(e.message, true);
  } finally {
    button.disabled = false;
  }
}

function setTheme(value) {
  if (value === 'auto') document.body.removeAttribute('data-theme');
  else document.body.dataset.theme = value;
}

function populate() {
  for (const element of $('settings-form').elements) {
    if (!element.name || !(element.name in settings)) continue;
    const value = settings[element.name];
    if (element.type === 'checkbox') element.checked = value;
    else element.value = element.name.endsWith('_bytes') ? value / 1024**2 : value;
  }
  setTheme(settings.theme);
}

function row(container, text, buttonText, callback) {
  const line = document.createElement('div');
  const content = document.createElement('span');
  content.textContent = text;
  line.append(content);
  if (buttonText) {
    const button = document.createElement('button');
    button.textContent = buttonText;
    button.addEventListener('click', () => action(button, callback));
    line.append(button);
  }
  container.append(line);
  return line;
}

function empty(container, text) {
  if (!container.children.length) {
    const p = document.createElement('p');
    p.textContent = text;
    container.append(p);
  }
}

async function loadSessions() {
  const data = await api('sessions');
  const previousSelection = $('cookie-platform').value;
  $('session-list').replaceChildren();
  $('cookie-platform').replaceChildren();
  const labels = {MISSING: '未导入', IMPORTED: '已导入', UNVERIFIED: '未检测', VALID: '有效', INVALID: '凭据失效'};
  for (const session of data) {
    const option = document.createElement('option');
    option.value = option.textContent = session.platform;
    $('cookie-platform').append(option);
    const line = document.createElement('div');
    line.className = 'platform';
    const text = document.createElement('span');
    text.textContent = session.platform;
    const status = document.createElement('small');
    const enabledLabel = session.enabled ? '已启用' : '已禁用';
    const valState = labels[session.validation.state] || session.validation.state;
    status.textContent = ` ${labels[session.state] || session.state} · ${enabledLabel} / ${valState}`;
    text.append(status);
    line.append(text);
    const buttons = document.createElement('div');
    buttons.className = 'buttons';
    const toggleAction = session.enabled
      ? ['禁用', 'POST', '/disable', 'Cookie 已禁用']
      : ['启用', 'POST', '/enable', 'Cookie 已启用'];
    const ops = [
      toggleAction,
      ['检测', 'POST', '/verify', '已完成实际账号检测'],
      ['移除', 'DELETE', '', 'Cookie 已移除'],
    ];
    for (const [name, method, suffix, okMsg] of ops) {
      const b = document.createElement('button');
      b.textContent = name;
      b.addEventListener('click', () => action(b, async () => {
        const result = await api('sessions/' + encodeURIComponent(session.platform) + suffix, method);
        notify(result.state === 'INVALID' ? '凭据已失效，请重新导入' : okMsg);
        await loadSessions();
      }));
      buttons.append(b);
    }
    line.append(buttons);
    $('session-list').append(line);
  }
  if (previousSelection && Array.from($('cookie-platform').options).some(o => o.value === previousSelection)) {
    $('cookie-platform').value = previousSelection;
  }
}

function field(form, name, label, value, type = 'text') {
  const l = document.createElement('label');
  l.textContent = label;
  const input = document.createElement(type === 'textarea' ? 'textarea' : 'input');
  if (type !== 'textarea') input.type = type;
  input.name = name;
  if (type === 'checkbox') input.checked = value;
  else input.value = value ?? '';
  if (type === 'password') input.autocomplete = 'new-password';
  l.append(input);
  form.append(l);
  return input;
}

async function loadBots() {
  bots = await api('bots');
  $('bot-forms').replaceChildren();
  for (const bot of bots) {
    const block = document.createElement('div');
    block.className = 'bot-block';
    const h = document.createElement('h4');
    h.textContent = bot.adapter === 'telegram' ? 'Telegram' : 'QQ / OneBot / NapCat';
    block.append(h);
    const form = document.createElement('form');
    block.append(form);
    const grid = document.createElement('div');
    grid.className = 'field-grid';
    form.append(grid);
    field(grid, 'enabled', '启用适配器', bot.enabled, 'checkbox');
    if (bot.adapter === 'telegram') field(grid, 'local_api', '使用本地 Bot API Server', bot.local_api, 'checkbox');
    field(grid, 'base_url', 'API 地址', bot.base_url || (bot.adapter === 'telegram' ? 'https://api.telegram.org' : 'http://127.0.0.1:3000'));
    const secret = field(grid, 'token', bot.has_token ? '令牌（已加密保存，留空保留）' : '机器人访问令牌', '', 'password');
    field(grid, 'max_upload_bytes', '此发送端上传上限（MiB）', bot.max_upload_bytes / 1024**2, 'number');
    field(grid, 'allowed_targets', '允许目标（每行一个）', bot.allowed_targets.join('\n'), 'textarea');
    if (bot.adapter === 'onebot') {
      field(grid, 'shared_root', '服务端共享目录', bot.shared_root);
      field(grid, 'remote_root', 'NapCat 中对应目录', bot.remote_root);
    }
    const buttons = document.createElement('div');
    buttons.className = 'actions';
    const save = document.createElement('button');
    save.textContent = '保存连接';
    save.type = 'submit';
    buttons.append(save);
    const probe = document.createElement('button');
    probe.type = 'button';
    probe.textContent = '检测连接';
    buttons.append(probe);
    form.append(buttons);
    form.addEventListener('submit', event => {
      event.preventDefault();
      action(save, async () => {
        const value = {...bot};
        delete value.has_token;
        for (const input of form.elements) {
          if (!input.name) continue;
          value[input.name] = input.type === 'checkbox'
            ? input.checked
            : input.name === 'allowed_targets'
              ? input.value.split('\n').map(v => v.trim()).filter(Boolean)
              : input.name === 'max_upload_bytes'
                ? Number(input.value) * 1024**2
                : input.value;
        }
        await api('bots', 'PUT', value);
        secret.value = '';
        notify('机器人连接已保存');
        await loadBots();
      });
    });
    probe.addEventListener('click', () => action(probe, async () => {
      await api('bots/' + bot.adapter + '/probe', 'POST');
      notify('机器人 API 已确认连接');
      await refresh();
    }));
    $('bot-forms').append(block);
  }
}

async function refresh() {
  const status = await api('status');
  const cap = status.capabilities;
  $('version').textContent = cap.version;
  $('connection').textContent = '服务已连接';
  const entries = [
    ['MCP 服务', '运行中 · 免令牌本机直连（Streamable HTTP 与 stdio）'],
    ['HTTP 地址', location.origin + '/mcp'],
    ['服务数据目录', status.data_path],
    ['实际缓存目录', status.cache_path],
    ['下载中的任务', String((status.tasks.DOWNLOADING || 0) + (status.tasks.PROCESSING || 0))],
    ['等待交付的任务', String(status.tasks.READY || 0)],
    ['缓存占用', bytes(status.cache_bytes) + ' / ' + bytes(settings.max_cache_bytes)],
    ['FFmpeg', cap.ffmpeg ? '可用' : '不可用'],
    ['机器人状态', Object.entries(cap.bots).map(([k, v]) => `${k}: ${!v.enabled ? '未启用' : v.status.connected === true ? '已连接' : v.status.connected === false ? '连接失败' : '未检测'}`).join(' / ')],
  ];
  $('service-status').replaceChildren();
  for (const [key, value] of entries) {
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = value;
    $('service-status').append(dt, dd);
  }
  for (const id of ['approvals', 'deliveries', 'errors', 'cleanup-log']) $(id).replaceChildren();
  for (const approval of status.approvals) {
    row($('approvals'), approval.summary + (approval.approved ? '（已批准）' : ''), approval.approved ? '' : '批准此请求', async () => {
      await api('approvals/' + approval.id, 'POST');
      notify('已批准，Agent 可使用原参数与 approval_id 再次调用');
      await refresh();
    });
  }
  empty($('approvals'), '没有待确认的大批量请求');
  for (const delivery of status.deliveries) {
    row($('deliveries'), `${delivery.adapter} → ${delivery.target} · ${delivery.state}${delivery.error ? ' · ' + delivery.error : ''}`, ['FAILED', 'UNCERTAIN'].includes(delivery.state) ? '重试' : '', async () => {
      if (delivery.state === 'UNCERTAIN' && !confirm('平台可能已收到文件。请先核对机器人端记录，确认允许再次发送后继续。')) return;
      await api('deliveries/' + delivery.id + '/retry', 'POST', {reconciled_allow_retry: delivery.state === 'UNCERTAIN'});
      await refresh();
    });
  }
  empty($('deliveries'), '没有交付记录');
  for (const error of status.errors) row($('errors'), `${error.id.slice(0, 8)} · ${error.error}`);
  empty($('errors'), '暂无错误');
  if (status.cleanup_error) row($('errors'), status.cleanup_error + ' · 文件清理已延后，平台发送状态不受影响');
  for (const entry of status.cleanup) row($('cleanup-log'), `${new Date(entry.created * 1000).toLocaleString()} · 清理 ${entry.files} 个文件 · ${bytes(entry.bytes)}`);
  empty($('cleanup-log'), '暂无清理记录');
}

async function init() {
  settings = await api('config');
  populate();
  await Promise.all([loadSessions(), loadBots(), refresh()]);
  mcpConfig = await api('mcp-config');
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    Promise.all([refresh(), loadSessions()]).catch(() => {
      $('connection').textContent = '连接已断开';
    });
  }, 10000);
}

$('save').addEventListener('click', () => action($('save'), async () => {
  if (!$('settings-form').reportValidity()) return;
  const next = {...settings};
  for (const element of $('settings-form').elements) {
    if (!element.name) continue;
    next[element.name] = element.type === 'checkbox'
      ? element.checked
      : typeof settings[element.name] === 'number'
        ? Number(element.value) * (element.name.endsWith('_bytes') ? 1024**2 : 1)
        : element.value;
  }
  await api('config', 'PUT', next);
  settings = next;
  notify('设置已保存，用户与 Agent 调用同步生效');
  await refresh();
}));

$('theme').addEventListener('click', () => {
  const current = document.body.dataset.theme || (matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light');
  settings.theme = current === 'dark' ? 'light' : 'dark';
  setTheme(settings.theme);
  notify('外观已切换，点击保存设置以持久化');
});

$('cookie-file').addEventListener('change', async e => {
  if (e.target.files[0]) $('cookie-text').value = await e.target.files[0].text();
});

$('import-cookie').addEventListener('click', () => action($('import-cookie'), async () => {
  await api('sessions', 'POST', {platform: $('cookie-platform').value, text: $('cookie-text').value});
  $('cookie-text').value = '';
  $('cookie-file').value = '';
  notify('凭据已加密保存并启用');
  await loadSessions();
}));

$('refresh').addEventListener('click', () => action($('refresh'), async () => {
  settings = await api('config');
  populate();
  await Promise.all([loadSessions(), loadBots(), refresh()]);
}));

$('cleanup').addEventListener('click', () => action($('cleanup'), async () => {
  const result = await api('cleanup', 'POST');
  notify(`已清理 ${result.files_removed} 个文件，释放 ${bytes(result.bytes_removed)}`);
  await refresh();
}));

for (const mode of ['stdio', 'http']) {
  $('copy-' + mode).addEventListener('click', () => action($('copy-' + mode), async () => {
    const text = JSON.stringify(mcpConfig[mode], null, 2);
    try {
      await navigator.clipboard.writeText(text);
      notify(mode === 'http' ? 'HTTP MCP 配置已复制' : 'stdio MCP 配置已复制');
    } catch {
      const win = window.open('', '_blank');
      if (win) {
        const pre = win.document.createElement('pre');
        pre.textContent = text;
        win.document.body.append(pre);
      } else {
        throw new Error('剪贴板不可用，请通过 /api/admin/mcp-config 读取配置');
      }
    }
  }));
}

init().catch(e => {
  $('connection').textContent = '连接失败';
  notify(e.message, true);
});
