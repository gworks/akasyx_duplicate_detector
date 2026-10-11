// defaults-summary.js - 実行画面に出す「今の既定」の 1 行（#21）。main のプレビューが作って画面に渡す
//
// 機能ごとの既定は設定画面へ移したので、見えない場所の値で思わぬ動きをしないよう、実行画面に今の値を 1 行で出す。
// 中身は buildArgs が組んだ引数から作る（フォームの生の値から作ると、正規化・検証で落ちる指定と食い違う。#20 の
// 実行ボタンの印と同じ）。ファイルの扱いに関わる「空フォルダの削除」「退避先」と、DB の記録を変える「処置予定の印」は、
// 既定のままでも必ず出す。既定から変えた項目は changed（画面で目立たせる）。文言は辞書のキー（sum_*）と差し込む値で返す。
const { buildArgs, FormError, normalizePath, DEFAULT_FOLDER_LIMIT, DEFAULT_MIN_SIZE } = require('./commands');

// 空フォルダの削除の初期値は機能で違う（取り込みは有効・後始末は無効。index.html の checked と同じ。settings.test.js が確かめる）
const PRUNE_DEFAULT = { add: true, 'delete-duplicates': false };

const item = (key, vars, changed) => ({ key, vars: vars || {}, changed: !!changed });

/** `--name value` の value（無ければ null）。 */
function valueOf(args, name) {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? String(args[i + 1]) : null;
}

/** `--name a b c` の値の並び（次の `--` で始まる引数の前まで）。 */
function valuesOf(args, name) {
  const i = args.indexOf(name);
  if (i < 0) return [];
  const out = [];
  for (let j = i + 1; j < args.length && !String(args[j]).startsWith('--'); j += 1) out.push(String(args[j]));
  return out;
}

function prune(mode, args) {
  const on = args.includes('--prune-empty-dirs');
  return item(on ? 'sum_prune_on' : 'sum_prune_off', {}, on !== PRUNE_DEFAULT[mode]);
}

/**
 * 組んだ引数から 1 行の項目を作る。
 * @param {string[]} args buildArgs の結果
 * @returns {{key: string, vars: object, changed: boolean}[]}
 */
function summarizeArgs(args) {
  const a = Array.isArray(args) ? args : [];
  const mode = a[0];
  if (mode === 'add') {
    const limit = valueOf(a, '--folder-limit');
    const minSize = valueOf(a, '--min-size');
    const items = [
      prune('add', a),
      item('sum_folder_limit', { n: limit ?? String(DEFAULT_FOLDER_LIMIT) }, limit !== null),
      item(a.includes('--with-meta') ? 'sum_meta_on' : 'sum_meta_off', {}, a.includes('--with-meta')),
    ];
    if (minSize !== null) items.push(item('sum_min_size', { n: minSize }, true));
    if (a.includes('--follow-symlinks')) items.push(item('sum_symlinks_on', {}, true));
    return items;
  }
  if (mode === 'verify') {
    const kinds = valuesOf(a, '--flag-quarantine');
    const items = [kinds.length ? item('sum_flag', { kinds: kinds.join(', ') }, true) : item('sum_flag_none')];
    if (a.includes('--follow-symlinks')) items.push(item('sum_symlinks_on', {}, true));
    return items;
  }
  if (mode === 'delete-duplicates') {
    const trash = valueOf(a, '--trash-dir');
    return [trash !== null ? item('sum_trash', { path: trash }, true) : item('sum_trash_none'), prune(mode, a)];
  }
  return [];
}

/**
 * プレビュー用（コマンドを組めなかったとき）。実行画面の欄（保存用フォルダ・投入元・実行 ID）は既定に関わらないので、
 * 空や不正でも既定は見せたい。保存用フォルダ・投入元は仮の値、実行 ID は空にして組む。設定の値が不正で組めないとき
 * （上限件数が 0 など）は出さない（理由はプレビューのエラーとして出る）。
 */
function previewDefaults(form) {
  const f = form || {};
  // 空かどうかはコマンドと同じく正規化した値で見る（'' を貼っただけの欄も空として仮の値を入れる）
  const placeholder = (v) => (normalizePath(v) ? v : '/');
  try {
    return summarizeArgs(buildArgs({
      ...f, archiveRoot: placeholder(f.archiveRoot), sourcePath: placeholder(f.sourcePath), ingestId: '',
    }));
  } catch (e) {
    if (e instanceof FormError) return [];
    throw e;
  }
}

module.exports = { summarizeArgs, previewDefaults };
