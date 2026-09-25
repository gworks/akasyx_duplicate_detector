// main.js - メインプロセス。ウィンドウ、フォルダ選択、detector CLI の子プロセス起動
const { app, BrowserWindow, dialog, ipcMain, shell, clipboard } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { buildArgs, formatCommand, quoteArg, FormError } = require('./commands');
const i18n = require('./i18n');

// electron-ui/ はリポジトリルート直下に置く（設計書 §16）
const REPO_ROOT = path.dirname(__dirname);
const DETECTOR_DIR = path.join(REPO_ROOT, 'detector');

// 同梱した detector / crawler の実行形式（packaging/build_python.sh の PyInstaller onedir）の置き場。
// 配布版は Contents/Resources/bin。開発時も AKASYX_BIN_DIR=<repo>/build/pyi/dist で実行形式を試せる。
// 無ければ開発時の扱いで、detector を `uv run main.py` で起動する
const BIN_DIR = app.isPackaged
  ? path.join(process.resourcesPath, 'bin')
  : (process.env.AKASYX_BIN_DIR ? path.resolve(process.env.AKASYX_BIN_DIR) : null);
const DETECTOR_EXE = BIN_DIR
  ? path.join(BIN_DIR, 'akasyx-detector', `akasyx-detector${process.platform === 'win32' ? '.exe' : ''}`)
  : null;
// 子プロセスの cwd。相対で指定・出力されたパスはここ基準で解決する（resolveDetectorPath）
const RUN_DIR = DETECTOR_EXE ? path.dirname(DETECTOR_EXE) : DETECTOR_DIR;

// GUI から起動された Electron は PATH が最小限になるため、uv の在処を自力で探す
const UV_CANDIDATES = [
  path.join(os.homedir(), '.local', 'bin', 'uv'),
  '/opt/homebrew/bin/uv',
  '/usr/local/bin/uv',
];
const EXTRA_PATH_DIRS = UV_CANDIDATES.map((p) => path.dirname(p));

// detector の出力（英語固定）のうち、UI が拾う 2 種類。detector/ingest.py・dedupe.py・verify.py と揃える
const PROGRESS_LINE = /^Progress: (\d+) files \((.+)\)$/;
const CSV_LINE = /CSV report: (.+)$/;
const FLUSH_INTERVAL_MS = 80;

// 終了コードの意味。文言は辞書のキーで renderer に渡し、renderer が今の言語で出す
const EXIT_MEANINGS = {
  0: { level: 'ok', key: 'exit_0' },
  1: { level: 'error', key: 'exit_1' },
  2: { level: 'warn', key: 'exit_2' },
  3: { level: 'error', key: 'exit_3' },
};

let mainWindow = null;
let child = null;          // 同時実行は 1 本だけ
let starting = false;      // buildArgs〜spawn の間（削除確認ダイアログ待ちを含む）も「実行中」扱いにする
let activeRun = null;      // { command, startedAt, pump } ウィンドウを閉じて開き直したときの復元用
let stoppedByUser = false;

// --- 言語（i18n.js。前回選んだ言語 → OS の言語 → en） -------------------------

let lang = null;

function currentLang() {
  if (!lang) lang = i18n.resolveLang(loadSettings().lang, app.getLocale());
  return lang;
}

/** メインプロセスの文言（ダイアログ・起動エラー）を今の言語で。 */
function tr(key, vars) {
  return i18n.translator(currentLang())(key, vars);
}

/** 今開いているウィンドウへ送る。閉じられていれば捨てる（子プロセス自体は走り続ける）。 */
function sendToWindow(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
}

// --- 設定の保存（最後に使った値と保存用フォルダの履歴） ---------------------

function settingsPath() {
  return path.join(app.getPath('userData'), 'settings.json');
}

function loadSettings() {
  try {
    return JSON.parse(fs.readFileSync(settingsPath(), 'utf-8'));
  } catch {
    return {};
  }
}

function saveSettings(settings) {
  try {
    fs.mkdirSync(path.dirname(settingsPath()), { recursive: true });
    fs.writeFileSync(settingsPath(), JSON.stringify(settings, null, 2), 'utf-8');
    return true;
  } catch (e) {
    console.error('Failed to save settings:', e);
    return false;
  }
}

// --- データフォルダ（detector/config.py の data_root() と同じ規則） -------------

// akasyx_search の ~/Library/Application Support/akasyx/ とは分ける
// （search のデータ移動は akasyx/ の中身を丸ごと移し、削除手順も akasyx/ ごと消すため）
const APP_DATA_NAME = 'akasyx-duplicate-detector';

/** 配布版のデータフォルダ。.app の中は書き込めないので OS ごとの利用者データの場所に置く。 */
function appHome() {
  if (process.env.AKASYX_DETECTOR_HOME) return path.resolve(process.env.AKASYX_DETECTOR_HOME);
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', APP_DATA_NAME);
  }
  if (process.platform === 'win32') {
    return path.join(process.env.LOCALAPPDATA || os.homedir(), APP_DATA_NAME);
  }
  return path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), APP_DATA_NAME);
}

/** 正本 DB・作業用 DB・ログの既定の置き場。開発時（uv run）は <リポジトリ>/dist/。
 * 固めた実行形式は sys.frozen で配布版の置き場を使うので、実行形式を起動するときはそちらに合わせる。 */
function dataDir() {
  return DETECTOR_EXE ? appHome() : path.join(REPO_ROOT, 'dist');
}

// 配布版では UI の設定（settings.json）と Electron のキャッシュもデータフォルダの下に置く。
// 既定の ~/Library/Application Support/<productName>/ だと利用者のデータが 2 か所に分かれ、
// 完全に削除するときに消し忘れるため（README.dist.txt の削除手順は 1 フォルダで済むようにする）
if (app.isPackaged) app.setPath('userData', path.join(appHome(), 'ui'));

// --- 実行環境 ---------------------------------------------------------------

function resolveUv() {
  for (const candidate of UV_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return 'uv'; // PATH 頼み。見つからなければ spawn の error で伝わる
}

function appVersion() {
  // 配布版では version.txt を同梱しない。electron-builder が package.json の version を焼き込む
  if (app.isPackaged) return app.getVersion();
  try {
    return fs.readFileSync(path.join(REPO_ROOT, 'version.txt'), 'utf-8').trim();
  } catch {
    return '0.0.0';
  }
}

/** detector の起動コマンドの先頭部分（表示・コピー用）。実行形式があればそれ、無ければ `uv run main.py`。 */
function commandBase() {
  return DETECTOR_EXE ? [DETECTOR_EXE] : ['uv', 'run', 'main.py'];
}

function childEnv() {
  const extra = EXTRA_PATH_DIRS.join(path.delimiter);
  return {
    ...process.env,
    // 逐次出力させる。無いとログが終了までまとめて届く
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    PATH: `${process.env.PATH || ''}${path.delimiter}${extra}`,
    // 実行形式では detector が同梱の crawler を起動する（uv と crawler のソースが要らない）
    ...(BIN_DIR ? { AKASYX_SIBLINGS_BIN: BIN_DIR } : {}),
  };
}

// --- ウィンドウ -------------------------------------------------------------

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1120,
    height: 820,
    minWidth: 900,
    minHeight: 620,
    title: `akasyx Duplicate Detector  v${appVersion()}`, // 読み込み後は renderer の document.title（訳した名前）
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  interrupt();
});

/** 実行中の子プロセスへ SIGINT を送ります（uv とその下の python の両方に届かせる）。 */
function interrupt() {
  if (!child) return false;
  try {
    // detached:true で自前のプロセスグループにしてあるので、グループごと送る
    process.kill(-child.pid, 'SIGINT');
  } catch {
    try {
      child.kill('SIGINT');
    } catch {
      return false;
    }
  }
  return true;
}

// --- 子プロセスの出力を間引いて renderer へ ---------------------------------

/** stdout/stderr を行単位に切り、ログは束ねて、進捗は集計だけ送る。 */
function createStreamPump(send) {
  let pending = [];              // 未送信のログ行
  let progress = null;           // { processed, tally }
  let timer = null;
  let csvPath = null;

  const flush = () => {
    timer = null;
    if (pending.length) {
      send('run:log', pending);
      pending = [];
    }
    if (progress) {
      send('run:progress', progress);
      progress = null;
    }
  };

  const schedule = () => {
    if (!timer) timer = setTimeout(flush, FLUSH_INTERVAL_MS);
  };

  const tally = {};
  let processed = 0;

  const handleLine = (stream, text) => {
    const match = PROGRESS_LINE.exec(text);
    if (match) {
      // 1ファイル1行。件数が多いとログ窓が実用にならないので集計に落とす
      processed = Number(match[1]);
      const result = match[2];
      tally[result] = (tally[result] || 0) + 1;
      progress = { processed, tally: { ...tally } };
      schedule();
      return;
    }
    const csv = CSV_LINE.exec(text);
    if (csv) csvPath = csv[1].trim();
    pending.push({ stream, text });
    if (pending.length > 400) flush();
    else schedule();
  };

  const makeReader = (stream) => {
    let buffer = '';
    return (chunk) => {
      buffer += chunk;
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) handleLine(stream, line.replace(/\r$/, ''));
    };
  };

  return {
    onStdout: makeReader('out'),
    onStderr: makeReader('err'),
    /** 現時点の集計（ウィンドウを開き直したときの初期表示用）。 */
    snapshot() {
      return { processed, tally: { ...tally }, csvPath };
    },
    finish() {
      if (timer) clearTimeout(timer);
      flush();
      return { processed, tally: { ...tally }, csvPath };
    },
  };
}

// --- IPC --------------------------------------------------------------------

ipcMain.handle('app:context', () => ({
  repoRoot: REPO_ROOT,
  detectorDir: RUN_DIR,
  // 「コピー」で `cd` に貼る用。スペース等を含むチェックアウト先でもそのまま貼れるようにする
  detectorDirQuoted: quoteArg(RUN_DIR),
  version: appVersion(),
  dataDir: dataDir(),
  uvPath: resolveUv(),
  detectorFound: fs.existsSync(DETECTOR_EXE || path.join(DETECTOR_DIR, 'main.py')),
  bundled: !!DETECTOR_EXE,
  // macOS でウィンドウを閉じても子プロセスは走り続けるので、開き直した画面に状態を返す
  run: activeRun
    ? { command: activeRun.command, startedAt: activeRun.startedAt, ...activeRun.pump.snapshot() }
    : null,
}));

ipcMain.handle('i18n:get', () => ({ lang: currentLang(), dict: i18n.dictionary(currentLang()) }));
ipcMain.handle('i18n:set', (_event, next) => {
  // 保存は renderer の settings:save（snapshot に lang を含む）に任せる
  lang = i18n.normalizeLang(next) || currentLang();
  return { lang, dict: i18n.dictionary(lang) };
});

ipcMain.handle('settings:load', () => loadSettings());
ipcMain.handle('settings:save', (_event, settings) => saveSettings(settings));

ipcMain.handle('dialog:pick', async (_event, options = {}) => {
  const properties = options.files
    ? ['openFile', 'openDirectory']   // macOS はファイルとフォルダを同時に許可できる
    : ['openDirectory'];
  const result = await dialog.showOpenDialog(mainWindow, {
    title: options.title || tr('pick_default_title'),
    defaultPath: options.defaultPath || undefined,
    properties: [...properties, 'createDirectory'],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle('command:preview', (_event, form) => {
  try {
    return { command: formatCommand(buildArgs(form), commandBase()), error: null };
  } catch (e) {
    if (e instanceof FormError) return { command: null, error: tr(e.key, e.vars) };
    throw e;
  }
});

/**
 * detector が出力したパス（CSV レポート、--log-dir 等）を絶対パスにします。
 * 子プロセスは cwd=RUN_DIR で動くので、相対で指定・出力されたものはそこ基準で解決する。
 * Electron 自身の cwd（npm run dev なら electron-ui/）基準にすると別の場所を指してしまう。
 */
function resolveDetectorPath(target) {
  return target ? path.resolve(RUN_DIR, String(target)) : null;
}

ipcMain.handle('shell:reveal', (_event, target) => {
  const resolved = resolveDetectorPath(target);
  if (!resolved || !fs.existsSync(resolved)) return false;
  shell.showItemInFolder(resolved);
  return true;
});

ipcMain.handle('shell:open', (_event, target) => {
  const resolved = resolveDetectorPath(target);
  if (!resolved || !fs.existsSync(resolved)) return false;
  shell.openPath(resolved);
  return true;
});

// データフォルダは初回の実行まで無いので、作ってから開く（空でも場所が分かるように）
ipcMain.handle('data:open', () => {
  const target = dataDir();
  try {
    fs.mkdirSync(target, { recursive: true });
  } catch {
    return false;
  }
  shell.openPath(target);
  return true;
});

ipcMain.handle('clipboard:write', (_event, text) => {
  clipboard.writeText(String(text ?? ''));
  return true;
});

ipcMain.handle('run:start', async (_event, form) => {
  // 確認ダイアログを待っている間も含めて 1 本に絞る（child は spawn 後にしか立たない）
  if (child || starting) return { started: false, error: tr('busy') };
  starting = true;
  try {
    return await startRun(form);
  } finally {
    starting = false; // spawn できていれば以降は child が「実行中」を表す
  }
});

async function startRun(form) {
  let args;
  try {
    args = buildArgs(form);
  } catch (e) {
    if (e instanceof FormError) return { started: false, error: tr(e.key, e.vars) };
    throw e;
  }

  // ファイルを消すコマンドだけは、renderer 任せにせずここで必ず確認を取る
  if (form.mode === 'delete-duplicates' && form.yes) {
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: 'warning',
      buttons: [tr('delete_confirm_cancel'), tr('delete_confirm_ok')],
      defaultId: 0,
      cancelId: 0,
      message: tr('delete_confirm_message'),
      detail: `${tr('delete_confirm_detail')}\n\n${formatCommand(args, commandBase())}`,
    });
    if (response !== 1) return { started: false, error: null, canceled: true };
  }

  // 起動時のウィンドウではなく「今のウィンドウ」へ送る。閉じて開き直しても制御を失わないようにする
  const send = sendToWindow;
  const pump = createStreamPump(send);
  // uv は PATH が最小限でも見つかるよう絶対パスで起動する（表示は `uv` のまま）
  const [program, ...baseArgs] = DETECTOR_EXE ? commandBase() : [resolveUv(), 'run', 'main.py'];
  const command = formatCommand(args, commandBase());
  stoppedByUser = false;

  child = spawn(program, [...baseArgs, ...args], {
    cwd: RUN_DIR,
    env: childEnv(),
    detached: true, // 中断をプロセスグループごと送るため（uv の下の python にも届く）
  });
  activeRun = { command, startedAt: Date.now(), pump };
  let finished = false;
  const finish = (payload) => {
    if (finished) return;
    finished = true;
    child = null;
    activeRun = null;
    send('run:exit', { ...payload, ...pump.finish() });
  };
  child.stdout.setEncoding('utf-8');
  child.stderr.setEncoding('utf-8');
  child.stdout.on('data', pump.onStdout);
  child.stderr.on('data', pump.onStderr);

  child.on('error', (e) => {
    let detail = e.message;
    if (e.code === 'ENOENT') {
      detail = tr(DETECTOR_EXE ? 'bundled_detector_missing' : 'uv_missing', { program });
    }
    send('run:log', [{ stream: 'err', text: tr('launch_failed_detail', { detail }) }]);
    // spawn 自体が失敗した場合は close が来ないことがあるので、ここで打ち切る
    finish({
      code: null,
      signal: null,
      stoppedByUser: false,
      meaning: { level: 'error', key: 'launch_failed' },
    });
  });

  child.on('close', (code, signal) => {
    finish({
      code,
      signal,
      stoppedByUser,
      meaning: EXIT_MEANINGS[code] || (signal
        ? { level: 'error', key: 'exit_signal', vars: { signal } }
        : { level: 'error', key: 'exit_code', vars: { code } }),
    });
  });

  return { started: true, error: null, command };
}

ipcMain.handle('run:stop', () => {
  if (!child) return false;
  stoppedByUser = true;
  // detector は KeyboardInterrupt を受けて status='interrupted' で後片付けする
  return interrupt();
});
