# u3v-webui Developer Guide

**Version:** 6.10.0  
**Project:** Industrial Camera LAN WebUI — multi-camera, extensible

---

## Table of Contents

1. [Installation](#1-installation)
2. [Security Architecture](#2-security-architecture)
3. [Application Framework](#3-application-framework)
4. [Hardware Abstraction Layer (HAL)](#4-hardware-abstraction-layer-hal)
5. [Writing a Custom Driver](#5-writing-a-custom-driver)
6. [Pipeline Architecture](#6-pipeline-architecture)
7. [Writing a Plugin](#7-writing-a-plugin)

---

## 1. Installation

### System Requirements

| Component | Minimum |
|-----------|---------|
| Python | 3.9+ |
| OS | Linux (recommended), macOS, Windows |
| OpenSSL | Any version (for self-signed TLS cert) |

### 1. System Packages

```bash
sudo apt-get install -y \
    python3-opencv \
    python3-numpy \
    gir1.2-aravis-0.8 aravis-tools-cli \
    python3-gi python3-gi-cairo
```

### 2. Virtual Environment

Create with `--system-site-packages` so the system packages installed above are accessible:

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install flask flask-socketio
```

### 3. Option-Drives USB3 Vision camera  

Camera Access Permissions (one-time setup) 
This project is developed and tested with **Hikvision USB3 Vision cameras**

```bash
sudo bash -c 'cat > /etc/udev/rules.d/99-hikvision.rules << EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="2bdf", MODE="0666", GROUP="plugdev"
EOF'

sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev $USER
```

> Group membership takes effect after re-logging in.

### Running the Server

```bash
python run.py
```

The server starts on `https://0.0.0.0:45221` by default.  
A self-signed TLS certificate is auto-generated to `temp/cert.pem` on first run.

### Default Credentials

| Username | Password | Role |
|----------|----------|------|
| `admin`  | `1234`   | Full camera control |
| `viewer` | `view1`  | View-only streaming |

> Edit `u3v_webui/config.py` → `WEB_USERS` to change credentials.

### Configuration Reference

All tunable constants live in `u3v_webui/config.py`:

```python
WEB_PORT           = 45221        # Listening port
STREAM_JPEG_Q      = 60           # Streaming JPEG quality (0-100)
STREAM_FPS         = 30           # Max push rate per viewer (fps)
FAIL_MAX_ATTEMPTS  = 3            # IP block threshold (cumulative fails)
ADAPTIVE_STREAM    = True         # Per-viewer resolution scaling
CAM_JOIN_TIMEOUT   = 2            # Driver thread shutdown timeout (s)
```

---

## 2. Security Architecture

### 2.1 Transport Security (HTTPS / TLS)

All traffic is encrypted via TLS. On startup, `app.main()` calls `_ensure_ssl_cert(local_ip)`, which generates a self-signed RSA-2048 certificate with a Subject Alternative Name (SAN) matching the server's LAN IP address:

```
openssl req -x509 -newkey rsa:2048
            -keyout temp/key.pem
            -out    temp/cert.pem
            -days 3650
            -subj "/CN=<local_ip>"
            -addext "subjectAltName=IP:<local_ip>"
```

If `openssl` is unavailable, the server falls back to plain HTTP (logged as a warning).

### 2.2 Session-Based Authentication

Login flow:

```
Browser                          Server
  |                                |
  |  GET /login                    |
  |<-------------------------------|  Set session["csrf_token"] (32-byte random)
  |                                |
  |  POST /login {user, pw, csrf}  |
  |------------------------------>|
  |                                | 1. hmac.compare_digest(form_csrf, session_csrf)
  |                                | 2. Validate username + password
  |                                | 3. 1s artificial delay (brute-force resistance)
  |                                | 4. SecurityManager.record_success/fail(ip)
  |                                | 5. If OK → create_token() → set session["token"]
  |<------------------------------|  Redirect to / or /viewer
```

CSRF tokens use `hmac.compare_digest` for timing-safe comparison.

### 2.3 Token-Gated WebSocket

After login, the browser receives a token stored in the session cookie. Every SocketIO connection must present this token:

```
wss://host:port/socket.io/?token=<hex_token>
```

The `on_connect` handler validates the token against `AppState._token_is_admin`. Invalid tokens are rejected immediately. The `is_admin` flag embedded in the token controls which SocketIO events a client may emit.

### 2.4 IP Rate Limiting and Blacklist

Implemented in `u3v_webui/security.py` → `SecurityManager`:

```
Each failed login:
  _fail_counts[ip] += 1
  if _fail_counts[ip] >= FAIL_MAX_ATTEMPTS:
      blacklist ip → save to security_blacklist.json
      queue admin notification
```

- Counts are **cumulative** (survive correct logins)
- Blacklisted IPs receive HTTP 403 on every subsequent request
- Admins can clear the blacklist via the security panel in the UI

**Persistent state files:**

| File | Contents |
|------|----------|
| `security_blacklist.json` | Blacklisted IPs, fail counts, admin notifications |
| `security_log.json` | Audit log: login success/fail events with timestamp and IP |

### 2.5 Admin Privilege Model

All camera-control SocketIO events (open, close, scan, add_plugin, etc.) call `_is_admin_sid(sid)` before executing. Viewer connections can receive frames and state updates but cannot issue commands.

```
Admin session  → full camera control, plugin management, security panel
Viewer session → stream view, pause/resume own stream only
```

### 2.6 Server Header Stripping

```python
@app.after_request
def strip_server_header(response):
    response.headers.pop("Server", None)
    return response
```

This removes the Flask/Werkzeug server banner from all HTTP responses.

---

## 3. Application Framework

### 3.1 Component Overview

```
run.py
  └─ u3v_webui/app.py :: main()
        ├─ PluginManager.scan()           # discover plugins
        ├─ PluginManager.register_routes() # wire Flask + SocketIO handlers
        ├─ _ensure_ssl_cert()             # TLS setup
        ├─ AppState.scan_cameras()        # initial device scan
        └─ SocketIO.run()                 # blocking server loop
```

**Core singletons (module-level in `app.py`):**

| Object | Type | Purpose |
|--------|------|---------|
| `app` | `Flask` | HTTP routes, session, static files |
| `sio` | `SocketIO` | WebSocket events (threading async mode) |
| `state` | `AppState` | Camera drivers, frames, tokens, viewer sessions |
| `security` | `SecurityManager` | Auth, blacklist, audit log |
| `stream_mgr` | `StreamManager` | Per-camera JPEG push threads |
| `manager` | `PluginManager` | Plugin registry, pipeline routing |

### 3.2 HTTP Routes

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/login` | None | Login form (CSRF token issued) |
| `POST` | `/login` | None | Credential validation |
| `GET` | `/logout` | Session | Revoke token, clear session |
| `GET` | `/` | Admin | Admin interface |
| `GET` | `/viewer` | Session | Viewer-only interface |

### 3.3 SocketIO Events

**Camera management (admin only):**

| Event | Description |
|-------|-------------|
| `scan_cameras` | Re-scan available devices |
| `open_camera` | Open a specific camera |
| `close_camera` | Close a specific camera |
| `close_all_cameras` | Close all cameras |
| `apply_native_mode` | Reopen camera at specific resolution/fps |

**Per-viewer state:**

| Event | Description |
|-------|-------------|
| `select_camera` | Switch which camera this viewer's sidebar tracks |
| `set_stream_size` | Report viewer's display size (adaptive scaling) |
| `set_stream_paused` | Pause/resume frame delivery for this viewer |

**Plugin control (admin only):**

| Event | Description |
|-------|-------------|
| `add_plugin` | Instantiate a plugin on a camera |
| `remove_plugin` | Remove a plugin from a camera |
| `plugin_action` | Dispatch named action to plugin pipeline |
| `set_param` | Set a camera/plugin parameter |

**Security (admin only):**

| Event | Description |
|-------|-------------|
| `confirm_notification` | Acknowledge a single security alert |
| `confirm_all_notifications` | Acknowledge all alerts |
| `get_security_records` | Fetch blacklist + audit log |
| `clear_blacklist` | Reset the IP blacklist |

### 3.4 Multi-User Isolation

Each SocketIO session (`sid`) carries independent state:

```python
state._sid_selected_cam[sid]           # which camera this viewer tracks
state._stream_sizes[(cam_id, sid)]     # viewer's display resolution
state._stream_paused[(cam_id, sid)]    # per-viewer stream pause flag
```

`emit_state(sid)` sends a personalized snapshot to each client, so two viewers can simultaneously track different cameras.

### 3.5 Auto-Open / Auto-Close

- Cameras **auto-close** when the last viewer disconnects AND no plugin reports `is_busy()`.
- If an admin had previously opened a camera (`_manual_closed=False`), it is tracked in `_auto_reopen_cams`.
- On the **next viewer connect**, those cameras auto-reopen.
- Cameras start in `_manual_closed=True` — **no auto-open at startup**.

---

## 4. Hardware Abstraction Layer (HAL)

### 4.1 Abstract Interface

All drivers extend `CameraDriver` defined in `u3v_webui/drivers/base.py`:

```python
class CameraDriver(threading.Thread, ABC):

    # --- Discovery ---
    @staticmethod
    def scan_devices() -> list[dict]:
        """Return [{device_id, model, serial, label}, ...]"""

    # --- Lifecycle ---
    def open(self, device_id: str) -> dict:
        """Open hardware. Return info dict."""

    def close(self) -> None:
        """Release hardware."""

    # --- Acquisition (thread entry) ---
    def run(self) -> None:
        """Frame acquisition loop. Call self.on_frame(frame, ts_ns)."""

    def stop(self) -> None:
        """Signal acquisition thread to stop."""

    # --- Parameters ---
    def set_param(self, key: str, value) -> None:
        """Queue a parameter change (thread-safe)."""

    def query_native_modes(self) -> list[dict]:
        """Return [{width, height, fps}, ...] sorted by area then fps."""

    def read_hw_bounds(self) -> dict:
        """Return actual hardware limits (exp_min/max, gain_min/max, fps_min/max)."""

    # --- Live properties ---
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

### 4.2 Driver Info Dict (returned by `open()`)

```python
{
    "model":     "Camera Model Name",
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

### 4.3 Frame Callback

```python
driver.on_frame = callback   # set before driver.start()

# Signature:
def callback(frame: np.ndarray, hw_ts_ns: int) -> None:
    ...
```

`hw_ts_ns` is the hardware timestamp in nanoseconds (or a software timestamp if the hardware does not provide one). The callback runs in the driver thread — keep it fast.

### 4.4 Supported Parameters

Each driver declares which parameters it honors:

```python
SUPPORTED_PARAMS: frozenset = frozenset({
    "exposure",       # µs (int/float)
    "gain",           # dB (float)
    "fps",            # frames/sec (float)
    "exposure_auto",  # bool
    "gain_auto",      # bool
    "exp_auto_upper", # µs upper limit for auto exposure
})

DEFAULT_PARAMS: dict = {
    "exposure": 10_000,
    "gain": 0.0,
    "fps": 30.0,
    ...
}
```

### 4.5 Built-in Drivers

| Driver | File | Transport | Library |
|--------|------|-----------|---------|
| `AravisDriver` | `aravis_driver.py` | USB3 Vision, GigE | Aravis (GObject) |
| `UVCDriver` | `uvc_driver.py` | USB Video Class, V4L2 | OpenCV |
| `RPiDriver` | `rpi_driver.py` | CSI (Raspberry Pi) | libcamera |
| `VirtualDriver` | `virtual_driver.py` | Synthetic (test) | NumPy |

### 4.6 Driver Auto-Discovery

`u3v_webui/drivers/__init__.py` scans `drivers/*.py` at import time, collects all `CameraDriver` subclasses, and populates:

```python
DRIVERS: list       # all driver classes
DRIVER_MAP: dict    # {ClassName: class}
```

`scan_all_devices()` calls `scan_devices()` on every driver and merges results, adding a `"driver"` field to each entry.

---

## 5. Writing a Custom Driver

### 5.1 File Location

Create a new file in `u3v_webui/drivers/`:

```
u3v_webui/drivers/my_driver.py
```

The auto-discovery system will pick it up automatically on next startup.

### 5.2 Minimal Template

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
        self._pending   = {}        # queued param changes
        self._pending_lock = threading.Lock()

        self._latest_frame = None
        self._cap_fps      = 0.0
        self._current_exp  = 10_000.0
        self._current_gain = 0.0
        self._is_running   = False

    # --- Discovery ---
    @staticmethod
    def scan_devices() -> list[dict]:
        devices = []
        # TODO: query hardware for available device IDs
        devices.append({
            "device_id": "my_device_0",
            "model":     "My Camera Model",
            "serial":    "SN000001",
            "label":     "My Camera 0",
        })
        return devices

    # --- Lifecycle ---
    def open(self, device_id: str) -> dict:
        # TODO: open hardware connection, configure defaults
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
        # TODO: release hardware
        self._device = None

    # --- Acquisition thread ---
    def run(self) -> None:
        self._is_running = True
        self._stop_flag.clear()

        while not self._stop_flag.is_set():
            # Apply queued parameter changes
            with self._pending_lock:
                pending = self._pending.copy()
                self._pending.clear()
            for key, value in pending.items():
                self._apply_param(key, value)

            # Capture frame from hardware
            frame = self._capture_one_frame()   # np.ndarray, BGR
            if frame is None:
                continue

            self._latest_frame = frame

            # Notify pipeline
            hw_ts_ns = 0   # replace with real hardware timestamp if available
            if self.on_frame is not None:
                self.on_frame(frame, hw_ts_ns)

        self._is_running = False

    def stop(self) -> None:
        self._stop_flag.set()

    # --- Parameters ---
    def set_param(self, key: str, value) -> None:
        with self._pending_lock:
            self._pending[key] = value

    def _apply_param(self, key: str, value) -> None:
        if key == "exposure":
            self._current_exp = float(value)
            # TODO: push to hardware
        elif key == "gain":
            self._current_gain = float(value)
            # TODO: push to hardware

    # --- Native mode query (optional) ---
    def query_native_modes(self) -> list[dict]:
        return [
            {"width": 1920, "height": 1080, "fps": 30.0},
            {"width": 1280, "height":  720, "fps": 60.0},
        ]

    # --- Live properties ---
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

    # --- Internal helpers ---
    def _capture_one_frame(self) -> np.ndarray | None:
        # TODO: pull one frame from hardware SDK
        # Must return BGR np.ndarray or None on timeout
        raise NotImplementedError
```

### 5.3 FPS Measurement

Use a rolling window to keep `cap_fps` accurate:

```python
import time
from collections import deque

self._ts_buf = deque(maxlen=30)

# Inside run loop, after successful capture:
now = time.monotonic()
self._ts_buf.append(now)
if len(self._ts_buf) >= 2:
    self._cap_fps = (len(self._ts_buf) - 1) / (self._ts_buf[-1] - self._ts_buf[0])
```

### 5.4 Audio Support (Optional)

If your hardware provides audio:

```python
@property
def supports_audio(self) -> bool:
    return True

def audio_start(self) -> None:
    ...  # start audio capture

def audio_stop(self) -> None:
    ...  # stop audio capture
```

---

## 6. Pipeline Architecture

### 6.1 Frame Flow Overview

```
Hardware (driver thread)
    │  frame (np.ndarray BGR) + hw_ts_ns
    ▼
CameraDriver.on_frame callback
    │
    ▼
AppState._on_frame(frame, ts_ns, cam_id)
    │
    ▼
PluginManager.process_frame_for_camera(frame, ts_ns, cam_id)
    │
    ├─ [pipeline mode plugins] → pipeline_frame  (saved to disk, feeds later plugins)
    │
    └─ [display mode plugins]  → display_frame   (streaming only, NOT saved)
    │
    ▼
AppState._pipeline_frames[cam_id] = pipeline_frame   ← photo/record use this
AppState._display_frames[cam_id]  = display_frame    ← streaming uses this
```

### 6.2 Pipeline Mode vs Display Mode

| Mode | Frame source | Result stored | Used by |
|------|-------------|---------------|---------|
| `"pipeline"` | Output of previous plugin | Yes (`_pipeline_frames`) | Recording, photo, downstream plugins |
| `"display"` | Copy of current pipeline frame | No | Streaming to viewers only |

A plugin declared with `"display"` mode can add visual overlays, annotations, or previews without polluting saved files.

### 6.3 Per-Camera Plugin Pipeline

Each camera maintains an ordered list of active plugins:

```python
manager._pipeline[cam_id] = [
    {"name": "basic_params",   "ptype": "local",  "mode": "pipeline"},
    {"name": "basic_record",   "ptype": "local",  "mode": "pipeline"},
    {"name": "overlay_text",   "ptype": "local",  "mode": "display"},
]
```

Execution is strictly sequential within a single frame. Plugins see the output of all preceding pipeline-mode plugins.

**Global vs Local plugins:**

| Type | Instance | Use case |
|------|----------|---------|
| `"global"` | One shared instance across all cameras | Cross-camera analytics |
| `"local"` | One instance per camera | Per-camera parameters, recording |

### 6.4 Example Execution Trace

```python
# Pipeline: [BasicParams(pipeline), BasicRecord(pipeline), OverlayText(display)]

pipeline_frame = raw_frame

# Step 1 — BasicParams (pipeline)
result = basic_params.on_frame(pipeline_frame, hw_ts_ns, "cam_1")
if result is not None:
    pipeline_frame = result          # modified frame propagates

# Step 2 — BasicRecord (pipeline)
result = basic_record.on_frame(pipeline_frame, hw_ts_ns, "cam_1")
if result is not None:
    pipeline_frame = result          # saved to disk

# Step 3 — OverlayText (display)
display_frame = pipeline_frame.copy()   # branch off pipeline
result = overlay_text.on_frame(display_frame, hw_ts_ns, "cam_1")
if result is not None:
    display_frame = result           # only affects viewers

return (pipeline_frame, display_frame)
```

### 6.5 Adaptive Streaming

After pipeline processing, `StreamManager` pushes frames to viewers:

```
display_frame
    │
    ▼
For each viewer SID subscribed to cam_id:
    target = state._stream_sizes.get((cam_id, sid))
    if ADAPTIVE_STREAM and target:
        scaled = cv2.resize(display_frame, target, INTER_AREA)
    else:
        scaled = display_frame

    # JPEG encode cache: one encode per unique (width, height)
    buf = encode_cache.setdefault((w,h), cv2.imencode(".jpg", scaled, [60]))

    sio.emit("frame", {cam_id, img=base64(buf), cap_fps, **meta}, to=sid)
```

Multiple viewers at the same display resolution share one JPEG encode.

### 6.6 Action and Parameter Dispatch

SocketIO events `plugin_action` and `set_param` walk the plugin pipeline in order and stop at the first plugin that claims the request:

```python
# Action dispatch
for plugin in pipeline[cam_id]:
    result = plugin.handle_action(action, data, driver)
    if result is not None:
        return result    # (ok: bool, msg: str)
return (False, "No handler found")

# Parameter dispatch
for plugin in pipeline[cam_id]:
    if plugin.handle_set_param(key, value, driver):
        return True
return False
```

---

## 7. Writing a Plugin

### 7.1 Directory Structure

```
u3v_webui/plugins/my_plugin/
├── plugin.py      # required: metadata exports
├── basic.py       # implementation class
├── defaults.py    # default parameter values (optional)
└── ui.html        # sidebar HTML fragment (optional)
```

### 7.2 `plugin.py` — Metadata

```python
from .basic import MyPlugin

PLUGIN_CLASS       = MyPlugin
PLUGIN_NAME        = "my_plugin"         # unique identifier, used in API calls
PLUGIN_TYPE        = "local"             # "local" or "global"
PLUGIN_VERSION     = "1.0.0"
PLUGIN_DESCRIPTION = "Does something useful"
```

### 7.3 Implementation Class

```python
import numpy as np
from ..base import PluginBase


class MyPlugin(PluginBase):

    # --- Identity ---
    @property
    def name(self) -> str:
        return "my_plugin"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def plugin_type(self) -> str:
        return "local"   # or "global"

    # --- Lifecycle ---
    def on_load(self) -> None:
        self._active = False
        self._value  = 0

    def on_unload(self) -> None:
        self._active = False

    def on_camera_open(self, cam_info: dict, cam_id: str, driver) -> None:
        # cam_info contains model, serial, width, height, exp_min/max, etc.
        self._width  = cam_info.get("width",  1920)
        self._height = cam_info.get("height", 1080)

    def on_camera_close(self, cam_id: str) -> None:
        self._active = False

    # --- Frame processing ---
    def on_frame(self, frame: np.ndarray, hw_ts_ns: int, cam_id: str = "") -> np.ndarray | None:
        """
        Return a modified frame (same shape and dtype) to replace the input.
        Return None to pass the frame unchanged to the next plugin.
        Keep this fast — runs in the acquisition thread.
        """
        if not self._active:
            return None

        result = frame.copy()
        # ... process result ...
        return result

    # --- State (sent to UI on every state update) ---
    def get_state(self, cam_id: str = "") -> dict:
        return {
            "my_plugin_active": self._active,
            "my_plugin_value":  self._value,
        }

    # --- Frame metadata (merged into every "frame" SocketIO event) ---
    def frame_payload(self, cam_id: str = "") -> dict:
        return {}   # return {} if no per-frame metadata needed

    # --- Action handler ---
    def handle_action(self, action: str, data: dict, driver) -> tuple | None:
        """
        Return (ok: bool, msg: str) to claim the action.
        Return None to pass to the next plugin.
        """
        if action == "toggle_my_plugin":
            self._active = not self._active
            return (True, "toggled")
        return None

    # --- Parameter handler ---
    def handle_set_param(self, key: str, value, driver) -> bool:
        """
        Return True to claim the parameter (stops propagation).
        Return False to pass to the next plugin.
        """
        if key == "my_value":
            self._value = int(value)
            return True
        return False

    # --- Busy guard ---
    def is_busy(self, cam_id: str = "") -> bool:
        """
        Return True to prevent auto-close while this plugin is working.
        Use this when the plugin is saving data in a background thread.
        """
        return False
```

### 7.4 Pipeline Mode Declaration

Pipeline mode is declared in `plugin.py`, not in the class itself:

```python
# plugin.py
PLUGIN_MODE = "pipeline"   # or "display"
```

The `PluginManager` reads this when building the pipeline entry for the camera. If absent, defaults to `"pipeline"`.

### 7.5 Background Work Pattern

When a plugin needs to do I/O (saving files, network calls) without blocking the frame callback:

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
        # runs in background thread
        for f in frames:
            pass   # save f to disk
        self._saving = False
        if self._notify_idle:
            self._notify_idle(cam_id)   # signal auto-close logic

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

`self._notify_idle` is injected by `PluginManager` after `on_load()`. Calling it signals that the plugin is no longer busy, which unblocks auto-close logic.

### 7.6 Sidebar UI (Optional)

Create `u3v_webui/plugins/my_plugin/ui.html` with an HTML fragment:

```html
<div id="my-plugin-panel">
  <label>My Plugin</label>
  <button onclick="myPluginToggle()">Toggle</button>
  <span id="my-plugin-status">inactive</span>
</div>
```

JavaScript for the panel goes in `ui.js` (served at `/plugin/my_plugin/ui.js`). The UI receives state updates via the SocketIO `state` event — extract your fields from the `data` object:

```javascript
// Called whenever server emits "state"
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

### 7.7 HTTP Routes (Optional)

If your plugin needs dedicated HTTP endpoints (e.g., file downloads):

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

Only add HTTP routes when SocketIO actions are insufficient. Prefer the `plugin_action` / `set_param` dispatch for all control flow.

---

## Appendix: Built-in Plugin Reference

| Plugin | Name | Type | Mode | Key Actions | Key Params |
|--------|------|------|------|-------------|-----------|
| `BasicPhoto` | `basic_photo` | local | pipeline | `take_photo` | `photo_fmt` |
| `BasicRecord` | `basic_record` | local | pipeline | `toggle_record` | `fmt` |
| `BasicBufRecord` | `basic_buf_record` | local | pipeline | `toggle_buf_record` | `buf_fmt` |
| `BasicParams` | `basic_params` | local | pipeline | — | `exposure`, `gain`, `fps`, `exposure_auto`, `gain_auto`, `exp_auto_upper` |
| `OverlayText` | `overlay_text` | local | pipeline | — | `overlay_text` |

---

*This document reflects version 6.10.0 of u3v-webui.*
