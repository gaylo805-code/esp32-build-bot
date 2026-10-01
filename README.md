# esp32-fastbuild

Build firmware ESP32 bằng **ESP-IDF v5.1.4** trên GitHub Actions — **miễn phí** (repo public), có **cache nhiều tầng** để các lần build sau gần như tức thì.

## Cache 3 tầng — vì sao build nhanh

| Tầng | Cached gì | Key | Hiệu quả |
|---|---|---|---|
| 1 | `/opt/esp/idf`, `~/.espressif` (~2 GB toolchain + python env) | theo version, **không** theo commit | Bỏ qua bước clone + `install.sh` (5–8 phút → 0 giây) |
| 2 | `build/` (đối tượng .o đã compile) | hash source + `restore-keys` fallback | Chỉ compile file thay đổi |
| 3 | `~/.cache/ccache` (500 MB, nén) | theo source | Compile lại file đã từng build ở runner khác → cache hit |

Ngoài ra:
- `concurrency` + `cancel-in-progress`: commit mới hủy build cũ, không tốn phút.
- `idf.py set-target` chỉ chạy một lần vì `build/` được restore.
- Artifact `.bin` tải về bằng link trong Actions run.

## Số đo thực tế (đo local trên ubuntu, ESP-IDF v5.1.4)

| Tình huống | Thời gian |
|---|---|
| Build lạnh (không ccache) | 45s |
| Build lạnh + ccache đã có đủ object | 12.4s (842/842 hit, 100% direct hit) |
| Rebuild incremental, chỉ 1 file đổi | 5.3s |
| Clone + `install.sh` (tầng 1 miss) | ~6 phút |

Tức là từ ~6 phút (run đầu) xuống **~12 giây** cho các run sau khi cache ấm.

## Dùng

```bash
git clone https://github.com/<owner>/esp32-fastbuild
cd esp32-fastbuild
# sửa code trong main/
git push        # build tự chạy
```

Firmware nằm ở artifact `firmware-esp32` / `firmware-esp32s3`.

Flash: `idf.py -p /dev/ttyUSB0 flash monitor` (ESP-IDF cài local), hoặc dùng `espflash`/`esptool.py` với file `.bin` từ artifact.

## Local build

```bash
git clone -b v5.1.4 --depth 1 --recursive https://github.com/espressif/esp-idf.git ~/esp-idf
~/esp-idf/install.sh esp32,esp32s3 esp32s3
. ~/esp-idf/export.sh
idf.py set-target esp32
idf.py build
```

## Thêm component / thư viện

`idf.py add-dependency` cập nhật `dependencies.lock` và `main/idf_component.yml` — commit cả hai file để CI cài đúng version. Bước `idf.py build` tự pull component từ Component Registry và kết quả được cache ở tầng 1.

## Tối ưu thêm nếu build vẫn chậm

- **Dùng `restore-keys` rộng hơn** cho tầng 2 nếu đổi nhiều file (build/ cũ vẫn dùng được phần lớn .o).
- **Tăng `CCACHE_MAXSIZE`** lên `2G` nếu repo có nhiều component.
- **Chia job theo target** thay vì matrix khi repo lớn, để cache không bị ghi đè chéo.