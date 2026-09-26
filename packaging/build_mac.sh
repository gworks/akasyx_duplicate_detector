#!/usr/bin/env bash
# packaging/build_mac.sh - 配布物を作る（署名・公証まで）。Apple Silicon の Mac で実行する
#
#   packaging/build_mac.sh
#
# 出力: build/release/（akasyx Duplicate Detector.app・README.txt・THIRD_PARTY_LICENSES.txt）と、その zip
#       （AKASYX_DIST_DIR で出力先フォルダを変えられる）
# akasyx_search の packaging/build_mac.sh と同じ流れ。
#
# 環境変数（秘密の実体はここにもリポジトリにも書かない。名前だけを渡す）:
#   AKASYX_SIGN_IDENTITY   署名 ID の名前（省略時は keychain の最初の "Developer ID Application"）
#   公証はどちらか一方（どちらも無ければ公証しない。zip 名に UNNOTARIZED を付け、最後に警告する）:
#   AKASYX_NOTARY_PROFILE  `xcrun notarytool store-credentials` で保存した keychain プロファイル名
#   APPLE_API_KEY / APPLE_API_KEY_ID / APPLE_API_ISSUER
#                          App Store Connect API キー（.p8 のパス・Key ID・Issuer ID）。youtube_downloder の
#                          .env.notarize と同じ変数名
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
OUT="${AKASYX_DIST_DIR:-$BUILD/release}"
STAGE="$BUILD/stage"
VERSION="$(tr -d '[:space:]' < "$ROOT/version.txt")"
APP_NAME="akasyx Duplicate Detector"
MARKER=".akasyx-build"

die() { echo "エラー: $*" >&2; exit 1; }
step() { echo; echo "=== $*"; }

# ---- 前提 ----
[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || die "Apple Silicon の Mac で実行してください"
command -v uv >/dev/null || die "uv がありません"
command -v npm >/dev/null || die "npm がありません"
IDENTITY="${AKASYX_SIGN_IDENTITY:-$(security find-identity -v -p codesigning | sed -nE 's/.*"(Developer ID Application: [^"]+)".*/\1/p' | head -1)}"
[ -n "$IDENTITY" ] || die "署名 ID（Developer ID Application）が keychain にありません"
NOTARY=()
if [ -n "${AKASYX_NOTARY_PROFILE:-}" ]; then
  NOTARY=(--keychain-profile "$AKASYX_NOTARY_PROFILE")
elif [ -n "${APPLE_API_KEY:-}" ]; then
  [ -f "$APPLE_API_KEY" ] || die "APPLE_API_KEY の .p8 ファイルがありません（パスを確認してください）"
  [ -n "${APPLE_API_KEY_ID:-}" ] && [ -n "${APPLE_API_ISSUER:-}" ] || die "APPLE_API_KEY_ID と APPLE_API_ISSUER も指定してください"
  NOTARY=(--key "$APPLE_API_KEY" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER")
fi
# 出力先は、空か、前回このスクリプトが作ったフォルダのときだけ作り直す（手で置いたファイルを消さない）
if [ -d "$OUT" ] && [ -n "$(ls -A "$OUT")" ] && [ ! -f "$OUT/$MARKER" ]; then
  die "$OUT に、このスクリプトが作っていないファイルがあります。中身を確認して空にしてから実行してください"
fi

step "1/6 Python を実行形式にする"
"$ROOT/packaging/build_python.sh"

step "2/6 ライセンス表記"
"$ROOT/packaging/build_licenses.sh"

step "3/6 同梱物を並べる（build/stage）"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$BUILD/pyi/dist" "$STAGE/bin"
cp "$ROOT/packaging/THIRD_PARTY_LICENSES.txt" "$STAGE/"

step "4/6 Electron の .app を作って署名する"
rm -rf "$BUILD/electron"
# 版の正本は version.txt。package.json には持たせず、ビルド時に焼き込む（配布版の UI は app.getVersion() を表示）
(cd "$ROOT/electron-ui" && npm ci --silent && CSC_NAME="${IDENTITY#Developer ID Application: }" \
  npx electron-builder --mac dir --arm64 -c.extraMetadata.version="$VERSION")
APP="$BUILD/electron/mac-arm64/$APP_NAME.app"
RES="$APP/Contents/Resources"

# 検査: 入っているべきものが入っていて、署名が正しいこと（欠けていたら失敗させる）
for exe in detector crawler; do
  [ -x "$RES/bin/akasyx-$exe/akasyx-$exe" ] || die "同梱物が欠けています: bin/akasyx-$exe"
done
got_version="$("$RES/bin/akasyx-detector/akasyx-detector" --version | awk '{print $NF}')"
[ "$got_version" = "$VERSION" ] || die "同梱した detector の版（$got_version）が version.txt（$VERSION）と違います"
codesign --verify --deep --strict "$APP" || die "署名の検証に失敗しました"
if find "$APP" \( -iname "*x265*" -o -iname "*heif*" -o -ipath "*/mutagen/*" \) | grep -q .; then
  die "配布物に GPL 系のライブラリが含まれています"
fi

step "5/6 公証（notarization）"
NOTARIZED=0
if [ ${#NOTARY[@]} -gt 0 ]; then
  SUBMIT="$BUILD/notarize.zip"
  ditto -c -k --keepParent "$APP" "$SUBMIT"
  xcrun notarytool submit "$SUBMIT" "${NOTARY[@]}" --wait
  xcrun stapler staple "$APP"
  spctl -a -vv -t exec "$APP" || die "Gatekeeper の検証に失敗しました（公証の結果を確認してください）"
  rm -f "$SUBMIT"
  NOTARIZED=1
else
  echo "公証の認証情報（AKASYX_NOTARY_PROFILE または APPLE_API_KEY 等）が無いため公証しません（このままでは利用者の Mac で起動が止められます）"
fi

step "6/6 出力フォルダと zip"
rm -rf "$OUT"
mkdir -p "$OUT"
ditto "$APP" "$OUT/$APP_NAME.app"
cp "$ROOT/packaging/README.dist.txt" "$OUT/README.txt"
cp "$ROOT/packaging/THIRD_PARTY_LICENSES.txt" "$OUT/"
echo "$VERSION $(date -u +%Y-%m-%dT%H:%M:%SZ) notarized=$NOTARIZED" > "$OUT/$MARKER"
SUFFIX=""
[ "$NOTARIZED" = 1 ] || SUFFIX="-UNNOTARIZED"
ZIP="$(dirname "$OUT")/akasyx-duplicate-detector-$VERSION-mac-arm64$SUFFIX.zip"
rm -f "$ZIP"
# 目印ファイルは zip に入れない（ditto は除外指定が無いので一時フォルダ経由）
TMPD="$(mktemp -d)"
ditto "$OUT" "$TMPD/akasyx-duplicate-detector"
rm -f "$TMPD/akasyx-duplicate-detector/$MARKER"
ditto -c -k --keepParent "$TMPD/akasyx-duplicate-detector" "$ZIP"
rm -rf "$TMPD"

echo
du -sh "$OUT/$APP_NAME.app" "$ZIP"
echo "出力: $OUT"
echo "zip:  $ZIP"
[ "$NOTARIZED" = 1 ] || echo "警告: 公証していません。配布前に公証の認証情報を指定して作り直してください" >&2
