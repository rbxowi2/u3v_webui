"""
plugins/overlaytext/basic.py — OverlayTextPlugin (1.0.0)

Local plugin: renders user-supplied text centered on the camera frame.
"""

from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase


class OverlayTextPlugin(PluginBase):
    """Renders centered text overlay on the camera frame."""

    def __init__(self):
        self._text: str = ""

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "OverlayText"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Render centered text overlay on camera frame"

    @property
    def plugin_type(self) -> str:
        return "local"

    # ── Frame hook ────────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        text = self._text.strip()
        if not text:
            return None

        out = frame.copy()
        h, w = out.shape[:2]

        font       = cv2.FONT_HERSHEY_DUPLEX
        font_scale = max(0.6, w / 800.0)
        thickness  = max(1, int(font_scale * 2))

        (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
        x = (w - tw) // 2
        y = (h + th) // 2

        # Black shadow + white foreground for readability on any background
        cv2.putText(out, text, (x, y), font, font_scale,
                    (0, 0, 0), thickness + 2, cv2.LINE_AA)
        cv2.putText(out, text, (x, y), font, font_scale,
                    (255, 255, 255), thickness, cv2.LINE_AA)
        return out

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        return {"overlay_text": self._text}

    # ── Parameter dispatch ────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key != "overlay_text":
            return False
        self._text = str(value)
        return True
