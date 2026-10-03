// commands.test.js - パス欄の正規化（node --test）
const test = require('node:test');
const assert = require('node:assert');

const { buildArgs, normalizePath, toFieldValue, FormError } = require('./commands');

test('POSIX ではターミナル由来のバックスラッシュエスケープを解く', () => {
  assert.strictEqual(normalizePath('/Users/a\\ b/c', 'darwin'), '/Users/a b/c');
  assert.strictEqual(normalizePath("'/Users/a b/c'", 'darwin'), '/Users/a b/c');
});

test('Windows のドライブパス・UNC パスの区切りは消さない', () => {
  for (const platform of ['win32', 'darwin']) {
    assert.strictEqual(normalizePath('C:\\Users\\me\\archive', platform), 'C:\\Users\\me\\archive');
    assert.strictEqual(normalizePath('"C:\\My Files\\in"', platform), 'C:\\My Files\\in');
    assert.strictEqual(normalizePath('\\\\server\\share\\a', platform), '\\\\server\\share\\a');
  }
  // Windows 上では相対パスでも \ は区切り
  assert.strictEqual(normalizePath('data\\inbox', 'win32'), 'data\\inbox');
});

test('buildArgs に Windows パスがそのまま渡る', () => {
  const args = buildArgs({ mode: 'add', archiveRoot: 'D:\\archive', sourcePath: '\\\\nas\\photos' });
  assert.deepStrictEqual(args.slice(0, 3), ['add', 'D:\\archive', '\\\\nas\\photos']);
});

test('archives は保存フォルダの指定が要らず、一覧・JSON・登録を外すを組み立てる', () => {
  assert.deepStrictEqual(buildArgs({ mode: 'archives' }), ['archives']);
  assert.deepStrictEqual(buildArgs({ mode: 'archives', archiveRoot: '/a', json: true }), ['archives', '--json']);
  // 登録を外すときは、一覧で見た保存フォルダの uid を必ず渡す（別の正本 DB の同じ ID を外さないため）
  assert.deepStrictEqual(
    buildArgs({ mode: 'archives', forgetId: 3, forgetUid: 'abc123', archiveDb: '/d/archive.db' }),
    ['archives', '--forget', '3', '--expect-uid=abc123', '--archive-db', '/d/archive.db'],
  );
  assert.throws(() => buildArgs({ mode: 'archives', forgetId: 3 }), (e) => e instanceof FormError);
  // uid は識別子ファイルの中身そのまま（文字種の保証が無い）。先頭が - でもオプションと取り違えない形で渡す
  assert.deepStrictEqual(
    buildArgs({ mode: 'archives', forgetId: 3, forgetUid: '-odd uid!' }),
    ['archives', '--forget', '3', '--expect-uid=-odd uid!'],
  );
  for (const bad of ['0', '-1', '1.5', 'x']) {
    assert.throws(() => buildArgs({ mode: 'archives', forgetId: bad, forgetUid: 'abc' }), (e) => e instanceof FormError, bad);
  }
});

test('adopt は保存フォルダだけを渡す', () => {
  assert.deepStrictEqual(buildArgs({ mode: 'adopt', archiveRoot: "'/Users/a b/photos'" }), ['adopt', '/Users/a b/photos']);
  assert.throws(() => buildArgs({ mode: 'adopt' }), (e) => e instanceof FormError);
});

test('detector が出した実パスは正規化で変わらない（一覧の正本 DB・adopt の案内）', () => {
  // 登録を外すときは一覧を読んだ正本 DB をそのまま渡す（入力欄の値より優先）
  assert.deepStrictEqual(
    buildArgs({ mode: 'archives', forgetId: 1, forgetUid: 'u', forgetArchiveDb: '/Users/me/a\\b/archive.db', archiveDb: '/x.db' }),
    ['archives', '--forget', '1', '--expect-uid=u', '--archive-db', '/Users/me/a\\b/archive.db'],
  );
  // 入力欄に入れる値は、正規化すると元の実パスに戻る
  for (const [p, platform] of [
    ['/Users/me/a\\b/photos', 'darwin'],
    ["/Users/me/it's/photos", 'darwin'],
    ['/Users/me/plain', 'linux'],
    ['C:\\Users\\me\\photos', 'win32'],
    ['\\\\nas\\share\\photos', 'win32'],
  ]) {
    assert.strictEqual(normalizePath(toFieldValue(p, platform), platform), p, `${platform}: ${p}`);
  }
});
