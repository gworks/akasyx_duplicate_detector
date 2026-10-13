// quick-queue.js - かんたん取り込み（#22）: ドロップされたものを受け付けるかの判定と、順番待ちの列。main だけが使う
//
// ドロップしたものは確認のダイアログで許可したときだけ列に入り、1 つずつ add で取り込む。コマンドは許可した時点の
// 設定で組んで列に固定する（待っている間に設定を変えても、ダイアログで見せた内容と違う形で実行しない）。
// 受け付けないもの（保存フォルダそのもの・その中・それを含むフォルダ・列にすでにあるもの）は、ダイアログの前に断る
// （detector も PreflightError で断るが、確認してから断られる流れにはしない）。落としたフォルダの中にある保存フォルダ
// （登録されていないもの）も、時間とフォルダ数の上限の中で探して断る（#39）。探しきれなかったときは受け付け、
// detector が走査の前に断る（大きなフォルダでダイアログを待たせない）。
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
  // ファイル（リンクはたどる。detector の os.path.isdir と同じ向き）か。読めなければ false
  isFile: (p) => fs.promises.stat(p).then((st) => st.isFile(), () => false),
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
  /**
   * dir の下の階層にある保存フォルダ（目印のあるフォルダ。dir 自身は含めない）を 1 つ探す（detector/archives.py の
   * archives_below と同じく `.akasyx/` の中には入らず、読めないフォルダは飛ばす）。walkLinks のときはリンク先の
   * フォルダにも入る（循環しても、deadline（時刻）か maxDirs（読むフォルダの数）で必ず止まる。inode で見張らないのは、
   * inode が 0 になる共有で全部を「読んだ」と取り違えないため）。上限に達したら打ち切る。
   * 戻り値 { found: パス|null, gaveUp: 打ち切った（読みきれていない）}
   */
  markerBelow: async (dir, { deadline = Infinity, maxDirs = Infinity, walkLinks = false } = {}) => {
    const queue = [dir];
    let truncated = false; // maxDirs を超えて積まなかったフォルダがある
    for (let next = 0; next < queue.length; next += 1) {
      if (Date.now() > deadline) return { found: null, gaveUp: true };
      const d = queue[next];
      let entries;
      try {
        // readdir で読む（opendir の 1 件ずつの読み取りは、種類を返さない共有で同期の lstat になり main を止める）
        entries = await fs.promises.readdir(d, { withFileTypes: true });
      } catch {
        continue; // ファイル・読めないフォルダ（detector の走査も読めない）
      }
      for (const e of entries) {
        if (Date.now() > deadline) return { found: null, gaveUp: true };
        if (e.name === '.akasyx') {
          // 目印は種類に関わらず見る（`.akasyx` 自体がリンクでも、detector の has_archive_marker はたどって見る）。中には入らない
          // 落としたフォルダ自身の目印は見ない（上の階層として別に見ている）。目印の問い合わせも期限まで（つながって
          // いない共有で返らなくても、全体の期限を使い切らない。期限が無ければ待つ）
          if (d === dir) continue;
          const wait = Number.isFinite(deadline) ? Math.max(1, deadline - Date.now()) : 0;
          if (await askWithin(() => realFs.hasMarker(d), wait, false)) return { found: d, gaveUp: false };
          continue;
        }
        if (queue.length >= maxDirs) {
          // もう積まない（読むのは maxDirs 件まで）。このフォルダの残りは `.akasyx` の名前だけを見る（リンクを stat しない）
          if (e.isDirectory() || e.isSymbolicLink()) truncated = true;
          continue;
        }
        let isDir = e.isDirectory(); // シンボリックリンク・ジャンクションは false
        if (!isDir && walkLinks && e.isSymbolicLink()) {
          if (Date.now() > deadline) return { found: null, gaveUp: true };
          try {
            isDir = (await fs.promises.stat(path.join(d, e.name))).isDirectory();
          } catch {
            isDir = false;
          }
        }
        if (isDir) queue.push(path.join(d, e.name));
      }
    }
    return { found: null, gaveUp: truncated };
  },
};

// 落としたフォルダの中を探す上限（時間は 1 回のドロップ全体で、受け付けると決まったものを探す間。フォルダ数は 1 件
// あたり）。超えたら受け付け、detector が走査の前に確かめる（#39）
const BELOW_SCAN_MS = 1500;
const BELOW_SCAN_DIRS = 5000;

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
 * @returns {Promise<{accepted: {path: string, key: string, keys: string[], file: boolean}[], rejected: {path: string, key: string, vars: object}[]}>}
 *   accepted の keys は実体の位置と書かれた位置の比較キー（列に入れたら重複の判定に使う）。file はファイルか（フォルダ・
 *   分からないものは false）
 *   rejected の key は理由の辞書のキー
 */
async function checkSources({
  sources, archiveRoot, registeredRoots = [], queuedKeys = [], runningKeys = [], platform = process.platform,
  pathApi = path, fsx = realFs, timeoutMs = 0, belowMs = BELOW_SCAN_MS, belowDirs = BELOW_SCAN_DIRS,
  followSymlinks = false,
}) {
  // timeoutMs: 1 回の問い合わせを待つ上限（返らなければ、実体の位置は書かれた位置・存在は「応答がない」で断る・目印は無し）
  const opts = { platform, pathApi, fsx, timeoutMs };
  const accepted = [];
  const rejected = [];
  // 断った記録を list に足す関数（記録の形はここだけで決める）
  const rejectInto = (list) => (i, p, key, vars = {}) => list.push({ i, path: p, key, vars: { path: p, ...vars } });
  const reject = rejectInto(rejected);
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
    // ファイルか（続けて落としたファイルは 1 回の実行にまとめる。#37）。分からなければファイルとしない（1 件ずつ実行する）
    const file = exists === true && typeof fsx.isFile === 'function'
      && (await askWithin(() => fsx.isFile(e.p), timeoutMs, false)) === true;
    const keys = pairKeys(real, e.p, opts);
    return { ...e, real, keys, exists, marker, file, len: Math.min(...keys.map((k) => k.length)) };
  })).sort((a, b) => a.len - b.len);
  // 受け付けるものを決める。below は中を探して見つかった保存フォルダ（落としたものの番号 → その落とし方の形のパス）。中を探した結果が
  // 増えたら、最初から決め直す（外側が断られたら内側が受け付けられる、などの入れ子・重複の規則を 1 か所で守る。#39）
  const decide = (below) => {
    const acc = [];
    const rej = [];
    const no = rejectInto(rej);
    for (const entry of entries) {
      const { i, p, keys: mine, exists, marker } = entry;
      const overlap = roots.find((r) => mine.some((k) => overlapsAny([r.key], k, pathApi.sep)));
      if (overlap) {
        no(i, p, 'quick_reject_archive', { archive: overlap.root });
        continue;
      }
      // 列にあるものと同じか入れ子なら断る（同じ木を 2 回走査・移動しない）。こちらも両方の形で見る
      if (mine.some((k) => overlapsAny(queuedKeys, k, pathApi.sep))) {
        no(i, p, 'quick_reject_queued');
        continue;
      }
      // 実行ボタンから走っている取り込みと同じ木も断る（終わった直後に同じ木を走査・移動しない）
      if (mine.some((k) => overlapsAny(runningKeys, k, pathApi.sep))) {
        no(i, p, 'quick_reject_running');
        continue;
      }
      // 中に保存フォルダがある（#39）。入れ子の判定より先に断る（断ったものの中のものは、それと一緒には取り込まれない）
      if (below.has(i)) {
        no(i, p, 'quick_reject_contains_archive', { archive: below.get(i) });
        continue;
      }
      // 一緒に落とした（外側の）ものと同じ・その中にある（そちらと一緒に取り込まれる）。両方の形のキーで見る
      const outer = acc.find((a) => anyWithin(a.keys, mine, pathApi.sep));
      if (outer) {
        const same = outer.keys.some((ak) => mine.includes(ak));
        no(i, p, same ? 'quick_reject_duplicate' : 'quick_reject_nested', { other: outer.path });
        continue;
      }
      // 返らなかった（つながっていない・遅い場所）は「見つからない」と言わない
      if (exists === NO_ANSWER) {
        no(i, p, 'quick_reject_unreachable');
        continue;
      }
      if (exists === UNREADABLE) {
        no(i, p, 'quick_reject_unreadable');
        continue;
      }
      if (!exists) {
        no(i, p, 'quick_reject_missing');
        continue;
      }
      if (marker) {
        no(i, p, 'quick_reject_in_archive', { archive: marker });
        continue;
      }
      // 先に受け付けたものがこの中にあれば（実体の位置の長さで外側が後から来た）、それを外してこちらを取り込む
      for (const inner of acc.filter((a) => anyWithin(mine, a.keys, pathApi.sep))) {
        acc.splice(acc.indexOf(inner), 1);
        no(inner.i, inner.path, 'quick_reject_nested', { other: p });
        // その内側と一緒に取り込むはずだった（同じ・その中）と断ったものも、こちらと一緒に取り込まれる
        for (const r of rej) {
          if (r.vars.other === inner.path && (r.key === 'quick_reject_duplicate' || r.key === 'quick_reject_nested')) {
            r.key = 'quick_reject_nested';
            r.vars.other = p;
          }
        }
      }
      acc.push({ i, path: p, key: mine[0], keys: mine, real: entry.real, file: entry.file });
    }
    return { acc, rej };
  };

  // 受け付けると決まったフォルダの中にある保存フォルダを探す（#39）。時間はここから全体で belowMs まで（断る・入れ子の
  // ものには使わない。遅い場所の問い合わせで使い切らない）。返らない・打ち切ったものは受け付けたまま（detector が走査の
  // 前に断る）。Windows のジャンクションは、detector（os.walk）がリンクをたどらない設定でも中に入る。Node からは
  // シンボリックリンクと見分けられないので、Windows ではリンク先のフォルダにも入る
  // 見つけた保存フォルダ foundReal と、落としたもの entry の関係（#39）。内か外かは比較キー（大文字小文字・表記ゆれを
  // そろえたもの）で、entry の実体の位置と落としたパスの位置の両方で見る（リンク・割り当てたドライブで落としたもの）。
  // 戻り値: 'archive'（entry 自身かその上が保存フォルダ）/ 落としたパスの形での場所（entry の中にある。作れなければ
  // 見つけた場所のまま）/ null（関係ない）
  const relation = (entry, foundReal) => {
    const fk = keyOf(pathApi.resolve(foundReal), platform);
    for (const base of [entry.real, pathApi.resolve(entry.p)]) {
      const bk = keyOf(base, platform);
      if (keyWithin(fk, bk, pathApi.sep)) return 'archive';
      if (!keyWithin(bk, fk, pathApi.sep)) continue;
      const rel = pathApi.relative(base, foundReal);
      const outside = !rel || rel === '..' || rel.startsWith(`..${pathApi.sep}`) || pathApi.isAbsolute(rel);
      return outside ? foundReal : pathApi.join(entry.p, rel);
    }
    return null;
  };
  const below = new Map();
  // 分かった保存フォルダ found を、落としたもの全部に割り当てる。中に含むもの（外側に加えて一緒に落とした内側も）は
  // 読まずに断り、自身かその上が保存フォルダのものは保存フォルダの中として断る（目印の問い合わせが返らなかったもの）
  const assign = (found) => {
    for (const entry of entries) {
      if (below.has(entry.i) || entry.marker) continue;
      for (const f of found) {
        const r = relation(entry, f);
        if (r === 'archive') entry.marker = f;
        else if (r) below.set(entry.i, r);
        if (r) break;
      }
    }
  };
  // 落としたものの上の階層で分かった目印（保存フォルダ）も、一緒に落とした外側に割り当てる（外側の中を探しきれなくても）
  assign([...new Set(entries.map((e) => e.marker).filter(Boolean))]);
  const scanned = new Set();
  let decided = decide(below);
  if (belowMs > 0 && typeof fsx.markerBelow === 'function') {
    const deadline = Date.now() + belowMs;
    const walkLinks = followSymlinks || platform === 'win32';
    const scan = async (a) => {
      scanned.add(a.key);
      const left = deadline - Date.now();
      if (left <= 0) return null;
      const ask = () => fsx.markerBelow(a.real, { deadline, maxDirs: belowDirs, walkLinks });
      // 返らない問い合わせ（つながっていない共有の 1 回の読み取り）も、期限の後は待たない
      const answer = await askWithin(ask, left, null);
      return answer && answer.found ? answer.found : null;
    };
    for (;;) {
      const round = decided.acc.filter((a) => !scanned.has(a.key));
      if (!round.length || Date.now() >= deadline) break;
      const found = (await mapLimit(round, QUERY_CONCURRENCY, scan)).filter(Boolean);
      if (!found.length) break;
      assign(found);
      decided = decide(below); // 外側を断ったので、内側などを決め直す（新しく受け付けたものは次の回に中を探す）
    }
  }
  accepted.push(...decided.acc);
  rejected.push(...decided.rej);
  return finish(accepted, rejected);
}

/** 落とした順に並べ直して返す。 */
function finish(accepted, rejected) {
  const byDrop = (a, b) => a.i - b.i;
  return {
    accepted: accepted.sort(byDrop).map(({ path: p, key, keys, file }) => ({ path: p, key, keys, file: !!file })),
    rejected: rejected.sort(byDrop).map(({ path: p, key, vars }) => ({ path: p, key, vars })),
  };
}

// まとめた 1 回の実行のコマンドラインの長さの上限。Windows のコマンドラインは全体で 32,767 文字まで（開発時は uv が
// python を同じ引数で起動し直すので、起動するプログラムのパスの違いの分を残す）
const GROUP_CMDLINE_CHARS = 30000;

/**
 * まとめた 1 回の実行に入れられる、投入元のパスの長さの合計（groupSources の maxChars）。fixedLength は投入元を除いた
 * コマンド（起動するプログラム・保存フォルダ・設定の引数）の長さ。足りなくても 1 件ずつは入れる（groupSources）。
 */
function groupMaxChars(fixedLength) {
  return Math.max(0, GROUP_CMDLINE_CHARS - fixedLength);
}

/**
 * 受け付けたもの（checkSources の accepted）を、1 回の実行の単位にまとめる（#37）。続けて落としたファイルを 1 つにまとめ
 * （長さの上限を超えたら分ける）、フォルダは 1 件ずつ。落とした順は変えない（親フォルダごとに集めると、交互に落としたとき
 * 順が入れ替わり、同じ内容のどちらを取り込むか・保存先の名前が 1 件ずつのときと変わる）。
 * 戻り値: { sources: パス[], key: 先頭の比較キー, keys: 全部の比較キー（列の重なりの判定に使う）, files: 1 件ずつの
 * { path, keys }（後から一部を外して組み直すときに使う）, folder: 全部が同じフォルダならその場所（比較キーで見る）・違えば null }[]
 */
function groupSources(accepted, { platform = process.platform, pathApi = path, maxChars = groupMaxChars(0) } = {}) {
  const groups = []; // { files: [{ path, keys }], file: ファイルのまとまりか }
  let open = null; // 今足しているファイルのまとまり
  let chars = 0;
  const cost = (p) => p.length + 3;
  for (const a of accepted) {
    const entry = { path: a.path, keys: a.keys };
    if (a.file && open && chars + cost(a.path) <= maxChars) {
      open.files.push(entry);
      chars += cost(a.path);
      continue;
    }
    const one = { files: [entry], file: !!a.file };
    groups.push(one);
    open = a.file ? one : null; // フォルダを挟んだら区切る
    chars = cost(a.path);
  }
  return groups.map((g) => {
    const item = withFiles({}, g.files, { platform, pathApi });
    return g.file ? item : { ...item, folder: null }; // フォルダは見出しのフォルダを持たない
  });
}

/**
 * 項目を、files（その一部）だけを持つ形にする。投入元・比較キー・見出しのフォルダは files からだけ決める（ここ 1 か所）。
 * コマンドは呼び出し側が組み直す。
 */
function withFiles(item, files, { platform = process.platform, pathApi = path } = {}) {
  const sources = files.map((f) => f.path);
  const keys = [...new Set(files.flatMap((f) => f.keys))];
  return {
    ...item, source: sources[0], sources, files, key: files[0].keys[0], keys, folder: sharedFolder(sources, { platform, pathApi }),
  };
}

/**
 * 中断したまとめた項目の完了の行に残す形（処理した processed 件だけ。残りは列に戻した行にある。1 件も処理していなければ
 * ファイル 0 個）。件数が分からない・1 件だけの項目・全部処理したなら null（行は元のまま）。
 */
function doneAfter(item, processed) {
  const split = splitAt(item, processed);
  if (!split || !split.rest.length) return null;
  // 1 件も処理していなければファイル 0 個。フォルダ名は残す（どのドロップの行か分かるように）
  if (!split.done.length) return { source: '', sources: [], key: null, keys: [], folder: item.folder ?? null };
  const { source, sources, key, keys, folder } = withFiles(item, split.done);
  return { source, sources, key, keys, folder };
}

/** paths（ファイル）が全部同じフォルダならその場所（落としたパスの形）、違えば null。同じかは比較キーで見る。 */
function sharedFolder(paths, { platform = process.platform, pathApi = path } = {}) {
  const parentKey = (p) => keyOf(pathApi.dirname(pathApi.resolve(p)), platform);
  const first = parentKey(paths[0]);
  return paths.every((p) => parentKey(p) === first) ? pathApi.dirname(paths[0]) : null;
}

/**
 * 列に入れる直前に、ほかのものと重なったファイルを外す（確認のダイアログを待つ間に、別のドロップ・実行ボタンの取り込みが
 * 同じものを始めた）。reasonOf(keys) は重なりの理由（辞書のキー）か null。まとめた項目は重なったファイルだけを外し、残りを
 * rebuild(項目, 残りのパス) で組み直す（コマンドを作り直す）。戻り値 { fresh: 入れる項目, late: 外したもの（理由つき）}
 */
function dropTaken(items, reasonOf, rebuild) {
  const fresh = [];
  const late = [];
  for (const item of items) {
    const keep = item.files.filter((f) => {
      const reason = reasonOf(f.keys);
      if (reason) late.push({ path: f.path, key: reason, vars: { path: f.path } });
      return !reason;
    });
    if (!keep.length) continue;
    if (keep.length === item.files.length) {
      fresh.push(item);
      continue;
    }
    const next = withFiles(item, keep);
    fresh.push(rebuild(next, next.sources));
  }
  return { fresh, late };
}

/**
 * まとめた項目（複数のファイル）を中断したときに、まだ始めていないファイルの項目（列に戻して「再開」で続ける。1 件ずつの
 * 項目だったときと同じく、止めた項目より後のものは失われない）。processed は detector が判定を出し終えた件数（ファイルは
 * 渡した順に処理される）。戻すものが無い・1 件だけの項目・件数が分からないときは null。rebuild(項目, 残りのパス) で
 * コマンドを組み直す。
 */
function remainderAfter(item, processed, rebuild) {
  const split = splitAt(item, processed);
  if (!split || !split.rest.length) return null;
  const { id: _id, status: _status, result: _result, runId: _runId, ...rest } = item;
  const next = withFiles(rest, split.rest);
  return rebuild(next, next.sources);
}

/**
 * まとめた項目（複数のファイル）を、detector が結果の行を出し終えた processed 件（渡した順に処理される）で分ける。
 * { done: 処理したファイル, rest: 始めていないファイル }。1 件だけの項目・件数が分からないときは null（3 つの関数の判定を
 * ここ 1 か所で決める）。
 */
function splitAt(item, processed) {
  const files = item.files || [];
  if (files.length < 2 || !Number.isInteger(processed) || processed < 0) return null;
  const at = Math.min(processed, files.length);
  return { done: files.slice(0, at), rest: files.slice(at) };
}

/**
 * まとめた項目（複数のファイル）が中断以外で終わったとき（致命的エラー・断り・強制終了）の、始めていないファイルの数
 * （列には戻さない。同じ理由で止まり続けないように。行に出して、もう一度落としてもらう）。1 件だけの項目・件数が
 * 分からないときは 0。
 */
function unstartedCount(item, processed) {
  const split = splitAt(item, processed);
  return split ? split.rest.length : 0;
}

/**
 * 終わったまとめた項目の、始めていないファイルを列に戻すか。中断したときだけ。中断と同時に detector が断った（終了コード
 * 3。同じ理由でもう一度断られる）なら戻さず、始めていない件数を行に出す。終了コード 1 は戻す（ロックを待つ間などの中断も
 * 1 で終わる。戻した列は一時停止するので、同じエラーでも繰り返さない）。
 */
function requeuesAfter(summary) {
  return !!summary.stoppedByUser && summary.code !== 3;
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
 * source は 1 件か、まとめたファイルの配列（groupSources。1 回の add に全部を渡す。#37）。
 */
function buildQuickArgs(form, source) {
  const sources = (Array.isArray(source) ? source : [source]).map(String);
  return buildArgs(addForm(form, String(form.defaultArchiveRoot ?? ''), sources[0]), { verbatimPaths: true, sources });
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
    /** 待ちの項目の先頭に入れる（中断したまとめた項目の残り。ほかの待ちより先に続ける）。 */
    addFront(list) {
      const created = list.map((i) => ({ ...i, id: nextId++, status: 'waiting', result: null }));
      const at = items.findIndex((x) => x.status === 'waiting');
      items.splice(at < 0 ? items.length : at, 0, ...created);
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
    /** 完了にする。fields は行に出すものの差し替え（中断したまとめた項目を処理した分だけにする。doneAfter）。 */
    markDone(id, result, fields = null) {
      const i = find(id);
      if (!i) return;
      if (fields) Object.assign(i, fields);
      i.status = 'done';
      i.result = result;
      // 行に出さないもの（設定のフォーム・ファイルごとの比較キー・コマンドとその引数）は捨てる（完了の行は 20 件まで残り、
      // 列の写しを送るたびに写す。まとめた項目のコマンドは 3 万文字近くになる。完了の行は重複の判定にも使わない）
      delete i.form;
      delete i.files;
      delete i.keys;
      delete i.args;
      delete i.command;
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
  pairKeys, sourceKeys, keyWithin, overlapsAny, checkSources, createQueue, buildQuickArgs, quickPreviewForm, groupSources, dropTaken, groupMaxChars, remainderAfter, unstartedCount, doneAfter, requeuesAfter,
};
