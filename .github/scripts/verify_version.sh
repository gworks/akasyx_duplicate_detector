#!/usr/bin/env bash
# バージョンの検証。全対象を一覧表示してどこがズレたか一目で分かるようにする
set -euo pipefail
EXPECTED="${1:?usage: verify_version.sh <x.y.z>}"

# CR と空白を落としてから比較する（Windows クローンの CRLF 対策）
declare -a NAMES=("version.txt" "detector/pyproject.toml")
declare -a VALUES=(
  "$(tr -d '[:space:]' < version.txt)"
  "$(tr -d '\r' < detector/pyproject.toml | grep -m1 -E '^version = ' \
    | sed -E 's/^version = "(.*)"$/\1/' | tr -d '[:space:]')"
)

FAILED=0
for i in "${!NAMES[@]}"; do
  if [ "${VALUES[$i]}" = "$EXPECTED" ]; then
    echo "  OK       ${NAMES[$i]} = ${VALUES[$i]}"
  else
    echo "  MISMATCH ${NAMES[$i]} = ${VALUES[$i]}（期待: ${EXPECTED}）"
    FAILED=1
  fi
done
[ "$FAILED" -eq 0 ] || { echo "::error::バージョンが ${EXPECTED} に揃っていません"; exit 1; }
