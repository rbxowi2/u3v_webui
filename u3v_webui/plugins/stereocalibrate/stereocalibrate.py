"""plugins/stereocalibrate/stereocalibrate.py — StereoCalibrate plugin (1.0.1)

Stereo camera calibration using per-camera LensCalibrate JSON as intrinsics.
Supports normal (pinhole) and fisheye lens models.

Workflow:
  1. Sidebar: select L/R camera, lens type, board settings → click "Calibrate"
  2. Modal: detect chessboard on both cameras simultaneously; auto-capture
     when both detect the board; RMS filter keeps quality shots.
  3. Save → captures/stereo/<safe_L>__<safe_R>.json

Output JSON: R, T, E, F, lens_type, cam_left, cam_right, rms, shot_count,
             KL, DL, KR, DR, image_size, calibrated_at.
"""

import io
import json
import math
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

CALIB_DIR       = os.path.join(CAPTURE_DIR, "calibration")
STEREO_DIR      = os.path.join(CAPTURE_DIR, "stereo")
SAVE_MIN_SHOTS  = 5      # minimum accepted shots to save
CAL_MIN_SHOTS   = 4      # minimum shots before running stereoCalibrate in worker
MAX_CAL_SHOTS   = 30     # max shots kept; oldest dropped when exceeded
AUTO_COOLDOWN   = 2.0    # seconds between auto-captures


class StereoCalibrate(PluginBase):

    _instances: dict = {}   # cam_id → instance (for HTTP snapshot route)

    def __init__(self):
        self._sio        = None
        self._state      = None
        self._emit_state = None
        self._cam_id     = ""
        self._lock       = threading.Lock()

        # Sidebar settings
        self._cam_left    = ""
        self._cam_right   = ""
        self._lens_type   = "normal"
        self._board_cols  = 9
        self._board_rows  = 6
        self._square_size = 25.0

        # Session state
        self._session_active  = False
        self._auto_enabled    = False
        self._shots_obj: list  = []
        self._shots_img_L: list = []
        self._shots_img_R: list = []
        self._shot_centers: list = []
        self._img_size: Optional[tuple] = None
        self._rms   = float('inf')
        self._R: Optional[np.ndarray] = None
        self._T: Optional[np.ndarray] = None
        self._E: Optional[np.ndarray] = None
        self._F: Optional[np.ndarray] = None
        self._KL: Optional[np.ndarray] = None
        self._DL: Optional[np.ndarray] = None
        self._KR: Optional[np.ndarray] = None
        self._DR: Optional[np.ndarray] = None
        self._accepted = 0
        self._rejected = 0

        # Auto-capture guards
        self._last_capture_t = 0.0

        # Object points template cache
        self._obj_tmpl_cache: Optional[np.ndarray] = None
        self._obj_tmpl_key: tuple = (0, 0, 0.0)

        # Stereo calibration result cache
        self._cal_cache: Optional[dict] = None
        self._cal_cache_mtime: float    = -1.0

        # Threads
        self._cal_thread: Optional[threading.Thread]    = None
        self._detect_stop   = threading.Event()
        self._detect_thread: Optional[threading.Thread] = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:    return "StereoCalibrate"

    @property
    def version(self) -> str: return "1.0.1"

    @property
    def description(self) -> str:
        return "Stereo camera calibration (normal and fisheye)"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(STEREO_DIR, exist_ok=True)
        os.makedirs(CALIB_DIR,  exist_ok=True)

    def on_unload(self):
        self._stop_detect()

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        StereoCalibrate._instances[cam_id] = self

    def on_camera_close(self, cam_id: str = ""):
        self._stop_detect()
        with self._lock:
            self._session_active = False
        StereoCalibrate._instances.pop(cam_id, None)

    def is_busy(self, cam_id: str = "") -> bool:
        return self._session_active

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        cal = self._load_stereo_cal(self._cam_left, self._cam_right)
        with self._lock:
            active   = self._session_active
            accepted = self._accepted
        return {
            "stereo_cam_left":      self._cam_left,
            "stereo_cam_right":     self._cam_right,
            "stereo_lens_type":     self._lens_type,
            "stereo_board_cols":    self._board_cols,
            "stereo_board_rows":    self._board_rows,
            "stereo_square_size":   self._square_size,
            "stereo_has_data":      cal is not None,
            "stereo_rms":           cal["rms"]           if cal else None,
            "stereo_calibrated_at": cal["calibrated_at"] if cal else None,
            "stereo_shot_count":    cal["shot_count"]    if cal else 0,
            "stereo_session_active": active,
            "stereo_session_shots":  accepted,
        }

    # ── Params ────────────────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == "stereo_cam_left":
            with self._lock:
                self._cam_left = str(value)
            if self._emit_state:
                self._emit_state()
            return True
        if key == "stereo_cam_right":
            with self._lock:
                self._cam_right = str(value)
            if self._emit_state:
                self._emit_state()
            return True
        if key == "stereo_lens_type":
            v = str(value)
            with self._lock:
                self._lens_type = v if v in ("normal", "fisheye") else "normal"
            return True
        if key == "stereo_board_cols":
            with self._lock:
                self._board_cols = max(3, int(value))
            return True
        if key == "stereo_board_rows":
            with self._lock:
                self._board_rows = max(3, int(value))
            return True
        if key == "stereo_square_size":
            with self._lock:
                self._square_size = max(0.1, float(value))
            return True
        return False

    # ── Routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        from flask import request as _req, send_file

        is_admin   = ctx["is_admin"]
        emit_state = ctx["emit_state"]

        @app.route("/plugin/stereocalibrate/snapshot")
        def _stereo_snapshot():
            cam_id = _req.args.get("cam_id", "")
            # Any instance's _state can fetch any camera's frame
            inst = StereoCalibrate._instances.get(cam_id)
            if inst is None or inst._state is None:
                inst = next((i for i in StereoCalibrate._instances.values()
                             if i._state is not None), None)
            if inst is None or inst._state is None:
                return "Not found", 404
            frame = inst._state.get_latest_frame(cam_id)
            if frame is None:
                return "No frame", 404
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                return "Encode error", 500
            return send_file(io.BytesIO(buf.tobytes()), mimetype="image/jpeg")

        @sio.on("stereo_start")
        def _stereo_start(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = StereoCalibrate._instances.get(cam_id)
            if inst is None:
                sio.emit("stereo_event",
                         {"type": "error", "msg": "StereoCalibrate not active for this camera"},
                         to=_req.sid)
                return

            cam_left  = data.get("cam_left",  inst._cam_left)
            cam_right = data.get("cam_right", inst._cam_right)
            lens_type = data.get("lens_type", inst._lens_type)

            with inst._lock:
                inst._cam_left  = cam_left
                inst._cam_right = cam_right
                inst._lens_type = lens_type

            # Load lens calibrations
            KL, DL = inst._load_lens_cal(cam_left,  lens_type)
            KR, DR = inst._load_lens_cal(cam_right, lens_type)
            with inst._lock:
                inst._KL = KL;  inst._DL = DL
                inst._KR = KR;  inst._DR = DR

            missing = []
            if KL is None: missing.append(f"L ({cam_left})")
            if KR is None: missing.append(f"R ({cam_right})")

            inst._begin_session()

            cal = inst._load_stereo_cal(cam_left, cam_right)
            sio.emit("stereo_event", {
                "type":       "started",
                "has_data":   cal is not None,
                "has_KL":     KL is not None,
                "has_KR":     KR is not None,
                "missing_cal": missing,
                "cam_left":   cam_left,
                "cam_right":  cam_right,
                "lens_type":  lens_type,
            }, to=_req.sid)
            emit_state()

        @sio.on("stereo_toggle_auto")
        def _stereo_toggle_auto(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = StereoCalibrate._instances.get(cam_id)
            if inst is None: return
            with inst._lock:
                inst._auto_enabled = not inst._auto_enabled
                state = inst._auto_enabled
            sio.emit("stereo_auto_toggled",
                     {"cam_id": cam_id, "enabled": state}, to=_req.sid)

        @sio.on("stereo_reset")
        def _stereo_reset(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = StereoCalibrate._instances.get(cam_id)
            if inst is None: return
            inst._reset_shots()
            sio.emit("stereo_auto_status", {
                "cam_id": cam_id, "action": "reset",
                "rms": None, "old_rms": None,
                "accepted": 0, "rejected": 0,
                "shot_centers": [], "reason": "",
            })

        @sio.on("stereo_remove_shot")
        def _stereo_remove_shot(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = StereoCalibrate._instances.get(cam_id)
            if inst is None: return
            result = inst._remove_last_shot()
            if result is None:
                sio.emit("stereo_event",
                         {"type": "error", "msg": "No shots to remove"}, to=_req.sid)
                return
            sio.emit("stereo_auto_status", dict(result, cam_id=cam_id, action="remove"))

        @sio.on("stereo_save")
        def _stereo_save(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = StereoCalibrate._instances.get(cam_id)
            if inst is None: return
            result = inst._save_calibration()
            sio.emit("stereo_event", result, to=_req.sid)
            emit_state()

        @sio.on("stereo_cancel")
        def _stereo_cancel(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = StereoCalibrate._instances.get(cam_id)
            if inst: inst._end_session()
            emit_state()

    # ── Session ───────────────────────────────────────────────────────────────

    def _begin_session(self):
        with self._lock:
            self._session_active  = True
            self._auto_enabled    = False
            self._shots_obj       = []
            self._shots_img_L     = []
            self._shots_img_R     = []
            self._shot_centers    = []
            self._img_size        = None
            self._rms             = float('inf')
            self._R = None;  self._T = None
            self._E = None;  self._F = None
            self._accepted        = 0
            self._rejected        = 0
            self._last_capture_t  = 0.0
        self._start_detect()
        log(f"[StereoCalibrate] Session started [{self._cam_id}]  L={self._cam_left} R={self._cam_right}")

    def _end_session(self):
        self._stop_detect()
        with self._lock:
            self._session_active = False
        log(f"[StereoCalibrate] Session ended [{self._cam_id}]")

    def _reset_shots(self):
        with self._lock:
            self._shots_obj    = []
            self._shots_img_L  = []
            self._shots_img_R  = []
            self._shot_centers = []
            self._img_size     = None
            self._rms          = float('inf')
            self._R = None;  self._T = None
            self._E = None;  self._F = None
            self._accepted       = 0
            self._rejected       = 0
            self._last_capture_t = 0.0
            self._auto_enabled   = False
        log(f"[StereoCalibrate] Reset [{self._cam_id}]")

    def _remove_last_shot(self) -> Optional[dict]:
        with self._lock:
            if not self._shots_obj:
                return None
            self._shots_obj.pop()
            self._shots_img_L.pop()
            self._shots_img_R.pop()
            self._shot_centers.pop()
            self._accepted = max(0, self._accepted - 1)
            remaining  = self._accepted
            rej        = self._rejected
            img_size   = self._img_size
            lens       = self._lens_type
            obj_pts    = list(self._shots_obj)
            img_pts_L  = list(self._shots_img_L)
            img_pts_R  = list(self._shots_img_R)
            centers    = list(self._shot_centers)
            KL = self._KL.copy() if self._KL is not None else None
            DL = self._DL.copy() if self._DL is not None else None
            KR = self._KR.copy() if self._KR is not None else None
            DR = self._DR.copy() if self._DR is not None else None

        if remaining < CAL_MIN_SHOTS:
            with self._lock:
                self._R = None;  self._T = None
                self._E = None;  self._F = None
                self._rms = float('inf')
            return {"accepted": remaining, "rejected": rej, "rms": None,
                    "old_rms": None, "shot_centers": centers, "reason": "shot removed"}

        try:
            w, h = img_size
            rms, R, T, E, F = self._run_stereo_calibrate(
                obj_pts, img_pts_L, img_pts_R, w, h, lens, KL, DL, KR, DR)
            with self._lock:
                self._R = R;  self._T = T
                self._E = E;  self._F = F
                self._rms = rms
            return {"accepted": remaining, "rejected": rej,
                    "rms": round(rms, 4), "old_rms": None,
                    "shot_centers": centers, "reason": "shot removed"}
        except Exception as e:
            log(f"[StereoCalibrate] re-calibrate after remove failed: {e}")
            with self._lock:
                self._R = None;  self._T = None
                self._E = None;  self._F = None
                self._rms = float('inf')
            return {"accepted": remaining, "rejected": rej, "rms": None,
                    "old_rms": None, "shot_centers": centers,
                    "reason": "re-calibration failed after remove"}

    # ── Detection thread ──────────────────────────────────────────────────────

    def _start_detect(self):
        self._stop_detect()
        self._detect_stop.clear()
        t = threading.Thread(target=self._detect_loop, daemon=True,
                             name=f"stereo-detect-{self._cam_id}")
        self._detect_thread = t
        t.start()

    def _stop_detect(self):
        self._detect_stop.set()
        t = self._detect_thread
        self._detect_thread = None
        if t and t.is_alive():
            t.join(timeout=1.5)

    def _detect_loop(self):
        while not self._detect_stop.is_set():
            self._detect_stop.wait(0.4)
            if self._detect_stop.is_set():
                break

            with self._lock:
                if not self._session_active:
                    break
                cols    = self._board_cols
                rows    = self._board_rows
                cam_L   = self._cam_left
                cam_R   = self._cam_right

            if self._state is None or self._sio is None:
                continue

            frame_L = self._state.get_latest_frame(cam_L) if cam_L else None
            frame_R = self._state.get_latest_frame(cam_R) if cam_R else None

            found_L, corners_L, fw_L, fh_L = self._detect_board(frame_L, cols, rows)
            found_R, corners_R, fw_R, fh_R = self._detect_board(frame_R, cols, rows)
            frame_L = frame_R = None   # release frame buffers after board detection

            corners_norm_L = None
            corners_norm_R = None
            if found_L and corners_L is not None:
                corners_norm_L = [[float(c[0][0]) / fw_L, float(c[0][1]) / fh_L]
                                  for c in corners_L]
            if found_R and corners_R is not None:
                corners_norm_R = [[float(c[0][0]) / fw_R, float(c[0][1]) / fh_R]
                                  for c in corners_R]

            auto_triggered = False
            if found_L and found_R:
                auto_triggered = self._try_auto_capture(
                    corners_L, corners_R, cols, rows, fw_L, fh_L)

            self._sio.emit("stereo_detect", {
                "cam_id":    self._cam_id,
                "found_L":   found_L,
                "found_R":   found_R,
                "corners_L": corners_norm_L,
                "corners_R": corners_norm_R,
                "cols":      cols,
                "rows":      rows,
                "auto_triggered": auto_triggered,
            })

    def _detect_board(self, frame, cols, rows):
        if frame is None:
            return False, None, 0, 0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        fh, fw = gray.shape[:2]
        found, corners = cv2.findChessboardCorners(
            gray, (cols, rows),
            flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE,
        )
        if found:
            crit    = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
            corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), crit)
        return found, corners, fw, fh

    # ── Auto-capture ──────────────────────────────────────────────────────────

    def _try_auto_capture(self, corners_L, corners_R, cols, rows, fw, fh) -> bool:
        with self._lock:
            if not self._auto_enabled:
                return False
            last_t = self._last_capture_t
            img_sz = self._img_size
            sq     = self._square_size
            lens   = self._lens_type
            KL = self._KL.copy() if self._KL is not None else None
            DL = self._DL.copy() if self._DL is not None else None
            KR = self._KR.copy() if self._KR is not None else None
            DR = self._DR.copy() if self._DR is not None else None

        if time.time() - last_t < AUTO_COOLDOWN:
            return False
        if self._cal_thread and self._cal_thread.is_alive():
            return False
        if img_sz is not None and img_sz != (fw, fh):
            return False

        xs   = corners_L[:, 0, 0];  ys = corners_L[:, 0, 1]
        cx_n = float(xs.mean()) / fw
        cy_n = float(ys.mean()) / fh

        key = (cols, rows, sq)
        if self._obj_tmpl_key != key or self._obj_tmpl_cache is None:
            tmpl = np.zeros((rows * cols, 3), np.float32)
            tmpl[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * sq
            self._obj_tmpl_cache = tmpl
            self._obj_tmpl_key   = key
        obj_pts = self._obj_tmpl_cache

        with self._lock:
            self._last_capture_t = time.time()

        t = threading.Thread(
            target=self._calibration_worker,
            args=(obj_pts, corners_L.copy(), corners_R.copy(),
                  (fw, fh), cx_n, cy_n, lens, KL, DL, KR, DR),
            daemon=True, name=f"stereo-cal-{self._cam_id}",
        )
        self._cal_thread = t
        t.start()
        return True

    # ── Calibration worker ────────────────────────────────────────────────────

    def _calibration_worker(self, new_obj, new_img_L, new_img_R,
                             img_size, cx, cy, lens_type, KL, DL, KR, DR):
        with self._lock:
            # Slice directly to avoid copying the full history before trimming
            obj_pts   = self._shots_obj[-(MAX_CAL_SHOTS - 1):]   + [new_obj]
            img_pts_L = self._shots_img_L[-(MAX_CAL_SHOTS - 1):] + [new_img_L]
            img_pts_R = self._shots_img_R[-(MAX_CAL_SHOTS - 1):] + [new_img_R]
            old_rms   = self._rms
            accepted  = self._accepted

        w, h = img_size

        if len(obj_pts) < CAL_MIN_SHOTS:
            # Accept shot unconditionally; not enough for calibration yet
            with self._lock:
                self._shots_obj.append(new_obj)
                self._shots_img_L.append(new_img_L)
                self._shots_img_R.append(new_img_R)
                self._shot_centers.append((cx, cy))
                if len(self._shots_obj) > MAX_CAL_SHOTS:
                    del self._shots_obj[:-MAX_CAL_SHOTS]
                    del self._shots_img_L[:-MAX_CAL_SHOTS]
                    del self._shots_img_R[:-MAX_CAL_SHOTS]
                    del self._shot_centers[:-MAX_CAL_SHOTS]
                if self._img_size is None:
                    self._img_size = img_size
                self._accepted += 1
                acc     = self._accepted
                rej     = self._rejected
                centers = list(self._shot_centers)
            self._emit_status("accepted", rms=None, old_rms=None,
                              accepted=acc, rejected=rej, shot_centers=centers,
                              reason=f"collecting (need {CAL_MIN_SHOTS})")
            return

        try:
            rms, R, T, E, F = self._run_stereo_calibrate(
                obj_pts, img_pts_L, img_pts_R, w, h, lens_type, KL, DL, KR, DR)
        except Exception as e:
            log(f"[StereoCalibrate] worker error: {e}")
            with self._lock:
                self._rejected += 1
                rej     = self._rejected
                acc     = self._accepted
                centers = list(self._shot_centers)
                cur_rms = self._rms if math.isfinite(self._rms) else None
            self._emit_status("rejected", rms=cur_rms, old_rms=None,
                              accepted=acc, rejected=rej, shot_centers=centers,
                              reason=f"calibration error: {e}")
            return

        if math.isfinite(old_rms) and rms >= old_rms:
            with self._lock:
                self._rejected += 1
                rej     = self._rejected
                acc     = self._accepted
                centers = list(self._shot_centers)
            self._emit_status("rejected",
                              rms=round(old_rms, 4), old_rms=round(old_rms, 4),
                              accepted=acc, rejected=rej, shot_centers=centers,
                              reason="RMS did not improve")
            return

        with self._lock:
            self._shots_obj.append(new_obj)
            self._shots_img_L.append(new_img_L)
            self._shots_img_R.append(new_img_R)
            self._shot_centers.append((cx, cy))
            # Keep only the most recent MAX_CAL_SHOTS shots
            if len(self._shots_obj) > MAX_CAL_SHOTS:
                del self._shots_obj[:-MAX_CAL_SHOTS]
                del self._shots_img_L[:-MAX_CAL_SHOTS]
                del self._shots_img_R[:-MAX_CAL_SHOTS]
                del self._shot_centers[:-MAX_CAL_SHOTS]
            if self._img_size is None:
                self._img_size = img_size
            self._rms = rms
            self._R = R;  self._T = T
            self._E = E;  self._F = F
            self._accepted += 1
            acc     = self._accepted
            rej     = self._rejected
            centers = list(self._shot_centers)

        log(f"[StereoCalibrate] accepted shot {acc}, rms={rms:.4f} [{self._cam_id}]")
        self._emit_status("accepted",
                          rms=round(rms, 4),
                          old_rms=round(old_rms, 4) if math.isfinite(old_rms) else None,
                          accepted=acc, rejected=rej, shot_centers=centers, reason="")

    # ── CV stereo calibration ─────────────────────────────────────────────────

    def _run_stereo_calibrate(self, obj_pts, img_pts_L, img_pts_R,
                               w, h, lens_type, KL, DL, KR, DR):
        if KL is None or KR is None:
            raise RuntimeError("Missing intrinsic calibration for one or both cameras")

        if lens_type == "fisheye":
            obj_f  = [o.reshape(-1, 1, 3).astype(np.float64) for o in obj_pts]
            img_fL = [i.reshape(-1, 1, 2).astype(np.float64) for i in img_pts_L]
            img_fR = [i.reshape(-1, 1, 2).astype(np.float64) for i in img_pts_R]

            KL_ = KL.copy();  DL_ = DL.reshape(-1, 1).astype(np.float64)
            KR_ = KR.copy();  DR_ = DR.reshape(-1, 1).astype(np.float64)
            flags = (cv2.fisheye.CALIB_FIX_INTRINSIC |
                     cv2.fisheye.CALIB_FIX_SKEW)
            R_out = np.eye(3, dtype=np.float64)
            T_out = np.zeros((3, 1), dtype=np.float64)
            # Use *_ to absorb any extra return values added in newer OpenCV versions
            rms, KL_o, DL_o, KR_o, DR_o, R_out, T_out, *_ = cv2.fisheye.stereoCalibrate(
                obj_f, img_fL, img_fR, KL_, DL_, KR_, DR_,
                (w, h), R_out, T_out, flags=flags,
            )
            # fisheye.stereoCalibrate does not return E, F — compute manually
            T_flat = T_out.flatten()
            T_skew = np.array([
                [       0.0,  -T_flat[2],  T_flat[1]],
                [ T_flat[2],         0.0, -T_flat[0]],
                [-T_flat[1],   T_flat[0],        0.0],
            ], dtype=np.float64)
            E_out = T_skew @ R_out
            F_out = np.linalg.inv(KR_o).T @ E_out @ np.linalg.inv(KL_o)
            return rms, R_out, T_out, E_out, F_out
        else:
            # Pre-undistort image points ourselves to avoid the internal
            # undistortPoints NaN→assertion failure in cv2.stereoCalibrate
            def _ud(pts_list, K, D):
                D_flat = D.flatten().astype(np.float64)
                return [
                    cv2.undistortPoints(
                        p.reshape(-1, 1, 2).astype(np.float64),
                        K, D_flat, None, K,
                    ).astype(np.float32)
                    for p in pts_list
                ]

            pts_L = _ud(img_pts_L, KL, DL)
            pts_R = _ud(img_pts_R, KR, DR)

            for pts in pts_L + pts_R:
                if not np.isfinite(pts).all():
                    raise RuntimeError(
                        "Undistorted points contain NaN/Inf — "
                        "lens calibration may be wrong or resolution mismatch")

            D_zero = np.zeros((1, 5), dtype=np.float64)
            # Use *_ to absorb any extra return values in newer OpenCV builds
            rms, KL_o, DL_o, KR_o, DR_o, R_out, T_out, E_out, F_out, *_ = \
                cv2.stereoCalibrate(
                    obj_pts, pts_L, pts_R,
                    KL.copy(), D_zero, KR.copy(), D_zero,
                    (w, h), flags=cv2.CALIB_FIX_INTRINSIC,
                )
            return rms, R_out, T_out, E_out, F_out

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_calibration(self) -> dict:
        with self._lock:
            if self._accepted < SAVE_MIN_SHOTS:
                return {"type": "save_result", "ok": False,
                        "error": f"Need at least {SAVE_MIN_SHOTS} accepted shots (have {self._accepted})"}
            if self._R is None:
                return {"type": "save_result", "ok": False, "error": "No calibration data yet"}
            rms      = self._rms
            R        = self._R.tolist()
            T        = self._T.tolist()
            E        = self._E.tolist()
            F        = self._F.tolist()
            KL       = self._KL.tolist()
            DL       = self._DL.flatten().tolist()
            KR       = self._KR.tolist()
            DR       = self._DR.flatten().tolist()
            img_size = self._img_size
            accepted = self._accepted
            cols     = self._board_cols
            rows     = self._board_rows
            sq       = self._square_size
            cam_L    = self._cam_left
            cam_R    = self._cam_right
            lens     = self._lens_type

        w, h = img_size
        cal_data = {
            "format_version": 1,
            "lens_type":      lens,
            "cam_left":       cam_L,
            "cam_right":      cam_R,
            "R":              R,
            "T":              T,
            "E":              E,
            "F":              F,
            "KL":             KL,
            "DL":             DL,
            "KR":             KR,
            "DR":             DR,
            "image_size":     [w, h],
            "rms":            float(rms),
            "calibrated_at":  datetime.now().isoformat(timespec="seconds"),
            "shot_count":     accepted,
            "board_cols":     int(cols),
            "board_rows":     int(rows),
            "square_size":    float(sq),
        }
        self._save_stereo_cal(cam_L, cam_R, cal_data)
        self._end_session()
        log(f"[StereoCalibrate] Saved L={cam_L} R={cam_R} lens={lens} rms={rms:.4f} shots={accepted}")
        return {"type": "save_result", "ok": True,
                "rms": round(float(rms), 4), "shot_count": accepted,
                "image_size": [w, h], "lens_type": lens}

    # ── Status broadcast ──────────────────────────────────────────────────────

    def _emit_status(self, action, *, rms, old_rms, accepted, rejected,
                     shot_centers, reason=""):
        if self._sio is None:
            return
        self._sio.emit("stereo_auto_status", {
            "cam_id":       self._cam_id,
            "action":       action,
            "rms":          rms,
            "old_rms":      old_rms,
            "accepted":     accepted,
            "rejected":     rejected,
            "shot_centers": shot_centers,
            "reason":       reason,
        })

    # ── Lens cal loader ───────────────────────────────────────────────────────

    def _load_lens_cal(self, cam_id: str, lens_type: str):
        """Return (K, D) np arrays from LensCalibrate JSON, or (None, None)."""
        if not cam_id:
            return None, None
        safe = cam_id.replace("/", "_").replace(":", "_").replace(" ", "_")
        path = os.path.join(CALIB_DIR, f"{safe}.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            saved_lens = data.get("lens_type", "normal")
            if saved_lens != lens_type:
                log(f"[StereoCalibrate] lens type mismatch for {cam_id}: "
                    f"saved={saved_lens} requested={lens_type}")
                return None, None
            K = np.array(data["camera_matrix"], dtype=np.float64)
            D = np.array(data["dist_coeffs"], dtype=np.float64).reshape(-1, 1)
            return K, D
        except Exception:
            return None, None

    # ── Stereo cal persistence ────────────────────────────────────────────────

    def _stereo_cal_path(self, cam_left: str, cam_right: str) -> str:
        os.makedirs(STEREO_DIR, exist_ok=True)
        def safe(s): return s.replace("/", "_").replace(":", "_").replace(" ", "_")
        return os.path.join(STEREO_DIR, f"{safe(cam_left)}__{safe(cam_right)}.json")

    def _load_stereo_cal(self, cam_left: str, cam_right: str) -> Optional[dict]:
        if not cam_left or not cam_right:
            return None
        path = self._stereo_cal_path(cam_left, cam_right)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        if mtime == self._cal_cache_mtime and self._cal_cache is not None:
            return self._cal_cache
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._cal_cache       = data
            self._cal_cache_mtime = mtime
            return data
        except Exception:
            return None

    def _save_stereo_cal(self, cam_left: str, cam_right: str, data: dict):
        path = self._stereo_cal_path(cam_left, cam_right)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._cal_cache       = data
            self._cal_cache_mtime = os.path.getmtime(path)
        except Exception as e:
            log(f"[StereoCalibrate] save error: {e}")
