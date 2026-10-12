// settings.test.js - 実行の設定とシステムの設定を分ける（#21）（node --test）
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { summarizeArgs, previewDefaults } = require('./defaults-summary');

const html = fs.readFileSync(path.join(__dirname, 'renderer', 'index.html'), 'utf-8');

// 保存済みの設定は要素の id がキー（app.js の snapshot / restore）。動かしても id を変えない（アップデートで設定が消えない）
const PERSISTED_IDS = [
  'archiveRoot', 'sourcePath', 'dryRun', 'folderLimit', 'minSize', 'withMeta', 'followSymlinks', 'pruneEmptyDirs',
  'verifyFollowSymlinks', 'trashDir', 'ingestId', 'delPruneEmptyDirs', 'yes', 'reportIngestId',
  'archiveDb', 'crawlerRepo', 'dbDir', 'logDir',
];

test('保存済みの設定の id がすべて残っている（項目を動かしても設定が消えない）', () => {
  for (const id of PERSISTED_IDS) {
    const m = html.match(new RegExp(`<(input|select)[^>]*\\sid="${id}"[^>]*>`));
    assert.ok(m, `id=${id} が無い`);
    assert.match(m[0], /\sdata-field="/, `id=${id} に data-field が無い`);
  }
  for (const kind of ['missing', 'unregistered', 'archive_duplicate', 'hash_mismatch']) {
    assert.match(html, new RegExp(`data-group="flagQuarantine" value="${kind}"`));
  }
});

/** 設定画面の、ある機能の既定の区画（data-for-mode）の中身。次の区画の始まりまでで区切る。 */
function forModeBlock(mode) {
  const settings = html.slice(html.indexOf('id="settings"'));
  const starts = [...settings.matchAll(/data-for-mode="([^"]+)"/g)];
  const i = starts.findIndex((m) => m[1] === mode);
  assert.ok(i >= 0, `設定画面に ${mode} の区画が無い`);
  const end = i + 1 < starts.length ? starts[i + 1].index : settings.indexOf('data-settings-section="system"');
  return settings.slice(starts[i].index, end);
}

test('機能ごとの既定は、設定画面のその機能の区画にある（ほかの機能のコマンドに混ざらない）', () => {
  const ids = (block) => [...block.matchAll(/\sid="([^"]+)"[^>]*data-field|data-field="[^"]*"[^>]*\sid="([^"]+)"/g)]
    .map((m) => m[1] || m[2]);
  assert.deepStrictEqual(
    ids(forModeBlock('add')).sort(),
    ['folderLimit', 'followSymlinks', 'minSize', 'pruneEmptyDirs', 'withMeta'].sort()
  );
  assert.deepStrictEqual(ids(forModeBlock('verify')), ['verifyFollowSymlinks']);
  assert.match(forModeBlock('verify'), /data-group="flagQuarantine"/);
  assert.deepStrictEqual(ids(forModeBlock('delete-duplicates')).sort(), ['delPruneEmptyDirs', 'trashDir'].sort());
});

test('実行ごとに変える項目は実行画面（機能のパネル）に残す。確認を省く --yes も実行画面のまま', () => {
  const settings = html.slice(html.indexOf('id="settings"'));
  for (const id of ['archiveRoot', 'sourcePath', 'dryRun', 'ingestId', 'yes', 'reportIngestId']) {
    assert.ok(!new RegExp(`\\sid="${id}"`).test(settings), `${id} が設定画面に入っている`);
  }
});

test('システムの設定（データの場所・正本 DB・crawler・ログ・既定の保存フォルダ）は設定画面の「システム」にある', () => {
  const system = html.slice(html.indexOf('data-settings-section="system"'));
  for (const id of ['defaultArchiveRoot', 'archiveDb', 'crawlerRepo', 'dbDir', 'logDir', 'data-dir']) {
    assert.match(system, new RegExp(`\\sid="${id}"`), id);
  }
});

// ---- 実行画面に出す今の既定の 1 行。組んだ引数から作る（表示と実際のコマンドを食い違わせない）

const { buildArgs } = require('./commands');
const summaryOf = (form) => summarizeArgs(buildArgs({ archiveRoot: '/a', sourcePath: '/s', ...form }));
const keys = (items) => items.map((i) => i.key);
const byKey = (items, key) => items.find((i) => i.key === key);

test('取り込み: 空フォルダの削除は必ず出す（有効でも無効でも）。上限件数・メタデータも出す', () => {
  const on = summaryOf({ mode: 'add', pruneEmptyDirs: true });
  assert.ok(keys(on).includes('sum_prune_on'));
  assert.strictEqual(byKey(on, 'sum_prune_on').changed, false); // 取り込みの初期値は「削除する」
  const off = summaryOf({ mode: 'add', pruneEmptyDirs: false });
  assert.strictEqual(byKey(off, 'sum_prune_off').changed, true);
  assert.deepStrictEqual(byKey(on, 'sum_folder_limit').vars, { n: '500' });
  assert.ok(keys(on).includes('sum_meta_off'));
});

test('取り込み: 既定から変えた項目は changed。コマンドで既定と同じになる値（500.0 など）は変えていない扱い', () => {
  const same = summaryOf({ mode: 'add', pruneEmptyDirs: true, folderLimit: '500.0', minSize: '1.0' });
  assert.strictEqual(byKey(same, 'sum_folder_limit').changed, false);
  assert.deepStrictEqual(byKey(same, 'sum_folder_limit').vars, { n: '500' });
  assert.ok(!keys(same).includes('sum_min_size'));
  const changed = summaryOf({ mode: 'add', pruneEmptyDirs: true, folderLimit: '200', minSize: '10', withMeta: true, followSymlinks: true });
  assert.deepStrictEqual(byKey(changed, 'sum_folder_limit'), { key: 'sum_folder_limit', vars: { n: '200' }, changed: true });
  assert.deepStrictEqual(byKey(changed, 'sum_min_size'), { key: 'sum_min_size', vars: { n: '10' }, changed: true });
  assert.strictEqual(byKey(changed, 'sum_meta_on').changed, true);
  assert.strictEqual(byKey(changed, 'sum_symlinks_on').changed, true);
});

test('整合性チェック: 処置予定の印は必ず出し、コマンドに付く種類だけを出す（一覧に無い種類は出さない）', () => {
  assert.ok(keys(summaryOf({ mode: 'verify' })).includes('sum_flag_none'));
  const some = summaryOf({ mode: 'verify', flagQuarantine: ['missing', 'unknown-kind', 'hash_mismatch'] });
  assert.deepStrictEqual(byKey(some, 'sum_flag'), { key: 'sum_flag', vars: { kinds: 'missing, hash_mismatch' }, changed: true });
  assert.ok(keys(summaryOf({ mode: 'verify', flagQuarantine: ['unknown-kind'] })).includes('sum_flag_none'));
});

test('重複の後始末: 退避先と空フォルダの削除は必ず出す（退避先はコマンドに付いたときだけ。空の引用符は削除）', () => {
  const plain = summaryOf({ mode: 'delete-duplicates', trashDir: "''" });
  assert.ok(keys(plain).includes('sum_trash_none'));
  assert.strictEqual(byKey(plain, 'sum_prune_off').changed, false); // 後始末の初期値は「削除しない」
  const trash = summaryOf({ mode: 'delete-duplicates', trashDir: "'/t t'", pruneEmptyDirs: true });
  assert.deepStrictEqual(byKey(trash, 'sum_trash').vars, { path: '/t t' });
  assert.strictEqual(byKey(trash, 'sum_prune_on').changed, true);
});

test('既定を持たない機能・知らない引数は 1 行を出さない', () => {
  assert.deepStrictEqual(summaryOf({ mode: 'report' }), []);
  assert.deepStrictEqual(summaryOf({ mode: 'adopt' }), []);
  assert.deepStrictEqual(summarizeArgs(buildArgs({ mode: 'archives' })), []);
  assert.deepStrictEqual(summarizeArgs([]), []);
});

test('空フォルダの削除の初期値は index.html の checked と同じ（取り込みは有効・後始末は無効）', () => {
  assert.match(html, /id="pruneEmptyDirs" data-field="pruneEmptyDirs" checked/);
  assert.doesNotMatch(html, /id="delPruneEmptyDirs"[^>]*checked/);
});

test('必須の欄が空でも 1 行は出す（仮の値で組む）。値が不正で組めないときは出さない', () => {
  assert.ok(keys(previewDefaults({ mode: 'add', archiveRoot: '', sourcePath: '', pruneEmptyDirs: true })).includes('sum_prune_on'));
  assert.deepStrictEqual(previewDefaults({ mode: 'add', archiveRoot: '/a', sourcePath: '/s', folderLimit: '0' }), []);
});

test('画面（index.html）では読み込まない（main だけが使う）', () => {
  assert.doesNotMatch(html, /src="[^"]*defaults[^"]*\.js"/);
});

test('main のプレビューが 1 行の中身も返す（組んだ引数から作る）', () => {
  const main = fs.readFileSync(path.join(__dirname, 'main.js'), 'utf-8');
  const handler = main.slice(main.indexOf("ipcMain.handle('command:preview'"));
  const body = handler.slice(0, handler.indexOf('\n});'));
  assert.match(body, /defaults: summarizeArgs\(args\)/);
  assert.match(body, /defaults: previewDefaults\(form\)/);
});

test('必須の欄に空の引用符だけを貼っても、1 行は出す（正規化すると空なので仮の値で組む）', () => {
  assert.ok(keys(previewDefaults({ mode: 'add', archiveRoot: "''", sourcePath: '""', pruneEmptyDirs: true })).includes('sum_prune_on'));
});

test('実行画面の欄（実行 ID）が不正でも、退避先と空フォルダの削除は 1 行に出す（設定の値が不正なときだけ出さない）', () => {
  const items = previewDefaults({ mode: 'delete-duplicates', archiveRoot: '/a', ingestId: '1.5', trashDir: '' });
  assert.ok(keys(items).includes('sum_trash_none'));
  assert.ok(keys(items).includes('sum_prune_off'));
});
