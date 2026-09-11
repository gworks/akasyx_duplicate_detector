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

class FormError extends Error {}

function trimmed(value) {
  return typeof value === 'string' ? value.trim() : '';
}

/** フォーム1件から CLI 引数配列を作ります。不備は FormError で返します。 */
function buildArgs(form) {
  const mode = trimmed(form && form.mode);
  if (!MODES.includes(mode)) {
    throw new FormError(`不明なコマンドです: ${mode || '(未指定)'}`);
  }

  const archiveRoot = trimmed(form.archiveRoot);
  if (!archiveRoot) {
    throw new FormError('保存用フォルダを指定してください');
  }

  const args = [mode, archiveRoot];

  if (mode === 'add') {
    const sourcePath = trimmed(form.sourcePath);
    if (!sourcePath) {
      throw new FormError('投入元（追加したいファイル / フォルダ）を指定してください');
    }
    args.push(sourcePath);

    if (form.useDestSubdir) {
      // '' は「保存フォルダ直下に展開」を意味する有効な指定なので、空でも渡す
      args.push('--dest-subdir', trimmed(form.destSubdir));
    }
    if (form.dryRun) args.push('--dry-run');

    // 空欄は「既定のまま」。既定値と同じなら引数を増やさない
    const rawMinSize = trimmed(String(form.minSize ?? ''));
    if (rawMinSize) {
      const minSize = Number(rawMinSize);
      if (!Number.isInteger(minSize) || minSize < 0) {
        throw new FormError(`最小サイズは 0 以上の整数で指定してください: ${rawMinSize}`);
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
    const trashDir = trimmed(form.trashDir);
    if (trashDir) args.push('--trash-dir', trashDir);
    if (form.pruneEmptyDirs) args.push('--prune-empty-dirs');
    if (form.yes) args.push('--yes');
  }

  if (mode === 'report') {
    pushIngestId(args, form.ingestId);
  }

  // crawler 関連の上書き（未指定なら detector の既定に任せる）
  for (const [key, flag] of [
    ['crawlerRepo', '--crawler-repo'],
    ['dbDir', '--db-dir'],
    ['logDir', '--log-dir'],
  ]) {
    const value = trimmed(form[key]);
    if (value) args.push(flag, value);
  }

  return args;
}

function pushIngestId(args, raw) {
  const value = trimmed(String(raw ?? ''));
  if (!value) return;
  const id = Number(value);
  if (!Number.isInteger(id) || id <= 0) {
    throw new FormError(`実行 ID は正の整数で指定してください: ${value}`);
  }
  args.push('--ingest-id', String(id));
}

/** 画面表示・コピー用のコマンド文字列（実際に spawn する内容と同じ引数から作る）。 */
function formatCommand(args) {
  return ['uv', 'run', 'main.py', ...args].map(quoteArg).join(' ');
}

function quoteArg(arg) {
  return /^[A-Za-z0-9_./:=+-]+$/.test(arg) ? arg : `'${arg.replace(/'/g, "'\\''")}'`;
}

module.exports = { MODES, QUARANTINE_KINDS, FormError, buildArgs, formatCommand };
