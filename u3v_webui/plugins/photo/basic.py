"""plugins/photo/basic.py — BasicPhoto local plugin (1.0.0)

Local plugin: one instance per camera.
Handles single-shot photo capture only.
"""

import os
from datetime import datetime
from typing import Optional

import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import disk_free_gb, imwrite_fmt, log
from .defaults import JPEG_QUALITY


class BasicPhoto(PluginBase):
    """Single-shot photo capture.  Local plugin — one instance per camera."""

    def __init__(self):
        self._sio        = None
        self._emit_state = None
        self._state      = None
        self._fmt        = "BMP"

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "BasicPhoto"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Single-shot photo capture"

    @property
    def plugin_type(self) -> str:
        return "local"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        pass  # fmt preserved across reopen

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        return {"photo_fmt": self._fmt}

    # ── Action / param ────────────────────────────────────────────────────────

    def handle_action(self, action: str, data: dict, driver) -> "tuple | None":
        if action != "take_photo":
            return None
        return self._take_photo(data.get("cam_id", ""), driver)

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key != "photo_fmt":
            return False
        self._fmt = str(value)
        return True

    # ── Photo ─────────────────────────────────────────────────────────────────

    def _take_photo(self, cam_id: str, driver) -> tuple:
        frame: Optional[np.ndarray] = None
        if self._state and cam_id:
            frame = self._state.get_latest_frame(cam_id)
        if frame is None and driver is not None:
            frame = driver.latest_frame
        if frame is None:
            return False, "Capture failed: no frame received"

        ext = ".jpg" if self._fmt == "JPG" else ".bmp"
        now = datetime.now()
        subdir = os.path.join(CAPTURE_DIR, now.strftime("%Y%m%d"))
        os.makedirs(subdir, exist_ok=True)
        path = os.path.join(subdir, f"photo_{now.strftime('%Y%m%d_%H%M%S')}{ext}")
        imwrite_fmt(path, frame, self._fmt, JPEG_QUALITY)

        size_mb = os.path.getsize(path) / 1024 ** 2
        free_gb = disk_free_gb(CAPTURE_DIR)
        msg = (f"Saved: {now.strftime('%Y%m%d')}/{os.path.basename(path)}"
               f" ({size_mb:.2f} MB, {free_gb:.1f} GB free)")
        log(f"Photo  {os.path.basename(path)}  {size_mb:.2f} MB")
        return True, msg
