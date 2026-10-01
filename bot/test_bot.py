#!/usr/bin/env python3
"""
Test the bot against a local mock of the Telegram Bot API.

Exercises the parts that are easy to get wrong: project-root discovery,
preflight checks, zip extraction, message formatting and the poll loop.
Run with:  python3 bot/test_bot.py
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
os.environ.setdefault("TELEGRAM_CHAT_ID", "111")
os.environ["TELEGRAM_API_BASE"] = "http://127.0.0.1:8799/botTESTTOKEN"
os.environ["TELEGRAM_FILE_BASE"] = "http://127.0.0.1:8799/file/botTESTTOKEN"

import bot  # noqa: E402

SENT = []
EDITS = []
SERVED = {}

PROJECT_FILES = {
    "myproj/CMakeLists.txt": "cmake_minimum_required(VERSION 3.16)\n",
    "myproj/main/CMakeLists.txt": 'idf_component_register(SRCS "app_main.c")\n',
    "myproj/main/app_main.c": "void app_main(void) {}\n",
    "myproj/partitions.csv": "nvs, data, nvs, 0x9000, 0x5000,\n",
    "myproj/sdkconfig.defaults": (
        'CONFIG_IDF_TARGET="esp32"\n'
        'CONFIG_PARTITION_TABLE_CUSTOM=y\n'
        'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"\n'
    ),
}


class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/getMe"):
            return self._json({"ok": True, "result": {"username": "mockbot"}})
        if "/getFile" in path:
            return self._json({"ok": True, "result": {"file_path": "docs/in.zip"}})
        if "/file/" in path:
            data = SERVED.get("zip", b"")
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"ok": False}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        path = self.path.split("?")[0]
        if path.endswith("/sendMessage"):
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"raw": raw.decode(errors="replace")[:200]}
            SENT.append(("message", payload.get("text", "")))
            return self._json({"ok": True, "result": {"message_id": 1}})
        if path.endswith("/sendDocument"):
            ctype = self.headers.get("Content-Type", "")
            fname = ""
            for part in raw.split(b"----ESP32BOTBOUNDARY9f3a"):
                if b'name="document"; filename="' in part:
                    fname = part.split(b'filename="')[1].split(b'"')[0].decode()
            cap = ""
            if b'name="caption"' in raw:
                cap = raw.split(b'name="caption"')[-1].split(b"\r\n\r\n")[1].split(b"\r\n--")[0].decode(errors="replace")
            SENT.append(("document", fname, len(raw), cap))
            assert "multipart/form-data" in ctype, ctype
            return self._json({"ok": True, "result": {"message_id": 2}})
        if path.endswith("/getMe"):
            return self._json({"ok": True, "result": {"username": "mockbot"}})
        if path.endswith("/getFile"):
            return self._json({"ok": True, "result": {"file_path": "docs/in.zip"}})
        if path.endswith("/editMessageText"):
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            EDITS.append(payload.get("text", ""))
            return self._json({"ok": True, "result": {"message_id": 9}})
        if path.endswith("/getUpdates"):
            if "sent" not in SERVED:
                SERVED["sent"] = True
                return self._json({
                    "ok": True,
                    "result": [{
                        "update_id": 42,
                        "message": {
                            "message_id": 7,
                            "chat": {"id": 111},
                            "document": {
                                "file_id": "abc",
                                "file_name": "myproj.zip",
                            },
                        },
                    }],
                })
            return self._json({"ok": True, "result": []})
        return self._json({"ok": False}, 404)


def make_zip(files):
    buf = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(buf.name, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return Path(buf.name)


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8799), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name} {detail}")

    print("== find_project_root ==")
    tmp = Path(tempfile.mkdtemp())
    z = make_zip(PROJECT_FILES)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(tmp / "src")
    root = bot.find_project_root(tmp / "src")
    check("finds nested root dir", root is not None and root.name == "myproj", root)

    print("== preflight ==")
    check("valid project passes", bot.preflight(root) is None, bot.preflight(root))

    bad = dict(PROJECT_FILES)
    del bad["myproj/partitions.csv"]
    tmp2 = Path(tempfile.mkdtemp())
    z2 = make_zip(bad)
    with zipfile.ZipFile(z2) as zf:
        zf.extractall(tmp2 / "src")
    r2 = bot.find_project_root(tmp2 / "src")
    err = bot.preflight(r2)
    check("missing partitions.csv detected", err is not None and "partitions.csv" in err, err)

    tmp3 = Path(tempfile.mkdtemp())
    z3 = make_zip({"x/CMakeLists.txt": "x", "x/README.md": "y"})
    with zipfile.ZipFile(z3) as zf:
        zf.extractall(tmp3 / "src")
    r3 = bot.find_project_root(tmp3 / "src")
    check("missing main/ detected", r3 is not None and bot.preflight(r3) is not None)

    print("== zip with build/ junk is tolerated ==")
    junk = dict(PROJECT_FILES)
    junk["myproj/build/CMakeCache.txt"] = "garbage"
    junk["myproj/sdkconfig"] = "garbage"
    tmp4 = Path(tempfile.mkdtemp())
    z4 = make_zip(junk)
    with zipfile.ZipFile(z4) as zf:
        zf.extractall(tmp4 / "src")
    r4 = bot.find_project_root(tmp4 / "src")
    check("root found despite build/", r4 is not None and r4.name == "myproj", r4)
    check("preflight ok despite junk", bot.preflight(r4) is None)

    print("== message building ==")
    srv_payload = bot.send_message("hello <b>x</b> & y", 5)
    check("sendMessage accepted", srv_payload and srv_payload.get("ok") is True)
    check("html escaped for safety", bot._escape("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;")

    print("== offset persistence ==")
    st = Path(tempfile.mkdtemp()) / "offset"
    os.environ["TELEGRAM_OFFSET_FILE"] = str(st)
    bot.OFFSET_FILE = st
    check("no offset file -> None", bot.load_offset() is None)
    bot.save_offset(99)
    check("offset round-trips", bot.load_offset() == 99, bot.load_offset())
    check("offset written to disk", st.read_text().strip() == "99")

    # Regression: run_local.sh once did `mkdir -p $TELEGRAM_OFFSET_FILE`,
    # leaving a DIRECTORY at that path. The offset then never persisted and the
    # bot re-sent firmware for zips it had already handled after each restart.
    stdir = Path(tempfile.mkdtemp()) / "offset"
    stdir.mkdir()
    bot.OFFSET_FILE = stdir
    bot.save_offset(7)
    check("save_offset replaces dir at path with a file",
          stdir.is_file() and stdir.read_text().strip() == "7")
    check("offset loadable after self-heal", bot.load_offset() == 7)

    print("== progress bar ==")
    check("0% = empty bar", bot.Status("").bar(0, 100) == "░" * 10,
          bot.Status("").bar(0, 100))
    check("50% = half bar", bot.Status("").bar(50, 100) == "▓" * 5 + "░" * 5,
          bot.Status("").bar(50, 100))
    check("100% = full bar", bot.Status("").bar(100, 100) == "▓" * 10,
          bot.Status("").bar(100, 100))
    check("clamped above 100", bot.Status("").bar(150, 100) == "▓" * 10)
    check("no div by zero when total=0", isinstance(bot.Status("").bar(0, 0), str))

    print("== ninja progress parsing ==")
    check("parses [12/340]", bot.PROGRESS_RE.search("[12/340] Building C") is not None)
    m = bot.PROGRESS_RE.search("[803/1150] Linking C executable")
    check("extracts done,total", m and m.group(1) == "803" and m.group(2) == "1150")
    check("ignores non-progress lines",
          bot.PROGRESS_RE.search("ninja: build stopped") is None)

    print("== streaming build output ==")
    rc, out = bot.run_streaming(
        "for i in 1 2 3; do echo \"[$i/3] step $i\"; done", cwd="/tmp")
    check("streaming returns rc 0", rc == 0, rc)
    check("captured all output", out.count("step") == 3, out)
    seen = []
    rc2, _ = bot.run_streaming(
        "for i in 1 2 3; do echo \"[$i/3] s\"; done", cwd="/tmp",
        on_progress=lambda d, t: seen.append((d, t)))
    check("progress callback fired", seen == [(1, 3), (2, 3), (3, 3)], seen)

    print("== extract_errors ==")
    noisy = "\n".join([f"[{i}/900] Building C object x{i}" for i in range(500)])
    log = noisy + "\nmain/web_server.c:217:75: error: format '%s' bad\nninja: build stopped"
    ex = bot.extract_errors(log)
    check("real error surfaced from noise", "web_server.c:217" in ex)
    check("noise trimmed", ex.count("Building C object") <= 2, ex.count("Building C object"))
    check("empty log is safe", bot.extract_errors("") is not None)
    check("log without errors returns tail", "ninja" in bot.extract_errors("ninja failed"))

    print("== find_project_root picks the project, not main/ ==")
    # Zip WITHOUT a wrapping folder (files at top level) - the case that broke
    # the bot, because "main" sorts before most project names.
    flat = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(make_zip({"CMakeLists.txt": "x", "main/CMakeLists.txt": "y"})) as zf:
        zf.extractall(flat)
    got = bot.find_project_root(flat)
    check("flat zip -> root, not main/", got == flat, got)
    check("flat zip has main/", (got / "main").is_dir())

    nested = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(make_zip({"aaa/main/CMakeLists.txt": "y",
                                   "zzz/CMakeLists.txt": "x"})) as zf:
        zf.extractall(nested)
    got2 = bot.find_project_root(nested)
    check("wrapper dir -> its child", got2 is not None and got2.name == "zzz", got2)

    deep = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(make_zip({"a/b/proj/CMakeLists.txt": "x",
                                   "a/b/proj/main/CMakeLists.txt": "y"})) as zf:
        zf.extractall(deep)
    got3 = bot.find_project_root(deep)
    check("deeply nested zip found", got3 is not None and got3.name == "proj", got3)

    check("no project -> None",
          bot.find_project_root(Path(tempfile.mkdtemp())) is None)

    print("== stable workspace (ccache depends on it) ==")
    ws = Path(tempfile.mkdtemp()) / "src"
    zA = make_zip({"p/CMakeLists.txt": "v1", "p/main/CMakeLists.txt": "x",
                   "p/build/CMakeCache.txt": "junk",
                   "p/sdkconfig": "junk"})
    bot.sync_extract(zA, ws)
    check("build/ from zip is dropped", not (ws / "p" / "build").exists())
    check("sdkconfig from zip is dropped", not (ws / "p" / "sdkconfig").exists())
    check("real sources extracted", (ws / "p" / "CMakeLists.txt").is_file())

    # Second upload of the same project must land in the SAME paths.
    root_before = str(bot.find_project_root(ws))
    zB = make_zip({"p/CMakeLists.txt": "v2", "p/main/CMakeLists.txt": "x"})
    bot.sync_extract(zB, ws)
    check("path identical across uploads",
          str(bot.find_project_root(ws)) == root_before, root_before)

    # A file dropped in the new upload must not linger from the old one.
    check("content updated in place",
          (ws / "p" / "CMakeLists.txt").read_text() == "v2")

    zC = make_zip({"p/CMakeLists.txt": "v3", "p/main/CMakeLists.txt": "x",
                   "p/extra.h": "new"})
    bot.sync_extract(zC, ws)
    check("new file present after upload C", (ws / "p" / "extra.h").is_file())

    # Upload D drops extra.h again: it must not linger and get compiled.
    zD = make_zip({"p/CMakeLists.txt": "v4", "p/main/CMakeLists.txt": "x"})
    bot.sync_extract(zD, ws)
    check("stale files removed from previous upload",
          not (ws / "p" / "extra.h").exists() and
          (ws / "p" / "CMakeLists.txt").read_text() == "v4")

    print("== zip safety ==")
    evil = make_zip({"../escape.txt": "bad", "p/CMakeLists.txt": "x"})
    try:
        bot.sync_extract(evil, Path(tempfile.mkdtemp()) / "s")
        check("path traversal rejected", False, "không raise")
    except RuntimeError:
        check("path traversal rejected", True)

    print("== safe_name ==")
    check("strips slashes", "/" not in bot.safe_name("a/b/c"))
    check("keeps normal name", bot.safe_name("my-proj_v1") == "my-proj_v1")

    print("== progress updates are throttled (429 regression) ==")
    # Regression: the per-step callback used force=True, bypassing the
    # throttle and firing ~900 API calls per build -> HTTP 429 flooded the
    # chat and froze the progress bar.
    EDITS.clear()
    st = bot.Status("start", None)
    st.message_id = 1
    st.last_send = time.time()
    st.last_text = None
    st.blocked_until = 0.0
    for i in range(900):
        st.update(f"step {i}/900")          # exactly how the callback calls it
    check("900 steps -> few API calls", len(EDITS) <= 2, len(EDITS))

    EDITS.clear()
    st2 = bot.Status("start", None)
    st2.message_id = 2
    st2.last_send = 0.0
    st2.last_text = None
    st2.blocked_until = 0.0
    st2.min_interval = 0.0
    for i in range(5):
        st2.update(f"same text")
    check("identical text not re-sent", len(EDITS) <= 1, len(EDITS))

    st3 = bot.Status("s", None)
    st3.message_id = 3
    st3.blocked_until = time.time() + 30
    st3.last_send = 0.0
    st3.last_text = None
    EDITS.clear()
    st3.update("during backoff", force=True)
    check("backoff blocks even force updates", len(EDITS) == 0, len(EDITS))

    print("== no zombie child after streaming ==")
    rc3, _ = bot.run_streaming("sleep 0.1; echo '[1/1] done'", cwd="/tmp")
    check("fast command returns", rc3 == 0, rc3)
    rc4, out4 = bot.run_streaming(
        "for i in $(seq 1 5); do echo \"[$i/5] s\"; done; exit 3", cwd="/tmp")
    check("propagates exit code", rc4 == 3, rc4)
    rc5, _ = bot.run_streaming("echo '[1/1] x'; kill -9 $$", cwd="/tmp")
    check("survives killed child", rc5 != 0)

    print("== set-target skipped when target unchanged ==")
    bt = Path(tempfile.mkdtemp())
    (bt / "build").mkdir()
    (bt / "build" / "build.ninja").write_text("")
    (bt / "build" / "project_description.json").write_text('{"target": "esp32"}')
    check("reads configured target", bot.configured_target(bt) == "esp32",
          bot.configured_target(bt))
    (bt / "build" / "project_description.json").write_text('{"target": "esp32s3"}')
    check("detects different target", bot.configured_target(bt) == "esp32s3")
    (bt / "build" / "project_description.json").unlink()
    check("no description -> None", bot.configured_target(bt) is None)
    (bt / "build" / "project_description.json").write_text("{not json")
    check("broken json -> None, no crash", bot.configured_target(bt) is None)
    (bt / "build" / "project_description.json").write_text('{"target": "esp32"}')
    (bt / "build" / "build.ninja").unlink()
    check("no build.ninja -> must reconfigure",
          not (bot.configured_target(bt) == "esp32"
               and (bt / "build" / "build.ninja").is_file()))

    print("== merge handles projects without OTA partitions ==")
    # Regression: ota_data_initial.bin is hardcoded into the merge command.
    # Non-OTA projects build fine but then fail at merge with
    # "[Errno 2] No such file: build/ota_data_initial.bin".
    mb = Path(tempfile.mkdtemp()) / "build"
    (mb / "bootloader").mkdir(parents=True)
    (mb / "partition_table").mkdir(parents=True)
    (mb / "bootloader" / "bootloader.bin").write_bytes(b"\xe9" + b"\0" * 32)
    (mb / "partition_table" / "partition-table.bin").write_bytes(b"\xaa" + b"\0" * 32)
    (mb / "myapp.bin").write_bytes(b"\xbb" + b"\0" * 32)
    check("no ota file present", not (mb / "ota_data_initial.bin").exists())
    out = mb.parent / "merged.bin"
    bot.esptool_stub = True
    real_run = bot.run
    captured = {}

    def fake_run(cmd, cwd=None, timeout=None):
        captured["cmd"] = cmd
        out.write_bytes(b"\xcc" * 64)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    bot.run = fake_run
    try:
        res = bot.merge_firmware(mb, "esp32", out)
        cmd = captured.get("cmd", "")
    finally:
        bot.run = real_run
    check("merge succeeds without ota_data", res is not None, res)
    check("does not reference missing ota file",
          "ota_data_initial" not in cmd, cmd[:160])
    check("still includes bootloader + partition + app",
          all(k in cmd for k in ("0x0", "0x8000", "0x10000")))

    (mb / "ota_data_initial.bin").write_bytes(b"\xdd" + b"\0" * 32)
    captured.clear()
    bot.run = fake_run
    try:
        bot.merge_firmware(mb, "esp32s3", out)
        cmd = captured.get("cmd", "")
    finally:
        bot.run = real_run
    check("includes ota when present", "0xe000" in cmd and "ota_data_initial" in cmd)

    (mb / "bootloader" / "bootloader.bin").unlink()
    captured.clear()
    bot.run = fake_run
    try:
        res2 = bot.merge_firmware(mb, "esp32", out)
    finally:
        bot.run = real_run
    check("refuses to merge without bootloader", res2 is None, res2)

    print("== end-to-end poll loop (build stubbed) ==")
    SERVED["zip"] = z.read_bytes()
    calls = {"build": 0}

    def fake_build(root, target, status=None, prefix=""):
        calls["build"] += 1
        d = root / "build"
        d.mkdir(exist_ok=True)
        if status:
            for pct in (25, 50, 100):
                status.update(
                    f"🔨 {prefix} {status.bar(pct, 100)} {pct}% {pct}/100",
                    force=True)
        return True, d, ""

    orig = bot.build_target
    orig_merge = bot.merge_firmware
    bot.build_target = fake_build

    def fake_merge(build_dir, target, out):
        out.write_bytes(b"\xe9" + b"\x00" * 100)
        return out

    bot.merge_firmware = fake_merge

    bot.IDLE_EXIT_SECONDS = 2
    bot.MAX_SECONDS = 12
    rc = bot.main()
    bot.build_target = orig
    bot.merge_firmware = orig_merge

    docs = [s for s in SENT if s[0] == "document"]
    texts = [s[1] for s in SENT if s[0] == "message"]
    check("bot exited cleanly", rc == 0, rc)
    check("built both targets", calls["build"] == 2, calls["build"])
    check("sent 2 merged .bin", len(docs) == 2, len(docs))
    check("caption names target", all(t in " ".join(d[1] + d[3] for d in docs)
                                      for t in ("esp32", "esp32s3")))
    check("acknowledged receipt", any("Đã nhận" in t for t in texts), texts[:2])
    check("progress edits were sent", len(EDITS) >= 4, len(EDITS))
    check("progress shows a bar", any("░" in e or "▓" in e for e in EDITS), EDITS[:2])
    check("progress shows percentage", any("25%" in e or "50%" in e for e in EDITS))
    check("progress names the target", any("esp32" in e for e in EDITS))
    check("final status reports done", any("Hoàn tất" in e for e in EDITS), EDITS[-1:])
    check("no token leaked in text", all("TESTTOKEN" not in t for t in texts))

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())