// run-history.js - 実行ボタンから始めた実行の出力の履歴（#38）。main だけが使う
//
// 詳細ログは 1 つで、次の実行（かんたん取り込みの列の項目を含む）が始まると置き換わる。実行ボタンの実行の結果
// （終わり方・件数・失敗の一覧・警告の行・CSV・出力の末尾）を終わった順に残し、画面のそのタブから開けるようにする。
// 列の項目の結果は列の行にあるので、ここには入れない。残したものは後から変わらない（版は 1 つ。差分で 1 度だけ送る）。

/**
 * タブごとに直近 limit 回の実行を残す（古いものから捨てる。画面はタブごとに出すので、ほかのタブの実行で押し出さない）。
 * 画面は、そのうち出力欄に出ている実行を除いて出す（数え方を出力欄に合わせて変えない）。
 */
function createRunHistory({ limit = 10 } = {}) {
  let entries = [];
  return {
    /** entry: { id: 実行の id, mode: 実行したタブ, command, finishedAt, result }。写して残す。 */
    add(entry) {
      entries.push({ ...structuredClone(entry), rev: 1 });
      const same = entries.filter((e) => e.mode === entry.mode);
      if (same.length > limit) {
        const drop = new Set(same.slice(0, same.length - limit));
        entries = entries.filter((e) => !drop.has(e));
      }
    },
    list: () => entries.slice(),
  };
}

module.exports = { createRunHistory };
