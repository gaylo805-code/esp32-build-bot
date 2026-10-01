#!/usr/bin/env bash
# Cài đặt và khởi động Telegram build bot từ một bản git clone.
#
#   git clone https://github.com/gaylo805-code/esp32-build-bot.git
#   cd esp32-build-bot
#   ./setup.sh
#
# Script idempotent: chạy lại nhiều lần được.
set -euo pipefail

IDF_VERSION="${IDF_VERSION:-v5.1.4}"
IDF_PATH="${IDF_PATH:-/opt/esp/idf}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[0;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[0;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[0;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- sudo helper
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if sudo -n true 2>/dev/null; then
    SUDO="sudo -n"
  else
    die "Cần quyền sudo (để cài ESP-IDF và systemd). Chạy: sudo -v rồi chạy lại script."
  fi
fi

# ---------------------------------------------------------------- 1. python
say "Kiểm tra Python"
command -v python3 >/dev/null || die "Thiếu python3"
ok "python3 $(python3 --version | cut -d' ' -f2)"

# ---------------------------------------------------------------- 2. ccache
say "Kiểm tra ccache"
if ! command -v ccache >/dev/null 2>&1; then
  warn "Chưa có ccache, đang cài..."
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq ccache
  else
    warn "Không dùng apt-get. Build vẫn chạy, chỉ chậm hơn."
  fi
fi
command -v ccache >/dev/null 2>&1 && ok "ccache $(ccache --version | head -1 | grep -oE '[0-9]+\.[0-9]+' || echo ok)" \
                                 || warn "Bỏ qua ccache"

# ---------------------------------------------------------------- 3. ESP-IDF
say "Kiểm tra ESP-IDF ($IDF_VERSION)"
MARKER="$IDF_PATH/.install-complete"
if [ -f "$MARKER" ] && [ -x "$IDF_PATH/export.sh" ]; then
  ok "Đã có sẵn tại $IDF_PATH"
else
  if [ -e "$IDF_PATH" ]; then
    warn "Thư mục $IDF_PATH tồn tại nhưng thiếu/sai -> xoá và cài lại"
    $SUDO rm -rf "$IDF_PATH"
  fi
  warn "Cài ESP-IDF, mất khoảng 5-8 phút (tải ~2.4 GB)..."
  $SUDO mkdir -p "$IDF_PATH"
  $SUDO chown -R "$(id -u):$(id -g)" "$IDF_PATH"
  git clone -b "$IDF_VERSION" --depth 1 --recursive \
    https://github.com/espressif/esp-idf.git "$IDF_PATH"
  ( cd "$IDF_PATH" && ./install.sh esp32,esp32s3 )
  touch "$MARKER"
  ok "Cài xong ESP-IDF $IDF_VERSION"
fi

# ---------------------------------------------------------------- 4. secrets
say "Cấu hình Telegram"
ENV_FILE="$ROOT/.env"
if [ -f "$ENV_FILE" ] && grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE"; then
  ok ".env đã có sẵn"
elif [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
IDF_PATH=$IDF_PATH
IDF_VERSION=$IDF_VERSION
EOF
  chmod 600 "$ENV_FILE"
  ok "Tạo .env từ biến môi trường"
else
  warn "Chưa có token."
  cat <<'EOF'

  Làm theo 1 trong 2 cách:

  (A) Điền file .env thủ công:
        cp bot/.env.example .env
        nano .env

  (B) Truyền thẳng khi chạy:
        TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy ./setup.sh

  Lấy token : @BotFather -> /newbot
  Lấy chatID: nhắn bot, mở https://api.telegram.org/bot<TOKEN>/getUpdates,
              tìm "chat":{"id": ...

EOF
fi

# ---------------------------------------------------------------- 5. chạy bot
say "Khởi động bot"
if [ -f "$ENV_FILE" ] && grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE"; then
  if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
    $SUDO cp "$ROOT/bot/systemd/esp32-bot.service" /etc/systemd/system/
    # Chỉnh path nếu clone ở chỗ khác
    if [ "$ROOT" != "/home/runner/esp32-fastbuild" ]; then
      $SUDO sed -i "s#/home/runner/esp32-fastbuild#$ROOT#g" /etc/systemd/system/esp32-bot.service
    fi
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable esp32-bot >/dev/null 2>&1 || true
    $SUDO systemctl restart esp32-bot
    sleep 3
    if $SUDO systemctl is-active --quiet esp32-bot; then
      ok "Bot đang chạy (systemd)"
      echo "     log: sudo journalctl -u esp32-bot -f"
      echo "     dừng: sudo systemctl stop esp32-bot"
    else
      warn "Service không khởi động, xem log:"
      $SUDO journalctl -u esp32-bot -n 30 --no-pager || true
    fi
  else
    warn "Không có systemd -> chạy nền bằng nohup"
    mkdir -p "$ROOT/bot_state"
    nohup env IDF_PATH="$IDF_PATH" IDF_VERSION="$IDF_VERSION" \
      "$ROOT/bot/run_local.sh" >> /tmp/esp32-bot.log 2>&1 &
    echo $! > "$ROOT/bot_state/bot.pid"
    ok "Bot PID $(cat "$ROOT/bot_state/bot.pid")"
    echo "     log: tail -f /tmp/esp32-bot.log"
  fi
else
  warn "Bỏ qua bước khởi động vì thiếu token. Làm xong .env thì chạy lại ./setup.sh"
fi

say "Xong"