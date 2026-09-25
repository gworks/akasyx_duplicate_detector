// app.js - 画面側。フォームの値を集めてメインプロセスに渡し、出力を描画する
const api = window.detector;

const LOG_LIMIT = 4000;        // これを超えたら古い行から捨てる
const LOG_TRIM = 1000;

// 進捗チップの並び順（detector の result 値そのまま届く）。表示名は辞書の result_<値>
const RESULT_ORDER = [
  'moved', 'duplicate', 'skipped_empty', 'skipped_nohash', 'failed',
  'ok', 'missing', 'unregistered', 'archive_duplicate', 'hash_mismatch',
  'check_ok', 'deleted', 'moved_to_trash',
];

const el = (id) => document.getElementById(id);
const dom = {
  version: el('version'), envWarning: el('env-warning'), tabs: el('tabs'),
  commandText: el('command-text'), copyCommand: el('copy-command'),
  run: el('run'), stop: el('stop'), spinner: el('spinner'), elapsed: el('elapsed'),
  status: el('status'), progress: el('progress'),
  processed: el('progress-processed'), chips: el('progress-chips'),
  log: el('log'), openCsv: el('open-csv'), revealLog: el('reveal-log'),
  clearLog: el('clear-log'), recent: el('recent-archives'),
  dataDir: el('data-dir'), openDataDir: el('open-data-dir'), runCwd: el('run-cwd'),
  langSelect: el('lang-select'),
};

let context = { repoRoot: '', dataDir: '', version: '', detectorFound: true };
let mode = 'add';
let running = false;
let recent = [];
let lastCsvPath = null;
let elapsedTimer = null;
let startedAt = 0;
let logLines = 0;
let lastProgress = null;   // 言語を切り替えたときに進捗チップを描き直すため
let lastStatus = null;     // { level, key, vars } 同上

// ------------------------------------------------------------------ 言語

let lang = 'en';
let dict = {};

/** 辞書を引いて {name} を差し込みます。キーが無ければキー名を返す（main 側で英語の値で穴埋め済み）。 */
function t(key, vars = {}) {
  const template = key in dict ? dict[key] : key;
  return String(template).replace(/\{(\w+)\}/g, (all, name) => (name in vars ? String(vars[name]) : all));
}

function applyLanguage(next) {
  lang = next.lang;
  dict = next.dict;
  document.documentElement.lang = lang;
  for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = t(node.dataset.i18n);
  // *_html は同梱の辞書（自分たちで書いた固定の文言）だけなので innerHTML で入れてよい
  for (const node of document.querySelectorAll('[data-i18n-html]')) node.innerHTML = t(node.dataset.i18nHtml);
  for (const node of document.querySelectorAll('[data-i18n-placeholder]')) {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  }
  for (const node of document.querySelectorAll('[data-i18n-aria-label]')) {
    node.setAttribute('aria-label', t(node.dataset.i18nAriaLabel));
  }
  dom.langSelect.value = lang;
  document.title = context.version ? `${t('app_title')}  v${context.version}` : t('app_title');
  renderContext();
  if (lastProgress) renderProgress(lastProgress);
  if (lastStatus) setStatus(lastStatus.level, lastStatus.key, lastStatus.vars);
}

async function changeLanguage(next) {
  applyLanguage(await api.setLanguage(next));
  refreshPreview(); // 入力エラーの文言も訳し直す
  persist();
}

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
  return { mode, values, groups, recent, lang };
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
  if (text.startsWith('[Summary]')) return 'summary';
  if (/ ERROR /.test(text) || text.startsWith('Error:')) return 'err';
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
  lastStatus = null;
  lastProgress = null;
  dom.progress.hidden = true;
  dom.processed.textContent = '0';
  dom.chips.replaceChildren();
  dom.openCsv.hidden = true;
  lastCsvPath = null;
}

/** 状態の帯。key は辞書のキー（vars で差し込み）。言語を切り替えたら描き直す。
 * vars の値に { key, vars } を渡すと、描くたびにそれも訳す（訳した文字列で持つと切り替えで古い言語が残る）。 */
function setStatus(level, key, vars = {}) {
  lastStatus = { level, key, vars };
  const resolved = Object.fromEntries(
    Object.entries(vars).map(([k, v]) => [k, v && typeof v === 'object' && v.key ? t(v.key, v.vars) : v])
  );
  dom.status.className = `banner banner-${level}`;
  dom.status.textContent = t(key, resolved);
  dom.status.hidden = false;
}

function renderProgress(progress) {
  const { processed, tally } = progress;
  lastProgress = progress;
  dom.progress.hidden = false;
  dom.processed.textContent = processed.toLocaleString(lang);
  const keys = Object.keys(tally).sort((a, b) => {
    const ia = RESULT_ORDER.indexOf(a);
    const ib = RESULT_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  dom.chips.replaceChildren(
    ...keys.map((key) => {
      const chip = document.createElement('span');
      chip.className = `chip ${key}`;
      const labelKey = `result_${key}`;
      chip.textContent = `${labelKey in dict ? t(labelKey) : key} ${tally[key].toLocaleString(lang)}`;
      return chip;
    })
  );
}

// ------------------------------------------------------------------ 実行

/** 実行中表示の切り替え。since を渡すと（開き直し時など）その時刻からの経過で表示する。 */
function setRunning(next, since = Date.now()) {
  running = next;
  dom.run.disabled = next;
  dom.stop.disabled = !next;
  dom.spinner.hidden = !next;
  for (const tab of dom.tabs.querySelectorAll('.tab')) tab.disabled = next;

  clearInterval(elapsedTimer);
  if (next) {
    startedAt = since;
    const renderElapsed = () => {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      dom.elapsed.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    };
    renderElapsed();
    elapsedTimer = setInterval(renderElapsed, 1000);
  } else {
    refreshPreview();
  }
}

/** ウィンドウを閉じて開き直したとき、走り続けている実行の表示と「中断」を取り戻す。 */
function resumeRun(run) {
  clearLog();
  appendLog([
    { stream: 'meta', text: `$ ${run.command}` },
    { stream: 'meta', text: t('reopened_note') },
  ]);
  if (run.processed) renderProgress(run);
  setStatus('info', 'status_running');
  setRunning(true, run.startedAt);
}

async function start() {
  if (running) return;
  clearLog();
  const form = collectForm();
  // 応答を待つ前に実行中にする。spawn 失敗（uv 不在など）では応答より先に run:exit が届くことがあり、
  // 応答側で後から実行中に戻すと、子プロセスが無いのに操作不能になる
  setRunning(true);
  setStatus('info', 'status_starting');
  const result = await api.start(form);
  if (!result.started) {
    setRunning(false);
    // error は main が今の言語に訳した文言。キーとして引いても見つからないのでそのまま出る
    setStatus(result.error ? 'error' : 'info', result.error || 'status_canceled');
    return;
  }
  appendLog([{ stream: 'meta', text: `$ ${result.command}` }]);
  rememberRecent((form.archiveRoot || '').trim());
  persist();
  // 応答より先に終了通知を受けて片付いていたら（onExit が running を落としている）、状態を上書きしない
  if (!running) return;
  setStatus('info', 'status_running');
}

function logDir() {
  const custom = el('logDir').value.trim();
  return custom || `${context.dataDir}/log`;
}

// ------------------------------------------------------------ 初期化・配線

/** 実行環境に依存する表示（作業ディレクトリ・detector が見つからないときの警告）。 */
function renderContext() {
  dom.runCwd.textContent = t('cwd', { dir: context.bundled ? context.detectorDir : 'detector/' });
  if (!context.detectorFound) {
    dom.envWarning.textContent = t(context.bundled ? 'env_bundled_missing' : 'env_dev_missing', {
      dir: context.detectorDir,
    });
    dom.envWarning.hidden = false;
  }
}

function wirePickers() {
  for (const button of document.querySelectorAll('[data-pick]')) {
    button.addEventListener('click', async () => {
      const target = el(button.dataset.pick);
      const picked = await api.pick({
        title: t(button.dataset.pickTitle),
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
      const path = droppedPath(e.dataTransfer);
      if (path) {
        input.value = path;
        refreshPreview();
        persist();
      }
    });
  }
}

/**
 * ドロップされたものからローカルパスを取り出します。
 * 通常のファイル / フォルダは File として来るが、Finder のサイドバーや「場所」から
 * ネットワークボリューム（/Volumes/xxx）を落とすと File が空で URL だけが来ることがある。
 * その場合は file:// URL をデコードしてパスにする。
 */
function droppedPath(dt) {
  const file = dt.files && dt.files[0];
  if (file) {
    const p = api.pathForFile(file);
    if (p) return p;
  }
  const raw = dt.getData('text/uri-list') || dt.getData('text/plain') || '';
  const line = raw.split(/\r?\n/).find((l) => l && !l.startsWith('#')) || '';
  if (line.startsWith('file://')) {
    try {
      let p = decodeURIComponent(new URL(line).pathname);
      if (p.length > 1 && p.endsWith('/')) p = p.slice(0, -1);
      return p;
    } catch {
      return '';
    }
  }
  return line.startsWith('/') ? line.trim() : '';
}

function wireForm() {
  for (const control of document.querySelectorAll('[data-field], [data-group]')) {
    const event = control.tagName === 'INPUT' && control.type !== 'checkbox' ? 'input' : 'change';
    control.addEventListener(event, () => {
      refreshPreview();
      persist();
    });
  }
  dom.langSelect.addEventListener('change', () => changeLanguage(dom.langSelect.value));
  dom.tabs.addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (tab && !tab.disabled) setMode(tab.dataset.mode);
  });
}


function wireRunControls() {
  dom.run.addEventListener('click', start);
  dom.stop.addEventListener('click', () => {
    api.stop();
    dom.stop.disabled = true;
    setStatus('warn', 'status_stop_requested');
  });
  dom.clearLog.addEventListener('click', clearLog);
  dom.copyCommand.addEventListener('click', () => {
    const text = dom.commandText.textContent;
    if (text && !dom.commandText.classList.contains('invalid')) {
      // cd 先はメインプロセスで引数と同じ規則でクォート済み（スペース入りパス対策）
      api.copy(`cd ${context.detectorDirQuoted} && ${text}`);
      dom.copyCommand.textContent = t('copied');
      setTimeout(() => { dom.copyCommand.textContent = t('copy'); }, 1400);
    }
  });
  dom.openCsv.addEventListener('click', () => lastCsvPath && api.reveal(lastCsvPath));
  dom.revealLog.addEventListener('click', () => api.open(logDir()));
  dom.openDataDir.addEventListener('click', () => api.openDataDir());
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
    // 開き直し直後、getContext() の後〜復元前に終わった場合に古い状態で復元しないようにする
    context.run = null;
    setRunning(false);
    if (result.csvPath) {
      lastCsvPath = result.csvPath;
      dom.openCsv.hidden = false;
    }
    if (result.stoppedByUser) {
      setStatus('warn', 'status_stopped');
      return;
    }
    // 終了の意味は main からキーで届く（言語を切り替えても訳し直せるように）
    const { level, key, vars } = result.meaning;
    setStatus(level, 'status_exit', { text: { key, vars }, code: result.code });
  });
}

async function init() {
  // 最初の await より前に購読する。走り続けている実行の終了通知を取りこぼさないため
  wireRunEvents();
  context = await api.getContext();
  dom.version.textContent = `v${context.version}`;
  dom.revealLog.hidden = false;
  dom.dataDir.textContent = context.dataDir;
  applyLanguage(await api.getLanguage());

  restore(await api.loadSettings());
  wirePickers();
  wireDropTargets();
  wireForm();
  wireRunControls();
  setMode(mode);
  if (context.run) resumeRun(context.run);
}

init();
