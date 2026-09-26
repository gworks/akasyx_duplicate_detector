// commands.js - フォームの値から detector CLI の引数を組み立てる（唯一の正本）
//
// 設計書 §16「Electron UI」の方針どおり、UI とロジックの境界は CLI に固定する。
// detector 側には一切手を入れず、ここで組んだ引数で main.py を子プロセス起動する。
// 引数の組み立てをメインプロセスに閉じ込めることで、画面に出すコマンドプレビューと
// 実際に実行するコマンドが食い違わないことを保証する。

const MODES = ['add', 'verify', 'delete-duplicates', 'report'];

const QUARANTINE_KINDS = [
  'missing',
  'unregistered',
  'archive_duplicate',
  'hash_mismatch',
];

const DEFAULT_MIN_SIZE = 1; // config.py の既定値。同じ値なら引数に出さない
const DEFAULT_FOLDER_LIMIT = 500; // config.py の DEFAULT_FOLDER_LIMIT

/** 入力の不備。文言は辞書のキー（key）と差し込み（vars）で持ち、main が今の言語に訳す。 */
class FormError extends Error {
  constructor(key, vars = {}) {
    super(key);
    this.key = key;
    this.vars = vars;
  }
}

function trimmed(value) {
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * パス欄の値を正規化します。ターミナルからコピーした形をそのまま貼っても通るように:
 *   - 前後の空白を落とす
 *   - 全体を囲む '…' / "…" を外す（`'/Users/a b/c'` → `/Users/a b/c`）
 *   - シェルのバックスラッシュエスケープを解く（`/Users/a\ b` → `/Users/a b`）
 *     ただし Windows ではパス区切りなので解かない（`C:\Users\me` や UNC `\\server\share`）
 *   - 先頭の `~/` をホームに展開する
 * 正規化後の値がコマンドプレビューに出るので、何が渡るかは画面で確認できる。
 */
function normalizePath(value, platform = process.platform) {
  let p = trimmed(value);
  if (!p) return '';
  const q = p[0];
  if ((q === "'" || q === '"') && p.length >= 2 && p[p.length - 1] === q) {
    p = p.slice(1, -1).trim();
  }
  if (platform !== 'win32' && !isWindowsPath(p)) {
    p = p.replace(/\\(.)/g, '$1');
  }
  if (p === '~' || p.startsWith('~/')) {
    p = require('node:os').homedir() + p.slice(1);
  }
  return p;
}

/** ドライブ指定（`C:\` `C:/`）か UNC（`\\server`）で始まる Windows のパスか。 */
function isWindowsPath(p) {
  return /^[A-Za-z]:[\\/]/.test(p) || p.startsWith('\\\\');
}

/** フォーム1件から CLI 引数配列を作ります。不備は FormError で返します。 */
function buildArgs(form) {
  const mode = trimmed(form && form.mode);
  if (!MODES.includes(mode)) {
    throw new FormError('err_unknown_command', { mode: mode || '-' });
  }

  const archiveRoot = normalizePath(form.archiveRoot);
  if (!archiveRoot) {
    throw new FormError('err_archive_root_required');
  }

  const args = [mode, archiveRoot];

  if (mode === 'add') {
    const sourcePath = normalizePath(form.sourcePath);
    if (!sourcePath) {
      throw new FormError('err_source_required');
    }
    args.push(sourcePath);

    // 1 フォルダの上限件数。既定値と同じなら引数に出さず detector の既定に任せる
    const rawFolderLimit = trimmed(String(form.folderLimit ?? ''));
    if (rawFolderLimit) {
      const folderLimit = Number(rawFolderLimit);
      if (!Number.isInteger(folderLimit) || folderLimit < 1) {
        throw new FormError('err_folder_limit', { value: rawFolderLimit });
      }
      if (folderLimit !== DEFAULT_FOLDER_LIMIT) args.push('--folder-limit', String(folderLimit));
    }

    if (form.dryRun) args.push('--dry-run');

    // 空欄は「既定のまま」。既定値と同じなら引数を増やさない
    const rawMinSize = trimmed(String(form.minSize ?? ''));
    if (rawMinSize) {
      const minSize = Number(rawMinSize);
      if (!Number.isInteger(minSize) || minSize < 0) {
        throw new FormError('err_min_size', { value: rawMinSize });
      }
      if (minSize !== DEFAULT_MIN_SIZE) args.push('--min-size', String(minSize));
    }
    if (form.withMeta) args.push('--with-meta');
    if (form.followSymlinks) args.push('--follow-symlinks');
    if (form.pruneEmptyDirs) args.push('--prune-empty-dirs');
  }

  if (mode === 'verify') {
    const kinds = (form.flagQuarantine || []).filter((k) =>
      QUARANTINE_KINDS.includes(k)
    );
    if (kinds.length) args.push('--flag-quarantine', ...kinds);
    if (form.followSymlinks) args.push('--follow-symlinks');
  }

  if (mode === 'delete-duplicates') {
    pushIngestId(args, form.ingestId);
    const trashDir = normalizePath(form.trashDir);
    if (trashDir) args.push('--trash-dir', trashDir);
    if (form.pruneEmptyDirs) args.push('--prune-empty-dirs');
    if (form.yes) args.push('--yes');
  }

  if (mode === 'report') {
    pushIngestId(args, form.ingestId);
  }

  // crawler 関連の上書き（未指定なら detector の既定に任せる）
  for (const [key, flag] of [
    ['archiveDb', '--archive-db'],
    ['crawlerRepo', '--crawler-repo'],
    ['dbDir', '--db-dir'],
    ['logDir', '--log-dir'],
  ]) {
    const value = normalizePath(form[key]);
    if (value) args.push(flag, value);
  }

  return args;
}

function pushIngestId(args, raw) {
  const value = trimmed(String(raw ?? ''));
  if (!value) return;
  const id = Number(value);
  if (!Number.isInteger(id) || id <= 0) {
    throw new FormError('err_ingest_id', { value });
  }
  args.push('--ingest-id', String(id));
}

/** 画面表示・コピー用のコマンド文字列（実際に spawn する内容と同じ引数から作る）。
 * base は起動コマンドの先頭部分（既定は開発時の `uv run main.py`）。 */
function formatCommand(args, base = ['uv', 'run', 'main.py']) {
  return [...base, ...args].map(quoteArg).join(' ');
}

function quoteArg(arg) {
  return /^[A-Za-z0-9_./:=+-]+$/.test(arg) ? arg : `'${arg.replace(/'/g, "'\\''")}'`;
}

module.exports = {
  MODES, QUARANTINE_KINDS, FormError, buildArgs, formatCommand, quoteArg, normalizePath,
};
