#!/usr/bin/env bash
# Chạy Telegram build bot trên máy local (thay cho GitHub Actions).
#
# Dùng:
#   cp bot/.env.example bot/.env      # rồi điền token + chat id
#   ./bot/run_local.sh                # chạy foreground
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Không tìm thấy $ENV_FILE" >&2
  echo "Tạo file rồi điền TELEGRAM_BOT_TOKEN và TELEGRAM_CHAT_ID." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:?thiếu TELEGRAM_BOT_TOKEN trong $ENV_FILE}"
# TELEGRAM_CHAT_ID is optional: if blank, the bot locks onto whoever messages
# it first and persists that id to bot_state/chat_id.

export IDF_PATH="${IDF_PATH:-/opt/esp/idf}"
export IDF_VERSION="${IDF_VERSION:-v5.1.4}"
export CCACHE_DIR="${CCACHE_DIR:-$HOME/.cache/ccache}"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-2G}"
export CCACHE_COMPRESS=1
export TELEGRAM_OFFSET_FILE="${TELEGRAM_OFFSET_FILE:-$ROOT/bot_state/offset}"
export BOT_WORKDIR="${BOT_WORKDIR:-$ROOT/bot_work}"
export PYTHONUNBUFFERED=1

mkdir -p "$(dirname "$TELEGRAM_OFFSET_FILE")" "$BOT_WORKDIR" "$CCACHE_DIR"

# If a previous bug (or a stray mkdir) left the offset path as a directory,
# remove it - otherwise offset can never persist and the bot re-sends
# firmware for the same zip after every restart.
if [ -d "$TELEGRAM_OFFSET_FILE" ]; then
  echo "[run_local] $TELEGRAM_OFFSET_FILE là thư mục, xoá để tạo file"
  rmdir "$TELEGRAM_OFFSET_FILE" 2>/dev/null || rm -rf "$TELEGRAM_OFFSET_FILE"
fi

# Sloppiness giúp ccache hit khi file được restore từ nơi khác.
command -v ccache >/dev/null 2>&1 && \
  ccache --set-config=sloppiness=file_stat_matches,include_file_mtime,include_file_ctime,time_macros,locale || true

echo "[run_local] IDF_PATH=$IDF_PATH"
echo "[run_local] CCACHE_DIR=$CCACHE_DIR"
echo "[run_local] offset=$(cat "$TELEGRAM_OFFSET_FILE" 2>/dev/null || echo 'chưa có')"

exec python3 "$HERE/bot.py"