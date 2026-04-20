"""
plugins/anaglyph/basic.py — AnaglyphPlugin (2.1.0)

Designed for virtual cameras: on_frame() ignores the incoming dummy frame,
reads independently from two real cameras via _state, and returns the composed
anaglyph as the virtual camera's pipeline frame.

Settings (via set_param):
  anaglyph_left_cam     — cam_id of left-eye camera
  anaglyph_right_cam    — cam_id of right-eye camera
  anaglyph_left_source  — "pipeline" | "display"
  anaglyph_right_source — "pipeline" | "display"
  anaglyph_color_mode   — "rc" (Red/Cyan)  |  "rb" (Red/Blue)
  anaglyph_left_is_red  — True: left eye = Red channel
                           False: left eye = Cyan/Blue channel
  anaglyph_parallax     — horizontal pixel shift on right image (px)
"""

import threading
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase


class AnaglyphPlugin(PluginBase):
    """Red-blue anaglyph stereo compositor for virtual cameras."""

    def __init__(self):
        self._emit_state  = None
        self._state       = None   # injected by PluginManager

        self._left_cam:     str  = ""
        self._right_cam:    str  = ""
        self._left_source:  str  = "pipeline"
        self._right_source: str  = "pipeline"
        self._color_mode:   str  = "rc"   # "rc" = Red/Cyan, "rb" = Red/Blue
        self._left_is_red:  bool = True
        self._parallax:     int  = 0

        self._lock = threading.Lock()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Anaglyph"

    @property
    def version(self) -> str:
        return "2.1.0"

    @property
    def description(self) -> str:
        return "Red-blue anaglyph stereo from two cameras"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        pass

    def on_unload(self):
        pass

    # ── Frame processing ──────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        """Read from both source cameras and return the composed anaglyph.
        The incoming frame (from the virtual camera driver) is ignored."""
        with self._lock:
            left_cam      = self._left_cam
            right_cam     = self._right_cam
            left_source   = self._left_source
            right_source  = self._right_source
            color_mode    = self._color_mode
            left_is_red   = self._left_is_red
            parallax      = self._parallax

        if not left_cam or not right_cam or self._state is None:
            return None

        lf = _get_frame(self._state, left_cam,  left_source)
        rf = _get_frame(self._state, right_cam, right_source)
        if lf is None or rf is None:
            return None

        return _make_anaglyph(lf, rf, color_mode, left_is_red, parallax)

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        return {
            "anaglyph_left_cam":     self._left_cam,
            "anaglyph_right_cam":    self._right_cam,
            "anaglyph_left_source":  self._left_source,
            "anaglyph_right_source": self._right_source,
            "anaglyph_color_mode":   self._color_mode,
            "anaglyph_left_is_red":  self._left_is_red,
            "anaglyph_parallax":     self._parallax,
        }

    # ── Parameter dispatch ────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        with self._lock:
            if key == "anaglyph_left_cam":
                self._left_cam = str(value)
            elif key == "anaglyph_right_cam":
                self._right_cam = str(value)
            elif key == "anaglyph_left_source":
                if value in ("pipeline", "display"):
                    self._left_source = value
            elif key == "anaglyph_right_source":
                if value in ("pipeline", "display"):
                    self._right_source = value
            elif key == "anaglyph_color_mode":
                if value in ("rc", "rb"):
                    self._color_mode = value
            elif key == "anaglyph_left_is_red":
                self._left_is_red = bool(value)
            elif key == "anaglyph_parallax":
                self._parallax = int(value)
            else:
                return False
        if self._emit_state:
            self._emit_state()
        return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_frame(state, cam_id: str, source: str):
    if source == "display":
        return state.get_display_frame(cam_id)
    return state.get_latest_frame(cam_id)


def _make_anaglyph(left: np.ndarray, right: np.ndarray,
                   color_mode: str, left_is_red: bool,
                   parallax: int) -> np.ndarray:
    h, w = left.shape[:2]
    if right.shape[:2] != (h, w):
        right = cv2.resize(right, (w, h))

    if parallax != 0:
        blank = np.zeros((h, abs(parallax), 3), dtype=np.uint8)
        if parallax > 0:
            right = np.hstack([blank, right[:, :-parallax]])
        else:
            right = np.hstack([right[:, -parallax:], blank])

    lg = cv2.cvtColor(left,  cv2.COLOR_BGR2GRAY)
    rg = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)

    out = np.zeros((h, w, 3), dtype=np.uint8)

    if color_mode == "rc":
        # Red / Cyan — cyan eye gets both G and B channels (more natural depth)
        if left_is_red:
            out[:, :, 2] = lg          # R = left
            out[:, :, 1] = rg          # G = right (cyan)
            out[:, :, 0] = rg          # B = right (cyan)
        else:
            out[:, :, 2] = rg          # R = right
            out[:, :, 1] = lg          # G = left (cyan)
            out[:, :, 0] = lg          # B = left (cyan)
    else:
        # Red / Blue
        if left_is_red:
            out[:, :, 2] = lg          # R = left
            out[:, :, 0] = rg          # B = right
        else:
            out[:, :, 0] = lg          # B = left
            out[:, :, 2] = rg          # R = right

    return out
