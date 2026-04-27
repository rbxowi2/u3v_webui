"""plugins/stereorectify/stereorectify.py — StereoRectify plugin (1.1.0)

Computes epipolar rectification from StereoCalibrate data.
Single-shot computation — no iterative capture loop.

Workflow:
  1. Sidebar: select L/R cameras → click "Rectify"
  2. Modal: loads captures/stereo/<L>__<R>.json, runs stereoRectify,
     shows live rectified preview with epipolar lines overlay.
  3. Adjust alpha (0=crop to valid ROI, 1=full image).
  4. Save → captures/rectify/<safe_L>__<safe_R>.json

Output JSON: R1, R2, P1, P2, Q, roi1, roi2, image_size, lens_type, alpha.
Rectification maps are recomputed at runtime from these values.
"""

import io
import json
import math
import os
import threading
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import log

STEREO_DIR  = os.path.join(CAPTURE_DIR, "stereo")
RECTIFY_DIR = os.path.join(CAPTURE_DIR, "rectify")


class StereoRectify(PluginBase):

    _instances: dict = {}

    def __init__(self):
        self._sio        = None
        self._state      = None
        self._emit_state = None
        self._cam_id     = ""
        self._lock       = threading.Lock()

        # Sidebar settings
        self._cam_left  = ""
        self._cam_right = ""
        self._alpha     = 1.0   # 0 = crop to valid pixels, 1 = full image
        self._fov_out   = 0.0   # 0 = use alpha; >0 = output H-FOV in degrees

        # Session state
        self._session_active = False
        self._lens_type  = "normal"
        self._img_size: Optional[tuple] = None
        self._R1: Optional[np.ndarray] = None
        self._R2: Optional[np.ndarray] = None
        self._P1: Optional[np.ndarray] = None
        self._P2: Optional[np.ndarray] = None
        self._Q:  Optional[np.ndarray] = None
        self._roi1: Optional[tuple]    = None
        self._roi2: Optional[tuple]    = None
        self._map_L: Optional[tuple]   = None  # (map1, map2) for left camera
        self._map_R: Optional[tuple]   = None  # (map1, map2) for right camera

        # Cal caches
        self._stereo_cache: Optional[dict] = None
        self._stereo_cache_mtime: float    = -1.0
        self._rectify_cache: Optional[dict] = None
        self._rectify_cache_mtime: float    = -1.0

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:    return "StereoRectify"

    @property
    def version(self) -> str: return "1.1.0"

    @property
    def description(self) -> str:
        return "Epipolar rectification from StereoCalibrate data"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(RECTIFY_DIR, exist_ok=True)

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        StereoRectify._instances[cam_id] = self

    def on_camera_close(self, cam_id: str = ""):
        with self._lock:
            self._session_active = False
        StereoRectify._instances.pop(cam_id, None)

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        cal = self._load_rectify_cal(self._cam_left, self._cam_right)
        with self._lock:
            active = self._session_active
        return {
            "rectify_cam_left":      self._cam_left,
            "rectify_cam_right":     self._cam_right,
            "rectify_alpha":         self._alpha,
            "rectify_fov_out":       self._fov_out,
            "rectify_has_data":      cal is not None,
            "rectify_calibrated_at": cal["calibrated_at"] if cal else None,
            "rectify_session_active": active,
        }

    # ── Params ────────────────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == "rectify_cam_left":
            with self._lock:
                self._cam_left = str(value)
            if self._emit_state:
                self._emit_state()
            return True
        if key == "rectify_cam_right":
            with self._lock:
                self._cam_right = str(value)
            if self._emit_state:
                self._emit_state()
            return True
        if key == "rectify_alpha":
            with self._lock:
                self._alpha = max(0.0, min(1.0, float(value)))
            return True
        if key == "rectify_fov_out":
            with self._lock:
                self._fov_out = max(0.0, min(179.0, float(value)))
            return True
        return False

    # ── Routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        from flask import request as _req, send_file

        is_admin   = ctx["is_admin"]
        emit_state = ctx["emit_state"]

        @app.route("/plugin/stereorectify/snapshot")
        def _rect_snapshot():
            cam_id    = _req.args.get("cam_id", "")
            side      = _req.args.get("side", "L")       # "L" or "R"
            rectified = _req.args.get("rectified", "0") == "1"

            # Any instance with _state can serve any camera's frame
            inst = StereoRectify._instances.get(cam_id)
            if inst is None or inst._state is None:
                inst = next((i for i in StereoRectify._instances.values()
                             if i._state is not None), None)
            if inst is None or inst._state is None:
                return "Not found", 404

            src_cam = inst._cam_left if side == "L" else inst._cam_right
            if not src_cam:
                src_cam = cam_id
            frame = inst._state.get_latest_frame(src_cam)
            if frame is None:
                return "No frame", 404

            if rectified:
                frame = inst._apply_rect(frame, side)

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                return "Encode error", 500
            return send_file(io.BytesIO(buf.tobytes()), mimetype="image/jpeg")

        @sio.on("rectify_open")
        def _rectify_open(data):
            if not is_admin(): return
            cam_id    = data.get("cam_id", "")
            cam_left  = data.get("cam_left",  "")
            cam_right = data.get("cam_right", "")
            alpha     = float(data.get("alpha", 1.0))
            fov_out   = float(data.get("fov_out", 0.0))

            inst = StereoRectify._instances.get(cam_id)
            if inst is None:
                sio.emit("rectify_event",
                         {"type": "error", "msg": "StereoRectify not active for this camera"},
                         to=_req.sid)
                return

            with inst._lock:
                inst._cam_left  = cam_left  or inst._cam_left
                inst._cam_right = cam_right or inst._cam_right
                inst._alpha     = max(0.0, min(1.0, alpha))
                inst._fov_out   = max(0.0, min(179.0, fov_out))

            try:
                result = inst._run_rectify(
                    inst._cam_left, inst._cam_right, inst._alpha, inst._fov_out)
            except Exception as e:
                log(f"[StereoRectify] rectify failed: {e}")
                sio.emit("rectify_event",
                         {"type": "error", "msg": str(e)}, to=_req.sid)
                return

            with inst._lock:
                inst._session_active = True

            sio.emit("rectify_event", {"type": "computed", **result}, to=_req.sid)
            emit_state()

        @sio.on("rectify_set_alpha")
        def _rectify_set_alpha(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            alpha  = float(data.get("alpha", 1.0))
            inst   = StereoRectify._instances.get(cam_id)
            if inst is None: return
            with inst._lock:
                inst._alpha   = max(0.0, min(1.0, alpha))
                inst._fov_out = 0.0   # alpha mode clears fov_out
            try:
                result = inst._run_rectify(inst._cam_left, inst._cam_right,
                                           inst._alpha, 0.0)
                sio.emit("rectify_event", {"type": "computed", **result}, to=_req.sid)
            except Exception as e:
                sio.emit("rectify_event",
                         {"type": "error", "msg": str(e)}, to=_req.sid)

        @sio.on("rectify_set_fov")
        def _rectify_set_fov(data):
            if not is_admin(): return
            cam_id  = data.get("cam_id", "")
            fov_out = float(data.get("fov_out", 0.0))
            inst    = StereoRectify._instances.get(cam_id)
            if inst is None: return
            with inst._lock:
                inst._fov_out = max(0.0, min(179.0, fov_out))
            try:
                result = inst._run_rectify(inst._cam_left, inst._cam_right,
                                           inst._alpha, inst._fov_out)
                sio.emit("rectify_event", {"type": "computed", **result}, to=_req.sid)
            except Exception as e:
                sio.emit("rectify_event",
                         {"type": "error", "msg": str(e)}, to=_req.sid)

        @sio.on("rectify_save")
        def _rectify_save(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = StereoRectify._instances.get(cam_id)
            if inst is None: return
            result = inst._save_calibration()
            sio.emit("rectify_event", result, to=_req.sid)
            emit_state()

        @sio.on("rectify_cancel")
        def _rectify_cancel(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = StereoRectify._instances.get(cam_id)
            if inst:
                with inst._lock:
                    inst._session_active = False
            emit_state()

    # ── Rectification computation ─────────────────────────────────────────────

    def _run_rectify(self, cam_left: str, cam_right: str,
                     alpha: float, fov_out: float = 0.0) -> dict:
        cal = self._load_stereo_cal_data(cam_left, cam_right)
        if cal is None:
            raise RuntimeError(
                f"No StereoCalibrate data for '{cam_left}' ↔ '{cam_right}'")

        lens  = cal["lens_type"]
        KL    = np.array(cal["KL"], dtype=np.float64)
        KR    = np.array(cal["KR"], dtype=np.float64)
        DL_r  = np.array(cal["DL"], dtype=np.float64)
        DR_r  = np.array(cal["DR"], dtype=np.float64)
        R     = np.array(cal["R"],  dtype=np.float64)
        T     = np.array(cal["T"],  dtype=np.float64)
        w, h  = cal["image_size"]

        # Shape D for stereoRectify (pass zeros for fisheye — maps handle undistort)
        if lens == "fisheye":
            D_rect_L = np.zeros((1, 4), dtype=np.float64)
            D_rect_R = np.zeros((1, 4), dtype=np.float64)
            DL_map   = DL_r.reshape(4, 1)
            DR_map   = DR_r.reshape(4, 1)
        else:
            D_rect_L = DL_r.reshape(1, -1)
            D_rect_R = DR_r.reshape(1, -1)
            DL_map   = D_rect_L
            DR_map   = D_rect_R

        R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(
            KL, D_rect_L, KR, D_rect_R,
            (w, h), R, T, alpha=alpha,
        )

        if fov_out > 0:
            # Override output FOV: recompute P1/P2 with desired focal length
            f_new  = (w / 2.0) / math.tan(math.radians(fov_out / 2.0))
            cx_new = w / 2.0
            cy_new = h / 2.0
            f_old  = float(P1[0, 0])
            scale  = (f_new / f_old) if f_old != 0 else 1.0
            P1 = P1.copy()
            P1[0, 0] = f_new; P1[1, 1] = f_new
            P1[0, 2] = cx_new; P1[1, 2] = cy_new
            P2 = P2.copy()
            P2[0, 0] = f_new; P2[1, 1] = f_new
            P2[0, 2] = cx_new; P2[1, 2] = cy_new
            P2[0, 3] = P2[0, 3] * scale   # keep physical baseline Tx constant
            # Recompute Q from modified P1/P2
            Tx = -P2[0, 3] / f_new if f_new != 0 else 1.0
            Q = np.array([
                [1, 0, 0, -cx_new],
                [0, 1, 0, -cy_new],
                [0, 0, 0,  f_new],
                [0, 0, -1.0 / Tx, 0.0],
            ], dtype=np.float64)
            roi1 = (0, 0, w, h)
            roi2 = (0, 0, w, h)

        if lens == "fisheye":
            map1L, map2L = cv2.fisheye.initUndistortRectifyMap(
                KL, DL_map, R1, P1, (w, h), cv2.CV_16SC2)
            map1R, map2R = cv2.fisheye.initUndistortRectifyMap(
                KR, DR_map, R2, P2, (w, h), cv2.CV_16SC2)
        else:
            map1L, map2L = cv2.initUndistortRectifyMap(
                KL, DL_map, R1, P1, (w, h), cv2.CV_16SC2)
            map1R, map2R = cv2.initUndistortRectifyMap(
                KR, DR_map, R2, P2, (w, h), cv2.CV_16SC2)

        with self._lock:
            self._lens_type = lens
            self._img_size  = (w, h)
            self._R1 = R1;  self._R2 = R2
            self._P1 = P1;  self._P2 = P2
            self._Q  = Q
            self._roi1 = tuple(roi1);  self._roi2 = tuple(roi2)
            self._map_L = (map1L, map2L)
            self._map_R = (map1R, map2R)
            self._cam_left  = cam_left
            self._cam_right = cam_right
            self._alpha     = alpha
            self._fov_out   = fov_out

        log(f"[StereoRectify] computed  L={cam_left} R={cam_right} "
            f"lens={lens} alpha={alpha:.2f} fov_out={fov_out:.1f} "
            f"roi1={tuple(roi1)} roi2={tuple(roi2)}")
        return {
            "ok":         True,
            "lens_type":  lens,
            "image_size": [w, h],
            "roi1":       list(roi1),
            "roi2":       list(roi2),
            "alpha":      alpha,
            "fov_out":    fov_out,
        }

    # ── Frame application ─────────────────────────────────────────────────────

    def _apply_rect(self, frame: np.ndarray, side: str) -> np.ndarray:
        with self._lock:
            maps     = self._map_L if side == "L" else self._map_R
            img_size = self._img_size
        if maps is None:
            return frame
        fh, fw = frame.shape[:2]
        if img_size is not None and (fw, fh) != img_size:
            return frame
        return cv2.remap(frame, maps[0], maps[1], cv2.INTER_LINEAR)

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_calibration(self) -> dict:
        with self._lock:
            if self._R1 is None:
                return {"type": "save_result", "ok": False,
                        "error": "No rectification computed yet"}
            data = {
                "format_version": 1,
                "lens_type":      self._lens_type,
                "cam_left":       self._cam_left,
                "cam_right":      self._cam_right,
                "R1":             self._R1.tolist(),
                "R2":             self._R2.tolist(),
                "P1":             self._P1.tolist(),
                "P2":             self._P2.tolist(),
                "Q":              self._Q.tolist(),
                "roi1":           list(self._roi1),
                "roi2":           list(self._roi2),
                "image_size":     list(self._img_size),
                "alpha":          self._alpha,
                "fov_out":        self._fov_out,
                "calibrated_at":  datetime.now().isoformat(timespec="seconds"),
            }
            cam_L = self._cam_left
            cam_R = self._cam_right

        self._save_rectify_cal(cam_L, cam_R, data)
        log(f"[StereoRectify] saved L={cam_L} R={cam_R}")
        return {"type": "save_result", "ok": True,
                "calibrated_at": data["calibrated_at"]}

    # ── Persistence ───────────────────────────────────────────────────────────

    def _safe(self, s: str) -> str:
        return s.replace("/", "_").replace(":", "_").replace(" ", "_")

    def _stereo_cal_path(self, cam_left: str, cam_right: str) -> str:
        return os.path.join(STEREO_DIR,
                            f"{self._safe(cam_left)}__{self._safe(cam_right)}.json")

    def _rectify_cal_path(self, cam_left: str, cam_right: str) -> str:
        os.makedirs(RECTIFY_DIR, exist_ok=True)
        return os.path.join(RECTIFY_DIR,
                            f"{self._safe(cam_left)}__{self._safe(cam_right)}.json")

    def _load_stereo_cal_data(self, cam_left: str, cam_right: str) -> Optional[dict]:
        if not cam_left or not cam_right:
            return None
        path = self._stereo_cal_path(cam_left, cam_right)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        if mtime == self._stereo_cache_mtime and self._stereo_cache is not None:
            return self._stereo_cache
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._stereo_cache       = data
            self._stereo_cache_mtime = mtime
            return data
        except Exception:
            return None

    def _load_rectify_cal(self, cam_left: str, cam_right: str) -> Optional[dict]:
        if not cam_left or not cam_right:
            return None
        path = self._rectify_cal_path(cam_left, cam_right)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        if mtime == self._rectify_cache_mtime and self._rectify_cache is not None:
            return self._rectify_cache
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._rectify_cache       = data
            self._rectify_cache_mtime = mtime
            return data
        except Exception:
            return None

    def _save_rectify_cal(self, cam_left: str, cam_right: str, data: dict):
        path = self._rectify_cal_path(cam_left, cam_right)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._rectify_cache       = data
            self._rectify_cache_mtime = os.path.getmtime(path)
        except Exception as e:
            log(f"[StereoRectify] save error: {e}")
