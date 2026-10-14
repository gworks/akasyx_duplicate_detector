// list-sync.test.js - 列・履歴の写しを差分で送る（#36）（node --test）
const test = require('node:test');
const assert = require('node:assert');

const { createListSync, receiveList, createResync } = require('./renderer/list-sync');
const { createQueue } = require('./quick-queue');

const view = (i) => ({ id: i.id, status: i.status, result: i.result });
const empty = () => ({ seq: -1, items: [], meta: null });

/** 写しを受け取り続ける画面（開いたときに全体を問い合わせ、飛びがあれば問い合わせ直す）。 */
function screen(sync, source) {
  let state = receiveList(empty(), sync.full(source.list(), source.meta())).state;
  let resyncs = 0;
  return {
    get: () => state,
    resyncs: () => resyncs,
    take(msg) {
      if (!msg) return;
      const r = receiveList(state, msg);
      state = r.state;
      if (r.resync) {
        resyncs += 1;
        state = receiveList(state, sync.full(source.list(), source.meta())).state;
      }
    },
  };
}

function queueSource() {
  const q = createQueue({ keepDone: 3 });
  return { q, list: () => q.list(), meta: () => ({ paused: q.isPaused() }) };
}

test('差分を順に当てた結果は、全体の写しと一致する（追加・実行中・完了・取り消し・先頭に戻す・古い完了を捨てる・一時停止）', () => {
  const src = queueSource();
  const sync = createListSync(view);
  const s = screen(sync, src);
  const send = () => s.take(sync.diff(src.list(), src.meta()));
  const check = () => {
    const full = receiveList(empty(), createListSync(view).full(src.list(), src.meta())).state;
    assert.deepStrictEqual(s.get().items, full.items);
    assert.deepStrictEqual(s.get().meta, full.meta);
  };
  const { q } = src;
  const [a, b, c] = q.add([{ source: '/a' }, { source: '/b' }, { source: '/c' }]);
  send(); check();
  q.markRunning(a.id, 1); send(); check();
  q.markDone(a.id, { code: 0 }); send(); check();
  q.cancel(c.id); send(); check();
  q.markRunning(b.id, 2); send(); check();
  q.markDone(b.id, { code: 0, stoppedByUser: true }); q.addFront([{ source: '/b2' }]); q.add([{ source: '/d' }]); q.pause();
  send(); check();
  assert.deepStrictEqual(s.get().items.map((i) => i.status), ['done', 'done', 'waiting', 'waiting']);
  assert.deepStrictEqual(s.get().meta, { paused: true });
  q.resume(); send(); check();
  for (const i of q.list().filter((x) => x.status === 'waiting')) {
    q.markRunning(i.id, 9);
    q.markDone(i.id, { code: 0 });
    send(); check();
  }
  assert.strictEqual(s.get().items.length, 3); // keepDone を超えた古い完了の行は消える
  q.add([{ source: '/e' }]); q.discardWaiting(); send(); check();
  assert.strictEqual(s.resyncs(), 0);
});

test('完了の行が 20 件ある状態で行が 1 つ終わっても、送るのはその行の分だけ（終わった行の結果は 1 度だけ送る）', () => {
  const q = createQueue();
  const sync = createListSync(view);
  const meta = () => ({ paused: q.isPaused() });
  for (let n = 0; n < 21; n += 1) {
    const [i] = q.add([{ source: `/f${n}` }]);
    q.markRunning(i.id, n);
    q.markDone(i.id, { code: 0, failures: Array.from({ length: 100 }, (_, k) => ({ path: `/f${n}/${k}` })) });
  }
  const [last] = q.add([{ source: '/last' }]);
  q.markRunning(last.id, 99);
  sync.diff(q.list(), meta());
  q.markDone(last.id, { code: 0 });
  const msg = sync.diff(q.list(), meta());
  assert.deepStrictEqual(msg.upserts.map((i) => i.id), [last.id]);
  assert.strictEqual(msg.removed.length, 1); // 20 件を超えた一番古い完了の行
  assert.strictEqual(msg.order.length, 20);
  assert.strictEqual(sync.diff(q.list(), meta()), null); // 変わっていなければ送らない
});

test('一時停止だけが変わったときも送る', () => {
  const q = createQueue();
  const sync = createListSync(view);
  q.add([{ source: '/a' }]);
  sync.diff(q.list(), { paused: q.isPaused() });
  q.pause();
  const msg = sync.diff(q.list(), { paused: q.isPaused() });
  assert.deepStrictEqual(msg.upserts, []);
  assert.deepStrictEqual(msg.meta, { paused: true });
});

test('列のどの操作も、変わった行の版を上げる（上げないと差分に入らない）', () => {
  const q = createQueue();
  const [a] = q.add([{ source: '/a' }]);
  const rev = () => q.list().find((i) => i.id === a.id).rev;
  const r0 = rev();
  q.markRunning(a.id, 1);
  const r1 = rev();
  assert.ok(r1 > r0);
  q.markDone(a.id, { code: 0 });
  assert.ok(rev() > r1);
});

test('写しが飛んだら（窓を閉じていた・順が入れ替わった）全体を問い合わせ、古い写しは当てない', () => {
  const src = queueSource();
  const sync = createListSync(view);
  src.q.add([{ source: '/a' }]);
  const first = sync.diff(src.list(), src.meta());
  src.q.add([{ source: '/b' }]);
  const second = sync.diff(src.list(), src.meta());
  // 1 つ目を取りこぼした
  let state = receiveList(empty(), sync.full(src.list(), src.meta())).state;
  const r = receiveList(state, second); // 全体の写しより古い差分
  assert.strictEqual(r.resync, false);
  assert.strictEqual(r.state, state);
  // 全体の写しを持たない画面が、途中の差分を受けた
  const gap = receiveList(empty(), second);
  assert.strictEqual(gap.resync, true);
  assert.deepStrictEqual(gap.state.items, []);
  // 全体の写しの応答より先に、その後の差分が届いた（飛びとして問い合わせ直す）。遅れて届いた古い全体の写しは当てない
  const old = sync.full(src.list(), src.meta());
  src.q.add([{ source: '/c' }]);
  assert.strictEqual(receiveList(state, sync.diff(src.list(), src.meta())).resync, true);
  state = receiveList(state, sync.full(src.list(), src.meta())).state;
  assert.strictEqual(receiveList(state, old).state, state);
  assert.strictEqual(state.items.length, 3);
  assert.ok(first.seq < second.seq);
});

test('全体の写しを問い合わせている間に飛んだ差分が届いても、その差分を取りこぼさない（もう 1 度問い合わせる）', async () => {
  const src = queueSource();
  const sync = createListSync(view);
  let state = receiveList(empty(), sync.full(src.list(), src.meta())).state;
  let fetches = 0;
  let release;
  const fetchFull = () => {
    fetches += 1;
    const snap = sync.full(src.list(), src.meta()); // 頼まれた時点の写し（答えが届くのは後）
    return fetches === 1 ? new Promise((r) => { release = () => r(snap); }) : Promise.resolve(snap);
  };
  const take = (msg) => {
    const r = receiveList(state, msg);
    state = r.state;
    if (r.resync) resync(msg.seq);
  };
  const resync = createResync(fetchFull, take, () => {}, () => state.seq);
  src.q.add([{ source: '/a' }]);
  sync.diff(src.list(), src.meta()); // 取りこぼした
  src.q.add([{ source: '/b' }]);
  take(sync.diff(src.list(), src.meta())); // 飛び → 問い合わせ（答えはまだ）
  await new Promise((r) => setImmediate(r));
  src.q.add([{ source: '/c' }]);
  take(sync.diff(src.list(), src.meta())); // 写しより新しい差分。まだ飛びとして捨てる
  release();
  for (let n = 0; n < 5; n += 1) await new Promise((r) => setImmediate(r));
  assert.strictEqual(fetches, 2);
  assert.deepStrictEqual(state.items.map((i) => i.id), src.list().map((i) => i.id));
});

test('問い合わせている間に捨てた差分が答えの写しより古ければ、もう 1 度は問い合わせない（開いた直後の差分）', async () => {
  const src = queueSource();
  const sync = createListSync(view);
  let state = empty();
  let fetches = 0;
  let release;
  src.q.add([{ source: '/a' }]);
  const early = sync.diff(src.list(), src.meta()); // 開いた画面が全体を問い合わせる前の差分
  const fetchFull = () => {
    fetches += 1;
    const snap = sync.full(src.list(), src.meta());
    return new Promise((r) => { release = () => r(snap); });
  };
  const take = (msg) => {
    const r = receiveList(state, msg);
    state = r.state;
    if (r.resync) resync(msg.seq);
  };
  const resync = createResync(fetchFull, take, () => {}, () => state.seq);
  resync(); // 開いたときの問い合わせ
  await new Promise((r) => setImmediate(r));
  take(early); // 写しより古い差分が、答えより先に届いた（飛びに見える）
  release();
  for (let n = 0; n < 5; n += 1) await new Promise((r) => setImmediate(r));
  assert.strictEqual(fetches, 1);
  assert.deepStrictEqual(state.items.map((i) => i.id), src.list().map((i) => i.id));
});
