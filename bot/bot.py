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
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
IDF_PATH = os.environ.get("IDF_PATH", "/opt/esp/idf")
IDF_VERSION = os.environ.get("IDF_VERSION", "v5.1.4")

# Targets to build. Override with BOT_TARGETS="esp32,esp32s3" if you need more.
TARGETS = [t.strip() for t in
           os.environ.get("BOT_TARGETS", "esp32").split(",") if t.strip()]
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

# When no chat id is configured yet, the bot locks onto whoever messages it
# first and persists that id, so the setup only needs the BotFather token.
CHAT_FILE = Path(os.environ.get("TELEGRAM_CHAT_FILE", "bot_state/chat_id"))


def load_chat_id():
    global CHAT_ID
    if CHAT_ID:
        return CHAT_ID
    try:
        CHAT_ID = CHAT_FILE.read_text().strip()
    except Exception:
        CHAT_ID = ""
    return CHAT_ID


def lock_chat(chat_id):
    global CHAT_ID
    CHAT_ID = str(chat_id)
    try:
        CHAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHAT_FILE.write_text(CHAT_ID)
    except Exception as e:
        log(f"could not persist chat id: {e}")


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
        # Self-heal: a stray `mkdir -p` on this path would leave a directory
        # here, and the offset would silently never persist (causing duplicate
        # firmware sends after every restart).
        if OFFSET_FILE.is_dir():
            log(f"{OFFSET_FILE} was a directory - replacing with file")
            shutil.rmtree(OFFSET_FILE, ignore_errors=True)
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


class Status:
    """A single Telegram message that is edited in place to show progress.

    Editing one message avoids flooding the chat, but the Bot API rate-limits
    edits hard. Every edit is therefore throttled, and a 429 makes us back off
    for exactly as long as Telegram asks instead of retrying into the wall.
    """

    BAR = "░░░░░░░░░░"

    def __init__(self, initial, reply_to=None, min_interval=6.0):
        self.reply_to = reply_to
        self.min_interval = min_interval
        self.message_id = None
        self.last_text = None
        self.last_send = 0.0
        self.blocked_until = 0.0
        r = send_message(initial, reply_to)
        if r and r.get("ok"):
            self.message_id = r["result"]["message_id"]

    def update(self, text, force=False):
        now = time.time()
        if not self.message_id:
            return
        if now < self.blocked_until:
            return
        # Throttle unless this is a discrete state change. force=True must
        # never be used from the per-step build callback: that would fire one
        # API call per ninja line (~900 calls) and trip HTTP 429.
        if not force and now - self.last_send < self.min_interval:
            return
        if text == self.last_text:
            return
        self.last_send = now
        self.last_text = text
        r = api("editMessageText",
                {"chat_id": CHAT_ID, "message_id": self.message_id,
                 "text": text, "disable_web_page_preview": True},
                timeout=20)
        if r and not r.get("ok"):
            retry = 20
            params = r.get("parameters") or {}
            if isinstance(params, dict):
                retry = params.get("retry_after", retry)
            self.blocked_until = time.time() + float(retry) + 1
            log(f"rate limited, pausing progress updates for {retry}s")

    def bar(self, done, total):
        frac = (done / total) if total else 0.0
        filled = max(0, min(10, round(frac * 10)))
        return "▓" * filled + self.BAR[len("▓") * filled:]


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
def safe_name(name):
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:60] or "project"


def sync_extract(zip_path: Path, dest: Path):
    """Extract a zip into a STABLE directory, keeping paths constant.

    ccache includes the source file path in its hash, so extracting into a
    fresh directory per build (bot_work/j<timestamp>/...) gave a ~8% hit rate.
    Reusing one directory per project keeps the paths identical and lets
    ccache hit on everything except files that actually changed.

    A manifest of extracted paths is kept so files deleted from the new zip
    do not linger and get compiled into stale code.
    """
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = dest / ".bot_manifest"

    with zipfile.ZipFile(zip_path) as z:
        if len(z.namelist()) > 20000:
            raise RuntimeError("Zip chứa quá nhiều file (>20000)")
        names = [n for n in z.namelist() if not n.endswith("/")]
        # Refuse path traversal before extracting anything.
        for n in names:
            target = (dest / n).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise RuntimeError(f"Zip chứa đường dẫn không hợp lệ: {n}")
        # Ignore build output the uploader zipped in: its absolute paths and
        # stale CMake cache never help, and our own persistent build dir is
        # what makes the next upload incremental.
        kept = []
        for n in names:
            parts = Path(n).parts
            base = Path(n).name
            if base in ("sdkconfig", "sdkconfig.old") or "build" in parts:
                continue
            kept.append(n)
        z.extractall(dest, members=kept)

    # Drop files that existed in the previous upload but not in this one.
    old = set()
    if manifest_path.is_file():
        try:
            old = set(manifest_path.read_text().splitlines())
        except Exception:
            old = set()
    new = set(kept)
    for stale in old - new:
        p = dest / stale
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
        except Exception:
            pass
    manifest_path.write_text("\n".join(sorted(new)))

    root = find_project_root(dest)
    if root is not None and root != dest:
        return root, dest
    return root, dest


def find_project_root(base: Path):
    """Locate the ESP-IDF project directory inside an extracted zip.

    A project directory has CMakeLists.txt AND a main/ subdirectory. Checking
    only for CMakeLists.txt picks main/ itself, because "main" sorts before
    most project names.
    """

    def looks_like_project(d: Path):
        return (d / "CMakeLists.txt").is_file() and (d / "main").is_dir()

    # Zip may have no wrapping folder at all.
    if looks_like_project(base):
        return base

    for child in sorted(base.iterdir()):
        if child.is_dir() and looks_like_project(child):
            return child

    # Fallback: search deeper, ignoring build output. Prefer real projects.
    fallback = None
    for cm in sorted(base.rglob("CMakeLists.txt")):
        parts = cm.relative_to(base).parts
        if len(parts) > 4 or "build" in parts:
            continue
        parent = cm.parent
        if looks_like_project(parent):
            return parent
        if (fallback is None and parent.name != "main"
                and parent != base
                and (parent / "CMakeLists.txt").is_file()):
            fallback = parent
    # Return a plausible root anyway so preflight can report the real problem
    # ("thiếu main/") instead of a vague "no CMakeLists.txt found".
    if fallback is not None:
        return fallback
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
    """Run a shell command under bash.

    Must NOT use /bin/sh: ESP-IDF's export.sh uses bash-only syntax such as
    [[ ]] and would abort with "[[: not found" under dash.
    """
    if isinstance(cmd, str):
        return subprocess.run(["/bin/bash", "-c", cmd], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def extract_errors(text):
    """Pull the lines a human actually needs out of an IDF build log.

    A failing build ends with hundreds of lines of unrelated linker noise, so
    returning only the tail buries the real error.
    """
    if not text:
        return text
    keep = []
    for line in text.splitlines():
        low = line.lower()
        if ("error:" in low or "fatal:" in low or "undefined reference" in low
                or "traceback" in low or "::error" in low
                or "command failed" in low or "no such file" in low):
            keep.append(line)
    if keep:
        return "\n".join(keep[-12:])
    return text[-1200:]


PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")


def run_streaming(cmd, cwd, on_progress=None, timeout=2400):
    """Run under bash, streaming stdout so build progress can be reported.

    Returns (returncode, combined_output). Uses bash because ESP-IDF's
    export.sh is bash-only.
    """
    proc = subprocess.Popen(["/bin/bash", "-c", cmd], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    lines = []
    deadline = time.time() + timeout
    rc = 1
    try:
        try:
            for line in proc.stdout:
                lines.append(line)
                if on_progress:
                    m = PROGRESS_RE.search(line)
                    if m:
                        on_progress(int(m.group(1)), int(m.group(2)))
                if time.time() > deadline:
                    proc.kill()
                    log("build timed out")
                    return 124, "".join(lines)[-4000:]
        finally:
            # Must always reap: otherwise a killed/timed-out build leaves a
            # zombie child attached to the bot for the rest of the session.
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
    except Exception as e:
        log(f"streaming build aborted: {e}")
        return rc or 1, "".join(lines)[-4000:]
    return rc, "".join(lines)


def configured_target(root: Path):
    """Return the target already configured in build/, or None."""
    try:
        import json as _json
        p = root / "build" / "project_description.json"
        if p.is_file():
            return _json.loads(p.read_text()).get("target")
    except Exception:
        pass
    return None


def build_target(root: Path, target: str, status=None, prefix=""):
    """Build one target; return (ok, merged_bin_path_or_None, log_text)."""
    logdir = root / "build" / "log"
    try:
        # set-target wipes build/ and reconfigures CMake from scratch, which
        # costs a full 40s rebuild every upload. Skip it when the existing
        # build dir is already configured for this same target: ninja then
        # rebuilds only the files that actually changed (~1s for one file).
        current = configured_target(root)
        if current == target and (root / "build" / "build.ninja").is_file():
            log(f"{prefix or target}: build/ already set for {target}, skipping set-target")
        else:
            p1 = run(f". {IDF_PATH}/export.sh && idf.py "
                     f"-DCMAKE_C_COMPILER_LAUNCHER=ccache "
                     f"-DCMAKE_CXX_COMPILER_LAUNCHER=ccache set-target {target}",
                     cwd=root, timeout=900)
            if p1.returncode != 0:
                return False, None, extract_errors(p1.stdout + p1.stderr)

        state = {"t0": time.time()}

        def cb(done, total):
            if status is None:
                return
            pct = int(done * 100 / total) if total else 0
            elapsed = int(time.time() - state["t0"])
            rate = done / max(elapsed, 1)
            # Early steps are cheap (config, codegen) and later ones are heavy
            # compiles, so an ETA computed at 7% wildly overshoots. Show it only
            # once enough of the build has run to be meaningful.
            if rate > 0 and pct >= 15:
                eta = int((total - done) / rate)
                timing = f"⏱ {elapsed}s đã trôi qua · ETA ~{eta}s"
            else:
                timing = f"⏱ {elapsed}s · đang ước lượng…"
            # No force=True here: this fires once per ninja line (~900 times)
            # and rate limiting would freeze the progress bar entirely.
            status.update(
                f"🔨 <b>{prefix}</b> {status.bar(done, total)} "
                f"<b>{pct}%</b>  {done}/{total}\n{timing}")

        rc, out = run_streaming(
            f". {IDF_PATH}/export.sh && idf.py "
            f"-DCMAKE_C_COMPILER_LAUNCHER=ccache "
            f"-DCMAKE_CXX_COMPILER_LAUNCHER=ccache build",
            cwd=root, on_progress=cb)
        if rc != 0:
            return False, None, extract_errors(out)
        logtxt = ""
        if logdir.is_dir():
            for f in sorted(logdir.glob("idf_py_std*")):
                logtxt += f.read_text(errors="replace")[-6000:]
        return True, root / "build", logtxt
    except subprocess.TimeoutExpired:
        return False, None, "build timeout"


def merge_firmware(build_dir: Path, target: str, out: Path):
    """Bundle bootloader + partition table + app into one flashable image.

    ota_data_initial.bin only exists when the partition table declares OTA app
    slots. Assembling the layout from what is actually on disk avoids a merge
    failure on every non-OTA project that otherwise built fine.
    """
    app = None
    for p in sorted(build_dir.glob("*.bin")):
        if not any(k in p.name for k in ("bootloader", "partition", "ota_data")):
            app = p
            break
    if app is None:
        log("no application .bin found for merging")
        return None

    bootloader = build_dir / "bootloader" / "bootloader.bin"
    partition = build_dir / "partition_table" / "partition-table.bin"
    ota = build_dir / "ota_data_initial.bin"

    # Bootloader, partition table and app are all mandatory for a flashable
    # image; only ota_data_initial.bin is optional.
    if not (bootloader.is_file() and partition.is_file()):
        missing = [n for n, p in (("bootloader.bin", bootloader),
                                   ("partition-table.bin", partition))
                   if not p.is_file()]
        log(f"missing {missing}, cannot merge a flashable image")
        return None

    parts = [("0x0", bootloader), ("0x8000", partition)]
    if ota.is_file():
        parts.append(("0xe000", ota))
    parts.append(("0x10000", app))

    args = " ".join(f"{addr} {path}" for addr, path in parts)
    cmd = (
        f". {IDF_PATH}/export.sh && python -m esptool --chip {target} merge_bin "
        f"--format raw --flash_mode dio --flash_freq 40m --flash_size 4MB "
        f"-o {out} {args}"
    )
    p = run(cmd, timeout=300)
    if p.returncode != 0:
        log(f"merge failed: {(p.stdout + p.stderr)[-400:]}")
        return None
    return out if out.is_file() else None


def build_and_reply(zip_path: Path, msg_id: int, label: str):
    # One stable directory per zip name keeps ccache paths identical between
    # uploads, which is what turns a 8% hit rate into ~100%.
    workdir = WORK / safe_name(Path(label).stem)
    try:
        try:
            root, srcroot = sync_extract(zip_path, workdir / "src")
        except zipfile.BadZipFile:
            send_message("❌ File không phải zip hợp lệ (có thể bị zip lại?).", msg_id)
            return
        except RuntimeError as e:
            send_message(f"❌ {e}", msg_id)
            return

        if root is None:
            send_message("❌ Không tìm thấy CMakeLists.txt trong zip.", msg_id)
            return

        # Our own build/ and sdkconfig are KEPT between uploads so ninja does
        # an incremental build. Anything the zip itself carries under those
        # names was already filtered out during extraction.

        err = preflight(root)
        if err:
            send_message(f"❌ Kiểm tra project thất bại: {err}", msg_id)
            return

        status = Status(f"⏳ Đã nhận `{label}`\n\n🔨 Chuẩn bị build {', '.join(TARGETS)}...",
                        msg_id)
        t0 = time.time()
        results = []
        for idx, target in enumerate(TARGETS):
            ok, build_dir, out = build_target(
                root, target, status=status,
                prefix=f"[{idx+1}/{len(TARGETS)}] {target}")
            if ok:
                merged = merge_firmware(build_dir, target,
                                        root / f"{root.name}-{target}.bin")
                results.append((target, merged, out))
                if merged:
                    status.update(
                        f"✅ <b>{target}</b> xong — "
                        f"{merged.stat().st_size // 1024} KB\n"
                        f"Tiếp tục target tiếp theo...", force=True)
            else:
                results.append((target, None, out))
                break  # a broken project will break the next target too
        dt = int(time.time() - t0)

        for target, merged, out in results:
            if merged is None:
                err_txt = extract_errors(out)
                status.update(
                    f"❌ <b>{target}</b> build lỗi sau {dt}s\n\n"
                    f"<pre>{_escape(err_txt)}</pre>\n\n"
                    f"💡 Sửa lỗi rồi gửi lại zip.",
                    force=True)
            else:
                kb = merged.stat().st_size // 1024
                upload_document(
                    merged,
                    f"📦 <b>{target}</b> — FILE ĐÃ GỘP ({kb} KB)\n"
                    f"Đã gộp sẵn: bootloader + partition table"
                    f"{' + OTA data' if (root / 'build' / 'ota_data_initial.bin').is_file() else ''}"
                    f" + app.\n\n"
                    f"⬇️ Tải file đính kèm này, flash 1 lần:\n"
                    f"<code>esptool.py -p /dev/ttyUSB0 write_flash 0x0 {merged.name}</code>\n\n"
                    f"⚠️ Không cần flash riêng file .bin nào khác.",
                    msg_id,
                )
                log(f"sent {merged.name} ({kb} KB)")
        if all(r[1] for r in results):
            status.update(f"✅ Hoàn tất {dt}s — đã gửi "
                          f"{len(results)} file .bin.", force=True)
        else:
            status.update(f"⏹ Dừng sau {dt}s.", force=True)
    finally:
        # NOT deleted: keeping this directory is what makes ccache and ninja
        # work on the next upload. Only the downloaded zip is transient.
        for leftover in workdir.glob("*.bin"):
            leftover.unlink(missing_ok=True)
        # Sweep any zip left behind by an earlier failed/crashed build.
        for old_zip in WORK.glob("in-*.zip"):
            try:
                if old_zip.stat().st_mtime < time.time() - 3600:
                    old_zip.unlink()
            except Exception:
                pass


def _escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    WORK.mkdir(parents=True, exist_ok=True)
    known = load_chat_id()
    log(f"chat id: {known or 'chưa thiết lập (sẽ lấy từ người nhắn đầu tiên)'}")
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

    send_message("🤖 Bot đã sẵn sàng. Gửi file .zip dự án ESP-IDF để build.") \
        if CHAT_ID else log("Chưa có chat id — nhắn /start cho bot để thiết lập.")

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
            if not CHAT_ID and chat.get("id"):
                # First person to talk to an unconfigured bot owns it.
                lock_chat(chat["id"])
                log(f"locked onto chat {CHAT_ID}")
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