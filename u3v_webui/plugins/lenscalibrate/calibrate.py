"""plugins/lenscalibrate/calibrate.py — LensCalibrate plugin (2.3.0)

Two-stage calibration for fisheye lenses:
  Stage 1 (fisheye, D fixed): cv2.fisheye.calibrate with D=[0,0,0,0] fixed.
           Classic corner detection. Collect shots until K converges, then "Enter Stage 2".
  Stage 2 (fisheye, D free): cv2.fisheye.calibrate with D free to update.
           Uses Stage 1 K as intrinsic guess; D₀ = [0,0,0,0].
Normal lenses use Stage 1 only (pinhole, no Stage 2). Classic detection only.
Reset always returns to Stage 1.

- v2.1.2: robust circle detection (Otsu + conncomp + fill); balance=1.0 preview maps.
- v2.1.3–2.1.14: see git history.
- v2.1.13: Remove perspective-score gate; auto-capture triggers on board detection only.
- v2.1.14: Fisheye session defaults to Classic detection.
- v2.2.0: Remove K₀ image-circle section; two-stage fisheye calibration.
          Always preload K on session open; D only if lens type matches saved.
- v2.3.0: Stage 1 fisheye uses fisheye model with D fixed at 0 (was pinhole).
          Remove SB detection option; Classic detection only for all cases.
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

CALIB_DIR               = os.path.join(CAPTURE_DIR, "calibration")
SAVE_MIN_SHOTS          = 3      # minimum accepted shots required to save
AUTO_COOLDOWN           = 1.5
MAX_CAL_SHOTS           = 30     # max shots fed to calibrate(); oldest dropped when exceeded

_EYE3 = np.eye(3, dtype=np.float64)


class LensCalibrate(PluginBase):

    _instances: dict = {}

    def __init__(self):
        self._sio        = None
        self._state      = None
        self._emit_state = None
        self._cam_id     = ""
        self._lock       = threading.Lock()

        # Sidebar settings
        self._lens_type   = "normal"
        self._board_cols  = 9
        self._board_rows  = 6
        self._square_size = 25.0

        # Session state
        self._session_active   = False
        self._auto_enabled     = False
        self._shots_obj: list         = []
        self._shots_img: list         = []
        self._shot_centers: list      = []
        self._img_size: Optional[tuple] = None
        self._rms      = float('inf')
        self._K: Optional[np.ndarray]  = None
        self._D: Optional[np.ndarray]  = None
        self._maps: Optional[tuple]    = None
        self._maps_size: Optional[tuple] = None
        self._accepted = 0
        self._rejected = 0

        # Two-stage state
        self._stage    = 1
        self._stage1_K: Optional[np.ndarray] = None   # K from Stage 1, K₀ for Stage 2

        # Auto-capture guards
        self._last_capture_t   = 0.0

        # Caches
        self._K_version        = 0
        self._new_K_cache: Optional[np.ndarray] = None
        self._new_K_ver: int   = -1
        self._new_K_size: Optional[tuple] = None
        self._obj_tmpl_cache: Optional[np.ndarray] = None
        self._obj_tmpl_key: tuple = (0, 0, 0.0)

        # Calibration file cache
        self._cal_cache: Optional[dict]  = None
        self._cal_cache_mtime: float     = -1.0

        # Threads
        self._cal_thread: Optional[threading.Thread] = None
        self._detect_stop   = threading.Event()
        self._detect_thread: Optional[threading.Thread] = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:    return "LensCalibrate"

    @property
    def version(self) -> str: return "2.3.0"

    @property
    def description(self) -> str:
        return "Lens distortion calibration — normal (pinhole) and fisheye"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(CALIB_DIR, exist_ok=True)

    def on_unload(self):
        self._stop_detect()

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        LensCalibrate._instances[cam_id] = self

    def on_camera_close(self, cam_id: str = ""):
        self._stop_detect()
        with self._lock:
            self._session_active = False
        LensCalibrate._instances.pop(cam_id, None)

    def is_busy(self, cam_id: str = "") -> bool:
        return self._session_active

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        cal = self._load_cal(self._cam_id)
        with self._lock:
            active   = self._session_active
            accepted = self._accepted
            stage    = self._stage
        return {
            "calib_lens_type":      self._lens_type,
            "calib_board_cols":     self._board_cols,
            "calib_board_rows":     self._board_rows,
            "calib_square_size":    self._square_size,
            "calib_has_data":       cal is not None,
            "calib_rms":            cal["rms"]           if cal else None,
            "calib_calibrated_at":  cal["calibrated_at"] if cal else None,
            "calib_shot_count":     cal["shot_count"]    if cal else 0,
            "calib_k0_only":        cal.get("k0_only", False) if cal else False,
            "calib_session_active": active,
            "calib_session_shots":  accepted,
            "calib_stage":          stage,
        }

    # ── Params ────────────────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == "calib_lens_type":
            v = str(value)
            with self._lock:
                self._lens_type = v if v in ("normal", "fisheye") else "normal"
            return True
        if key == "calib_board_cols":
            with self._lock:
                self._board_cols = max(3, int(value))
            return True
        if key == "calib_board_rows":
            with self._lock:
                self._board_rows = max(3, int(value))
            return True
        if key == "calib_square_size":
            with self._lock:
                self._square_size = max(0.1, float(value))
            return True
        return False

    # ── Routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        from flask import request as _req, send_file

        is_admin   = ctx["is_admin"]
        emit_state = ctx["emit_state"]

        @app.route("/plugin/lenscalibrate/snapshot")
        def _calib_snapshot():
            cam_id    = _req.args.get("cam_id", "")
            corrected = _req.args.get("corrected", "0") == "1"
            inst = LensCalibrate._instances.get(cam_id)
            if inst is None or inst._state is None:
                return "Not found", 404
            frame = inst._state.get_latest_frame(cam_id)
            if frame is None:
                return "No frame", 404
            if corrected:
                frame = inst._apply_correction(frame)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                return "Encode error", 500
            return send_file(io.BytesIO(buf.tobytes()), mimetype="image/jpeg")

        @sio.on("calib_start")
        def _calib_start(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = LensCalibrate._instances.get(cam_id)
            if inst is None:
                sio.emit("calib_event",
                         {"type": "error", "msg": "LensCalibrate not active for this camera"},
                         to=_req.sid)
                return
            inst._begin_session()
            cal = inst._load_cal(cam_id)
            event_data: dict = {"type": "started", "has_data": cal is not None}

            # Always preload K if saved data exists;
            # build maps + preload D only when saved lens type matches current.
            if cal and "camera_matrix" in cal:
                saved_lens = cal.get("lens_type", "normal")
                event_data["preloaded_K"] = cal["camera_matrix"]
                if saved_lens == inst._lens_type and inst._state is not None:
                    try:
                        K_s = np.array(cal["camera_matrix"], dtype=np.float64)
                        D_s = np.array(cal["dist_coeffs"], dtype=np.float64).reshape(-1, 1)
                        frame0 = inst._state.get_latest_frame(cam_id)
                        if frame0 is not None:
                            fh0, fw0 = frame0.shape[:2]
                            maps0 = inst._rebuild_maps(K_s, D_s, (fw0, fh0), saved_lens)
                            with inst._lock:
                                inst._maps      = maps0
                                inst._maps_size = (fw0, fh0)
                                if inst._img_size is None:
                                    inst._img_size = (fw0, fh0)
                        event_data["preloaded_D"] = cal["dist_coeffs"]
                    except Exception as e:
                        log(f"[LensCalibrate] preload K/D failed: {e}")

            sio.emit("calib_event", event_data, to=_req.sid)
            emit_state()

        @sio.on("calib_enter_stage2")
        def _calib_enter_stage2(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = LensCalibrate._instances.get(cam_id)
            if inst is None: return
            with inst._lock:
                if inst._K is None:
                    sio.emit("calib_event",
                             {"type": "error", "msg": "No Stage 1 calibration yet"},
                             to=_req.sid)
                    return
                stage1_K   = inst._K.copy()
                stage1_rms = inst._rms
            inst._enter_stage2(stage1_K)
            sio.emit("calib_auto_status", {
                "cam_id": cam_id, "action": "stage2",
                "rms": None,
                "old_rms": round(stage1_rms, 4) if math.isfinite(stage1_rms) else None,
                "accepted": 0, "rejected": 0,
                "shot_centers": [],
                "reason": f"Stage 1 RMS {stage1_rms:.4f}",
            })

        @sio.on("calib_skip_to_stage2")
        def _calib_skip_to_stage2(data):
            """Jump directly to Stage 2 using K from the saved calibration file."""
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = LensCalibrate._instances.get(cam_id)
            if inst is None: return
            cal = inst._load_cal(cam_id)
            if cal is None or "camera_matrix" not in cal:
                sio.emit("calib_event",
                         {"type": "error", "msg": "No saved calibration — complete Stage 1 first"},
                         to=_req.sid)
                return
            K_saved = np.array(cal["camera_matrix"], dtype=np.float64)
            inst._enter_stage2(K_saved)
            sio.emit("calib_auto_status", {
                "cam_id": cam_id, "action": "stage2",
                "rms": None, "old_rms": None,
                "accepted": 0, "rejected": 0,
                "shot_centers": [],
                "reason": "Skipped Stage 1 — using saved K",
            })

        @sio.on("calib_toggle_auto")
        def _calib_toggle_auto(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = LensCalibrate._instances.get(cam_id)
            if inst is None: return
            with inst._lock:
                inst._auto_enabled = not inst._auto_enabled
                state = inst._auto_enabled
            sio.emit("calib_auto_toggled",
                     {"cam_id": cam_id, "enabled": state}, to=_req.sid)

        @sio.on("calib_reset")
        def _calib_reset(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = LensCalibrate._instances.get(cam_id)
            if inst is None: return
            inst._reset_shots()
            sio.emit("calib_auto_status", {
                "cam_id": cam_id, "action": "reset",
                "rms": None, "old_rms": None,
                "accepted": 0, "rejected": 0,
                "shot_centers": [], "reason": "",
            })

        @sio.on("calib_remove_shot")
        def _calib_remove_shot(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = LensCalibrate._instances.get(cam_id)
            if inst is None: return
            result = inst._remove_last_shot()
            if result is None:
                sio.emit("calib_event",
                         {"type": "error", "msg": "No shots to remove"}, to=_req.sid)
                return
            sio.emit("calib_auto_status", dict(result, cam_id=cam_id, action="remove"))

        @sio.on("calib_save")
        def _calib_save(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = LensCalibrate._instances.get(cam_id)
            if inst is None: return
            result = inst._save_calibration(cam_id)
            sio.emit("calib_event", result, to=_req.sid)
            emit_state()

        @sio.on("calib_cancel")
        def _calib_cancel(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst   = LensCalibrate._instances.get(cam_id)
            if inst: inst._end_session()
            emit_state()

    # ── Session ───────────────────────────────────────────────────────────────

    def _begin_session(self):
        with self._lock:
            self._session_active  = True
            self._auto_enabled    = False
            self._shots_obj       = []
            self._shots_img       = []
            self._shot_centers    = []
            self._img_size        = None
            self._rms             = float('inf')
            self._K               = None
            self._D               = None
            self._maps            = None
            self._maps_size       = None
            self._K_version      += 1
            self._accepted        = 0
            self._rejected        = 0
            self._last_capture_t  = 0.0
            self._stage           = 1
            self._stage1_K        = None
        self._start_detect()
        log(f"[LensCalibrate] Session started [{self._cam_id}]")

    def _end_session(self):
        self._stop_detect()
        with self._lock:
            self._session_active = False
        log(f"[LensCalibrate] Session ended [{self._cam_id}]")

    def _reset_shots(self):
        """Discard all shots and return to Stage 1."""
        with self._lock:
            self._shots_obj      = []
            self._shots_img      = []
            self._shot_centers   = []
            self._img_size       = None
            self._rms            = float('inf')
            self._K              = None
            self._D              = None
            self._K_version     += 1
            self._maps           = None
            self._maps_size      = None
            self._accepted       = 0
            self._rejected       = 0
            self._last_capture_t = 0.0
            self._auto_enabled   = False
            self._stage          = 1
            self._stage1_K       = None
        log(f"[LensCalibrate] Reset to Stage 1 [{self._cam_id}]")

    def _enter_stage2(self, stage1_K: np.ndarray):
        with self._lock:
            self._stage1_K       = stage1_K
            self._stage          = 2
            self._shots_obj      = []
            self._shots_img      = []
            self._shot_centers   = []
            self._img_size       = None
            self._rms            = float('inf')
            self._K              = None
            self._D              = None
            self._K_version     += 1
            self._maps           = None
            self._maps_size      = None
            self._accepted       = 0
            self._rejected       = 0
            self._last_capture_t = 0.0
            self._auto_enabled   = False
        log(f"[LensCalibrate] Entered Stage 2 [{self._cam_id}]")

    def _remove_last_shot(self) -> Optional[dict]:
        with self._lock:
            if not self._shots_obj:
                return None
            self._shots_obj.pop()
            self._shots_img.pop()
            self._shot_centers.pop()
            self._accepted = max(0, self._accepted - 1)
            remaining = self._accepted
            rej       = self._rejected
            img_size  = self._img_size
            lens      = self._lens_type
            stage     = self._stage
            all_obj   = list(self._shots_obj)
            all_img   = list(self._shots_img)
            centers   = list(self._shot_centers)

        cal_type = lens
        fix_D    = (lens == "fisheye" and stage == 1)
        obj_pts  = all_obj[-MAX_CAL_SHOTS:]
        img_pts  = all_img[-MAX_CAL_SHOTS:]

        if remaining == 0:
            with self._lock:
                self._K = None;  self._D = None
                self._rms = float("inf")
                self._maps = None;  self._maps_size = None
                self._K_version += 1
            return {"accepted": 0, "rejected": rej, "rms": None,
                    "old_rms": None, "shot_centers": [], "reason": "shot removed"}

        try:
            w, h = img_size
            rms, K, D = self._run_cv_calibrate(obj_pts, img_pts, w, h, cal_type, fix_D=fix_D)
            maps = self._rebuild_maps(K, D, img_size, cal_type)
            with self._lock:
                self._K = K;  self._D = D
                self._rms = rms
                self._maps = maps;  self._maps_size = img_size
                self._K_version += 1
            return {"accepted": remaining, "rejected": rej,
                    "rms": round(rms, 4), "old_rms": None,
                    "shot_centers": centers, "reason": "shot removed",
                    "K": K.tolist(), "D": D.flatten().tolist()}
        except Exception as e:
            log(f"[LensCalibrate] re-calibrate after remove failed: {e}")
            with self._lock:
                self._K = None;  self._D = None
                self._rms = float("inf")
                self._maps = None;  self._maps_size = None
                self._K_version += 1
            return {"accepted": remaining, "rejected": rej, "rms": None,
                    "old_rms": None, "shot_centers": centers,
                    "reason": "re-calibration failed after remove"}

    # ── Detection thread ──────────────────────────────────────────────────────

    def _start_detect(self):
        self._stop_detect()
        self._detect_stop.clear()
        t = threading.Thread(target=self._detect_loop, daemon=True,
                             name=f"calib-detect-{self._cam_id}")
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
                K_c     = self._K.copy() if self._K is not None else None
                D_c     = self._D.copy() if self._D is not None else None
                lens_c  = self._lens_type
                stage_c = self._stage
                K_ver   = self._K_version

            if self._state is None or self._sio is None:
                continue
            frame = self._state.get_latest_frame(self._cam_id)
            if frame is None:
                continue

            gray = (cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    if len(frame.shape) == 3 else frame)
            fh, fw = gray.shape[:2]
            frame = None

            found, corners = cv2.findChessboardCorners(
                gray, (cols, rows),
                flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE,
            )
            if found:
                crit    = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
                corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), crit)
            if found:
                corners_norm = [[float(c[0][0]) / fw, float(c[0][1]) / fh]
                                for c in corners]

                auto_triggered = self._try_auto_capture(corners, cols, rows, fw, fh)

                # Compute undistorted corner positions (cache new_K across frames)
                cal_type_c = lens_c
                corners_corr = None
                if K_c is not None and D_c is not None:
                    try:
                        if (K_ver != self._new_K_ver or
                                (fw, fh) != self._new_K_size):
                            if cal_type_c == "fisheye":
                                Ks = K_c.copy()
                                Ks[0, 0] = abs(Ks[0, 0])
                                Ks[1, 1] = abs(Ks[1, 1])
                                self._new_K_cache = \
                                    cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                                        Ks, D_c, (fw, fh), _EYE3, balance=1.0)
                            else:
                                self._new_K_cache, _ = cv2.getOptimalNewCameraMatrix(
                                    K_c, D_c, (fw, fh), 1.0)
                            self._new_K_ver  = K_ver
                            self._new_K_size = (fw, fh)

                        new_K = self._new_K_cache
                        pts   = corners.reshape(-1, 1, 2).astype(np.float64)
                        if cal_type_c == "fisheye":
                            Ks = K_c.copy()
                            Ks[0, 0] = abs(Ks[0, 0])
                            Ks[1, 1] = abs(Ks[1, 1])
                            undist = cv2.fisheye.undistortPoints(
                                pts, Ks, D_c, R=_EYE3, P=new_K)
                        else:
                            undist = cv2.undistortPoints(pts, K_c, D_c, P=new_K)
                        corners_corr = [[float(c[0][0]) / fw, float(c[0][1]) / fh]
                                        for c in undist]
                    except Exception:
                        self._new_K_cache = None

                payload = {
                    "cam_id": self._cam_id, "detected": True,
                    "corners": corners_norm, "cols": cols, "rows": rows,
                    "auto_triggered": auto_triggered,
                }
                if corners_corr is not None:
                    payload["corners_corrected"] = corners_corr
                self._sio.emit("calib_detect", payload)
            else:
                self._sio.emit("calib_detect", {"cam_id": self._cam_id, "detected": False})

    # ── Auto-capture ──────────────────────────────────────────────────────────

    def _try_auto_capture(self, corners, cols, rows, fw, fh) -> bool:
        with self._lock:
            if not self._auto_enabled:
                return False
            last_t = self._last_capture_t
            img_sz = self._img_size
            sq     = self._square_size
            lens   = self._lens_type

        if time.time() - last_t < AUTO_COOLDOWN:
            return False
        if self._cal_thread and self._cal_thread.is_alive():
            return False
        if img_sz is not None and img_sz != (fw, fh):
            return False

        xs   = corners[:, 0, 0];  ys = corners[:, 0, 1]
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
            args=(obj_pts, corners.copy(), (fw, fh), cx_n, cy_n, lens),
            daemon=True, name=f"calib-cal-{self._cam_id}",
        )
        self._cal_thread = t
        t.start()
        return True

    # ── Calibration worker ────────────────────────────────────────────────────

    def _calibration_worker(self, new_obj, new_img, img_size, cx, cy, lens_type):
        with self._lock:
            all_obj  = list(self._shots_obj) + [new_obj]
            all_img  = list(self._shots_img) + [new_img]
            old_rms  = self._rms
            accepted = self._accepted
            stage    = self._stage
        obj_pts = all_obj[-MAX_CAL_SHOTS:]
        img_pts = all_img[-MAX_CAL_SHOTS:]
        w, h = img_size
        cal_type = lens_type
        fix_D    = (lens_type == "fisheye" and stage == 1)

        try:
            rms, K, D = self._run_cv_calibrate(obj_pts, img_pts, w, h, cal_type, fix_D=fix_D)
        except Exception as e:
            log(f"[LensCalibrate] cal worker error: {e}")
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

        maps = self._rebuild_maps(K, D, img_size, cal_type)

        with self._lock:
            self._shots_obj.append(new_obj)
            self._shots_img.append(new_img)
            self._shot_centers.append((cx, cy))
            if self._img_size is None:
                self._img_size = img_size
            self._rms        = rms
            self._K          = K
            self._D          = D
            self._maps       = maps
            self._maps_size  = img_size
            self._K_version += 1
            self._accepted  += 1
            acc     = self._accepted
            rej     = self._rejected
            centers = list(self._shot_centers)

        log(f"[LensCalibrate] accepted shot {acc} (stage {stage}), rms={rms:.4f} [{self._cam_id}]")
        self._emit_status("accepted",
                          rms=round(rms, 4),
                          old_rms=round(old_rms, 4) if math.isfinite(old_rms) else None,
                          accepted=acc, rejected=rej, shot_centers=centers, reason="",
                          K=K.tolist(), D=D.flatten().tolist())

    # ── CV calibration ────────────────────────────────────────────────────────

    def _run_cv_calibrate(self, obj_pts, img_pts, w, h, cal_type, fix_D=False):
        """cal_type: 'normal' (pinhole) or 'fisheye'.
        fix_D=True: fisheye Stage 1 — solve only K, D stays [0,0,0,0].
        fix_D=False: fisheye Stage 2 — solve K and D freely.
        """
        if cal_type == "fisheye":
            obj_f = [o.reshape(-1, 1, 3).astype(np.float64) for o in obj_pts]
            img_f = [i.reshape(-1, 1, 2).astype(np.float64) for i in img_pts]
            if fix_D:
                # Stage 1: fisheye model with all D coefficients frozen at 0.
                # Finds K in the correct model space without distortion interference.
                locked = (cv2.fisheye.CALIB_FIX_K1 | cv2.fisheye.CALIB_FIX_K2 |
                          cv2.fisheye.CALIB_FIX_K3 | cv2.fisheye.CALIB_FIX_K4)
                for extra in [
                    cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC | cv2.fisheye.CALIB_FIX_SKEW | locked,
                    cv2.fisheye.CALIB_FIX_SKEW | locked,
                    locked,
                ]:
                    try:
                        K_t = np.zeros((3, 3))
                        D_t = np.zeros((4, 1), dtype=np.float64)
                        r, Kt, Dt, _, _ = cv2.fisheye.calibrate(
                            obj_f, img_f, (w, h), K_t, D_t, flags=extra
                        )
                        return r, Kt, Dt
                    except cv2.error:
                        pass
                raise RuntimeError("Fisheye Stage 1 calibration failed for all flag combinations")
            else:
                # Stage 2: K₀ from Stage 1, D free to update.
                with self._lock:
                    K0 = self._stage1_K.copy() if self._stage1_K is not None else None
                guess_flag = cv2.fisheye.CALIB_USE_INTRINSIC_GUESS if K0 is not None else 0
                for extra in [
                    cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC | cv2.fisheye.CALIB_FIX_SKEW,
                    cv2.fisheye.CALIB_FIX_SKEW,
                    0,
                ]:
                    try:
                        K_t = K0.copy() if K0 is not None else np.zeros((3, 3))
                        D_t = np.zeros((4, 1), dtype=np.float64)
                        r, Kt, Dt, _, _ = cv2.fisheye.calibrate(
                            obj_f, img_f, (w, h), K_t, D_t, flags=guess_flag | extra
                        )
                        return r, Kt, Dt
                    except cv2.error:
                        pass
                raise RuntimeError("Fisheye calibration failed for all flag combinations")
        else:
            rms, K, D, _, _ = cv2.calibrateCamera(obj_pts, img_pts, (w, h), None, None)
            return rms, K, D

    # ── Undistortion maps ─────────────────────────────────────────────────────

    def _rebuild_maps(self, K, D, img_size, cal_type):
        w, h = img_size
        try:
            if cal_type == "fisheye":
                Ks = K.copy()
                Ks[0, 0] = abs(Ks[0, 0])
                Ks[1, 1] = abs(Ks[1, 1])
                new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                    Ks, D, (w, h), _EYE3, balance=1.0)
                map1, map2 = cv2.fisheye.initUndistortRectifyMap(
                    Ks, D, _EYE3, new_K, (w, h), cv2.CV_16SC2)
            else:
                new_K, _ = cv2.getOptimalNewCameraMatrix(K, D, (w, h), 1.0)
                map1, map2 = cv2.initUndistortRectifyMap(K, D, None, new_K, (w, h), cv2.CV_16SC2)
            return (map1, map2)
        except Exception as e:
            log(f"[LensCalibrate] rebuild_maps error: {e}")
            return None

    def _apply_correction(self, frame):
        with self._lock:
            maps  = self._maps
            msize = self._maps_size
        if maps is None:
            return frame
        fh, fw = frame.shape[:2]
        if (fw, fh) != msize:
            return frame
        return cv2.remap(frame, maps[0], maps[1], cv2.INTER_LINEAR)

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_calibration(self, cam_id: str) -> dict:
        with self._lock:
            lens  = self._lens_type
            stage = self._stage
            if self._accepted < SAVE_MIN_SHOTS:
                return {"type": "save_result", "ok": False,
                        "error": f"Need at least {SAVE_MIN_SHOTS} accepted shots (have {self._accepted})"}
            if self._K is None:
                return {"type": "save_result", "ok": False, "error": "No calibration data yet"}
            rms = self._rms;  K = self._K.tolist();  D = self._D.flatten().tolist()
            img_size = self._img_size;  accepted = self._accepted
            cols = self._board_cols;  rows = self._board_rows;  sq = self._square_size

        w, h = img_size
        cal_data = {
            "format_version": 1, "lens_type": lens,
            "camera_matrix":  K, "dist_coeffs": D,
            "image_size":     [w, h], "rms": float(rms),
            "calibrated_at":  datetime.now().isoformat(timespec="seconds"),
            "shot_count":     accepted, "board_cols": int(cols),
            "board_rows":     int(rows), "square_size": float(sq),
        }
        self._save_cal(cam_id, cal_data)
        self._end_session()
        log(f"[LensCalibrate] Saved [{cam_id}] lens={lens} rms={rms:.4f} shots={accepted}")
        return {"type": "save_result", "ok": True,
                "rms": round(float(rms), 4), "shot_count": accepted,
                "image_size": [w, h], "lens_type": lens}

    # ── Status broadcast ──────────────────────────────────────────────────────

    def _emit_status(self, action, *, rms, old_rms, accepted, rejected,
                     shot_centers, reason="", K=None, D=None):
        if self._sio is None:
            return
        payload = {
            "cam_id": self._cam_id, "action": action,
            "rms": rms, "old_rms": old_rms,
            "accepted": accepted, "rejected": rejected,
            "shot_centers": shot_centers, "reason": reason,
        }
        if K is not None:
            payload["K"] = K
        if D is not None:
            payload["D"] = D
        self._sio.emit("calib_auto_status", payload)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _cal_path(self, cam_id: str) -> str:
        os.makedirs(CALIB_DIR, exist_ok=True)
        safe = cam_id.replace("/", "_").replace(":", "_").replace(" ", "_")
        return os.path.join(CALIB_DIR, f"{safe}.json")

    def _load_cal(self, cam_id: str) -> Optional[dict]:
        if not cam_id:
            return None
        path = self._cal_path(cam_id)
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

    def _save_cal(self, cam_id: str, data: dict):
        try:
            with open(self._cal_path(cam_id), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._cal_cache       = data
            self._cal_cache_mtime = os.path.getmtime(self._cal_path(cam_id))
        except Exception as e:
            log(f"[LensCalibrate] save error: {e}")
