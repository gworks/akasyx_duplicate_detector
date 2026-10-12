// quick-queue.js - かんたん取り込み（#22）: ドロップされたものを受け付けるかの判定と、順番待ちの列。main だけが使う
//
// ドロップしたものは確認のダイアログで許可したときだけ列に入り、1 つずつ add で取り込む。コマンドは許可した時点の
// 設定で組んで列に固定する（待っている間に設定を変えても、ダイアログで見せた内容と違う形で実行しない）。
// 受け付けないもの（保存フォルダそのもの・その中・それを含むフォルダ・列にすでにあるもの）は、ダイアログの前に断る
// （detector も PreflightError で断るが、確認してから断られる流れにはしない）。保存フォルダの中にある保存フォルダ
// （登録されていないもの）までは歩いて探さない（大きなフォルダで待たせない）。それは detector が走査の前に断る。
const path = require('node:path');
const fs = require('node:fs');

const { buildArgs, toFieldValue } = require('./commands');

// 取り込みに使う設定の欄（設定画面の取り込みの既定と、システムの設定）。取り込みタブの保存用フォルダ・投入元・
// dry-run は使わない（かんたん取り込みは既定の保存フォルダへ、実際に取り込む）
const ADD_FIELDS = ['folderLimit', 'minSize', 'withMeta', 'followSymlinks', 'pruneEmptyDirs'];
const COMMON_FIELDS = ['archiveDb', 'crawlerRepo', 'dbDir', 'logDir'];

// fs は非同期で問い合わせる（つながっていないネットワークの場所でも、main プロセス（画面・実行中の出力）を止めない。#7）
const realFs = {
  exists: (p) => fs.promises.access(p).then(() => true, () => false),
  realpath: (p) => fs.promises.realpath(p),
  // detector/archives.py の has_archive_marker と同じ目印（v0.2 の archive.id と、まだ開いていない v0.1.x の archive.db）
  hasMarker: async (dir) => {
    for (const name of ['archive.id', 'archive.db']) {
      try {
        if ((await fs.promises.stat(path.join(dir, '.akasyx', name))).isFile()) return true;
      } catch {
        // 無い・読めない
      }
    }
    return false;
  },
};

/**
 * 絶対パスにし、real なら実体の位置にする（シンボリックリンクを解く。まだ無い・読めないときは書かれた位置）。
 * real=false は書かれた位置のまま（つながっていないネットワークの場所に問い合わせない。#7）。timeoutMs を渡すと、
 * それまでに実体の位置が分からなければ書かれた位置にする（待ち続けない）。
 */
async function resolvePath(p, { pathApi = path, fsx = realFs, real = true, timeoutMs = 0 } = {}) {
  const resolved = pathApi.resolve(String(p));
  if (!real) return resolved;
  const found = await askWithin(() => fsx.realpath(resolved), timeoutMs, null);
  return found ? pathApi.resolve(found) : resolved;
}

// 落としたものを確かめるときに、同時に問い合わせる件数（libuv の fs のスレッドの既定の数と同じ）
const QUERY_CONCURRENCY = 4;

/** list の各要素に fn を、同時に limit 件までで適用する（結果は list の順）。 */
async function mapLimit(list, limit, fn) {
  const results = new Array(list.length);
  let next = 0;
  const worker = async () => {
    while (next < list.length) {
      const i = next;
      next += 1;
      results[i] = await fn(list[i]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, list.length) }, worker));
  return results;
}

// 問い合わせが timeoutMs までに返らなかった印（askWithin の fallback に使う）
const NO_ANSWER = Symbol('no answer');
// 実体の位置を引けず、その理由が「無い」ではなかった印（権限が無いなど）
const UNREADABLE = Symbol('unreadable');

/**
 * ask() の答え。投げたときは fallback、timeoutMs（0 は待ち続ける）までに返らないときは onTimeout（省けば fallback。
 * つながっていない場所の問い合わせを待ち続けない。#7）。
 */
async function askWithin(ask, timeoutMs, fallback, onTimeout = fallback) {
  let timer = null;
  try {
    const answer = Promise.resolve().then(ask);
    if (!(timeoutMs > 0)) return await answer;
    const late = new Promise((resolve) => { timer = setTimeout(() => resolve(onTimeout), timeoutMs); });
    return await Promise.race([answer, late]);
  } catch {
    return fallback;
  } finally {
    clearTimeout(timer);
  }
}

/** 比較キー: macOS・Windows は大文字小文字違いも同じとみなす（区別するボリュームでは、取りこぼしより入れ子の見逃しが危険なので安全側）。 */
function keyOf(resolved, platform = process.platform) {
  // macOS は Unicode の表記ゆれ（NFD で来る濁点など）もそろえる（ファイルシステムが同じ名前とみなす）
  if (platform === 'darwin') return resolved.normalize('NFC').toLowerCase();
  return platform === 'win32' ? resolved.toLowerCase() : resolved;
}

/**
 * 実体の位置 real と書かれた位置 p の比較キー（同じなら 1 つ。先頭が実体の位置）。割り当てたドライブなどで実体の
 * 表記が変わっても、どちらかで重なりを見つけられるように、重なりの判定は両方で行う。
 */
function pairKeys(real, p, { platform = process.platform, pathApi = path } = {}) {
  return [...new Set([keyOf(real, platform), keyOf(pathApi.resolve(String(p)), platform)])];
}

/**
 * パスを突き合わせる比較キー（detector/utl/helpers.py の path_key と同じ考え方）: 実体の位置と書かれた位置（pairKeys）。
 */
async function sourceKeys(p, { platform = process.platform, pathApi = path, fsx = realFs, timeoutMs = 0, real = true } = {}) {
  return pairKeys(await resolvePath(p, { pathApi, fsx, timeoutMs, real }), p, { platform, pathApi });
}

/** 比較キー同士で、inner が outer の配下（または同一）か。区切りで見る（/a/bc は /a/b の中ではない）。 */
function keyWithin(outer, inner, sep = path.sep) {
  if (inner === outer) return true;
  return inner.startsWith(outer.endsWith(sep) ? outer : outer + sep);
}

/** inners のどれかが outers のどれかの配下（または同一）か（一方向）。 */
function anyWithin(outers, inners, sep = path.sep) {
  return outers.some((o) => inners.some((k) => keyWithin(o, k, sep)));
}

/** key が keys のどれかと同じか入れ子（どちら向きでも）か。 */
function overlapsAny(keys, key, sep = path.sep) {
  return anyWithin(keys, [key], sep) || anyWithin([key], keys, sep);
}

/**
 * 実体の位置 dir 自身か、その上にある保存フォルダの目印のあるフォルダ（無ければ null）。cache はフォルダごとの
 * 目印の有無の問い合わせ（Promise。同じフォルダから多く落としても、並行に調べても、上の階層を 1 度だけ問い合わせる）。
 * stopped() が true になったら、それ以上の階層は問い合わせない（呼び出し側が待つのをやめたとき）。
 */
async function enclosingMarker(dir, { pathApi, fsx, timeoutMs = 0 }, cache = new Map(), stopped = () => false) {
  for (let d = dir; ; d = pathApi.dirname(d)) {
    if (stopped()) return null; // 呼び出し側が待つのをやめた（上の階層へ問い合わせ続けない）
    // 返らない問い合わせは「目印なし」とみなす（detector が実行の前に確かめ直す）
    if (!cache.has(d)) cache.set(d, askWithin(() => fsx.hasMarker(d), timeoutMs, false));
    if (await cache.get(d)) return d;
    if (pathApi.dirname(d) === d) return null;
  }
}

/**
 * ドロップされたものを受け付けるか決める。
 * @param {object} o
 * @param {string[]} o.sources ドロップされた実パス
 * @param {string} o.archiveRoot 既定の保存フォルダ（detector が返した実パス。空なら全部断る）
 * @param {string[]} o.registeredRoots 登録済みの保存フォルダ（detector が返した実パス。分かっている分だけ）。
 *   つながっていない場所かもしれないので、書かれた位置のまま比べる（realpath しない）
 * @param {string[]} o.queuedKeys 列にある（待ち・実行中の）ものの比較キー（実体の位置と書かれた位置の両方）
 * @param {string[]} o.runningKeys 実行ボタンから走っている（始めようとしている）取り込みの投入元の比較キー
 * @returns {Promise<{accepted: {path: string, key: string, keys: string[]}[], rejected: {path: string, key: string, vars: object}[]}>}
 *   accepted の keys は実体の位置と書かれた位置の比較キー（列に入れたら重複の判定に使う）
 *   rejected の key は理由の辞書のキー
 */
async function checkSources({
  sources, archiveRoot, registeredRoots = [], queuedKeys = [], runningKeys = [], platform = process.platform,
  pathApi = path, fsx = realFs, timeoutMs = 0,
}) {
  // timeoutMs: 1 回の問い合わせを待つ上限（返らなければ、実体の位置は書かれた位置・存在は「応答がない」で断る・目印は無し）
  const opts = { platform, pathApi, fsx, timeoutMs };
  const accepted = [];
  const rejected = [];
  const reject = (i, p, key, vars = {}) => rejected.push({ i, path: p, key, vars: { path: p, ...vars } });
  const given = sources.map((s, i) => ({ i, p: String(s ?? '') })).filter((e) => e.p);
  // 保存先は detector が返した実パス（絶対パス）。そうでなければ決まっていないとみなす（相対パスは、ここで確かめる場所と
  // detector が実行する場所がずれる）
  if (!archiveRoot || !pathApi.isAbsolute(archiveRoot)) {
    for (const { i, p } of given) reject(i, p, 'quick_reject_no_archive');
    return finish(accepted, rejected);
  }
  // 絶対パスだけを受け付ける（相対パスは、ここで確かめる場所と detector が実行する場所（その作業フォルダ）がずれる）
  const list = given.filter((e) => {
    if (pathApi.isAbsolute(e.p)) return true;
    reject(e.i, e.p, 'quick_reject_not_absolute');
    return false;
  });
  // 保存フォルダ（取り込み先と登録済みのもの）は、detector が返した実パスなので書かれた位置で比べる
  // （つながっていない場所かもしれない。問い合わせて待たない）
  const roots = [archiveRoot, ...registeredRoots].filter(Boolean)
    .map((r) => ({ root: r, key: keyOf(pathApi.resolve(r), platform) }));
  const markers = new Map();
  // 実体の位置・存在・保存フォルダの目印は、落としたものについて QUERY_CONCURRENCY 件ずつ並行に問い合わせる（1 件ずつ
  // 待たない。大量に落としても、OS の問い合わせの順番待ちで時間切れにしない。1 件の中の問い合わせは順に行う）。
  // 保存フォルダとの重なりは、実体の位置と書かれた位置の両方で見る（割り当てたドライブなどで実体の表記が変わっても）。
  // 一緒に落とした入れ子は、落とした順に関わらず外側を取り込む（短いキーから見て、後から外側が来たら先に受け付けた
  // 内側を外す）。結果は落とした順に返す
  const entries = (await mapLimit(list, QUERY_CONCURRENCY, async (e) => {
    // 実体の位置が分かれば、あるものとみなす（もう一度問い合わせない）。返らなければ応答なし（存在も問い合わせない）。
    // 引けなかった（無い・読めない）ときだけ存在を問い合わせる
    const written = pathApi.resolve(e.p);
    // 引けなかった理由が「無い」でなければ（権限・特殊なボリュームなど）、存在を確かめ、あれば書かれた位置で受け付ける。
    // 存在も確かめられなければ、読めないものとして断る（「見つからない」と取り違えない）
    const lookup = async () => {
      try {
        return await fsx.realpath(written);
      } catch (err) {
        return err && (err.code === 'ENOENT' || err.code === 'ENOTDIR') ? null : UNREADABLE;
      }
    };
    const found = await askWithin(lookup, timeoutMs, null, NO_ANSWER);
    const real = typeof found === 'string' ? pathApi.resolve(found) : written;
    let exists = found === NO_ANSWER ? NO_ANSWER : true;
    if (found === null || found === UNREADABLE) {
      exists = await askWithin(() => fsx.exists(e.p), timeoutMs, NO_ANSWER);
      if (found === UNREADABLE && exists === false) exists = UNREADABLE;
    }
    // 目印は実体の位置と書かれた位置の両方の上の階層で探す（保存フォルダの中のリンクが外を指していても見つける）。
    // 存在を確かめられたものだけ。上の階層をたどる全体で timeoutMs まで（返らなければ目印なし。detector が確かめ直す）
    const markerOf = async (dir) => {
      let gaveUp = false;
      try {
        return await askWithin(() => enclosingMarker(dir, opts, markers, () => gaveUp), timeoutMs, null);
      } finally {
        gaveUp = true;
      }
    };
    let marker = null;
    if (exists === true) marker = (await markerOf(real)) || (written === real ? null : await markerOf(written));
    const keys = pairKeys(real, e.p, opts);
    return { ...e, keys, exists, marker, len: Math.min(...keys.map((k) => k.length)) };
  })).sort((a, b) => a.len - b.len);
  for (const { i, p, keys: mine, exists, marker } of entries) {
    const overlap = roots.find((r) => mine.some((k) => overlapsAny([r.key], k, pathApi.sep)));
    if (overlap) {
      reject(i, p, 'quick_reject_archive', { archive: overlap.root });
      continue;
    }
    // 列にあるものと同じか入れ子なら断る（同じ木を 2 回走査・移動しない）。こちらも両方の形で見る
    if (mine.some((k) => overlapsAny(queuedKeys, k, pathApi.sep))) {
      reject(i, p, 'quick_reject_queued');
      continue;
    }
    // 実行ボタンから走っている取り込みと同じ木も断る（終わった直後に同じ木を走査・移動しない）
    if (mine.some((k) => overlapsAny(runningKeys, k, pathApi.sep))) {
      reject(i, p, 'quick_reject_running');
      continue;
    }
    // 一緒に落とした（外側の）ものと同じ・その中にある（そちらと一緒に取り込まれる）。両方の形のキーで見る
    const outer = accepted.find((a) => anyWithin(a.keys, mine, pathApi.sep));
    if (outer) {
      const same = outer.keys.some((ak) => mine.includes(ak));
      reject(i, p, same ? 'quick_reject_duplicate' : 'quick_reject_nested', { other: outer.path });
      continue;
    }
    // 返らなかった（つながっていない・遅い場所）は「見つからない」と言わない
    if (exists === NO_ANSWER) {
      reject(i, p, 'quick_reject_unreachable');
      continue;
    }
    if (exists === UNREADABLE) {
      reject(i, p, 'quick_reject_unreadable');
      continue;
    }
    if (!exists) {
      reject(i, p, 'quick_reject_missing');
      continue;
    }
    if (marker) {
      reject(i, p, 'quick_reject_in_archive', { archive: marker });
      continue;
    }
    // 先に受け付けたものがこの中にあれば（実体の位置の長さで外側が後から来た）、それを外してこちらを取り込む
    for (const inner of accepted.filter((a) => anyWithin(mine, a.keys, pathApi.sep))) {
      accepted.splice(accepted.indexOf(inner), 1);
      reject(inner.i, inner.path, 'quick_reject_nested', { other: p });
      // その内側と一緒に取り込むはずだった（同じ・その中）と断ったものも、こちらと一緒に取り込まれる
      for (const r of rejected) {
        if (r.vars.other === inner.path && (r.key === 'quick_reject_duplicate' || r.key === 'quick_reject_nested')) {
          r.key = 'quick_reject_nested';
          r.vars.other = p;
        }
      }
    }
    accepted.push({ i, path: p, key: mine[0], keys: mine });
  }
  return finish(accepted, rejected);
}

/** 落とした順に並べ直して返す。 */
function finish(accepted, rejected) {
  const byDrop = (a, b) => a.i - b.i;
  return {
    accepted: accepted.sort(byDrop).map(({ path: p, key, keys }) => ({ path: p, key, keys })),
    rejected: rejected.sort(byDrop).map(({ i, ...rest }) => rest),
  };
}

/** 画面のフォームから、取り込みに使う欄だけを取り出して add のフォームにする。 */
function addForm(form, archiveRoot, sourcePath) {
  const f = { mode: 'add', archiveRoot, sourcePath, dryRun: false };
  for (const name of [...ADD_FIELDS, ...COMMON_FIELDS]) f[name] = form[name];
  return f;
}

/**
 * かんたん取り込み 1 件のコマンドの引数（既定の保存フォルダへの add）。保存フォルダと投入元は確かめた実パスのまま入れる
 * （verbatimPaths。パス欄の正規化は前後の空白を落とすので、名前の前後に空白があると別のパスになる）。
 */
function buildQuickArgs(form, source) {
  return buildArgs(addForm(form, String(form.defaultArchiveRoot ?? ''), String(source)), { verbatimPaths: true });
}

/** プレビュー（今の既定の 1 行）用の add のフォーム。投入元は空（previewDefaults が仮の値を入れる）。 */
function quickPreviewForm(form) {
  const root = form.defaultArchiveRoot ? toFieldValue(form.defaultArchiveRoot) : '';
  return addForm(form, root, '');
}

/**
 * 順番待ちの列。項目: { id, source, key, args, command, status: waiting|running|done, result }。
 * 完了したものは結果を見られるように残すが、keepDone 件を超えたら古いものから捨てる。
 */
function createQueue({ keepDone = 20 } = {}) {
  let items = [];
  let nextId = 1;
  // 一時停止（中断したとき）。止めている間は次の項目を出さない。止めるのは待ちの項目があるときだけで、
  // 待ちが無くなったら解ける（その後に入れたものは止めない）
  let paused = false;
  const hasWaiting = () => items.some((i) => i.status === 'waiting');
  const find = (id) => items.find((i) => i.id === id);
  // 完了の記録は keepDone 件まで（古いものから捨てる。待ち・実行中は捨てない）
  const trimDone = () => {
    const done = items.filter((x) => x.status === 'done');
    const drop = new Set(done.slice(0, Math.max(0, done.length - keepDone)));
    items = items.filter((x) => !drop.has(x));
  };
  return {
    add(list) {
      if (!hasWaiting()) paused = false; // 待ちが無いまま止まっていた（取り消し尽くした等）なら、新しいものは止めない
      const created = list.map((i) => ({ ...i, id: nextId++, status: 'waiting', result: null }));
      items.push(...created);
      return created;
    },
    nextWaiting: () => (paused ? null : items.find((i) => i.status === 'waiting') || null),
    pause() {
      paused = hasWaiting();
    },
    resume() {
      paused = false;
    },
    isPaused: () => paused && hasWaiting(),
    /** 実行中にする。runId はその実行の id（行の中断は、この id で頼む）。 */
    markRunning(id, runId = null) {
      const i = find(id);
      if (!i) return;
      i.status = 'running';
      i.runId = runId;
    },
    markDone(id, result) {
      const i = find(id);
      if (!i) return;
      i.status = 'done';
      i.result = result;
      trimDone();
    },
    /**
     * 他のタブの実行の結果を完了の行として足す（列の次の項目が始まると、出力の表示が置き換わるため。
     * mode は実行したタブ。source・key は持たないので、重複の判定には使わない）。
     */
    addDone({ mode, command, result }) {
      const done = { id: nextId++, source: '', key: null, mode, command, status: 'done', result };
      // 終わった順に並ぶよう、待ち・実行中の項目より前に入れる
      const at = items.findIndex((x) => x.status !== 'done');
      items.splice(at < 0 ? items.length : at, 0, done);
      trimDone();
      return { ...done };
    },
    /** 待ちの項目だけ取り消せる。取り消したら true。 */
    cancel(id) {
      const i = find(id);
      if (!i || i.status !== 'waiting') return false;
      items = items.filter((x) => x !== i);
      if (!hasWaiting()) paused = false;
      return true;
    },
    // 重複の判定に使うキー（項目が両方の形を持っていれば両方）
    activeKeys: () => items.filter((i) => i.status !== 'done').flatMap((i) => i.keys || [i.key]),
    discardWaiting() {
      items = items.filter((i) => i.status !== 'waiting');
      paused = false;
    },
    list: () => items.map((i) => ({ ...i })),
  };
}

module.exports = {
  pairKeys, sourceKeys, keyWithin, overlapsAny, checkSources, createQueue, buildQuickArgs, quickPreviewForm,
};
