#!/usr/bin/env bash
# ToFetch Linux launcher
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 Python 3，请先安装（例如：sudo apt install python3）"
  exit 1
fi
# Pick the bundled engine for this CPU; fall back to a system aria2.
case "$(uname -m)" in
  x86_64|amd64)  BUNDLED="./aria2c-x86_64" ;;
  aarch64|arm64) BUNDLED="./aria2c-aarch64" ;;
  *)             BUNDLED="" ;;
esac
ARIA2_BIN=""
if [ -n "$BUNDLED" ] && [ -f "$BUNDLED" ]; then
  chmod +x "$BUNDLED" 2>/dev/null || true
  if "$BUNDLED" --version >/dev/null 2>&1; then ARIA2_BIN="$PWD/${BUNDLED#./}"; fi
fi
if [ -z "$ARIA2_BIN" ] && command -v aria2c >/dev/null 2>&1; then
  ARIA2_BIN="$(command -v aria2c)"
  echo "提示：自带引擎不可用，已改用系统 aria2（$ARIA2_BIN）"
fi
if [ -z "$ARIA2_BIN" ]; then
  echo "错误：没有可用的 aria2 引擎。请安装 aria2（例如：sudo apt install aria2）后重试。"
  exit 1
fi
export ARIA2_BIN
export TOFETCH_HOST="${TOFETCH_HOST:-127.0.0.1}"
export TOFETCH_PORT="${TOFETCH_PORT:-8077}"
export TOFETCH_DOWNLOAD_DIR="${TOFETCH_DOWNLOAD_DIR:-$HOME/Downloads/ToFetch}"
mkdir -p "$TOFETCH_DOWNLOAD_DIR"
(sleep 2; xdg-open "http://localhost:$TOFETCH_PORT" >/dev/null 2>&1) &
echo "ToFetch 已启动：http://localhost:$TOFETCH_PORT"
echo "下载文件保存在：$TOFETCH_DOWNLOAD_DIR（Ctrl+C 停止）"
exec python3 server.py
