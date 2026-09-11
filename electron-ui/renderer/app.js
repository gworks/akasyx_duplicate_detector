// app.js - 画面側。フォームの値を集めてメインプロセスに渡し、出力を描画する
const api = window.detector;

const LOG_LIMIT = 4000;        // これを超えたら古い行から捨てる
const LOG_TRIM = 1000;

// 進捗チップの表示名と並び順（detector の result 値そのまま届く）
const RESULT_LABELS = {
  moved: '移動', duplicate: '重複(据え置き)', skipped_empty: '空ファイル',
  skipped_nohash: 'ハッシュ不明', failed: '失敗',
  ok: '一致', missing: '実体なし', unregistered: '未登録',
  archive_duplicate: '保存内重複', hash_mismatch: 'ハッシュ相違',
  check_ok: '検証OK', deleted: '削除', moved_to_trash: '退避',
};
const RESULT_ORDER = Object.keys(RESULT_LABELS);

const el = (id) => document.getElementById(id);
const dom = {
  version: el('version'), envWarning: el('env-warning'), tabs: el('tabs'),
  commandText: el('command-text'), copyCommand: el('copy-command'),
  run: el('run'), stop: el('stop'), spinner: el('spinner'), elapsed: el('elapsed'),
  status: el('status'), progress: el('progress'),
  processed: el('progress-processed'), chips: el('progress-chips'),
  log: el('log'), openCsv: el('open-csv'), revealLog: el('reveal-log'),
  clearLog: el('clear-log'), recent: el('recent-archives'),
  destSubdir: el('destSubdir'), useDestSubdir: el('useDestSubdir'),
};

let context = { repoRoot: '', version: '', detectorFound: true };
let mode = 'add';
let running = false;
let recent = [];
let lastCsvPath = null;
let elapsedTimer = null;
let startedAt = 0;
let logLines = 0;

// ---------------------------------------------------------------- フォーム

/** 今のタブで有効なコントロール（他タブのパネルは無視する）。 */
function activeControls() {
  return [...document.querySelectorAll('[data-field], [data-group]')].filter((c) => {
    const panel = c.closest('.mode-panel');
    return !panel || panel.dataset.panel === mode;
  });
}

function collectForm() {
  const form = { mode };
  for (const c of activeControls()) {
    if (c.dataset.group) {
      if (c.checked) (form[c.dataset.group] ||= []).push(c.value);
    } else if (c.type === 'checkbox') {
      form[c.dataset.field] = c.checked;
    } else {
      form[c.dataset.field] = c.value;
    }
  }
  return form;
}

function setMode(next) {
  mode = next;
  for (const tab of dom.tabs.querySelectorAll('.tab')) {
    tab.setAttribute('aria-selected', String(tab.dataset.mode === next));
  }
  for (const panel of document.querySelectorAll('.mode-panel')) {
    panel.classList.toggle('active', panel.dataset.panel === next);
  }
  refreshPreview();
  persist();
}

// ------------------------------------------------------------ 設定の保存

function snapshot() {
  const values = {};
  for (const c of document.querySelectorAll('[data-field]')) {
    if (!c.id) continue;
    values[c.id] = c.type === 'checkbox' ? c.checked : c.value;
  }
  const groups = {};
  for (const c of document.querySelectorAll('[data-group]')) {
    groups[`${c.dataset.group}:${c.value}`] = c.checked;
  }
  return { mode, values, groups, recent };
}

let persistTimer = null;
function persist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => api.saveSettings(snapshot()), 400);
}

function restore(saved) {
  if (!saved || typeof saved !== 'object') return;
  recent = Array.isArray(saved.recent) ? saved.recent : [];
  renderRecent();

  for (const [id, value] of Object.entries(saved.values || {})) {
    // 実削除フラグは復元しない（前回の指定を引きずって消してしまわないため）
    if (id === 'yes') continue;
    const control = el(id);
    if (!control) continue;
    if (control.type === 'checkbox') control.checked = Boolean(value);
    else control.value = value ?? '';
  }
  for (const [key, checked] of Object.entries(saved.groups || {})) {
    const [group, value] = key.split(/:(.+)/);
    const control = document.querySelector(`[data-group="${group}"][value="${value}"]`);
    if (control) control.checked = Boolean(checked);
  }
  if (typeof saved.mode === 'string') mode = saved.mode;
}

function renderRecent() {
  dom.recent.replaceChildren(
    ...recent.map((path) => {
      const option = document.createElement('option');
      option.value = path;
      return option;
    })
  );
}

function rememberRecent(path) {
  if (!path) return;
  recent = [path, ...recent.filter((p) => p !== path)].slice(0, 8);
  renderRecent();
}

// ------------------------------------------------------------ プレビュー

let previewTimer = null;
function refreshPreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const { command, error } = await api.preview(collectForm());
    dom.commandText.textContent = command || (error ? `— ${error}` : '—');
    dom.commandText.classList.toggle('invalid', !command);
    dom.run.disabled = running || !command;
  }, 120);
}

// ------------------------------------------------------------------ ログ

/** 行の見た目を決めます。detector は INFO ログも stderr に出すので、色は内容で判断する。 */
function logClass(entry) {
  if (entry.stream === 'meta') return 'meta';
  const text = entry.text;
  if (text.startsWith('[実行サマリ]')) return 'summary';
  if (/ ERROR /.test(text) || text.startsWith('エラー:')) return 'err';
  if (/ WARNING /.test(text)) return 'warn';
  // stderr の通常ログ（INFO）は本来の出力より控えめに見せる
  return entry.stream === 'err' ? 'meta' : '';
}

function appendLog(entries) {
  const atBottom = dom.log.scrollHeight - dom.log.scrollTop - dom.log.clientHeight < 40;
  const fragment = document.createDocumentFragment();
  for (const entry of entries) {
    const line = document.createElement('span');
    line.className = logClass(entry);
    line.textContent = `${entry.text}\n`;
    fragment.append(line);
    logLines += 1;
  }
  dom.log.append(fragment);
  if (logLines > LOG_LIMIT) {
    for (let i = 0; i < LOG_TRIM && dom.log.firstChild; i += 1) dom.log.firstChild.remove();
    logLines -= LOG_TRIM;
  }
  if (atBottom) dom.log.scrollTop = dom.log.scrollHeight;
}

function clearLog() {
  dom.log.replaceChildren();
  logLines = 0;
  dom.status.hidden = true;
  dom.progress.hidden = true;
  dom.processed.textContent = '0';
  dom.chips.replaceChildren();
  dom.openCsv.hidden = true;
  lastCsvPath = null;
}

function setStatus(level, text) {
  dom.status.className = `banner banner-${level}`;
  dom.status.textContent = text;
  dom.status.hidden = false;
}

function renderProgress({ processed, tally }) {
  dom.progress.hidden = false;
  dom.processed.textContent = processed.toLocaleString('ja-JP');
  const keys = Object.keys(tally).sort((a, b) => {
    const ia = RESULT_ORDER.indexOf(a);
    const ib = RESULT_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  dom.chips.replaceChildren(
    ...keys.map((key) => {
      const chip = document.createElement('span');
      chip.className = `chip ${key}`;
      chip.textContent = `${RESULT_LABELS[key] || key} ${tally[key].toLocaleString('ja-JP')}`;
      return chip;
    })
  );
}

// ------------------------------------------------------------------ 実行

function setRunning(next) {
  running = next;
  dom.run.disabled = next;
  dom.stop.disabled = !next;
  dom.spinner.hidden = !next;
  for (const tab of dom.tabs.querySelectorAll('.tab')) tab.disabled = next;

  clearInterval(elapsedTimer);
  if (next) {
    startedAt = Date.now();
    dom.elapsed.textContent = '0:00';
    elapsedTimer = setInterval(() => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      dom.elapsed.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    }, 1000);
  } else {
    refreshPreview();
  }
}

async function start() {
  if (running) return;
  clearLog();
  const form = collectForm();
  const result = await api.start(form);
  if (!result.started) {
    if (result.error) setStatus('error', result.error);
    return;
  }
  appendLog([{ stream: 'meta', text: `$ ${result.command}` }]);
  setStatus('info', '実行中…');
  setRunning(true);
  rememberRecent((form.archiveRoot || '').trim());
  persist();
}

function logDir() {
  const custom = el('logDir').value.trim();
  return custom || `${context.repoRoot}/dist/log`;
}

// ------------------------------------------------------------ 初期化・配線

function wirePickers() {
  for (const button of document.querySelectorAll('[data-pick]')) {
    button.addEventListener('click', async () => {
      const target = el(button.dataset.pick);
      const picked = await api.pick({
        title: button.dataset.pickTitle,
        files: button.dataset.pickFiles === '1',
        defaultPath: target.value.trim() || undefined,
      });
      if (picked) {
        target.value = picked;
        refreshPreview();
        persist();
      }
    });
  }
}

function wireDropTargets() {
  // ウィンドウ全体でのドロップはナビゲーションになってしまうので必ず止める
  for (const type of ['dragover', 'drop']) {
    document.addEventListener(type, (e) => e.preventDefault());
  }
  for (const input of document.querySelectorAll('.drop')) {
    const highlight = (on) => input.classList.toggle('dragover', on);
    input.addEventListener('dragover', (e) => { e.preventDefault(); highlight(true); });
    input.addEventListener('dragenter', (e) => { e.preventDefault(); highlight(true); });
    input.addEventListener('dragleave', () => highlight(false));
    input.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      highlight(false);
      const file = e.dataTransfer.files[0];
      if (!file) return;
      const path = api.pathForFile(file);
      if (path) {
        input.value = path;
        refreshPreview();
        persist();
      }
    });
  }
}

function wireForm() {
  for (const control of document.querySelectorAll('[data-field], [data-group]')) {
    const event = control.tagName === 'INPUT' && control.type !== 'checkbox' ? 'input' : 'change';
    control.addEventListener(event, () => {
      refreshPreview();
      persist();
    });
  }
  dom.useDestSubdir.addEventListener('change', syncDestSubdir);
  dom.tabs.addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (tab && !tab.disabled) setMode(tab.dataset.mode);
  });
}

function syncDestSubdir() {
  dom.destSubdir.disabled = !dom.useDestSubdir.checked;
}

function wireRunControls() {
  dom.run.addEventListener('click', start);
  dom.stop.addEventListener('click', () => {
    api.stop();
    dom.stop.disabled = true;
    setStatus('warn', '中断を要求しました（後片付けの完了を待っています）…');
  });
  dom.clearLog.addEventListener('click', clearLog);
  dom.copyCommand.addEventListener('click', () => {
    const text = dom.commandText.textContent;
    if (text && !dom.commandText.classList.contains('invalid')) {
      api.copy(`cd ${context.detectorDir} && ${text}`);
      dom.copyCommand.textContent = 'コピーしました';
      setTimeout(() => { dom.copyCommand.textContent = 'コピー'; }, 1400);
    }
  });
  dom.openCsv.addEventListener('click', () => lastCsvPath && api.reveal(lastCsvPath));
  dom.revealLog.addEventListener('click', () => api.open(logDir()));
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      if (!dom.run.disabled) start();
    }
  });
}

function wireRunEvents() {
  api.onLog(appendLog);
  api.onProgress(renderProgress);
  api.onExit((result) => {
    setRunning(false);
    if (result.csvPath) {
      lastCsvPath = result.csvPath;
      dom.openCsv.hidden = false;
    }
    if (result.stoppedByUser) {
      setStatus('warn', '中断しました（処理済みぶんまでは記録されています）');
      return;
    }
    setStatus(result.meaning.level, `${result.meaning.text}（終了コード ${result.code}）`);
  });
}

async function init() {
  context = await api.getContext();
  dom.version.textContent = `v${context.version}`;
  if (!context.detectorFound) {
    dom.envWarning.textContent =
      `detector/main.py が見つかりません（${context.detectorDir}）。`
      + 'electron-ui はリポジトリルート直下に置いてください。';
    dom.envWarning.hidden = false;
  }

  dom.revealLog.hidden = false;

  restore(await api.loadSettings());
  wirePickers();
  wireDropTargets();
  wireForm();
  wireRunControls();
  wireRunEvents();
  syncDestSubdir();
  setMode(mode);
}

init();
