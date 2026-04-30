"""drivers/sr300_driver.py — Intel RealSense SR300 / SR305 via V4L2 (v1.0.1)

Works without pyrealsense2 by accessing the V4L2 nodes exposed by uvcvideo.
The librealsense2 SDK (2.50+) dropped SR300 enumeration; this driver works
around that by talking directly to the kernel UVC driver.

Virtual cameras:
  <usb_path>:depth     — colourised depth map (BGR, 640×480 @ 30 fps)
  <usb_path>:infrared  — IR image            (BGR grey, 640×480 @ 30 fps)
  <usb_path>:color     — colour camera       (BGR, up to 1920×1080 @ 30 fps)

Depth and IR share the same V4L2 node with different pixel formats (Z16 /
Y10).  Opening both simultaneously on the same physical device will cause
the later-opened stream to override the format on that node.  For typical
use (one stream at a time) this is not an issue.

No extra Python packages required beyond OpenCV and NumPy.
"""

import fcntl
import os
import pathlib
import struct
import threading
import time
from collections import deque
from typing import Optional

import cv2
import numpy as np

from .base import CameraDriver
from ..config import FPS_SAMPLE_FRAMES
from ..utils import log

# ── USB IDs ───────────────────────────────────────────────────────────────────

_VENDOR = "8086"
_PRODUCTS = {
    "0aa5": "RealSense SR300",
    "0b07": "RealSense SR305",
}

# ── V4L2 control ioctls (x86-64 Linux) ───────────────────────────────────────
# struct v4l2_control { uint32 id; int32 value; } = 8 bytes
# IOWR('V', 0x1b/0x1c, 8) → 0xC008561B / 0xC008561C

_VIDIOC_G_CTRL       = 0xC008561B
_VIDIOC_S_CTRL       = 0xC008561C

_V4L2_CID_EXPOSURE_AUTO     = 0x009A0901   # 1=manual, 3=auto
_V4L2_CID_EXPOSURE_ABSOLUTE = 0x009A0902   # 100 µs units
_V4L2_CID_GAIN              = 0x00980913

# VIDIOC_ENUM_FMT: struct v4l2_fmtdesc = 64 bytes
_VIDIOC_ENUM_FMT = 0xC0405602
_V4L2_BUF_TYPE_VIDEO_CAPTURE = 1

# Mapping colorizer index → cv2 colormap constant
_COLORMAP_TABLE = [
    cv2.COLORMAP_JET,
    cv2.COLORMAP_BONE,
    cv2.COLORMAP_HOT,
    cv2.COLORMAP_PINK,
    cv2.COLORMAP_OCEAN,
    cv2.COLORMAP_WINTER,
    cv2.COLORMAP_AUTUMN,
    cv2.COLORMAP_RAINBOW,
    cv2.COLORMAP_INFERNO,
]
_COLORMAP_NAMES = [
    "Jet", "Bone", "Hot", "Pink", "Ocean",
    "Winter", "Autumn", "Rainbow", "Inferno",
]

# ── Sysfs helpers ─────────────────────────────────────────────────────────────

def _enum_fmts(dev_path: str) -> set:
    """Return set of fourcc strings supported by this V4L2 node.

    struct v4l2_fmtdesc layout (64 bytes):
      offset  0: index (uint32)
      offset  4: type  (uint32)
      offset  8: flags (uint32)
      offset 12: description char[32]
      offset 44: pixelformat (uint32) ← fourcc lives here
    """
    try:
        fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return set()
    fmts: set = set()
    idx = 0
    while True:
        buf = bytearray(64)
        struct.pack_into("<I", buf, 0, idx)                           # index
        struct.pack_into("<I", buf, 4, _V4L2_BUF_TYPE_VIDEO_CAPTURE) # type
        try:
            fcntl.ioctl(fd, _VIDIOC_ENUM_FMT, buf)
            fmts.add(buf[44:48].decode("ascii", "replace"))           # pixelformat at offset 44
            idx += 1
        except (OSError, IOError):
            break
    os.close(fd)
    return fmts


def _scan_devices() -> list:
    """Return list of device dicts for each connected SR300/SR305."""
    results = []
    base = pathlib.Path("/sys/bus/usb/devices")
    if not base.exists():
        return results

    for dev in sorted(base.iterdir()):
        try:
            vendor  = (dev / "idVendor").read_text().strip()
            product = (dev / "idProduct").read_text().strip()
        except (OSError, IOError):
            continue
        if vendor != _VENDOR or product not in _PRODUCTS:
            continue

        usb_path   = dev.name
        model      = _PRODUCTS[product]
        depth_node = None
        color_node = None
        color_max_w = 0

        for iface in sorted(dev.glob(f"{usb_path}:*")):
            v4l_dir = iface / "video4linux"
            if not v4l_dir.exists():
                continue
            for vdev in sorted(v4l_dir.iterdir()):
                node = f"/dev/{vdev.name}"
                fmts = _enum_fmts(node)
                if "Z16 " in fmts and depth_node is None:
                    depth_node = node
                elif "Z16 " not in fmts and "YUYV" in fmts:
                    # Pick the YUYV node with highest supported width as color
                    try:
                        cap = cv2.VideoCapture(node, cv2.CAP_V4L2)
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 9999)
                        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        cap.release()
                    except Exception:
                        w = 0
                    if w > color_max_w:
                        color_max_w = w
                        color_node = node

        if depth_node:
            results.append({
                "usb_path":   usb_path,
                "model":      model,
                "product":    product,
                "depth_node": depth_node,
                "color_node": color_node,
            })

    return results

# ── V4L2 control helpers ──────────────────────────────────────────────────────

def _v4l2_get(fd: int, cid: int) -> Optional[int]:
    buf = bytearray(8)
    struct.pack_into("<I", buf, 0, cid)
    try:
        fcntl.ioctl(fd, _VIDIOC_G_CTRL, buf)
        return struct.unpack_from("<i", buf, 4)[0]
    except OSError:
        return None


def _v4l2_set(fd: int, cid: int, value: int) -> bool:
    buf = struct.pack("<Ii", cid, value)
    try:
        fcntl.ioctl(fd, _VIDIOC_S_CTRL, bytearray(buf))
        return True
    except OSError:
        return False

# ── Driver ────────────────────────────────────────────────────────────────────

class SR300Driver(CameraDriver):
    """
    Intel RealSense SR300 / SR305 driver using direct V4L2 access.

    Device IDs: ``<usb_path>:<stream>``
    where ``usb_path`` is the USB sysfs path (e.g. ``2-1``) and
    ``stream`` is ``depth``, ``infrared``, or ``color``.
    """

    SUPPORTED_PARAMS: frozenset = frozenset({
        "exposure", "gain", "exposure_auto",
        "rs_depth_colorizer",
    })
    DEFAULT_PARAMS: dict = {
        "exposure_auto":      True,
        "rs_depth_colorizer": 0,
    }

    # ── Discovery ─────────────────────────────────────────────────────────────

    @staticmethod
    def scan_devices() -> list:
        results = []
        for d in _scan_devices():
            usb   = d["usb_path"]
            model = d["model"]
            base  = f"{model} [{usb}]"
            for stream in ("depth", "infrared"):
                results.append({
                    "device_id": f"{usb}:{stream}",
                    "model":     model,
                    "serial":    usb,
                    "label":     f"{base} [{stream}]",
                    "driver":    "SR300Driver",
                })
            if d["color_node"]:
                results.append({
                    "device_id": f"{usb}:color",
                    "model":     model,
                    "serial":    usb,
                    "label":     f"{base} [color]",
                    "driver":    "SR300Driver",
                })
        return results

    # ── Init ──────────────────────────────────────────────────────────────────

    def __init__(self):
        super().__init__()
        self._usb_path:    str = ""
        self._stream_type: str = ""
        self._video_node:  str = ""

        self._running = False
        self._depth_colormap: int = cv2.COLORMAP_JET

        # Persistent fd for V4L2 control ioctls (opened in open(), closed in close())
        self._ctrl_fd: Optional[int] = None

        self._frame_lock = threading.Lock()
        self._latest:     Optional[np.ndarray] = None
        self._latest_raw: Optional[np.ndarray] = None   # native pixel format
        self._raw_format: str = ""

        self._fps_buf: deque    = deque()
        self._cap_fps_val: float = 0.0

        # Exposure/gain shadow for current_exposure / current_gain properties
        self._exp_shadow:  float = 0.0
        self._gain_shadow: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self, device_id: Optional[str] = None) -> dict:
        if not device_id or ":" not in device_id:
            raise RuntimeError(
                f"SR300 device_id must be '<usb_path>:<stream>', got {device_id!r}")

        self._usb_path, self._stream_type = device_id.rsplit(":", 1)
        if self._stream_type not in ("depth", "infrared", "color"):
            raise RuntimeError(f"Unknown stream: {self._stream_type!r}")

        devs = _scan_devices()
        dev  = next((d for d in devs if d["usb_path"] == self._usb_path), None)
        if dev is None:
            raise RuntimeError(f"SR300 device not found: {self._usb_path!r}")

        if self._stream_type == "color":
            if not dev["color_node"]:
                raise RuntimeError(
                    f"{dev['model']} [{self._usb_path}] has no colour node")
            self._video_node = dev["color_node"]
        else:
            self._video_node = dev["depth_node"]

        # Open a persistent fd for V4L2 control ioctls
        try:
            self._ctrl_fd = os.open(self._video_node, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            log(f"[SR300] cannot open ctrl fd {self._video_node}: {exc}")
            self._ctrl_fd = None

        log(f"[SR300] opened  {self._usb_path}:{self._stream_type} → {self._video_node}")
        return self._build_info(dev)

    def _build_info(self, dev: dict) -> dict:
        info: dict = {
            "model":   dev["model"],
            "serial":  self._usb_path,
            "width":   640,
            "height":  480,
            "fps_min": 6.0,
            "fps_max": 30.0,
            "exp_min": 1.0,
            "exp_max": 165000.0,
            "gain_min": 0.0,
            "gain_max": 255.0,
            "rs_depth_colorizer_names": _COLORMAP_NAMES,
        }
        if self._ctrl_fd is not None:
            # Try to read actual exposure range (not critical if fails)
            pass
        return info

    def stop(self) -> None:
        self._running = False

    def close(self) -> None:
        self._running = False
        if self._ctrl_fd is not None:
            try:
                os.close(self._ctrl_fd)
            except OSError:
                pass
            self._ctrl_fd = None
        log(f"[SR300] closed  {self._usb_path}:{self._stream_type}")

    def read_hw_bounds(self) -> dict:
        return {
            "exp_min":  1.0,    "exp_max":  165000.0,
            "gain_min": 0.0,    "gain_max": 255.0,
            "fps_min":  6.0,    "fps_max":  30.0,
        }

    # ── Acquisition thread ────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        dispatch = {
            "depth":     self._run_depth,
            "infrared":  self._run_ir,
            "color":     self._run_color,
        }
        dispatch[self._stream_type]()

    # ── Colour ────────────────────────────────────────────────────────────────

    def _run_color(self) -> None:
        cap = cv2.VideoCapture(self._video_node, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)

        if not cap.isOpened():
            log(f"[SR300] cannot open color node {self._video_node}")
            return

        log(f"[SR300] color stream active  {self._video_node}")
        while self._running:
            ret, bgr = cap.read()
            if ret:
                # Color stream: OpenCV converts YUYV→BGR internally;
                # raw channel is not available for this stream.
                self._deliver(bgr)
        cap.release()

    # ── Depth ─────────────────────────────────────────────────────────────────

    def _run_depth(self) -> None:
        cap = cv2.VideoCapture(self._video_node, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC,      cv2.VideoWriter_fourcc(*"Z16 "))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)   # keep raw bytes

        if not cap.isOpened():
            log(f"[SR300] cannot open depth node {self._video_node}")
            return

        log(f"[SR300] depth stream active  {self._video_node}")
        while self._running:
            ret, raw = cap.read()
            if not ret:
                continue
            # raw: uint8 shape=(1, H*W*2); reinterpret as uint16 depth map
            depth = raw.flatten().view(np.uint16).reshape(480, 640)
            bgr   = self._colorize_depth(depth)
            self._deliver(bgr, raw=depth, raw_fmt="Z16")
        cap.release()

    # ── Infrared ──────────────────────────────────────────────────────────────

    def _run_ir(self) -> None:
        cap = cv2.VideoCapture(self._video_node, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC,      cv2.VideoWriter_fourcc(*"Y10 "))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)

        if not cap.isOpened():
            log(f"[SR300] cannot open IR node {self._video_node}")
            return

        log(f"[SR300] IR stream active  {self._video_node}")
        while self._running:
            ret, raw = cap.read()
            if not ret:
                continue
            # Y10: 10-bit values in 16-bit containers → shift right 2 → uint8
            ir16 = raw.flatten().view(np.uint16).reshape(480, 640)
            ir8  = (ir16 >> 2).astype(np.uint8)
            bgr  = cv2.cvtColor(ir8, cv2.COLOR_GRAY2BGR)
            self._deliver(bgr, raw=ir16, raw_fmt="Y10")
        cap.release()

    # ── Depth colourisation ───────────────────────────────────────────────────

    def _colorize_depth(self, depth: np.ndarray) -> np.ndarray:
        valid    = depth > 0
        depth_u8 = np.zeros(depth.shape, dtype=np.uint8)
        if valid.any():
            d_min = int(depth[valid].min())
            d_max = int(depth[valid].max())
            if d_max > d_min:
                depth_u8[valid] = (
                    (depth[valid].astype(np.float32) - d_min)
                    / (d_max - d_min) * 255
                ).astype(np.uint8)
            else:
                depth_u8[valid] = 128
        bgr = cv2.applyColorMap(depth_u8, self._depth_colormap)
        bgr[~valid] = 0
        return bgr

    # ── Frame delivery ────────────────────────────────────────────────────────

    def _deliver(self, bgr: np.ndarray,
                 raw: Optional[np.ndarray] = None,
                 raw_fmt: str = "") -> None:
        ts_ns = time.time_ns()
        with self._frame_lock:
            self._latest     = bgr
            self._latest_raw = raw
            self._raw_format = raw_fmt

        now = time.monotonic()
        self._fps_buf.append(now)
        if len(self._fps_buf) > FPS_SAMPLE_FRAMES:
            self._fps_buf.popleft()
        n = len(self._fps_buf)
        if n >= 2:
            span = self._fps_buf[-1] - self._fps_buf[0]
            self._cap_fps_val = (n - 1) / span if span > 0 else 0.0

        if self.on_frame is not None:
            try:
                self.on_frame(bgr, ts_ns)
            except Exception as exc:
                log(f"[SR300] on_frame error ({self._stream_type}): {exc}")

    # ── Parameter control ─────────────────────────────────────────────────────

    def set_param(self, key: str, value) -> None:
        fd = self._ctrl_fd

        if key == "exposure" and fd is not None:
            # Disable auto-exposure, then set absolute exposure (100µs units)
            _v4l2_set(fd, _V4L2_CID_EXPOSURE_AUTO, 1)   # 1 = manual
            _v4l2_set(fd, _V4L2_CID_EXPOSURE_ABSOLUTE, max(1, int(float(value) / 100)))
            self._exp_shadow = float(value)

        elif key == "gain" and fd is not None:
            _v4l2_set(fd, _V4L2_CID_GAIN, int(float(value)))
            self._gain_shadow = float(value)

        elif key == "exposure_auto" and fd is not None:
            # V4L2_CID_EXPOSURE_AUTO: 1 = manual, 3 = aperture-priority (auto)
            _v4l2_set(fd, _V4L2_CID_EXPOSURE_AUTO, 3 if value else 1)

        elif key == "rs_depth_colorizer":
            idx = int(value)
            if 0 <= idx < len(_COLORMAP_TABLE):
                self._depth_colormap = _COLORMAP_TABLE[idx]

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def latest_frame(self) -> Optional[np.ndarray]:
        with self._frame_lock:
            f = self._latest
            return f.copy() if f is not None else None

    @property
    def latest_raw_frame(self) -> Optional[np.ndarray]:
        with self._frame_lock:
            f = self._latest_raw
            return f.copy() if f is not None else None

    @property
    def raw_frame_format(self) -> Optional[str]:
        return self._raw_format or None

    @property
    def cap_fps(self) -> float:
        return self._cap_fps_val

    @property
    def current_exposure(self) -> float:
        if self._ctrl_fd is not None:
            v = _v4l2_get(self._ctrl_fd, _V4L2_CID_EXPOSURE_ABSOLUTE)
            if v is not None:
                return float(v) * 100.0   # 100µs units → µs
        return self._exp_shadow

    @property
    def current_gain(self) -> float:
        if self._ctrl_fd is not None:
            v = _v4l2_get(self._ctrl_fd, _V4L2_CID_GAIN)
            if v is not None:
                return float(v)
        return self._gain_shadow

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Native modes ──────────────────────────────────────────────────────────

    def query_native_modes(self) -> list:
        return [
            {"width": 640, "height": 480, "fps": 30.0},
            {"width": 640, "height": 480, "fps": 15.0},
            {"width": 640, "height": 480, "fps":  6.0},
        ]
