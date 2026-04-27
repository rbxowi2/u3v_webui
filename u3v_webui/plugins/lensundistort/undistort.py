"""plugins/lensundistort/undistort.py — LensUndistort plugin (1.1.0)

Local plugin: one instance per camera.
Reads calibration data produced by LensCalibrate and applies cv2.remap()
to every frame in the pipeline.  Supports both normal and fisheye models.

Maps are built lazily on the first frame after enable, and rebuilt automatically
if the frame resolution changes (e.g. after apply_native_mode).

Output modes:
  balance (fov_out=0): 0.0=crop black borders, 1.0=retain full original FOV.
  fov_out  (fov_out>0): rectilinear output at the specified H-FOV (degrees).
                        K_new is derived directly — balance is ignored.
- v1.1.0: Add fov_out parameter — rectilinear output at a specific H-FOV angle.
"""

import json
import math
import os
import threading
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import log

CALIB_DIR = os.path.join(CAPTURE_DIR, "calibration")


class LensUndistort(PluginBase):
    """Apply lens undistortion using a pre-computed calibration file. Local plugin."""

    def __init__(self):
        self._sio        = None
        self._emit_state = None

        self._cam_id = ""
        self._lock   = threading.Lock()

        # User settings
        self._enabled  = False
        self._balance  = 0.0   # 0 = crop black borders, 1 = full original FOV
        self._fov_out  = 0.0   # 0 = use balance; >0 = rectilinear output H-FOV (degrees)

        # Loaded calibration (from JSON)
        self._cal: Optional[dict] = None

        # Computed undistort maps
        self._map1: Optional[np.ndarray] = None
        self._map2: Optional[np.ndarray] = None
        self._map_size: Optional[tuple]  = None   # (w, h) the maps were built for

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "LensUndistort"

    @property
    def version(self) -> str:
        return "1.1.0"

    @property
    def description(self) -> str:
        return "Apply lens undistortion to live frames (normal and fisheye)"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        self._reload_cal()   # load JSON; maps built on first frame

    def on_camera_close(self, cam_id: str = ""):
        with self._lock:
            self._map1     = None
            self._map2     = None
            self._map_size = None

    # ── Frame pipeline ────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            if not self._enabled:
                return None
            cal      = self._cal
            map1     = self._map1
            map2     = self._map2
            map_size = self._map_size

        if cal is None:
            return None

        fh, fw = frame.shape[:2]

        # Rebuild maps if not yet built or frame resolution changed
        if map1 is None or map_size != (fw, fh):
            self._build_maps(cal, (fw, fh))
            with self._lock:
                map1 = self._map1
                map2 = self._map2

        if map1 is None:
            return None

        return cv2.remap(frame, map1, map2, cv2.INTER_LINEAR)

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        with self._lock:
            cal       = self._cal
            maps_ok   = self._map1 is not None
            enabled   = self._enabled
            balance   = self._balance
            fov_out   = self._fov_out
        return {
            "undistort_enabled":       enabled,
            "undistort_balance":       balance,
            "undistort_fov_out":       fov_out,
            "undistort_has_cal":       cal is not None,
            "undistort_maps_ready":    maps_ok,
            "undistort_lens_type":     cal["lens_type"]     if cal else None,
            "undistort_rms":           cal["rms"]           if cal else None,
            "undistort_calibrated_at": cal["calibrated_at"] if cal else None,
        }

    # ── Params ────────────────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == "undistort_enabled":
            with self._lock:
                self._enabled = bool(value)
            return True
        if key == "undistort_balance":
            new_bal = max(0.0, min(1.0, float(value)))
            with self._lock:
                changed = (new_bal != self._balance)
                self._balance = new_bal
                if changed:
                    self._map1 = None; self._map2 = None; self._map_size = None
            return True
        if key == "undistort_fov_out":
            new_fov = max(0.0, min(179.0, float(value)))
            with self._lock:
                changed = (new_fov != self._fov_out)
                self._fov_out = new_fov
                if changed:
                    self._map1 = None; self._map2 = None; self._map_size = None
            return True
        return False

    # ── Actions ───────────────────────────────────────────────────────────────

    def handle_action(self, action: str, data: dict, driver) -> "tuple | None":
        if action != "undistort_reload":
            return None
        ok = self._reload_cal()
        if ok:
            return True, "Calibration reloaded"
        return False, "No calibration data found for this camera"

    # ── Map construction ──────────────────────────────────────────────────────

    def _reload_cal(self) -> bool:
        """Load calibration JSON from disk; invalidate existing maps."""
        cal = self._load_cal_file()
        with self._lock:
            self._cal      = cal
            self._map1     = None
            self._map2     = None
            self._map_size = None
        if cal:
            log(f"[LensUndistort] Calibration loaded [{self._cam_id}] "
                f"lens={cal['lens_type']} rms={cal['rms']:.4f}")
        else:
            log(f"[LensUndistort] No calibration data for [{self._cam_id}]")
        return cal is not None

    def _build_maps(self, cal: dict, frame_size: tuple):
        """Build undistort maps for the given frame size.  Thread-safe."""
        w, h         = frame_size
        cal_w, cal_h = cal["image_size"]
        lens_type    = cal["lens_type"]

        K = np.array(cal["camera_matrix"], dtype=np.float64)
        D = np.array(cal["dist_coeffs"],   dtype=np.float64)

        if (cal_w, cal_h) != (w, h):
            sx = w / cal_w; sy = h / cal_h
            K = K.copy()
            K[0, 0] *= sx; K[0, 2] *= sx
            K[1, 1] *= sy; K[1, 2] *= sy

        with self._lock:
            balance = self._balance
            fov_out = self._fov_out

        try:
            if fov_out > 0:
                # Rectilinear output: K_new derived from desired H-FOV
                f_new = (w / 2) / math.tan(math.radians(fov_out / 2))
                K_new = np.array(
                    [[f_new, 0, w / 2], [0, f_new, h / 2], [0, 0, 1]],
                    dtype=np.float64)

            if lens_type == "fisheye":
                D = D.reshape(4, 1)
                if fov_out <= 0:
                    K_new = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                        K, D, (w, h), np.eye(3), balance=balance)
                map1, map2 = cv2.fisheye.initUndistortRectifyMap(
                    K, D, np.eye(3), K_new, (w, h), cv2.CV_32FC1)
            else:
                D = D.reshape(1, -1)
                if fov_out <= 0:
                    K_new, _ = cv2.getOptimalNewCameraMatrix(K, D, (w, h), balance)
                map1, map2 = cv2.initUndistortRectifyMap(
                    K, D, None, K_new, (w, h), cv2.CV_32FC1)

            with self._lock:
                self._map1 = map1; self._map2 = map2; self._map_size = (w, h)
            if fov_out > 0:
                log(f"[LensUndistort] Maps built [{self._cam_id}] {w}×{h} fov_out={fov_out:.0f}°")
            else:
                log(f"[LensUndistort] Maps built [{self._cam_id}] {w}×{h} balance={balance}")

        except Exception as e:
            with self._lock:
                self._map1 = None; self._map2 = None; self._map_size = None
            log(f"[LensUndistort] Map build error [{self._cam_id}]: {e}")

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load_cal_file(self) -> Optional[dict]:
        if not self._cam_id:
            return None
        safe = self._cam_id.replace("/", "_").replace(":", "_").replace(" ", "_")
        path = os.path.join(CALIB_DIR, f"{safe}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
