# Intel RealSense SR300 / SR305 設定指南

> 驅動：SR300Driver v1.0.0（`drivers/sr300_driver.py`）

---

## 概述

SR300Driver 透過 Linux 內建的 `uvcvideo` 核心驅動直接存取 Intel RealSense SR300 與 SR305 深度相機，**不需要**安裝 Intel RealSense SDK（`pyrealsense2` / `librealsense2`）。

每台實體相機在裝置列表中最多顯示三個虛擬相機：

| Stream | 格式 | 解析度 | 幀率 |
|--------|------|--------|------|
| `[depth]` | Z16 → 彩色化 BGR | 640 × 480 | 最高 30 fps |
| `[infrared]` | Y10 → 灰階 BGR | 640 × 480 | 最高 60 fps |
| `[color]` | YUYV → BGR | 640 × 480（最高 1920 × 1080）| 最高 30 fps |

> 深度與紅外線 stream 共用同一個 V4L2 node（`/dev/videoN`），但使用不同的像素格式。同一台裝置同時開啟兩者，後開的 stream 會覆蓋前者的格式設定。建議每次只開啟其中一個。

---

## 系統需求

| 需求 | 說明 |
|------|------|
| Linux kernel 含 `uvcvideo` | 所有現代發行版內建，不需額外安裝 |
| OpenCV（含 V4L2 後端） | 本應用程式原有依賴 |
| NumPy | 本應用程式原有依賴 |
| 使用者在 `plugdev` 或 `video` 群組 | 存取 `/dev/video*` 節點所需 |

**不需要：** `pyrealsense2`、`librealsense2`、Intel apt 套件庫、DKMS 模組。

---

## 一次性設定

### 1. 安裝 udev 規則（建議）

讓所有使用者無需加入特定群組即可存取 SR300 裝置節點。執行一次即可：

```bash
sudo tee /etc/udev/rules.d/99-realsense.rules << 'EOF'
SUBSYSTEMS=="usb", ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0aa5", MODE:="0666", GROUP:="plugdev"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0b07", MODE:="0666", GROUP:="plugdev"
EOF
sudo udevadm control --reload-rules
```

完成後拔掉相機重新插入。

### 2. （替代方案）將使用者加入 video 群組

若不想安裝自訂 udev 規則，也可以將使用者加入 `plugdev`（或 `video`）群組：

```bash
sudo usermod -aG plugdev $USER
```

需要登出再重新登入才會生效。

### 3. 驗證

```bash
python3 -c "
from u3v_webui.drivers.sr300_driver import SR300Driver
devs = SR300Driver.scan_devices()
print('找到', len(devs), '個 stream：')
for d in devs: print(' ', d['label'])
"
```

預期輸出範例：

```
找到 3 個 stream：
  RealSense SR300 [2-1] [depth]
  RealSense SR300 [2-1] [infrared]
  RealSense SR300 [2-1] [color]
```

---

## 問題排查

**找不到任何 stream（`Found 0 streams`）**

1. 確認 `lsusb` 能看到相機：
   ```bash
   lsusb | grep 8086
   # 預期：Bus 002 Device XXX: ID 8086:0aa5 Intel Corp. RealSense SR300
   ```
2. 確認 `/dev/video*` 節點存在且當前使用者有權限存取：
   ```bash
   ls -la /dev/video*
   ```
   節點需對當前使用者可讀寫（屬於 `plugdev`/`video` 群組，或 `MODE=0666`）。
3. 確認 `uvcvideo` 已載入：
   ```bash
   lsmod | grep uvcvideo
   ```
   若未載入：`sudo modprobe uvcvideo`

---

**深度畫面顯示全綠或純色**

SR300Driver 會根據實際場景深度範圍自動正規化後再上色，即使場景深度均勻（例如正對牆壁）也會顯示完整色彩漸層，不會出現純色畫面。若畫面全黑，表示沒有收到深度資料——請確認 IR 發射器未被遮擋，且物體在相機有效測距範圍內（SR300 約 0.2 m – 1.5 m）。

---

**深度與紅外線同時顯示空白畫面**

兩個 stream 共用同一個 V4L2 node，但使用不同像素格式，同一時間只能有一個處於作用狀態。請先關閉深度 stream 再開啟紅外線，或反之。

---

**`pyrealsense2` 顯示找不到裝置**

`pyrealsense2`（pip 版）v2.50 以後移除了 SR300 的裝置枚舉支援。本應用程式的 SR300Driver 完全不使用 pyrealsense2，此警告可以忽略。

---

## 可調整參數

選取 SR300 stream 後，側邊欄提供以下參數：

| 參數 | 說明 |
|------|------|
| `exposure_auto` | 啟用 / 停用自動曝光 |
| `exposure` | 手動曝光時間（µs）；設定後自動關閉自動曝光 |
| `gain` | 類比增益（0–255）|
| `rs_depth_colorizer` | 深度色彩方案（0–8）：Jet、Bone、Hot、Pink、Ocean、Winter、Autumn、Rainbow、Inferno |

> `rs_depth_colorizer` 僅影響 `[depth]` stream 的顯示，對 `[infrared]` 和 `[color]` 無效。

---

## 注意事項

- SR300 透過核心 UVC 驅動（`uvcvideo`）暴露各 stream，插入 USB 後核心會自動建立 `/dev/videoN` 節點。
- 內部使用的裝置 ID 格式為 `<usb_sysfs_path>:<stream>`（例如 `2-1:depth`）。將相機插到不同 USB 埠時 sysfs 路徑可能改變。
- 深度數值單位與場景一致（相機回傳原始 Z16 值，本驅動不做公制距離校正）。如需毫米級深度，請使用雙目校正流程。
