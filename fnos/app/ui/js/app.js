/* AirConnect for fnOS —— 管理界面前端
 *
 * 两点必须遵守：
 *  1. 所有 API 都用**相对路径**（`api/status`）。应用挂在飞牛统一网关的
 *     `/app/airconnect/` 下，绝对路径 `/api/...` 会打到面板自身。
 *  2. 每个写操作都带 `X-Requested-With: XMLHttpRequest`（服务端的 CSRF 防护）。
 */
'use strict';

const LATENCY_PRESETS = ['500:0', '0:0', '1000:2000', '500:1000'];
const $ = (id) => document.getElementById(id);

const state = {
  status: null,
  settings: null,
  devices: [],
  interfaces: [],
  dirtySettings: false,
  logTimer: null,
  statusTimer: null,
};

/* ------------------------------------------------------------------ 基础 */
async function api(path, options = {}) {
  const opts = { method: options.method || 'GET', headers: {} };
  if (opts.method !== 'GET') {
    opts.headers['X-Requested-With'] = 'XMLHttpRequest';
    if (options.body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(options.body);
    }
  }
  const response = await fetch(path + window.location.search.replace(/^\?/, '?'), opts);
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { ok: false, error: text }; }
  if (!response.ok || data.ok === false) {
    throw new Error((data && data.error) || `HTTP ${response.status}`);
  }
  return data;
}

function banner(message, kind = 'info', timeout = 5000) {
  const el = $('banner');
  el.textContent = message;
  el.className = 'banner ' + kind;
  if (timeout) {
    clearTimeout(banner._t);
    banner._t = setTimeout(() => el.classList.add('hidden'), timeout);
  }
}

function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtUptime(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  if (seconds < 60) return seconds + ' 秒';
  const m = Math.floor(seconds / 60);
  if (m < 60) return m + ' 分钟';
  const h = Math.floor(m / 60);
  if (h < 24) return h + ' 小时 ' + (m % 60) + ' 分';
  return Math.floor(h / 24) + ' 天 ' + (h % 24) + ' 小时';
}

/* ------------------------------------------------------------------ Tab */
$('tabs').addEventListener('click', (event) => {
  const button = event.target.closest('.tab');
  if (!button) return;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('is-active', t === button));
  document.querySelectorAll('.panel').forEach((p) => {
    p.classList.toggle('is-active', p.dataset.panel === button.dataset.tab);
  });
  if (button.dataset.tab === 'logs') loadLogs();
  if (button.dataset.tab === 'devices') loadDevices();
  if (button.dataset.tab === 'settings') loadSettings();
  if (button.dataset.tab === 'about') loadStatus();
});

/* ------------------------------------------------------------------ 状态 */
async function loadStatus() {
  const data = await api('api/status');
  state.status = data;

  const bridge = data.bridge || {};
  const pill = $('status-pill');
  const crashLoop = (bridge.processes || []).some((p) => p.crash_loop);
  if (bridge.running) {
    pill.className = 'pill pill-running';
    pill.textContent = bridge.mode === 'both' ? '运行中（UPnP + Cast）'
      : bridge.mode === 'cast' ? '运行中（Chromecast）' : '运行中（DLNA/UPnP）';
  } else if (crashLoop) {
    pill.className = 'pill pill-error';
    pill.textContent = '已停止（反复崩溃）';
  } else {
    pill.className = 'pill pill-idle';
    pill.textContent = '已停止';
  }

  $('version-tag').textContent = 'v' + data.app.version;

  const lines = [];
  (bridge.processes || []).forEach((proc) => {
    if (!proc.running && !proc.restarts) return;
    lines.push(`${proc.binary}: ${proc.running ? '运行中 pid=' + proc.pid + '，已运行 ' + fmtUptime(proc.uptime) : '未运行'}` +
      (proc.restarts ? `，重启 ${proc.restarts} 次` : ''));
  });
  $('footer-status').textContent = lines.length ? lines.join('　|　')
    : '桥接程序当前未运行';

  const about = { version: data.app.version, socket: data.paths.socket, etc: data.paths.etc, var: data.paths.var };
  $('a-version').textContent = data.app.version;
  $('a-airupnp').textContent = (data.binaries || {}).airupnp || '-';
  $('a-aircast').textContent = (data.binaries || {}).aircast || '-';
  $('a-socket').textContent = about.socket;
  $('a-etc').textContent = about.etc;
  $('a-var').textContent = about.var;
  return data;
}

async function action(name, confirmText) {
  if (confirmText && !window.confirm(confirmText)) return;
  try {
    const data = await api('api/action', { method: 'POST', body: { action: name } });
    banner(data.message || '操作已完成', 'ok');
  } catch (err) {
    banner('操作失败：' + err.message, 'err', 8000);
  }
  await loadStatus();
  if (name === 'rescan' || name === 'restart') {
    setTimeout(loadDevices, 12000);
  } else {
    loadDevices();
  }
}

/* ------------------------------------------------------------------ 设备 */
async function loadDevices() {
  try {
    const data = await api('api/devices');
    state.devices = data.devices || [];
    renderDevices(data);
  } catch (err) {
    banner('读取设备列表失败：' + err.message, 'err', 8000);
  }
}

function renderDevices(data) {
  const body = $('devices-body');
  const devices = state.devices;
  if (!devices.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">还没有发现任何播放设备。' +
      '点右上角「重新扫描」，然后等 10–30 秒再刷新。</td></tr>';
    $('devices-empty-hint').textContent = '';
    return;
  }
  body.innerHTML = devices.map((device) => {
    const badge = device.bridge === 'cast'
      ? '<span class="badge badge-cast">Chromecast</span>'
      : '<span class="badge badge-upnp">UPnP</span>';
    const sub = [device.friendly_name ? '原名：' + escapeHtml(device.friendly_name) : '',
                 device.model ? escapeHtml(device.model) : ''].filter(Boolean).join(' · ');
    return `<tr data-udn="${escapeHtml(device.udn)}">
      <td class="col-on">
        <label class="switch"><input type="checkbox" class="dev-on" ${device.enabled ? 'checked' : ''}>
        <span></span></label>
      </td>
      <td><input type="text" class="dev-name" maxlength="60" value="${escapeHtml(device.name)}"></td>
      <td>${badge} ${sub ? '<span class="hint">' + sub + '</span>' : ''}</td>
      <td class="col-mac mono">${escapeHtml(device.mac || '-')}</td>
      <td class="col-op">
        <button class="btn btn-mini dev-save" disabled>保存</button>
        <button class="btn btn-mini btn-danger dev-forget" title="仅从列表移除记录；设备仍在局域网时会被再次发现">移除</button>
      </td>
    </tr>`;
  }).join('');

  const bridgeWord = data.mode === 'cast' ? 'Chromecast' : (data.mode === 'both' ? 'UPnP 与 Chromecast' : 'DLNA / UPnP');
  $('devices-empty-hint').textContent =
    `当前模式：${bridgeWord}　·　AirPlay 名称后缀：「${data.name_suffix}」　·　共 ${devices.length} 个设备`;

  body.querySelectorAll('tr').forEach((row) => {
    const udn = row.dataset.udn;
    const on = row.querySelector('.dev-on');
    const name = row.querySelector('.dev-name');
    const save = row.querySelector('.dev-save');
    const forget = row.querySelector('.dev-forget');

    name.addEventListener('input', () => { save.disabled = false; });
    on.addEventListener('change', () => saveDevice(udn, { enabled: on.checked ? 1 : 0 }, row));
    save.addEventListener('click', () => saveDevice(udn, { name: name.value }, row));
    forget.addEventListener('click', async () => {
      if (!window.confirm('确定从列表里移除这条设备记录吗？\n（设备仍在局域网时，下次扫描会重新出现）')) return;
      try {
        await api('api/devices', { method: 'POST', body: { action: 'forget', udn } });
        banner('已移除记录', 'ok');
        loadDevices();
      } catch (err) { banner('移除失败：' + err.message, 'err', 8000); }
    });
  });
}

async function saveDevice(udn, payload, row) {
  row.querySelectorAll('input,button').forEach((el) => { el.disabled = true; });
  try {
    await api('api/devices', { method: 'POST', body: Object.assign({ udn }, payload) });
    banner('已保存，桥接程序重启中…', 'ok');
    setTimeout(loadDevices, 3000);
  } catch (err) {
    banner('保存失败：' + err.message, 'err', 8000);
    row.querySelectorAll('input,button').forEach((el) => { el.disabled = false; });
  }
}

/* ------------------------------------------------------------------ 设置 */
async function loadInterfaces() {
  try {
    const data = await api('api/interfaces');
    state.interfaces = data.interfaces || [];
  } catch (e) { state.interfaces = []; }
  const select = $('f-binding');
  const options = ['<option value="?">自动（推荐）</option>'];
  state.interfaces.forEach((iface) => {
    options.push(`<option value="${escapeHtml(iface.name)}">${escapeHtml(iface.label)}</option>`);
  });
  select.innerHTML = options.join('');
}

async function loadSettings() {
  try {
    const data = await api('api/settings');
    state.settings = data.settings;
    renderSettings(data.settings);
    state.dirtySettings = false;
  } catch (err) {
    banner('读取设置失败：' + err.message, 'err', 8000);
  }
}

function renderSettings(s) {
  $('f-mode').value = s.mode;
  $('f-name-suffix').value = s.name_suffix;
  $('f-codec').value = s.codec;
  $('f-main-log').value = s.main_log;
  $('f-http-length').value = String(s.http_length);
  $('f-stream-type').value = s.stream_type;
  $('f-max-players').value = s.max_players;
  $('f-port-base').value = s.port_base;
  $('f-port-range').value = s.port_range;
  $('f-upnp-port').value = s.upnp_port;
  $('f-artwork').value = s.artwork;
  $('f-metadata').checked = !!s.metadata;
  $('f-flush').checked = !!s.flush;
  $('f-drift').checked = !!s.drift;
  $('f-enabled-default').checked = !!s.enabled_by_default;

  const preset = $('f-latency-preset');
  if (LATENCY_PRESETS.indexOf(s.latency) >= 0) {
    preset.value = s.latency;
    $('f-latency').value = s.latency;
    $('f-latency').disabled = true;
  } else {
    preset.value = '__custom__';
    $('f-latency').value = s.latency;
    $('f-latency').disabled = false;
  }

  const known = state.interfaces.map((i) => i.name);
  if (s.binding && s.binding !== '?' && known.indexOf(s.binding) < 0) {
    $('f-binding').insertAdjacentHTML('beforeend',
      `<option value="${escapeHtml(s.binding)}">${escapeHtml(s.binding)}（当前）</option>`);
  }
  $('f-binding').value = s.binding || '?';

  const codecOption = Array.from($('f-codec').options).some((o) => o.value === s.codec);
  if (!codecOption) {
    $('f-codec').insertAdjacentHTML('beforeend',
      `<option value="${escapeHtml(s.codec)}">${escapeHtml(s.codec)}（当前）</option>`);
    $('f-codec').value = s.codec;
  }
}

function collectSettings() {
  const preset = $('f-latency-preset').value;
  const latency = preset === '__custom__' ? $('f-latency').value.trim() : preset;
  return {
    mode: $('f-mode').value,
    name_suffix: $('f-name-suffix').value,
    latency: latency || '500:0',
    codec: $('f-codec').value,
    binding: $('f-binding').value || '?',
    main_log: $('f-main-log').value,
    http_length: parseInt($('f-http-length').value, 10),
    stream_type: $('f-stream-type').value,
    max_players: parseInt($('f-max-players').value, 10),
    port_base: parseInt($('f-port-base').value, 10),
    port_range: parseInt($('f-port-range').value, 10),
    upnp_port: parseInt($('f-upnp-port').value, 10),
    artwork: $('f-artwork').value.trim(),
    metadata: $('f-metadata').checked ? 1 : 0,
    flush: $('f-flush').checked ? 1 : 0,
    drift: $('f-drift').checked ? 1 : 0,
    enabled_by_default: $('f-enabled-default').checked ? 1 : 0,
  };
}

/* ------------------------------------------------------------------ 日志 */
async function loadLogs() {
  const source = $('log-source').value;
  const box = $('logbox');
  try {
    const data = await api('api/logs?source=' + encodeURIComponent(source) + '&lines=400');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
    box.innerHTML = (data.text || '（暂无日志）').split('\n').map((line) => {
      const cls = /\b(ERROR|error)\b/.test(line) ? 'ln-err'
        : /\b(WARN|warn)\b/.test(line) ? 'ln-warn' : '';
      return cls ? `<span class="${cls}">${escapeHtml(line)}</span>` : escapeHtml(line);
    }).join('\n');
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (err) {
    box.textContent = '读取日志失败：' + err.message;
  }
}

function scheduleLogs() {
  if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
  if ($('log-auto').checked) {
    state.logTimer = setInterval(() => {
      const active = document.querySelector('.panel.is-active');
      if (active && active.dataset.panel === 'logs') loadLogs();
    }, 3000);
  }
}

/* ------------------------------------------------------------------ 绑定 */
function bind() {
  $('btn-devices-refresh').addEventListener('click', loadDevices);
  $('btn-rescan').addEventListener('click', () => action('rescan',
    '重新扫描会重启桥接程序，正在播放的 AirPlay 会话会中断。继续？'));
  $('btn-settings-reload').addEventListener('click', () => { loadSettings(); banner('已放弃未保存的修改', 'info'); });
  $('btn-settings-save').addEventListener('click', async () => {
    const button = $('btn-settings-save');
    button.disabled = true;
    try {
      await api('api/settings', { method: 'POST', body: collectSettings() });
      banner('设置已保存，桥接程序重启中…', 'ok');
      setTimeout(() => { loadDevices(); loadStatus(); }, 3000);
    } catch (err) {
      banner('保存失败：' + err.message, 'err', 10000);
    } finally {
      button.disabled = false;
    }
  });
  $('f-latency-preset').addEventListener('change', (event) => {
    const custom = event.target.value === '__custom__';
    $('f-latency').disabled = !custom;
    if (!custom) $('f-latency').value = event.target.value;
  });
  $('log-source').addEventListener('change', loadLogs);
  $('log-auto').addEventListener('change', scheduleLogs);
  $('btn-log-refresh').addEventListener('click', loadLogs);
  $('btn-log-download').addEventListener('click', () => {
    window.location.href = 'api/logs/download?source=' + encodeURIComponent($('log-source').value);
  });
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  bind();
  await loadInterfaces();
  await loadStatus().catch((err) => banner('无法连接管理服务：' + err.message, 'err', 0));
  await loadDevices();
  await loadSettings();
  scheduleLogs();
  state.statusTimer = setInterval(() => { loadStatus().catch(() => {}); }, 5000);
}

document.addEventListener('DOMContentLoaded', boot);
