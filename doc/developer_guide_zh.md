# u3v-webui 開發者指南

**版本：** 6.13.3  
**專案：** 工業相機區域網路 WebUI — 多相機、可擴充架構

---

## 目錄

1. [安裝](#1-安裝)
2. [網路安全機制](#2-網路安全機制)
3. [主程式框架](#3-主程式框架)
4. [硬體抽象層（HAL）](#4-硬體抽象層hal)
5. [驅動編寫](#5-驅動編寫)
6. [Pipeline 架構](#6-pipeline-架構)
7. [插件編寫](#7-插件編寫)

---

## 1. 安裝

### 系統需求

| 元件 | 最低要求 |
|------|---------|
| Python | 3.9+ |
| 作業系統 | Linux（建議）、macOS、Windows |
| OpenSSL | 任意版本（用於產生自簽 TLS 憑證） |

### 1. 系統套件

```bash
sudo apt-get install -y \
    python3-opencv \
    python3-numpy \
    gir1.2-aravis-0.8 aravis-tools-cli \
    python3-gi python3-gi-cairo
```

### 2. 虛擬環境

使用 `--system-site-packages` 建立虛擬環境，讓上述系統套件可在環境中存取：

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install flask flask-socketio
```

### 3. 選用 — USB3 Vision 相機

相機存取權限設定（一次性）。  
本專案以 **Hikvision USB3 Vision 相機** 開發與測試。

```bash
sudo bash -c 'cat > /etc/udev/rules.d/99-hikvision.rules << EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="2bdf", MODE="0666", GROUP="plugdev"
EOF'

sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev $USER
```

> 群組成員資格在重新登入後生效。

### 啟動伺服器

```bash
python run.py
```

伺服器預設啟動於 `https://0.0.0.0:45221`。  
首次啟動時會自動產生自簽 TLS 憑證至 `temp/cert.pem`。

### 預設帳號

| 帳號 | 密碼 | 角色 |
|------|------|------|
| `admin`  | `1234`   | 完整相機控制 |
| `viewer` | `view1`  | 僅限觀看串流 |

> 修改帳號請編輯 `u3v_webui/config.py` 中的 `WEB_USERS`。

### 設定參數參考

所有可調整的常數都在 `u3v_webui/config.py`：

```python
WEB_PORT           = 45221        # 監聽埠號
STREAM_JPEG_Q      = 60           # 串流 JPEG 品質（0-100）
STREAM_FPS         = 30           # 每位觀看者最大推送幀率
FAIL_MAX_ATTEMPTS  = 3            # IP 封鎖門檻（累計失敗次數）
ADAPTIVE_STREAM    = True         # 依觀看者視窗自動縮放
CAM_JOIN_TIMEOUT   = 2            # 驅動執行緒關閉逾時（秒）
```

---

## 2. 網路安全機制

| 機制 | 說明 |
|------|------|
| TLS（HTTPS） | 首次啟動自動產生 RSA-2048 自簽憑證；找不到 `openssl` 時退回 HTTP |
| Session 身份驗證 | 含 CSRF 防護的登入表單；每個 IP 1 秒延遲與累計失敗計數 |
| Token 閘控 WebSocket | 每次 SocketIO 連線必須附帶 session token；token 內嵌 `is_admin` 旗標 |
| IP 黑名單 | 累計失敗次數達 `FAIL_MAX_ATTEMPTS` 後封鎖該 IP；重啟後持久（`security_blacklist.json`） |
| 管理員權限 | 所有相機控制與插件管理事件均需管理員 token；觀看者只能接收幀串流 |
| 伺服器標頭隱藏 | 所有 HTTP 回應移除 Flask/Werkzeug 版本資訊 |

設定：`FAIL_MAX_ATTEMPTS` 位於 `u3v_webui/config.py`；完整實作見 `u3v_webui/security.py`（`SecurityManager`）。

---

## 3. 主程式框架

### 3.1 元件總覽

```
run.py
  └─ u3v_webui/app.py :: main()
        ├─ PluginManager.scan()            # 掃描並載入插件
        ├─ PluginManager.register_routes() # 掛載 Flask + SocketIO 路由
        ├─ _ensure_ssl_cert()              # TLS 憑證初始化
        ├─ AppState.scan_cameras()         # 初始設備掃描
        └─ SocketIO.run()                  # 阻塞式伺服器主迴圈
```

**核心單例物件（定義於 `app.py` 模組層級）：**

| 物件 | 型別 | 用途 |
|------|------|------|
| `app` | `Flask` | HTTP 路由、session、靜態檔案 |
| `sio` | `SocketIO` | WebSocket 事件（threading 非同步模式） |
| `state` | `AppState` | 相機驅動、幀緩衝、token、觀看者 session |
| `security` | `SecurityManager` | 身份驗證、黑名單、稽核日誌 |
| `stream_mgr` | `StreamManager` | 每台相機的 JPEG 推送執行緒 |
| `manager` | `PluginManager` | 插件登錄表、pipeline 路由 |

### 3.2 HTTP 路由

| 方法 | 路徑 | 驗證 | 說明 |
|------|------|------|------|
| `GET` | `/login` | 無 | 登入頁面（核發 CSRF token） |
| `POST` | `/login` | 無 | 驗證帳號密碼 |
| `GET` | `/logout` | Session | 撤銷 token、清除 session |
| `GET` | `/` | Admin | 管理員介面 |
| `GET` | `/viewer` | Session | 觀看者介面 |

### 3.3 SocketIO 事件

**相機管理（僅限管理員）：**

| 事件 | 說明 |
|------|------|
| `scan_cameras` | 重新掃描可用設備 |
| `open_camera` | 開啟指定相機 |
| `close_camera` | 關閉指定相機 |
| `close_all_cameras` | 關閉所有相機 |
| `apply_native_mode` | 以指定解析度／幀率重開相機 |

**個人觀看狀態：**

| 事件 | 說明 |
|------|------|
| `select_camera` | 切換本觀看者側邊欄追蹤的相機 |
| `set_stream_size` | 回報觀看者的顯示尺寸（自適應縮放用） |
| `set_stream_paused` | 暫停／繼續本觀看者的幀推送 |

**插件控制（僅限管理員）：**

| 事件 | 說明 |
|------|------|
| `add_plugin` | 在指定相機上實例化插件 |
| `remove_plugin` | 從相機移除插件 |
| `plugin_action` | 向插件 pipeline 派送具名動作 |
| `set_param` | 設定相機／插件參數 |

**安全管理（僅限管理員）：**

| 事件 | 說明 |
|------|------|
| `confirm_notification` | 確認單一安全警示 |
| `confirm_all_notifications` | 確認所有警示 |
| `get_security_records` | 取得黑名單與稽核日誌 |
| `clear_blacklist` | 重置 IP 黑名單 |

### 3.4 多使用者隔離

每個 SocketIO session（`sid`）維護獨立狀態：

```python
state._sid_selected_cam[sid]           # 本觀看者追蹤的相機
state._stream_sizes[(cam_id, sid)]     # 本觀看者的顯示解析度
state._stream_paused[(cam_id, sid)]    # 本觀看者的串流暫停旗標
```

`emit_state(sid)` 對每位用戶端發送個人化狀態快照，兩位觀看者可同時追蹤不同相機。

### 3.5 自動開啟／關閉機制

| 事件 | 動作 |
|------|------|
| 管理員開啟相機 | 加入 `_auto_reopen_cams` |
| 管理員手動關閉相機 | 從 `_auto_reopen_cams` 移除 |
| 最後一位觀看者離線（無忙碌插件） | 相機自動關閉；`_auto_reopen_cams` 記錄**保留** |
| 下一位觀看者連線 | `_auto_reopen_cams` 中未開啟的相機**自動重開** |
| 伺服器啟動 | `_auto_reopen_cams` 為空，**不自動開啟任何相機** |

**來源相機保護（`held_cam_ids`）：**  
跨相機合成插件（如 `MultiView`）覆寫 `held_cam_ids() -> set`，宣告它所依賴的相機 ID。`try_auto_close_all()` 採用兩階段演算法：

1. 找出所有「自身有忙碌插件」的相機（`busy_cams`）。
2. 僅從這些忙碌相機出發，收集其插件的 `held_cam_ids()`，形成保護集合。

來源相機只在**消費者相機本身也是忙碌的**條件下才受保護。

| 虛擬相機 A 的插件狀態 | 來源相機 B 狀態 | 無人登入時結果 |
|----------------------|---------------|--------------|
| 只掛 MultiView | 無忙碌插件 | **A 和 B 都關閉** |
| MultiView + MotionDetect（已啟用） | 無忙碌插件 | **A 和 B 都保持開啟** |
| MultiView + MotionDetect（未啟用） | 無忙碌插件 | **A 和 B 都關閉** |
| MultiView + 錄影（進行中） | 無忙碌插件 | **A 和 B 都保持開啟** |
| 只掛 MultiView | B 自身有錄影進行中 | A 關閉，**B 保持開啟**（B 本身忙碌） |

---

## 4. 硬體抽象層（HAL）

### 4.1 抽象介面

所有驅動繼承自 `u3v_webui/drivers/base.py` 的 `CameraDriver`：

```python
class CameraDriver(threading.Thread, ABC):

    # --- 設備探索 ---
    @staticmethod
    def scan_devices() -> list[dict]:
        """回傳 [{device_id, model, serial, label}, ...]"""

    # --- 生命週期 ---
    def open(self, device_id: str) -> dict:
        """開啟硬體，回傳設備資訊 dict。"""

    def close(self) -> None:
        """釋放硬體資源。"""

    # --- 採集執行緒進入點 ---
    def run(self) -> None:
        """幀採集主迴圈，每幀呼叫 self.on_frame(frame, ts_ns)。"""

    def stop(self) -> None:
        """通知採集執行緒停止。"""

    # --- 參數控制 ---
    def set_param(self, key: str, value) -> None:
        """佇列一筆參數異動（執行緒安全）。"""

    def query_native_modes(self) -> list[dict]:
        """回傳 [{width, height, fps}, ...]，依解析度再依幀率排序。"""

    def read_hw_bounds(self) -> dict:
        """查詢硬體實際限制（exp_min/max、gain_min/max、fps_min/max）。"""

    # --- 即時屬性 ---
    @property
    def latest_frame(self) -> np.ndarray | None: ...
    @property
    def cap_fps(self) -> float: ...
    @property
    def current_gain(self) -> float: ...
    @property
    def current_exposure(self) -> float: ...
    @property
    def is_running(self) -> bool: ...
```

### 4.2 open() 回傳格式

```python
{
    "model":     "相機型號",
    "serial":    "SN001234",
    "width":     1920,
    "height":    1080,
    "exp_min":   100,        # µs
    "exp_max":   200_000,    # µs
    "gain_min":  0.0,        # dB
    "gain_max":  24.0,       # dB
    "fps_min":   1.0,
    "fps_max":   60.0,
}
```

### 4.3 幀回調

```python
driver.on_frame = callback   # 在 driver.start() 前設定

# 函式簽名：
def callback(frame: np.ndarray, hw_ts_ns: int) -> None:
    ...
```

`hw_ts_ns` 為硬體時間戳記（奈秒），若硬體不支援則使用軟體時間戳記。  
回調在驅動執行緒中執行，**務必保持快速**。

### 4.4 支援的參數

每個驅動宣告自己支援哪些參數：

```python
SUPPORTED_PARAMS: frozenset = frozenset({
    "exposure",       # µs（int/float）
    "gain",           # dB（float）
    "fps",            # 幀/秒（float）
    "exposure_auto",  # bool
    "gain_auto",      # bool
    "exp_auto_upper", # 自動曝光上限（µs）
})

DEFAULT_PARAMS: dict = {
    "exposure": 10_000,
    "gain": 0.0,
    "fps": 30.0,
    ...
}
```

### 4.5 內建驅動

| 驅動 | 檔案 | 傳輸介面 | 相依函式庫 |
|------|------|---------|-----------|
| `AravisDriver` | `aravis_driver.py` | USB3 Vision、GigE | Aravis（GObject） |
| `UVCDriver` | `uvc_driver.py` | USB Video Class、V4L2 | OpenCV |
| `RPiDriver` | `rpi_driver.py` | CSI（Raspberry Pi，影片模式） | libcamera |
| `RPiImgDriver` | `rpi_img_driver.py` | CSI（Raspberry Pi，靜態拍照模式） | libcamera |
| `VirtualDriver` | `virtual_driver.py` | 合成幀（測試用） | NumPy |

> **靜態拍照模式說明：** `RPiImgDriver` 完全以 `time.sleep()` 控制幀率，不使用感測器定時暫存器。其 `query_native_modes()` 回傳的每筆記錄只含 `{"width": w, "height": h}`，**不含** `"fps"` 欄位；UI 在此類模式下會隱藏 fps 欄。

### 4.6 驅動自動探索

`u3v_webui/drivers/__init__.py` 在匯入時掃描 `drivers/*.py`，收集所有 `CameraDriver` 子類別，並填入：

```python
DRIVERS: list       # 所有驅動類別
DRIVER_MAP: dict    # {ClassName: class}
```

`scan_all_devices()` 呼叫每個驅動的 `scan_devices()` 並合併結果，在每筆記錄中新增 `"driver"` 欄位。

---

## 5. 驅動編寫

### 5.1 檔案位置

在 `u3v_webui/drivers/` 建立新檔案：

```
u3v_webui/drivers/my_driver.py
```

自動探索機制會在下次啟動時自動載入。

### 5.2 最小可用範本

```python
import threading
import numpy as np
from .base import CameraDriver


class MyDriver(CameraDriver):

    SUPPORTED_PARAMS = frozenset({"exposure", "gain"})
    DEFAULT_PARAMS   = {"exposure": 10_000, "gain": 0.0}

    def __init__(self):
        super().__init__()
        self._device    = None
        self._stop_flag = threading.Event()
        self._pending   = {}
        self._pending_lock = threading.Lock()

        self._latest_frame = None
        self._cap_fps      = 0.0
        self._current_exp  = 10_000.0
        self._current_gain = 0.0
        self._is_running   = False

    # --- 設備探索 ---
    @staticmethod
    def scan_devices() -> list[dict]:
        # TODO: 查詢硬體取得可用設備列表
        return [{
            "device_id": "my_device_0",
            "model":     "My Camera Model",
            "serial":    "SN000001",
            "label":     "My Camera 0",
        }]

    # --- 生命週期 ---
    def open(self, device_id: str) -> dict:
        # TODO: 建立硬體連線，套用預設參數
        self._device = device_id
        self._current_exp  = self.DEFAULT_PARAMS["exposure"]
        self._current_gain = self.DEFAULT_PARAMS["gain"]
        return {
            "model":    "My Camera Model",
            "serial":   "SN000001",
            "width":    1920,
            "height":   1080,
            "exp_min":  100,
            "exp_max":  200_000,
            "gain_min": 0.0,
            "gain_max": 24.0,
            "fps_min":  1.0,
            "fps_max":  60.0,
        }

    def close(self) -> None:
        # TODO: 釋放硬體資源
        self._device = None

    # --- 採集執行緒 ---
    def run(self) -> None:
        self._is_running = True
        self._stop_flag.clear()

        while not self._stop_flag.is_set():
            # 套用佇列中的參數異動
            with self._pending_lock:
                pending = self._pending.copy()
                self._pending.clear()
            for key, value in pending.items():
                self._apply_param(key, value)

            # 從硬體取得一幀
            frame = self._capture_one_frame()   # np.ndarray，BGR 格式
            if frame is None:
                continue

            self._latest_frame = frame

            hw_ts_ns = 0   # 請替換為真實硬體時間戳記
            if self.on_frame is not None:
                self.on_frame(frame, hw_ts_ns)

        self._is_running = False

    def stop(self) -> None:
        self._stop_flag.set()

    # --- 參數控制 ---
    def set_param(self, key: str, value) -> None:
        with self._pending_lock:
            self._pending[key] = value

    def _apply_param(self, key: str, value) -> None:
        if key == "exposure":
            self._current_exp = float(value)
            # TODO: 寫入硬體
        elif key == "gain":
            self._current_gain = float(value)
            # TODO: 寫入硬體

    # --- 原生模式查詢（選用） ---
    def query_native_modes(self) -> list[dict]:
        return [
            {"width": 1920, "height": 1080, "fps": 30.0},
            {"width": 1280, "height":  720, "fps": 60.0},
        ]

    # --- 即時屬性 ---
    @property
    def latest_frame(self) -> np.ndarray | None:
        return self._latest_frame

    @property
    def cap_fps(self) -> float:
        return self._cap_fps

    @property
    def current_gain(self) -> float:
        return self._current_gain

    @property
    def current_exposure(self) -> float:
        return self._current_exp

    @property
    def is_running(self) -> bool:
        return self._is_running

    # --- 內部輔助 ---
    def _capture_one_frame(self) -> np.ndarray | None:
        # TODO: 透過硬體 SDK 取得一幀
        # 回傳 BGR 格式的 np.ndarray，逾時時回傳 None
        raise NotImplementedError
```

### 5.3 幀率計算

使用滾動時間窗口維持 `cap_fps` 的準確性：

```python
import time
from collections import deque

self._ts_buf = deque(maxlen=30)

# 在 run() 迴圈中，每次成功取幀後：
now = time.monotonic()
self._ts_buf.append(now)
if len(self._ts_buf) >= 2:
    self._cap_fps = (len(self._ts_buf) - 1) / (self._ts_buf[-1] - self._ts_buf[0])
```

### 5.4 音訊支援（選用）

若硬體提供音訊輸出：

```python
@property
def supports_audio(self) -> bool:
    return True

def audio_start(self) -> None:
    ...  # 開始音訊採集

def audio_stop(self) -> None:
    ...  # 停止音訊採集
```

---

## 6. Pipeline 架構

### 6.1 幀流向總覽

```
硬體（驅動執行緒）
    │  frame（np.ndarray BGR）+ hw_ts_ns
    ▼
CameraDriver.on_frame 回調
    │
    ▼
AppState._on_frame(frame, ts_ns, cam_id)
    │
    ▼
PluginManager.process_frame_for_camera(frame, ts_ns, cam_id)
    │
    ├─ [pipeline 模式插件] → pipeline_frame
    │
    └─ [display 模式插件]  → display_frame
    │
    ▼
AppState._pipeline_frames[cam_id] = pipeline_frame
AppState._display_frames[cam_id]  = display_frame
```

### 6.2 Pipeline 模式 vs Display 模式

兩種模式都是**管線分段標籤**，決定插件所屬的處理階段，與插件的功能種類無關。任何插件（錄影、拍照、偵測、疊加效果等）均可設定為任一模式。

| 模式 | 分段 | 執行時機 | 輸出幀 |
|------|------|---------|-------|
| `"pipeline"` | 第一階段，優先執行 | 在所有 display 插件之前 | `_pipeline_frames[cam_id]` |
| `"display"` | 第二階段，後續執行 | 在所有 pipeline 插件完成後 | `_display_frames[cam_id]` |

每個階段內部依列表順序嚴格循序執行。pipeline 與 display 插件可交錯排列於列表中，各插件依模式自動分配至正確的執行階段，不受列表位置影響。display 插件永遠以最終 pipeline 幀為輸入起點。

### 6.3 每台相機的插件 Pipeline

每台相機維護一個有序插件列表：

```python
manager._pipeline[cam_id] = [
    {"name": "BasicParams", "instance_key": "BasicParams", "mode": "pipeline"},
    {"name": "BasicRecord", "instance_key": "BasicRecord", "mode": "pipeline"},
    {"name": "OverlayText", "instance_key": "OverlayText", "mode": "display"},
]
```

同一幀內的執行順序嚴格按列表順序。display 模式插件看到的是所有前序 pipeline 模式插件處理後的結果。

**跨相機畫面存取：** 每個插件都是針對特定相機新增的。需要讀取其他相機畫面的插件可在實作中宣告 `self._state = None`，PluginManager 會注入 AppState 實例，讓插件存取任意相機的畫面：

| 方法 | 回傳內容 |
|------|---------|
| `state.get_latest_frame(cam_id)` | 最終 pipeline 幀 |
| `state.get_display_frame(cam_id)` | 最終 display 幀 |

所有插件均為單一相機所有，跨相機功能透過 state 注入實現。

### 6.4 執行流程範例

```python
# Pipeline：[BasicParams(pipeline), BasicRecord(pipeline), OverlayText(display)]

pipeline_frame = raw_frame

# 步驟 1 — BasicParams（pipeline 模式）
result = basic_params.on_frame(pipeline_frame, hw_ts_ns, "cam_1")
if result is not None:
    pipeline_frame = result          # 修改後的幀往下傳

# 步驟 2 — BasicRecord（pipeline 模式）
result = basic_record.on_frame(pipeline_frame, hw_ts_ns, "cam_1")
if result is not None:
    pipeline_frame = result          # 此幀寫入磁碟

# 步驟 3 — OverlayText（display 模式）
display_frame = pipeline_frame.copy()   # 從 pipeline 分叉出副本
result = overlay_text.on_frame(display_frame, hw_ts_ns, "cam_1")
if result is not None:
    display_frame = result           # 僅影響觀看者畫面

return (pipeline_frame, display_frame)
```

### 6.5 自適應串流

Pipeline 處理完成後，`StreamManager` 將幀推送給各觀看者：

```
display_frame
    │
    ▼
對每位訂閱此相機的觀看者（SID）：
    target = state._stream_sizes.get((cam_id, sid))
    if ADAPTIVE_STREAM and target：
        scaled = cv2.resize(display_frame, target, INTER_AREA)
    else：
        scaled = display_frame

    # JPEG 編碼快取：相同（width, height）只編碼一次
    buf = encode_cache.setdefault((w,h), cv2.imencode(".jpg", scaled, [60]))

    sio.emit("frame", {cam_id, img=base64(buf), cap_fps, **meta}, to=sid)
```

顯示解析度相同的多位觀看者共享同一次 JPEG 編碼，節省 CPU。

### 6.6 動作與參數派送

SocketIO 事件 `plugin_action` 和 `set_param` 依序走訪插件 pipeline，第一個認領請求的插件回傳結果後停止傳播：

```python
# 動作派送
for plugin in pipeline[cam_id]:
    result = plugin.handle_action(action, data, driver)
    if result is not None:
        return result    # (ok: bool, msg: str)
return (False, "找不到處理器")

# 參數派送
for plugin in pipeline[cam_id]:
    if plugin.handle_set_param(key, value, driver):
        return True
return False
```

---

## 7. 插件編寫

### 7.1 目錄結構

```
u3v_webui/plugins/my_plugin/
├── plugin.py      # 必要：元資料匯出
├── basic.py       # 實作類別
├── defaults.py    # 預設參數值（選用）
└── ui.html        # 側邊欄 HTML 片段（選用）
```

### 7.2 plugin.py — 元資料

```python
from .basic import MyPlugin

PLUGIN_CLASS       = MyPlugin
PLUGIN_NAME        = "my_plugin"         # 唯一識別碼，用於 API 呼叫
PLUGIN_VERSION     = "1.0.0"
PLUGIN_DESCRIPTION = "執行某項有用的功能"
```

### 7.3 實作類別

```python
import numpy as np
from ..base import PluginBase


class MyPlugin(PluginBase):

    # --- 身份識別 ---
    @property
    def name(self) -> str:
        return "my_plugin"

    @property
    def version(self) -> str:
        return "1.0.0"

    # --- 生命週期 ---
    def on_load(self) -> None:
        self._active = False
        self._value  = 0

    def on_unload(self) -> None:
        self._active = False

    def on_camera_open(self, cam_info: dict, cam_id: str, driver) -> None:
        # cam_info 包含 model、serial、width、height、exp_min/max 等
        self._width  = cam_info.get("width",  1920)
        self._height = cam_info.get("height", 1080)

    def on_camera_close(self, cam_id: str) -> None:
        self._active = False

    # --- 幀處理 ---
    def on_frame(self, frame: np.ndarray, hw_ts_ns: int, cam_id: str = "") -> np.ndarray | None:
        """
        回傳修改後的幀（形狀與 dtype 必須相同）以取代輸入幀。
        回傳 None 表示不修改，直接傳給下一個插件。
        此函式在採集執行緒中執行，務必保持快速。
        """
        if not self._active:
            return None

        result = frame.copy()
        # ... 處理 result ...
        return result

    # --- 狀態（每次狀態更新時發送給 UI） ---
    def get_state(self, cam_id: str = "") -> dict:
        return {
            "my_plugin_active": self._active,
            "my_plugin_value":  self._value,
        }

    # --- 幀元資料（合併至每次 "frame" SocketIO 事件） ---
    def frame_payload(self, cam_id: str = "") -> dict:
        return {}   # 若無逐幀元資料則回傳空 dict

    # --- 動作處理 ---
    def handle_action(self, action: str, data: dict, driver) -> tuple | None:
        """
        回傳 (ok: bool, msg: str) 表示認領此動作。
        回傳 None 表示傳給下一個插件。
        """
        if action == "toggle_my_plugin":
            self._active = not self._active
            return (True, "toggled")
        return None

    # --- 參數處理 ---
    def handle_set_param(self, key: str, value, driver) -> bool:
        """
        回傳 True 表示認領此參數（停止傳播）。
        回傳 False 表示傳給下一個插件。
        """
        if key == "my_value":
            self._value = int(value)
            return True
        return False

    # --- 忙碌守衛 ---
    def is_busy(self, cam_id: str = "") -> bool:
        """
        回傳 True 可防止相機在插件工作期間被自動關閉。
        適用於有背景執行緒正在儲存資料的情況。
        """
        return False

    # --- 來源相機依賴宣告 ---
    def held_cam_ids(self) -> set:
        """
        回傳此插件需要保持開啟的 cam_id 集合。
        跨相機合成插件（如 MultiView）需覆寫此方法，
        防止來源相機在無人登入時被自動關閉。
        """
        return set()
```

### 7.4 Pipeline 模式宣告

Pipeline 模式在 `plugin.py` 中宣告，而非在類別內：

```python
# plugin.py
PLUGIN_MODE = "pipeline"   # 或 "display"
```

`PluginManager` 建立 pipeline 條目時讀取此值。若未宣告，預設為 `"pipeline"`。

### 7.4a 多實例插件

在 `plugin.py` 設定 `PLUGIN_ALLOW_MULTIPLE = True` 可讓使用者在同一相機多次新增此插件。

每個實例會收到一個注入至 `self._instance_key` 的唯一鍵值。使用它為 state 和 param key 加命名空間，避免實例間衝突：

```python
def _sk(self, field: str) -> str:
    ik = self._instance_key or self.name
    suffix = "" if ik == self.name else f"_{ik}"
    return f"my_plugin_{field}{suffix}"

def get_state(self, cam_id=""):
    return {self._sk("value"): self._value}

def handle_set_param(self, key, value, driver):
    if key == self._sk("value"):
        self._value = value
        return True
    return False
```

UI 腳本透過 `block.dataset.instance` 取得 instance key，並據此組合 param key。

### 7.4b 跨相機來源插件（虛擬相機）

來源插件加入至 **VirtualDriver** 相機，完全忽略傳入的 dummy frame，改從其他真實相機讀取畫面並合成，回傳的幀取代虛擬相機的 pipeline 輸出。

```python
class MyStereoPlugin(PluginBase):

    def __init__(self):
        self._state      = None   # 注入後可存取所有相機幀
        self._emit_state = None
        self._cam_a = ""
        self._cam_b = ""
        self._lock  = threading.Lock()

    @property
    def name(self): return "MyStereo"

    def on_frame(self, frame, hw_ts_ns, cam_id=""):
        with self._lock:
            cam_a, cam_b = self._cam_a, self._cam_b

        if not cam_a or not cam_b or self._state is None:
            return None   # 來源未選擇時直接透傳 dummy frame

        fa = self._state.get_latest_frame(cam_a)
        fb = self._state.get_latest_frame(cam_b)
        if fa is None or fb is None:
            return None

        return _my_compose(fa, fb)   # 回傳合成幀，取代虛擬相機畫面
```

`plugin.py`：

```python
PLUGIN_CLASS = MyStereoPlugin
PLUGIN_NAME  = "MyStereo"
PLUGIN_MODE  = "pipeline"   # 合成幀傳給下游插件
```

使用方式：開啟 VirtualDriver 相機 → 新增 MyStereo → 選擇 cam_a、cam_b。虛擬相機的串流、錄影及後續插件均接收合成後的畫面。

**自我參照防護：** 絕對不要將虛擬相機自身的 `cam_id` 設為來源槽位——這會產生循環依賴並導致 pipeline 停滯。每個槽位取幀前需加入防護：

```python
frames = [
    self._state.get_latest_frame(cams[i])
    if cams[i] and cams[i] != cam_id else None
    for i in range(n)
]
```

**`held_cam_ids()` — 來源相機依賴宣告：**  
覆寫此方法列出插件所讀取的相機。保護以消費者相機是否忙碌為前提——詳見 §3.5 表格。

```python
def held_cam_ids(self) -> set:
    with self._lock:
        return {c for c in self._source_cams if c}
```

| 消費者相機是否忙碌 | `held_cam_ids()` 是否生效 | 來源相機是否自動關閉 |
|-----------------|--------------------------|------------------|
| 是（如 MotionDetect 執行中） | 是 | 否，保持開啟 |
| 否（只掛 MultiView，閒置） | 否 | 是，正常關閉 |

### 7.5 背景工作模式

當插件需要執行 I/O（儲存檔案、網路請求）而不阻塞幀回調時：

```python
import threading

class MyPlugin(PluginBase):

    def on_load(self):
        self._saving = False
        self._queue  = []
        self._lock   = threading.Lock()

    def on_frame(self, frame, hw_ts_ns, cam_id=""):
        if self._active:
            with self._lock:
                self._queue.append(frame.copy())
        return None

    def _do_save(self, frames, cam_id):
        # 在背景執行緒中執行
        for f in frames:
            pass   # 將 f 儲存至磁碟
        self._saving = False
        if self._notify_idle:
            self._notify_idle(cam_id)   # 通知 auto-close 邏輯此插件已閒置

    def handle_action(self, action, data, driver):
        if action == "flush":
            with self._lock:
                batch = self._queue[:]
                self._queue.clear()
            self._saving = True
            t = threading.Thread(target=self._do_save,
                                 args=(batch, data.get("cam_id", "")),
                                 daemon=True)
            t.start()
            return (True, "saving")
        return None

    def is_busy(self, cam_id=""):
        return self._saving
```

`self._notify_idle` 由 `PluginManager` 在 `on_load()` 後注入。呼叫它可通知框架此插件已不再忙碌，解除對 auto-close 的阻塞。

### 7.6 側邊欄 UI（選用）

建立 `u3v_webui/plugins/my_plugin/ui.html`，內容為 HTML 片段：

```html
<div id="my-plugin-panel">
  <label>My Plugin</label>
  <button onclick="myPluginToggle()">切換</button>
  <span id="my-plugin-status">inactive</span>
</div>
```

對應的 JavaScript 放在 `ui.js`（由伺服器掛載於 `/plugin/my_plugin/ui.js`）。UI 透過 SocketIO `state` 事件接收狀態更新：

```javascript
// 每次伺服器 emit "state" 時呼叫
function onState(data) {
    const active = data.my_plugin_active ?? false;
    document.getElementById("my-plugin-status").textContent =
        active ? "active" : "inactive";
}

function myPluginToggle() {
    socket.emit("plugin_action", {
        cam_id: selectedCamId,
        action: "toggle_my_plugin",
        data:   {}
    });
}
```

### 7.7 HTTP 路由（選用）

若插件需要專屬 HTTP 端點（例如檔案下載）：

```python
def register_routes(self, app, sio, ctx):
    state    = ctx["state"]
    is_admin = ctx["is_admin"]   # callable: is_admin(session)

    @app.route("/plugin/my_plugin/download")
    def my_plugin_download():
        from flask import session, send_file, abort
        if not is_admin(session):
            abort(403)
        return send_file("/path/to/file")
```

只有在 SocketIO 動作無法滿足需求時才新增 HTTP 路由。所有控制流程優先透過 `plugin_action` / `set_param` 派送機制實作。

---

## 附錄：內建插件參考

| 插件 | PLUGIN_NAME | 版本 | 類型 | 模式 | 主要動作 | 主要參數 |
|------|------------|------|------|------|---------|---------|
| `BasicPhoto` | `BasicPhoto` | — | local | pipeline | `take_photo` | `photo_fmt` |
| `BasicRecord` | `BasicRecord` | — | local | pipeline | `toggle_record` | `rec_fmt` |
| `BasicBufRecord` | `BasicBufRecord` | — | local | pipeline | `toggle_buf_record` | `buf_fmt` |
| `BasicParams` | `BasicParams` | — | local | pipeline | — | `exposure`、`gain`、`fps`、`exposure_auto`、`gain_auto`、`exp_auto_upper` |
| `OverlayText` | `OverlayText` | — | local | display | — | `overlay_text` |
| `MultiView` | `MultiView` | 1.1.1 | source | pipeline | — | `multiview_layout`、`multiview_cam_1..4`、`multiview_src_1..4`、`multiview_res` |
| `Anaglyph` | `Anaglyph` | — | source | pipeline | — | `anaglyph_left_cam`、`anaglyph_right_cam`、`anaglyph_left_source`、`anaglyph_right_source`、`anaglyph_color_mode`、`anaglyph_left_is_red`、`anaglyph_parallax` |
| `MotionDetect` | `MotionDetect` | 1.0.3 | local | pipeline | `motdet_save_zones` | `motdet_enabled`、`motdet_var_threshold`、`motdet_min_pixel_count`、`motdet_cooldown_sec` |

**來源插件**（`MultiView`、`Anaglyph`）專為 **VirtualDriver** 相機設計，從其他相機合成畫面後回傳，虛擬相機的 dummy 幀將被丟棄。`BasicRecord` 會自動重新分配 ping-pong 緩衝區以符合合成幀尺寸，錄製來源插件輸出無需額外設定。兩個來源插件均實作 `held_cam_ids()` 宣告來源相機依賴。

**MotionDetect** 在背景執行緒以 10 Hz 對縮小版 pipeline 幀進行 MOG2 偵測。在任一配置區域偵測到動作時，儲存全解析度 JPEG 至 `captures/<YYYYMMDD>/motdet_<cam_safe>_<時間戳_微秒>.jpg`。各相機實例的偵測區域（儲存於 `captures/motdet_zones/<cam_safe>_zones.json`）、冷卻計時器與偵測執行緒完全獨立，同時觸發時不會互相干擾。

---

*本文件對應 u3v-webui 版本 6.13.3。*
