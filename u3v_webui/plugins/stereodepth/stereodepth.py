"""plugins/stereodepth/stereodepth.py — StereoDepth plugin (1.0.0)

Live depth map from stereo rectified frames, with binary PLY export.

Workflow:
  1. Sidebar: select L/R cameras → click Enable (loads match + rectify params)
  2. Main window overlays a JET-colorized inverse-depth map (letterboxed).
  3. Modal: clip_min/clip_max sliders, live stats, Save PLY button.
  4. PLY: binary_little_endian, XYZ float32 + RGB uint8, saved to captures/depth/
     and delivered to browser as a download.
"""

import io
import json
import os
import struct
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


def _letterbox(img: np.ndarray, fw: int, fh: int) -> np.ndarray:
    ih, iw = img.shape[:2]
    scale  = min(fw / iw, fh / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas  = np.zeros((fh, fw, 3), dtype=np.uint8)
    x0 = (fw - nw) // 2
    y0 = (fh - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas


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

    # Pack as structured array: 3×float32 + 3×uint8 (no padding)
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


class StereoDepth(PluginBase):

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

        # Session flag (modal open)
        self._session_active = False

        # Depth clip range (mm)
        self._clip_min = 200.0
        self._clip_max = 5000.0

        # Match algo params (loaded from match JSON)
        self._algorithm        = "sgbm"
        self._num_disparities  = 64
        self._block_size       = 9
        self._p1               = -1
        self._p2               = -1
        self._uniqueness_ratio = 5
        self._speckle_window   = 100
        self._speckle_range    = 2

        # Thread
        self._depth_active = False

        # Results
        self._depth_jpg:  Optional[bytes]     = None
        self._pts_xyz:    Optional[np.ndarray] = None
        self._pts_rgb:    Optional[np.ndarray] = None
        self._ply_bytes:  Optional[bytes]      = None
        self._ply_path:   Optional[str]        = None

        # Map cache
        self._maps_cam: Optional[tuple] = None
        self._map_L:    Optional[tuple] = None
        self._map_R:    Optional[tuple] = None
        self._Q:        Optional[np.ndarray] = None
        self._img_size: Optional[tuple] = None

        # JSON caches
        self._rectify_cache:       Optional[dict] = None
        self._rectify_cache_mtime: float          = -1.0
        self._stereo_cache:        Optional[dict] = None
        self._stereo_cache_mtime:  float          = -1.0

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self)        -> str: return "StereoDepth"
    @property
    def version(self)     -> str: return "1.0.0"
    @property
    def description(self) -> str: return "Live depth map and point-cloud export from stereo rectified frames"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(DEPTH_DIR, exist_ok=True)

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        StereoDepth._instances[cam_id] = self

    def on_camera_close(self, cam_id: str = ""):
        with self._lock:
            self._depth_active   = False
            self._enabled        = False
            self._session_active = False
        StereoDepth._instances.pop(cam_id, None)

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        cal = self._load_match_cal(self._cam_left, self._cam_right)
        with self._lock:
            enabled = self._enabled
            dside   = self._display_side
            has_ply = self._ply_bytes is not None
        return {
            "depth_cam_left":     self._cam_left,
            "depth_cam_right":    self._cam_right,
            "depth_enabled":      enabled,
            "depth_display_side": dside,
            "depth_has_data":     cal is not None,
            "depth_has_ply":      has_ply,
            "depth_clip_min":     self._clip_min,
            "depth_clip_max":     self._clip_max,
        }

    # ── Params ────────────────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == "depth_cam_left":
            with self._lock: self._cam_left = str(value)
            if self._emit_state: self._emit_state()
            return True
        if key == "depth_cam_right":
            with self._lock: self._cam_right = str(value)
            if self._emit_state: self._emit_state()
            return True
        if key == "depth_display_side":
            with self._lock: self._display_side = "R" if str(value) == "R" else "L"
            return True
        if key == "depth_enabled":
            enabled = bool(value)
            if enabled:
                try:
                    self._ensure_calibration(self._cam_left, self._cam_right)
                except Exception as e:
                    log(f"[StereoDepth] enable failed: {e}")
                    if self._emit_state: self._emit_state()
                    return True
                self._start_worker()
            with self._lock:
                self._enabled = enabled
            if self._emit_state: self._emit_state()
            return True
        if key == "depth_clip_min":
            with self._lock: self._clip_min = max(0.0, float(value))
            return True
        if key == "depth_clip_max":
            with self._lock: self._clip_max = max(1.0, float(value))
            return True
        return False

    # ── Frame pipeline ────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            if not self._enabled:
                return None
            jpg = self._depth_jpg

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

        @app.route("/plugin/stereodepth/depth")
        def _depth_img():
            cam_id = _req.args.get("cam_id", "")
            inst = StereoDepth._instances.get(cam_id)
            if inst is None:
                inst = next((i for i in StereoDepth._instances.values()
                             if i._state is not None), None)
            if inst is None:
                return "Not found", 404
            with inst._lock:
                jpg = inst._depth_jpg
            if jpg is None:
                return "No depth", 404
            return send_file(io.BytesIO(jpg), mimetype="image/jpeg")

        @app.route("/plugin/stereodepth/download_ply")
        def _download_ply():
            cam_id = _req.args.get("cam_id", "")
            inst = StereoDepth._instances.get(cam_id)
            if inst is None:
                return "Not found", 404
            with inst._lock:
                data     = inst._ply_bytes
                filename = inst._ply_path
            if data is None:
                return "No PLY available", 404
            fname = os.path.basename(filename) if filename else "pointcloud.ply"
            return send_file(
                io.BytesIO(data),
                mimetype="application/octet-stream",
                as_attachment=True,
                download_name=fname,
            )

        @sio.on("depth_open")
        def _depth_open(data):
            if not is_admin(): return
            cam_id    = data.get("cam_id", "")
            cam_left  = data.get("cam_left",  "")
            cam_right = data.get("cam_right", "")

            inst = StereoDepth._instances.get(cam_id)
            if inst is None:
                sio.emit("depth_event",
                         {"type": "error", "msg": "StereoDepth not active"}, to=_req.sid)
                return

            with inst._lock:
                if cam_left:  inst._cam_left  = cam_left
                if cam_right: inst._cam_right = cam_right

            try:
                inst._ensure_calibration(inst._cam_left, inst._cam_right)
            except Exception as e:
                sio.emit("depth_event",
                         {"type": "error", "msg": str(e)}, to=_req.sid)
                return

            inst._start_worker()
            with inst._lock:
                inst._session_active = True
            sio.emit("depth_event", {"type": "started"}, to=_req.sid)
            emit_state()

        @sio.on("depth_set_params")
        def _depth_set_params(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst = StereoDepth._instances.get(cam_id)
            if inst is None: return
            for k, v in data.items():
                if k != "cam_id":
                    inst.handle_set_param(k, v, None)

        @sio.on("depth_save_ply")
        def _depth_save_ply(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst = StereoDepth._instances.get(cam_id)
            if inst is None:
                sio.emit("depth_event",
                         {"type": "ply_result", "ok": False, "error": "Not active"},
                         to=_req.sid)
                return
            result = inst._generate_ply()
            sio.emit("depth_event", result, to=_req.sid)
            emit_state()

        @sio.on("depth_cancel")
        def _depth_cancel(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            inst = StereoDepth._instances.get(cam_id)
            if inst:
                with inst._lock:
                    inst._session_active = False
                    if not inst._enabled:
                        inst._depth_active = False
            emit_state()

    # ── Worker ────────────────────────────────────────────────────────────────

    def _start_worker(self):
        with self._lock:
            if self._depth_active:
                return
            self._depth_active = True
        threading.Thread(target=self._worker_loop, daemon=True).start()

    def _worker_loop(self):
        log(f"[StereoDepth] worker started  L={self._cam_left} R={self._cam_right}")
        while True:
            with self._lock:
                if not self._depth_active:
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
                    gL_f  = cv2.flip(gL, 1)
                    gR_f  = cv2.flip(gR, 1)
                    disp  = cv2.flip(matcher.compute(gR_f, gL_f), 1)
                    color_ref = rR
                else:
                    disp      = matcher.compute(gL, gR)
                    color_ref = rL

                disp_f = disp.astype(np.float32) / 16.0

                # Reproject to 3-D
                pts3d = cv2.reprojectImageTo3D(disp_f, Q)          # (H,W,3) float32
                Z     = pts3d[:, :, 2]

                valid_disp = disp_f > 0
                valid_z    = np.isfinite(Z) & (Z > clip_mn) & (Z < clip_mx)
                valid      = valid_disp & valid_z

                valid_pct = float(valid.mean() * 100.0)
                z_vals = Z[valid]
                min_d  = float(z_vals.min()) if z_vals.size else 0.0
                max_d  = float(z_vals.max()) if z_vals.size else 0.0
                med_d  = float(np.median(z_vals)) if z_vals.size else 0.0

                # Colorize: closer → warmer; use inverted normalized Z on JET
                depth_u8 = np.zeros(Z.shape, dtype=np.uint8)
                if valid.any() and max_d > min_d:
                    norm = np.clip((Z[valid] - min_d) / (max_d - min_d), 0, 1)
                    depth_u8[valid] = ((1.0 - norm) * 255).astype(np.uint8)
                elif valid.any():
                    depth_u8[valid] = 128
                colored = cv2.applyColorMap(depth_u8, cv2.COLORMAP_JET)
                colored[~valid] = 0

                ok, buf = cv2.imencode(".jpg", colored, [cv2.IMWRITE_JPEG_QUALITY, 85])

                # Store point cloud (BGR→RGB for PLY)
                xyz  = pts3d[valid]
                bgr  = color_ref[valid]
                rgb  = bgr[:, ::-1]

                with self._lock:
                    if ok:
                        self._depth_jpg = buf.tobytes()
                    self._pts_xyz = xyz
                    self._pts_rgb = rgb

                if self._sio:
                    self._sio.emit("depth_stats", {
                        "cam_id":    self._cam_id,
                        "valid_pct": round(valid_pct, 1),
                        "min_d":     round(min_d, 0),
                        "max_d":     round(max_d, 0),
                        "med_d":     round(med_d, 0),
                    })

            except Exception as e:
                log(f"[StereoDepth] worker error: {e}")

            slack = _DT - (time.time() - t0)
            if slack > 0:
                time.sleep(slack)

        log(f"[StereoDepth] worker stopped")

    # ── Calibration ───────────────────────────────────────────────────────────

    def _ensure_calibration(self, cam_left: str, cam_right: str):
        with self._lock:
            if self._maps_cam == (cam_left, cam_right):
                return

        # Load match params
        match = self._load_match_cal(cam_left, cam_right)
        if match is None:
            raise RuntimeError(
                f"No StereoMatch params for '{cam_left}' ↔ '{cam_right}'. "
                "Run StereoMatch → Save first.")

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
            self._algorithm        = match.get("algorithm",        "sgbm")
            self._num_disparities  = match.get("num_disparities",  64)
            self._block_size       = match.get("block_size",       9)
            self._p1               = match.get("p1",               -1)
            self._p2               = match.get("p2",               -1)
            self._uniqueness_ratio = match.get("uniqueness_ratio", 5)
            self._speckle_window   = match.get("speckle_window",   100)
            self._speckle_range    = match.get("speckle_range",    2)
            self._map_L    = (m1L, m2L)
            self._map_R    = (m1R, m2R)
            self._Q        = Q
            self._img_size = (w, h)
            self._maps_cam = (cam_left, cam_right)

        log(f"[StereoDepth] calibration loaded  L={cam_left} R={cam_right} "
            f"lens={lens} {w}×{h} algo={match.get('algorithm')}")

    # ── PLY generation ────────────────────────────────────────────────────────

    def _generate_ply(self) -> dict:
        with self._lock:
            xyz = self._pts_xyz
            rgb = self._pts_rgb
            cam_L = self._cam_left
            cam_R = self._cam_right

        if xyz is None or len(xyz) == 0:
            return {"type": "ply_result", "ok": False, "error": "No point cloud available"}

        try:
            ply_bytes = _build_ply(xyz, rgb)
        except Exception as e:
            return {"type": "ply_result", "ok": False, "error": str(e)}

        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe  = lambda s: s.replace("/", "_").replace(":", "_").replace(" ", "_")
        fname = f"{safe(cam_L)}__{safe(cam_R)}_{ts}.ply"
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

        log(f"[StereoDepth] PLY saved  {path}  ({len(xyz)} pts)")
        return {
            "type":      "ply_result",
            "ok":        True,
            "filename":  fname,
            "n_points":  len(xyz),
            "saved_at":  ts,
        }

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

    def _load_match_cal(self, cam_left: str, cam_right: str) -> Optional[dict]:
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
