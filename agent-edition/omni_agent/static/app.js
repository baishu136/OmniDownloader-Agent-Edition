'use strict';
const $ = id => document.getElementById(id);
let settings = {}, bots = [], mcpConfig = {}, sessionsData = [];
let selectedPlatform = '';
let noticeTimer, refreshTimer, autoSaveTimer;
let savingPromise = Promise.resolve();
const bytes = v => `${(v / 1024 / 1024).toFixed(1)} MiB`;

function notify(message, error = false) {
  const n = $('notice');
  n.textContent = message;
  n.className = `visible${error ? ' error' : ''}`;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => n.className = '', 6000);
}

function setSaveStatus(state, text) {
  const el = $('save-status');
  if (!el) return;
  el.className = `save-status ${state}`;
  el.textContent = text;
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

// -------------------- 深浅色主题（确保即时生效并自动持久化） --------------------

function isCurrentlyDark() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === 'dark') return true;
  if (explicit === 'light') return false;
  return matchMedia('(prefers-color-scheme: dark)').matches;
}

function setTheme(value) {
  const normalized = ['light', 'dark', 'auto'].includes(value) ? value : 'auto';
  for (const el of [document.documentElement, document.body]) {
    if (normalized === 'auto') el.removeAttribute('data-theme');
    else el.dataset.theme = normalized;
  }
  const btn = $('theme');
  if (btn) {
    btn.textContent = normalized === 'dark' ? '外观：深色' : normalized === 'light' ? '外观：浅色' : '外观：自动';
  }
}

// -------------------- 美化动画下拉组件（参照 Android OmniOptionMenu） --------------------

const chevronSvg = '<svg class="omni-select-chevron" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M5 7.5L10 12.5L15 7.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const checkSvg = '<svg class="omni-select-check" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M4.5 10.5L8.2 14.2L15.5 6.8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function closeAllSelects(exceptHost = null) {
  document.querySelectorAll('.omni-select-host.open').forEach(host => {
    if (host !== exceptHost) {
      host.classList.remove('open');
      host.querySelector('.omni-select-trigger')?.setAttribute('aria-expanded', 'false');
    }
  });
}

function enhanceSelect(selectEl, inline = false) {
  if (!selectEl || selectEl.dataset.enhanced === '1') {
    selectEl?._syncSelect?.();
    return;
  }
  selectEl.dataset.enhanced = '1';
  const host = document.createElement('div');
  host.className = 'omni-select-host' + (inline ? ' inline' : '');
  selectEl.parentNode.insertBefore(host, selectEl);
  host.appendChild(selectEl);
  selectEl.classList.add('omni-native-select');
  selectEl.tabIndex = -1;

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'omni-select-trigger';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  const labelSpan = document.createElement('span');
  trigger.appendChild(labelSpan);
  trigger.insertAdjacentHTML('beforeend', chevronSvg);

  const menu = document.createElement('div');
  menu.className = 'omni-select-menu';
  menu.setAttribute('role', 'listbox');
  host.append(trigger, menu);

  function sync() {
    menu.replaceChildren();
    const opts = Array.from(selectEl.options);
    const active = opts.find(o => o.value === selectEl.value) || opts[0];
    labelSpan.textContent = active ? active.textContent : '';
    for (const opt of opts) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'omni-select-option' + (opt.value === selectEl.value ? ' selected' : '');
      btn.setAttribute('role', 'option');
      btn.setAttribute('aria-selected', opt.value === selectEl.value ? 'true' : 'false');
      const txt = document.createElement('span');
      txt.textContent = opt.textContent;
      btn.appendChild(txt);
      if (opt.value === selectEl.value) btn.insertAdjacentHTML('beforeend', checkSvg);
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const changed = selectEl.value !== opt.value;
        selectEl.value = opt.value;
        sync();
        host.classList.remove('open');
        trigger.setAttribute('aria-expanded', 'false');
        if (changed) selectEl.dispatchEvent(new Event('change', {bubbles: true}));
      });
      menu.appendChild(btn);
    }
  }

  selectEl._syncSelect = sync;
  selectEl.addEventListener('change', sync);

  trigger.addEventListener('click', e => {
    e.stopPropagation();
    const isOpen = host.classList.contains('open');
    closeAllSelects(host);
    if (!isOpen) {
      const rect = trigger.getBoundingClientRect();
      const below = window.innerHeight - rect.bottom;
      menu.classList.toggle('above', below < 260 && rect.top > below);
      host.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
    } else {
      host.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
    }
  });

  trigger.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      host.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
    }
  });

  sync();
}

document.addEventListener('click', () => closeAllSelects());

function initAllSelects() {
  document.querySelectorAll('select').forEach(sel => enhanceSelect(sel, sel.id === 'cookie-platform'));
}

// -------------------- 设置表单序列化、填充与自动保存 --------------------

function populate() {
  const form = $('settings-form');
  const displaySettings = {...settings};
  // 历史 IMAGE_ZIP 映射为分离后的“图文原图 + 打包压缩包”状态
  if (displaySettings.default_type === 'IMAGE_ZIP') {
    displaySettings.default_type = 'IMAGE';
    displaySettings.pack_zip = true;
  }
  for (const element of form.elements) {
    if (!element.name || !(element.name in displaySettings)) continue;
    const value = displaySettings[element.name];
    if (element.type === 'checkbox') element.checked = Boolean(value);
    else element.value = element.name.endsWith('_bytes') ? value / 1024**2 : value;
    element._syncSelect?.();
  }
  setTheme(settings.theme);
}

function collectSettings() {
  const next = {...settings};
  for (const element of $('settings-form').elements) {
    if (!element.name) continue;
    if (element.type === 'checkbox') next[element.name] = element.checked;
    else if (typeof settings[element.name] === 'number') next[element.name] = Number(element.value) * (element.name.endsWith('_bytes') ? 1024**2 : 1);
    else next[element.name] = element.value.trim();
  }
  if (next.default_type === 'IMAGE_ZIP') {
    next.default_type = 'IMAGE';
    next.pack_zip = true;
  }
  return next;
}

async function saveSettingsNow(showErrorNotice = true) {
  const form = $('settings-form');
  if (!form.checkValidity()) {
    setSaveStatus('error', '请检查输入值范围');
    if (showErrorNotice) form.reportValidity();
    return false;
  }
  const next = collectSettings();
  setSaveStatus('saving', '正在保存...');
  savingPromise = savingPromise.then(async () => {
    await api('config', 'PUT', next);
    settings = next;
    setSaveStatus('saved', '已自动保存');
    await refresh(false);
  }).catch(err => {
    setSaveStatus('error', '保存失败');
    if (showErrorNotice) notify(err.message, true);
  });
  await savingPromise;
  return true;
}

function scheduleAutoSave(delay = 150, showError = false) {
  clearTimeout(autoSaveTimer);
  setSaveStatus('saving', '准备保存...');
  autoSaveTimer = setTimeout(() => { saveSettingsNow(showError); }, delay);
}

// -------------------- 辅助渲染 --------------------

function row(container, text, buttonText, callback) {
  const line = document.createElement('div');
  const content = document.createElement('span');
  content.textContent = text;
  line.append(content);
  if (buttonText) {
    const button = document.createElement('button');
    button.type = 'button';
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

// -------------------- Cookie 管理（参照源项目 Android 版聚焦卡片） --------------------

function renderSessionCard() {
  if (!sessionsData.length) return;
  if (!selectedPlatform || !sessionsData.some(s => s.platform === selectedPlatform)) {
    selectedPlatform = sessionsData[0].platform;
  }
  const current = sessionsData.find(s => s.platform === selectedPlatform) || sessionsData[0];
  const sel = $('cookie-platform');
  sel.replaceChildren();
  const labels = {MISSING: '未导入', IMPORTED: '已导入', UNVERIFIED: '未检测', VALID: '有效', INVALID: '凭据失效'};
  for (const s of sessionsData) {
    const opt = document.createElement('option');
    opt.value = s.platform;
    const tag = !s.enabled ? '（已禁用）' : s.state === 'IMPORTED' ? '（已导入）' : '';
    opt.textContent = s.platform + tag;
    sel.append(opt);
  }
  sel.value = current.platform;
  sel._syncSelect?.();

  // 状态概览快捷芯片：展示所有已导入或已禁用的平台，方便快速切换
  const overview = $('session-overview');
  overview.replaceChildren();
  const notable = sessionsData.filter(s => s.state === 'IMPORTED' || !s.enabled);
  for (const s of notable) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip ' + (s.platform === current.platform ? 'active ' : '') + (!s.enabled ? 'disabled' : 'imported');
    chip.textContent = s.platform + (!s.enabled ? ' · 已禁用' : s.validation.state === 'VALID' ? ' · 有效' : ' · 已导入');
    chip.addEventListener('click', () => {
      selectedPlatform = s.platform;
      renderSessionCard();
    });
    overview.append(chip);
  }

  $('session-disable-desc').textContent = `仅影响 ${current.platform} 的后续解析与下载；已保存会话继续加密保留`;
  $('session-disable-switch').checked = !current.enabled;

  const pill = $('session-pill');
  if (!current.enabled) {
    pill.textContent = current.state === 'IMPORTED' ? '已禁用 · 会话保留' : '已禁用 · 未导入';
    pill.className = 'status-pill warn';
  } else if (current.state === 'MISSING') {
    pill.textContent = '未导入';
    pill.className = 'status-pill';
  } else if (current.validation.state === 'VALID') {
    pill.textContent = '已导入 · 账号有效';
    pill.className = 'status-pill ok';
  } else if (current.validation.state === 'INVALID') {
    pill.textContent = '凭据失效 · 请重新导入';
    pill.className = 'status-pill err';
  } else {
    pill.textContent = '已导入 · ' + (labels[current.validation.state] || '未检测');
    pill.className = 'status-pill info';
  }

  $('session-clear').disabled = current.state === 'MISSING' && current.enabled;
  $('session-verify').disabled = current.state === 'MISSING' || !current.enabled;

  if (current.platform === 'YouTube') {
    $('session-extra-hint').textContent = 'YouTube 建议通过浏览器插件导出 cookies.txt 文件后直接导入，以获得完整域名凭证。';
  } else if (current.platform === '哔哩哔哩' || current.platform === 'Pixiv') {
    $('session-extra-hint').textContent = `${current.platform} 支持点击上方“检测”执行真实账号有效性校验；可粘贴请求头（如 SESSDATA=...）或 Netscape 文件导入。`;
  } else {
    $('session-extra-hint').textContent = '凭据在本地加密保存，不会返回给 Agent；需要验证码或浏览器登录的平台请在外部浏览器完成后导入。';
  }
}

async function loadSessions() {
  sessionsData = await api('sessions');
  renderSessionCard();
}

// -------------------- Bot 连接信息摘要与配置表单 --------------------

function renderBotSummary(list) {
  const container = $('bot-status-summary');
  container.replaceChildren();
  for (const bot of list) {
    const card = document.createElement('div');
    card.className = 'bot-summary-card';
    const head = document.createElement('div');
    head.className = 'bot-summary-head';
    const title = document.createElement('strong');
    title.textContent = bot.adapter === 'telegram' ? 'Telegram Bot' : 'QQ / OneBot / NapCat';
    const badge = document.createElement('span');
    const st = bot.status || {};
    if (!bot.enabled) {
      badge.className = 'status-pill';
      badge.textContent = '未启用';
    } else if (st.connected === true) {
      badge.className = 'status-pill ok';
      badge.textContent = '已连接';
    } else if (st.connected === false) {
      badge.className = 'status-pill err';
      badge.textContent = '连接失败';
    } else {
      badge.className = 'status-pill info';
      badge.textContent = '已启用 · 待检测';
    }
    head.append(title, badge);
    card.append(head);

    const meta = document.createElement('div');
    meta.className = 'bot-summary-meta';
    const lines = [];
    if (st.connected === true) {
      if (bot.adapter === 'telegram') {
        const namePart = st.username ? `@${st.username}` : (st.first_name || 'Bot');
        lines.push(`账号：${namePart}（ID: ${st.bot_id || '-'} · ${st.mode === 'local' ? '本地 Bot API' : '云端 API'}）`);
      } else {
        const qqInfo = st.bot_id ? ` · 账号 ${st.nickname || ''} (${st.bot_id})` : '';
        lines.push(`协议端：${st.implementation || 'OneBot 11'} ${st.app_version || ''}${qqInfo}`);
      }
    } else if (st.connected === false) {
      lines.push(`错误：${st.message || st.error || '连接检测失败'}`);
    }
    lines.push(`地址：${bot.base_url || '未配置'} · 令牌：${bot.has_token ? '已保存' : '未配置'}`);
    lines.push(`许可目标：${bot.allowed_targets.length ? bot.allowed_targets.join(', ') : '未设置'} · 上限：${bytes(bot.max_upload_bytes)}`);
    if (st.checked_at) {
      lines.push(`上次检测：${new Date(st.checked_at * 1000).toLocaleTimeString()}`);
    }
    for (const line of lines) {
      const div = document.createElement('div');
      div.textContent = line;
      meta.append(div);
    }
    card.append(meta);
    container.append(card);
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
  renderBotSummary(bots);
  $('bot-forms').replaceChildren();
  for (const bot of bots) {
    const block = document.createElement('div');
    block.className = 'bot-block';
    const h = document.createElement('h4');
    h.textContent = bot.adapter === 'telegram' ? 'Telegram 连接设置' : 'QQ / OneBot / NapCat 连接设置';
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

    async function saveBotForm(silentValidation = false) {
      const value = {...bot};
      delete value.has_token;
      delete value.status;
      for (const input of form.elements) {
        if (!input.name) continue;
        value[input.name] = input.type === 'checkbox'
          ? input.checked
          : input.name === 'allowed_targets'
            ? input.value.split('\n').map(v => v.trim()).filter(Boolean)
            : input.name === 'max_upload_bytes'
              ? Number(input.value) * 1024**2
              : input.value.trim();
      }
      if (silentValidation && value.enabled && ((!value.token && !bot.has_token) || !value.allowed_targets.length)) {
        return;
      }
      await api('bots', 'PUT', value);
      secret.value = '';
      setSaveStatus('saved', '机器人配置已自动保存');
      if (!silentValidation) notify('机器人连接已保存');
      await loadBots();
    }

    let botAutoTimer;
    form.addEventListener('change', () => {
      clearTimeout(botAutoTimer);
      botAutoTimer = setTimeout(() => { saveBotForm(true).catch(() => {}); }, 250);
    });
    form.addEventListener('submit', event => {
      event.preventDefault();
      clearTimeout(botAutoTimer);
      action(save, () => saveBotForm(false));
    });
    probe.addEventListener('click', () => action(probe, async () => {
      await api('bots/' + bot.adapter + '/probe', 'POST');
      notify('机器人 API 已确认连接');
      await Promise.all([loadBots(), refresh()]);
    }));
    $('bot-forms').append(block);
  }
}

// -------------------- 服务状态轮询与刷新 --------------------

async function refresh(syncBots = true) {
  const status = await api('status');
  const cap = status.capabilities;
  $('version').textContent = cap.version;
  $('connection').textContent = '服务已连接';
  if (syncBots && Array.isArray(status.bots)) renderBotSummary(status.bots);

  const entries = [
    ['MCP 服务', '运行中 · 免令牌直连（Streamable HTTP 与 stdio · 22 工具）'],
    ['HTTP 地址', location.origin + '/mcp'],
    ['下载指令前缀', '/us（合集、分集视频、全部图片）· /zip（打包压缩包发送）'],
    ['服务数据目录', status.data_path],
    ['媒体下载保存目录', status.download_dir || '未指定（仅存受控缓存）'],
    ['图文保存目录', status.image_dir || '未指定（跟随媒体下载目录）'],
    ['实际缓存目录', status.cache_path],
    ['下载中的任务', String((status.tasks.DOWNLOADING || 0) + (status.tasks.PROCESSING || 0))],
    ['等待交付的任务', String(status.tasks.READY || 0)],
    ['缓存占用', bytes(status.cache_bytes) + ' / ' + bytes(settings.max_cache_bytes || 1)],
    ['FFmpeg', cap.ffmpeg ? '可用' : '不可用'],
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
  initAllSelects();
  settings = await api('config');
  populate();
  setSaveStatus('saved', '已自动保存');
  await Promise.all([loadSessions(), loadBots(), refresh()]);
  mcpConfig = await api('mcp-config');
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    Promise.all([refresh(), loadSessions()]).catch(() => {
      $('connection').textContent = '连接已断开';
    });
  }, 10000);
}

// -------------------- 事件绑定 --------------------

const formEl = $('settings-form');
formEl.addEventListener('change', e => {
  const t = e.target;
  if (!t || !t.name) return;
  const immediate = t.tagName === 'SELECT' || t.type === 'checkbox';
  scheduleAutoSave(immediate ? 80 : 120, true);
});
formEl.addEventListener('input', e => {
  const t = e.target;
  if (!t || !t.name || t.tagName === 'SELECT' || t.type === 'checkbox') return;
  scheduleAutoSave(600, false);
});

document.querySelectorAll('.clear-dir').forEach(btn => {
  btn.addEventListener('click', () => {
    const fieldEl = formEl.elements[btn.dataset.target];
    if (fieldEl) {
      fieldEl.value = '';
      scheduleAutoSave(80, true);
    }
  });
});

$('theme').addEventListener('click', () => {
  const nextTheme = isCurrentlyDark() ? 'light' : 'dark';
  settings.theme = nextTheme;
  setTheme(nextTheme);
  api('config', 'PATCH', {theme: nextTheme}).then(() => {
    setSaveStatus('saved', '外观已保存');
  }).catch(err => {
    setSaveStatus('error', '外观保存失败');
    notify(err.message, true);
  });
});

$('reset-settings').addEventListener('click', () => action($('reset-settings'), async () => {
  if (!confirm('确定将服务设置恢复为默认值吗？（已保存的平台 Cookie 与机器人连接保持不变）')) return;
  clearTimeout(autoSaveTimer);
  const res = await api('config/reset', 'POST', {reset_cache_dir: false});
  settings = res.settings;
  populate();
  setSaveStatus('saved', '已重置默认设置');
  await refresh();
  notify(res.cache_dir_preserved ? '设置已重置（缓存目录因含未清理文件而保留）' : '已重置为默认设置并保存');
}));

$('cookie-platform').addEventListener('change', e => {
  selectedPlatform = e.target.value;
  renderSessionCard();
});

$('session-disable-switch').addEventListener('change', async e => {
  const disable = e.target.checked;
  const suffix = disable ? '/disable' : '/enable';
  try {
    await api('sessions/' + encodeURIComponent(selectedPlatform) + suffix, 'POST');
    notify(disable ? `${selectedPlatform} Cookie 已禁用（会话仍加密保留）` : `${selectedPlatform} Cookie 已启用`);
    await loadSessions();
  } catch (err) {
    e.target.checked = !disable;
    notify(err.message, true);
  }
});

$('session-clear').addEventListener('click', () => action($('session-clear'), async () => {
  await api('sessions/' + encodeURIComponent(selectedPlatform), 'DELETE');
  notify(`${selectedPlatform} Cookie 已清除`);
  await loadSessions();
}));

$('session-verify').addEventListener('click', () => action($('session-verify'), async () => {
  const result = await api('sessions/' + encodeURIComponent(selectedPlatform) + '/verify', 'POST');
  notify(result.state === 'INVALID' ? '凭据已失效，请重新导入' : `${selectedPlatform} 账号状态有效`);
  await loadSessions();
}));

$('cookie-file').addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    await api('sessions', 'POST', {platform: selectedPlatform, text});
    e.target.value = '';
    notify(`${selectedPlatform} cookies.txt 已加密导入并启用`);
    await loadSessions();
  } catch (err) {
    e.target.value = '';
    notify(err.message, true);
  }
});

$('import-cookie').addEventListener('click', () => action($('import-cookie'), async () => {
  await api('sessions', 'POST', {platform: selectedPlatform, text: $('cookie-text').value});
  $('cookie-text').value = '';
  notify(`${selectedPlatform} 凭据已加密保存并启用`);
  await loadSessions();
}));

$('refresh').addEventListener('click', () => action($('refresh'), async () => {
  settings = await api('config');
  populate();
  await Promise.all([loadSessions(), loadBots(), refresh()]);
  notify('状态已同步');
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
  setSaveStatus('error', '未连接');
  notify(e.message, true);
});
