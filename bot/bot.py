#!/usr/bin/env python3
"""
Telegram bot that builds ESP-IDF projects sent as .zip documents and replies
with a single merged .bin firmware.

Runs inside one GitHub Actions job (max 6h). Polls Telegram with long-polling;
exits early once IDLE_EXIT_MINUTES pass with no activity, so we do not burn a
runner for 6 hours when nobody is using the bot.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
IDF_PATH = os.environ.get("IDF_PATH", "/opt/esp/idf")
IDF_VERSION = os.environ.get("IDF_VERSION", "v5.1.4")

TARGETS = ["esp32", "esp32s3"]
WORK = Path(os.environ.get("BOT_WORKDIR", "bot_work"))
POLL_TIMEOUT = 50
IDLE_EXIT_SECONDS = int(os.environ.get("IDLE_EXIT_MINUTES", "30")) * 60
MAX_SECONDS = int(os.environ.get("MAX_RUN_MINUTES", "340")) * 60

# Base URL is overridable so the test suite can point at a local mock server.
TG = os.environ.get("TELEGRAM_API_BASE", f"https://api.telegram.org/bot{TOKEN}")
FILE_BASE = os.environ.get(
    "TELEGRAM_FILE_BASE", f"https://api.telegram.org/file/bot{TOKEN}"
)

# Telegram keeps pending updates for ~24h. Without persisting the offset, every
# job restart would reprocess and re-send firmware for the same zip.
OFFSET_FILE = Path(os.environ.get("TELEGRAM_OFFSET_FILE", "bot_state/offset"))


def load_offset():
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return None


def save_offset(offset):
    if not offset:
        return
    try:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(str(offset))
    except Exception as e:
        log(f"could not persist offset: {e}")


def log(msg):
    print(f"[bot] {msg}", flush=True)


# --------------------------------------------------------------------------
# Telegram helpers
# --------------------------------------------------------------------------
def api(method, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{TG}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log(f"HTTP {e.code} on {method}: {body[:300]}")
        return None
    except Exception as e:  # network hiccup - caller retries
        log(f"error on {method}: {e}")
        return None


def send_message(text, reply_to=None):
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return api("sendMessage", payload)


def upload_document(path: Path, caption: str, reply_to=None):
    """Multipart upload of a file to sendDocument."""
    boundary = "----ESP32BOTBOUNDARY9f3a"
    name = path.name.encode()
    caption_b = caption.encode()
    crlf = b"\r\n"
    parts = []
    fields = {"chat_id": CHAT_ID}
    if reply_to:
        fields["reply_to_message_id"] = str(reply_to)
    for k, v in fields.items():
        parts.append(b"--" + boundary.encode() + crlf)
        parts.append(f'Content-Disposition: form-data; name="{k}"'.encode() + crlf + crlf)
        parts.append(str(v).encode() + crlf)
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(
        f'Content-Disposition: form-data; name="document"; filename="{name}"'.encode()
        + crlf
        + b"Content-Type: application/octet-stream" + crlf + crlf
    )
    parts.append(path.read_bytes() + crlf)
    parts.append(b"--" + boundary.encode() + crlf)
    parts.append(
        f'Content-Disposition: form-data; name="caption"'.encode() + crlf + crlf
    )
    parts.append(caption_b + crlf)
    parts.append(b"--" + boundary.encode() + b"--" + crlf)
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{TG}/sendDocument", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"sendDocument failed: {e}")
        return None


def download_file(file_id, dest: Path):
    r = api("getFile", {"file_id": file_id})
    if not r or not r.get("ok"):
        return False
    fpath = r["result"]["file_path"]
    url = f"{FILE_BASE}/{fpath}"
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception as e:
        log(f"download failed: {e}")
        return False


# --------------------------------------------------------------------------
# Build logic
# --------------------------------------------------------------------------
def find_project_root(base: Path):
    """Locate the directory holding CMakeLists.txt inside an extracted zip."""
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / "CMakeLists.txt").is_file():
            return child
    # Fallback: search a couple of levels down, ignoring build output.
    for cm in sorted(base.rglob("CMakeLists.txt")):
        parts = cm.relative_to(base).parts
        if len(parts) <= 2 and "build" not in parts:
            return cm.parent
    return None


def preflight(root: Path):
    """Return an error string if the project obviously cannot build."""
    if not (root / "main").is_dir():
        return "thiếu thư mục main/ (project cần có main/CMakeLists.txt)"
    defaults = root / "sdkconfig.defaults"
    if defaults.is_file():
        for line in defaults.read_text(errors="replace").splitlines():
            if "CONFIG_PARTITION_TABLE_CUSTOM_FILENAME" in line:
                csv = line.split('"')
                if len(csv) >= 2 and not (root / csv[1]).is_file():
                    return f"sdkconfig.defaults trỏ tới '{csv[1]}' nhưng thiếu file đó trong zip"
    return None


def run(cmd, cwd=None, timeout=1800):
    return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                          capture_output=True, text=True, timeout=timeout)


def build_target(root: Path, target: str):
    """Build one target; return (ok, merged_bin_path_or_None, log_text)."""
    logdir = root / "build" / "log"
    try:
        p1 = run(f". {IDF_PATH}/export.sh && idf.py "
                 f"-DCMAKE_C_COMPILER_LAUNCHER=ccache "
                 f"-DCMAKE_CXX_COMPILER_LAUNCHER=ccache set-target {target}",
                 cwd=root, timeout=900)
        if p1.returncode != 0:
            return False, None, (p1.stdout + p1.stderr)[-4000:]
        p2 = run(f". {IDF_PATH}/export.sh && idf.py "
                 f"-DCMAKE_C_COMPILER_LAUNCHER=ccache "
                 f"-DCMAKE_CXX_COMPILER_LAUNCHER=ccache build",
                 cwd=root, timeout=1800)
        if p2.returncode != 0:
            return False, None, (p2.stdout + p2.stderr)[-4000:]
        logtxt = ""
        if logdir.is_dir():
            for f in sorted(logdir.glob("idf_py_std*")):
                logtxt += f.read_text(errors="replace")[-6000:]
        return True, root / "build", logtxt
    except subprocess.TimeoutExpired:
        return False, None, "build timeout"


def merge_firmware(build_dir: Path, target: str, out: Path):
    app = None
    for p in sorted(build_dir.glob("*.bin")):
        if not any(k in p.name for k in ("bootloader", "partition", "ota_data")):
            app = p
            break
    if app is None:
        return None
    cmd = (
        f". {IDF_PATH}/export.sh && python -m esptool --chip {target} merge_bin "
        f"--format raw --flash_mode dio --flash_freq 40m --flash_size 4MB "
        f"-o {out} "
        f"0x0 {build_dir}/bootloader/bootloader.bin "
        f"0x8000 {build_dir}/partition_table/partition-table.bin "
        f"0xe000 {build_dir}/ota_data_initial.bin "
        f"0x10000 {app}"
    )
    p = run(cmd, timeout=300)
    return out if p.returncode == 0 and out.is_file() else None


def build_and_reply(zip_path: Path, msg_id: int, label: str):
    workdir = WORK / f"j{int(time.time())}"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    try:
        try:
            with zipfile.ZipFile(zip_path) as z:
                if len(z.namelist()) > 20000:
                    send_message("⚠️ Zip quá nhiều file (>20000), đã bỏ qua.", msg_id)
                    return
                z.extractall(workdir / "src")
        except zipfile.BadZipFile:
            send_message("❌ File không phải zip hợp lệ (có thể bị zip lại?).", msg_id)
            return

        root = find_project_root(workdir / "src")
        if root is None:
            send_message("❌ Không tìm thấy CMakeLists.txt trong zip.", msg_id)
            return

        # Local build leftovers never help and only slow extraction.
        for junk in ("build", "sdkconfig", "sdkconfig.old"):
            shutil.rmtree(root / junk, ignore_errors=True)
            if (root / junk).is_file():
                (root / junk).unlink()

        err = preflight(root)
        if err:
            send_message(f"❌ Kiểm tra project thất bại: {err}", msg_id)
            return

        send_message(f"⏳ Đã nhận `{label}`, bắt đầu build {', '.join(TARGETS)}...", msg_id)
        t0 = time.time()
        results = []
        for target in TARGETS:
            ok, build_dir, out = build_target(root, target)
            if not ok:
                results.append((target, None, out))
                continue
            merged = merge_firmware(build_dir, target, root / f"{root.name}-{target}.bin")
            results.append((target, merged, out))
        dt = int(time.time() - t0)

        for target, merged, out in results:
            if merged is None:
                send_message(
                    f"❌ <b>{target}</b> build lỗi sau {dt}s — xem log bên dưới.\n\n"
                    f"<pre>{_escape(out or 'không rõ')[-1500:]}</pre>",
                    msg_id,
                )
            else:
                kb = merged.stat().st_size // 1024
                upload_document(
                    merged,
                    f"✅ <b>{target}</b> — {kb} KB ({dt}s)\n"
                    f"Flash 1 lần ở offset 0x0:\n"
                    f"esptool.py -p /dev/ttyUSB0 write_flash 0x0 {merged.name}",
                    msg_id,
                )
                log(f"sent {merged.name} ({kb} KB)")
        try:
            api("sendMessage", {"chat_id": CHAT_ID, "text": f"⏱ Hoàn tất sau {dt}s.",
                                "reply_to_message_id": msg_id})
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    WORK.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + MAX_SECONDS
    offset = load_offset()
    if offset:
        log(f"resuming from offset {offset}")
    last_activity = time.time()

    me = api("getMe")
    if me and me.get("ok"):
        log(f"connected as @{me['result']['username']}")
    else:
        log("FATAL: token invalid or network down")
        return 1

    send_message("🤖 Bot đã sẵn sàng. Gửi file .zip dự án ESP-IDF để build.")

    while time.time() < deadline:
        payload = {"timeout": POLL_TIMEOUT}
        if offset:
            payload["offset"] = offset
        payload["allowed_updates"] = ["message"]
        r = api("getUpdates", payload, timeout=POLL_TIMEOUT + 15)
        if r is None or not r.get("ok"):
            time.sleep(10)
            continue

        updates = r.get("result", [])
        if not updates:
            if time.time() - last_activity > IDLE_EXIT_SECONDS:
                log("idle timeout reached, exiting early")
                break
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            # Persist before doing work: if the build is long and the job dies,
            # we must not redo (and re-send) the same document next time.
            save_offset(offset)
            msg = upd.get("message") or {}
            chat = msg.get("chat", {})
            if str(chat.get("id")) != str(CHAT_ID):
                continue  # ignore anyone else
            last_activity = time.time()

            text = (msg.get("text") or "").strip().lower()
            if text.startswith("/start") or text.startswith("/help"):
                send_message(
                    "Gửi file .zip chứa project ESP-IDF (thư mục có CMakeLists.txt ở gốc).\n"
                    "Tôi sẽ build cho esp32 + esp32s3 và gửi lại 1 file .bin đã gộp.\n\n"
                    "/start - hướng dẫn\n/status - trạng thái",
                    msg.get("message_id"),
                )
                continue
            if text.startswith("/status"):
                send_message(
                    f"✅ Online. Uptime {int(time.time() - (deadline - MAX_SECONDS))}s.\n"
                    f"Còn {int(deadline - time.time())}s trước khi nghỉ.",
                    msg.get("message_id"),
                )
                continue

            doc = msg.get("document")
            if not doc:
                continue
            fname = doc.get("file_name", "")
            if not fname.lower().endswith(".zip"):
                send_message(f"❌ Chỉ nhận file .zip. Bạn gửi: `{fname}`",
                             msg.get("message_id"))
                continue

            zpath = WORK / f"in-{int(time.time())}.zip"
            if not download_file(doc["file_id"], zpath):
                send_message("❌ Không tải được file từ Telegram.", msg.get("message_id"))
                continue
            build_and_reply(zpath, msg.get("message_id"), fname)
            zpath.unlink(missing_ok=True)

    send_message("😴 Bot hết giờ chạy, sẽ tự thức lại. Gửi .zip để build tiếp.")
    return 0


if __name__ == "__main__":
    sys.exit(main())