// modes.js - 機能（タブ）の目的のまとまりと、何を変えるかの見分け（#20）。main（プレビュー）・画面（app.js）・node のテストから読む
//
// 取り込む（ファイルを動かす）→ 確認する（ファイルは動かさない）→ 準備・復旧（保存フォルダを用意したとき・
// 正本 DB をなくしたとき）の順に並べる。#22 のかんたん取り込みは「取り込む」の先頭に入る。
//
// 印は「その操作自体がすること」。保存フォルダ一覧（archives）以外のどの実行も、始める前に保存フォルダの登録
// （初めてなら .akasyx/archive.id を書く）と、前回中断した移動の後始末（detector/main.py の recover_pending）・
// 実行履歴の 1 行を書くので、「何も変えない」とは言わない（文言は「表示」「判定だけ」のように操作の中身で書く）。
(function (root) {
  // 機能ごとの表。tab: タブの印（その機能でできるいちばん大きな変更。
  // files=ファイルを動かす・消す / db=正本 DB の記録を変える / none=表示する） / run: 組んだ引数から実行ボタンの印を決める
  const MODE_INFO = {
    add: {
      tab: 'files',
      run: (args) => (args.includes('--dry-run') ? ['none', 'dry_run'] : ['files', 'move']),
    },
    'delete-duplicates': {
      tab: 'files',
      // --yes が無ければ削除しないが、投入元が既に無い重複の記録（resolution）は書く（detector/dedupe.py）。
      // 退避するのは引数に --trash-dir が付いたときだけ（欄の値ではなく、正規化して付いたかどうか）
      run: (args) => {
        if (!args.includes('--yes')) return ['db', 'check'];
        return args.includes('--trash-dir') ? ['files', 'trash'] : ['files', 'delete'];
      },
    },
    report: { tab: 'none', run: () => ['none', 'view'] },
    // ファイルは動かさないが、見つからない印・確認日時・処置予定の印を記録する
    verify: { tab: 'db', run: () => ['db', 'update_db'] },
    // 実行ボタンは一覧を出すだけ。一覧の行の「登録を外す」が DB を変える（タブの印は db）
    archives: {
      tab: 'db',
      // buildArchivesArgs は `--forget <ID>` と分けて出す。前方一致にすると、--forget で始まる値（パスなど）と取り違える
      run: (args) => (args.includes('--forget') ? ['db', 'forget'] : ['none', 'view']),
    },
    adopt: { tab: 'db', run: () => ['db', 'register'] },
  };

  // 目的のまとまりとタブの並び（ここが正本。index.html と同じ並びであることを modes.test.js が確かめる）
  const GROUPS = [
    { id: 'ingest', modes: ['add', 'delete-duplicates'] },
    { id: 'check', modes: ['report', 'verify', 'archives'] },
    { id: 'setup', modes: ['adopt'] },
  ];

  /** タブの印。知らない機能は unknown（「何も変えない」と言わない）。 */
  function tabEffect(mode) {
    return Object.prototype.hasOwnProperty.call(MODE_INFO, mode) ? MODE_INFO[mode].tab : 'unknown';
  }

  /**
   * 実行ボタンの印。commands.js の buildArgs が組んだ引数から決める（フォームの生の値で決めると、正規化で落ちる
   * 指定と食い違う）。effect はボタンの色、action は文言のキー（run_<action>）。
   */
  function argsEffect(args) {
    const mode = Array.isArray(args) ? args[0] : undefined;
    if (!Object.prototype.hasOwnProperty.call(MODE_INFO, mode)) return { mode, effect: 'unknown', action: 'unknown' };
    const [effect, action] = MODE_INFO[mode].run(args);
    return { mode, effect, action };
  }

  /**
   * 押したときに見えていた印と、実行する引数の印が同じか（#20）。両方あって、機能・色・文言が同じときだけ true
   * （色と文言が同じでも機能が違えば別の操作。押せない状態で押したときは印が無いので、同じとはみなさない）。
   * main（run:start）と画面の予約の両方で使う
   */
  function sameEffect(a, b) {
    return !!a && !!b && a.mode === b.mode && a.effect === b.effect && a.action === b.action;
  }

  const api = { GROUPS, MODE_INFO, tabEffect, argsEffect, sameEffect };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Modes = api;
})(this);
