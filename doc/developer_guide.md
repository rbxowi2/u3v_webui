# u3v-webui Developer Guide

**Version:** 6.13.3 / Plugin suite 2025-04  
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

### 3. Option — USB3 Vision Camera

Camera access permissions (one-time setup).  
This project is developed and tested with **Hikvision USB3 Vision cameras**.

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

| Mechanism | Description |
|-----------|-------------|
| TLS (HTTPS) | Self-signed RSA-2048 certificate auto-generated on first run; falls back to HTTP if `openssl` is unavailable |
| Session authentication | CSRF-protected login form; 1 s artificial delay and cumulative fail counter per IP |
| Token-gated WebSocket | Every SocketIO connection must present the session token; `is_admin` flag embedded in token |
| IP blacklist | IP blocked after `FAIL_MAX_ATTEMPTS` cumulative failures; persisted to `security_blacklist.json` |
| Admin privilege | All camera-control and plugin-management events require admin token; viewers receive frames only |
| Server header stripping | Flask/Werkzeug server banner removed from all HTTP responses |

Configuration: `FAIL_MAX_ATTEMPTS` in `u3v_webui/config.py`; full implementation in `u3v_webui/security.py` (`SecurityManager`).

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

| Event | Action |
|-------|--------|
| Admin opens camera | Added to `_auto_reopen_cams` |
| Admin explicitly closes camera | Removed from `_auto_reopen_cams` |
| Last viewer disconnects (no busy plugins) | Camera auto-closes; `_auto_reopen_cams` entry **preserved** |
| Next viewer connects | Cameras in `_auto_reopen_cams` that are not open are reopened |
| Server starts | `_auto_reopen_cams` is empty — no cameras open automatically |

**Source-camera protection (`held_cam_ids`):**  
Cross-camera plugins (e.g. `MultiView`) declare which cameras they depend on by overriding `held_cam_ids() -> set`.  `try_auto_close_all()` uses a two-step algorithm:

1. Find all cameras that have at least one busy plugin (`busy_cams`).
2. For each busy camera, expand the protected set with its plugins' `held_cam_ids()`.

A source camera is protected only when its **consumer camera is itself busy**.

| Virtual cam A plugins | Source cam B state | Result when no viewers |
|-----------------------|--------------------|------------------------|
| MultiView only | no busy plugins | **Both A and B close** |
| MultiView + MotionDetect (enabled) | no busy plugins | **Both stay open** |
| MultiView + MotionDetect (disabled) | no busy plugins | **Both A and B close** |
| MultiView + Recording (active) | no busy plugins | **Both stay open** |
| MultiView only | B has Recording active | A closes, **B stays open** (B is busy itself) |

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
| `RPiDriver` | `rpi_driver.py` | CSI (Raspberry Pi, video mode) | libcamera |
| `RPiImgDriver` | `rpi_img_driver.py` | CSI (Raspberry Pi, still mode) | libcamera |
| `VirtualDriver` | `virtual_driver.py` | Synthetic (test) | NumPy |

> **Still-mode note:** `RPiImgDriver` controls FPS entirely via `time.sleep()` rather than sensor timing registers.  Its `query_native_modes()` returns `{"width": w, "height": h}` entries **without** an `"fps"` key; the UI omits the fps column for these modes.

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
    ├─ Phase 1: pipeline plugins (in list order) → pipeline_frame
    │
    └─ Phase 2: display plugins (in list order)  → display_frame
    │
    ▼
AppState._pipeline_frames[cam_id] = pipeline_frame
AppState._display_frames[cam_id]  = display_frame
```

### 6.2 Pipeline Mode vs Display Mode

Both modes are **pipeline segment markers** — they determine which processing phase a plugin belongs to, not what the plugin does.  Any plugin type (recording, photo, detection, overlay) can be assigned either mode.

| Mode | Segment | Runs | Output frame |
|------|---------|------|--------------|
| `"pipeline"` | Phase 1 — runs first | Before any display plugin | `_pipeline_frames[cam_id]` |
| `"display"` | Phase 2 — runs after | After all pipeline plugins complete | `_display_frames[cam_id]` |

Execution within each phase is strictly sequential in list order.  Pipeline and display plugins may be interleaved in the list — each plugin is automatically routed to the correct phase regardless of its position.  Display plugins always receive the fully-processed pipeline frame as their starting point.

### 6.3 Per-Camera Plugin Pipeline

Each camera maintains an ordered list of active plugins.  Each entry carries an `instance_key` that uniquely identifies the instance (equals `name` for single-instance plugins; auto-suffixed for multi-instance):

```python
manager._pipeline[cam_id] = [
    {"name": "BasicParams",  "instance_key": "BasicParams",   "mode": "pipeline"},
    {"name": "BasicRecord",  "instance_key": "BasicRecord",   "mode": "pipeline"},
    {"name": "OverlayText",  "instance_key": "OverlayText",   "mode": "display"},
    {"name": "OverlayText",  "instance_key": "OverlayText_2", "mode": "display"},
]
```

Within each phase, execution is strictly sequential in list order.

**Cross-camera frame access:** Any plugin can read frames from any open camera by declaring `self._state = None`.  PluginManager injects the AppState instance, which exposes:

| Method | Returns |
|--------|---------|
| `state.get_latest_frame(cam_id)` | Final pipeline frame (used by recording/photo) |
| `state.get_display_frame(cam_id)` | Display frame (includes display-mode plugin effects) |

All plugins are per-camera; there is no separate "global" type.  Cross-camera functionality is achieved through state injection.

**Source plugin pattern (virtual cameras):** A plugin added to a VirtualDriver camera can ignore the incoming dummy frame and synthesize a new frame from any combination of real camera inputs.  Returning that synthesized frame from `on_frame` replaces the virtual camera's pipeline frame entirely — downstream plugins (recording, overlays) process the result normally.  Set `PLUGIN_MODE = "pipeline"` so the output propagates through the rest of the pipeline.

**Multi-instance plugins** set `PLUGIN_ALLOW_MULTIPLE = True` in `plugin.py`.  They may be added to the same camera multiple times; each instance receives a unique `instance_key` (e.g. `OverlayText`, `OverlayText_2`).  The instance key is injected into `_instance_key` on the plugin object and should be used to namespace state/param keys.

### 6.4 Example Execution Trace

```python
# Pipeline list: [BasicParams(pipeline), OverlayText(display), BasicRecord(pipeline)]
# After two-phase split:
#   Phase 1 (pipeline): BasicParams → BasicRecord
#   Phase 2 (display):  OverlayText

pipeline_frame = raw_frame

# Phase 1 — pipeline plugins in list order
result = basic_params.on_frame(pipeline_frame, hw_ts_ns, "cam_1")
if result is not None:
    pipeline_frame = result

result = basic_record.on_frame(pipeline_frame, hw_ts_ns, "cam_1")
if result is not None:
    pipeline_frame = result          # saved to disk

# Phase 2 — display plugins in list order
display_frame = pipeline_frame.copy()   # branch off final pipeline result
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

PLUGIN_CLASS          = MyPlugin
PLUGIN_NAME           = "my_plugin"         # unique identifier, used in API calls
PLUGIN_VERSION        = "1.0.0"
PLUGIN_DESCRIPTION    = "Does something useful"
PLUGIN_ALLOW_MULTIPLE = False               # set True to allow multiple instances per camera
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

    # --- Source-camera dependency guard ---
    def held_cam_ids(self) -> set:
        """
        Return cam_ids this plugin requires to stay open.
        Override in cross-camera plugins to protect source cameras from
        being auto-closed while they are still needed (e.g. MultiView).
        """
        return set()
```

### 7.4 Pipeline Mode Declaration

Default mode is set in `plugin.py` (the user can toggle it at runtime):

```python
# plugin.py — optional; defaults to "pipeline" if absent
PLUGIN_MODE = "pipeline"   # or "display"
```

### 7.4a Multi-Instance Plugins

Set `PLUGIN_ALLOW_MULTIPLE = True` in `plugin.py` to allow the user to add the plugin more than once to the same camera.

Each instance is assigned a unique `instance_key` injected into `self._instance_key`.  Use it to namespace state and parameter keys so instances do not collide:

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

The UI script reads `block.dataset.instance` to determine the instance key and builds param keys accordingly.

### 7.4b Cross-Camera Source Plugin

A source plugin is added to a **VirtualDriver** camera and ignores the incoming frame.  It reads from other cameras via `self._state` and returns a synthesized frame that becomes the virtual camera's pipeline output.

```python
class MyStereoPlugin(PluginBase):

    def __init__(self):
        self._state      = None   # injected — gives access to all camera frames
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
            return None   # pass through virtual camera's dummy frame unchanged

        fa = self._state.get_latest_frame(cam_a)
        fb = self._state.get_latest_frame(cam_b)
        if fa is None or fb is None:
            return None

        # synthesize and return — replaces the virtual camera's pipeline frame
        return _my_compose(fa, fb)
```

`plugin.py` for this plugin:

```python
PLUGIN_CLASS = MyStereoPlugin
PLUGIN_NAME  = "MyStereo"
PLUGIN_MODE  = "pipeline"   # result propagates to downstream plugins
```

Usage: open a VirtualDriver camera → add MyStereo → select cam_a and cam_b.  The virtual camera's stream, recording, and any downstream plugins all receive the composed output.

**Self-reference guard:** Never read the virtual camera's own `cam_id` as a source slot — it creates a circular dependency and stalls the pipeline.  Guard every slot before fetching:

```python
frames = [
    self._state.get_latest_frame(cams[i])
    if cams[i] and cams[i] != cam_id else None
    for i in range(n)
]
```

**`held_cam_ids()` — source-camera dependency declaration:**  
Override this method to list the cameras your plugin reads from.  Protection is conditional on the consumer camera being busy — see the table in §3.5.

```python
def held_cam_ids(self) -> set:
    with self._lock:
        return {c for c in self._source_cams if c}
```

| Consumer cam busy? | `held_cam_ids()` honoured? | Source cam auto-closed? |
|--------------------|---------------------------|------------------------|
| Yes (e.g. MotionDetect running) | Yes | No — stays open |
| No (only MultiView, idle) | No | Yes — closes normally |

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

| Plugin | PLUGIN_NAME | Version | Type | Mode | Key Actions | Key Params |
|--------|------------|---------|------|------|-------------|-----------|
| `BasicPhoto` | `BasicPhoto` | — | local | pipeline | `take_photo` | `photo_fmt` |
| `BasicRecord` | `BasicRecord` | — | local | pipeline | `toggle_record` | `rec_fmt` |
| `BasicBufRecord` | `BasicBufRecord` | — | local | pipeline | `toggle_buf_record` | `buf_fmt` |
| `BasicParams` | `BasicParams` | — | local | pipeline | — | `exposure`, `gain`, `fps`, `exposure_auto`, `gain_auto`, `exp_auto_upper` |
| `OverlayText` | `OverlayText` | — | local | display | — | `overlay_text` |
| `MultiView` | `MultiView` | 1.1.1 | source | pipeline | — | `multiview_layout`, `multiview_cam_1..4`, `multiview_src_1..4`, `multiview_res` |
| `Anaglyph` | `Anaglyph` | — | source | pipeline | — | `anaglyph_left_cam`, `anaglyph_right_cam`, `anaglyph_left_source`, `anaglyph_right_source`, `anaglyph_color_mode`, `anaglyph_left_is_red`, `anaglyph_parallax` |
| `MotionDetect` | `MotionDetect` | 1.0.3 | local | pipeline | `motdet_save_zones` | `motdet_enabled`, `motdet_var_threshold`, `motdet_min_pixel_count`, `motdet_cooldown_sec` |

**Source plugins** (`MultiView`, `Anaglyph`) are intended for **VirtualDriver** cameras.  They synthesize a composite frame from other camera inputs; the virtual camera's dummy frame is discarded.  `BasicRecord` automatically reallocates its ping-pong buffer to match the composite frame dimensions, so recording source plugin output requires no extra configuration.  Both source plugins implement `held_cam_ids()` to declare their source camera dependencies.

**MotionDetect** runs a background detection thread (MOG2, 10 Hz) on a downscaled copy of the pipeline frame.  When motion is detected in any configured zone the plugin saves a full-resolution JPEG to `captures/<YYYYMMDD>/motdet_<cam_safe>_<timestamp_us>.jpg`.  Each camera instance has independent zones (stored in `captures/motdet_zones/<cam_safe>_zones.json`), independent cooldown timers, and independent detection threads — they never interfere with each other even when triggered simultaneously.

---

*This document reflects version 6.13.3 of u3v-webui.*
