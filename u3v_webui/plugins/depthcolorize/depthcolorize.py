"""plugins/depthcolorize/depthcolorize.py — DepthColorize plugin (1.5.0)

Display-only plugin: reads Z16 raw depth data from the driver's raw frame
channel and renders it with a user-configurable depth range and colormap.
Also exports point clouds as PLY (XYZ float32, optional RGB vertex colour).

Operating modes
---------------
Single-camera (no colour cam selected):
  Intrinsics only.  Auto-load / save → captures/calibration/<safe_depth>.json
  Format: {"camera_matrix": [[fx,0,cx],[0,fy,cy],[0,0,1]], "dist_coeffs": [...], ...}
  Compatible with LensCalibrate output (other plugins can read it).

Dual-camera (colour cam selected):
  Intrinsics + extrinsics (R, T).
  Auto-load / save → captures/stereo/<safe_depth>__<safe_color>.json
  Format: {"KL": [[...]], "KR": [[...]], "T": [tx,ty,tz] mm, "R": [[...]], ...}
  Compatible with StereoCalibrate output (reads KL/KR/R/T directly).

On camera open: auto-loads depth intrinsics from calibration folder.
On colour-cam selection: auto-loads from stereo folder (falls back to
  calibration folder for colour intrinsics only).

Parameters (session-only, not persisted to disk)
------------------------------------------------
dc_enabled        bool    Plugin active (default False)
dc_auto_range     bool    Auto-scale each frame (default True)
dc_clip_min       float   Near clip mm, manual mode (default 200)
dc_clip_max       float   Far clip mm, manual mode (default 1500)
dc_depth_scale    float   mm per Z16 unit (default 0.125 — SR300)
dc_colormap       int     OpenCV colormap 0-8 (default 0 = Jet)
dc_fx/fy/cx/cy    float   Depth intrinsics (default SR300 nominal)
dc_vertex_color   bool    Include RGB vertex colour in PLY (default False)
dc_color_cam      str     Colour source cam_id, "" = auto
dc_color_fx/fy    float   Colour camera focal length (default SR300 nominal)
dc_color_cx/cy    float   Colour camera principal point (default SR300 nominal)
dc_ext_tx/ty/tz   float   Extrinsic T depth→colour in mm (default 25/0/0)
dc_ext_r{i}{j}   float   R matrix element [i,j], i/j in 0-2 (default identity)
dc_color_cam_source str  Colour frame source: "pipeline" | "display" (default pipeline)
"""

import io
import json
import os
import threading
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import log

_DEPTH_PLY_DIR = os.path.join(CAPTURE_DIR, "depth")
_CALIB_DIR     = os.path.join(CAPTURE_DIR, "calibration")
_STEREO_DIR    = os.path.join(CAPTURE_DIR, "stereo")

_COLORMAPS = [
    cv2.COLORMAP_JET,
    cv2.COLORMAP_BONE,
    cv2.COLORMAP_HOT,
    cv2.COLORMAP_PINK,
    cv2.COLORMAP_OCEAN,
    cv2.COLORMAP_WINTER,
    cv2.COLORMAP_AUTUMN,
    cv2.COLORMAP_RAINBOW,
    cv2.COLORMAP_INFERNO,
]


def _build_ply_xyz(pts: np.ndarray) -> bytes:
    n = len(pts)
    hdr = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "end_header\n"
    ).encode()
    return hdr + pts.astype(np.float32).tobytes()


def _build_ply_xyz_rgb(pts: np.ndarray, rgb: np.ndarray) -> bytes:
    n = len(pts)
    hdr = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    ).encode()
    dt  = np.dtype([("x","<f4"),("y","<f4"),("z","<f4"),("r","u1"),("g","u1"),("b","u1")])
    rec = np.empty(n, dtype=dt)
    rec["x"] = pts[:, 0].astype(np.float32)
    rec["y"] = pts[:, 1].astype(np.float32)
    rec["z"] = pts[:, 2].astype(np.float32)
    rec["r"] = rgb[:, 0]
    rec["g"] = rgb[:, 1]
    rec["b"] = rgb[:, 2]
    return hdr + rec.tobytes()


class DepthColorize(PluginBase):

    _instances: dict = {}

    def __init__(self):
        self._state      = None
        self._sio        = None
        self._lock       = threading.Lock()
        self._cam_id     = ""

        # Display
        self._enabled     = False
        self._auto_range  = True
        self._clip_min    = 200.0
        self._clip_max    = 1500.0
        self._depth_scale = 0.125
        self._colormap    = cv2.COLORMAP_JET

        # Depth intrinsics (SR300 nominal @ 640×480)
        self._fx = 460.0
        self._fy = 460.0
        self._cx = 320.0
        self._cy = 240.0

        # Vertex colour
        self._vertex_color = False
        self._color_cam        = ""       # "" = auto
        self._color_cam_source = "pipeline"   # "pipeline" | "display"

        # Colour camera intrinsics (SR300 nominal @ 640×480)
        self._color_fx = 616.0
        self._color_fy = 616.0
        self._color_cx = 320.0
        self._color_cy = 240.0

        # Extrinsics depth→colour (R=I default, T in mm)
        self._ext_R  = np.eye(3, dtype=np.float64)
        self._ext_tx = 25.0
        self._ext_ty =  0.0
        self._ext_tz =  0.0

        # Latest PLY for download route
        self._ply_bytes: Optional[bytes] = None
        self._ply_path:  Optional[str]   = None

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self)        -> str: return "DepthColorize"
    @property
    def version(self)     -> str: return "1.5.0"
    @property
    def description(self) -> str: return "Re-colorize Z16 depth + PLY export with optional vertex colour"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(_DEPTH_PLY_DIR, exist_ok=True)
        os.makedirs(_CALIB_DIR,     exist_ok=True)
        os.makedirs(_STEREO_DIR,    exist_ok=True)

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        DepthColorize._instances[cam_id] = self
        # Single-camera mode on open: auto-load depth intrinsics from calibration
        intr = self._load_lens_intrinsics(cam_id)
        if intr is not None:
            fx, fy, cx, cy = intr
            with self._lock:
                self._fx = fx; self._fy = fy
                self._cx = cx; self._cy = cy
            log(f"[DepthColorize] Auto-loaded depth intrinsics for {cam_id}")
            if self._sio:
                self._sio.emit("dc_params_event", {
                    "cam_id":   cam_id,
                    "ok":       True,
                    "source":   "lens_cal",
                    "depth_fx": fx, "depth_fy": fy,
                    "depth_cx": cx, "depth_cy": cy,
                })

    def on_camera_close(self, cam_id: str = ""):
        DepthColorize._instances.pop(cam_id, None)

    # ── Frame hook ────────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            enabled     = self._enabled
            auto_range  = self._auto_range
            clip_min    = self._clip_min
            clip_max    = self._clip_max
            depth_scale = self._depth_scale
            colormap    = self._colormap

        if not enabled or self._state is None:
            return None

        drv = self._state.get_driver(cam_id)
        if drv is None or drv.raw_frame_format != "Z16":
            return None

        raw = drv.latest_raw_frame
        if raw is None:
            return None

        valid = raw > 0

        if auto_range:
            if not valid.any():
                return frame.copy()
            d_min = int(raw[valid].min())
            d_max = int(raw[valid].max())
        else:
            d_min = int(clip_min / depth_scale) if depth_scale > 0 else 0
            d_max = int(clip_max / depth_scale) if depth_scale > 0 else 65535

        u8 = np.zeros(raw.shape, dtype=np.uint8)
        if d_max > d_min:
            clipped = np.clip(raw.astype(np.int32), d_min, d_max)
            u8[valid] = (255 - (clipped[valid] - d_min) / (d_max - d_min) * 255).astype(np.uint8)
        elif valid.any():
            u8[valid] = 128

        bgr = cv2.applyColorMap(u8, colormap)
        bgr[~valid] = 0
        return bgr

    # ── Colour camera helpers ─────────────────────────────────────────────────

    def _auto_color_cam(self) -> str:
        """Find the matching colour stream for the current depth camera."""
        if self._state is None:
            return ""
        prefix = self._cam_id.rsplit(":", 1)[0] if ":" in self._cam_id else ""
        for cid in self._state.open_cam_ids:
            if cid == self._cam_id:
                continue
            if prefix and not cid.startswith(prefix):
                continue
            if cid.endswith(":color") or "color" in cid.lower():
                return cid
        for cid in self._state.open_cam_ids:
            if cid != self._cam_id:
                return cid
        return ""

    # ── Calibration persistence helpers ───────────────────────────────────────

    @staticmethod
    def _dc_safe(cam_id: str) -> str:
        return cam_id.replace("/", "_").replace(":", "_").replace(" ", "_")

    @staticmethod
    def _stereo_params_path(depth_cam: str, color_cam: str) -> str:
        d = DepthColorize._dc_safe(depth_cam)
        c = DepthColorize._dc_safe(color_cam)
        return os.path.join(_STEREO_DIR, f"{d}__{c}.json")

    @staticmethod
    def _load_lens_intrinsics(cam_id: str):
        """Return (fx, fy, cx, cy) from LensCalibrate JSON in calibration folder, or None."""
        if not cam_id:
            return None
        safe = DepthColorize._dc_safe(cam_id)
        path = os.path.join(_CALIB_DIR, f"{safe}.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            K = data["camera_matrix"]   # [[fx,0,cx],[0,fy,cy],[0,0,1]]
            return float(K[0][0]), float(K[1][1]), float(K[0][2]), float(K[1][2])
        except Exception:
            return None

    def _load_stereo_params(self, depth_cam: str, color_cam: str) -> Optional[dict]:
        """Load from stereo folder (StereoCalibrate or dc_stereo format). Returns dict or None."""
        try:
            with open(self._stereo_params_path(depth_cam, color_cam), encoding="utf-8") as f:
                data = json.load(f)
            if "KL" not in data or "KR" not in data or "T" not in data:
                return None
            return data
        except Exception:
            return None

    def _auto_load_for_color_cam(self, color_cam: str):
        """Auto-load params when colour cam is selected (dual-camera mode)."""
        resolved = color_cam if color_cam else self._auto_color_cam()
        if not resolved:
            return

        # Priority 1: stereo folder (full KL, KR, R, T)
        params = self._load_stereo_params(self._cam_id, resolved)
        if params:
            KL = params["KL"]   # [[fx,0,cx],[0,fy,cy],[0,0,1]]
            KR = params["KR"]
            T  = params["T"]    # [tx, ty, tz] in mm
            R  = params.get("R", [[1,0,0],[0,1,0],[0,0,1]])
            with self._lock:
                self._fx       = float(KL[0][0]); self._fy = float(KL[1][1])
                self._cx       = float(KL[0][2]); self._cy = float(KL[1][2])
                self._color_fx = float(KR[0][0]); self._color_fy = float(KR[1][1])
                self._color_cx = float(KR[0][2]); self._color_cy = float(KR[1][2])
                self._ext_tx   = float(T[0])
                self._ext_ty   = float(T[1])
                self._ext_tz   = float(T[2])
                self._ext_R    = np.array(R, dtype=np.float64)
                evt = {
                    "cam_id":   self._cam_id, "ok": True, "source": "stereo_cal",
                    "depth_fx": self._fx,   "depth_fy": self._fy,
                    "depth_cx": self._cx,   "depth_cy": self._cy,
                    "color_fx": self._color_fx, "color_fy": self._color_fy,
                    "color_cx": self._color_cx, "color_cy": self._color_cy,
                    "ext_tx":   self._ext_tx, "ext_ty": self._ext_ty, "ext_tz": self._ext_tz,
                    "ext_R":    self._ext_R.flatten().tolist(),
                }
            log(f"[DepthColorize] Auto-loaded stereo params for {self._cam_id}→{resolved}")
            if self._sio:
                self._sio.emit("dc_params_event", evt)
            return

        # Priority 2: calibration folder for colour cam intrinsics only
        intr = self._load_lens_intrinsics(resolved)
        if intr is not None:
            cfx, cfy, ccx, ccy = intr
            with self._lock:
                self._color_fx = cfx; self._color_fy = cfy
                self._color_cx = ccx; self._color_cy = ccy
            log(f"[DepthColorize] Auto-loaded colour intrinsics for {resolved}")
            if self._sio:
                self._sio.emit("dc_params_event", {
                    "cam_id":   self._cam_id, "ok": True, "source": "lens_cal",
                    "color_fx": cfx, "color_fy": cfy,
                    "color_cx": ccx, "color_cy": ccy,
                })

    def _save_dc_params(self) -> dict:
        """Save current params.
        Dual-camera (colour cam known): saves to stereo folder (KL/KR/R/T format).
        Single-camera: saves to calibration folder (camera_matrix format).
        """
        with self._lock:
            cam_depth  = self._cam_id
            vertex_on  = self._vertex_color
            cam_color  = (self._color_cam or self._auto_color_cam()) if vertex_on else ""
            fx  = self._fx;  fy  = self._fy;  cx  = self._cx;  cy  = self._cy
            cfx = self._color_fx; cfy = self._color_fy
            ccx = self._color_cx; ccy = self._color_cy
            tx  = self._ext_tx; ty = self._ext_ty; tz = self._ext_tz
            R   = self._ext_R.tolist()

        if not cam_depth:
            return {"cam_id": cam_depth, "ok": False, "error": "no depth cam"}

        ts = datetime.now().isoformat(timespec="seconds")

        if cam_color:
            # Dual-camera mode → stereo folder
            data = {
                "format_version": 1,
                "type":      "dc_stereo",
                "cam_left":  cam_depth,
                "cam_right": cam_color,
                "KL": [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
                "KR": [[cfx, 0.0, ccx], [0.0, cfy, ccy], [0.0, 0.0, 1.0]],
                "T":  [tx, ty, tz],   # mm
                "R":  R,
                "saved_at": ts,
            }
            path = self._stereo_params_path(cam_depth, cam_color)
            os.makedirs(_STEREO_DIR, exist_ok=True)
        else:
            # Single-camera mode → calibration folder (camera_matrix format)
            data = {
                "format_version": 1,
                "type":          "dc_intrinsics",
                "lens_type":     "normal",
                "camera_matrix": [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
                "dist_coeffs":   [0.0, 0.0, 0.0, 0.0, 0.0],
                "saved_at": ts,
            }
            path = os.path.join(_CALIB_DIR, f"{self._dc_safe(cam_depth)}.json")
            os.makedirs(_CALIB_DIR, exist_ok=True)

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log(f"[DepthColorize] Params saved: {path}")
            return {"cam_id": cam_depth, "ok": True, "source": "saved",
                    "filename": os.path.basename(path)}
        except Exception as e:
            return {"cam_id": cam_depth, "ok": False, "error": str(e)}

    # ── PLY helpers ───────────────────────────────────────────────────────────

    def _generate_ply(self) -> dict:
        if self._state is None:
            return {"ok": False, "error": "Plugin not attached to state"}

        drv = self._state.get_driver(self._cam_id)
        if drv is None or drv.raw_frame_format != "Z16":
            return {"ok": False, "error": "No Z16 raw data available"}

        raw = drv.latest_raw_frame
        if raw is None:
            return {"ok": False, "error": "No frame captured yet"}

        with self._lock:
            depth_scale  = self._depth_scale
            auto_range   = self._auto_range
            clip_min     = self._clip_min
            clip_max     = self._clip_max
            fx  = self._fx;  fy  = self._fy
            cx  = self._cx;  cy  = self._cy
            vc       = self._vertex_color
            ccam_sel = self._color_cam
            ccam_src = self._color_cam_source
            cfx = self._color_fx; cfy = self._color_fy
            ccx = self._color_cx; ccy = self._color_cy
            tx  = self._ext_tx / 1000.0   # mm → m
            ty  = self._ext_ty / 1000.0
            tz  = self._ext_tz / 1000.0
            R_mat = self._ext_R.copy()

        # Depth range mask
        valid = raw > 0
        if not auto_range:
            lo = clip_min / depth_scale if depth_scale > 0 else 0
            hi = clip_max / depth_scale if depth_scale > 0 else 65535
            valid &= (raw >= lo) & (raw <= hi)

        if not valid.any():
            return {"ok": False, "error": "No valid depth pixels in range"}

        # Back-project to 3D (metres)
        h, w = raw.shape
        uu, vv = np.meshgrid(np.arange(w, dtype=np.float32),
                             np.arange(h, dtype=np.float32))
        z_m = raw.astype(np.float32) * (depth_scale / 1000.0)
        x_m = (uu - cx) * z_m / fx
        y_m = (vv - cy) * z_m / fy

        pts = np.stack([x_m[valid], y_m[valid], z_m[valid]], axis=1)

        # Vertex colour
        rgb = None
        color_note = ""
        if vc:
            ccam = ccam_sel if ccam_sel else self._auto_color_cam()
            if ccam:
                if ccam_src == "display":
                    color_frame = self._state.get_display_frame(ccam)
                else:
                    color_frame = self._state.get_latest_frame(ccam)
                if color_frame is not None:
                    rgb = self._sample_color(
                        x_m[valid], y_m[valid], z_m[valid],
                        R_mat, tx, ty, tz,
                        cfx, cfy, ccx, ccy,
                        color_frame
                    )
                    color_note = f" +RGB from {ccam}"
                else:
                    color_note = " (colour frame unavailable)"
            else:
                color_note = " (no colour camera found)"

        try:
            ply_bytes = _build_ply_xyz_rgb(pts, rgb) if rgb is not None else _build_ply_xyz(pts)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe  = self._dc_safe(self._cam_id)
        fname = f"depth_{safe}_{ts}.ply"
        path  = os.path.join(_DEPTH_PLY_DIR, fname)

        try:
            os.makedirs(_DEPTH_PLY_DIR, exist_ok=True)
            with open(path, "wb") as f:
                f.write(ply_bytes)
        except Exception as e:
            return {"ok": False, "error": f"Save failed: {e}"}

        with self._lock:
            self._ply_bytes = ply_bytes
            self._ply_path  = path

        n = len(pts)
        log(f"[DepthColorize] PLY saved {path}  ({n} pts{color_note})")
        return {"ok": True, "filename": fname, "n_points": n,
                "has_color": rgb is not None, "saved_at": ts}

    @staticmethod
    def _sample_color(x_m, y_m, z_m, R_mat, tx, ty, tz,
                      cfx, cfy, ccx, ccy, bgr_frame) -> np.ndarray:
        """Back-project depth points into colour image; return RGB uint8 Nx3.
        Applies full extrinsic: p_color = R @ p_depth + T  (all in metres).
        """
        ch, cw = bgr_frame.shape[:2]

        pts = np.stack([x_m, y_m, z_m], axis=1).astype(np.float64)   # Nx3 m
        T_vec = np.array([tx, ty, tz], dtype=np.float64)
        pts_c = pts @ R_mat.T + T_vec                                  # Nx3 m

        xc = pts_c[:, 0]; yc = pts_c[:, 1]; zc = pts_c[:, 2]
        uc = (cfx * xc / zc + ccx).astype(np.float32)
        vc = (cfy * yc / zc + ccy).astype(np.float32)

        ui = np.clip(np.round(uc).astype(np.int32), 0, cw - 1)
        vi = np.clip(np.round(vc).astype(np.int32), 0, ch - 1)

        bgr = bgr_frame[vi, ui]
        return bgr[:, ::-1].copy()         # → RGB

    # ── Routes ────────────────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        from flask import request as _req, send_file as _send

        is_admin = ctx["is_admin"]

        @app.route("/plugin/depthcolorize/download_ply")
        def _dc_download_ply():
            cam_id = _req.args.get("cam_id", "")
            inst   = DepthColorize._instances.get(cam_id)
            if inst is None:
                return "Not found", 404
            with inst._lock:
                data = inst._ply_bytes
                path = inst._ply_path
            if data is None:
                return "No PLY available", 404
            fname = os.path.basename(path) if path else "pointcloud.ply"
            return _send(io.BytesIO(data), mimetype="application/octet-stream",
                         as_attachment=True, download_name=fname)

        @sio.on("dc_save_ply")
        def _dc_save_ply(data):
            if not is_admin():
                return
            cam_id = data.get("cam_id", "")
            inst   = DepthColorize._instances.get(cam_id)
            if inst is None:
                sio.emit("dc_event",
                         {"ok": False, "error": "DepthColorize not active"},
                         to=_req.sid)
                return
            sio.emit("dc_event", inst._generate_ply(), to=_req.sid)

        @sio.on("dc_save_params")
        def _dc_save_params(data):
            if not is_admin():
                return
            cam_id = data.get("cam_id", "")
            inst   = DepthColorize._instances.get(cam_id)
            if inst is None:
                sio.emit("dc_params_event",
                         {"cam_id": cam_id, "ok": False, "error": "DepthColorize not active"},
                         to=_req.sid)
                return
            sio.emit("dc_params_event", inst._save_dc_params(), to=_req.sid)

    # ── Parameter dispatch ────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        _float  = lambda v, lo=None: max(lo, float(v)) if lo is not None else float(v)
        _bool   = lambda v: bool(v)

        if key == "dc_color_cam_source":
            src = str(value)
            if src in ("pipeline", "display"):
                with self._lock:
                    self._color_cam_source = src
            return True

        if key.startswith("dc_ext_r") and len(key) == 10:
            try:
                i, j = int(key[8]), int(key[9])
                if 0 <= i < 3 and 0 <= j < 3:
                    with self._lock:
                        self._ext_R[i, j] = float(value)
            except (ValueError, IndexError):
                pass
            return True

        # dc_color_cam: switching mode triggers auto-load
        if key == "dc_color_cam":
            color_cam = str(value)
            with self._lock:
                self._color_cam = color_cam
            self._auto_load_for_color_cam(color_cam)
            return True

        if key == "dc_colormap":
            idx = int(value)
            if 0 <= idx < len(_COLORMAPS):
                with self._lock:
                    self._colormap = _COLORMAPS[idx]
            return True

        mapping = {
            "dc_enabled":     ("_enabled",     _bool),
            "dc_auto_range":  ("_auto_range",  _bool),
            "dc_clip_min":    ("_clip_min",    lambda v: _float(v, 0.0)),
            "dc_clip_max":    ("_clip_max",    lambda v: _float(v, 1.0)),
            "dc_depth_scale": ("_depth_scale", lambda v: float(v) if float(v) > 0 else 0.125),
            "dc_fx":          ("_fx",          lambda v: _float(v, 1.0)),
            "dc_fy":          ("_fy",          lambda v: _float(v, 1.0)),
            "dc_cx":          ("_cx",          float),
            "dc_cy":          ("_cy",          float),
            "dc_vertex_color":("_vertex_color",_bool),
            "dc_color_fx":    ("_color_fx",    lambda v: _float(v, 1.0)),
            "dc_color_fy":    ("_color_fy",    lambda v: _float(v, 1.0)),
            "dc_color_cx":    ("_color_cx",    float),
            "dc_color_cy":    ("_color_cy",    float),
            "dc_ext_tx":      ("_ext_tx",      float),
            "dc_ext_ty":      ("_ext_ty",      float),
            "dc_ext_tz":      ("_ext_tz",      float),
        }
        if key in mapping:
            attr, conv = mapping[key]
            with self._lock:
                setattr(self, attr, conv(value))
            return True
        return False

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        with self._lock:
            cm_idx   = _COLORMAPS.index(self._colormap) if self._colormap in _COLORMAPS else 0
            auto_cam = self._auto_color_cam() if not self._color_cam else ""
            return {
                "dc_enabled":           self._enabled,
                "dc_auto_range":        self._auto_range,
                "dc_clip_min":          self._clip_min,
                "dc_clip_max":          self._clip_max,
                "dc_depth_scale":       self._depth_scale,
                "dc_colormap":          cm_idx,
                "dc_fx":                self._fx,
                "dc_fy":                self._fy,
                "dc_cx":                self._cx,
                "dc_cy":                self._cy,
                "dc_vertex_color":      self._vertex_color,
                "dc_color_cam":         self._color_cam,
                "dc_color_cam_auto":    auto_cam,
                "dc_color_cam_source":  self._color_cam_source,
                "dc_color_fx":          self._color_fx,
                "dc_color_fy":          self._color_fy,
                "dc_color_cx":          self._color_cx,
                "dc_color_cy":          self._color_cy,
                "dc_ext_tx":            self._ext_tx,
                "dc_ext_ty":            self._ext_ty,
                "dc_ext_tz":            self._ext_tz,
                "dc_ext_R":             self._ext_R.flatten().tolist(),
            }
