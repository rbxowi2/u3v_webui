"""plugins/bufrecord/basic.py — BasicBufRecord local plugin (1.0.0)

Local plugin: one instance per camera.
Accumulates frames in RAM then saves them all at once in a background thread.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import elapsed_str, imwrite_fmt, log
from .defaults import BUF_MAX_BYTES, BUF_SAVE_WORKERS, JPEG_QUALITY


class BasicBufRecord(PluginBase):
    """RAM-buffer recording with background flush-to-disk.  Local plugin — one instance per camera."""

    def __init__(self):
        self._sio        = None
        self._emit_state = None

        self._fmt          = "BMP"
        self._buf_frames:  list = []
        self._buf_ts:      list = []
        self._buf_count    = 0
        self._buf_bytes_c  = 0
        self._buf_recording = False
        self._buf_saving    = False
        self._buf_active    = False
        self._lock = threading.Lock()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "BasicBufRecord"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "RAM-buffer recording (accumulate then save)"

    @property
    def plugin_type(self) -> str:
        return "local"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        pass  # fmt preserved; buf state resets naturally on next toggle

    def on_camera_close(self, cam_id: str = ""):
        with self._lock:
            self._buf_active    = False
            self._buf_recording = False

    # ── Frame hook ────────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        save_trigger = None
        with self._lock:
            if not self._buf_active:
                return None
            self._buf_frames.append(frame.copy())
            self._buf_ts.append(hw_ts_ns)
            self._buf_count   += 1
            self._buf_bytes_c += frame.nbytes
            if self._buf_bytes_c >= BUF_MAX_BYTES:
                self._buf_active    = False
                self._buf_recording = False
                self._buf_saving    = True
                frames, self._buf_frames = self._buf_frames, []
                ts,     self._buf_ts    = self._buf_ts,     []
                save_trigger = (frames, ts, self._fmt)

        if save_trigger is not None:
            frames, ts, fmt = save_trigger
            threading.Thread(
                target=self._save_buf_frames,
                args=(frames, ts, fmt, cam_id),
                daemon=True,
            ).start()
        return None

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        with self._lock:
            return {
                "buf_recording":    self._buf_recording,
                "buf_saving":       self._buf_saving,
                "buf_fmt":          self._fmt,
                "buf_record_status": self._buf_status(),
            }

    def frame_payload(self, cam_id: str = "") -> dict:
        with self._lock:
            buf_rec = self._buf_recording
            status  = self._buf_status()
        result = {"buf_rec": buf_rec}
        if status:
            result["buf_status"] = status
        return result

    def is_busy(self, cam_id: str = "") -> bool:
        return self._buf_saving

    # ── Action / param ────────────────────────────────────────────────────────

    def handle_action(self, action: str, data: dict, driver) -> "tuple | None":
        if action != "toggle_buf_record":
            return None
        return self._toggle_buf_record(data.get("cam_id", ""), driver)

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key != "buf_fmt":
            return False
        with self._lock:
            self._fmt = str(value)
        return True

    # ── Buffer recording ──────────────────────────────────────────────────────

    def _toggle_buf_record(self, cam_id: str, driver) -> tuple:
        with self._lock:
            saving      = self._buf_saving
            recording   = self._buf_recording
        if saving:
            return False, "Saving, please wait..."
        if recording:
            return self._do_stop_buf_record(cam_id)
        if driver is None:
            return False, "Buffer record failed: camera not open"
        return self._do_start_buf_record()

    def _do_start_buf_record(self) -> tuple:
        with self._lock:
            self._buf_frames  = []
            self._buf_ts      = []
            self._buf_count   = 0
            self._buf_bytes_c = 0
            self._buf_active    = True
            self._buf_recording = True
        log("Buffer record start")
        return True, "Buffer recording..."

    def _do_stop_buf_record(self, cam_id: str) -> tuple:
        with self._lock:
            self._buf_active    = False
            self._buf_recording = False
            frames, self._buf_frames = self._buf_frames, []
            ts,     self._buf_ts    = self._buf_ts,     []
        if frames:
            with self._lock:
                self._buf_saving = True
                fmt = self._fmt
            threading.Thread(
                target=self._save_buf_frames,
                args=(frames, ts, fmt, cam_id),
                daemon=True,
            ).start()
        return True, "Buffer recording stopped, saving in background..."

    def _save_buf_frames(self, frames: list, timestamps: list,
                          fmt: str, cam_id: str):
        now = datetime.now()
        save_dir = os.path.join(
            CAPTURE_DIR, now.strftime("%Y%m%d"),
            f"buf_{now.strftime('%Y%m%d_%H%M%S')}"
        )
        os.makedirs(save_dir, exist_ok=True)
        ext      = ".jpg" if fmt == "JPG" else ".bmp"
        start_ts = timestamps[0] if timestamps else 0

        def write_one(args):
            i, frame, ts_ns = args
            tc   = elapsed_str(start_ts, ts_ns)
            path = os.path.join(save_dir, f"frame_{i:06d}_{tc}{ext}")
            imwrite_fmt(path, frame, fmt, JPEG_QUALITY)

        with ThreadPoolExecutor(max_workers=BUF_SAVE_WORKERS) as pool:
            pool.map(write_one,
                     ((i, f, t) for i, (f, t) in enumerate(zip(frames, timestamps))))

        n = len(frames)
        with self._lock:
            self._buf_saving = False

        log(f"Buffer record saved  {n} frames  -> {save_dir}")
        if self._sio:
            self._sio.emit("status", {
                "msg": f"Saved {n} frames -> {os.path.relpath(save_dir, CAPTURE_DIR)}",
            })
        if self._emit_state:
            self._emit_state()
        self._mark_idle(cam_id)

    def _buf_status(self) -> str:
        """Must be called with self._lock held."""
        if self._buf_saving:
            return "Saving buffer recording..."
        if self._buf_recording:
            used_gb = self._buf_bytes_c / 1024 ** 3
            rem_gb  = (BUF_MAX_BYTES - self._buf_bytes_c) / 1024 ** 3
            return (
                f"Buffer recording: {self._buf_count} frames / "
                f"Used {used_gb:.2f} GB  (Remaining {rem_gb:.2f} GB)"
            )
        return ""
