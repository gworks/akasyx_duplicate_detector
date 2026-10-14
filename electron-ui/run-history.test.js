// run-history.test.js - 実行ボタンの実行の出力の履歴（#38）（node --test）
const test = require('node:test');
const assert = require('node:assert');

const { createRunHistory } = require('./run-history');

test('実行を終わった順に残し、上限を超えたら古いものから捨てる', () => {
  const h = createRunHistory({ limit: 3 });
  for (let id = 1; id <= 5; id += 1) h.add({ id, mode: 'verify', command: `c${id}`, result: { code: 0 } });
  assert.deepStrictEqual(h.list().map((e) => e.id), [3, 4, 5]);
});

test('上限はタブごと（ほかのタブの実行で、あるタブの履歴が押し出されない）', () => {
  const h = createRunHistory({ limit: 2 });
  h.add({ id: 1, mode: 'verify', command: 'v', result: { code: 0 } });
  for (let id = 2; id <= 6; id += 1) h.add({ id, mode: 'report', command: `r${id}`, result: { code: 0 } });
  assert.deepStrictEqual(h.list().map((e) => e.id), [1, 5, 6]);
});

test('残したものは後から変わらない（版は 1 つ。差分の送信で 1 度だけ送る）', () => {
  const h = createRunHistory();
  const entry = { id: 1, mode: 'report', command: 'c', result: { code: 0, output: { lines: ['a'], omitted: 0 } } };
  h.add(entry);
  entry.result.code = 9;
  const [kept] = h.list();
  assert.strictEqual(kept.result.code, 0);
  assert.strictEqual(kept.rev, 1);
});
