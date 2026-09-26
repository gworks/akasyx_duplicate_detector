#!/usr/bin/env bash
# packaging/build_python.sh - detector と crawler を PyInstaller の onedir で固める（配布版の同梱物）
#
#   packaging/build_python.sh            → build/pyi/dist/akasyx-{detector,crawler}/
#
# UI（electron-ui）は配布版でこの 2 つを起動する。利用者の Mac に uv・Python が無くても動くようにするため。
# akasyx_search の packaging/build_python.sh と同じ作り（crawler の除外指定も揃える）。
# 兄弟リポジトリの開発用 .venv は変えない（--inexact: 既存パッケージを消さない / --with: PyInstaller は一時的に重ねるだけ）。
# モジュールフォルダは __init__.py を持つため、PyInstaller は親を検索パスにしてしまう。
# `import config` のようなトップレベル import が解決できるよう --paths でモジュールフォルダを明示する。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIB="$(cd "$ROOT/.." && pwd)"
PYI="$ROOT/build/pyi"
DIST="$PYI/dist"
mkdir -p "$DIST"

# $1=出力名 $2=作業ディレクトリ $3...=uv run の追加引数 -- PyInstaller の追加引数
pyi() {
  local name="$1" dir="$2"; shift 2
  local uv_args=() pyi_args=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do uv_args+=("$1"); shift; done
  [ $# -gt 0 ] && shift
  pyi_args=("$@")
  echo "== $name ($dir)"
  (cd "$dir" && uv run --inexact ${uv_args[@]+"${uv_args[@]}"} --with pyinstaller pyinstaller main.py \
      --name "$name" --onedir --noconfirm --clean --log-level WARN \
      --distpath "$DIST" --workpath "$PYI/work" --specpath "$PYI/spec" \
      --paths "$dir" \
      --exclude-module pytest --exclude-module IPython --exclude-module tkinter \
      ${pyi_args[@]+"${pyi_args[@]}"})
}

# version.txt は --version と UI の表示に使う（config._version_file が _MEIPASS から読む）
pyi akasyx-detector "$ROOT/detector" -- \
  --add-data "$ROOT/version.txt:."

# mutagen（GPL-2.0+）と pillow-heif（ホイールに GPL の libx265 を同梱）は配布物に入れない。
# crawler は依存が無い形式をメタデータなしで続行する（音声・HEIC のメタデータだけが取れなくなる）
pyi akasyx-crawler "$SIB/akasyx_crawler/crawler" --extra meta -- \
  --exclude-module mutagen --exclude-module pillow_heif

# 外したパッケージのメタデータ（dist-info）だけが残ることがある（版の確認用にコピーされる）。コードではないが消す
find "$DIST" -maxdepth 3 -type d \( -iname "mutagen*.dist-info" -o -iname "pillow_heif*.dist-info" \) -exec rm -rf {} +

# GPL 系が紛れ込んでいたら失敗させる（配布物がソース公開義務を負わないように）
bad=$(find "$DIST" \( -iname "*x265*" -o -iname "*heif*" -o -iname "*de265*" -o -ipath "*/mutagen/*" \) | head -5)
if [ -n "$bad" ]; then
  echo "エラー: 配布物に入れてはいけないものが含まれています:" >&2
  echo "$bad" >&2
  exit 1
fi

du -sh "$DIST"/akasyx-*
