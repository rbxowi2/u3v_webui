"""plugins/record/basic.py — BasicRecord local plugin (1.0.0)

Local plugin: one instance per camera.
Handles continuous recording via ping-pong buffer + disk-write thread.
"""

import os
import threading
from datetime import datetime
from typing import Optional

import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import disk_free_gb, elapsed_str, imwrite_fmt, log
from .defaults import DISK_JOIN_TIMEOUT, JPEG_QUALITY


# ── Disk-write helper thread ───────────────────────────────────────────────────

class _DiskWriteThread(threading.Thread):
    """Writes ping-pong buffer slots to disk as frames arrive."""

    def __init__(self):
        super().__init__(daemon=True)
        self.buf:          Optional[np.ndarray] = None
        self.ts_buf:       Optional[list]       = None
        self.save_dir:     str = ""
        self.fmt:          str = "BMP"
        self.jpeg_quality: int = JPEG_QUALITY
        self.running           = False
        self._count        = 0
        self._slot         = 0
        self._start_ts:    int = 0
        self._event        = threading.Event()

    @property
    def count(self) -> int:
        return self._count

    def signal(self, slot: int):
        self._slot = slot
        self._event.set()

    def run(self):
        ext = ".jpg" if self.fmt == "JPG" else ".bmp"
        self.running = True
        while self.running:
            if self._event.wait(timeout=0.5):
                self._event.clear()
                slot  = self._slot
                ts_ns = self.ts_buf[slot]
                if self._start_ts == 0:
                    self._start_ts = ts_ns
                tc   = elapsed_str(self._start_ts, ts_ns)
                path = os.path.join(self.save_dir,
                                    f"frame_{self._count:06d}_{tc}{ext}")
                try:
                    imwrite_fmt(path, self.buf[slot], self.fmt, self.jpeg_quality)
                    self._count += 1
                except Exception as e:
                    log(f"[Disk] Write failed: {e}")

    def stop(self):
        self.running = False


# ── Plugin ─────────────────────────────────────────────────────────────────────

class BasicRecord(PluginBase):
    """Continuous frame recording to disk.  Local plugin — one instance per camera."""

    def __init__(self):
        self._sio        = None
        self._emit_state = None

        self._fmt        = "BMP"
        self._cam_w      = 0
        self._cam_h      = 0
        self._pp_buf:    Optional[np.ndarray] = None
        self._pp_ts:     list = [0, 0]
        self._pp_wslot   = 0
        self._disk_thread: Optional[_DiskWriteThread] = None
        self._recording  = False
        self._record_dir = ""
        self._audio_available = False
        self._lock = threading.Lock()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "BasicRecord"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Continuous frame-by-frame recording"


    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        w, h = cam_info["width"], cam_info["height"]
        with self._lock:
            self._cam_w = w
            self._cam_h = h
            self._pp_buf   = np.zeros((2, h, w, 3), dtype=np.uint8)
            self._pp_ts    = [0, 0]
            self._pp_wslot = 0
            self._audio_available = bool(cam_info.get("supports_audio", False))

    def on_camera_close(self, cam_id: str = ""):
        with self._lock:
            recording = self._recording
        if recording:
            self._do_stop_record()

    # ── Frame hook ────────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            if not self._recording or self._pp_buf is None:
                return None
            fh, fw = frame.shape[:2]
            # Source plugins (MultiView, Anaglyph) may produce a different
            # resolution than the virtual camera's native size.  Reallocate
            # the ping-pong buffer whenever the frame shape changes.
            if self._pp_buf.shape[1:3] != (fh, fw):
                self._pp_buf = np.zeros((2, fh, fw, 3), dtype=np.uint8)
                self._cam_w, self._cam_h = fw, fh
                if self._disk_thread is not None:
                    self._disk_thread.buf = self._pp_buf
            slot = self._pp_wslot
            self._pp_ts[slot] = hw_ts_ns
            np.copyto(self._pp_buf[slot], frame)
            if self._disk_thread:
                self._disk_thread.signal(slot)
            self._pp_wslot = 1 - slot
        return None

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        with self._lock:
            return {
                "recording":       self._recording,
                "rec_fmt":         self._fmt,
                "record_status":   self._record_status(),
                "audio_available": self._audio_available,
            }

    def frame_payload(self, cam_id: str = "") -> dict:
        with self._lock:
            recording = self._recording
            status    = self._record_status()
        result = {"recording": recording}
        if status:
            result["rec_status"] = status
        return result

    def is_busy(self, cam_id: str = "") -> bool:
        return self._recording

    # ── Action / param ────────────────────────────────────────────────────────

    def handle_action(self, action: str, data: dict, driver) -> "tuple | None":
        if action != "toggle_record":
            return None
        return self._toggle_record(data.get("cam_id", ""), driver)

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key != "rec_fmt":
            return False
        with self._lock:
            self._fmt = str(value)
        return True

    # ── Recording ─────────────────────────────────────────────────────────────

    def _toggle_record(self, cam_id: str, driver) -> tuple:
        if self._recording:
            result = self._do_stop_record()
            self._mark_idle(cam_id)
            return result
        if driver is None:
            return False, "Record failed: camera not open"
        if self._pp_buf is None:
            return False, "Record failed: camera not ready"
        return self._do_start_record()

    def _do_start_record(self) -> tuple:
        now = datetime.now()
        record_dir = os.path.join(
            CAPTURE_DIR, now.strftime("%Y%m%d"),
            f"video_{now.strftime('%Y%m%d_%H%M%S')}"
        )
        os.makedirs(record_dir, exist_ok=True)

        with self._lock:
            dt              = _DiskWriteThread()
            dt.buf          = self._pp_buf
            dt.ts_buf       = self._pp_ts
            dt.save_dir     = record_dir
            dt.fmt          = self._fmt
            dt.jpeg_quality = JPEG_QUALITY
            dt.start()
            self._disk_thread = dt
            self._pp_wslot    = 0
            self._recording   = True
            self._record_dir  = record_dir

        log(f"Record start  -> {record_dir}")
        return True, f"Recording: {os.path.relpath(record_dir, CAPTURE_DIR)}"

    def _do_stop_record(self) -> tuple:
        with self._lock:
            self._recording  = False
            dt               = self._disk_thread
            self._disk_thread = None
            record_dir       = self._record_dir
        n = 0
        if dt:
            dt.stop()
            dt.join(timeout=DISK_JOIN_TIMEOUT)
            n = dt.count
        log(f"Record stop  {n} frames  -> {record_dir}")
        return True, f"Saved {n} frames -> {os.path.relpath(record_dir, CAPTURE_DIR)}"

    def _record_status(self) -> str:
        """Must be called with self._lock held."""
        if self._recording and self._disk_thread:
            used_gb = self._disk_thread.count * self._cam_w * self._cam_h * 3 / 1024 ** 3
            free_gb = disk_free_gb(self._record_dir)
            return (
                f"Recording: {self._disk_thread.count} frames / "
                f"Used {used_gb:.2f} GB  (Free {free_gb:.1f} GB)"
            )
        return ""
