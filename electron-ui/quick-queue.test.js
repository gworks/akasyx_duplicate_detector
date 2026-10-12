// quick-queue.test.js - かんたん取り込み（#22）の受け付けと順番待ちの列（node --test）
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const {
  sourceKeys, keyWithin, checkSources, createQueue, buildQuickArgs, quickPreviewForm,
} = require('./quick-queue');

// 実体の位置の比較キー（sourceKeys の先頭）
const pathKey = async (p, opts) => (await sourceKeys(p, opts))[0];

// 実ファイルに触らない fs の代わり。files はあるもの（フォルダ・ファイル）、markers は保存フォルダの目印があるフォルダ
// realpath は本物と同じく、無いものでは投げる（リンクは先を返す）
function fakeFs({ files = [], markers = [], links = {} } = {}) {
  const exists = (p) => files.includes(p) || markers.includes(p);
  return {
    exists,
    realpath: (p) => {
      if (links[p]) return links[p];
      if (!exists(p)) throw Object.assign(new Error(`ENOENT: ${p}`), { code: 'ENOENT' });
      return p;
    },
    hasMarker: (dir) => markers.includes(dir),
  };
}

const posix = { platform: 'linux', pathApi: path.posix };

test('比較キー: macOS・Windows は大文字小文字を区別しない。Linux は区別する', async () => {
  const fsx = fakeFs();
  assert.strictEqual(await pathKey('/A/b', { ...posix, platform: 'darwin', fsx }), await pathKey('/a/B', { ...posix, platform: 'darwin', fsx }));
  assert.notStrictEqual(await pathKey('/A/b', { ...posix, fsx }), await pathKey('/a/B', { ...posix, fsx }));
  assert.strictEqual(
    await pathKey('C:\\Data\\X', { platform: 'win32', pathApi: path.win32, fsx }),
    await pathKey('c:\\data\\x\\', { platform: 'win32', pathApi: path.win32, fsx })
  );
});

test('比較キー: シンボリックリンクは実体の位置で見る', async () => {
  const fsx = fakeFs({ links: { '/link': '/real/archive' } });
  assert.strictEqual(await pathKey('/link', { ...posix, fsx }), await pathKey('/real/archive', { ...posix, fsx }));
});

test('配下の判定は区切りで見る（/a/bc は /a/b の中ではない）', async () => {
  assert.ok(keyWithin('/a/b', '/a/b', '/'));
  assert.ok(keyWithin('/a/b', '/a/b/c', '/'));
  assert.ok(!keyWithin('/a/b', '/a/bc', '/'));
  assert.ok(keyWithin('/', '/a', '/'));
});

const check = (sources, opts = {}) => checkSources({
  sources,
  archiveRoot: '/archive',
  registeredRoots: [],
  queuedKeys: [],
  ...posix,
  fsx: fakeFs({ files: ['/archive', '/in/a', '/in/b', '/in', '/archive/x', '/other/arc/y', '/x/file.jpg'], ...opts.fs }),
  ...opts,
});

test('ふつうのフォルダ・ファイルは受け付ける', async () => {
  const r = await check(['/in/a', '/x/file.jpg']);
  assert.deepStrictEqual(r.accepted.map((a) => a.path), ['/in/a', '/x/file.jpg']);
  assert.deepStrictEqual(r.rejected, []);
});

test('保存フォルダそのもの・その中・それを含むフォルダは断る（理由つき）', async () => {
  const r = await check(['/archive', '/archive/x', '/']);
  assert.deepStrictEqual(r.accepted, []);
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_archive', 'quick_reject_archive', 'quick_reject_archive']);
  assert.deepStrictEqual(r.rejected[0].vars, { path: '/archive', archive: '/archive' });
});

test('登録済みのほかの保存フォルダとの入れ子も断る', async () => {
  const r = await check(['/other', '/other/arc/y'], { registeredRoots: ['/other/arc'], fs: { files: ['/other', '/other/arc/y'] } });
  assert.deepStrictEqual(r.rejected.map((x) => [x.path, x.key]), [
    ['/other', 'quick_reject_archive'], ['/other/arc/y', 'quick_reject_archive'],
  ]);
});

test('登録されていなくても、保存フォルダの目印があるフォルダの中は断る', async () => {
  const r = await check(['/mnt/arc/sub'], { fs: { files: ['/mnt/arc/sub'], markers: ['/mnt/arc'] } });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_in_archive']);
  assert.deepStrictEqual(r.rejected[0].vars, { path: '/mnt/arc/sub', archive: '/mnt/arc' });
});

test('見つからないものは断る（存在しないものを列に入れない）', async () => {
  const r = await check(['/nope']);
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_missing']);
});

test('列にすでにあるもの（大文字小文字違い・リンク経由を含む）は断る', async () => {
  const fsx = fakeFs({ files: ['/in/a', '/link'], links: { '/link': '/in/a' } });
  const queuedKeys = [await pathKey('/in/a', { ...posix, fsx })];
  const r = await checkSources({ sources: ['/link'], archiveRoot: '/archive', registeredRoots: [], queuedKeys, ...posix, fsx });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_queued']);
});

test('1 回のドロップの中の同じものは 1 つにまとめ、もう 1 つは理由つきで外す（黙って消さない）', async () => {
  const r = await check(['/in/a', '/in/a/']);
  assert.deepStrictEqual(r.accepted.map((a) => a.path), ['/in/a']);
  assert.deepStrictEqual(r.rejected.map((x) => [x.path, x.key, x.vars.other]), [['/in/a/', 'quick_reject_duplicate', '/in/a']]);
});

test('受け付けの確認は fs の応答を待つ（非同期の fs でも同じ判定）', async () => {
  const sync = fakeFs({ files: ['/in/a', '/mnt/arc/x'], markers: ['/mnt/arc'] });
  const fsx = {
    exists: async (p) => sync.exists(p), realpath: async (p) => sync.realpath(p), hasMarker: async (d) => sync.hasMarker(d),
  };
  const r = await checkSources({ sources: ['/in/a', '/mnt/arc/x', '/nope'], archiveRoot: '/archive', ...posix, fsx });
  assert.deepStrictEqual(r.accepted.map((a) => a.path), ['/in/a']);
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_in_archive', 'quick_reject_missing']);
});

test('既定の保存フォルダが無いときは全部断る', async () => {
  const r = await check(['/in/a'], { archiveRoot: '' });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_no_archive']);
});

// ---- コマンド: 確認した時点の設定で組む

const form = {
  mode: 'quick', defaultArchiveRoot: '/archive', archiveRoot: '/typed/elsewhere', sourcePath: '/typed/src',
  folderLimit: '300', minSize: '1', withMeta: true, followSymlinks: false, pruneEmptyDirs: true,
  dryRun: true, yes: true, trashDir: '/t', archiveDb: '/db/archive.db', logDir: '',
};

test('かんたん取り込みのコマンドは既定の保存フォルダへの add（取り込みタブの欄・dry-run は使わない）', async () => {
  const args = buildQuickArgs(form, '/in/a');
  assert.deepStrictEqual(args, [
    'add', '/archive', '/in/a', '--folder-limit', '300', '--with-meta', '--prune-empty-dirs', '--archive-db', '/db/archive.db',
  ]);
});

// \ と先頭の引用符がファイル名の文字になるのは macOS・Linux だけ（Windows では区切り・名前に使えない）
test('実パスはパス欄の正規化で書き換わらない（\\ や引用符を含む名前）', { skip: process.platform === 'win32' }, async () => {
  const args = buildQuickArgs({ ...form, defaultArchiveRoot: "/ar\\c" }, "'/in/q'");
  assert.strictEqual(args[1], '/ar\\c');
  assert.strictEqual(args[2], "'/in/q'");
});

test('プレビュー用のフォームは取り込みの既定で組む（保存先が無ければ空のまま）', async () => {
  const f = quickPreviewForm(form);
  assert.strictEqual(f.mode, 'add');
  assert.strictEqual(f.archiveRoot, '/archive');
  assert.strictEqual(f.dryRun, false);
  assert.strictEqual(quickPreviewForm({ ...form, defaultArchiveRoot: '' }).archiveRoot, '');
});

// ---- 順番待ちの列

const item = (source) => ({ source, key: source, args: ['add', '/archive', source], command: `add ${source}` });

test('列は入れた順に 1 つずつ始め、終わったら次へ', async () => {
  const q = createQueue();
  q.add([item('/a'), item('/b')]);
  const first = q.nextWaiting();
  assert.strictEqual(first.source, '/a');
  q.markRunning(first.id, 7);
  const running = q.list().find((i) => i.status === 'running');
  assert.strictEqual(running.id, first.id);
  assert.strictEqual(running.runId, 7); // 行の中断は、この実行の id で頼む
  assert.strictEqual(q.nextWaiting().source, '/b'); // 次の候補（始めるかは呼び出し側が決める）
  q.markDone(first.id, { code: 0 });
  assert.ok(!q.list().some((i) => i.status === 'running'));
  assert.deepStrictEqual(q.list().map((i) => [i.source, i.status]), [['/a', 'done'], ['/b', 'waiting']]);
});

test('待ちの項目は取り消せる。実行中・完了の項目は取り消せない', async () => {
  const q = createQueue();
  const [a, b] = q.add([item('/a'), item('/b')]);
  q.markRunning(a.id);
  assert.strictEqual(q.cancel(a.id), false);
  assert.strictEqual(q.cancel(b.id), true);
  assert.strictEqual(q.nextWaiting(), null);
  assert.deepStrictEqual(q.list().map((i) => i.source), ['/a']);
});

test('列にある（待ち・実行中）もののキーだけが重複の判定に使われる（完了したものはもう一度入れられる）', async () => {
  const q = createQueue();
  const [a] = q.add([item('/a'), item('/b')]);
  q.markRunning(a.id);
  assert.deepStrictEqual(q.activeKeys().sort(), ['/a', '/b']);
  q.markDone(a.id, { code: 0 });
  assert.deepStrictEqual(q.activeKeys(), ['/b']);
});

test('終了するときは待ちの列を捨てる（次回に持ち越さない・知らないうちに始まらない）', async () => {
  const q = createQueue();
  const [a] = q.add([item('/a'), item('/b')]);
  q.markRunning(a.id);
  q.discardWaiting();
  assert.strictEqual(q.nextWaiting(), null);
  assert.deepStrictEqual(q.list().map((i) => i.status), ['running']);
});

test('完了の記録は古いものから捨てる（待ち・実行中は捨てない）', async () => {
  const q = createQueue({ keepDone: 2 });
  const items = q.add([item('/a'), item('/b'), item('/c'), item('/d')]);
  for (const i of items.slice(0, 3)) {
    q.markRunning(i.id);
    q.markDone(i.id, { code: 0 });
  }
  assert.deepStrictEqual(q.list().map((i) => i.source), ['/b', '/c', '/d']);
});

test('id は項目ごとに別（取り消し・中断で取り違えない）', async () => {
  const q = createQueue();
  const ids = q.add([item('/a'), item('/b')]).map((i) => i.id);
  const more = q.add([item('/c')]).map((i) => i.id);
  assert.strictEqual(new Set([...ids, ...more]).size, 3);
});

// ---- 配布物: main が読む自前のファイルは electron-builder の files に入っている（漏れると配布版だけ起動できない）

test('main.js が require する自前のファイルは、すべて配布物（package.json の build.files）に入っている', async () => {
  const fs = require('node:fs');
  const main = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf-8');
  const files = require('./package.json').build.files;
  const local = [...main.matchAll(/require\('\.\/([^']+)'\)/g)].map((m) => m[1].replace(/(\.js)?$/, '.js'));
  assert.ok(local.includes('quick-queue.js'));
  for (const f of local) {
    const listed = files.includes(f) || files.some((g) => g.endsWith('/**/*') && f.startsWith(g.slice(0, -4)));
    assert.ok(listed, `${f} が build.files に無い`);
  }
});

test('列にあるものと入れ子（その中・それを含む）も断る（同じ木を 2 回動かさない）', async () => {
  const fsx = fakeFs({ files: ['/data/photos', '/data/photos/2020', '/data'] });
  const queuedKeys = [await pathKey('/data/photos', { ...posix, fsx })];
  const r = await checkSources({
    sources: ['/data/photos/2020', '/data'], archiveRoot: '/archive', registeredRoots: [], queuedKeys, ...posix, fsx,
  });
  assert.deepStrictEqual(r.rejected.map((x) => [x.path, x.key]), [
    ['/data/photos/2020', 'quick_reject_queued'], ['/data', 'quick_reject_queued'],
  ]);
});

test('一緒にドロップしたものどうしの入れ子は、落とした順に関わらず外側を取り込み、内側は理由つきで外す', async () => {
  for (const order of [['/in', '/in/a'], ['/in/a', '/in']]) {
    const r = await check(order);
    assert.deepStrictEqual(r.accepted.map((a) => a.path), ['/in'], order.join(','));
    assert.deepStrictEqual(r.rejected.map((x) => [x.path, x.key, x.vars.other]), [['/in/a', 'quick_reject_nested', '/in']]);
  }
});

test('受け付けたものは落とした順に並ぶ', async () => {
  const r = await check(['/x/file.jpg', '/in/b', '/in/a']);
  assert.deepStrictEqual(r.accepted.map((a) => a.path), ['/x/file.jpg', '/in/b', '/in/a']);
});

test('登録済みの保存フォルダは書かれた位置で比べる（つながっていない場所を realpath しない）', async () => {
  const touched = [];
  const fsx = { ...fakeFs({ files: ['/in/a'] }), realpath: (p) => { touched.push(p); return p; } };
  await checkSources({ sources: ['/in/a'], archiveRoot: '/archive', registeredRoots: ['/nas/offline'], queuedKeys: [], ...posix, fsx });
  assert.ok(!touched.some((p) => p.startsWith('/nas')), touched.join(','));
});

test('他のタブの実行の結果を完了の行として残せる（列の項目に押し出されても結果を見られる）。重複の判定には使わない', async () => {
  const q = createQueue();
  q.add([item('/a')]);
  const other = q.addDone({ mode: 'verify', command: 'verify /archive', result: { code: 0 } });
  assert.strictEqual(other.status, 'done');
  assert.strictEqual(other.mode, 'verify');
  assert.deepStrictEqual(q.list().map((i) => i.status), ['done', 'waiting']); // 終わった順（待ちより前）
  assert.deepStrictEqual(q.activeKeys(), ['/a']);
  assert.strictEqual(q.nextWaiting().source, '/a');
});

test('取り込み先の保存フォルダにも問い合わせない（つながっていなくても待たない）。目印は同じフォルダを 1 度だけ問い合わせる', async () => {
  const touched = [];
  const asked = [];
  const base = fakeFs({ files: ['/in/d/a', '/in/d/b', '/in/d/c'] });
  const fsx = {
    ...base,
    realpath: (p) => { touched.push(p); return p; },
    hasMarker: (d) => { asked.push(d); return false; },
  };
  const r = await checkSources({ sources: ['/in/d/a', '/in/d/b', '/in/d/c'], archiveRoot: '/nas/archive', ...posix, fsx });
  assert.strictEqual(r.accepted.length, 3);
  assert.ok(!touched.some((p) => p.startsWith('/nas')), touched.join(','));
  assert.strictEqual(asked.filter((d) => d === '/in').length, 1);
});

test('落としたものの実体の位置が保存フォルダと別の表記になっても（割り当てたドライブ等）、書かれた位置でも重なりを見る', async () => {
  // Z:\photos の実体が \nas\share\photos になる。保存フォルダは Z:\photos\archive と書かれている
  const fsx = { ...fakeFs({ files: ['/z/photos'] }), realpath: (p) => (p === '/z/photos' ? '/nas/share/photos' : p) };
  const r = await checkSources({ sources: ['/z/photos'], archiveRoot: '/z/photos/archive', ...posix, fsx });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_archive']);
});

test('存在と目印の確認はまとめて並行に問い合わせる（1 件ずつ待たない）', async () => {
  let inFlight = 0;
  let peak = 0;
  const slow = (v) => new Promise((res) => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    setTimeout(() => { inFlight -= 1; res(v); }, 5);
  });
  const fsx = { exists: () => slow(true), realpath: (p) => p, hasMarker: () => slow(false) };
  const r = await checkSources({ sources: ['/a/1', '/b/2', '/c/3'], archiveRoot: '/archive', ...posix, fsx });
  assert.strictEqual(r.accepted.length, 3);
  assert.ok(peak > 1, `peak=${peak}`);
});

test('列にあるものとの重なりも、実体の位置と書かれた位置の両方で見る（受け付けたものは両方の形のキーを持つ）', async () => {
  // 列に入れたときは実体を引けず（つながっていない）書かれた位置で入った /z/photos。今は実体が /nas/photos と引ける
  const queued = await checkSources({ sources: ['/z/photos'], archiveRoot: '/archive', ...posix, fsx: fakeFs({ files: ['/z/photos'] }) });
  assert.deepStrictEqual(queued.accepted[0].keys, ['/z/photos']);
  const fsx = { ...fakeFs({ files: ['/z/photos/2020'] }), realpath: (p) => p.replace('/z/', '/nas/') };
  const r = await checkSources({
    sources: ['/z/photos/2020'], archiveRoot: '/archive', queuedKeys: queued.accepted[0].keys, ...posix, fsx,
  });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_queued']);
});

test('列の重複の判定には、項目の両方の形のキーを使う', () => {
  const q = createQueue();
  q.add([{ source: '/z/a', key: '/nas/a', keys: ['/nas/a', '/z/a'], args: [], command: '' }]);
  assert.deepStrictEqual(q.activeKeys().sort(), ['/nas/a', '/z/a']);
});

test('一緒に落としたもの同士の入れ子も、書かれた位置で見る（片方の実体を引けなくても）', async () => {
  // /z/photos の実体は /nas/photos。子の /nas/photos/2020 は実体を引けず書かれた位置のまま
  const fsx = { ...fakeFs({ files: ['/z/photos', '/nas/photos/2020'] }), realpath: (p) => (p === '/z/photos' ? '/nas/photos' : p) };
  const both = await checkSources({ sources: ['/z/photos', '/z/photos/2020'], archiveRoot: '/archive', ...posix,
    fsx: { ...fsx, realpath: (p) => (p === '/z/photos' ? '/nas/photos' : (() => { throw new Error('offline'); })()) },
    // ↑ 子の実体は引けない（書かれた位置 /z/photos/2020 のまま）。親は実体 /nas/photos
  });
  assert.deepStrictEqual(both.accepted.map((a) => a.path), ['/z/photos']);
  assert.deepStrictEqual(both.rejected.map((x) => x.key), ['quick_reject_nested']);
});

test('sourceKeys: 実体の位置と書かれた位置の比較キー（実行中の取り込みの投入元との比較にも使う）', async () => {
  const fsx = { ...fakeFs(), realpath: (p) => p.replace('/z/', '/nas/') };
  assert.deepStrictEqual(await sourceKeys('/z/a', { ...posix, fsx }), ['/nas/a', '/z/a']);
  assert.deepStrictEqual(await sourceKeys('/b', { ...posix, fsx: fakeFs() }), ['/b']);
});

test('一緒に落とした入れ子は、外側が後から来ても（実体の位置が長くても）外側を取り込み、内側を外す', async () => {
  // /a の実体は長い /zzzz/long/a。/a/b の実体は短い /q（先に見られる）
  const fsx = { ...fakeFs({ files: ['/a', '/a/b'] }), realpath: (p) => ({ '/a': '/zzzz/long/a', '/a/b': '/q' })[p] || p };
  const r = await checkSources({ sources: ['/a', '/a/b'], archiveRoot: '/archive', ...posix, fsx });
  assert.deepStrictEqual(r.accepted.map((a) => a.path), ['/a']);
  assert.deepStrictEqual(r.rejected.map((x) => [x.path, x.key, x.vars.other]), [['/a/b', 'quick_reject_nested', '/a']]);
});

test('保存フォルダの目印は、書かれた位置の上の階層でも探す（保存フォルダの中のリンクが外を指していても断る）', async () => {
  const fsx = { ...fakeFs({ files: ['/vault/link'], markers: ['/vault'] }), realpath: (p) => (p === '/vault/link' ? '/imports/file' : p) };
  const r = await checkSources({ sources: ['/vault/link'], archiveRoot: '/archive', ...posix, fsx });
  assert.deepStrictEqual(r.rejected.map((x) => [x.key, x.vars.archive]), [['quick_reject_in_archive', '/vault']]);
});

test('中断で列を一時停止する: 止めている間は次を出さず、再開で続ける。待ちが無くなったら一時停止は解ける', () => {
  const q = createQueue();
  const [a, b, c] = q.add([item('/a'), item('/b'), item('/c')]);
  q.markRunning(a.id, 1);
  q.markDone(a.id, { stoppedByUser: true });
  q.pause();
  assert.strictEqual(q.isPaused(), true);
  assert.strictEqual(q.nextWaiting(), null); // 止めている間は次を出さない
  q.resume();
  assert.strictEqual(q.nextWaiting().id, b.id);
  q.pause();
  q.cancel(b.id);
  q.cancel(c.id);
  assert.strictEqual(q.isPaused(), false); // 待ちが無ければ止める意味が無い（次に落としたものはすぐ始まる）
});

test('一時停止: 待ちが無いまま止めた後に入れたものは止めない（読む順番に関わらず）。isPaused は状態を変えない', () => {
  const q = createQueue();
  q.pause(); // 待ちが無いときの中断
  q.add([item('/x')]);
  assert.strictEqual(q.nextWaiting().source, '/x'); // isPaused を呼ばなくても止まったままにならない
  q.pause();
  assert.strictEqual(q.isPaused(), true);
  assert.strictEqual(q.isPaused(), true); // 読むだけ
});

test('名前の前後に空白があるものも、確かめたパスのままコマンドに入れる（パス欄の正規化を通さない）', () => {
  const args = buildQuickArgs({ ...form, defaultArchiveRoot: '/arch ' }, '/in/report ');
  assert.strictEqual(args[1], '/arch ');
  assert.strictEqual(args[2], '/in/report ');
});

test('外側が後から来て内側を外したら、その内側を指していた断りの理由も外側を指す', async () => {
  // /b の実体は短い /q（先に見られる）。/b を 2 度落とし、外側の /a（/b の実体の外側）は後から来る
  const fsx = { ...fakeFs({ files: ['/a', '/a/b'] }), realpath: (p) => ({ '/a': '/zzzz/long/a', '/a/b': '/q' })[p] || p };
  const r = await checkSources({ sources: ['/a/b', '/a/b/', '/a'], archiveRoot: '/archive', ...posix, fsx });
  assert.deepStrictEqual(r.accepted.map((x) => x.path), ['/a']);
  for (const x of r.rejected) assert.strictEqual(x.vars.other, '/a', JSON.stringify(x));
});

test('sourceKeys の timeoutMs: 実体の位置が間に合えば両方、間に合わなければ書かれた位置だけ（待ち続けない）', async () => {
  const quick = { ...fakeFs(), realpath: async (p) => p.replace('/link', '/data') };
  assert.deepStrictEqual(await sourceKeys('/link/x', { ...posix, fsx: quick, timeoutMs: 50 }), ['/data/x', '/link/x']);
  const hang = { ...fakeFs(), realpath: () => new Promise(() => {}) };
  const started = Date.now();
  assert.deepStrictEqual(await sourceKeys('/nas/x', { ...posix, fsx: hang, timeoutMs: 30 }), ['/nas/x']);
  assert.ok(Date.now() - started < 1000);
});

test('受け付けの確認は、問い合わせが返らなくても timeoutMs で打ち切る（存在が分からなければ応答なしで断る・目印が分からなければ detector に任せる）', async () => {
  const hang = () => new Promise(() => {});
  const fsx = { exists: hang, realpath: hang, hasMarker: hang };
  const started = Date.now();
  const r = await checkSources({ sources: ['/nas/x'], archiveRoot: '/archive', ...posix, fsx, timeoutMs: 30 });
  assert.ok(Date.now() - started < 1000);
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_unreachable']);
  const slowMarker = { ...fakeFs({ files: ['/in/a'] }), hasMarker: hang };
  const r2 = await checkSources({ sources: ['/in/a'], archiveRoot: '/archive', ...posix, fsx: slowMarker, timeoutMs: 30 });
  assert.deepStrictEqual(r2.accepted.map((x) => x.path), ['/in/a']);
});

test('存在の問い合わせが返らないものは「見つからない」ではなく「応答がない」で断る', async () => {
  const fsx = { ...fakeFs(), exists: () => new Promise(() => {}) };
  const r = await checkSources({ sources: ['/nas/x'], archiveRoot: '/archive', ...posix, fsx, timeoutMs: 20 });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_unreachable']);
});

test('目印の確認は、上の階層の数に関わらず全体で timeoutMs まで。存在を確かめられないものは目印を探さない', async () => {
  const asked = [];
  const hang = () => new Promise(() => {});
  const deep = { ...fakeFs({ files: ['/a/b/c/d/e/f'] }), hasMarker: (d) => { asked.push(d); return hang(); } };
  const started = Date.now();
  const r = await checkSources({ sources: ['/a/b/c/d/e/f'], archiveRoot: '/archive', ...posix, fsx: deep, timeoutMs: 30 });
  assert.ok(Date.now() - started < 200, `${Date.now() - started}ms`);
  assert.deepStrictEqual(r.accepted.map((x) => x.path), ['/a/b/c/d/e/f']); // 目印が分からなければ detector に任せる
  asked.length = 0;
  const gone = { ...fakeFs(), exists: hang, hasMarker: (d) => { asked.push(d); return false; } };
  await checkSources({ sources: ['/nas/x'], archiveRoot: '/archive', ...posix, fsx: gone, timeoutMs: 20 });
  assert.deepStrictEqual(asked, []);
});

test('実体の位置が分かったものは存在を問い合わせ直さない。実体の位置が返らないものは存在も問い合わせず応答なしで断る', async () => {
  let existsAsked = 0;
  const base = fakeFs({ files: ['/in/a'] });
  const counted = { ...base, exists: (p) => { existsAsked += 1; return base.exists(p); } };
  const ok = await checkSources({ sources: ['/in/a'], archiveRoot: '/archive', ...posix, fsx: counted, timeoutMs: 20 });
  assert.deepStrictEqual(ok.accepted.map((x) => x.path), ['/in/a']);
  assert.strictEqual(existsAsked, 0);
  const hang = { ...counted, realpath: () => new Promise(() => {}) };
  const r = await checkSources({ sources: ['/nas/x'], archiveRoot: '/archive', ...posix, fsx: hang, timeoutMs: 20 });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_unreachable']);
  assert.strictEqual(existsAsked, 0);
});

test('絶対パスでないものは断る（確かめる場所と、detector が実行する場所が食い違わないように）', async () => {
  const r = await checkSources({ sources: ['photos', '../x'], archiveRoot: '/archive', ...posix, fsx: fakeFs({ files: ['/in/a'] }) });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_not_absolute', 'quick_reject_not_absolute']);
  assert.deepStrictEqual(r.accepted, []);
});

test('実行中の取り込み（実行ボタン）と重なるものは、列ではなく実行中の取り込みと重なると断る', async () => {
  const r = await checkSources({
    sources: ['/photos/2024', '/in/a'], archiveRoot: '/archive', runningKeys: ['/photos'], queuedKeys: ['/in'], ...posix,
    fsx: fakeFs({ files: ['/photos/2024', '/in/a'] }),
  });
  assert.deepStrictEqual(r.rejected.map((x) => [x.path, x.key]), [
    ['/photos/2024', 'quick_reject_running'], ['/in/a', 'quick_reject_queued'],
  ]);
});

test('既定の保存フォルダが絶対パスでなければ、保存先が決まっていないとして全部断る', async () => {
  const r = await checkSources({ sources: ['/in/a'], archiveRoot: 'archive', ...posix, fsx: fakeFs({ files: ['/in/a'] }) });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_no_archive']);
});

test('権限が無く読めないものは「見つからない」ではなく「読めない」で断る', async () => {
  const fsx = {
    ...fakeFs(),
    realpath: () => { throw Object.assign(new Error('EACCES'), { code: 'EACCES' }); },
    exists: () => false,
  };
  const r = await checkSources({ sources: ['/locked/x'], archiveRoot: '/archive', ...posix, fsx });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_unreadable']);
});

test('macOS では比較キーの Unicode を NFC にそろえる（NFD で来た名前も同じ場所とみなす）', async () => {
  const nfc = '/Volumes/X/写真が';
  const nfd = nfc.normalize('NFD');
  const fsx = fakeFs({ files: [`${nfd}/sub`] });
  const r = await checkSources({ sources: [`${nfd}/sub`], archiveRoot: nfc, ...posix, platform: 'darwin', fsx });
  assert.deepStrictEqual(r.rejected.map((x) => x.key), ['quick_reject_archive']);
});

test('realpath が「無い」以外の理由で失敗しても、あるものは受け付ける（特殊なボリューム）', async () => {
  const fsx = {
    ...fakeFs({ files: ['/ram/x'] }),
    realpath: () => { throw Object.assign(new Error('EISDIR'), { code: 'EISDIR' }); },
  };
  const r = await checkSources({ sources: ['/ram/x'], archiveRoot: '/archive', ...posix, fsx });
  assert.deepStrictEqual(r.accepted.map((x) => x.path), ['/ram/x']);
});

test('問い合わせは同時に 4 件まで（大量に落としても、順番待ちで時間切れにしない）', async () => {
  let inFlight = 0;
  let peak = 0;
  const slow = (v) => new Promise((res) => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    setTimeout(() => { inFlight -= 1; res(v); }, 2);
  });
  const sources = Array.from({ length: 20 }, (_, i) => `/in/f${i}`);
  const fsx = { exists: () => slow(true), realpath: (p) => slow(p), hasMarker: () => slow(false) };
  const r = await checkSources({ sources, archiveRoot: '/archive', ...posix, fsx, timeoutMs: 1000 });
  assert.strictEqual(r.accepted.length, 20);
  assert.ok(peak <= 4, `peak=${peak}`);
});
