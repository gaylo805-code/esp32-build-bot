# ESP32 Build Bot

Build firmware ESP32 bằng **ESP-IDF v5.1.4** trên GitHub Actions — repo public nên **miễn phí**, có cache nhiều tầng để build sau gần như tức thì.

Có 3 cách dùng:

| Cách | Khi nào dùng |
|---|---|
| 🤖 **Telegram bot** (xem dưới) | Gửi `.zip` trong chat, nhận `.bin` về chat |
| 📦 `uploads/` + push | Build dự án trong repo này |
| 🔧 `workflow_dispatch` | Chạy tay từ tab Actions |

---

## 🤖 Telegram bot (cách chính)

Gửi file `.zip` chứa project ESP-IDF vào chat → bot build cho `esp32` + `esp32s3` → trả lại **2 file `.bin` đã gộp**, mỗi target 1 file, flash ở offset `0x0`.

Lệnh trong chat:

| Lệnh | Tác dụng |
|---|---|
| `/start` | Hướng dẫn |
| `/status` | Thời gian bot đã chạy / còn bao lâu nghỉ |

### Cấu trúc zip

```text
ten_project.zip
└── <tên thư mục tùy ý>/
    ├── CMakeLists.txt        ← bắt buộc
    ├── main/                 ← bắt buộc
    │   ├── CMakeLists.txt
    │   └── *.c
    ├── partitions.csv            (tuỳ chọn)
    ├── sdkconfig.defaults        (tuỳ chọn)
    └── components/               (tuỳ chọn)
```

Tên thư mục gốc không quan trọng, không có thư mục bọc ngoài cũng được — bot tự dò `CMakeLists.txt`.
Zip có kèm `build/` hay `sdkconfig` cũng không sao, bot tự xoá.

### Cách bot hoạt động

Bot **không chạy 24/7** — GitHub Actions là hệ thống chạy một lần rồi tắt, không phải server:

- Job bắt đầu mỗi 6 giờ (`schedule: cron`), chạy tối đa ~5,5 giờ
- Trong đó dùng **long-polling** Telegram, không tốn CPU
- **Tự thoát sớm sau 30 phút không có tin nhắn** → không đốt runner khi không dùng
- Lưu `offset` vào cache để restart không xử lý lại tin nhắn cũ
- `concurrency` + `cancel-in-progress: false` để không huỷ build đang dở

**Độ trễ thực tế:** gửi zip → nhận `.bin` trong **3–7 phút** (phần lớn thời gian là tải file + build).

### Thiết lập secrets

```bash
gh secret set TELEGRAM_BOT_TOKEN --repo <owner>/<repo>
gh secret set TELEGRAM_CHAT_ID   --repo <owner>/<repo>
```

- Token: [@BotFather](https://t.me/BotFather) → `/newbot`
- Chat ID: nhắn bot bất kỳ rồi mở `https://api.telegram.org/bot<TOKEN>/getUpdates`, tìm `chat.id`

Bot chỉ trả lời trong chat có `CHAT_ID` đã khai báo — ai khác gửi file sẽ bị bỏ qua.

### Chạy thử

```bash
python3 bot/test_bot.py     # 17 test, dùng mock Telegram server, không cần token thật
```

Test bao phủ: dò thư mục gốc, preflight, xoá rác trong zip, escape HTML, lưu offset, và vòng lặp end-to-end.

---

## 📦 Cách 2: `uploads/`

```bash
cp ten.zip uploads/
git add -A && git commit -m "ten" && git push
```

Push lên `main` mà đụng `uploads/*.zip` là workflow `build-zip.yml` tự chạy.
Artifact tải ở tab Actions.

---

## Cache 3 tầng

| Tầng | Cached gì | Key | Hiệu quả |
|---|---|---|---|
| 1 | `/opt/esp/idf`, `~/.espressif` (~2.4 GB) | theo version, **dùng chung mọi target** | Bỏ clone + `install.sh` (~6 phút → 0) |
| 2 | `~/.cache/ccache` (300 MB) | `<prefix>-<run_id>`, restore theo prefix | **100% hit** ở cả `esp32`/`esp32s3` |
| 3 | `build/` (109 MB) | theo target + hash source | Chỉ compile file thay đổi |

Vài quyết định thiết kế đáng lưu ý:

- **Tầng 1 dùng chung key cho mọi target.** `install.sh esp32,esp32s3` chỉ cài *một* bộ toolchain. Key theo target sẽ lưu 2 bản giống hệt ≈ 8.2 GB, gần kín quota 10 GB và tầng giá trị nhất sẽ bị evict trước.
- **Tầng 2 phải "rolling".** `actions/cache` chỉ ghi khi key chưa tồn tại; 2 job song song tranh một key sẽ tạo entry trùng và lookup hỏng (đã gặp: 4 entry cùng key `ccache-esp32-Linux`, mọi run đều 0 hit). Cách đúng: `cache/restore` lấy bản mới nhất + `cache/save` với key chứa `github.run_id`.
- **Cache xoá sau 7 ngày không dùng.** Toolchain là tầng đắt nhất nên đừng bỏ repo im quá 1 tuần.

## Số đo thật

| Run | Thời gian |
|---|---|
| Lần đầu (tải toolchain 2.4 GB) | 4m19s |
| Các run sau (cache ấm) | ~3m |

ccache sau khi sửa: `esp32` 803/2408 hit, `esp32s3` 1684/2526 hit — tức **100% file được compile đều lấy từ cache**.

Đo local: build lạnh 45s → build ấm 12.4s → incremental 5.3s.

## Cấu trúc

```
CMakeLists.txt          # project + bật ccache launcher (phải đặt TRƯỚC project())
main/                   # code mẫu
bot/bot.py              # Telegram bot
bot/test_bot.py         # test với mock server
.github/workflows/
  build.yml             # build dự án trong repo
  build-zip.yml         # build từ uploads/*.zip
  telegram-bot.yml      # bot
uploads/                # nơi đặt .zip
sdkconfig.defaults
partitions.csv
```