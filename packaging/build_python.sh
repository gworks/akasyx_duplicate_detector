#!/usr/bin/env bash
# packaging/build_python.sh - detector と crawler を PyInstaller の onedir で固める（配布版の同梱物）
#
#   packaging/build_python.sh            → build/pyi/dist/akasyx-{detector,crawler}/
#
# UI（electron-ui）は配布版でこの 2 つを起動する。利用者の Mac に uv・Python が無くても動くようにするため。
# akasyx_search の packaging/build_python.sh をもとにした作り（crawler の除外指定も揃える）。出力の設定（--python-option）と
# crawler の版の確認は、こちらにだけ足した（#24）。
# 兄弟リポジトリの開発用 .venv は変えない（--inexact: 既存パッケージを消さない / --with: PyInstaller は一時的に重ねるだけ）。
# モジュールフォルダは __init__.py を持つため、PyInstaller は親を検索パスにしてしまう。
# `import config` のようなトップレベル import が解決できるよう --paths でモジュールフォルダを明示する。
# 実行形式は PYTHONUNBUFFERED・PYTHONIOENCODING を読まない（UI が渡しても効かない）ので、同じことをビルドで指定する（#24）:
#   --python-option u        出力をバッファしない（crawler の進み具合 Processing files: n/全体 が終わりにまとめて届かないように）
#   --python-option "X utf8" UTF-8 モード（ロケールに依らず stdout を utf-8 にし、UI で日本語のパスが化けないように。
#                            Windows で固めたとき、無いと cp932 で出る）。open() の既定も utf-8 になるが、
#                            detector と crawler はテキストを開くとき encoding を明記しているので変わらない
# --python-option は PyInstaller 6 から（5.13 には無い）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SIB="$(cd "$ROOT/.." && pwd)"

# UI は crawler の進み具合を英語の行（Processing files: n/全体）で読む。akasyx_crawler v0.1.1 までは日本語
# （ファイル処理中: …）で出すので、それを同梱すると一覧作成の進み具合が出ない。固める前に止める（#24）
# 確かめるのは「既知の古い版ではない」ことだけ（main.py の進み具合の行の文字列。git の履歴が無い取り出し方でも使える）。
# crawler が出力の作りを変えたら、ここも合わせて直す
CRAWLER_DIR="$SIB/akasyx_crawler/crawler"  # 下で固める crawler と同じ場所を確かめる
CRAWLER_MAIN="$CRAWLER_DIR/main.py"
if [ ! -f "$CRAWLER_MAIN" ]; then
  echo "エラー: $CRAWLER_MAIN がありません（akasyx_crawler をこのリポジトリと同じ階層に置いてください）。" >&2
  exit 1
fi
if [ ! -r "$CRAWLER_MAIN" ]; then  # 読めないのを「古い版」と取り違えない
  echo "エラー: $CRAWLER_MAIN を読めません。" >&2
  exit 1
fi
# コメントの行は数えない。日本語の行が 1 つでも残っていれば（全体が分からないときの行など）古い版として止める
if ! grep -qE '^[^#]*Processing files: ' "$CRAWLER_MAIN" || grep -qE '^[^#]*ファイル処理中: ' "$CRAWLER_MAIN"; then
  echo "エラー: $CRAWLER_DIR の crawler が進み具合を Processing files: の行で出しません（古い版です）。" >&2
  echo "        UI が読める版（akasyx_crawler の 76ed58a「#3 english output」を含む版。版番号ではなくこのコミットを
        含むかで決まる。v0.1.1 までのリリースには無い）に切り替えてから実行してください。" >&2
  exit 1
fi

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
  (cd "$dir" && uv run --inexact ${uv_args[@]+"${uv_args[@]}"} --with 'pyinstaller>=6' pyinstaller main.py \
      --name "$name" --onedir --noconfirm --clean --log-level WARN \
      --distpath "$DIST" --workpath "$PYI/work" --specpath "$PYI/spec" \
      --paths "$dir" \
      --python-option u --python-option "X utf8" \
      --exclude-module pytest --exclude-module IPython --exclude-module tkinter \
      ${pyi_args[@]+"${pyi_args[@]}"})
}

# version.txt は --version と UI の表示に使う（config._version_file が _MEIPASS から読む）
pyi akasyx-detector "$ROOT/detector" -- \
  --add-data "$ROOT/version.txt:."

# mutagen（GPL-2.0+）と pillow-heif（ホイールに GPL の libx265 を同梱）は配布物に入れない。
# crawler は依存が無い形式をメタデータなしで続行する（音声・HEIC のメタデータだけが取れなくなる）
pyi akasyx-crawler "$CRAWLER_DIR" --extra meta -- \
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
