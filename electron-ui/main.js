// main.js - メインプロセス。ウィンドウ、フォルダ選択、detector CLI の子プロセス起動
const { app, BrowserWindow, dialog, ipcMain, shell, clipboard } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const crypto = require('node:crypto');
const path = require('node:path');

const { buildArgs, formatCommand, quoteArg, FormError, normalizePath, toFieldValue } = require('./commands');
const {
  scanLine, parseArchivesJson, createRunState, countsAsWarning, createNoticeCollector, createTailCollector,
} = require('./detector-output');
const i18n = require('./i18n');
// 実行ボタンの印（#20）。画面と同じファイルを使い、組んだ引数から決める
const { argsEffect, sameEffect } = require('./renderer/modes');
// 実行画面に出す今の既定の 1 行（#21）。組んだ引数から作る
const { summarizeArgs, previewDefaults } = require('./defaults-summary');
// かんたん取り込み（#22）: ドロップされたものの受け付けと順番待ちの列
const {
  checkSources, pairKeys, sourceKeys, overlapsAny, createQueue, buildQuickArgs, quickPreviewForm,
} = require('./quick-queue');

// 確認のダイアログに並べる投入元・コマンドの数（多く落としても画面からはみ出さない。残りは件数で出す）
const QUICK_CONFIRM_SOURCES = 10;
const QUICK_CONFIRM_COMMANDS = 2;
const QUICK_FAILURES = 100; // 列の完了の行に持たせる失敗の一覧の件数
// 実行ボタンの取り込みの投入元の実体の位置を待つ上限（列との重なりを見るため。つながっていない場所で止めない）
const REALPATH_WAIT_MS = 2000;

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

const FLUSH_INTERVAL_MS = 80;
const ETA_TICK_MS = 1000; // 残り時間を計算し直して送る間隔（#18）
const LIST_TIMEOUT_MS = 120000; // 保存フォルダの一覧（archives --json）を待つ上限

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
let listing = null;        // archives --json で一覧を読んでいるプロセス。終了（close）するまで実行を断る
let stoppedByUser = false;
// かんたん取り込みの順番待ちの列（#22）。次の項目は、何も実行していない（child・starting・listing が無い）ときに始める。
// アプリを終えるときは待ちの列を捨て、次回に持ち越さない（quitting の後は始めない）
const quickQueue = createQueue();
let quitting = false;
let stopKillTimer = null;  // Windows で停止ファイルを作った後、応えなければ木ごと止める予約（#32）
let runSeq = 0;            // 実行の通し番号（activeRun.id。中断がどの実行へのものかを見分ける）
// 実行ボタンから始めた実行の、最後の結果（#22）。列の項目が始まると画面の出力が置き換わるので、そのときに列の完了の
// 行として残す。実行ボタンで次の実行を始めたら（自分の操作で置き換えた）捨てる
// （finish で入れ、pumpQuickQueue で列に移し、run:start で捨てる）
let lastOtherRun = null;
let reservedAddKeys = [];  // 実行ボタンの取り込みが起動するまでの投入元の比較キー（実体の位置を待つ間の予約。#22）
// 最後に読めた登録済みの保存フォルダと、そのときの正本 DB の欄の値（archives:list。かんたん取り込みの重なりの確認に、
// 同じ正本 DB のときだけ使う）
let lastListed = { archiveDb: null, roots: [] };

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
    // 逐次出力させる。無いとログが終了までまとめて届く。配布版の実行形式はこの 2 つ（PYTHONUNBUFFERED・
    // PYTHONIOENCODING）を読まないので、ビルドの --python-option で代わりを指定している（出力はバッファせず utf-8。
    // UTF-8 モードなので utf-8 にできない文字は、stdout では strict ではなく surrogateescape で出す（stderr は
    // backslashreplace）。packaging/build_python.sh・#24）
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    // 処理中のファイル・失敗の行（JSON）を出させる。CLI では出ない（detector/ingest.py の UI_PROGRESS_ENV。#18）
    AKASYX_UI_PROGRESS: '1',
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
  // 中断した実行が終わった後に、列の次の項目を始めない
  quitting = true;
  quickQueue.discardWaiting();
  interrupt();
  killGroup(listing, 'SIGKILL');
});

/** detached で起動した子プロセスのグループへシグナルを送ります（uv の下の python にも届く）。
 * Windows は taskkill で木ごと止める。止め終わりを待つ Promise を返す。 */
function killGroup(proc, signal) {
  if (!proc) return Promise.resolve();
  if (process.platform === 'win32') {
    // Windows は負の PID（プロセスグループ）へのシグナルが無い。uv の下の python まで木ごと止め、
    // taskkill が終わるのを待てるようにする（止まる前に「終わった」とみなさない）
    return new Promise((resolve) => {
      const killer = spawn('taskkill', ['/pid', String(proc.pid), '/T', '/F'], { windowsHide: true });
      killer.on('error', () => {
        try {
          proc.kill(signal);
        } catch {
          // 既に終わっている
        }
        resolve();
      });
      killer.on('close', () => resolve());
    });
  }
  try {
    process.kill(-proc.pid, signal);
  } catch {
    try {
      proc.kill(signal);
    } catch {
      // 既に終わっている
    }
  }
  return Promise.resolve();
}

/**
 * 実行中の子プロセスへ SIGINT を送ります（uv とその下の python の両方に届かせる）。1 つの実行には 1 度だけ送る
 * （2 度目の SIGINT は detector の後片付けを途中で止めうる。中断ボタンもアプリの終了も、ここを通る）。
 * true=送った（前に送ったものを含む） / false=送れなかった / null=もう居ない（終わるところ。exit がこの後に届く）。
 */
function interrupt() {
  if (!child) return null;
  if (stoppedByUser) return true;
  const sent = sendInterrupt();
  if (sent) stoppedByUser = true;
  return sent;
}

function sendInterrupt() {
  // Windows にはプロセスグループへのシグナルが無く、child.kill は uv を強制終了するだけで、その下の python（detector）には
  // 中断が届かない（#32）。停止ファイルを作って detector 自身に中断させる（SIGINT と同じ後片付けになる）。
  // 作れなければ木ごと強制終了する
  // 停止ファイルの場所は実行の記録が持つ（実行ごとに違う。起動の途中で失敗した実行には無い）
  const stopFile = activeRun && activeRun.stopFile;
  if (process.platform === 'win32' && stopFile) {
    const target = child;
    const run = activeRun;
    // もう終わっている（close を待つところ）なら頼まない（終わった実行を「中断」と記録しない）
    if (target.exitCode !== null || target.signalCode !== null) return null;
    try {
      fs.writeFileSync(stopFile, '');
      // detector が応えない（起動前の uv sync の最中・長い C の呼び出しの中など）まま残らないよう、待っても終わらなければ
      // 木ごと強制終了する（後片付けは次の起動の復旧に任せる）
      clearTimeout(stopKillTimer);
      stopKillTimer = setTimeout(() => {
        stopKillTimer = null;
        if (child === target && target.exitCode === null && target.signalCode === null) {
          run.forceKilled = true; // 停止ファイルが残っていても中断として扱う（close の判定）
          killGroup(target, 'SIGKILL');
        }
      }, STOP_FILE_GRACE_MS);
      return true;
    } catch {
      // 下へ
    }
  }
  if (process.platform === 'win32') {
    // 停止ファイルを作れない（一時フォルダを拒まれた等）。下の止め方は uv しか止めず python が残るので、すぐ木ごと
    // 強制終了する（後片付けは次の起動の復旧に任せる。#32）
    if (child.exitCode !== null || child.signalCode !== null) return null;
    if (activeRun) activeRun.forceKilled = true;
    killGroup(child, 'SIGKILL');
    return true;
  }
  try {
    // detached:true で自前のプロセスグループにしてあるので、グループごと送る
    process.kill(-child.pid, 'SIGINT');
    return true;
  } catch {
    // グループに送れない（Windows にはプロセスグループが無く、ESRCH になる）。子プロセスへ直接送る
  }
  const exited = () => child.exitCode !== null || child.signalCode !== null;
  if (exited()) return null;
  try {
    if (child.kill('SIGINT')) return true;
  } catch {
    // 下で終わったかどうかを見る
  }
  return exited() ? null : false;
}

// --- 子プロセスの出力を間引いて renderer へ ---------------------------------

/** stdout/stderr を行単位に切り、ログは束ねて、進捗は集計だけ送る。 */
function createStreamPump(send, { keepOutput = true } = {}) {
  const run = createRunState();
  let pending = [];              // 未送信のログ行
  let progressChanged = false;   // 前回送ってから進み具合が変わったか
  let sentFailures = 0;          // 前回送った失敗の一覧の件数（一覧は変わったときだけ送る）
  let timer = null;
  // 終わった（finish / discard した）ポンプは、行を扱わず、送る予約・残り時間のタイマーも作らない（終わった後に届いた行を、
  // 次の実行の画面へ送らない）
  let closed = false;
  let csvPath = null;
  const forgetIds = new Set(); // Note: 行が示す、つながっていない保存フォルダ（登録を外す候補。#11）
  let adoptPath = null;        // 断りの案内が示す、adopt が要るフォルダ
  let warnings = 0;            // 警告・エラーの行の数（開き直す前に出たものも、終わったときに詳細ログを開く判断に使う）
  const notices = createNoticeCollector(); // 警告・エラーの行そのもの（かんたん取り込みの列の行に残す。#22）
  // 出力の末尾（crawler の行は除く）。実行ボタンから始めた実行の出力は、列の項目が始まると画面から消えるので行に残す
  // （列の項目の行には持たせないので、そのときは集めない。keepOutput=false）
  const tail = keepOutput ? createTailCollector() : null;

  const flush = () => {
    timer = null;
    if (pending.length) {
      send('run:log', pending);
      pending = [];
    }
    if (progressChanged) sendProgress();
  };

  // 失敗の一覧（最大 500 件）は、変わったときだけ、まるごと入れる。進み具合は 80 ミリ秒ごとに送るので毎回は写さない。
  // 増えた分だけ送る形にすると、受け手と食い違ったときに直らない（上限があるので、まるごとでも量は限られる）。
  // 上限を超えた後の失敗は一覧を変えない（超えた件数は一覧なしの写しにも入る）。sentFailures が -1（開き直した）なら次は必ず入れる
  const sendProgress = () => {
    const size = run.failureListSize();
    send('run:progress', run.snapshot({ failures: size !== sentFailures }));
    sentFailures = size;
    progressChanged = false;
  };

  // 残り時間は時間がたつだけで変わる。1 件に時間がかかって行が来ない間も、1 秒ごとに計算し直して送る。
  // タイマーは判定と移動の段階に入ってから作る（取り込み以外のコマンドでは作らない）
  let ticker = null;
  const stopTicker = () => {
    clearInterval(ticker);
    ticker = null;
  };
  const startTicker = () => {
    if (ticker || !run.ticking()) return;
    ticker = setInterval(() => {
      // 判定と移動が終わったら（後片付け・全件処理済み）止める。後片付けの間に空回りさせない
      if (!run.ticking()) {
        stopTicker();
        return;
      }
      if (!timer) sendProgress();
    }, ETA_TICK_MS);
  };

  const schedule = () => {
    if (!timer) timer = setTimeout(flush, FLUSH_INTERVAL_MS);
  };

  const stopTimers = () => {
    stopTicker();
    if (timer) clearTimeout(timer);
    timer = null;
  };

  const handleLine = (stream, text) => {
    // 終わった後に届いた行は扱わない（送る予約も、たまった行の直接の送信もしない）。閉じた後の出力を捨てるのはここ
    // 1 か所（起動の途中で失敗したときは、launch がリスナーも外す。close の後には出力は届かない）
    if (closed) return;
    const found = scanLine(text);
    // 進み具合の行（1 ファイルに 2〜3 行・crawler の進捗）は、件数が多いとログ窓が実用にならないので集計に落とす。
    // 読むのは stdout だけ（stderr のログに改行入りのファイル名が出ても、進み具合の行に化けないように）
    if (stream === 'out' && run.apply(found)) {
      startTicker();
      progressChanged = true;
      schedule();
      return;
    }
    if (found.csvPath) csvPath = found.csvPath;
    if (found.forgetId) forgetIds.add(found.forgetId);
    if (found.adoptPath) adoptPath = found.adoptPath;
    const warning = countsAsWarning(text);
    if (warning) warnings += 1;
    // 取り込みの一覧作成の段階に stdout に出た行は crawler のもの（その [Summary] は取り込みの完了ではない。#18）。
    // 段階の行も stdout なので順番が保たれる。stderr はパイプが別で前後が保証されないので印を付けない
    const crawler = stream === 'out' && run.inScan();
    // 列の行に残す警告の行は detector のものだけ（crawler の行で先頭が埋まり、終わった理由が押し出されないように）
    if (!crawler) notices.add(text, warning, stream);
    pending.push(crawler ? { stream, text, crawler: true } : { stream, text });
    if (tail && !crawler) tail.add(text);
    if (pending.length > 400) flush();
    else schedule();
  };

  const makeReader = (stream) => {
    let buffer = '';
    const read = (chunk) => {
      buffer += chunk;
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) handleLine(stream, line.replace(/\r$/, ''));
    };
    // 改行の無い最後の行（落ちたときの最後の例外の行など）も、終わったときに 1 行として扱う
    read.flush = () => {
      if (buffer) handleLine(stream, buffer);
      buffer = '';
    };
    return read;
  };
  const readOut = makeReader('out');
  const readErr = makeReader('err');

  const state = () => ({ ...run.snapshot(), csvPath, forgetIds: [...forgetIds], adoptPath, warnings });

  return {
    onStdout: readOut,
    onStderr: readErr,
    /** 現時点の集計（ウィンドウを開き直したときの初期表示用）。 */
    snapshot: state,
    /**
     * 次に送る進み具合に、失敗の一覧をまるごと入れる。開き直した画面は一覧を持っていないので、写しと次の送信の
     * 行き違いで一覧が足りないまま残らないように、開き直したとき（app:context）に呼ぶ。
     */
    resendFailures() {
      sentFailures = -1;
    },
    /** 送らずに止める（タイマーを止め、ためた行を捨てる）。始まったことを画面に伝えていない実行で使う。 */
    discard() {
      closed = true;
      stopTimers();
      pending = [];
      progressChanged = false;
    },
    finish() {
      // 読みかけの最後の行を先に流してから止める（流した行が送る予約・残り時間のタイマーを作っても、ここで止める）
      readOut.flush();
      readErr.flush();
      closed = true;
      stopTimers();
      flush();
      return { ...state(), notices: notices.snapshot(), output: tail ? tail.snapshot() : null };
    },
  };
}

// --- IPC --------------------------------------------------------------------

ipcMain.handle('app:context', () => {
  // 開き直した画面は失敗の一覧を持っていないので、次の送信で一覧をまるごと送る
  if (activeRun) activeRun.pump.resendFailures();
  return appContext();
});

function appContext() {
  return {
    repoRoot: REPO_ROOT,
    detectorDir: RUN_DIR,
    // 「コピー」で `cd` に貼る用。スペース等を含むチェックアウト先でもそのまま貼れるようにする
    detectorDirQuoted: quoteArg(RUN_DIR),
    version: appVersion(),
    dataDir: dataDir(),
    uvPath: resolveUv(),
    detectorFound: fs.existsSync(DETECTOR_EXE || path.join(DETECTOR_DIR, 'main.py')),
    bundled: !!DETECTOR_EXE,
    // renderer がパスの区切りを決めるのに使う（\ を区切りとみなすのは Windows だけ）
    platform: process.platform,
    // macOS でウィンドウを閉じても子プロセスは走り続けるので、開き直した画面に状態を返す
    run: activeRun
      ? {
        id: activeRun.id, command: activeRun.command, mode: activeRun.mode,
        logDir: activeRun.logDir, archiveDb: activeRun.archiveDb,
        startedAt: activeRun.startedAt,
        stopRequested: stoppedByUser, // 中断を頼んだ後に開き直した画面で、もう一度押させない（#19）
        ...activeRun.pump.snapshot(),
      }
      : null,
  };
}

ipcMain.handle('i18n:get', () => ({ lang: currentLang(), dict: i18n.dictionary(currentLang()) }));
ipcMain.handle('i18n:set', (_event, next) => {
  // 保存は renderer の settings:save（snapshot に lang を含む）に任せる
  lang = i18n.normalizeLang(next) || currentLang();
  return { lang, dict: i18n.dictionary(lang) };
});

ipcMain.handle('settings:load', () => loadSettings());
ipcMain.handle('settings:save', (_event, settings) => saveSettings(settings));

ipcMain.handle('dialog:pick', async (_event, options = {}) => {
  // macOS はファイルとフォルダを同時に許可できる。filesOnly はファイルだけ（Windows・Linux は両方を同時に選べず
  // フォルダだけになるので、ファイルを選ぶ入口を分ける。#22）
  const properties = options.filesOnly
    ? ['openFile']
    : options.files ? ['openFile', 'openDirectory'] : ['openDirectory'];
  // multiple: いくつも選べる（かんたん取り込み。#22）。選んだものの一覧を返す
  if (options.multiple) properties.push('multiSelections');
  const result = await dialog.showOpenDialog(mainWindow, {
    title: options.title || tr('pick_default_title'),
    defaultPath: options.defaultPath || undefined,
    properties: [...properties, 'createDirectory'],
  });
  if (result.canceled) return null;
  return options.multiple ? result.filePaths : result.filePaths[0];
});

ipcMain.handle('command:preview', (_event, form) => {
  // かんたん取り込み（#22）は実行ボタンを使わない。今の既定の 1 行だけを、取り込みの既定で作る
  if (form && form.mode === 'quick') {
    return { command: null, error: null, effect: null, defaults: previewDefaults(quickPreviewForm(form)) };
  }
  // 今の既定の 1 行は組んだ引数から作る。必須の欄が空でコマンドを組めないときも出す（仮の値で組む。値が不正なら出さない）
  try {
    const args = buildArgs(form);
    return {
      command: formatCommand(args, commandBase()), error: null, effect: argsEffect(args), defaults: summarizeArgs(args),
    };
  } catch (e) {
    if (e instanceof FormError) return { command: null, error: tr(e.key, e.vars), defaults: previewDefaults(form) };
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

// 呼び出し元は「ログの場所を開く」（パス欄の値）。CLI に渡す --log-dir と同じ正規化
// （囲み引用符・エスケープ・~/）を通さないと、detector の出力先と違う場所を開いてしまう
ipcMain.handle('shell:open', (_event, target) => {
  const resolved = resolveDetectorPath(normalizePath(String(target ?? '')));
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

// detector が出力した実パスをパス欄に入れる値にする（パス欄の正規化で書き換わらないように）
ipcMain.handle('path:field', (_event, target) => toFieldValue(target));

ipcMain.handle('clipboard:write', (_event, text) => {
  clipboard.writeText(String(text ?? ''));
  return true;
});

// detector が見張る停止ファイルの場所を渡す環境変数（detector/stop_watch.py の STOP_FILE_ENV と同じ）
const STOP_FILE_ENV = 'AKASYX_STOP_FILE';
// 停止ファイルを作ってから、detector が自分で止まるのを待つ時間（過ぎたら木ごと強制終了する）
// 後片付け（DB への書き込み・CSV・移動の途中の後始末）には時間がかかりうるので、長めにとる
const STOP_FILE_GRACE_MS = 60000;

/** 実行ごとの停止ファイルの場所（OS の一時フォルダ。投入元・保存フォルダの中には作らない。名前は実行ごとに違う）。 */
function newStopFile() {
  return path.join(os.tmpdir(), `akasyx-detector-stop-${process.pid}-${crypto.randomUUID()}`);
}

/** 停止ファイルを消す（無ければ何もしない。detector が中断を起こすときにも消す）。 */
function dropStopFile(file) {
  try {
    fs.rmSync(file, { force: true });
  } catch {
    // 消せなくても実行には関わらない（一時フォルダの中）
  }
}

/** detector を起動します。uv は PATH が最小限でも見つかるよう絶対パスで起動する（表示は `uv` のまま）。 */
function spawnDetector(args, { extraEnv = {}, ...options } = {}) {
  const [program, ...baseArgs] = DETECTOR_EXE ? commandBase() : [resolveUv(), 'run', 'main.py'];
  // extraEnv: この実行だけに渡す環境変数（停止ファイルの場所。#32）
  const proc = spawn(program, [...baseArgs, ...args], { cwd: RUN_DIR, env: { ...childEnv(), ...extraEnv }, ...options });
  return { proc, program };
}

/** 起動できなかったときの説明（uv や同梱の実行形式が無い）。 */
function launchErrorDetail(e, program) {
  if (e.code === 'ENOENT') return tr(DETECTOR_EXE ? 'bundled_detector_missing' : 'uv_missing', { program });
  return e.message;
}

// 登録済みの保存フォルダの一覧（#11）。実行中は断る（取り込み中の正本 DB を同時に開かない）。
// 読めなかったときは空の一覧ではなく error を返す（「登録が 0 件」と取り違えないため）
ipcMain.handle('archives:list', (_event, form) => {
  // 実行中・前の読み込み中は断る。busy は一時的なので、画面はエラーにせず少し待って読み直す
  if (runBusy()) return { archives: null, busy: true, error: null };
  let args;
  try {
    args = buildArgs({ ...form, mode: 'archives', forgetId: '', json: true });
  } catch (e) {
    if (e instanceof FormError) return { archives: null, error: tr(e.key, e.vars) };
    throw e;
  }
  return new Promise((resolve) => {
    let spawned;
    try {
      spawned = spawnDetector(args, { detached: true });
    } catch (e) {
      // 引数の不正（NUL 文字入りのパス等）では spawn が同期で投げる
      resolve({ archives: null, error: tr('archives_error', { detail: e.message }) });
      return;
    }
    const { proc, program } = spawned;
    listing = proc;
    let settled = false;
    // 実行を断るのはプロセスが本当に終わるまで（打ち切った後もまだ正本 DB を開いているかもしれない）
    const done = (result) => {
      if (settled) return;
      settled = true;
      if (listing === proc) listing = null;
      clearTimeout(timer);
      resolve(result);
      pumpQuickQueue(); // 一覧を読む間に待たせた列の項目を始める（#22）
    };
    // つながっていないネットワークの登録があると stat が OS のタイムアウトまで戻らない（#7）。
    // 一覧の取得中は実行を断っているので、いつまでも待たずに打ち切る（結果は close で返す）
    let killed = null; // 打ち切ったときの、止め終わりを待つ Promise
    const timer = setTimeout(() => {
      killed = killGroup(proc, 'SIGKILL');
    }, LIST_TIMEOUT_MS);

    let out = '';
    let err = '';
    proc.stdout.setEncoding('utf-8');
    proc.stderr.setEncoding('utf-8');
    proc.stdout.on('data', (chunk) => { out += chunk; });
    proc.stderr.on('data', (chunk) => { err += chunk; });
    // 起動できなかったとき（close が来ないことがある）
    // 起動できたプロセスの error（止めるシグナルを送れなかった等）では終わりとみなさない。まだ正本 DB を開いているかも
    // しれないので、終わりは close で知る（終わったことにすると、列の項目が並んで始まる。#22）
    proc.on('error', (e) => {
      if (proc.pid === undefined) done({ archives: null, error: launchErrorDetail(e, program) });
    });
    proc.on('close', (code) => {
      if (killed) {
        // Windows では uv が先に終わっても python が残りうるので、木ごと止め終わるまで「取得中」のまま
        killed.then(() => done({ archives: null, error: tr('archives_error', { detail: tr('archives_timeout') }) }));
        return;
      }
      if (code !== 0) {
        // detector の断り（Error: …）があればそれを、無ければ stderr の末尾を出す
        const lines = err.split('\n').map((l) => l.trimEnd()).filter(Boolean);
        const start = lines.findIndex((l) => l.startsWith('Error:'));
        const detail = (start >= 0 ? lines.slice(start) : lines.slice(-5)).join('\n');
        done({ archives: null, error: tr('archives_error', { detail: detail || tr('exit_code', { code }) }) });
        return;
      }
      try {
        const parsed = parseArchivesJson(out);
        // field: パス欄に入れる値（保存用フォルダの欄の候補に出す。画面で 1 件ずつ問い合わせなくて済むように。#21）
        parsed.archives = parsed.archives.map((a) => ({ ...a, field: toFieldValue(a.root) }));
        // かんたん取り込みが重なりを見るのに使う（実行中は画面が一覧を読み直せないので、main も最後の一覧を持つ。#22）
        lastListed = {
          archiveDb: String(form.archiveDb || '').trim(), roots: parsed.archives.map((a) => String(a.root)),
        };
        done({ ...parsed, error: null });
      } catch (e) {
        done({ archives: null, error: tr('archives_error', { detail: e.message }) });
      }
    });
  });
});

ipcMain.handle('run:start', async (_event, form) => {
  // 画面は実行ボタンを押した時点で前の出力を消している（始まらなくても）。前の結果を列に残す必要はもう無い（#22）
  lastOtherRun = null;
  // 確認ダイアログを待っている間も含めて 1 本に絞る（child は spawn 後にしか立たない）
  if (runBusy()) return { started: false, error: tr('busy') };
  starting = true;
  try {
    return await startRun(form);
  } finally {
    starting = false; // spawn できていれば以降は child が「実行中」を表す
    reservedAddKeys = [];
    pumpQuickQueue(); // 始まらなかった（確認で取り消した等）なら、待たせた列の項目を始める（#22）
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

  // 取り込み（add）は、かんたん取り込みの列にある（待ち・実行中の）ものと同じ木なら断る（一時停止中の列を再開すると、
  // 同じ木をもう一度走査・移動してしまう。#22）。dry-run はファイルを動かさないので断らない。求めたキーは実行の素性に
  // 持たせ、かんたん取り込みがこの実行と重なるものを受け付けないのに使う（重なりを確かめてから起動するまでは待たない。列と比べた後に列へ入ることは無い）
  let sourceKeyList = [];
  // 投入元は buildArgs が組んだ引数（`add <保存フォルダ> <投入元> …`）を、detector と同じく RUN_DIR（子プロセスの cwd）
  // 基準で絶対パスにしたもの（実際に走査する場所と食い違わないように）
  const addSource = form.mode === 'add' && !args.includes('--dry-run') ? path.resolve(RUN_DIR, args[2]) : '';
  if (addSource) {
    // 実体の位置も引く（リンク経由の同じ木も重なりと分かるように）。待つのは REALPATH_WAIT_MS まで（つながっていない
    // 場所で実行と列を止めない。間に合わなければ書かれた位置で比べる）
    // 実体の位置を待つ間も、かんたん取り込みが同じ木を受け付けないよう、書かれた位置のキーを予約する
    // （run:start の finally で外す。起動したら activeRun.sourceKeys が引き継ぐ）
    reservedAddKeys = pairKeys(addSource, addSource);
    sourceKeyList = await sourceKeys(addSource, { timeoutMs: REALPATH_WAIT_MS });
    reservedAddKeys = sourceKeyList; // 実体の位置も分かった（起動するまで、両方の形で予約する）
    const queued = quickQueue.activeKeys();
    if (sourceKeyList.some((k) => overlapsAny(queued, k))) return { started: false, error: tr('run_overlaps_queue') };
  }

  // 実行ボタンから始めるときは、押したときに画面に見えていた印（色と文言）を受け取り、組んだ引数の印と同じときだけ
  // 始める（#20。画面の予約の仕組みに頼らず、ここで保証する）。一覧の行から登録を外すときなどは印を渡さない
  if (form.expectEffect && !sameEffect(form.expectEffect, argsEffect(args))) {
    return { started: false, error: tr('run_effect_changed') };
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

  // 登録を外すのも renderer 任せにせず、ここで必ず確認を取る（#11）。場所の表示は renderer から来た値だが、
  // 実際に外すのは引数の ID だけ（コマンドも併記する）
  if (form.mode === 'archives' && args.includes('--forget')) {
    const id = args[args.indexOf('--forget') + 1];
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: 'warning',
      buttons: [tr('forget_confirm_cancel'), tr('forget_confirm_ok')],
      defaultId: 0,
      cancelId: 0,
      message: tr('forget_confirm_message', { id }),
      detail: `${form.forgetRoot ? `${String(form.forgetRoot)}\n\n` : ''}${tr('forget_confirm_detail')}`
        + `\n\n${formatCommand(args, commandBase())}`,
    });
    if (response !== 1) return { started: false, error: null, canceled: true };
  }

  let started;
  try {
    started = launch(args, { mode: form.mode, logDir: form.logDir || '', archiveDb: form.archiveDb || '' });
  } catch (e) {
    // 起動の途中で投げた。待っている列の項目も止める（次も同じ理由で起動できないかもしれない）
    if (pauseQueueAfter({ launchFailed: true }, true)) sendQuickQueue();
    throw e;
  }
  // かんたん取り込みが見る、この実行の投入元の比較キー（画面へは送らない。activeRun にだけ持たせる）
  activeRun.sourceKeys = sourceKeyList;
  return started;
}

/**
 * detector を起動して、出力を画面へ送る（実行ボタンからも、かんたん取り込みの列からも。同時に 1 本だけ）。
 * identity は実行の素性（mode は出力の持ち主のタブ）。onFinish は終わったときに集計を受け取る（列の結果の記録。#22）。
 * spawn が同期で投げたとき（引数の不正など）は、まだ何も変えていないのでそのまま例外を返す。起動した後の準備で
 * 投げたときは、起動したものを止め、何も実行していない状態に戻して例外を返す（終了の通知は出さない。始まったことを
 * 画面に伝えていないため）。
 */
function launch(args, identity, onFinish = null) {
  // 起動時のウィンドウではなく「今のウィンドウ」へ送る。閉じて開き直しても制御を失わないようにする
  const send = sendToWindow;
  const command = formatCommand(args, commandBase());
  stoppedByUser = false;

  // detached: 中断をプロセスグループごと送るため（uv の下の python にも届く）
  // 停止ファイルの場所（実行ごと。Windows の中断で使う。#32）
  const thisStopFile = newStopFile();
  const spawned = spawnDetector(args, { detached: true, extraEnv: { [STOP_FILE_ENV]: thisStopFile } });
  const { program } = spawned;
  child = spawned.proc;
  let finished = false;
  let pump = null;
  try {
    // spawn の後に作る（集計は 1 秒ごとのタイマーを持ち、finish で止める。spawn が例外で抜けると止める人がいない）
    pump = createStreamPump(send, { keepOutput: !onFinish });
    // 実行の素性。開き直した画面（appContext().run）と終了の通知（run:exit）の両方で同じものを渡す。
    // mode は開き直した画面が詳細ログを開くかたたむかを決めるのに使う（#18）。logDir・archiveDb は、実行中に変えた
    // 設定ではなくこの実行のログを開き、この実行の DB の一覧にだけ案内の印を付けるため（#19。画面の欄の値のまま）。
    // id は中断がどの実行へのものかを見分けるため（列の項目は次々に始まるので、前の実行に押した中断で次を止めない。#22）
    runSeq += 1;
    // movesFiles: 組んだ引数で実際にファイルを動かすか（dry-run は動かさない）。中断で列を止めるかに使う
    activeRun = {
      id: runSeq, command, ...identity, startedAt: Date.now(), pump, movesFiles: argsEffect(args).effect === 'files',
      stopFile: thisStopFile,
    };
    const run = activeRun;
    const finish = (payload) => {
      if (finished) return;
      finished = true;
      child = null;
      activeRun = null;
      dropStopFile(thisStopFile);
      clearTimeout(stopKillTimer); // 終わった実行の強制終了の予約は要らない
      stopKillTimer = null;
      const summary = { ...payload, ...pump.finish(), ...identity };
      // 実行で登録が変わりうる（登録を外す・初めての保存フォルダの登録）。main が持つ一覧は捨て、次に読んだものを使う
      lastListed = { archiveDb: null, roots: [] };
      // 開き直した画面が実行を取り戻す前に終わったときも、出力の持ち主とその実行のログ・DB が分かるように（#19）。
      // 出力の末尾・警告の行は列の行に残すためのもの（画面は使わないので送らない）
      const { output: _output, notices: _notices, ...exitPayload } = summary;
      send('run:exit', exitPayload);
      if (onFinish) {
        onFinish(summary);
      } else {
        // 実行ボタンから始めた実行の結果。この後に列の項目が始まると画面の出力が置き換わるので、そのときに列の
        // 完了の行として残す（pumpQuickQueue。#22）
        // 出力の本文（末尾）も残す（状態・履歴などは本文が結果。列の項目の行には持たせない。量を抑える）
        lastOtherRun = { mode: identity.mode, command, result: { ...quickResult(summary), output: summary.output } };
        // ファイルを動かす実行の中断・起動できなかったときは、待っている列の項目も止める（画面に「再開」を出す）
        if (pauseQueueAfter(summary, run.movesFiles)) sendQuickQueue();
      }
      // 列の次の項目へ（#22）。実行ボタンから始めた実行の後も、待たせていた項目を始める
      pumpQuickQueue();
    };
    child.stdout.setEncoding('utf-8');
    child.stderr.setEncoding('utf-8');
    child.stdout.on('data', pump.onStdout);
    child.stderr.on('data', pump.onStderr);

    const proc = spawned.proc;
    proc.on('error', (e) => {
      // 起動した後の error（止めるシグナルを送れなかった等）では、プロセスはまだ動いている。終わりとみなさない
      // （終わったことにすると列の次の項目が始まり、2 本が同時に保存フォルダへ移動する。#22）。終わりは close で知る
      // 起動できていれば（pid がある）、終わり方は必ず届く close で決める（もう終わっていても、close が終了コードと
      // 集計を持ってくる。ここで「起動できなかった」としない）
      if (proc.pid !== undefined) {
        if (finished) return; // 終わった後に届いた（次の実行の出力に混ぜない）
        // 出力と同じ経路に通す（Error: の行として、列の行の警告の行にも残る。次の項目が始まると詳細ログは消える）
        pump.onStderr(`Error: ${String(e.message || e)}\n`);
        return;
      }
      const detail = launchErrorDetail(e, program);
      send('run:log', [{ stream: 'err', text: tr('launch_failed_detail', { detail }) }]);
      // spawn 自体が失敗した場合は close が来ないことがあるので、ここで打ち切る
      // 理由（OS のエラー）も終わり方に入れる（列の行・状態の帯に残る。詳細ログは次の項目が始まると消える。#22）
      finish({
        code: null,
        signal: null,
        stoppedByUser: false,
        launchFailed: true, // 起動できなかった（列はここで止める）
        meaning: { level: 'error', key: 'launch_failed_detail', vars: { detail } },
      });
    });

    child.on('close', (code, signal) => {
      // 中断を受け止めた detector も終了コード 0 で終わるので、終了コードでは見分けない。Windows の停止ファイルが
      // 残っていれば、detector は受け取らずに（受け取るときに消す。見張りの外で作られた）自分で終わったので中断ではない
      // （終わった・断った実行を「中断」と記録しない・列を止めない。#32）。応えずに強制終了したときは中断のまま
      let stopFileLeft = false;
      try { stopFileLeft = fs.existsSync(thisStopFile); } catch { /* 分からなければ頼んだとおりに扱う */ }
      finish({
        code,
        signal,
        stoppedByUser: stoppedByUser && !(stopFileLeft && !run.forceKilled),
        meaning: EXIT_MEANINGS[code] || (signal
          ? { level: 'error', key: 'exit_signal', vars: { signal } }
          : { level: 'error', key: 'exit_code', vars: { code } }),
      });
    });
  } catch (e) {
    finished = true;
    // 後から届く error を受け止める（受け手が無いと main プロセスが落ちる）。集計のタイマーも止め、ためた出力は捨てる
    // （始まったことを画面に伝えていない実行。送ると、画面に出ている別の実行の出力に混ざる）
    spawned.proc.on('error', () => {});
    if (pump) pump.discard();
    activeRun = null;
    dropStopFile(thisStopFile);
    clearTimeout(stopKillTimer); // 下で止めるので、停止ファイルの後の強制終了の予約は要らない（#32）
    stopKillTimer = null;
    // 起動していたら、止め終わる（close）まで実行中のままにする（その間に列の次の項目や別の実行を始めない。
    // Windows では uv の下の python が残りうる。#22）。起動できていなければ（pid が無い）すぐ下ろす
    const proc = spawned.proc;
    if (proc.pid === undefined) {
      child = null;
    } else {
      proc.once('close', () => {
        if (child === proc) child = null;
        pumpQuickQueue();
      });
      // 止めるまでに届く出力は画面へ送らない（始まったことを画面に伝えていない実行）
      proc.stdout.removeAllListeners('data');
      proc.stderr.removeAllListeners('data');
      killGroup(proc, 'SIGKILL');
    }
    throw e;
  }

  return { started: true, error: null, command, startedAt: activeRun.startedAt, runId: activeRun.id };
}

// --- かんたん取り込み（#22） -------------------------------------------------

/** 列の中身（画面に要るものだけ）と、一時停止しているか。 */
function quickQueueView() {
  // mode・command は他のタブの実行の結果の行だけが持つ（addDone）
  const items = quickQueue.list().map((i) => ({
    id: i.id, source: i.source, status: i.status, result: i.result, mode: i.mode || null, command: i.command,
    runId: i.runId ?? null,
  }));
  return { items, paused: quickQueue.isPaused() };
}

function sendQuickQueue() {
  sendToWindow('quick:queue', quickQueueView());
}

/** 列の項目の結果（完了した項目から結果を開けるように）。失敗の一覧は送られた分（上限あり）。 */
function quickResult(s) {
  return {
    code: s.code ?? null, stoppedByUser: !!s.stoppedByUser, meaning: s.meaning, tally: s.tally || {},
    // 失敗の一覧は先頭 QUICK_FAILURES 件まで（列の写しは変わるたびに丸ごと送る。全件は CSV にある）
    failureCount: s.failureCount || 0, failures: (s.failures || []).slice(0, QUICK_FAILURES),
    failuresOmitted: (s.failuresOmitted || 0) + Math.max(0, (s.failures || []).length - QUICK_FAILURES),
    csvPath: s.csvPath || null,
    // 警告・エラーの行の数（終了コード 0 でも、つながっていない保存フォルダの Note: などが出る。行の終わり方に出す）
    warnings: s.warnings || 0,
    // 警告・エラーの行（断りの続きを含む。先頭から上限まで）。次の項目が始まると詳細ログが消えるので、行に残す
    notices: s.notices || { lines: [], omitted: 0 },
    // 出力が示す次の操作（adopt が要るフォルダ・登録を外す候補）。詳細ログと一緒に消えるので、行から辿れるように残す
    adoptPath: s.adoptPath || null,
    forgetIds: s.forgetIds || [],
    archiveDb: s.archiveDb || '', // forgetIds はこの正本 DB の ID（保存フォルダ一覧で印を付けるときに照らし合わせる）
  };
}

/**
 * 実行・一覧の読み込みのどれかが動いている（1 本だけにする。どれかが終わったら、待たせた列の項目を pumpQuickQueue で
 * 始める）。
 */
function runBusy() {
  return Boolean(child || starting || listing);
}

/**
 * 終わった実行に応じて、待っている列の項目を止める（止めたら true。待ちが無ければ止めない）。止めるのは、ファイルを
 * 動かす実行（列の項目・実行ボタンの取り込み／重複の後始末。dry-run は除く）を中断したとき（間違いに気づいて止めた。
 * 次の項目が同じ保存先へ動き始めないように。2026-10-12 ユーザー決定）と、起動できなかったとき（次も必ず同じ理由で
 * 起動できない）。detector が断って終わったとき（その投入元だけの理由もある）は止めない。
 */
function pauseQueueAfter(summary, movesFiles) {
  if (!quickQueue.nextWaiting()) return false;
  if (!((summary.stoppedByUser && movesFiles) || summary.launchFailed)) return false;
  quickQueue.pause();
  return true;
}

/** 何も実行していなければ、列の次の項目を始める（実行は 1 本だけ。他のタブの実行・一覧の読み込みが終わるのを待つ）。 */
function pumpQuickQueue() {
  if (quitting || runBusy()) return;
  const next = quickQueue.nextWaiting();
  if (!next) return;
  // 画面に出ている他のタブの実行の結果は、この項目の出力に置き換わる。列の完了の行として残す（項目より前に並ぶ）
  if (lastOtherRun) {
    quickQueue.addDone(lastOtherRun);
    lastOtherRun = null;
  }
  let started;
  try {
    started = launch(next.args, next.identity, (summary) => {
      quickQueue.markDone(next.id, quickResult(summary));
      pauseQueueAfter(summary, true); // 中断・起動できなかったときは、待ちの項目を「再開」まで止める
      sendQuickQueue();
    });
  } catch (e) {
    // 起動の途中で投げた（引数の不正など）。完了として理由を残し、列を一時停止する（次も同じ理由で失敗しうる。
    // 「再開」で続ける。起動していたら、launch が止め終わるのを待つ）
    quickQueue.markDone(next.id, {
      ...quickResult({}), meaning: { level: 'error', key: 'launch_failed_detail', vars: { detail: e.message || String(e) } },
    });
    pauseQueueAfter({ launchFailed: true }, true); // 起動できなかった。待ちの項目は「再開」まで止める
    sendQuickQueue();
    return;
  }
  quickQueue.markRunning(next.id, started.runId); // 行の中断は、この実行の id で頼む（run:stop）
  // 画面は自分で始めていない実行なので、始まったことを知らせる（出力の持ち主は、かんたん取り込みのタブ）。
  // 出力（run:log・run:progress）は子プロセスの出力が届いてから送られるので、これより後に届く
  sendToWindow('run:started', {
    command: started.command, startedAt: started.startedAt, runId: started.runId, ...next.identity,
  });
  sendQuickQueue();
}

/**
 * 実行ボタンから走っている（始めようとしている）取り込み（add）の投入元の比較キー（同じ木を続けて走査・移動しない）。
 * 走っているものは起動するときに activeRun に持たせたもの、始めようとしているものは run:start が予約したもの
 * （dry-run は持たない）。
 */
function runningAddKeys() {
  return [...((activeRun && activeRun.sourceKeys) || []), ...reservedAddKeys];
}

/** 先頭の limit 行と、残りの件数（ダイアログの詳細用）。 */
function listed(lines, limit) {
  const rest = lines.length - limit;
  return [...lines.slice(0, limit), ...(rest > 0 ? [tr('quick_confirm_more', { count: rest })] : [])].join('\n');
}

ipcMain.handle('quick:list', () => quickQueueView());

// 画面が出力を消した（クリア）。前の実行の結果は画面から消えたので、列に残す必要は無い（#22）
ipcMain.handle('run:output-cleared', () => {
  lastOtherRun = null;
  return true;
});

// 一時停止した列を再開する
ipcMain.handle('quick:resume', () => {
  quickQueue.resume();
  sendQuickQueue();
  pumpQuickQueue();
  return true;
});

ipcMain.handle('quick:cancel', (_event, id) => {
  const ok = quickQueue.cancel(Number(id));
  sendQuickQueue();
  return ok;
});

// ドロップされたもの（実パス）を受け付けるか確かめ、確認のダイアログで許可されたら列に入れる。
// コマンドはこの時点の設定で組んで固定する（待っている間に設定を変えても、見せた内容と違う形で実行しない）
ipcMain.handle('quick:submit', async (_event, request = {}) => {
  const form = request.form || {};
  const sources = Array.isArray(request.sources) ? request.sources.map(String) : [];
  // 登録済みの保存フォルダ: 画面が持つ一覧と、main が最後に読めた一覧の両方（どちらかが古くても取りこぼさない）
  const registeredRoots = [...new Set([
    ...(Array.isArray(request.registeredRoots) ? request.registeredRoots.map(String) : []),
    ...(lastListed.archiveDb === String(form.archiveDb || '').trim() ? lastListed.roots : []),
  ])];
  const archiveRoot = String(form.defaultArchiveRoot || '');
  // 断った理由は辞書のキーで返す（画面が今の言語で出し、言語を切り替えたら訳し直す）
  const reasons = (list) => list.map((r) => ({ path: r.path, key: r.key, vars: r.vars }));
  const { accepted, rejected } = await checkSources({
    sources, archiveRoot, registeredRoots, queuedKeys: quickQueue.activeKeys(), runningKeys: runningAddKeys(),
    timeoutMs: REALPATH_WAIT_MS,
    // 中の保存フォルダを探すとき、detector と同じくシンボリックリンクをたどるか（取り込みの既定の設定。#39）
    followSymlinks: Boolean(form.followSymlinks),
  });
  if (!accepted.length) return { queued: 0, rejected: reasons(rejected), error: null, canceled: false };

  // 組めないとき（設定の値が不正など）はダイアログを出さずに断る
  let items;
  try {
    items = accepted.map((a) => {
      const args = buildQuickArgs(form, a.path);
      return {
        source: a.path, key: a.key, keys: a.keys, args, command: formatCommand(args, commandBase()),
        identity: { mode: 'quick', logDir: form.logDir || '', archiveDb: form.archiveDb || '' },
      };
    });
  } catch (e) {
    if (e instanceof FormError) return { queued: 0, rejected: reasons(rejected), error: tr(e.key, e.vars), canceled: false };
    throw e;
  }

  // 確認のダイアログ（「重複の後始末」「登録を外す」と同じ形）。許可したときだけ列に入れる
  const settings = summarizeArgs(items[0].args).map((i) => tr(i.key, i.vars)).join(' · ');
  const skipped = rejected.length ? `\n\n${tr('quick_confirm_skipped', { count: rejected.length })}` : '';
  // 列が一時停止しているなら、許可しても「再開」を押すまで始まらないことを知らせる
  const pausedNote = quickQueue.isPaused() ? `\n\n${tr('quick_confirm_paused')}` : '';
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: 'question',
    buttons: [tr('quick_confirm_cancel'), tr('quick_confirm_ok')],
    // ファイルを動かすので、ほかの確認と同じく既定はキャンセル（うっかり Enter で始めない）
    defaultId: 0,
    cancelId: 0,
    message: tr('quick_confirm_message'),
    detail: `${tr('quick_confirm_sources')}\n${listed(items.map((i) => `  ${i.source}`), QUICK_CONFIRM_SOURCES)}\n\n`
      + `${tr('quick_confirm_archive', { path: archiveRoot })}\n${tr('quick_confirm_settings', { settings })}${skipped}${pausedNote}`
      + `\n\n${listed(items.map((i) => i.command), QUICK_CONFIRM_COMMANDS)}`,
  });
  // 待つ間にアプリの終了が始まった（待ちの列は捨てた）なら入れない
  if (response !== 1 || quitting) return { queued: 0, rejected: reasons(rejected), error: null, canceled: true };

  // ダイアログを待つ間に同じもの・入れ子のものが列に入った（別のドロップ）なら、それは入れない
  const queued = quickQueue.activeKeys();
  const runningKeys = runningAddKeys();
  const reasonOf = (i) => {
    if (i.keys.some((k) => overlapsAny(queued, k))) return 'quick_reject_queued';
    if (i.keys.some((k) => overlapsAny(runningKeys, k))) return 'quick_reject_running';
    return null;
  };
  const judged = items.map((i) => ({ i, reason: reasonOf(i) }));
  const fresh = judged.filter((j) => !j.reason).map((j) => j.i);
  const late = judged.filter((j) => j.reason)
    .map(({ i, reason }) => ({ path: i.source, key: reason, vars: { path: i.source } }));
  quickQueue.add(fresh);
  sendQuickQueue();
  pumpQuickQueue();
  return { queued: fresh.length, rejected: reasons([...rejected, ...late]), error: null, canceled: false };
});

ipcMain.handle('run:stop', (_event, runId) => {
  // 画面が見ていた実行（runId）がもう走っていなければ何も送らない（その間に列の次の項目が始まっていても止めない。#22）
  // 実行の記録が無い（終わって run:exit を送るところ・起動の途中で失敗して止めているところ）なら何も送らない
  if (!activeRun) return null;
  if (runId !== null && runId !== undefined && activeRun.id !== Number(runId)) return 'stale';
  // 子プロセスがもう無い（終わって run:exit を送るところ）ときは interrupt() が null を返す（送れなかった false とは分ける）
  // detector は KeyboardInterrupt を受けて status='interrupted' で後片付けする。送れなかったら中断を頼んだことにしない
  // （終わり方を「中断しました」と出さない・開き直した画面でもう一度押せるように。#19）
  // 1 度だけ送る守りは interrupt() が持つ（画面の状態に頼らない）
  return interrupt();
});
