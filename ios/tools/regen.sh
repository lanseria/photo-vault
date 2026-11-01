#!/usr/bin/env bash
# 并发安全地重新生成 Xcode 工程（多个 agent 同时加文件时用这个，别直接 xcodegen）。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .build
LOCK=".build/.regen.lock"
tries=0
until mkdir "$LOCK" 2>/dev/null; do
  tries=$((tries + 1))
  if [ "$tries" -gt 300 ]; then echo "等待工程锁超时" >&2; exit 1; fi
  sleep 1
done
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
xcodegen generate
