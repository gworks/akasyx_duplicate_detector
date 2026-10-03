// detector-output.test.js - detector の出力から UI が拾うもの（node --test）
// 文言の正本は detector 側。detector/tests/test_archives_ui.py が同じ正規表現で実際の出力を確かめている
const test = require('node:test');
const assert = require('node:assert');

const { scanLine, parseArchivesJson } = require('./detector-output');

test('進捗行と CSV の行', () => {
  assert.deepStrictEqual(scanLine('Progress: 12 files (moved)'), { progress: { processed: 12, result: 'moved' } });
  assert.deepStrictEqual(scanLine('2026-09-28 INFO CSV report: /x/a.csv'), { csvPath: '/x/a.csv' });
});

test('Note: 行から登録を外す候補の保存フォルダ ID を拾う', () => {
  const line = 'Note: 3 duplicates are only in archive folder #7 (/Volumes/nas/a), which is not available now. '
    + 'If it was deleted, run `archives --forget 7` and add again.';
  assert.deepStrictEqual(scanLine(line), { forgetId: 7 });
  // ログ（stderr の WARNING 行）に同じ文が混ざっても拾わない（stdout の行頭だけ）
  assert.deepStrictEqual(scanLine(`2026-09-28 WARNING ${line}`), {});
});

test('adopt の案内から保存フォルダを拾う', () => {
  assert.deepStrictEqual(scanLine('  Register the files in it first: adopt /Users/a b/photos'), { adoptPath: '/Users/a b/photos' });
  assert.deepStrictEqual(scanLine('  Run adopt again: adopt /x'), { adoptPath: '/x' });
  // 識別子のある（別の正本 DB で使っていたかもしれない）フォルダの断り。対処は --archive-db の指定なので導線を出さない
  assert.deepStrictEqual(scanLine('  If that master DB is lost, register the files in it again: adopt /x'), {});
  assert.deepStrictEqual(scanLine('  Fix the permissions (or remove these) and run adopt again.'), {});
});

test('archives --json を読む。読めないものは空の一覧にせずエラーにする', () => {
  const ok = parseArchivesJson('{"master_db":"/d","archives":[{"id":1,"root":"/a","present":false,"stored_files":2}]}\n');
  assert.strictEqual(ok.archives.length, 1);
  assert.strictEqual(ok.masterDb, '/d');
  assert.strictEqual(ok.dbExists, true);
  const none = parseArchivesJson('{"master_db":"/d","db_exists":false,"archives":[]}');
  assert.strictEqual(none.dbExists, false);
  for (const bad of ['', 'not json', '{"archives":null}', '[]']) {
    assert.throws(() => parseArchivesJson(bad), Error, JSON.stringify(bad));
  }
});
