// list-sync.js - main が持つ一覧（かんたん取り込みの列・実行の履歴）の写しを、差分で画面へ送る（#36）。
// main（作る側）と画面（当てる側）と node のテストの両方から読む
//
// 一覧の項目は id と版（rev。中身が変わるたびに上げる）を持つ。main は前に送った版を覚えておき、版が変わった項目
// （新しい項目を含む）・消えた項目の id・並び順（id の配列）だけを送る。終わった行のように中身が変わらない項目は 1 度
// だけ送られる。写しには通し番号（seq）を付け、画面は番号が飛んだら（窓を閉じていた・取りこぼした）全体を
// 問い合わせ直す。全体の写しも番号を進める（それより古い差分・全体の写しは当てない）。
(function (root) {
  /**
   * main 側。view(項目) は画面へ送る形（送る項目だけ作る）。meta は一覧の外の小さな状態（列の一時停止など）。
   */
  function createListSync(view) {
    let sent = new Map(); // id → 前に送った版
    let sentOrder = [];
    let sentMeta = 'null';
    let seq = 0;
    const remember = (items, meta) => {
      sent = new Map(items.map((i) => [i.id, i.rev]));
      sentOrder = items.map((i) => i.id);
      sentMeta = JSON.stringify(meta ?? null);
    };
    return {
      /** 前に送ったものからの差分（変わっていなければ null）。送ったものとして覚える。 */
      diff(items, meta = null) {
        const upserts = items.filter((i) => sent.get(i.id) !== i.rev);
        const ids = new Set(items.map((i) => i.id));
        const removed = [...sent.keys()].filter((id) => !ids.has(id));
        const order = items.map((i) => i.id);
        const sameOrder = order.length === sentOrder.length && order.every((id, n) => id === sentOrder[n]);
        if (!upserts.length && !removed.length && sameOrder && JSON.stringify(meta ?? null) === sentMeta) return null;
        remember(items, meta);
        seq += 1;
        return { seq, upserts: upserts.map(view), removed, order, meta: meta ?? null };
      },
      /** 全体の写し（開き直した画面・番号が飛んだ画面へ）。送ったものとして覚える。 */
      full(items, meta = null) {
        remember(items, meta);
        seq += 1;
        return { seq, full: true, items: items.map(view), meta: meta ?? null };
      },
    };
  }

  /**
   * 画面側。state: { seq, items, meta }（まだ何も受け取っていなければ seq は -1）。
   * 戻り値 { state: 当てた後（当てなければ元の state のまま）, resync: 全体を問い合わせ直す }
   */
  function receiveList(state, msg) {
    if (!msg || !Number.isInteger(msg.seq)) return { state, resync: false };
    if (msg.full) {
      if (msg.seq <= state.seq) return { state, resync: false }; // 新しい写しを持っている
      return { state: { seq: msg.seq, items: msg.items || [], meta: msg.meta ?? null }, resync: false };
    }
    if (msg.seq <= state.seq) return { state, resync: false }; // 全体の写しより古い差分
    if (msg.seq !== state.seq + 1) return { state, resync: true };
    const byId = new Map(state.items.map((i) => [i.id, i]));
    for (const id of msg.removed || []) byId.delete(id);
    for (const item of msg.upserts || []) byId.set(item.id, item);
    // 並び順にない id は送り手の一覧に無い（並び順が正）。並び順にあるのに中身が無ければ食い違っている
    const items = [];
    for (const id of msg.order || []) {
      if (!byId.has(id)) return { state, resync: true };
      items.push(byId.get(id));
    }
    return { state: { seq: msg.seq, items, meta: msg.meta ?? null }, resync: false };
  }

  /**
   * 画面側。全体の写しを問い合わせ直す関数 run(seq) を作る（receiveList が resync を返したときに、飛びとして捨てた差分の
   * 番号を渡して呼ぶ。最初の問い合わせは番号なし）。問い合わせは同時に 1 本だけ。問い合わせている間に捨てた差分が、
   * 当てた写しより新しければ（答えの写しより後の変更を取りこぼした）もう 1 度問い合わせる。currentSeq() は当てた後の
   * 画面の番号（全体の写しと差分は同じ通し番号）。
   */
  function createResync(fetchFull, receive, onError = () => {}, currentSeq = () => -1) {
    let inFlight = false;
    let wanted = -Infinity; // 問い合わせている間に捨てた差分の、いちばん新しい番号
    const run = (seq = -Infinity) => {
      if (inFlight) {
        wanted = Math.max(wanted, seq);
        return;
      }
      inFlight = true;
      wanted = -Infinity;
      Promise.resolve()
        .then(fetchFull)
        .then(receive)
        .catch(onError) // 問い合わせの失敗も、当てる・描くときの失敗も
        .finally(() => {
          inFlight = false;
          if (wanted > currentSeq()) run(wanted);
        });
    };
    return run;
  }

  if (typeof module === 'object' && module.exports) module.exports = { createListSync, receiveList, createResync };
  else root.ListSync = { createListSync, receiveList, createResync };
})(this);
