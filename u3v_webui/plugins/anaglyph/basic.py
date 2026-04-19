"""
plugins/anaglyph/basic.py — AnaglyphPlugin (1.0.0)

Global plugin: merges the final pipeline frames of two user-selected cameras
into a red-blue anaglyph image and pushes it to all connected viewers via a
dedicated "anaglyph_frame" SocketIO event.

Settings (via set_param):
  anaglyph_left_cam    — cam_id of the left-eye camera
  anaglyph_right_cam   — cam_id of the right-eye camera
  anaglyph_left_is_red — True: left=Red right=Blue  False: left=Blue right=Red
  anaglyph_parallax    — horizontal pixel shift applied to the right image
                         (positive → shift right, negative → shift left)
"""

import base64
import threading
import time
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase
from ...utils import log


class AnaglyphPlugin(PluginBase):
    """Red-blue anaglyph stereo compositor.  Global plugin."""

    def __init__(self):
        self._sio        = None
        self._emit_state = None

        self._left_cam:    str  = ""
        self._right_cam:   str  = ""
        self._left_is_red: bool = True   # True → left=R, right=B
        self._parallax:    int  = 0      # pixels

        self._frames: dict = {}   # cam_id → latest display frame (np.ndarray)
        self._lock = threading.Lock()

        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "Anaglyph"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Red-blue anaglyph stereo from two cameras"

    @property
    def plugin_type(self) -> str:
        return "global"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        self._running = True
        self._thread = threading.Thread(target=self._push_loop, daemon=True,
                                        name="anaglyph-push")
        self._thread.start()

    def on_unload(self):
        self._running = False

    # ── Frame hook (display mode — receives final pipeline result) ─────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            if cam_id and cam_id in (self._left_cam, self._right_cam):
                self._frames[cam_id] = frame.copy()
        return None   # never modify the frame

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        return {
            "anaglyph_left_cam":    self._left_cam,
            "anaglyph_right_cam":   self._right_cam,
            "anaglyph_left_is_red": self._left_is_red,
            "anaglyph_parallax":    self._parallax,
        }

    # ── Parameter dispatch ────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        with self._lock:
            if key == "anaglyph_left_cam":
                self._left_cam = str(value)
                self._frames.pop(str(value), None)
            elif key == "anaglyph_right_cam":
                self._right_cam = str(value)
                self._frames.pop(str(value), None)
            elif key == "anaglyph_left_is_red":
                self._left_is_red = bool(value)
            elif key == "anaglyph_parallax":
                self._parallax = int(value)
            else:
                return False
        if self._emit_state:
            self._emit_state()
        return True

    # ── Background push loop ──────────────────────────────────────────────────

    def _push_loop(self):
        while self._running:
            time.sleep(1 / 30)
            try:
                self._try_emit()
            except Exception as e:
                log(f"[Anaglyph] push error: {e}")

    def _try_emit(self):
        with self._lock:
            left_cam    = self._left_cam
            right_cam   = self._right_cam
            left_is_red = self._left_is_red
            parallax    = self._parallax
            lf = self._frames.get(left_cam)
            rf = self._frames.get(right_cam)

        if not left_cam or not right_cam or lf is None or rf is None:
            return
        if not self._sio:
            return

        anaglyph = _make_anaglyph(lf, rf, left_is_red, parallax)
        ok, buf = cv2.imencode(".jpg", anaglyph, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        img_b64 = base64.b64encode(buf).decode()
        self._sio.emit("anaglyph_frame", {"img": img_b64})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_anaglyph(left: np.ndarray, right: np.ndarray,
                   left_is_red: bool, parallax: int) -> np.ndarray:
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
    if left_is_red:
        out[:, :, 2] = lg   # R channel (BGR index 2)
        out[:, :, 0] = rg   # B channel
    else:
        out[:, :, 0] = lg   # B channel
        out[:, :, 2] = rg   # R channel
    return out
