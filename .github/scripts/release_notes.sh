#!/usr/bin/env bash
# 直前タグからの差分でリリースノートを生成する
# GitHub の generate-notes（PR タイトル）に変更規模とコミット一覧を足す
set -euo pipefail
TAG="${1:?usage: release_notes.sh <vX.Y.Z> <sha>}"
SHA="${2:?usage: release_notes.sh <vX.Y.Z> <sha>}"
PREV="$(git tag --list 'v*' --sort=-v:refname | grep -vx "${TAG}" | head -1 || true)"

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
