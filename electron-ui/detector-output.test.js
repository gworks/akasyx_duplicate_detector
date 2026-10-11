// detector-output.test.js - detector の出力から UI が拾うもの（node --test）
// 文言の正本は detector 側。detector/tests/test_archives_ui.py が同じ正規表現で実際の出力を確かめている
const test = require('node:test');
const assert = require('node:assert');

const { scanLine, parseArchivesJson, createRunState, FAILURE_LIST_LIMIT } = require('./detector-output');

test('進捗行と CSV の行', () => {
  // 全体の件数つき（#18）と、数えられなかったときの旧形式の両方を読む
  assert.deepStrictEqual(scanLine('Progress: 12/30 files (moved)'), { progress: { processed: 12, total: 30, result: 'moved' } });
  assert.deepStrictEqual(scanLine('Progress: 12 files (moved)'), { progress: { processed: 12, total: null, result: 'moved' } });
  assert.deepStrictEqual(scanLine('2026-09-28 10:00:00,123 INFO ingest: CSV report: /x/a.csv'), { csvPath: '/x/a.csv' });
});

test('ファイル名やエラー文に同じ語が入った警告の行からは、CSV の場所も adopt の案内も拾わない', () => {
  // 失敗の WARNING（Failed: パス: メッセージ）は任意のファイル名とエラー文を含む
  assert.deepStrictEqual(scanLine('2026-10-11 10:00:00,123 WARNING ingest: Failed: /s/x CSV report: /evil.csv: boom'), {});
  assert.deepStrictEqual(scanLine('2026-10-11 10:00:00,123 WARNING ingest: Failed: /s/Run adopt again: adopt /evil: boom'), {});
});

test('段階・全体の件数・処理中のファイル・失敗の行（#18）', () => {
  assert.deepStrictEqual(scanLine('Stage: scan'), { stage: 'scan' });
  assert.deepStrictEqual(scanLine('Stage: ingest'), { stage: 'ingest' });
  assert.deepStrictEqual(scanLine('Stage: cleanup'), { stage: 'cleanup' });
  assert.deepStrictEqual(scanLine('Stage: done'), { stage: 'done' });
  assert.deepStrictEqual(scanLine('Stage: other'), {});
  assert.deepStrictEqual(scanLine('Total: 1572 files'), { total: 1572 });
  // パスは JSON（改行や `: ` を含む名前・ASCII に落とした日本語）
  assert.deepStrictEqual(scanLine('Current file: {"path": "/a/b: c\\nd \\u5199.txt"}'), { current: '/a/b: c\nd 写.txt' });
  assert.deepStrictEqual(
    scanLine('Failed: {"path": "/a/x.CR2", "message": "[Errno 22] Invalid argument"}'),
    { failed: { path: '/a/x.CR2', message: '[Errno 22] Invalid argument' } },
  );
  // 壊れた JSON・形の違うものは拾わない（ログ行として残る）
  assert.deepStrictEqual(scanLine('Current file: {broken'), {});
  assert.deepStrictEqual(scanLine('Failed: {"message": "no path"}'), {});
  // ログ（stderr）の WARNING 行に同じ語が混ざっても拾わない（行頭だけ）
  assert.deepStrictEqual(scanLine('2026-10-10 WARNING mover: Failed: {"path": "/a"}'), {});
});

test('crawler の進捗行を一覧作成の段階の進み具合として読む', () => {
  assert.deepStrictEqual(scanLine('Processing files: 1556/1572 (99.0%)'), { scanProgress: { done: 1556, total: 1572 } });
  assert.deepStrictEqual(scanLine('Processing files: 12/unknown'), { scanProgress: { done: 12, total: null } });
});

function clock(start = 0) {
  let now = start;
  return { now: () => now, advance: (ms) => { now += ms; } };
}

test('集計: 段階・件数・結果ごとの件数・処理中のファイル', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  assert.strictEqual(state.apply(scanLine('Stage: scan')), true);
  state.apply(scanLine('Processing files: 2/3 (66.7%)'));
  assert.deepStrictEqual(state.snapshot().scan, { done: 2, total: 3 });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 3 files'));
  state.apply(scanLine('Current file: {"path": "/s/a.txt"}'));
  let snap = state.snapshot();
  assert.strictEqual(snap.stage, 'ingest');
  assert.strictEqual(snap.total, 3);
  assert.strictEqual(snap.processed, 0);
  assert.strictEqual(snap.current, '/s/a.txt');
  state.apply(scanLine('Progress: 1/3 files (moved)'));
  state.apply(scanLine('Failed: {"path": "/s/b.txt", "message": "boom"}'));
  state.apply(scanLine('Progress: 2/3 files (failed)'));
  snap = state.snapshot();
  assert.strictEqual(snap.processed, 2);
  assert.deepStrictEqual(snap.tally, { moved: 1, failed: 1 });
  assert.deepStrictEqual(snap.failures, [{ path: '/s/b.txt', message: 'boom' }]);
  state.apply(scanLine('Stage: cleanup'));
  assert.strictEqual(state.snapshot().current, null); // 後片付けの段階では処理中のファイルは無い
  // 進捗に関係ない行は拾わない（ログに回す）
  assert.strictEqual(state.apply(scanLine('2026-10-10 INFO something')), false);
  // snapshot は中身の写し（後から変えても前に返したものは変わらない）
  const before = state.snapshot();
  state.apply(scanLine('Progress: 3/3 files (moved)'));
  assert.deepStrictEqual(before.tally, { moved: 1, failed: 1 });
});

test('全体の件数が分からないときは割合も残り時間も出さない', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  for (let i = 1; i <= 5; i += 1) {
    c.advance(20000);
    state.apply(scanLine(`Progress: ${i} files (moved)`));
  }
  const snap = state.snapshot();
  assert.strictEqual(snap.total, null);
  assert.strictEqual(snap.etaSeconds, null);
});

test('残り時間は直近の速さから出す。材料が足りないうちは出さない', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 100 files'));
  c.advance(20000);
  state.apply(scanLine('Progress: 1/100 files (moved)'));
  assert.strictEqual(state.snapshot().etaSeconds, null); // 1 件だけでは出さない
  c.advance(20000);
  state.apply(scanLine('Progress: 2/100 files (moved)'));
  // 2 件に 40 秒 → 1 件 20 秒、残り 98 件で 1960 秒
  assert.strictEqual(state.snapshot().etaSeconds, 1960);
  // 遅くなったら、古い速さを引きずらずに直近の速さに寄せる（10 分より前は捨てる）
  for (let i = 3; i <= 40; i += 1) {
    c.advance(60000);
    state.apply(scanLine(`Progress: ${i}/100 files (moved)`));
  }
  assert.strictEqual(state.snapshot().etaSeconds, 60 * 60); // 1 件 60 秒 × 残り 60 件
  state.apply(scanLine('Progress: 100/100 files (moved)'));
  assert.strictEqual(state.snapshot().etaSeconds, 0);
});

test('失敗の一覧は上限までで、超えた分は件数だけ数える', () => {
  const state = createRunState({ now: () => 0 });
  for (let i = 0; i < FAILURE_LIST_LIMIT + 3; i += 1) {
    state.apply(scanLine(`Failed: {"path": "/s/${i}", "message": "x"}`));
  }
  const snap = state.snapshot();
  assert.strictEqual(snap.failures.length, FAILURE_LIST_LIMIT);
  assert.strictEqual(snap.failuresOmitted, 3);
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

test('残り時間は送るたびに今の時刻で計算し直す（大きいファイル 1 件の間も止まらない）', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 100 files'));
  c.advance(20000);
  state.apply(scanLine('Progress: 1/100 files (moved)'));
  c.advance(20000);
  state.apply(scanLine('Progress: 2/100 files (moved)'));
  assert.strictEqual(state.snapshot().etaSeconds, 1960);
  // 次の行が来ないまま 10 秒たった（3 件目を処理中）
  c.advance(10000);
  assert.strictEqual(state.snapshot().etaSeconds, 1950);
  // 1 件ぶん以上かかっていても、残りのファイルぶんより小さくはしない（0 に向かって減り続けない）
  c.advance(60000);
  assert.strictEqual(state.snapshot().etaSeconds, 1940);
  assert.strictEqual(state.ticking(), true);
  state.apply(scanLine('Stage: cleanup'));
  assert.strictEqual(state.ticking(), false);
});

test('残り時間用の記録は 1 秒に 1 点までで、速い取り込みでも増え続けない', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 100000 files'));
  for (let i = 1; i <= 50000; i += 1) {
    c.advance(10); // 1 件 10 ミリ秒（10 分の窓に 6 万件入る速さ）
    state.apply(scanLine(`Progress: ${i}/100000 files (moved)`));
  }
  assert.ok(state.sampleCount() <= 602, `samples: ${state.sampleCount()}`);
  assert.strictEqual(state.snapshot().etaSeconds, 500); // 残り 5 万件 × 10 ミリ秒
});

test('失敗の一覧は変わったときだけ送れる（件数はいつも入る）', () => {
  const state = createRunState({ now: () => 0 });
  state.apply(scanLine('Failed: {"path": "/s/a", "message": "x"}'));
  const light = state.snapshot({ failures: false });
  assert.strictEqual('failures' in light, false);
  assert.strictEqual(light.failureCount, 1);
  const full = state.snapshot();
  assert.deepStrictEqual(full.failures, [{ path: '/s/a', message: 'x' }]);
  assert.strictEqual(full.failureCount, 1);
});

test('通った段階を覚える（単一ファイルは一覧作成を通らない）', () => {
  const state = createRunState({ now: () => 0 });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Stage: cleanup'));
  assert.deepStrictEqual(state.snapshot().stagesSeen, ['ingest', 'cleanup']);
});

test('crawler の進捗は、取り込みの一覧作成の段階でだけ集計する（ほかのコマンドではログに残す）', () => {
  const state = createRunState({ now: () => 0 });
  // verify・adopt は段階を出さない。crawler の 100% を完了と見間違えないよう、今までどおりログに出す
  assert.strictEqual(state.apply(scanLine('Processing files: 3/3 (100.0%)')), false);
  assert.strictEqual(state.snapshot().scan, null);
  assert.strictEqual(state.inScan(), false);
  state.apply(scanLine('Stage: scan'));
  assert.strictEqual(state.inScan(), true);
  assert.strictEqual(state.apply(scanLine('Processing files: 1/3 (33.3%)')), true);
  state.apply(scanLine('Stage: ingest'));
  assert.strictEqual(state.inScan(), false);
  assert.strictEqual(state.apply(scanLine('Processing files: 2/3 (66.7%)')), false);
});

test('速い取り込みの後に遅くなったら、残り時間は直近 10 分の速さに寄る（記録が 2 点に潰れない）', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 100000 files'));
  let i = 0;
  // 20 分間は 1 件 0.1 秒（行は 1 秒より短い間隔で届く）
  for (; i < 12000; ) {
    i += 1;
    c.advance(100);
    state.apply(scanLine(`Progress: ${i}/100000 files (moved)`));
  }
  assert.ok(state.sampleCount() > 100, `samples: ${state.sampleCount()}`);
  // その後 10 分以上、1 件 1 秒に落ちた
  for (let k = 0; k < 700; k += 1) {
    i += 1;
    c.advance(1000);
    state.apply(scanLine(`Progress: ${i}/100000 files (moved)`));
  }
  // 窓の中は全部 1 件 1 秒なので、残りの件数ぶんの秒数になる（全体の平均の速さだと大きく外れる）
  const remaining = 100000 - i;
  const eta = state.snapshot().etaSeconds;
  assert.ok(Math.abs(eta - remaining) <= remaining * 0.02, `eta ${eta} / remaining ${remaining}`);
});

test('上限を超えた後の失敗では、一覧の件数は変わらない（一覧を送り直さない）', () => {
  const state = createRunState({ now: () => 0 });
  for (let i = 0; i < FAILURE_LIST_LIMIT; i += 1) state.apply(scanLine(`Failed: {"path": "/s/${i}", "message": "x"}`));
  assert.strictEqual(state.failureListSize(), FAILURE_LIST_LIMIT);
  state.apply(scanLine('Failed: {"path": "/s/over", "message": "x"}'));
  assert.strictEqual(state.failureListSize(), FAILURE_LIST_LIMIT);
  // 上限を超えた件数は一覧なしの写しにも入る
  const light = state.snapshot({ failures: false });
  assert.strictEqual(light.failuresOmitted, 1);
  assert.strictEqual(light.failureCount, FAILURE_LIST_LIMIT + 1);
});

test('残り時間のタイマーは、判定と移動の段階に入ってから要る', () => {
  const state = createRunState({ now: () => 0 });
  assert.strictEqual(state.ticking(), false);
  state.apply(scanLine('Stage: scan'));
  assert.strictEqual(state.ticking(), false);
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 3 files'));
  assert.strictEqual(state.ticking(), true);
});

test('残り時間の起点は処理を始める直前（Total: 行）。判定の準備にかかった時間を 1 件あたりの時間に入れない', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  c.advance(30000); // 保存フォルダの照合などの準備
  state.apply(scanLine('Total: 100 files'));
  for (let i = 1; i <= 100; i += 1) {
    if (i > 50) break;
    c.advance(1000);
    state.apply(scanLine(`Progress: ${i}/100 files (moved)`));
  }
  assert.strictEqual(state.snapshot().etaSeconds, 50); // 1 件 1 秒 × 残り 50 件
});

test('長くかかった 1 件の後は、窓の中の点だけで速さを出す（窓より前の古い点に引きずられない）', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 2000 files'));
  c.advance(40 * 60 * 1000); // 1 件目に 40 分
  state.apply(scanLine('Progress: 1/2000 files (moved)'));
  for (let i = 2; i <= 101; i += 1) {
    c.advance(1000); // その後は 1 件 1 秒
    state.apply(scanLine(`Progress: ${i}/2000 files (moved)`));
  }
  const eta = state.snapshot().etaSeconds;
  assert.ok(Math.abs(eta - 1899) <= 20, `eta ${eta}`); // 残り 1899 件 × 1 秒
});

test('crawler が終わったら（失敗しても）、それより後の行は crawler のものとして扱わない', () => {
  const state = createRunState({ now: () => 0 });
  state.apply(scanLine('Stage: scan'));
  assert.strictEqual(state.inScan(), true);
  assert.strictEqual(state.apply(scanLine('Crawler finished')), true);
  assert.strictEqual(state.inScan(), false);
  assert.strictEqual(state.snapshot().stage, 'scan'); // 段階は一覧作成のまま（判定には入っていない）
  assert.strictEqual(state.apply(scanLine('Processing files: 3/3 (100.0%)')), false);
});

test('確定した点どうしは 1 秒以上離れる（届く間隔がばらついても）', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 100000 files'));
  // 0.9 秒と 0.1 秒の間隔が交互に来る（平均 2 件 / 秒）
  for (let i = 1; i <= 2000; i += 1) {
    c.advance(i % 2 ? 900 : 100);
    state.apply(scanLine(`Progress: ${i}/100000 files (moved)`));
  }
  // 1000 秒たったので、確定した点は 1 秒に 1 点以下（窓 600 秒 + 基準と仮の点）
  assert.ok(state.sampleCount() <= 603, `samples: ${state.sampleCount()}`);
});

test('判定のループを抜けたら（中断でも）処理中のファイルと残り時間を止める', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 100 files'));
  state.apply(scanLine('Current file: {"path": "/s/a"}'));
  state.apply(scanLine('Progress: 1/100 files (moved)'));
  assert.strictEqual(state.ticking(), true);
  assert.strictEqual(state.apply(scanLine('Ingest finished')), true);
  assert.strictEqual(state.ticking(), false);
  const snap = state.snapshot();
  assert.strictEqual(snap.current, null);
  assert.strictEqual(snap.etaSeconds, null);
  assert.strictEqual(snap.stage, 'ingest'); // 段階は進めない（判定が済んだとは detector が言っていない）
});

test('判定のループを抜けたことは写しに入る（後片付けの間に残り時間・実行中の印を出さない）', () => {
  const state = createRunState({ now: () => 0 });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 10 files'));
  assert.strictEqual(state.snapshot().ingestEnded, false);
  state.apply(scanLine('Ingest finished'));
  assert.strictEqual(state.snapshot().ingestEnded, true);
});

test('残り時間の起点は、1 件目を取り出したとき（最初の Current file）。一覧の読み出し・並べ替えの時間を入れない', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 100 files'));
  c.advance(60000); // 一覧の読み出し（ORDER BY）に 1 分
  for (let i = 1; i <= 50; i += 1) {
    state.apply(scanLine(`Current file: {"path": "/s/${i}"}`));
    c.advance(1000);
    state.apply(scanLine(`Progress: ${i}/100 files (moved)`));
  }
  assert.strictEqual(state.snapshot().etaSeconds, 50);
});

test('最後の 1 件が平均より長くかかっているときは、残り時間を「もうすぐ」と出さない（計算中に戻す）', () => {
  const c = clock();
  const state = createRunState({ now: c.now });
  state.apply(scanLine('Stage: ingest'));
  state.apply(scanLine('Total: 10 files'));
  for (let i = 1; i <= 9; i += 1) {
    c.advance(2000);
    state.apply(scanLine(`Progress: ${i}/10 files (moved)`));
  }
  assert.strictEqual(state.snapshot().etaSeconds, 2); // 最後の 1 件の目安（1 件 2 秒）
  c.advance(30 * 60 * 1000); // 最後の 1 件に 30 分
  assert.strictEqual(state.snapshot().etaSeconds, null);
});

test('crawler が動いている間は、crawler の進捗と Crawler finished だけを読む（同じ形の行を detector のものと取り違えない）', () => {
  const state = createRunState({ now: () => 0 });
  state.apply(scanLine('Stage: scan'));
  assert.strictEqual(state.apply(scanLine('Total: 1234 files')), false);
  assert.strictEqual(state.apply(scanLine('Progress: 5/9 files (moved)')), false);
  assert.strictEqual(state.snapshot().total, null);
  assert.strictEqual(state.snapshot().processed, 0);
  assert.strictEqual(state.apply(scanLine('Processing files: 3/9 (33.3%)')), true);
  assert.strictEqual(state.apply(scanLine('Crawler finished')), true);
  assert.strictEqual(state.apply(scanLine('Stage: ingest')), true);
  assert.strictEqual(state.apply(scanLine('Total: 9 files')), true);
  assert.strictEqual(state.snapshot().total, 9);
});

test('警告・エラーの行の数を数える（開き直す前に出た警告も、終わったときに詳細ログを開く判断に使う）', () => {
  const { countsAsWarning } = require('./detector-output');
  assert.strictEqual(countsAsWarning('2026-10-11 10:00:00,123 WARNING ingest: Skipped the detector own data: /x'), true);
  assert.strictEqual(countsAsWarning('2026-10-11 10:00:00,123 ERROR mover: Move failed but the file exists'), true);
  assert.strictEqual(countsAsWarning('Note: 3 duplicates are only in archive folder #7 (/v), which is not available now.'), true);
  assert.strictEqual(countsAsWarning('Error: Crawler scan did not complete'), true);
  assert.strictEqual(countsAsWarning('2026-10-11 10:00:00,123 INFO ingest: CSV report: /x.csv'), false);
  assert.strictEqual(countsAsWarning('[Summary] moved: 3'), false);
});
