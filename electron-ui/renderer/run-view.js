// run-view.js - 実行中に他のタブを開いたときの見せ方（#19）。画面（app.js）と node のテストの両方から読む
//
// 実行は 1 本だけで、出力・進み具合はそれを実行したタブ（runMode）のもの。他のタブを見ている間は出力を隠して
// 案内を出す（別のタブのコマンドの隣に元の実行のログが流れて食い違わないように）。他のタブでは上の帯から中断する。
// 実行したタブでは、中断・経過時間・進み具合はパネルにあるので帯は出さない（同じものを 2 つ並べない）。
(function (root) {
  /**
   * @param {object} s
   * @param {string} s.mode 今開いているタブ
   * @param {string|null} s.runMode 出力の持ち主のタブ（実行中・前回の実行。まだ無い・クリアした後は null）
   * @param {boolean} s.running 実行中か
   * @param {boolean} s.stopRequested 中断を頼んだ後か
   * @param {boolean} [s.starting] 実行を頼んで、始まったという応答を待っている間か（削除の確認ダイアログを含む）
   * @param {boolean} s.finishedUnseen 他のタブにいる間に終わり、まだ見に行っていないか
   */
  function runView({ mode, runMode, running, stopRequested, finishedUnseen, starting = false }) {
    const own = runMode === null || runMode === undefined || mode === runMode;
    let runbar = null;
    if (!own) runbar = running ? 'running' : finishedUnseen ? 'finished' : null;
    return {
      showOutput: own,
      showElsewhere: !own,
      // 実行ボタンの隣の中断・回転の印は、実行したタブでだけ（他のタブでは別のコマンドの隣に並んでしまう）
      paneStop: own,
      // 始まる前の中断は main が無視する（子プロセスがまだ無い）ので、応答が来るまで押させない
      paneStopEnabled: running && own && !stopRequested && !starting,
      paneSpinner: running && own,
      runbar,
      runbarGo: runbar !== null,
      runbarStopEnabled: running && !stopRequested && !starting,
    };
  }

  if (typeof module === 'object' && module.exports) module.exports = { runView };
  else root.RunView = { runView };
})(this);
