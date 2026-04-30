"""plugins/depthcolorize/depthcolorize.py — DepthColorize plugin (1.2.0)

Display-only plugin: reads Z16 raw depth data from the driver's raw frame
channel and renders it with a user-configurable depth range and colormap.
Also exports point clouds as PLY (XYZ float32, optional RGB vertex colour).

Vertex colour registration
--------------------------
When dc_vertex_color is True a colour camera frame is sampled.
dc_color_cam selects the source; "" = auto (same USB sysfs prefix, ":color"
stream).  The nominal extrinsic is R=I, T=[dc_ext_tx, dc_ext_ty, dc_ext_tz]
(mm).  SR300 nominal: tx=25 mm (colour camera 25 mm to the right of depth).

Parameters (session-only, not persisted to disk)
------------------------------------------------
dc_enabled        bool    Plugin active (default False)
dc_auto_range     bool    Auto-scale each frame; ignores clip_min/max (default True)
dc_clip_min       float   Near clip mm, manual mode (default 200)
dc_clip_max       float   Far clip mm, manual mode (default 1500)
dc_depth_scale    float   mm per Z16 unit (default 0.125 — SR300)
dc_colormap       int     OpenCV colormap 0-8 (default 0 = Jet)
dc_fx/fy/cx/cy    float   Depth intrinsics (default 460/460/320/240 — SR300 nominal)
dc_vertex_color   bool    Include RGB vertex colour in PLY (default False)
dc_color_cam      str     Colour source cam_id, "" = auto (default "")
dc_color_fx/fy    float   Colour camera focal length (default 616 — SR300 nominal)
dc_color_cx/cy    float   Colour camera principal point (default 320/240)
dc_ext_tx/ty/tz   float   Extrinsic translation depth→colour in mm (default 25/0/0)
"""

import io
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
        self._color_cam    = ""       # "" = auto

        # Colour camera intrinsics (SR300 nominal @ 640×480)
        self._color_fx = 616.0
        self._color_fy = 616.0
        self._color_cx = 320.0
        self._color_cy = 240.0

        # Extrinsics depth→colour, R=I, T in mm (SR300 nominal)
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
    def version(self)     -> str: return "1.2.0"
    @property
    def description(self) -> str: return "Re-colorize Z16 depth + PLY export with optional vertex colour"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(_DEPTH_PLY_DIR, exist_ok=True)

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        DepthColorize._instances[cam_id] = self

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

    # ── PLY helpers ───────────────────────────────────────────────────────────

    def _auto_color_cam(self) -> str:
        """Find the matching colour stream for the current depth camera."""
        if self._state is None:
            return ""
        # SR300 cam_id format: "<usb_path>:<stream>"  e.g. "2-1:depth"
        prefix = self._cam_id.rsplit(":", 1)[0] if ":" in self._cam_id else ""
        for cid in self._state.open_cam_ids:
            if cid == self._cam_id:
                continue
            if prefix and not cid.startswith(prefix):
                continue
            if cid.endswith(":color") or "color" in cid.lower():
                return cid
        # Fallback: any other open camera
        for cid in self._state.open_cam_ids:
            if cid != self._cam_id:
                return cid
        return ""

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
            cfx = self._color_fx; cfy = self._color_fy
            ccx = self._color_cx; ccy = self._color_cy
            tx  = self._ext_tx / 1000.0   # mm → m
            ty  = self._ext_ty / 1000.0
            tz  = self._ext_tz / 1000.0

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
                color_frame = self._state.get_latest_frame(ccam)
                if color_frame is not None:
                    rgb = self._sample_color(
                        x_m[valid], y_m[valid], z_m[valid],
                        tx, ty, tz,
                        cfx, cfy, ccx, ccy,
                        color_frame
                    )
                    color_note = f" +RGB from {ccam}"
                else:
                    color_note = " (colour frame unavailable)"
            else:
                color_note = " (no colour camera found)"

        try:
            if rgb is not None:
                ply_bytes = _build_ply_xyz_rgb(pts, rgb)
            else:
                ply_bytes = _build_ply_xyz(pts)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe  = self._cam_id.replace("/", "_").replace(":", "_").replace(" ", "_")
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
    def _sample_color(x_m, y_m, z_m, tx, ty, tz,
                      cfx, cfy, ccx, ccy, bgr_frame) -> np.ndarray:
        """Back-project depth points into colour image; return RGB uint8 Nx3."""
        ch, cw = bgr_frame.shape[:2]

        # Apply extrinsic (R=I, T)
        xc = x_m + tx
        yc = y_m + ty
        zc = z_m + tz

        # Project onto colour image
        uc = (cfx * xc / zc + ccx).astype(np.float32)
        vc = (cfy * yc / zc + ccy).astype(np.float32)

        ui = np.clip(np.round(uc).astype(np.int32), 0, cw - 1)
        vi = np.clip(np.round(vc).astype(np.int32), 0, ch - 1)

        bgr = bgr_frame[vi, ui]           # Nx3  BGR
        rgb = bgr[:, ::-1].copy()         # → RGB
        return rgb

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

    # ── Parameter dispatch ────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        _float  = lambda v, lo=None: max(lo, float(v)) if lo is not None else float(v)
        _bool   = lambda v: bool(v)
        _str    = lambda v: str(v)

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
            "dc_color_cam":   ("_color_cam",   _str),
            "dc_color_fx":    ("_color_fx",    lambda v: _float(v, 1.0)),
            "dc_color_fy":    ("_color_fy",    lambda v: _float(v, 1.0)),
            "dc_color_cx":    ("_color_cx",    float),
            "dc_color_cy":    ("_color_cy",    float),
            "dc_ext_tx":      ("_ext_tx",      float),
            "dc_ext_ty":      ("_ext_ty",      float),
            "dc_ext_tz":      ("_ext_tz",      float),
        }
        if key == "dc_colormap":
            idx = int(value)
            if 0 <= idx < len(_COLORMAPS):
                with self._lock:
                    self._colormap = _COLORMAPS[idx]
            return True
        if key in mapping:
            attr, conv = mapping[key]
            with self._lock:
                setattr(self, attr, conv(value))
            return True
        return False

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        with self._lock:
            cm_idx = _COLORMAPS.index(self._colormap) if self._colormap in _COLORMAPS else 0
            auto_cam = self._auto_color_cam() if not self._color_cam else ""
            return {
                "dc_enabled":      self._enabled,
                "dc_auto_range":   self._auto_range,
                "dc_clip_min":     self._clip_min,
                "dc_clip_max":     self._clip_max,
                "dc_depth_scale":  self._depth_scale,
                "dc_colormap":     cm_idx,
                "dc_fx":           self._fx,
                "dc_fy":           self._fy,
                "dc_cx":           self._cx,
                "dc_cy":           self._cy,
                "dc_vertex_color": self._vertex_color,
                "dc_color_cam":    self._color_cam,
                "dc_color_cam_auto": auto_cam,
                "dc_color_fx":     self._color_fx,
                "dc_color_fy":     self._color_fy,
                "dc_color_cx":     self._color_cx,
                "dc_color_cy":     self._color_cy,
                "dc_ext_tx":       self._ext_tx,
                "dc_ext_ty":       self._ext_ty,
                "dc_ext_tz":       self._ext_tz,
            }
