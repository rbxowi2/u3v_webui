"""plugins/stereomatch/stereomatch.py — StereoMatch plugin (1.1.0)

Live stereo disparity matching from rectified frames.

Workflow:
  1. Sidebar: select L/R cameras → click "Match"
  2. Modal: background thread grabs rectified frames, runs BM/SGBM,
     streams colorized disparity map.
  3. Tune parameters in real-time.
  4. Save → captures/match/<safe_L>__<safe_R>.json
"""

import io
import json
import os
import threading
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import log

STEREO_DIR  = os.path.join(CAPTURE_DIR, "stereo")
RECTIFY_DIR = os.path.join(CAPTURE_DIR, "rectify")
MATCH_DIR   = os.path.join(CAPTURE_DIR, "match")

_FPS = 10
_DT  = 1.0 / _FPS


def _letterbox(img: np.ndarray, fw: int, fh: int) -> np.ndarray:
    """Fit img into (fw × fh) with equal-aspect scaling and black borders."""
    ih, iw = img.shape[:2]
    scale  = min(fw / iw, fh / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas  = np.zeros((fh, fw, 3), dtype=np.uint8)
    x0 = (fw - nw) // 2
    y0 = (fh - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


class StereoMatch(PluginBase):

    _instances: dict = {}

    def __init__(self):
        self._sio        = None
        self._state      = None
        self._emit_state = None
        self._cam_id     = ""
        self._lock       = threading.Lock()

        # Sidebar / overlay settings
        self._cam_left     = ""
        self._cam_right    = ""
        self._enabled      = False   # overlay active in main view
        self._display_side = "L"     # "L" or "R" — which camera shows disparity

        # Session flag (modal open)
        self._session_active = False

        # Parameters
        self._algorithm        = "sgbm"
        self._num_disparities  = 64
        self._block_size       = 9
        self._p1               = -1   # -1 = auto (8 * 3 * bs^2)
        self._p2               = -1   # -1 = auto (32 * 3 * bs^2)
        self._uniqueness_ratio = 5
        self._speckle_window   = 100
        self._speckle_range    = 2

        # Thread
        self._match_active = False

        # Result
        self._disparity_jpg: Optional[bytes] = None

        # Map cache
        self._maps_cam: Optional[tuple] = None
        self._map_L: Optional[tuple]    = None
        self._map_R: Optional[tuple]    = None
        self._img_size: Optional[tuple] = None

        # JSON caches
        self._rectify_cache: Optional[dict] = None
        self._rectify_cache_mtime: float    = -1.0
        self._stereo_cache: Optional[dict]  = None
        self._stereo_cache_mtime: float     = -1.0

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self)        -> str: return "StereoMatch"
    @property
    def version(self)     -> str: return "1.1.0"
    @property
    def description(self) -> str: return "Live stereo disparity from rectified frames"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(MATCH_DIR, exist_ok=True)

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        StereoMatch._instances[cam_id] = self

    def on_camera_close(self, cam_id: str = ""):
        with self._lock:
            self._match_active   = False
            self._enabled        = False
            self._session_active = False
        StereoMatch._instances.pop(cam_id, None)

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        cal = self._load_match_cal(self._cam_left, self._cam_right)
        with self._lock:
            active  = self._match_active
            enabled = self._enabled
            dside   = self._display_side
        return {
            "match_cam_left":       self._cam_left,
            "match_cam_right":      self._cam_right,
            "match_enabled":        enabled,
            "match_display_side":   dside,
            "match_has_data":       cal is not None,
            "match_saved_at":       cal.get("saved_at") if cal else None,
            "match_session_active": active,
        }

    # ── Params ────────────────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == "match_cam_left":
            with self._lock: self._cam_left = str(value)
            if self._emit_state: self._emit_state()
            return True
        if key == "match_cam_right":
            with self._lock: self._cam_right = str(value)
            if self._emit_state: self._emit_state()
            return True
        if key == "match_algorithm":
            with self._lock: self._algorithm = str(value)
            return True
        if key == "match_num_disparities":
            v = max(16, (int(value) // 16) * 16)
            with self._lock: self._num_disparities = v
            return True
        if key == "match_block_size":
            v = max(5, int(value) | 1)
            with self._lock: self._block_size = v
            return True
        if key == "match_p1":
            with self._lock: self._p1 = int(value)
            return True
        if key == "match_p2":
            with self._lock: self._p2 = int(value)
            return True
        if key == "match_uniqueness_ratio":
            with self._lock: self._uniqueness_ratio = max(0, min(50, int(value)))
            return True
        if key == "match_speckle_window":
            with self._lock: self._speckle_window = max(0, int(value))
            return True
        if key == "match_speckle_range":
            with self._lock: self._speckle_range = max(1, int(value))
            return True
        if key == "match_display_side":
            with self._lock: self._display_side = "R" if str(value) == "R" else "L"
            return True
        if key == "match_enabled":
            enabled = bool(value)
            if enabled:
                try:
                    self._ensure_maps(self._cam_left, self._cam_right)
                except Exception as e:
                    log(f"[StereoMatch] enable failed: {e}")
                    if self._emit_state: self._emit_state()
                    return True
                self._start_worker()
            with self._lock:
                self._enabled = enabled
            if self._emit_state: self._emit_state()
            return True
        return False

    # ── Frame pipeline ────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            if not self._enabled:
                return None
            jpg = self._disparity_jpg

        if jpg is None:
            return None
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        fh, fw = frame.shape[:2]
        if img.shape[:2] != (fh, fw):
            img = _letterbox(img, fw, fh)
        return img

    # ── Routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        from flask import request as _req, send_file

        is_admin   = ctx["is_admin"]
        emit_state = ctx["emit_state"]

        @app.route("/plugin/stereomatch/disparity")
        def _disparity():
            cam_id = _req.args.get("cam_id", "")
            inst = StereoMatch._instances.get(cam_id)
            if inst is None:
                inst = next((i for i in StereoMatch._instances.values()
                             if i._state is not None), None)
            if inst is None:
                return "Not found", 404
            with inst._lock:
                jpg = inst._disparity_jpg
            if jpg is None:
                return "No disparity", 404
            return send_file(io.BytesIO(jpg), mimetype="image/jpeg")

        @sio.on("match_open")
        def _match_open(data):
            if not is_admin(): return
            cam_id    = data.get("cam_id", "")
            cam_left  = data.get("cam_left",  "")
            cam_right = data.get("cam_right", "")

            inst = StereoMatch._instances.get(cam_id)
            if inst is None:
                sio.emit("match_event",
                         {"type": "error", "msg": "StereoMatch not active"}, to=_req.sid)
                return

            with inst._lock:
                if cam_left:  inst._cam_left  = cam_left
                if cam_right: inst._cam_right = cam_right

            try:
                inst._ensure_maps(inst._cam_left, inst._cam_right)
            except Exception as e:
                sio.emit("match_event",
                         {"type": "error", "msg": str(e)}, to=_req.sid)
                return

            inst._start_worker()
            with inst._lock:
                inst._session_active = True
            sio.emit("match_event", {"type": "started"}, to=_req.sid)
            emit_state()

        @sio.on("match_set_params")
        def _match_set_params(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst = StereoMatch._instances.get(cam_id)
            if inst is None: return
            for k, v in data.items():
                if k != "cam_id":
                    inst.handle_set_param(k, v, None)

        @sio.on("match_save")
        def _match_save(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst = StereoMatch._instances.get(cam_id)
            if inst is None: return
            result = inst._save_params()
            sio.emit("match_event", result, to=_req.sid)
            emit_state()

        @sio.on("match_cancel")
        def _match_cancel(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst = StereoMatch._instances.get(cam_id)
            if inst:
                with inst._lock:
                    inst._session_active = False
                    # Stop worker only if overlay is also disabled
                    if not inst._enabled:
                        inst._match_active = False
            emit_state()

    # ── Worker ────────────────────────────────────────────────────────────────

    def _start_worker(self):
        with self._lock:
            if self._match_active:
                return
            self._match_active = True
        threading.Thread(target=self._worker_loop, daemon=True).start()

    def _worker_loop(self):
        log(f"[StereoMatch] worker started  L={self._cam_left} R={self._cam_right}")
        while True:
            with self._lock:
                if not self._match_active:
                    break
                cam_L  = self._cam_left
                cam_R  = self._cam_right
                side   = self._display_side
                algo   = self._algorithm
                nd     = self._num_disparities
                bs     = self._block_size
                p1     = self._p1
                p2     = self._p2
                ur     = self._uniqueness_ratio
                sws    = self._speckle_window
                sr     = self._speckle_range
                map_L  = self._map_L
                map_R  = self._map_R
                isz    = self._img_size

            t0 = time.time()
            try:
                if self._state is None or map_L is None:
                    time.sleep(_DT)
                    continue

                fL = self._state.get_latest_frame(cam_L)
                fR = self._state.get_latest_frame(cam_R)
                if fL is None or fR is None:
                    time.sleep(_DT)
                    continue

                if isz is not None:
                    if (fL.shape[1], fL.shape[0]) != isz:
                        fL = cv2.resize(fL, isz)
                    if (fR.shape[1], fR.shape[0]) != isz:
                        fR = cv2.resize(fR, isz)

                rL = cv2.remap(fL, map_L[0], map_L[1], cv2.INTER_LINEAR)
                rR = cv2.remap(fR, map_R[0], map_R[1], cv2.INTER_LINEAR)
                gL = cv2.cvtColor(rL, cv2.COLOR_BGR2GRAY)
                gR = cv2.cvtColor(rR, cv2.COLOR_BGR2GRAY)

                if algo == "bm":
                    matcher = cv2.StereoBM_create(numDisparities=nd, blockSize=bs)
                else:
                    a_p1 = p1 if p1 > 0 else 8  * 3 * bs * bs
                    a_p2 = p2 if p2 > 0 else 32 * 3 * bs * bs
                    if a_p2 <= a_p1:
                        a_p2 = a_p1 * 4
                    matcher = cv2.StereoSGBM_create(
                        minDisparity=0, numDisparities=nd, blockSize=bs,
                        P1=a_p1, P2=a_p2, disp12MaxDiff=1,
                        uniquenessRatio=ur, speckleWindowSize=sws,
                        speckleRange=sr, mode=cv2.StereoSGBM_MODE_SGBM_3WAY,
                    )

                if side == "R":
                    # Right-reference: flip both → compute → flip result back
                    # Keeps minDisparity=0 valid and produces symmetric quality
                    gL_f = cv2.flip(gL, 1)
                    gR_f = cv2.flip(gR, 1)
                    disp = cv2.flip(matcher.compute(gR_f, gL_f), 1)
                else:
                    disp = matcher.compute(gL, gR)
                disp_f = disp.astype(np.float32) / 16.0
                valid  = disp_f > 0
                valid_pct = float(valid.mean() * 100.0)
                avg_d     = float(disp_f[valid].mean()) if valid.any() else 0.0

                disp_u8 = np.zeros(disp_f.shape, dtype=np.uint8)
                if valid.any():
                    dmin, dmax = disp_f[valid].min(), disp_f[valid].max()
                    if dmax > dmin:
                        disp_u8[valid] = (
                            (disp_f[valid] - dmin) / (dmax - dmin) * 255
                        ).astype(np.uint8)
                    else:
                        disp_u8[valid] = 128
                colored = cv2.applyColorMap(disp_u8, cv2.COLORMAP_JET)
                colored[~valid] = 0

                ok, buf = cv2.imencode(".jpg", colored,
                                       [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    with self._lock:
                        self._disparity_jpg = buf.tobytes()
                    if self._sio:
                        self._sio.emit("match_stats", {
                            "cam_id":        self._cam_id,
                            "avg_disparity": round(avg_d, 1),
                            "valid_pct":     round(valid_pct, 1),
                        })

            except Exception as e:
                log(f"[StereoMatch] worker error: {e}")

            slack = _DT - (time.time() - t0)
            if slack > 0:
                time.sleep(slack)

        log(f"[StereoMatch] worker stopped")

    # ── Maps ──────────────────────────────────────────────────────────────────

    def _ensure_maps(self, cam_left: str, cam_right: str):
        with self._lock:
            if self._maps_cam == (cam_left, cam_right):
                return

        rect = self._load_rectify_cal(cam_left, cam_right)
        if rect is None:
            raise RuntimeError(
                f"No StereoRectify data for '{cam_left}' ↔ '{cam_right}'. "
                "Run StereoRectify first.")

        stereo = self._load_stereo_cal(cam_left, cam_right)
        if stereo is None:
            raise RuntimeError(
                f"No StereoCalibrate data for '{cam_left}' ↔ '{cam_right}'.")

        lens = rect["lens_type"]
        w, h = rect["image_size"]
        R1   = np.array(rect["R1"], dtype=np.float64)
        R2   = np.array(rect["R2"], dtype=np.float64)
        P1   = np.array(rect["P1"], dtype=np.float64)
        P2   = np.array(rect["P2"], dtype=np.float64)

        KL = np.array(stereo["KL"], dtype=np.float64)
        KR = np.array(stereo["KR"], dtype=np.float64)
        DL = np.array(stereo["DL"], dtype=np.float64)
        DR = np.array(stereo["DR"], dtype=np.float64)

        if lens == "fisheye":
            m1L, m2L = cv2.fisheye.initUndistortRectifyMap(
                KL, DL.flatten()[:4].reshape(4, 1), R1, P1, (w, h), cv2.CV_16SC2)
            m1R, m2R = cv2.fisheye.initUndistortRectifyMap(
                KR, DR.flatten()[:4].reshape(4, 1), R2, P2, (w, h), cv2.CV_16SC2)
        else:
            m1L, m2L = cv2.initUndistortRectifyMap(
                KL, DL.reshape(1, -1), R1, P1, (w, h), cv2.CV_16SC2)
            m1R, m2R = cv2.initUndistortRectifyMap(
                KR, DR.reshape(1, -1), R2, P2, (w, h), cv2.CV_16SC2)

        with self._lock:
            self._map_L    = (m1L, m2L)
            self._map_R    = (m1R, m2R)
            self._img_size = (w, h)
            self._maps_cam = (cam_left, cam_right)

        log(f"[StereoMatch] maps loaded  L={cam_left} R={cam_right} "
            f"lens={lens} {w}×{h}")

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_params(self) -> dict:
        with self._lock:
            if not self._cam_left or not self._cam_right:
                return {"type": "save_result", "ok": False,
                        "error": "No camera pair selected"}
            data = {
                "format_version":   1,
                "cam_left":         self._cam_left,
                "cam_right":        self._cam_right,
                "algorithm":        self._algorithm,
                "num_disparities":  self._num_disparities,
                "block_size":       self._block_size,
                "p1":               self._p1,
                "p2":               self._p2,
                "uniqueness_ratio": self._uniqueness_ratio,
                "speckle_window":   self._speckle_window,
                "speckle_range":    self._speckle_range,
                "saved_at":         datetime.now().isoformat(timespec="seconds"),
            }
            cam_L, cam_R = self._cam_left, self._cam_right

        os.makedirs(MATCH_DIR, exist_ok=True)
        path = self._match_path(cam_L, cam_R)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log(f"[StereoMatch] saved  L={cam_L} R={cam_R}")
            return {"type": "save_result", "ok": True, "saved_at": data["saved_at"]}
        except Exception as e:
            return {"type": "save_result", "ok": False, "error": str(e)}

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _safe(self, s: str) -> str:
        return s.replace("/", "_").replace(":", "_").replace(" ", "_")

    def _match_path(self, cam_left: str, cam_right: str) -> str:
        return os.path.join(MATCH_DIR,
                            f"{self._safe(cam_left)}__{self._safe(cam_right)}.json")

    def _rectify_cal_path(self, cam_left: str, cam_right: str) -> str:
        return os.path.join(RECTIFY_DIR,
                            f"{self._safe(cam_left)}__{self._safe(cam_right)}.json")

    def _stereo_cal_path(self, cam_left: str, cam_right: str) -> str:
        return os.path.join(STEREO_DIR,
                            f"{self._safe(cam_left)}__{self._safe(cam_right)}.json")

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

    def _load_stereo_cal(self, cam_left: str, cam_right: str) -> Optional[dict]:
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

    def _load_match_cal(self, cam_left: str, cam_right: str) -> Optional[dict]:
        if not cam_left or not cam_right:
            return None
        try:
            with open(self._match_path(cam_left, cam_right), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
