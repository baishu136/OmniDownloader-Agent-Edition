'use strict';
const $ = id => document.getElementById(id);
let settings = {}, bots = [], mcpConfig = {}, sessionsData = [];
let selectedPlatform = '';
let noticeTimer, refreshTimer, autoSaveTimer, qrTimer, browserLoginTimer;
let activeQrId = '', activeBrowserAttempt = '', activeBrowserPlatform = '', activeBrowserMulti = false;
let sessionActivityByPlatform = {};
let dirModalTargetEl = null, dirModalCurrent = '', dirModalParent = '';
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

// -------------------- 深浅色主题 --------------------

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

// -------------------- 美化动画下拉组件 --------------------

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

// -------------------- 设置表单序列化、保存与文件夹选择器 --------------------

function populate() {
  const form = $('settings-form');
  const displaySettings = {...settings};
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

async function saveSettingsNow(showSuccessNotice = false, showErrorNotice = true, successText = '设置已保存') {
  const form = $('settings-form');
  if (!form.checkValidity()) {
    setSaveStatus('error', '请检查输入值范围');
    if (showErrorNotice) form.reportValidity();
    return false;
  }
  const next = collectSettings();
  setSaveStatus('saving', '正在保存...');
  let ok = false;
  savingPromise = savingPromise.then(async () => {
    await api('config', 'PUT', next);
    settings = next;
    setSaveStatus('saved', '已保存');
    ok = true;
    await refresh(false);
    if (showSuccessNotice) notify(successText);
  }).catch(err => {
    setSaveStatus('error', '保存失败');
    if (showErrorNotice) notify(err.message, true);
  });
  await savingPromise;
  return ok;
}

function scheduleAutoSave(delay = 150, showError = false) {
  clearTimeout(autoSaveTimer);
  setSaveStatus('saving', '准备保存...');
  autoSaveTimer = setTimeout(() => { saveSettingsNow(false, showError); }, delay);
}

async function loadDirModalPath(path = '') {
  const res = await api('fs/list', 'POST', {path});
  dirModalCurrent = res.current || '';
  dirModalParent = res.parent ?? '';
  $('dir-modal-path').value = dirModalCurrent || '此电脑（驱动器列表）';
  $('dir-modal-new').disabled = !res.can_create;
  $('dir-modal-confirm').disabled = !dirModalCurrent;

  const list = $('dir-modal-list');
  list.replaceChildren();
  for (const item of res.entries || []) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'dir-item';
    btn.textContent = item.name;
    btn.addEventListener('click', () => { loadDirModalPath(item.path).catch(e => notify(e.message, true)); });
    list.append(btn);
  }
  if (!list.children.length) {
    const emptyMsg = document.createElement('p');
    emptyMsg.textContent = '此目录下没有子文件夹';
    emptyMsg.className = 'hint';
    list.append(emptyMsg);
  }
}

function closeDirModal() {
  $('dir-browser-modal').hidden = true;
  dirModalTargetEl = null;
}

async function openDirectoryPicker(inputEl, titleText = '选择文件夹', onPicked = null) {
  const initial = inputEl?.value?.trim() || '';
  const picked = await api('pick-directory', 'POST', {initial_dir: initial, title: titleText});
  if (picked.supported) {
    if (!picked.cancelled && picked.path && inputEl) {
      inputEl.value = picked.path;
      if (onPicked) await onPicked(picked.path);
    }
    return;
  }
  dirModalTargetEl = {inputEl, onPicked};
  $('dir-modal-title').textContent = titleText;
  $('dir-browser-modal').hidden = false;
  await loadDirModalPath(initial).catch(() => loadDirModalPath(''));
}

// -------------------- 代理连通性测试 --------------------

function renderProxyTestResult(res) {
  const box = $('proxy-test-result');
  box.hidden = false;
  box.className = 'probe-result ' + (res.ok ? 'ok' : 'err');
  box.replaceChildren();
  const title = document.createElement('div');
  title.textContent = res.message;
  box.append(title);
  if (Array.isArray(res.targets) && res.targets.length) {
    const chips = document.createElement('div');
    chips.className = 'probe-chips';
    for (const item of res.targets) {
      const chip = document.createElement('span');
      chip.className = 'probe-chip ' + (item.ok ? 'ok' : 'err');
      chip.textContent = item.ok
        ? `${item.name} · HTTP ${item.status_code} · ${item.latency_ms} ms`
        : `${item.name} · ${item.error || '不可达'}`;
      chips.append(chip);
    }
    box.append(chips);
  }
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

// -------------------- Cookie 登录与会话管理 --------------------

function setSessionActivity(platform, kind, badge, text) {
  if (!platform) return;
  sessionActivityByPlatform[platform] = {kind, badge, text};
  if (platform === selectedPlatform) renderSessionActivity(platform);
}

function renderSessionActivity(platform) {
  const box = $('session-activity-box');
  const badgeEl = $('session-activity-badge');
  const textEl = $('session-activity-text');
  const entry = sessionActivityByPlatform[platform];
  if (!entry) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.className = 'capture-activity ' + (entry.kind || '');
  badgeEl.className = 'status-pill ' + (entry.kind || 'info');
  badgeEl.textContent = entry.badge || '状态';
  textEl.textContent = entry.text || '';
}

function stopQrLogin() {
  clearInterval(qrTimer);
  activeQrId = '';
  $('qr-login-panel').hidden = true;
}

function stopBrowserPoll() {
  clearInterval(browserLoginTimer);
  activeBrowserAttempt = '';
  activeBrowserPlatform = '';
  activeBrowserMulti = false;
  $('browser-login-panel').hidden = true;
  $('browser-ready-platforms').hidden = true;
}

function renderQrState(data) {
  const pill = $('qr-stage-pill');
  const bar = $('qr-progress-bar');
  const timerChip = $('qr-countdown-badge');
  const stageClass = {
    waiting: 'info',
    scanned: 'ok',
    confirmed: 'ok',
    expired: 'err',
    error: 'err',
  };
  pill.className = 'status-pill ' + (stageClass[data.status] || 'info');
  pill.textContent = data.stage_label || '等待扫码';
  const pct = Number(data.progress ?? (data.status === 'scanned' ? 75 : data.status === 'confirmed' ? 100 : 35));
  bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  if (typeof data.remaining_seconds === 'number') {
    timerChip.textContent = data.remaining_seconds > 0 ? `剩余 ${data.remaining_seconds}s` : '已失效';
  }
}

function renderBrowserLoginState(st) {
  const titleName = st.multi_platform ? '多平台托管工作窗' : st.platform;
  $('browser-status-title').textContent = `${titleName} · ${st.browser_name || '本地浏览器'}`;
  const stagePill = $('browser-stage-pill');
  const stageClassMap = {
    LAUNCHING: 'info',
    CONNECTING: 'info',
    WAITING_LOGIN: 'info',
    CAPTURING: 'info',
    CONFIRMING: 'ok',
    SAVING: 'ok',
    VERIFYING: 'ok',
    COMPLETED: 'ok',
    CANCELLED: 'warn',
    FAILED: 'err',
  };
  stagePill.className = 'status-pill ' + (stageClassMap[st.stage] || 'info');
  stagePill.textContent = st.stage_label || '处理中';

  const pct = Number(st.progress ?? 20);
  $('browser-progress-bar').style.width = `${Math.max(5, Math.min(100, pct))}%`;

  const stepOrder = ['launch', 'connect', 'capture', 'save'];
  const stageToIndex = {
    LAUNCHING: 0,
    CONNECTING: 1,
    WAITING_LOGIN: 2,
    CAPTURING: 2,
    CONFIRMING: 2,
    SAVING: 3,
    VERIFYING: 3,
    COMPLETED: 4,
  };
  const currentIdx = stageToIndex[st.stage] ?? 0;
  for (const el of $('browser-steps').children) {
    const idx = stepOrder.indexOf(el.dataset.step);
    el.className = idx < currentIdx ? 'done' : idx === currentIdx ? 'active' : '';
  }

  const telemetry = $('browser-telemetry');
  telemetry.replaceChildren();
  const chips = [
    [st.cdp_connected ? 'ok' : '', st.cdp_connected ? 'CDP 通道已连接' : '正在建立通道'],
    [st.current_host ? 'ok' : '', st.current_host ? `当前域：${st.current_host}` : '等待页面域'],
    [
      st.cookie_count > 0 ? (st.marker_detected ? 'ok' : '') : '',
      st.cookie_count > 0 ? `已捕获 ${st.cookie_count} 项 Cookie` : '尚未检测到平台 Cookie',
    ],
    [
      st.marker_detected ? 'ok' : '',
      st.marker_detected
        ? (st.multi_platform ? '已识别平台登录态' : '已识别核心登录标记')
        : (st.multi_platform ? '等待任意平台登录' : (st.auto_capture ? '等待登录态标记' : '需登录后手动抓取')),
    ],
    ['', `已用 ${st.elapsed_seconds || 0}s`],
  ];
  for (const [cls, text] of chips) {
    const chip = document.createElement('span');
    chip.className = 'probe-chip' + (cls ? ' ' + cls : '');
    chip.textContent = text;
    telemetry.append(chip);
  }

  const readyWrap = $('browser-ready-platforms');
  readyWrap.replaceChildren();
  if (st.multi_platform && Array.isArray(st.ready_platforms) && st.ready_platforms.length) {
    readyWrap.hidden = false;
    for (const item of st.ready_platforms) {
      const pChip = document.createElement('span');
      pChip.className = 'probe-chip' + (item.marker_detected ? ' ok' : '');
      const tag = item.auto_sync_ready ? '已就绪' : (item.marker_detected ? '可保存' : '基础Cookie');
      pChip.textContent = `${item.platform} · ${item.cookie_count}项（${tag}）`;
      readyWrap.append(pChip);
    }
  } else {
    readyWrap.hidden = true;
  }

  $('browser-manual-capture').textContent = st.multi_platform ? '保存全部已登录平台' : '我已登录，抓取保存';
  $('browser-status-text').textContent = st.message || '';
}

function drawQrMatrix(matrix) {
  const canvas = $('qr-canvas');
  if (!canvas || !Array.isArray(matrix) || !matrix.length) return;
  const ctx = canvas.getContext('2d');
  const size = canvas.width;
  const n = matrix.length;
  const cell = size / n;
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = '#111827';
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (matrix[r][c]) ctx.fillRect(Math.floor(c * cell), Math.floor(r * cell), Math.ceil(cell), Math.ceil(cell));
    }
  }
}

async function startBilibiliQrLogin() {
  stopQrLogin();
  setSessionActivity('哔哩哔哩', 'info', '生成二维码', '正在请求哔哩哔哩官方登录二维码...');
  const res = await api('sessions/bilibili/qr', 'POST');
  activeQrId = res.qr_id;
  drawQrMatrix(res.matrix);
  renderQrState({status: 'waiting', stage_label: '等待扫码', progress: 25, remaining_seconds: res.expires_in || 180});
  $('qr-status-text').textContent = '请使用手机哔哩哔哩 App 扫描二维码';
  $('qr-login-panel').hidden = false;
  setSessionActivity('哔哩哔哩', 'info', '等待扫码', '二维码已生成，请使用哔哩哔哩手机端扫码确认登录');
  qrTimer = setInterval(async () => {
    if (!activeQrId) return;
    try {
      const poll = await api('sessions/bilibili/qr/' + encodeURIComponent(activeQrId) + '/poll', 'POST');
      renderQrState(poll);
      if (poll.message) $('qr-status-text').textContent = poll.message;
      if (poll.status === 'scanned') {
        setSessionActivity('哔哩哔哩', 'info', '已扫码', '手机已扫码，请在 App 上点击确认登录');
      } else if (poll.status === 'confirmed') {
        stopQrLogin();
        const msg = poll.message || '哔哩哔哩扫码登录成功，凭据已加密保存并启用';
        setSessionActivity('哔哩哔哩', 'ok', '扫码登录成功', msg);
        notify(msg);
        await loadSessions();
      } else if (poll.status === 'expired' || poll.status === 'error') {
        clearInterval(qrTimer);
        setSessionActivity('哔哩哔哩', 'err', poll.status === 'expired' ? '二维码失效' : '扫码异常', poll.message || '请刷新二维码重试');
      }
    } catch (err) {
      $('qr-status-text').textContent = err.message;
      setSessionActivity('哔哩哔哩', 'err', '扫码失败', err.message);
      clearInterval(qrTimer);
    }
  }, 1500);
}

function pollBrowserAttempt(res, targetPlatform, isMulti) {
  activeBrowserAttempt = res.attempt_id;
  activeBrowserPlatform = targetPlatform;
  activeBrowserMulti = isMulti;
  renderBrowserLoginState(res);
  $('browser-login-panel').hidden = false;

  browserLoginTimer = setInterval(async () => {
    if (!activeBrowserAttempt) return;
    try {
      const st = await api('sessions/browser-status/' + encodeURIComponent(activeBrowserAttempt));
      renderBrowserLoginState(st);
      if (st.state === 'COMPLETED') {
        stopBrowserPoll();
        const msg = st.message || `${targetPlatform} Cookie 已通过本地浏览器保存`;
        const badge = st.capture_mode === 'salvaged' ? '关窗兜底保存' : (isMulti ? '批量同步成功' : '抓取成功');
        setSessionActivity(targetPlatform, 'ok', badge, msg);
        notify(msg);
        await loadSessions();
      } else if (st.state === 'CANCELLED' || st.state === 'FAILED') {
        stopBrowserPoll();
        if (st.state === 'FAILED') {
          const errMsg = st.error || st.message || '本地浏览器登录失败';
          setSessionActivity(targetPlatform, 'err', '抓取失败', errMsg);
          notify(errMsg, true);
        } else {
          setSessionActivity(targetPlatform, 'warn', '窗口已关闭', st.message || '已取消或关闭浏览器登录窗口');
        }
      }
    } catch {
      stopBrowserPoll();
    }
  }, 700);
}

async function startBrowserLogin(platform) {
  stopBrowserPoll();
  setSessionActivity(platform, 'info', '启动登录', `正在启动持久化托管浏览器登录 ${platform}...`);
  const res = await api('sessions/' + encodeURIComponent(platform) + '/browser-login', 'POST');
  pollBrowserAttempt(res, platform, false);
}

async function startMultiBrowserLogin() {
  stopBrowserPoll();
  const displayTarget = selectedPlatform || '哔哩哔哩';
  setSessionActivity(displayTarget, 'info', '启动多平台窗', '正在打开持久化托管浏览器，可一次性登录并同步多个平台...');
  const res = await api('sessions/browser-workspace/start', 'POST');
  pollBrowserAttempt(res, displayTarget, true);
}

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
    pill.textContent = '凭据失效 · 请重新登录或导入';
    pill.className = 'status-pill err';
  } else {
    pill.textContent = '已导入 · ' + (labels[current.validation.state] || '未检测');
    pill.className = 'status-pill info';
  }

  const meta = current.ui || {};
  const metaParts = [];
  const sourceLabels = {
    browser_auto: '本地浏览器自动捕获',
    browser_manual: '本地浏览器手动抓取',
    browser_salvage: '本地浏览器关窗兜底保存',
    browser_sync: '托管浏览器免密同步',
    qr: 'B站官方扫码',
    import: '手动文件/文本导入',
  };
  if (meta.cookie_count) {
    metaParts.push(`共 ${meta.cookie_count} 项会话条目`);
    metaParts.push(meta.marker_present ? '包含核心登录标记' : '暂未检测到登录态标记');
    if (meta.source) metaParts.push('来源：' + (sourceLabels[meta.source] || meta.source));
    if (meta.updated_at) metaParts.push('保存于 ' + new Date(meta.updated_at * 1000).toLocaleTimeString());
    if (meta.earliest_expiry) {
      metaParts.push('最早有效期至 ' + new Date(meta.earliest_expiry * 1000).toLocaleDateString());
    }
  }
  if (current.validation?.checked_at) {
    metaParts.push('上次校验 ' + new Date(current.validation.checked_at * 1000).toLocaleTimeString());
  }
  $('session-meta-hint').textContent = metaParts.join(' · ');
  renderSessionActivity(current.platform);

  const profilePill = $('browser-profile-pill');
  const hasBrowser = current.local_browser_available !== false;
  if (!hasBrowser) {
    profilePill.textContent = '无本机 Chrome/Edge';
    profilePill.className = 'status-pill';
  } else if (current.browser_profile_active) {
    profilePill.textContent = '托管窗口运行中';
    profilePill.className = 'status-pill ok';
  } else if (current.browser_profile_exists) {
    profilePill.textContent = '已建立持久档案（可免密同步）';
    profilePill.className = 'status-pill info';
  } else {
    profilePill.textContent = '首次登录将自动保留档案';
    profilePill.className = 'status-pill';
  }
  $('browser-workspace-start').disabled = !hasBrowser;
  $('browser-profile-sync').disabled = !hasBrowser || (!current.browser_profile_exists && !current.browser_profile_active);
  $('browser-profile-reset').disabled = !current.browser_profile_exists && !current.browser_profile_active;

  $('session-clear').disabled = current.state === 'MISSING' && current.enabled;
  $('session-verify').disabled = current.state === 'MISSING' || !current.enabled;
  $('session-qr-login').hidden = !current.has_qr;
  const brBtn = $('session-browser-login');
  brBtn.disabled = current.local_browser_available === false;
  brBtn.title = current.local_browser_available === false
    ? '当前环境未检测到本机 Chrome / Edge / Chromium，请使用默认浏览器打开后导入，或使用官方扫码'
    : `使用本机 ${current.local_browser_name || 'Chrome / Edge'} 打开独立临时窗口并自动读取 Cookie`;
  const openBtn = $('session-open-browser');
  openBtn.disabled = !current.login_url;
  openBtn.title = current.login_url ? `使用系统默认浏览器打开 ${current.login_url}` : '当前平台暂无直达登录地址';

  if (current.platform !== '哔哩哔哩' && activeQrId) stopQrLogin();

  if (current.platform === 'YouTube') {
    $('session-extra-hint').textContent = 'YouTube 如遇 Google 登录拦截，建议先点“默认浏览器打开”完成登录，再通过浏览器插件导出 cookies.txt 或 Cookie-Editor JSON 导入。';
  } else if (current.platform === '哔哩哔哩') {
    $('session-extra-hint').textContent = '哔哩哔哩支持“B站扫码登录”、“本地浏览器登录”及多格式导入；获取凭据后可点击“检测”验证账号有效性。';
  } else if (current.platform === 'Pixiv') {
    $('session-extra-hint').textContent = 'Pixiv 推荐通过“本地浏览器登录”完成登录后自动读取（或点击“我已登录，抓取保存”），再点“检测”确认账号状态。';
  } else if (current.login_hint) {
    $('session-extra-hint').textContent = `${current.login_hint}；凭据仅本地加密保存，不会回显明文。`;
  } else {
    $('session-extra-hint').textContent = '推荐使用“本地浏览器登录”，也可点“默认浏览器打开”登录后粘贴请求头、Netscape 文件或 Cookie-Editor JSON 数组。';
  }
}

async function loadSessions() {
  sessionsData = await api('sessions');
  renderSessionCard();
}

// -------------------- 已识别桥接与 Bot 摘要 --------------------

function renderBridges(bridges = [], hints = []) {
  const list = $('bridge-list');
  list.replaceChildren();
  const kindLabel = {
    mcp_stdio: 'MCP stdio 桥接',
    mcp_http: 'Streamable HTTP MCP',
    astrbot: 'AstrBot 机器人插件',
    bot_client: 'Bot 客户端桥接',
    http_api: 'HTTP API 桥接',
  };
  const statusLabel = {online: ['在线', 'ok'], idle: ['空闲', 'info'], offline: ['已离线', '']};
  for (const b of bridges) {
    const card = document.createElement('div');
    card.className = 'bridge-card';
    const head = document.createElement('div');
    head.className = 'bot-summary-head';
    const title = document.createElement('strong');
    title.textContent = b.name || kindLabel[b.kind] || b.id;
    const badge = document.createElement('span');
    const [stText, stCls] = statusLabel[b.status] || ['未知', ''];
    badge.className = 'status-pill ' + stCls;
    badge.textContent = stText;
    head.append(title, badge);
    card.append(head);

    const meta = document.createElement('div');
    meta.className = 'bot-summary-meta';
    const lines = [
      `类型：${kindLabel[b.kind] || b.kind}（${b.transport || 'http'}）${b.client_name ? ' · 客户端 ' + b.client_name + (b.client_version ? ' ' + b.client_version : '') : ''}`,
      `调用次数：${b.call_count || 0} · 最近活跃：${new Date(b.last_seen * 1000).toLocaleTimeString()}`,
    ];
    if (Array.isArray(b.routes) && b.routes.length) {
      lines.push(`已配置投递路由：${b.routes.join(', ')}`);
    }
    for (const line of lines) {
      const div = document.createElement('div');
      div.textContent = line;
      meta.append(div);
    }
    card.append(meta);
    list.append(card);
  }
  empty(list, '暂未检测到接入的 MCP 或机器人插件客户端（启动 Agent、stdio 桥或插件调用后会自动出现在这里）');

  const hintsBox = $('bridge-hints');
  hintsBox.replaceChildren();
  for (const h of hints) {
    row(hintsBox, `${h.name} · ${h.hint}`);
  }
}

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
      const sharedLabel = document.createElement('label');
      sharedLabel.textContent = '服务端共享目录';
      const rowWrap = document.createElement('div');
      rowWrap.className = 'input-row';
      const sharedInput = document.createElement('input');
      sharedInput.name = 'shared_root';
      sharedInput.value = bot.shared_root ?? '';
      const pickBtn = document.createElement('button');
      pickBtn.type = 'button';
      pickBtn.textContent = '选择文件夹';
      pickBtn.addEventListener('click', () => action(pickBtn, () =>
        openDirectoryPicker(sharedInput, '选择服务端共享目录', async () => { await saveBotForm(true); })
      ));
      rowWrap.append(sharedInput, pickBtn);
      sharedLabel.append(rowWrap);
      grid.append(sharedLabel);
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
  renderBridges(status.bridges || [], status.bridge_hints || []);

  const entries = [
    ['MCP 服务', '运行中 · 免令牌直连（Streamable HTTP 与 stdio · 23 工具）'],
    ['HTTP 地址', location.origin + '/mcp'],
    ['下载指令前缀', '/us（合集、分集视频、全部图片）· /zip（打包压缩包发送）'],
    ['服务数据目录', status.data_path],
    ['媒体下载保存目录', status.download_dir || '未指定（仅存受控缓存）'],
    ['图文保存目录', status.image_dir || '未指定（跟随媒体下载目录）'],
    ['实际缓存目录', status.cache_path],
    ['下载中的任务', String((status.tasks.DOWNLOADING || 0) + (status.tasks.PROCESSING || 0))],
    ['等待交付的任务', String(status.tasks.READY || 0)],
    ['缓存占用', bytes(status.cache_bytes) + ' / ' + bytes(settings.max_cache_bytes || 1)],
    ['FFmpeg / 本地浏览器', `${cap.ffmpeg ? 'FFmpeg 可用' : 'FFmpeg 不可用'} · ${cap.local_browser ? '检测到 ' + cap.local_browser : '未检测到 Chrome/Edge（可默认浏览器打开后导入）'}`],
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
    const errText = delivery.error_message || delivery.error || '';
    row($('deliveries'), `${delivery.adapter} → ${delivery.target} · ${delivery.state}${errText ? ' · ' + errText : ''}`, ['FAILED', 'UNCERTAIN'].includes(delivery.state) ? '重试' : '', async () => {
      if (delivery.state === 'UNCERTAIN' && !confirm('平台可能已收到文件。请先核对机器人端记录，确认允许再次发送后继续。')) return;
      await api('deliveries/' + delivery.id + '/retry', 'POST', {reconciled_allow_retry: delivery.state === 'UNCERTAIN'});
      await refresh();
    });
  }
  empty($('deliveries'), '没有交付记录');
  for (const error of status.errors) row($('errors'), `${error.id.slice(0, 8)} · ${error.error_message || error.error}`);
  empty($('errors'), '暂无错误');
  if (status.cleanup_error) row($('errors'), status.cleanup_error + ' · 文件清理已延后，平台发送状态不受影响');
  for (const entry of status.cleanup) row($('cleanup-log'), `${new Date(entry.created * 1000).toLocaleString()} · 清理 ${entry.files} 个文件 · ${bytes(entry.bytes)}`);
  empty($('cleanup-log'), '暂无清理记录');
}

async function init() {
  initAllSelects();
  settings = await api('config');
  populate();
  setSaveStatus('saved', '已保存');
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
const manualSaveFields = new Set(['download_dir', 'image_dir', 'cache_dir', 'proxy']);
formEl.addEventListener('change', e => {
  const t = e.target;
  if (!t || !t.name) return;
  const immediate = t.tagName === 'SELECT' || t.type === 'checkbox';
  scheduleAutoSave(immediate ? 80 : 120, true);
});
formEl.addEventListener('input', e => {
  const t = e.target;
  if (!t || !t.name || t.tagName === 'SELECT' || t.type === 'checkbox') return;
  if (manualSaveFields.has(t.name)) {
    setSaveStatus('saving', '未保存（请点击保存）');
    clearTimeout(autoSaveTimer);
    return;
  }
  scheduleAutoSave(600, false);
});

document.querySelectorAll('.pick-dir').forEach(btn => {
  btn.addEventListener('click', () => action(btn, async () => {
    const fieldEl = formEl.elements[btn.dataset.target];
    if (!fieldEl) return;
    await openDirectoryPicker(fieldEl, '选择文件夹', async () => {
      await saveSettingsNow(true, true, '目录已选择并保存');
    });
  }));
});

document.querySelectorAll('.save-field').forEach(btn => {
  btn.addEventListener('click', () => action(btn, async () => {
    clearTimeout(autoSaveTimer);
    await saveSettingsNow(true, true, '设置已保存');
  }));
});

document.querySelectorAll('.clear-dir').forEach(btn => {
  btn.addEventListener('click', () => action(btn, async () => {
    const fieldEl = formEl.elements[btn.dataset.target];
    if (fieldEl) {
      fieldEl.value = '';
      clearTimeout(autoSaveTimer);
      await saveSettingsNow(true, true, '已清空并保存');
    }
  }));
});

$('save-visual').addEventListener('click', () => action($('save-visual'), async () => {
  clearTimeout(autoSaveTimer);
  await saveSettingsNow(true, true, '下载可视化通知规则已保存');
}));

$('test-proxy').addEventListener('click', () => action($('test-proxy'), async () => {
  const proxyVal = formEl.elements['proxy'].value.trim();
  const res = await api('proxy/test', 'POST', {proxy: proxyVal});
  renderProxyTestResult(res);
  notify(res.ok ? '代理连通性测试通过' : '代理连通性测试存在失败目标，请查看下方详情', !res.ok);
}));

$('dir-modal-close').addEventListener('click', closeDirModal);
$('dir-modal-cancel').addEventListener('click', closeDirModal);
$('dir-modal-up').addEventListener('click', () => loadDirModalPath(dirModalParent).catch(e => notify(e.message, true)));
$('dir-modal-new').addEventListener('click', async () => {
  if (!dirModalCurrent) return;
  const folder = prompt('请输入新建文件夹名称：');
  if (!folder || !folder.trim()) return;
  try {
    const res = await api('fs/mkdir', 'POST', {parent: dirModalCurrent, name: folder.trim()});
    await loadDirModalPath(res.path);
  } catch (e) {
    notify(e.message, true);
  }
});
$('dir-modal-confirm').addEventListener('click', async () => {
  if (!dirModalCurrent || !dirModalTargetEl) return;
  const {inputEl, onPicked} = dirModalTargetEl;
  const chosen = dirModalCurrent;
  closeDirModal();
  if (inputEl) {
    inputEl.value = chosen;
    if (onPicked) await onPicked(chosen).catch(e => notify(e.message, true));
  }
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
  const target = selectedPlatform;
  const suffix = disable ? '/disable' : '/enable';
  try {
    await api('sessions/' + encodeURIComponent(target) + suffix, 'POST');
    const msg = disable ? `${target} Cookie 已禁用（会话仍加密保留）` : `${target} Cookie 已启用`;
    setSessionActivity(target, disable ? 'warn' : 'ok', disable ? '已禁用' : '已启用', msg);
    notify(msg);
    await loadSessions();
  } catch (err) {
    e.target.checked = !disable;
    setSessionActivity(target, 'err', '切换失败', err.message);
    notify(err.message, true);
  }
});

$('session-clear').addEventListener('click', () => action($('session-clear'), async () => {
  const target = selectedPlatform;
  await api('sessions/' + encodeURIComponent(target), 'DELETE');
  setSessionActivity(target, 'warn', '已清除', `${target} Cookie 凭据已从本地加密存储中移除`);
  notify(`${target} Cookie 已清除`);
  await loadSessions();
}));

$('session-verify').addEventListener('click', () => action($('session-verify'), async () => {
  const target = selectedPlatform;
  setSessionActivity(target, 'info', '检测中', `正在在线校验 ${target} 登录凭据有效性...`);
  try {
    const result = await api('sessions/' + encodeURIComponent(target) + '/verify', 'POST');
    if (result.state === 'INVALID') {
      setSessionActivity(target, 'err', '凭据已失效', `${target} 账号校验未通过，请重新登录或导入有效 Cookie`);
      notify('凭据已失效，请重新登录或导入', true);
    } else {
      setSessionActivity(target, 'ok', '检测有效', `${target} 登录态校验通过，凭据可正常使用`);
      notify(`${target} 账号状态有效`);
    }
    await loadSessions();
  } catch (err) {
    setSessionActivity(target, 'err', '检测出错', err.message);
    throw err;
  }
}));

$('session-browser-login').addEventListener('click', () => action($('session-browser-login'), async () => {
  await startBrowserLogin(selectedPlatform);
}));

$('browser-workspace-start').addEventListener('click', () => action($('browser-workspace-start'), async () => {
  await startMultiBrowserLogin();
}));

$('browser-profile-sync').addEventListener('click', () => action($('browser-profile-sync'), async () => {
  const target = selectedPlatform;
  setSessionActivity(target, 'info', '免密同步中', '正在从持久化托管浏览器读取并同步全部已登录平台 Cookie...');
  try {
    const res = await api('sessions/browser-profile/sync', 'POST', {include_manual: false});
    const synced = res.synced_platforms || [];
    const skipped = res.skipped_platforms || [];
    if (synced.length) {
      const modeTip = res.used_headless ? '后台无感同步完成' : '实时同步完成';
      const skipTip = skipped.length ? `；另有 ${skipped.join('、')} 需在对应平台手动点“抓取保存”` : '';
      const msg = `${modeTip}：已更新 ${synced.join('、')}（共 ${res.total_cookies || 0} 项 Cookie）${skipTip}`;
      setSessionActivity(target, 'ok', '免密同步成功', msg);
      notify(msg);
    } else {
      const msg = skipped.length
        ? `仅发现 ${skipped.join('、')} 的基础或待确认 Cookie，请打开“多平台批量登录”完成账号登录`
        : '托管浏览器中暂未发现有效平台登录态，请先点击“多平台批量登录”完成登录';
      setSessionActivity(target, 'warn', '暂无可同步平台', msg);
      notify(msg, true);
    }
    await loadSessions();
  } catch (err) {
    setSessionActivity(target, 'err', '同步失败', err.message);
    throw err;
  }
}));

$('browser-profile-reset').addEventListener('click', () => action($('browser-profile-reset'), async () => {
  if (!confirm('确定清空专用托管浏览器的缓存与登录档案吗？（不影响你的日常主浏览器，也不清除已加密保存进会话库的 Cookie）')) return;
  await api('sessions/browser-profile/reset', 'POST');
  setSessionActivity(selectedPlatform, 'warn', '已清理浏览器档案', '专用托管浏览器档案已重置为空白状态，不影响已加密保存的会话库');
  notify('托管浏览器档案已清空');
  await loadSessions();
}));

$('session-open-browser').addEventListener('click', () => action($('session-open-browser'), async () => {
  const target = selectedPlatform;
  const res = await api('sessions/' + encodeURIComponent(target) + '/open-login', 'POST');
  if (res.opened === false) {
    setSessionActivity(target, 'warn', '需手动访问', `无法自动拉起系统默认浏览器，请手动打开：${res.url}`);
    notify('无法自动拉起系统默认浏览器，请手动访问：' + res.url, true);
  } else {
    setSessionActivity(target, 'info', '已外部打开', `已在系统默认浏览器打开 ${target} 登录页，登录后可使用浏览器扩展导出并粘贴保存`);
    notify(`已在系统默认浏览器打开 ${target} 登录页；登录后可使用 Cookie-Editor 等插件导出并粘贴导入`);
  }
}));

$('browser-manual-capture').addEventListener('click', () => action($('browser-manual-capture'), async () => {
  if (!activeBrowserAttempt) return;
  const path = activeBrowserMulti
    ? 'sessions/browser-workspace/capture'
    : 'sessions/' + encodeURIComponent(activeBrowserPlatform) + '/browser-capture';
  await api(path, 'POST');
  $('browser-stage-pill').className = 'status-pill info';
  $('browser-stage-pill').textContent = '即时抓取中';
  $('browser-status-text').textContent = '已触发即时抓取指令，正在读取当前会话完整 Cookie 并加密保存...';
}));

$('browser-cancel').addEventListener('click', () => action($('browser-cancel'), async () => {
  const target = activeBrowserPlatform || selectedPlatform;
  const path = activeBrowserMulti
    ? 'sessions/browser-workspace/cancel'
    : 'sessions/' + encodeURIComponent(target) + '/browser-cancel';
  await api(path, 'POST');
  stopBrowserPoll();
  setSessionActivity(target, 'warn', '已关闭登录窗', '已取消并关闭本地托管浏览器登录窗口');
}));

$('session-qr-login').addEventListener('click', () => action($('session-qr-login'), startBilibiliQrLogin));
$('qr-refresh').addEventListener('click', () => action($('qr-refresh'), startBilibiliQrLogin));
$('qr-cancel').addEventListener('click', () => {
  stopQrLogin();
  setSessionActivity('哔哩哔哩', 'warn', '已取消扫码', '已取消本次哔哩哔哩二维码登录');
});

$('cookie-file').addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  const target = selectedPlatform;
  setSessionActivity(target, 'info', '导入中', `正在解析并加密导入 ${target} Cookie 文件...`);
  try {
    const text = await file.text();
    const info = await api('sessions', 'POST', {platform: target, text});
    e.target.value = '';
    const count = info?.ui?.cookie_count || 0;
    const msg = `${target} Cookie 文件已加密导入并启用${count ? `（共 ${count} 项）` : ''}`;
    setSessionActivity(target, 'ok', '文件导入成功', msg);
    notify(msg);
    await loadSessions();
  } catch (err) {
    e.target.value = '';
    setSessionActivity(target, 'err', '导入失败', err.message);
    notify(err.message, true);
  }
});

$('import-cookie').addEventListener('click', () => action($('import-cookie'), async () => {
  const target = selectedPlatform;
  setSessionActivity(target, 'info', '保存中', `正在校验并加密保存 ${target} 凭据...`);
  try {
    const info = await api('sessions', 'POST', {platform: target, text: $('cookie-text').value});
    $('cookie-text').value = '';
    const count = info?.ui?.cookie_count || 0;
    const msg = `${target} 凭据已加密保存并启用${count ? `（共 ${count} 项）` : ''}`;
    setSessionActivity(target, 'ok', '凭据已保存', msg);
    notify(msg);
    await loadSessions();
  } catch (err) {
    setSessionActivity(target, 'err', '保存失败', err.message);
    throw err;
  }
}));

$('scan-bridges').addEventListener('click', () => action($('scan-bridges'), async () => {
  const res = await api('bridges/scan', 'POST');
  renderBridges(res.bridges || [], res.hints || []);
  if (Array.isArray(res.bots)) renderBotSummary(res.bots);
  notify('桥接扫描与机器人探测已完成');
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
