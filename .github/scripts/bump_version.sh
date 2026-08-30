#!/usr/bin/env bash
# バージョンファイルへの書き込み（正本: version.txt。detector/pyproject.toml は配布メタデータとして同期）
# ワークフローにベタ書きせずここに集約する（検証側とのロジック分裂を防ぐ）
set -euo pipefail
VER="${1:?usage: bump_version.sh <x.y.z>}"
echo "$VER" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "::error::形式不正: ${VER}"; exit 1; }

echo "$VER" > version.txt
echo "OK version.txt -> ${VER}"

# 最初の version 行だけ置換。sed -i / 0,/re/ は GNU 限定なので awk（POSIX）を使う
awk -v ver="$VER" '
  !done && /^version = "/ { print "version = \"" ver "\""; done = 1; next }
  { print }
' detector/pyproject.toml > detector/pyproject.toml.tmp
mv detector/pyproject.toml.tmp detector/pyproject.toml
grep -qx "version = \"${VER}\"" detector/pyproject.toml || { echo "::error::pyproject 更新失敗"; exit 1; }
echo "OK detector/pyproject.toml -> ${VER}"
