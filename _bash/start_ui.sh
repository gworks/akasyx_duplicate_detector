#!/usr/bin/env bash
# _bash/start_ui.sh - 動作確認用に UI を起動する
#
#   _bash/start_ui.sh          開発版（ソースのまま。detector は uv run、crawler は ../akasyx_crawler）
#   _bash/start_ui.sh --app    配布版（packaging/build_mac.sh で作った build/release の .app）
#
# 開発版は、足りない準備（detector の uv sync・electron-ui の npm install）があれば先に済ませる。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/release/akasyx Duplicate Detector.app"
CRAWLER="$(dirname "$ROOT")/akasyx_crawler"

die() { echo "エラー: $*" >&2; exit 1; }

case "${1:-}" in
  --app)
    [ -d "$APP" ] || die "配布版がありません: $APP（packaging/build_mac.sh で作ってください）"
    echo "配布版を起動します: $APP"
    open "$APP"
    exit 0
    ;;
  "") ;;
  *) die "不明な引数: $1（使い方: $0 [--app]）" ;;
esac

command -v uv >/dev/null || die "uv がありません"
command -v npm >/dev/null || die "npm がありません"
# add / verify は crawler を使う。無くても UI は開けるので警告だけにする
[ -f "$CRAWLER/crawler/main.py" ] || echo "警告: akasyx_crawler がありません（$CRAWLER）。add / verify は失敗します" >&2

if [ ! -d "$ROOT/detector/.venv" ]; then
  echo "detector の依存を入れます（uv sync）"
  (cd "$ROOT/detector" && uv sync)
fi
if [ ! -x "$ROOT/electron-ui/node_modules/.bin/electron" ]; then
  echo "electron-ui の依存を入れます（npm install。初回は Electron 本体 約110MB を取得）"
  (cd "$ROOT/electron-ui" && npm install)
fi
# npm install 済みでも Electron 本体（dist/）だけ無いことがある。起動時の取得は進捗が出ないので先に取る
if [ ! -d "$ROOT/electron-ui/node_modules/electron/dist" ]; then
  echo "Electron 本体を取得します（約110MB）"
  (cd "$ROOT/electron-ui" && node node_modules/electron/install.js)
fi

echo "開発版を起動します（終了は UI を閉じるか Ctrl+C）"
cd "$ROOT/electron-ui"
exec npm run dev
