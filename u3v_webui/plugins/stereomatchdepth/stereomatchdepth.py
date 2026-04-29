"""plugins/stereomatchdepth/stereomatchdepth.py — StereoMatch & Depth plugin (1.0.2)

Combined stereo disparity matching and depth mapping from rectified frames.

Workflow:
  1. Sidebar: select L/R cameras, set View (Disparity/Depth), Algorithm (BM/SGBM),
             Resolution (1x / ½ / ¼)
  2. Click Enable → loads saved params, builds remap maps
  3. Main window overlays disparity or depth map at native stereo resolution
  4. Modal: tune all numeric params, view stats, Save, Save PLY
  5. Save → captures/match/<L>__<R>.json  (numeric params only; View/Algo/Scale NOT saved)
  6. PLY  → captures/depth/<L>__<R>_<ts>.ply + browser download

Resolution scaling (smd_proc_scale = 1 | 2 | 4, session-only):
  Frames are resized to the full cal resolution before remap; remap maps are built
  at 1/scale output size with P and Q matrices scaled accordingly.
  Depth values are preserved — proof: Z = (f/s)·T/(d/s) = f·T/d.

Lifecycle note:
  _enabled is ONLY cleared by manual Disable.
  on_camera_close does NOT reset _enabled — the overlay persists through
  viewer disconnects and camera re-opens.
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
DEPTH_DIR   = os.path.join(CAPTURE_DIR, "depth")

_FPS = 5
_DT  = 1.0 / _FPS


def _build_ply(pts_xyz: np.ndarray, pts_rgb: np.ndarray) -> bytes:
    """Return binary_little_endian PLY bytes (XYZ float32 + RGB uint8)."""
    n = len(pts_xyz)
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "property uchar red\n"
        "property uchar green\n"
        "property uchar blue\n"
        "end_header\n"
    ).encode("ascii")
    dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                   ("r", "u1"),  ("g", "u1"),  ("b", "u1")])
    rec = np.empty(n, dtype=dt)
    rec["x"] = pts_xyz[:, 0].astype(np.float32)
    rec["y"] = pts_xyz[:, 1].astype(np.float32)
    rec["z"] = pts_xyz[:, 2].astype(np.float32)
    rec["r"] = pts_rgb[:, 0]
    rec["g"] = pts_rgb[:, 1]
    rec["b"] = pts_rgb[:, 2]
    return header + rec.tobytes()


class StereoMatchDepth(PluginBase):

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
        self._enabled      = False
        self._display_side = "L"

        # Session-only (not saved to JSON)
        self._view_mode = "disparity"   # "disparity" | "depth"
        self._algorithm = "sgbm"        # "bm" | "sgbm"

        # Modal open flag
        self._session_active = False

        # Saved numeric parameters
        self._num_disparities  = 64
        self._block_size       = 9
        self._p1               = -1
        self._p2               = -1
        self._uniqueness_ratio = 5
        self._speckle_window   = 100
        self._speckle_range    = 2
        self._clip_min         = 200.0
        self._clip_max         = 5000.0

        # Worker thread
        self._worker_active = False

        # Computed results
        self._disparity_jpg: Optional[bytes]      = None
        self._depth_jpg:     Optional[bytes]      = None
        self._pts_xyz:       Optional[np.ndarray] = None
        self._pts_rgb:       Optional[np.ndarray] = None
        self._ply_bytes:     Optional[bytes]      = None
        self._ply_path:      Optional[str]        = None

        # Calibration cache
        self._maps_cam:   Optional[tuple]      = None
        self._map_L:      Optional[tuple]      = None
        self._map_R:      Optional[tuple]      = None
        self._Q:          Optional[np.ndarray] = None
        self._img_size:   Optional[tuple]      = None  # remap output size (proc resolution)
        self._cal_size:   Optional[tuple]      = None  # full cal resolution (pre-remap resize)
        self._rect_raw:   Optional[dict]       = None  # raw rectification arrays (full res)
        self._proc_scale: int                  = 1     # 1, 2, or 4 — session-only

        # File caches
        self._rectify_cache:       Optional[dict] = None
        self._rectify_cache_mtime: float          = -1.0
        self._stereo_cache:        Optional[dict] = None
        self._stereo_cache_mtime:  float          = -1.0

        # Diagnostic throttle
        self._diag_last_log: float = 0.0

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self)        -> str: return "StereoMatch & Depth"
    @property
    def version(self)     -> str: return "1.0.2"
    @property
    def description(self) -> str: return "Combined stereo disparity and depth map with PLY export"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(MATCH_DIR, exist_ok=True)
        os.makedirs(DEPTH_DIR, exist_ok=True)

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        StereoMatchDepth._instances[cam_id] = self

    def on_camera_close(self, cam_id: str = ""):
        # _enabled intentionally NOT cleared — only manual Disable resets it
        with self._lock:
            self._session_active = False
        StereoMatchDepth._instances.pop(cam_id, None)

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        cal = self._load_saved_params(self._cam_left, self._cam_right)
        with self._lock:
            enabled = self._enabled
            dside   = self._display_side
            view    = self._view_mode
            algo    = self._algorithm
            has_ply = self._ply_bytes is not None
        return {
            "smd_cam_left":     self._cam_left,
            "smd_cam_right":    self._cam_right,
            "smd_enabled":      enabled,
            "smd_display_side": dside,
            "smd_view_mode":    view,
            "smd_algorithm":    algo,
            "smd_has_data":     cal is not None,
            "smd_saved_at":     cal.get("saved_at") if cal else None,
            "smd_has_ply":      has_ply,
            "smd_proc_scale":   self._proc_scale,
        }

    # ── Params ────────────────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == "smd_cam_left":
            with self._lock: self._cam_left = str(value)
            if self._emit_state: self._emit_state()
            return True
        if key == "smd_cam_right":
            with self._lock: self._cam_right = str(value)
            if self._emit_state: self._emit_state()
            return True
        if key == "smd_display_side":
            with self._lock: self._display_side = "R" if str(value) == "R" else "L"
            return True
        if key == "smd_view_mode":
            with self._lock:
                self._view_mode = "depth" if str(value) == "depth" else "disparity"
            return True
        if key == "smd_algorithm":
            with self._lock:
                self._algorithm = "bm" if str(value) == "bm" else "sgbm"
            return True
        if key == "smd_enabled":
            enabled = bool(value)
            if enabled:
                try:
                    self._ensure_calibration(self._cam_left, self._cam_right)
                except Exception as e:
                    log(f"[StereoMatchDepth] enable failed: {e}")
                    if self._emit_state: self._emit_state()
                    return True
                self._start_worker()
            with self._lock:
                self._enabled = enabled
            if self._emit_state: self._emit_state()
            return True
        if key == "smd_num_disparities":
            with self._lock: self._num_disparities = max(16, (int(value) // 16) * 16)
            return True
        if key == "smd_block_size":
            with self._lock: self._block_size = max(5, int(value) | 1)
            return True
        if key == "smd_p1":
            with self._lock: self._p1 = int(value)
            return True
        if key == "smd_p2":
            with self._lock: self._p2 = int(value)
            return True
        if key == "smd_uniqueness_ratio":
            with self._lock: self._uniqueness_ratio = max(0, min(50, int(value)))
            return True
        if key == "smd_speckle_window":
            with self._lock: self._speckle_window = max(0, int(value))
            return True
        if key == "smd_speckle_range":
            with self._lock: self._speckle_range = max(1, int(value))
            return True
        if key == "smd_clip_min":
            with self._lock: self._clip_min = max(0.0, float(value))
            return True
        if key == "smd_clip_max":
            with self._lock: self._clip_max = max(1.0, float(value))
            return True
        if key == "smd_proc_scale":
            s = int(value) if int(value) in (1, 2, 4) else 1
            with self._lock:
                self._proc_scale = s
            self._rebuild_scaled_maps()
            return True
        return False

    # ── Frame pipeline ────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            if not self._enabled:
                return None
            jpg = self._disparity_jpg if self._view_mode == "disparity" else self._depth_jpg

        if jpg is None:
            return None
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        return img

    # ── Routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        from flask import request as _req, send_file

        is_admin   = ctx["is_admin"]
        emit_state = ctx["emit_state"]

        def _get_inst(cam_id):
            return StereoMatchDepth._instances.get(cam_id) or \
                   next((i for i in StereoMatchDepth._instances.values()
                         if i._state is not None), None)

        @app.route("/plugin/stereomatchdepth/disparity")
        def _smd_disparity():
            inst = _get_inst(_req.args.get("cam_id", ""))
            if inst is None: return "Not found", 404
            with inst._lock: jpg = inst._disparity_jpg
            if jpg is None: return "No data", 404
            return send_file(io.BytesIO(jpg), mimetype="image/jpeg")

        @app.route("/plugin/stereomatchdepth/depth")
        def _smd_depth():
            inst = _get_inst(_req.args.get("cam_id", ""))
            if inst is None: return "Not found", 404
            with inst._lock: jpg = inst._depth_jpg
            if jpg is None: return "No data", 404
            return send_file(io.BytesIO(jpg), mimetype="image/jpeg")

        @app.route("/plugin/stereomatchdepth/download_ply")
        def _smd_download_ply():
            inst = StereoMatchDepth._instances.get(_req.args.get("cam_id", ""))
            if inst is None: return "Not found", 404
            with inst._lock:
                data     = inst._ply_bytes
                filename = inst._ply_path
            if data is None: return "No PLY available", 404
            fname = os.path.basename(filename) if filename else "pointcloud.ply"
            return send_file(io.BytesIO(data), mimetype="application/octet-stream",
                             as_attachment=True, download_name=fname)

        @sio.on("smd_open")
        def _smd_open(data):
            if not is_admin(): return
            cam_id    = data.get("cam_id", "")
            cam_left  = data.get("cam_left",  "")
            cam_right = data.get("cam_right", "")

            inst = StereoMatchDepth._instances.get(cam_id)
            if inst is None:
                sio.emit("smd_event",
                         {"type": "error", "msg": "StereoMatchDepth not active"},
                         to=_req.sid)
                return

            with inst._lock:
                if cam_left:  inst._cam_left  = cam_left
                if cam_right: inst._cam_right = cam_right

            try:
                inst._ensure_calibration(inst._cam_left, inst._cam_right)
            except Exception as e:
                sio.emit("smd_event", {"type": "error", "msg": str(e)}, to=_req.sid)
                return

            inst._start_worker()
            with inst._lock:
                inst._session_active = True
                params = {
                    "num_disparities":  inst._num_disparities,
                    "block_size":       inst._block_size,
                    "p1":               inst._p1,
                    "p2":               inst._p2,
                    "uniqueness_ratio": inst._uniqueness_ratio,
                    "speckle_window":   inst._speckle_window,
                    "speckle_range":    inst._speckle_range,
                    "clip_min":         inst._clip_min,
                    "clip_max":         inst._clip_max,
                }
            sio.emit("smd_event", {"type": "started", **params}, to=_req.sid)
            emit_state()

        @sio.on("smd_set_params")
        def _smd_set_params(data):
            if not is_admin(): return
            inst = StereoMatchDepth._instances.get(data.get("cam_id", ""))
            if inst is None: return
            for k, v in data.items():
                if k != "cam_id":
                    inst.handle_set_param(k, v, None)

        @sio.on("smd_save")
        def _smd_save(data):
            if not is_admin(): return
            inst = StereoMatchDepth._instances.get(data.get("cam_id", ""))
            if inst is None: return
            sio.emit("smd_event", inst._save_params(), to=_req.sid)
            emit_state()

        @sio.on("smd_save_ply")
        def _smd_save_ply(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst = StereoMatchDepth._instances.get(cam_id)
            if inst is None:
                sio.emit("smd_event",
                         {"type": "ply_result", "ok": False, "error": "Not active"},
                         to=_req.sid)
                return
            sio.emit("smd_event", inst._generate_ply(), to=_req.sid)
            emit_state()

        @sio.on("smd_cancel")
        def _smd_cancel(data):
            if not is_admin(): return
            inst = StereoMatchDepth._instances.get(data.get("cam_id", ""))
            if inst:
                with inst._lock:
                    inst._session_active = False
                    if not inst._enabled:
                        inst._worker_active = False
            emit_state()

    # ── Worker ────────────────────────────────────────────────────────────────

    def _start_worker(self):
        with self._lock:
            if self._worker_active:
                return
            self._worker_active = True
        threading.Thread(target=self._worker_loop, daemon=True).start()

    def _worker_loop(self):
        log(f"[StereoMatchDepth] worker started  L={self._cam_left} R={self._cam_right}")
        while True:
            with self._lock:
                if not self._worker_active:
                    break
                cam_L   = self._cam_left
                cam_R   = self._cam_right
                side    = self._display_side
                algo    = self._algorithm
                nd      = self._num_disparities
                bs      = self._block_size
                p1      = self._p1
                p2      = self._p2
                ur      = self._uniqueness_ratio
                sws     = self._speckle_window
                sr      = self._speckle_range
                clip_mn = self._clip_min
                clip_mx = self._clip_max
                map_L   = self._map_L
                map_R   = self._map_R
                Q       = self._Q
                isz     = self._img_size
                cal_sz  = self._cal_size
                scale   = self._proc_scale

            t0 = time.time()
            try:
                if self._state is None or map_L is None or Q is None:
                    time.sleep(_DT)
                    continue

                fL = self._state.get_latest_frame(cam_L)
                fR = self._state.get_latest_frame(cam_R)
                if fL is None or fR is None:
                    time.sleep(_DT)
                    continue

                # Resize to full cal resolution before remap; maps output at proc resolution.
                resize_to = cal_sz if cal_sz is not None else isz
                if resize_to is not None:
                    if (fL.shape[1], fL.shape[0]) != resize_to:
                        fL = cv2.resize(fL, resize_to)
                    if (fR.shape[1], fR.shape[0]) != resize_to:
                        fR = cv2.resize(fR, resize_to)

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
                    gL_f = cv2.flip(gL, 1); gR_f = cv2.flip(gR, 1)
                    disp  = cv2.flip(matcher.compute(gR_f, gL_f), 1)
                    color_ref = rR
                else:
                    disp      = matcher.compute(gL, gR)
                    color_ref = rL

                disp_f     = disp.astype(np.float32) / 16.0
                # Invalid pixels from OpenCV stereo are (minDisparity-1)*16 = -16 raw
                # → disp_f = -1.0.  Use >= 0 to include zero-disparity valid matches.
                valid_disp = disp_f >= 0
                valid_pct_d = float(valid_disp.mean() * 100.0)
                avg_d = float(disp_f[valid_disp].mean()) if valid_disp.any() else 0.0

                now = time.time()
                if now - self._diag_last_log >= 10.0:
                    self._diag_last_log = now
                    log(f"[StereoMatchDepth] diag  L={cam_L} R={cam_R} "
                        f"cal={cal_sz} proc={isz} scale=1/{scale} "
                        f"disp_raw min={int(disp.min())} max={int(disp.max())} "
                        f"valid={valid_pct_d:.1f}%")

                # ── Colorize disparity ────────────────────────────────────────
                disp_u8 = np.zeros(disp_f.shape, dtype=np.uint8)
                if valid_disp.any():
                    dmin, dmax = disp_f[valid_disp].min(), disp_f[valid_disp].max()
                    if dmax > dmin:
                        disp_u8[valid_disp] = (
                            (disp_f[valid_disp] - dmin) / (dmax - dmin) * 255
                        ).astype(np.uint8)
                    else:
                        disp_u8[valid_disp] = 128
                disp_colored = cv2.applyColorMap(disp_u8, cv2.COLORMAP_JET)
                disp_colored[~valid_disp] = 0

                # ── Reproject → depth ─────────────────────────────────────────
                pts3d = cv2.reprojectImageTo3D(disp_f, Q)
                Z     = pts3d[:, :, 2]
                valid_z     = np.isfinite(Z) & (Z > clip_mn) & (Z < clip_mx)
                valid_depth = valid_disp & valid_z
                valid_pct_z = float(valid_depth.mean() * 100.0)
                z_vals = Z[valid_depth]
                min_z  = float(z_vals.min())      if z_vals.size else 0.0
                max_z  = float(z_vals.max())      if z_vals.size else 0.0
                med_z  = float(np.median(z_vals)) if z_vals.size else 0.0

                # ── Colorize depth (inverted JET: closer = warmer) ────────────
                depth_u8 = np.zeros(Z.shape, dtype=np.uint8)
                if valid_depth.any() and max_z > min_z:
                    norm = np.clip((Z[valid_depth] - min_z) / (max_z - min_z), 0, 1)
                    depth_u8[valid_depth] = ((1.0 - norm) * 255).astype(np.uint8)
                elif valid_depth.any():
                    depth_u8[valid_depth] = 128
                depth_colored = cv2.applyColorMap(depth_u8, cv2.COLORMAP_JET)
                depth_colored[~valid_depth] = 0

                ok_d, buf_d = cv2.imencode(".jpg", disp_colored,  [cv2.IMWRITE_JPEG_QUALITY, 85])
                ok_z, buf_z = cv2.imencode(".jpg", depth_colored, [cv2.IMWRITE_JPEG_QUALITY, 85])

                xyz = pts3d[valid_depth]
                rgb = color_ref[valid_depth][:, ::-1]   # BGR→RGB

                with self._lock:
                    if ok_d: self._disparity_jpg = buf_d.tobytes()
                    if ok_z: self._depth_jpg     = buf_z.tobytes()
                    self._pts_xyz = xyz
                    self._pts_rgb = rgb

                if self._sio:
                    self._sio.emit("smd_stats", {
                        "cam_id":      self._cam_id,
                        "avg_disp":    round(avg_d,      1),
                        "valid_pct_d": round(valid_pct_d, 1),
                        "min_depth":   round(min_z,      0),
                        "max_depth":   round(max_z,      0),
                        "med_depth":   round(med_z,      0),
                        "valid_pct_z": round(valid_pct_z, 1),
                    })

            except Exception as e:
                log(f"[StereoMatchDepth] worker error: {e}")

            slack = _DT - (time.time() - t0)
            if slack > 0:
                time.sleep(slack)

        log(f"[StereoMatchDepth] worker stopped")

    # ── Calibration ───────────────────────────────────────────────────────────

    def _ensure_calibration(self, cam_left: str, cam_right: str):
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
        Q    = np.array(rect["Q"],  dtype=np.float64)

        KL = np.array(stereo["KL"], dtype=np.float64)
        KR = np.array(stereo["KR"], dtype=np.float64)
        DL = np.array(stereo["DL"], dtype=np.float64)
        DR = np.array(stereo["DR"], dtype=np.float64)

        saved = self._load_saved_params(cam_left, cam_right)
        with self._lock:
            if saved:
                self._num_disparities  = saved.get("num_disparities",  self._num_disparities)
                self._block_size       = saved.get("block_size",       self._block_size)
                self._p1               = saved.get("p1",               self._p1)
                self._p2               = saved.get("p2",               self._p2)
                self._uniqueness_ratio = saved.get("uniqueness_ratio", self._uniqueness_ratio)
                self._speckle_window   = saved.get("speckle_window",   self._speckle_window)
                self._speckle_range    = saved.get("speckle_range",    self._speckle_range)
                self._clip_min         = saved.get("clip_min",         self._clip_min)
                self._clip_max         = saved.get("clip_max",         self._clip_max)
            self._rect_raw = {
                "lens": lens, "wh": (w, h),
                "R1": R1, "R2": R2, "P1": P1, "P2": P2, "Q": Q,
                "KL": KL, "DL": DL, "KR": KR, "DR": DR,
            }
            self._maps_cam = (cam_left, cam_right)

        log(f"[StereoMatchDepth] calibration loaded  L={cam_left} R={cam_right} "
            f"lens={lens} {w}×{h}")
        self._rebuild_scaled_maps()

    def _rebuild_scaled_maps(self):
        """Rebuild remap maps and Q at the current proc_scale from stored raw cal data."""
        with self._lock:
            raw = self._rect_raw
            s   = self._proc_scale
        if raw is None:
            return

        lens    = raw["lens"]
        w, h    = raw["wh"]
        s_w     = max(1, w // s)
        s_h     = max(1, h // s)
        R1, R2  = raw["R1"], raw["R2"]
        KL, DL  = raw["KL"], raw["DL"]
        KR, DR  = raw["KR"], raw["DR"]

        # Scale P: rows 0–1 (fx, fy, cx, cy, Tx) all divide by s; row 2 unchanged.
        P1_s = raw["P1"].copy(); P1_s[:2, :] /= s
        P2_s = raw["P2"].copy(); P2_s[:2, :] /= s

        # Scale Q: column 3 holds pixel-space values (−cx, −cy, f, doffs).
        # T_x (Q[3,2]) is in real-world units and stays unchanged.
        Q_s = raw["Q"].copy(); Q_s[:, 3] /= s

        if lens == "fisheye":
            m1L, m2L = cv2.fisheye.initUndistortRectifyMap(
                KL, DL.flatten()[:4].reshape(4, 1), R1, P1_s, (s_w, s_h), cv2.CV_16SC2)
            m1R, m2R = cv2.fisheye.initUndistortRectifyMap(
                KR, DR.flatten()[:4].reshape(4, 1), R2, P2_s, (s_w, s_h), cv2.CV_16SC2)
        else:
            m1L, m2L = cv2.initUndistortRectifyMap(
                KL, DL.reshape(1, -1), R1, P1_s, (s_w, s_h), cv2.CV_16SC2)
            m1R, m2R = cv2.initUndistortRectifyMap(
                KR, DR.reshape(1, -1), R2, P2_s, (s_w, s_h), cv2.CV_16SC2)

        with self._lock:
            self._map_L    = (m1L, m2L)
            self._map_R    = (m1R, m2R)
            self._Q        = Q_s
            self._cal_size = (w, h)
            self._img_size = (s_w, s_h)

        log(f"[StereoMatchDepth] maps rebuilt  scale=1/{s}  cal={w}×{h}  proc={s_w}×{s_h}")

    # ── Save / Generate ───────────────────────────────────────────────────────

    def _save_params(self) -> dict:
        with self._lock:
            if not self._cam_left or not self._cam_right:
                return {"type": "save_result", "ok": False,
                        "error": "No camera pair selected"}
            data = {
                "format_version":   1,
                "cam_left":         self._cam_left,
                "cam_right":        self._cam_right,
                "num_disparities":  self._num_disparities,
                "block_size":       self._block_size,
                "p1":               self._p1,
                "p2":               self._p2,
                "uniqueness_ratio": self._uniqueness_ratio,
                "speckle_window":   self._speckle_window,
                "speckle_range":    self._speckle_range,
                "clip_min":         self._clip_min,
                "clip_max":         self._clip_max,
                "saved_at":         datetime.now().isoformat(timespec="seconds"),
            }
            cam_L, cam_R = self._cam_left, self._cam_right

        os.makedirs(MATCH_DIR, exist_ok=True)
        path = self._match_path(cam_L, cam_R)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log(f"[StereoMatchDepth] params saved  L={cam_L} R={cam_R}")
            return {"type": "save_result", "ok": True, "saved_at": data["saved_at"]}
        except Exception as e:
            return {"type": "save_result", "ok": False, "error": str(e)}

    def _generate_ply(self) -> dict:
        with self._lock:
            xyz   = self._pts_xyz
            rgb   = self._pts_rgb
            cam_L = self._cam_left
            cam_R = self._cam_right

        if xyz is None or len(xyz) == 0:
            return {"type": "ply_result", "ok": False, "error": "No point cloud available"}

        try:
            ply_bytes = _build_ply(xyz, rgb)
        except Exception as e:
            return {"type": "ply_result", "ok": False, "error": str(e)}

        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{self._safe(cam_L)}__{self._safe(cam_R)}_{ts}.ply"
        path  = os.path.join(DEPTH_DIR, fname)
        try:
            os.makedirs(DEPTH_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(ply_bytes)
        except Exception as e:
            return {"type": "ply_result", "ok": False, "error": f"Save failed: {e}"}

        with self._lock:
            self._ply_bytes = ply_bytes
            self._ply_path  = path

        log(f"[StereoMatchDepth] PLY saved  {path}  ({len(xyz)} pts)")
        return {"type": "ply_result", "ok": True,
                "filename": fname, "n_points": len(xyz), "saved_at": ts}

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

    def _load_saved_params(self, cam_left: str, cam_right: str) -> Optional[dict]:
        if not cam_left or not cam_right:
            return None
        try:
            with open(self._match_path(cam_left, cam_right), encoding="utf-8") as f:
                return json.load(f)
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
