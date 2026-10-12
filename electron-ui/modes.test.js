// modes.test.js - タブのまとめ方と、機能が何を変えるかの見分け（#20）（node --test）
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { GROUPS, UI_MODES, MODE_INFO, tabEffect, argsEffect, sameEffect } = require('./renderer/modes');
const { MODES, buildArgs } = require('./commands');

const html = fs.readFileSync(path.join(__dirname, 'renderer', 'index.html'), 'utf-8');

test('すべての機能が、目的のまとまりのどれか 1 つにだけ入っている', () => {
  const grouped = GROUPS.flatMap((g) => g.modes);
  // 画面だけのタブ（かんたん取り込み。#22）は CLI のコマンドに無い
  assert.deepStrictEqual([...grouped].sort(), [...MODES, ...UI_MODES].sort());
  assert.ok(UI_MODES.every((m) => !MODES.includes(m)));
  assert.strictEqual(new Set(grouped).size, grouped.length);
  assert.deepStrictEqual(GROUPS.map((g) => g.id), ['ingest', 'check', 'setup']);
  // 機能ごとの表も、コマンドの一覧と同じものを持つ（足し忘れ・消し忘れを止める）
  assert.deepStrictEqual(Object.keys(MODE_INFO).sort(), [...MODES, ...UI_MODES].sort());
});

test('タブの並びは目的のまとまりの順で、見出しの中に入っている（index.html）', () => {
  // まとまりは data-tab-group の始まりから次のまとまり（無ければ </nav>）までで区切る（中に div が入っても切れない）
  const nav = html.slice(html.indexOf('<nav class="tabs"'), html.indexOf('</nav>'));
  const starts = [...nav.matchAll(/data-tab-group="([^"]+)"/g)];
  assert.deepStrictEqual(starts.map((m) => m[1]), GROUPS.map((g) => g.id), 'まとまりの並び');
  starts.forEach((m, i) => {
    const block = nav.slice(m.index, i + 1 < starts.length ? starts[i + 1].index : nav.length);
    const modes = [...block.matchAll(/data-mode="([^"]+)"/g)].map((x) => x[1]);
    assert.deepStrictEqual(modes, GROUPS[i].modes, `${m[1]} のタブ`);
  });
});

test('フォームの値を集める data-group は、タブの見出しに使わない（混ざるとコマンドが壊れる）', () => {
  const tags = [...html.matchAll(/<[^>]*\sdata-group="[^"]*"[^>]*>/g)].map((m) => m[0]);
  assert.ok(tags.length > 0);
  for (const tag of tags) assert.match(tag, /^<input /, tag);
});

test('タブの印: ファイルを動かす機能・DB を変える機能・見るだけの機能', () => {
  assert.strictEqual(tabEffect('quick'), 'files'); // かんたん取り込み（#22）も実際に取り込む
  assert.strictEqual(tabEffect('add'), 'files');
  assert.strictEqual(tabEffect('delete-duplicates'), 'files');
  assert.strictEqual(tabEffect('verify'), 'db');
  assert.strictEqual(tabEffect('adopt'), 'db');
  assert.strictEqual(tabEffect('archives'), 'db'); // 「登録を外す」が DB を変える
  assert.strictEqual(tabEffect('report'), 'none');
});

// 実行ボタンの印は、実際に組んだ引数から決める（フォームの生の値で決めると、正規化で落ちる指定と食い違う）
const effectOf = (form) => {
  const { effect, action } = argsEffect(buildArgs({ archiveRoot: '/a', sourcePath: '/s', ...form }));
  return { effect, action };
};

test('実行ボタンの印は、組んだ引数で実際に起きることで決まる', () => {
  assert.deepStrictEqual(effectOf({ mode: 'add' }), { effect: 'files', action: 'move' });
  assert.deepStrictEqual(effectOf({ mode: 'add', dryRun: true }), { effect: 'none', action: 'dry_run' });
  assert.deepStrictEqual(effectOf({ mode: 'verify' }), { effect: 'db', action: 'update_db' });
  assert.deepStrictEqual(effectOf({ mode: 'adopt' }), { effect: 'db', action: 'register' });
  assert.deepStrictEqual(effectOf({ mode: 'report' }), { effect: 'none', action: 'view' });
  assert.deepStrictEqual(effectOf({ mode: 'archives' }), { effect: 'none', action: 'view' });
  assert.deepStrictEqual(
    effectOf({ mode: 'archives', forgetId: '3', forgetUid: 'a'.repeat(32) }),
    { effect: 'db', action: 'forget' }
  );
  // 値が --forget で始まっても（正本 DB のパスなど）、一覧を出すだけの実行を「登録を外す」にしない
  assert.deepStrictEqual(effectOf({ mode: 'archives', archiveDb: '--forget-backup.db' }), { effect: 'none', action: 'view' });
});

test('重複の後始末: --yes が無くても、投入元が無い重複の記録は DB に書く（見るだけではない）', () => {
  assert.deepStrictEqual(effectOf({ mode: 'delete-duplicates' }), { effect: 'db', action: 'check' });
  assert.deepStrictEqual(effectOf({ mode: 'delete-duplicates', trashDir: '/t' }), { effect: 'db', action: 'check' });
});

test('重複の後始末: --yes で削除。退避先は、引数に --trash-dir が付いたときだけ「退避」', () => {
  assert.deepStrictEqual(effectOf({ mode: 'delete-duplicates', yes: true }), { effect: 'files', action: 'delete' });
  assert.deepStrictEqual(
    effectOf({ mode: 'delete-duplicates', yes: true, trashDir: '/t' }),
    { effect: 'files', action: 'trash' }
  );
  // 空の引用符（ターミナルから貼ったもの）は正規化で空になり、--trash-dir が付かない＝削除
  for (const trashDir of ["''", '""', '   ']) {
    assert.deepStrictEqual(
      effectOf({ mode: 'delete-duplicates', yes: true, trashDir }),
      { effect: 'files', action: 'delete' },
      trashDir
    );
  }
});

test('押したときの印と実行の印の照合: 両方あって、色と文言が同じときだけ同じとみなす', () => {
  const move = { mode: 'add', effect: 'files', action: 'move' };
  assert.strictEqual(sameEffect(move, { mode: 'add', effect: 'files', action: 'move' }), true);
  assert.strictEqual(sameEffect(move, { mode: 'add', effect: 'files', action: 'delete' }), false);
  assert.strictEqual(sameEffect(move, { mode: 'add', effect: 'none', action: 'move' }), false);
  // 押せない（無効な）状態で押したときは印が無い。同じとはみなさない
  assert.strictEqual(sameEffect(null, move), false);
  assert.strictEqual(sameEffect(move, null), false);
  assert.strictEqual(sameEffect(undefined, undefined), false);
});

test('印には機能も入り、色と文言が同じでも機能が違えば同じとみなさない（タブを替えた直後の予約を通さない）', () => {
  const report = argsEffect(buildArgs({ mode: 'report', archiveRoot: '/a' }));
  const archives = argsEffect(buildArgs({ mode: 'archives' }));
  assert.strictEqual(report.mode, 'report');
  assert.strictEqual(archives.mode, 'archives');
  assert.deepStrictEqual([report.effect, report.action], [archives.effect, archives.action]);
  assert.strictEqual(sameEffect(report, archives), false);
  assert.strictEqual(sameEffect(report, argsEffect(buildArgs({ mode: 'report', archiveRoot: '/b' }))), true);
});

test('知らない機能は「見るだけ」と言わない（何が起きるか分からない）', () => {
  assert.deepStrictEqual(argsEffect(['something-new', '/a']), { mode: 'something-new', effect: 'unknown', action: 'unknown' });
  assert.deepStrictEqual(argsEffect([]), { mode: undefined, effect: 'unknown', action: 'unknown' });
  assert.strictEqual(tabEffect('something-new'), 'unknown');
  assert.strictEqual(tabEffect('toString'), 'unknown'); // 継承したプロパティを機能と取り違えない
});

test('かんたん取り込み（#22）は「取り込む」の先頭', () => {
  assert.deepStrictEqual(GROUPS[0].modes[0], 'quick');
});
