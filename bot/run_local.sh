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

: "${TELEGRAM_BOT_TOKEN:?thiếu TELEGRAM_BOT_TOKEN trong $ENV_FILE}"
: "${TELEGRAM_CHAT_ID:?thiếu TELEGRAM_CHAT_ID trong $ENV_FILE}"

export IDF_PATH="${IDF_PATH:-/opt/esp/idf}"
export IDF_VERSION="${IDF_VERSION:-v5.1.4}"
export CCACHE_DIR="${CCACHE_DIR:-$HOME/.cache/ccache}"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-2G}"
export CCACHE_COMPRESS=1
export TELEGRAM_OFFSET_FILE="${TELEGRAM_OFFSET_FILE:-$ROOT/bot_state/offset}"
export BOT_WORKDIR="${BOT_WORKDIR:-$ROOT/bot_work}"
export PYTHONUNBUFFERED=1

mkdir -p "$TELEGRAM_OFFSET_FILE" 2>/dev/null || true
mkdir -p "$(dirname "$TELEGRAM_OFFSET_FILE")" "$BOT_WORKDIR" "$CCACHE_DIR"

# Sloppiness giúp ccache hit khi file được restore từ nơi khác.
command -v ccache >/dev/null 2>&1 && \
  ccache --set-config=sloppiness=file_stat_matches,include_file_mtime,include_file_ctime,time_macros,locale || true

echo "[run_local] IDF_PATH=$IDF_PATH"
echo "[run_local] CCACHE_DIR=$CCACHE_DIR"
echo "[run_local] offset=$(cat "$TELEGRAM_OFFSET_FILE" 2>/dev/null || echo 'chưa có')"

exec python3 "$HERE/bot.py"