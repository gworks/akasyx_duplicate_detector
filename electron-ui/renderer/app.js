// app.js - 画面側。フォームの値を集めてメインプロセスに渡し、出力を描画する
const api = window.detector;

const LOG_LIMIT = 4000;        // これを超えたら古い行から捨てる
const LOG_TRIM = 1000;

// 進捗チップの並び順（detector の result 値そのまま届く）。表示名は辞書の result_<値>
const RESULT_ORDER = [
  'moved', 'duplicate', 'adopted', 'skipped_empty', 'skipped_nohash', 'skipped_own_data', 'skipped_in_archive', 'failed',
  'ok', 'missing', 'unregistered', 'archive_duplicate', 'hash_mismatch',
  'check_ok', 'deleted', 'moved_to_trash',
  'verified', 'gone', 'source_changed', 'archive_missing', 'archive_unavailable', 'source_in_archive', 'same_file',
];

const el = (id) => document.getElementById(id);
const dom = {
  version: el('version'), envWarning: el('env-warning'), tabs: el('tabs'),
  commandText: el('command-text'), copyCommand: el('copy-command'),
  run: el('run'), stop: el('stop'), spinner: el('spinner'), elapsed: el('elapsed'),
  status: el('status'), progress: el('progress'),
  stages: el('progress-stages'), count: el('progress-count'), eta: el('progress-eta'),
  bar: el('progress-bar'), barFill: el('progress-bar-fill'), current: el('progress-current'),
  chips: el('progress-chips'), failures: el('progress-failures'),
  failuresSummary: el('progress-failures-summary'), failureList: el('progress-failure-list'),
  failuresOmitted: el('progress-failures-omitted'),
  log: el('log'), logToggle: el('log-toggle'), openCsv: el('open-csv'), revealLog: el('reveal-log'),
  clearLog: el('clear-log'), recent: el('recent-archives'),
  dataDir: el('data-dir'), openDataDir: el('open-data-dir'), runCwd: el('run-cwd'),
  langSelect: el('lang-select'),
  archiveRootField: el('archive-root-field'), archiveList: el('archive-list'),
  archivesMasterDb: el('archives-master-db'), archivesMessage: el('archives-message'),
  archivesRefresh: el('archives-refresh'),
  nextForget: el('next-forget'), nextForgetGo: el('next-forget-go'),
  nextAdopt: el('next-adopt'), nextAdoptText: el('next-adopt-text'), nextAdoptGo: el('next-adopt-go'),
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
let renderedFailures = null; // 失敗の一覧に描いた一覧（同じものが来たら描き直さない）
// 失敗の一覧。main は変わったときだけ一覧を入れて送るので、最後に届いたものを持っておく
let failureState = { failures: [], omitted: 0 };
let failureListReceived = false; // この実行で一覧が 1 度でも届いたか（開き直したときに写しの一覧を使うか）
let archivesState = null;  // 保存フォルダの一覧: { masterDb, archives, error } / 読み込み中は { loading: true }
let nextAdoptPath = null;  // 出力の案内が示す、adopt が要るフォルダ
let commandValid = false;  // 今のフォームから実行できるコマンドが組めるか

/** 実行ボタン。一覧の問い合わせ中も main は実行を断るので、押せないようにしておく。 */
function updateRunButton() {
  dom.run.disabled = running || !commandValid || listInFlight;
}

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
  renderArchives();
  renderNextAdopt();
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
  dom.archiveRootField.hidden = next === 'archives';
  refreshPreview();
  persist();
  if (next === 'archives' && !running) loadArchives();
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
    commandValid = !!command;
    updateRunButton();
  }, 120);
}

// ------------------------------------------------------------------ ログ

/** 行の見た目を決めます。detector は INFO ログも stderr に出すので、色は内容で判断する。 */
function logClass(entry) {
  if (entry.stream === 'meta') return 'meta';
  const text = entry.text;
  // crawler（取り込みの一覧作成の段階に出た行）のサマリは、取り込みの完了ではないので目立たせない（#18）
  if (text.startsWith('[Summary]')) return entry.crawler ? 'meta' : 'summary';
  if (text.startsWith('Note:')) return 'warn';
  if (/ ERROR /.test(text) || text.startsWith('Error:')) return 'err';
  if (/ WARNING /.test(text)) return 'warn';
  // stderr の通常ログ（INFO）は本来の出力より控えめに見せる
  return entry.stream === 'err' ? 'meta' : '';
}

/** ログの行を作ります（足す側・先頭に差し込む側で同じ作り方にする）。 */
function logFragment(entries) {
  const fragment = document.createDocumentFragment();
  for (const entry of entries) {
    const line = document.createElement('span');
    line.className = logClass(entry);
    line.textContent = `${entry.text}\n`;
    fragment.append(line);
    logLines += 1;
  }
  return fragment;
}

function appendLog(entries) {
  const atBottom = dom.log.scrollHeight - dom.log.scrollTop - dom.log.clientHeight < 40;
  dom.log.append(logFragment(entries));
  if (logLines > LOG_LIMIT) {
    for (let i = 0; i < LOG_TRIM && dom.log.firstChild; i += 1) dom.log.firstChild.remove();
    logLines -= LOG_TRIM;
  }
  if (atBottom) dom.log.scrollTop = dom.log.scrollHeight;
}

function clearLogLines() {
  dom.log.replaceChildren();
  logLines = 0;
}

function clearLog() {
  clearLogLines();
  dom.status.hidden = true;
  lastStatus = null;
  lastProgress = null;
  dom.progress.hidden = true;
  dom.chips.replaceChildren();
  dom.failureList.replaceChildren();
  dom.failures.hidden = true;
  dom.failures.open = false;
  renderedFailures = null;
  failureState = { failures: [], omitted: 0 };
  failureListReceived = false;
  dom.openCsv.hidden = true;
  lastCsvPath = null;
  dom.nextForget.hidden = true;
  nextAdoptPath = null;
  renderNextAdopt();
}

/** 出力が示す次の操作（#11）。Note: 行 → 保存フォルダの一覧、adopt の案内 → adopt タブ。 */
function showNextSteps(result) {
  dom.nextForget.hidden = !(result.forgetIds && result.forgetIds.length);
  nextAdoptPath = result.adoptPath || null;
  renderNextAdopt();
}

function renderNextAdopt() {
  dom.nextAdopt.hidden = !nextAdoptPath;
  if (nextAdoptPath) dom.nextAdoptText.textContent = t('next_adopt', { path: nextAdoptPath });
}

// ------------------------------------------------------- 保存フォルダの一覧（#11）

// 操作を止めるのは、一覧の問い合わせが実際に飛んでいる間だけ（listInFlight は finally で必ず下ろす）。
// archivesState.loading は表示だけに使う（busy で待っている間も「読み込み中」と出すが、何も止めない）
let listInFlight = false;
let reloadAfterList = false; // 飛んでいる間に正本 DB の指定が変わった（その結果は捨てて読み直す）
let retryTimer = null;       // busy のときの読み直し。ほかの処理からは消さない

/** archives --json で一覧を読み直します。実行中は読まない（終わったら読み直す）。 */
async function loadArchives() {
  if (running) return;
  if (listInFlight) {
    reloadAfterList = true;
    return;
  }
  clearTimeout(retryTimer);
  listInFlight = true;
  archivesState = { loading: true };
  renderArchives();
  let result;
  try {
    result = await api.listArchives(collectForm());
  } catch (e) {
    result = { archives: null, error: t('archives_error', { detail: e.message || String(e) }) };
  } finally {
    listInFlight = false;
  }
  if (reloadAfterList) {
    reloadAfterList = false;
    archivesState = null;
    if (mode === 'archives') loadArchives();
    else renderArchives();
    return;
  }
  if (result.busy) {
    // 前の読み込み（閉じる前のウィンドウのもの等）や実行が終わるのを、読み込み中の表示のまま待って読み直す
    if (mode === 'archives' && !running) {
      retryTimer = setTimeout(loadArchives, 1000);
    } else {
      archivesState = null; // タブを開いたとき・実行が終わったときに読む
      renderArchives();
    }
    return;
  }
  archivesState = result;
  renderArchives();
}

/** 正本 DB の指定が変わった。前の DB の一覧は捨てて読み直す。 */
function invalidateArchives() {
  if (listInFlight) {
    reloadAfterList = true;
    return;
  }
  clearTimeout(retryTimer);
  archivesState = null;
  renderArchives();
  if (mode === 'archives') loadArchives();
}

function formatBytes(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Number(bytes) || 0;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toLocaleString(lang, { maximumFractionDigits: i ? 1 : 0 })} ${units[i]}`;
}

function chip(text, cls = '') {
  const node = document.createElement('span');
  node.className = `chip ${cls}`.trim();
  node.textContent = text;
  return node;
}

function renderArchives() {
  const state = archivesState;
  dom.archivesRefresh.disabled = running || listInFlight;
  updateRunButton();
  const message = (level, text) => {
    dom.archivesMessage.className = `banner banner-${level}`;
    dom.archivesMessage.textContent = text;
    dom.archivesMessage.hidden = false;
  };
  dom.archivesMessage.hidden = true;
  dom.archiveList.replaceChildren();
  dom.archivesMasterDb.textContent = state && state.masterDb ? t('archives_master_db', { path: state.masterDb }) : '';
  if (!state) return;
  if (state.loading) return message('info', t('archives_loading'));
  // 読めなかったときは空の一覧を出さない（「登録が 0 件」と取り違えないため）
  if (state.error) return message('error', state.error);
  if (!state.dbExists) return message('info', t('archives_no_db'));
  if (!state.archives.length) return message('info', t('archives_empty'));

  dom.archiveList.replaceChildren(...state.archives.map((a) => renderArchiveItem(a, state)));
}

function renderArchiveItem(a, state) {
  const item = document.createElement('li');
  item.className = 'archive-item';
  const body = document.createElement('div');
  body.className = 'archive-body';
  const root = document.createElement('div');
  root.className = 'archive-root';
  const id = document.createElement('span');
  id.className = 'archive-id';
  id.textContent = `#${a.id}`;
  root.append(id, document.createTextNode(a.root));

  const meta = document.createElement('div');
  meta.className = 'archive-meta';
  meta.append(a.present ? chip(t('archive_present')) : chip(t('archive_not_found'), 'absent'));
  meta.append(chip(t('archive_stored', {
    files: Number(a.stored_files).toLocaleString(lang), size: formatBytes(a.stored_bytes),
  })));
  if (a.forgotten_files) {
    meta.append(chip(t('archive_forgotten', { files: Number(a.forgotten_files).toLocaleString(lang) })));
  }
  if (a.pending_adoption) meta.append(chip(t('archive_pending_adoption'), 'pending'));
  if (a.last_used_at) {
    const used = new Date(a.last_used_at);
    if (!Number.isNaN(used.getTime())) {
      meta.append(chip(t('archive_last_used', { date: used.toLocaleString(lang, { dateStyle: 'medium', timeStyle: 'short' }) })));
    }
  }
  body.append(root, meta);
  item.append(body);

  // 外すものがあるときだけ出す。その場所にあるかの最終判断は detector（断りはログと状態の帯に出る）
  if (a.stored_files > 0) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn stop';
    button.textContent = t('archive_forget');
    button.disabled = running;
    // 一覧を読んだ正本 DB と、見た保存フォルダの uid で外す（その後で詳細設定の正本 DB が変わっても別のものを外さない）
    button.addEventListener('click', () => start({
      forgetId: a.id, forgetUid: a.uid, forgetRoot: a.root, forgetArchiveDb: state.masterDb,
    }));
    item.append(button);
  }
  return item;
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

/**
 * 詳細ログを開く・たたむ（#18）。実行するコマンドの種類だけで決める: 取り込みは進み具合のカードで足りるのでたたみ、
 * ほかのコマンドは出力そのものが結果なので開く（logOpenFor）。実行が始まらなかったとき・エラーで終わったときは開く。
 */
const logOpenFor = (mode) => mode !== 'add';

function setLogOpen(open) {
  dom.log.hidden = !open;
  dom.logToggle.setAttribute('aria-expanded', String(open));
  if (open) dom.log.scrollTop = dom.log.scrollHeight;
}

/** 秒数を「約 2 時間 5 分」のように丸めて出します。 */
function formatDuration(seconds) {
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return t('duration_under_minute');
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (!h) return t('duration_m', { m });
  return m ? t('duration_hm', { h, m }) : t('duration_h', { h });
}

const STAGE_ORDER = ['scan', 'ingest', 'cleanup', 'done']; // done は表示しない（後片付けが済んだ印）

/** 処理中のファイルに出す名前（フルパスは title で見られる）。`\` で区切るのは Windows だけ（macOS・Linux ではファイル名の文字）。 */
const fileLabel = (fullPath) => fullPath.split(context.platform === 'win32' ? /[\\/]/ : /\//).pop() || fullPath;

function renderProgress(progress) {
  const { processed, tally, stage, scan, current, etaSeconds, ingestEnded } = progress;
  const total = progress.total ?? null; // main は数か null で送る（古い写しに無くても null として扱う）
  lastProgress = progress;
  // 一覧が入っていればそれが今の一覧（main は変わったときにまるごと送る）。無ければ前のまま
  if ('failures' in progress) failureListReceived = true;
  failureState = {
    failures: 'failures' in progress ? progress.failures : failureState.failures,
    omitted: progress.failuresOmitted || 0,
  };
  dom.progress.hidden = false;

  // 段階（取り込みだけが出す。出さないコマンドでは並びを隠す）。完了かどうかは detector が出したことだけで決める:
  // 次の段階（最後は done）が出ていれば完了（detector は前の段階が最後まで済んだときだけ次を出す。中断・異常終了では
  // 止まった段階のまま）。通らなかった段階（単一ファイルの一覧作成）は出さない。実行中の印は実行中だけ
  const seen = progress.stagesSeen || (stage ? [stage] : []);
  dom.stages.hidden = !stage;
  const at = STAGE_ORDER.indexOf(stage);
  for (const item of dom.stages.children) {
    const name = item.dataset.stage;
    const i = STAGE_ORDER.indexOf(name);
    item.hidden = i < at && !seen.includes(name);
    item.classList.toggle('done', seen.includes(name) && i < at);
    // 判定のループを抜けた後（中断の後片付け）は、判定と移動を実行中に見せない
    item.classList.toggle('active', running && i === at && !(name === 'ingest' && ingestEnded));
  }

  // 件数と割合。一覧作成の段階は読み取った件数を出す
  const num = (n) => Number(n).toLocaleString(lang);
  let ratio = null;
  if (stage === 'scan' && scan) {
    // crawler の全体の件数は見積もり。走査中にファイルが増えると超えるので 100% で止める
    ratio = scan.total ? Math.min(scan.done / scan.total, 1) : null;
    dom.count.textContent = scan.total
      ? t('progress_scan', { done: num(scan.done), total: num(scan.total), pct: Math.floor(ratio * 100) })
      : t('progress_scan_unknown', { done: num(scan.done) });
  } else if (stage === 'scan' || (stage === 'ingest' && total === null && !processed && !current)) {
    // 始める前に止まった実行には「始めています」と出さない（理由は状態の帯と詳細ログ）。判定の段階で止まったときは
    // 処理した件数（0 件）と、全体が分からないことを出す（空欄にしない）
    if (running) dom.count.textContent = t(stage === 'scan' ? 'progress_scan_starting' : 'progress_starting');
    else dom.count.textContent = stage === 'ingest' ? t('progress_unknown_total', { done: num(processed) }) : '';
  } else if (total !== null) {
    ratio = total ? Math.min(processed / total, 1) : 1;
    dom.count.textContent = t('progress_of', { done: num(processed), total: num(total), pct: Math.floor(ratio * 100) });
  } else {
    // 全体の件数が分からない（数えられなかった）。0 件や 100% とは出さない
    dom.count.textContent = t('progress_unknown_total', { done: num(processed) });
  }
  dom.bar.hidden = ratio === null;
  dom.barFill.style.width = `${ratio === null ? 0 : (ratio * 100).toFixed(1)}%`;

  // 残り時間と処理中のファイルは、実行中だけ出す（異常終了の後に数え続けたり「処理中」と出したりしない）
  // 残り時間は、判定と移動の段階で全体の件数が分かっているときだけ
  if (running && stage === 'ingest' && !ingestEnded && total !== null && processed < total) {
    dom.eta.textContent = etaSeconds === null || etaSeconds === undefined
      ? t('eta_calculating')
      : t('eta_remaining', { time: formatDuration(etaSeconds) });
  } else {
    dom.eta.textContent = '';
  }

  const showCurrent = running && current;
  dom.current.hidden = !showCurrent;
  dom.current.textContent = showCurrent ? t('progress_current', { name: fileLabel(current) }) : '';
  dom.current.title = showCurrent ? current : '';

  renderFailures(progress.failureCount || 0);

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

/** 失敗したファイルの一覧（#18）。CSV を開かなくても、どのファイルが何で失敗したかが分かるように。 */
function renderFailures(count) {
  const { failures, omitted } = failureState;
  dom.failures.hidden = count === 0;
  if (!count) return;
  dom.failuresSummary.textContent = t('failures_summary', { count: count.toLocaleString(lang) });
  // 新しい一覧が届いたときだけ描き直す（届くのは一覧が変わったときだけ・上限 500 件。言語の切り替えでは中身は変わらない）
  if (failures === renderedFailures) return finishFailures(omitted);
  dom.failureList.replaceChildren();
  for (const f of failures) {
    const item = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'failure-path';
    name.textContent = f.path;
    const message = document.createElement('span');
    message.className = 'failure-message';
    message.textContent = f.message;
    item.append(name, message);
    dom.failureList.append(item);
  }
  renderedFailures = failures;
  return finishFailures(omitted);
}

function finishFailures(omitted) {
  dom.failuresOmitted.hidden = !omitted;
  if (omitted) dom.failuresOmitted.textContent = t('failures_omitted', { count: omitted.toLocaleString(lang) });
}

// ------------------------------------------------------------------ 実行

/** 実行中表示の切り替え。since を渡すと（開き直し時など）その時刻からの経過で表示する。 */
function setRunning(next, since = Date.now()) {
  running = next;
  updateRunButton();
  dom.stop.disabled = !next;
  dom.spinner.hidden = !next;
  for (const tab of dom.tabs.querySelectorAll('.tab')) tab.disabled = next;
  for (const button of dom.archiveList.querySelectorAll('button')) button.disabled = next;
  dom.archivesRefresh.disabled = next;

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
    // 登録を外した後など、一覧を見ているなら読み直す
    if (mode === 'archives') loadArchives();
  }
}

/** ウィンドウを閉じて開き直したとき、走り続けている実行の表示と「中断」を取り戻す。 */
function resumeRun(run) {
  // 開き直した画面は空なので消すものは無い。getContext() の後に届いたログ・進み具合（写しより新しい）を
  // 消したり古い写しで上書きしたりしないよう、案内は先頭に差し込み、進み具合はまだ届いていないときだけ写しで描く
  dom.log.prepend(logFragment([
    { stream: 'meta', text: `$ ${run.command}` },
    { stream: 'meta', text: t('reopened_note') },
  ]));
  setStatus('info', 'status_running');
  // 実行中にしてから描く（残り時間と処理中のファイルは実行中だけ出す）
  setRunning(true, run.startedAt);
  if (!lastProgress && (run.stage || run.processed)) {
    renderProgress(run);
  } else if (lastProgress) {
    if (!failureListReceived) {
      // 先に届いた進み具合に一覧が入っていなかった（開き直す前に送られたもの）。写しの一覧で埋める
      failureState = { failures: run.failures || [], omitted: run.failuresOmitted || 0 };
      failureListReceived = true;
    }
    // 先に届いた進み具合は実行中にする前に描かれている（実行中の印・残り時間・処理中のファイルが無い）ので描き直す
    renderProgress(lastProgress);
  }
  setLogOpen(logOpenFor(run.mode));
}

/** 実行します。extra はフォームに無い指定（一覧の行から登録を外すときの forgetId など）。 */
async function start(extra = {}) {
  if (running) return;
  clearLog();
  const form = { ...collectForm(), ...extra };
  setLogOpen(logOpenFor(form.mode));
  // 応答を待つ前に実行中にする。spawn 失敗（uv 不在など）では応答より先に run:exit が届くことがあり、
  // 応答側で後から実行中に戻すと、子プロセスが無いのに操作不能になる
  setRunning(true);
  setStatus('info', 'status_starting');
  const result = await api.start(form);
  if (!result.started) {
    setRunning(false);
    setLogOpen(true); // 始まらなかった（確認で取り消した・断られた）。たたんだままにしない
    // error は main が今の言語に訳した文言。キーとして引いても見つからないのでそのまま出る
    setStatus(result.error ? 'error' : 'info', result.error || 'status_canceled');
    return;
  }
  appendLog([{ stream: 'meta', text: `$ ${result.command}` }]);
  if (form.mode !== 'archives') rememberRecent((form.archiveRoot || '').trim());
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

/**
 * 選んだ・落とした・案内から拾った実パスをパス欄に入れます。パス欄の正規化（\ や引用符）で書き換わらない形にし、
 * 手で入力したときと同じく change を起こす（正本 DB 欄なら一覧を読み直す）。
 */
async function setPathField(input, realPath) {
  input.value = await api.toFieldValue(realPath);
  input.dispatchEvent(new Event('change'));
  refreshPreview();
  persist();
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
      if (picked) await setPathField(target, picked);
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
      if (path) setPathField(input, path);
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
  // 正本 DB の指定が変わったら、前の DB の一覧は捨てて読み直す。入力途中（1 文字ごと）ではなく確定したときだけ
  el('archiveDb').addEventListener('change', invalidateArchives);
  dom.langSelect.addEventListener('change', () => changeLanguage(dom.langSelect.value));
  dom.tabs.addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (tab && !tab.disabled) setMode(tab.dataset.mode);
  });
}


function wireRunControls() {
  dom.run.addEventListener('click', () => start());
  dom.archivesRefresh.addEventListener('click', loadArchives);
  // 導線をたどったら帯は消す（行き先のタブで続きを行う）
  dom.nextForgetGo.addEventListener('click', () => {
    dom.nextForget.hidden = true;
    setMode('archives');
  });
  dom.nextAdoptGo.addEventListener('click', async () => {
    // 案内の実パスはパス欄の正規化で書き換わらない形にして入れる
    await setPathField(el('archiveRoot'), nextAdoptPath);
    nextAdoptPath = null;
    renderNextAdopt();
    setMode('adopt');
  });
  dom.stop.addEventListener('click', () => {
    api.stop();
    dom.stop.disabled = true;
    setStatus('warn', 'status_stop_requested');
  });
  // 実行中のクリアはログの行だけ消す。進み具合と失敗の一覧はその実行のもので、main は失敗の一覧を
  // 増えたときにしか送らないので、ここで消すと二度と戻らない
  dom.clearLog.addEventListener('click', () => (running ? clearLogLines() : clearLog()));
  dom.logToggle.addEventListener('click', () => setLogOpen(dom.log.hidden));
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
    // 実行中の印・残り時間・処理中のファイルを消して描き直す（段階の完了は detector が出したとおり）
    if (result.stage) renderProgress(result);
    if (result.csvPath) {
      lastCsvPath = result.csvPath;
      dom.openCsv.hidden = false;
    }
    showNextSteps(result);
    // 正常に終わらなかったとき（中断を含む）・警告やエラーの行があったときは詳細ログを開く。止まった理由（事前チェックの
    // 断りなど）や、失敗の一覧に入らない警告（自データのスキップ・移動先に実体が残った・つながっていない保存フォルダなど）は
    // 詳細ログにしか無い（正常終了でも黙らせない）
    const { level, key, vars } = result.meaning;
    // 警告の数は main が数える（開き直す前に出たもの・実行中にクリアした行のものも含む）
    if (result.stoppedByUser || level !== 'ok' || result.warnings > 0) setLogOpen(true);
    if (result.stoppedByUser) {
      setStatus('warn', 'status_stopped');
      return;
    }
    // 終了の意味は main からキーで届く（言語を切り替えても訳し直せるように）
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
  // 走り続けている実行を先に取り戻す（setMode が archives の一覧を読みに行き、main に断られないように）
  if (context.run) resumeRun(context.run);
  setMode(mode);
}

init();
