// commands.test.js - パス欄の正規化（node --test）
const test = require('node:test');
const assert = require('node:assert');

const { buildArgs, normalizePath } = require('./commands');

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
