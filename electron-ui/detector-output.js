// detector-output.js - detector の出力（英語固定）から UI が拾うもの
//
// 文言の正本は detector 側（ingest.py・dedupe.py・verify.py・archives.py）。変えるときは両方を揃える。
// Note: 行と adopt の案内は detector/tests/test_archives_ui.py が同じ正規表現で実際の出力を確かめている。

const PROGRESS_LINE = /^Progress: (\d+) files \((.+)\)$/;
const CSV_LINE = /CSV report: (.+)$/;
// つながっていない保存フォルダにしか無い重複（ingest._report_unavailable_owners が stdout に出す行）。
// 同じ文がログ（stderr の WARNING 行）にも出るので、行頭の Note: だけを拾う
const NOTE_FORGET_LINE = /^Note: .*archive folder #(\d+) .*`archives --forget (\d+)`/;
// 中身のあるフォルダを断るときの案内（archives.resolve_archive など）。識別子のあるフォルダの断り
// （「… register the files in it again: adopt …」）は拾わない。対処は --archive-db で使っていた正本 DB を
// 指定することで、adopt を勧めると同じ保存フォルダが 2 つの正本 DB に登録されてしまう
const ADOPT_HINT_LINE = /(?:Register the files in it first|Run adopt again): adopt (.+)$/;

/** 1 行から拾えるものを返します（無ければ空のオブジェクト）。 */
function scanLine(text) {
  const progress = PROGRESS_LINE.exec(text);
  if (progress) return { progress: { processed: Number(progress[1]), result: progress[2] } };
  const csv = CSV_LINE.exec(text);
  if (csv) return { csvPath: csv[1].trim() };
  const note = NOTE_FORGET_LINE.exec(text);
  if (note) return { forgetId: Number(note[2]) };
  const adopt = ADOPT_HINT_LINE.exec(text);
  if (adopt) return { adoptPath: adopt[1].trim() };
  return {};
}

/** `archives --json` の出力を読みます。読めなければ例外（空の一覧として扱わない）。 */
function parseArchivesJson(stdout) {
  const data = JSON.parse(String(stdout));
  if (!data || typeof data !== 'object' || !Array.isArray(data.archives)) {
    throw new Error('unexpected output of `archives --json`');
  }
  // db_exists: false は正本 DB がまだ無い（初回起動など。detector は一覧のために作らない）
  return { masterDb: data.master_db || '', dbExists: data.db_exists !== false, archives: data.archives };
}

module.exports = { scanLine, parseArchivesJson };
