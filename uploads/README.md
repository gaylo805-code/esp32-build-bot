# Thư mục này dùng để đưa file `.zip` dự án ESP-IDF vào để build.

## Cách dùng

1. Nén thư mục dự án ESP-IDF (thư mục chứa `CMakeLists.txt` ở gốc) thành `ten_du_an.zip`
2. Copy file zip vào đây
3. `git add uploads/ten_du_an.zip && git commit -m "..." && git push`

Actions sẽ tự chạy (`build-zip.yml`), build cho cả `esp32` và `esp32s3`, rồi **gộp 4 file thành 1 file `.bin` duy nhất** nằm trong Artifacts.

## Artifact đầu ra

| File | Dung lượng |
|---|---|
| `ten_du_an-esp32-merged.bin` | 1 file, flash ở offset `0x0` |
| `ten_du_an-esp32s3-merged.bin` | 1 file, flash ở offset `0x0` |

File đã gộp sẵn: bootloader + partition table + OTA data + app — chỉ cần flash một lần:

```bash
esptool.py -p /dev/ttyUSB0 write_flash 0x0 ten_du_an-esp32-merged.bin
```

## Cấu trúc zip được chấp nhận

```text
ten_du_an.zip
└── ten_du_an/           ← thư mục gốc (đặt tên tùy ý)
    ├── CMakeLists.txt
    ├── main/
    │   ├── CMakeLists.txt
    │   └── *.c
    ├── partitions.csv          (nếu dùng custom partition)
    └── sdkconfig.defaults      (tuỳ chọn)
```

Zip có thể có hoặc không có thư mục gốc bên trong — workflow tự dò.

## Lưu ý

- Mỗi lần push zip mới là một lần build mới (~1–6 phút, tuỳ cache đã ấm hay chưa).
- File zip được commit vào git nên **được GitHub lưu vĩnh viễn trong lịch sử** — repo sẽ phình theo dung lượng zip. Xóa file zip ở commit mới không xóa được bản cũ trong history.
- Nếu build lỗi, artifact `build-logs-<target>` chứa log compile để debug.