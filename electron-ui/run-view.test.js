// run-view.test.js - 実行中に他のタブを開いたときの見せ方（#19）（node --test）
const test = require('node:test');
const assert = require('node:assert');

const { runView } = require('./renderer/run-view');

const base = { mode: 'add', runMode: 'add', running: true, stopRequested: false, finishedUnseen: false };

test('実行したタブでは出力と中断をパネルに出し、帯は出さない（中断・経過時間が 2 つ並ばない）', () => {
  const v = runView(base);
  assert.strictEqual(v.showOutput, true);
  assert.strictEqual(v.showElsewhere, false);
  assert.strictEqual(v.paneStop, true);
  assert.strictEqual(v.paneStopEnabled, true);
  assert.strictEqual(v.paneSpinner, true);
  assert.strictEqual(v.runbar, null);
});

test('実行中に他のタブを見ているときは出力を隠し、隣の実行ボタンに中断を並べない', () => {
  const v = runView({ ...base, mode: 'report' });
  assert.strictEqual(v.showOutput, false);
  assert.strictEqual(v.showElsewhere, true);
  assert.strictEqual(v.paneStop, false); // 別のコマンドの実行ボタンの隣に、押せない中断を並べない
  assert.strictEqual(v.paneSpinner, false);
  assert.strictEqual(v.runbar, 'running');
  assert.strictEqual(v.runbarGo, true);
  assert.strictEqual(v.runbarStopEnabled, true);
});

test('中断を頼んだ後は、どちらの中断も押せない', () => {
  for (const mode of ['add', 'verify']) {
    const v = runView({ ...base, mode, stopRequested: true });
    assert.strictEqual(v.paneStopEnabled, false);
    assert.strictEqual(v.runbarStopEnabled, false);
  }
});

test('他のタブにいる間に終わったら、見に行くまで帯に終わったことを残す', () => {
  const v = runView({ ...base, mode: 'report', running: false, finishedUnseen: true });
  assert.strictEqual(v.runbar, 'finished');
  assert.strictEqual(v.runbarGo, true);
  assert.strictEqual(v.showOutput, false);
  assert.strictEqual(v.showElsewhere, true);
  assert.strictEqual(v.paneStop, false);
});

test('終わった後は、実行したタブでだけ出力を見せる（他のタブの案内は残す）', () => {
  const v = runView({ ...base, mode: 'verify', running: false });
  assert.strictEqual(v.runbar, null);
  assert.strictEqual(v.showOutput, false);
  assert.strictEqual(v.showElsewhere, true);
  assert.deepStrictEqual(
    [runView({ ...base, running: false }).showOutput, runView({ ...base, running: false }).showElsewhere],
    [true, false]
  );
});

test('出力が無い（まだ実行していない・クリアした）ときは、どのタブでも出力の場所を出す', () => {
  for (const mode of ['add', 'archives']) {
    const v = runView({ ...base, mode, runMode: null, running: false });
    assert.strictEqual(v.showOutput, true);
    assert.strictEqual(v.showElsewhere, false);
    assert.strictEqual(v.runbar, null);
  }
});

test('実行したタブを開いていれば、終わった印の帯は出さない', () => {
  const v = runView({ ...base, running: false, finishedUnseen: true });
  assert.strictEqual(v.runbar, null);
});

test('始まったと main が応えるまでは中断を押せない（子プロセスが無いと main は中断を無視する）', () => {
  for (const mode of ['add', 'report']) {
    const v = runView({ ...base, mode, starting: true });
    assert.strictEqual(v.paneStopEnabled, false);
    assert.strictEqual(v.runbarStopEnabled, false);
  }
});
