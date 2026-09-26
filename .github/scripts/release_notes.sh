#!/usr/bin/env bash
# リリースノート: 手書き優先・自動生成フォールバック
# 出典: 40_Knowledge/release_version_tag_automation §5（2026-09-01 の既定）
#   __documents/release_note_vX_Y_Z.md があれば → その内容をそのまま Release 本文にする
#                                   無ければ → PR タイトル一覧 ＋ 直前タグからの差分サマリを自動生成
# 引数: <タグ名 vX.Y.Z> <リリース対象コミットSHA>
# 必要な環境変数: GH_TOKEN, GITHUB_REPOSITORY
# stdout はそのまま Release 本文になるので、進捗ログは必ず stderr に出す
set -euo pipefail
TAG="${1:?usage: release_notes.sh <vX.Y.Z> <sha>}"
SHA="${2:?usage: release_notes.sh <vX.Y.Z> <sha>}"
cd "$(git rev-parse --show-toplevel)"

VER="${TAG#v}"
IFS='.' read -r MAJOR MINOR PATCH <<< "$VER"

# --- 1. 手書きのリリースノートを探す ---
HAND="__documents/release_note_v${MAJOR}_${MINOR}_${PATCH}.md"
# 旧書式（release_note_v010.md）は各桁が1桁のときだけ後方互換で拾う（1.3.10 と 1.11.0 が両方 v1310 になるため）
if [ ! -f "$HAND" ] && [ "${#MAJOR}" -eq 1 ] && [ "${#MINOR}" -eq 1 ] && [ "${#PATCH}" -eq 1 ]; then
  HAND="__documents/release_note_v${MAJOR}${MINOR}${PATCH}.md"
fi
# 空・空白だけのファイルは「無い」とみなす（実質空の本文で公開しない）
if [ -f "$HAND" ] && grep -q '[^[:space:]]' "$HAND"; then
  echo "手書きのリリースノートを使用します: ${HAND}" >&2
  cat "$HAND"
  exit 0
fi
echo "手書きが無いため自動生成します" >&2

# --- 2. 自動生成 ---
# 比較元は「番号が最大のタグ」ではなく「履歴上で到達できる直近のタグ」。
# 番号最大を使うと、過去版の補完（v1.2.9 を v1.3.0 の後に出す等）で逆向きの差分になる
PREV="$(git describe --tags --abbrev=0 --match 'v[0-9]*' "${SHA}^" 2>/dev/null || true)"
if [ -z "$PREV" ]; then
  # 初回タグなら正常。shallow clone 等でタグが取れていないときも同じく空になるので、黙らせない
  echo "直前タグが見つかりません（${SHA}^）。比較元なしで自動生成します" >&2
fi
if [ -n "$PREV" ]; then
  BODY="$(gh api "repos/${GITHUB_REPOSITORY}/releases/generate-notes" \
    -f tag_name="${TAG}" -f target_commitish="${SHA}" -f previous_tag_name="${PREV}" --jq .body)"
else
  BODY="$(gh api "repos/${GITHUB_REPOSITORY}/releases/generate-notes" \
    -f tag_name="${TAG}" -f target_commitish="${SHA}" --jq .body)"
fi
echo "$BODY"
if [ -n "$PREV" ]; then
  COMMITS="$(git log --no-merges --pretty='- %s' "${PREV}..${SHA}")"
  echo; echo "### ${PREV} からの差分"; echo
  echo "- 変更規模:$(git diff --shortstat "${PREV}..${SHA}")"
  echo "- コミット数: $(printf '%s\n' "$COMMITS" | grep -c '^-')（マージコミットを除く）"
  echo; echo "<details><summary>コミット一覧</summary>"; echo; echo "$COMMITS"; echo; echo "</details>"
fi
