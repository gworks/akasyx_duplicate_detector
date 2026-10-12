// detector-output.js - detector の出力（英語固定）から UI が拾うもの
//
// 文言の正本は detector 側（ingest.py・dedupe.py・verify.py・archives.py）。変えるときは両方を揃える。
// Note: 行と adopt の案内は detector/tests/test_archives_ui.py が同じ正規表現で実際の出力を確かめている。

// 全体の件数（/30）は数えられたときだけ付く（#18。数えられないときは旧形式のまま）
const PROGRESS_LINE = /^Progress: (\d+)(?:\/(\d+))? files \((.+)\)$/;
// 取り込みの段階・全体の件数・処理中のファイル・失敗（#18。ingest.py が stdout に出す）。パスを含む行は JSON
// 段階は scan → ingest → cleanup → done。次の段階が出たら前の段階は最後まで済んでいる（detector が保証する）
const STAGE_LINE = /^Stage: (scan|ingest|cleanup|done)$/;
const TOTAL_LINE = /^Total: (\d+) files$/;
const CURRENT_LINE = /^Current file: (\{.*\})$/;
const FAILED_LINE = /^Failed: (\{.*\})$/;
// crawler の出力の終わり（失敗しても出る）。この後の行は crawler のものではない
const CRAWLER_FINISHED_LINE = /^Crawler finished$/;
// 判定のループの終わり（中断しても出る）。この後は処理中のファイルも残り時間も無い
const INGEST_FINISHED_LINE = /^Ingest finished$/;
// crawler（一覧作成）の進捗。全体が分からないときは unknown
const SCAN_PROGRESS_LINE = /^Processing files: (\d+)\/(\d+|unknown)\b/;
// CSV の場所はログの INFO 行（%(asctime)s %(levelname)s %(name)s: …）の形のときだけ拾う。ほかの行（失敗の WARNING など）は
// 任意のファイル名やエラー文を含むので、同じ語が入っていても拾わない。detector の config.setup_logging の書式に依るので、
// detector/tests/test_csv_line_ui.py がこの行から正規表現を読み、各コマンドの実際の行が合うことを確かめている（#26）
const CSV_LINE = /^\d{4}-\d\d-\d\d \S+ INFO \S+: CSV report: (.+)$/;
// つながっていない保存フォルダにしか無い重複（ingest._report_unavailable_owners が stdout に出す行）。
// 同じ文がログ（stderr の WARNING 行）にも出るので、行頭の Note: だけを拾う
const NOTE_FORGET_LINE = /^Note: .*archive folder #(\d+) .*`archives --forget (\d+)`/;
// 中身のあるフォルダを断るときの案内（archives.resolve_archive など）。識別子のあるフォルダの断り
// （「… register the files in it again: adopt …」）は拾わない。対処は --archive-db で使っていた正本 DB を
// 指定することで、adopt を勧めると同じ保存フォルダが 2 つの正本 DB に登録されてしまう
// 断りの文の中の、字下げした行の先頭からの形だけ拾う（ログの行の途中に同じ語があっても拾わない）
const ADOPT_HINT_LINE = /^\s+(?:Register the files in it first|Run adopt again): adopt (.+)$/;

/**
 * 警告・エラーの行か（renderer の logClass で warn / err になる行と同じ）。終わったときに、たたんだ詳細ログを開くかの
 * 判断に使う。main でも数えるのは、ウィンドウを開き直す前に出た警告も数えるため（#18）。
 */
function countsAsWarning(text) {
  return text.startsWith('Note:') || text.startsWith('Error:') || / (?:ERROR|WARNING) /.test(text);
}

// detector のログの行の形（detector/config.py の setup_logging の書式）。Traceback の終わりを見分けるのに使う
const LOG_LINE = /^\d{4}-\d\d-\d\d \S+ [A-Z]+ /;

/**
 * 警告・エラーの行（countsAsWarning）と、断り（Error:）の続きの字下げした行・Traceback を、limit 行まで残す（#22）。
 * かんたん取り込みの列の項目は、次の項目が始まると詳細ログが消えるので、終わった理由をこれで行に残す。
 */
function createNoticeCollector(limit = 20) {
  // 先頭と末尾を半分ずつ残す（先頭は最初の警告、末尾は終わった理由。間を数えて捨てる）
  const headLimit = Math.ceil(limit / 2);
  const tailLimit = limit - headLimit;
  const head = [];
  const tail = [];
  let omitted = 0;
  let inError = false; // 直前が断りの行か、その続き（字下げした行が続く）
  let inTrace = false; // Traceback の途中（stderr の、最後の例外の行まで）
  const keep = (text) => {
    if (head.length < headLimit) {
      head.push(text);
      return;
    }
    tail.push(text);
    if (tail.length > tailLimit) { // 末尾は少ない（limit の半分）ので、先頭を 1 つずつ捨てる
      tail.shift();
      omitted += 1;
    }
  };
  return {
    /**
     * warning: countsAsWarning の結果（呼び出し側で数えているなら渡す。省けばここで判定する）。
     * stream: 'out' | 'err'（Traceback は stderr に出る。途中に混ざった stdout の行で終わりとみなさない）。
     */
    add(text, warning = countsAsWarning(text), stream = 'err') {
      // detector が落ちたときの Traceback は、字下げした行と、最後の例外の行（字下げなし）まで残す
      // 次のログの行（`YYYY-MM-DD 時刻 レベル …`）が来るまでを Traceback とみなす（例外のメッセージが複数行でも切らない）
      if (inTrace && stream !== 'out') {
        if (!LOG_LINE.test(text)) {
          if (text.trim()) keep(text);
          return;
        }
        inTrace = false; // ログの行は下の通常の扱いへ
      }
      if (stream !== 'out' && text.startsWith('Traceback (most recent call last):')) {
        keep(text);
        inTrace = true;
        inError = false;
        return;
      }
      if (warning) {
        keep(text);
        inError = text.startsWith('Error:');
      } else if (inError && /^\s/.test(text)) {
        keep(text);
      } else {
        inError = false;
      }
    },
    snapshot: () => ({ lines: [...head, ...tail], omitted }),
  };
}

/**
 * 出力の末尾 limit 行を残す（古い行から捨て、捨てた行数を数える。#22）。実行ボタンから始めた実行の出力は、列の項目が
 * 始まると画面から消えるので、出力の本文（状態・履歴などは本文が結果）を列の完了の行に残すのに使う。
 */
function createTailCollector(limit = 300) {
  let lines = [];
  let dropped = 0;
  // 切り詰めは上限の 2 倍までためてからまとめて行う（出力の 1 行ごとに配列を写さない）
  return {
    add(text) {
      lines.push(text);
      if (lines.length >= limit * 2) {
        dropped += lines.length - limit;
        lines = lines.slice(-limit);
      }
    },
    snapshot: () => {
      const extra = Math.max(0, lines.length - limit);
      return { lines: lines.slice(extra), omitted: dropped + extra };
    },
  };
}

/** `{...}` を読みます。オブジェクトでなければ null。 */
function parseObject(json) {
  try {
    const value = JSON.parse(json);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

/** 1 行から拾えるものを返します（無ければ空のオブジェクト）。 */
function scanLine(text) {
  const progress = PROGRESS_LINE.exec(text);
  if (progress) {
    return {
      progress: {
        processed: Number(progress[1]),
        total: progress[2] === undefined ? null : Number(progress[2]),
        result: progress[3],
      },
    };
  }
  const stage = STAGE_LINE.exec(text);
  if (stage) return { stage: stage[1] };
  if (CRAWLER_FINISHED_LINE.test(text)) return { crawlerFinished: true };
  if (INGEST_FINISHED_LINE.test(text)) return { ingestFinished: true };
  const total = TOTAL_LINE.exec(text);
  if (total) return { total: Number(total[1]) };
  const current = CURRENT_LINE.exec(text);
  if (current) {
    const value = parseObject(current[1]);
    if (value && typeof value.path === 'string') return { current: value.path };
  }
  const failed = FAILED_LINE.exec(text);
  if (failed) {
    const value = parseObject(failed[1]);
    if (value && typeof value.path === 'string') {
      return { failed: { path: value.path, message: String(value.message ?? '') } };
    }
  }
  const scan = SCAN_PROGRESS_LINE.exec(text);
  if (scan) return { scanProgress: { done: Number(scan[1]), total: scan[2] === 'unknown' ? null : Number(scan[2]) } };
  const csv = CSV_LINE.exec(text);
  if (csv) return { csvPath: csv[1].trim() };
  const note = NOTE_FORGET_LINE.exec(text);
  if (note) return { forgetId: Number(note[2]) };
  const adopt = ADOPT_HINT_LINE.exec(text);
  if (adopt) return { adoptPath: adopt[1].trim() };
  return {};
}

/** `archives --json` の出力を読みます。読めなければ例外（空の一覧として扱わない）。 */
function parseArchivesJson(stdout) {
  const data = JSON.parse(String(stdout));
  if (!data || typeof data !== 'object' || !Array.isArray(data.archives)) {
    throw new Error('unexpected output of `archives --json`');
  }
  // db_exists: false は正本 DB がまだ無い（初回起動など。detector は一覧のために作らない）
  return { masterDb: data.master_db || '', dbExists: data.db_exists !== false, archives: data.archives };
}

// --- 実行中の集計（#18） --------------------------------------------------------

// 失敗の一覧に持つ上限。超えた分は件数だけ数える（全件は CSV にある）
const FAILURE_LIST_LIMIT = 500;
// 残り時間は直近この時間の速さで出す（ネットワーク越しなどで速さが変わっても、古い速さを引きずらない）
const ETA_WINDOW_MS = 10 * 60 * 1000;
// これより材料が少ないうちは残り時間を出さない（1 件目の時間だけで何時間と出すと外れる）
const ETA_MIN_FILES = 2;
const ETA_MIN_MS = 10 * 1000;
// 残り時間用の点はこれより細かく取らない（速い取り込みで点が際限なく増えないように。窓 10 分で 600 点まで）
const ETA_SAMPLE_INTERVAL_MS = 1000;

/**
 * 実行中の出力から、進み具合の表示に使うものを集計します。
 * apply(scanLine の結果) は進み具合の行なら true（ログ窓には出さない）を返す。
 */
function createRunState({ now = Date.now } = {}) {
  let stage = null;          // 'scan' | 'ingest' | 'cleanup' | 'done' | null（段階を出さないコマンド）
  const stagesSeen = [];     // 通った段階（単一ファイルは scan を通らない）
  let total = null;          // 処理する件数（分からなければ null）
  let processed = 0;
  let tally = {};
  let current = null;        // 処理中のファイル
  let crawlerRunning = false; // 一覧作成の段階で crawler が動いている間（この間の stdout は crawler のもの）
  let ingestEnded = false;   // 判定のループを抜けた（中断を含む）
  let scan = null;           // crawler の進み具合 { done, total }（取り込みの一覧作成の段階だけ）
  const failures = [];
  let failuresOmitted = 0;
  let samples = [];          // [時刻, 処理済みの件数]（残り時間用）
  let lastAt = 0;            // 最後に 1 件終わった（または判定を始めた）時刻

  const sample = () => {
    const t = now();
    lastAt = t;
    // 最後の点は「今」を表す仮の点。仮の点が確定した点（その 1 つ前）から 1 秒以上離れていれば確定にして新しい
    // 仮の点を足し、離れていなければ仮の点を置き換える（確定した点どうしは必ず 1 秒以上離れる）
    const committed = samples[samples.length - 2];
    const provisional = samples[samples.length - 1];
    if (committed && provisional[0] - committed[0] < ETA_SAMPLE_INTERVAL_MS) samples[samples.length - 1] = [t, processed];
    else samples.push([t, processed]);
    // 窓より前の点は 1 つだけ残す（窓の始まりの基準にする）
    while (samples.length > 2 && samples[1][0] <= t - ETA_WINDOW_MS) samples.shift();
  };

  /** 残り時間（秒）。送るたびに今の時刻で計算し直す（1 件に時間がかかっている間も古い値のまま止めない）。 */
  const etaSeconds = () => {
    if (total === null) return null;
    if (processed >= total) return 0;
    if (ingestEnded) return null; // 中断した（もう進まない）
    if (samples.length < 2) return null;
    const [t1, p1] = samples[samples.length - 1];
    // 基準は窓の直前の点。ただしそれが窓より前にあり（長くかかった 1 件の後など）、窓の中の点だけで
    // 足りるなら窓の中の最初の点を使う（窓の外の遅さ・速さを引きずらない）
    let base = samples[0];
    const inWindow = samples[1];
    if (samples.length >= 3 && base[0] < t1 - ETA_WINDOW_MS
      && p1 - inWindow[1] >= ETA_MIN_FILES && t1 - inWindow[0] >= ETA_MIN_MS) {
      base = inWindow;
    }
    const [t0, p0] = base;
    const files = p1 - p0;
    const ms = t1 - t0;
    if (files < ETA_MIN_FILES || ms < ETA_MIN_MS) return null;
    const perFile = ms / files;
    const remaining = total - processed;
    const elapsed = now() - lastAt; // 処理中のファイルにかかっている時間
    // 最後の 1 件が平均より長くかかっているときは、下限（残りのファイルぶん）が 0 になり「もうすぐ」と出てしまうので、
    // 計算中に戻す（大きい最後の 1 件の終わりは読めない）
    if (remaining === 1 && elapsed >= perFile) return null;
    // 処理中のファイルにかかっている時間を引く。ただし、それより後のファイルぶんより小さくはしない
    // （止まっているように遅いときに、0 に向かって減り続けて「もうすぐ終わる」と見せない）
    const left = Math.max(remaining * perFile - elapsed, (remaining - 1) * perFile);
    return Math.round(left / 1000);
  };

  return {
    apply(found) {
      // crawler が動いている間は、crawler の進捗と、crawler の終わり・段階の行（どちらも detector だけが出す）だけを読む
      // （crawler の行が detector の件数・進捗の行と同じ形でも、detector の状態と取り違えない）
      if (crawlerRunning && !found.scanProgress && !found.crawlerFinished && !found.stage) return false;
      if (found.progress) {
        processed = found.progress.processed;
        if (found.progress.total !== null) total = found.progress.total;
        const { result } = found.progress;
        tally[result] = (tally[result] || 0) + 1; // 外へ渡す写しは snapshot() が作る
        sample();
        return true;
      }
      if (found.stage) {
        stage = found.stage;
        if (!stagesSeen.includes(stage)) stagesSeen.push(stage);
        crawlerRunning = stage === 'scan';
        if (stage === 'cleanup' || stage === 'done') current = null;
        return true;
      }
      if (found.crawlerFinished) {
        crawlerRunning = false;
        return true;
      }
      if (found.ingestFinished) {
        ingestEnded = true;
        current = null;
        return true;
      }
      if (found.total !== undefined) {
        total = found.total;
        // 処理を始める直前に出る行なので、ここを残り時間の起点（0 件の点）にする。判定の準備にかかった時間は入れず、
        // 1 件目にかかった時間は入れる。出ないとき（数えられなかった）は全体が分からず、残り時間も出さない
        if (stage === 'ingest') {
          samples = [];
          sample();
        }
        return true;
      }
      if (found.current !== undefined) {
        current = found.current;
        // 1 件目を取り出したところを残り時間の起点にし直す（一覧の読み出し・並べ替えの時間を 1 件目に入れない）
        if (stage === 'ingest' && processed === 0) {
          samples = [];
          sample();
        }
        return true;
      }
      if (found.failed) {
        if (failures.length < FAILURE_LIST_LIMIT) failures.push(found.failed);
        else failuresOmitted += 1;
        return true;
      }
      // crawler の進捗は、取り込みの一覧作成の段階でだけ集計する。段階を出さないコマンド（verify・adopt）では
      // crawler の後にも長い処理が続き、100% のカードが完了に見えるので、今までどおりログに出す
      if (found.scanProgress && crawlerRunning) {
        scan = { ...found.scanProgress };
        return true;
      }
      return false;
    },
    /** 取り込みの一覧作成の段階で crawler が動いている間か（この間に stdout に出た行は crawler のもの）。 */
    inScan: () => crawlerRunning,
    /** 時間がたつだけで表示（残り時間）が変わるか。 */
    ticking: () => stage === 'ingest' && !ingestEnded && total !== null && processed < total,
    sampleCount: () => samples.length,
    /** 失敗の一覧に持っている件数（上限に達したら増えない。一覧を送り直すかの判断に使う）。 */
    failureListSize: () => failures.length,
    /**
     * 今の集計の写し（renderer へ送る・ウィンドウを開き直したときの初期表示）。
     * failures: false なら失敗の一覧は入れない（変わっていないときに毎回まるごと送らない。件数と上限を超えた件数は入る）。
     * 一覧を入れるときは、いつもまるごと入れる（受け手は届いた一覧で置き換えるだけで、食い違いが残らない。上限 500 件）。
     */
    snapshot({ failures: withFailures = true } = {}) {
      const snap = {
        stage, stagesSeen: [...stagesSeen], ingestEnded, total, processed, tally: { ...tally }, current,
        scan: scan && { ...scan }, failureCount: failures.length + failuresOmitted, failuresOmitted,
        etaSeconds: etaSeconds(),
      };
      if (withFailures) snap.failures = failures.map((f) => ({ ...f }));
      return snap;
    },
  };
}

module.exports = {
  scanLine, parseArchivesJson, createRunState, countsAsWarning, createNoticeCollector, createTailCollector,
  FAILURE_LIST_LIMIT,
};
