// main.js - メインプロセス。ウィンドウ、フォルダ選択、detector CLI の子プロセス起動
const { app, BrowserWindow, dialog, ipcMain, shell, clipboard } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { buildArgs, formatCommand, quoteArg, FormError } = require('./commands');

// electron-ui/ はリポジトリルート直下に置く（設計書 §16）
const REPO_ROOT = path.dirname(__dirname);
const DETECTOR_DIR = path.join(REPO_ROOT, 'detector');

// GUI から起動された Electron は PATH が最小限になるため、uv の在処を自力で探す
const UV_CANDIDATES = [
  path.join(os.homedir(), '.local', 'bin', 'uv'),
  '/opt/homebrew/bin/uv',
  '/usr/local/bin/uv',
];
const EXTRA_PATH_DIRS = UV_CANDIDATES.map((p) => path.dirname(p));

const PROGRESS_LINE = /^判定中: (\d+)件 \((.+)\)$/;
const CSV_LINE = /CSV レポート: (.+)$/;
const FLUSH_INTERVAL_MS = 80;

const EXIT_MEANINGS = {
  0: { level: 'ok', text: '正常終了しました' },
  1: { level: 'error', text: '致命的エラーで停止しました' },
  2: { level: 'warn', text: '完走しましたが失敗が 1 件以上あります（内容の確認が必要です）' },
  3: { level: 'error', text: '事前チェックで拒否されました（入れ子・crawler 不在など）' },
};

let mainWindow = null;
let child = null;          // 同時実行は 1 本だけ
let starting = false;      // buildArgs〜spawn の間（削除確認ダイアログ待ちを含む）も「実行中」扱いにする
let activeRun = null;      // { command, startedAt, pump } ウィンドウを閉じて開き直したときの復元用
let stoppedByUser = false;

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
    console.error('設定の保存に失敗:', e);
    return false;
  }
}

// --- 実行環境 ---------------------------------------------------------------

function resolveUv() {
  for (const candidate of UV_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return 'uv'; // PATH 頼み。見つからなければ spawn の error で伝わる
}

function appVersion() {
  try {
    return fs.readFileSync(path.join(REPO_ROOT, 'version.txt'), 'utf-8').trim();
  } catch {
    return '0.0.0';
  }
}

function childEnv() {
  const extra = EXTRA_PATH_DIRS.join(path.delimiter);
  return {
    ...process.env,
    // 逐次出力させる。無いとログが終了までまとめて届く
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    PATH: `${process.env.PATH || ''}${path.delimiter}${extra}`,
  };
}

// --- ウィンドウ -------------------------------------------------------------

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1120,
    height: 820,
    minWidth: 900,
    minHeight: 620,
    title: `重複判定アーカイバ  v${appVersion()}`,
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
  detectorDir: DETECTOR_DIR,
  // 「コピー」で `cd` に貼る用。スペース等を含むチェックアウト先でもそのまま貼れるようにする
  detectorDirQuoted: quoteArg(DETECTOR_DIR),
  version: appVersion(),
  uvPath: resolveUv(),
  detectorFound: fs.existsSync(path.join(DETECTOR_DIR, 'main.py')),
  // macOS でウィンドウを閉じても子プロセスは走り続けるので、開き直した画面に状態を返す
  run: activeRun
    ? { command: activeRun.command, startedAt: activeRun.startedAt, ...activeRun.pump.snapshot() }
    : null,
}));

ipcMain.handle('settings:load', () => loadSettings());
ipcMain.handle('settings:save', (_event, settings) => saveSettings(settings));

ipcMain.handle('dialog:pick', async (_event, options = {}) => {
  const properties = options.files
    ? ['openFile', 'openDirectory']   // macOS はファイルとフォルダを同時に許可できる
    : ['openDirectory'];
  const result = await dialog.showOpenDialog(mainWindow, {
    title: options.title || '選択',
    defaultPath: options.defaultPath || undefined,
    properties: [...properties, 'createDirectory'],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle('command:preview', (_event, form) => {
  try {
    return { command: formatCommand(buildArgs(form)), error: null };
  } catch (e) {
    if (e instanceof FormError) return { command: null, error: e.message };
    throw e;
  }
});

ipcMain.handle('shell:reveal', (_event, target) => {
  if (!target) return false;
  if (fs.existsSync(target)) {
    shell.showItemInFolder(target);
    return true;
  }
  return false;
});

ipcMain.handle('shell:open', (_event, target) => {
  if (!target || !fs.existsSync(target)) return false;
  shell.openPath(target);
  return true;
});

ipcMain.handle('clipboard:write', (_event, text) => {
  clipboard.writeText(String(text ?? ''));
  return true;
});

ipcMain.handle('run:start', async (_event, form) => {
  // 確認ダイアログを待っている間も含めて 1 本に絞る（child は spawn 後にしか立たない）
  if (child || starting) return { started: false, error: '実行中です。終わるまで待つか中断してください' };
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
    if (e instanceof FormError) return { started: false, error: e.message };
    throw e;
  }

  // ファイルを消すコマンドだけは、renderer 任せにせずここで必ず確認を取る
  if (form.mode === 'delete-duplicates' && form.yes) {
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: 'warning',
      buttons: ['キャンセル', '削除を実行する'],
      defaultId: 0,
      cancelId: 0,
      message: '据え置いた重複ファイルを実際に削除します',
      detail:
        '検証を通ったものだけが対象ですが、投入元のファイルは失われます。\n'
        + '退避先（--trash-dir）を指定しておけば削除ではなく移動になります。\n\n'
        + formatCommand(args),
    });
    if (response !== 1) return { started: false, error: null, canceled: true };
  }

  // 起動時のウィンドウではなく「今のウィンドウ」へ送る。閉じて開き直しても制御を失わないようにする
  const send = sendToWindow;
  const pump = createStreamPump(send);
  const uv = resolveUv();
  const command = formatCommand(args);
  stoppedByUser = false;

  child = spawn(uv, ['run', 'main.py', ...args], {
    cwd: DETECTOR_DIR,
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
    const detail = e.code === 'ENOENT'
      ? `uv が見つかりません（${uv}）。uv をインストールし PATH を通してから再実行してください`
      : e.message;
    send('run:log', [{ stream: 'err', text: `起動できませんでした: ${detail}` }]);
    // spawn 自体が失敗した場合は close が来ないことがあるので、ここで打ち切る
    finish({
      code: null,
      signal: null,
      stoppedByUser: false,
      meaning: { level: 'error', text: '起動できませんでした' },
    });
  });

  child.on('close', (code, signal) => {
    finish({
      code,
      signal,
      stoppedByUser,
      meaning: EXIT_MEANINGS[code] || {
        level: 'error',
        text: signal ? `シグナル ${signal} で終了しました` : `終了コード ${code}`,
      },
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
