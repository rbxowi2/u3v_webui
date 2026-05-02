"""cloudrecord/cloudrecord.py — Point Cloud Recorder (1.4.0)

Continuously captures Z16 depth frames as individual files.
No on-device fusion — all processing delegated to desktop.

Modes
-----
free            : capture at configured FPS
turntable_servo : GRBL-driven turntable, UI hidden — code kept for future use

Save modes
----------
pointcloud  : Z16 → backproject → PLY (optional vertex colour embedded)
depthimage  : RTAB-Map compatible RGB-D dataset
              depth/  — 16-bit PNG (16UC1, mm) or EXR (32FC1, metres)
              rgb/    — 8-bit PNG colour frames (if vertex colour enabled)
              depth.txt / rgb.txt / associations.txt  — TUM-format index files
              camera_info.yaml  — ROS-style intrinsics (RTAB-Map compatible)
              metadata.json     — azimuth/elevation per frame

Depth range filter [depth_min_m, depth_max_m]:
  - Applied before backproject / save (out-of-range pixels → 0 = invalid)
  - Also applied as live mask on the display frame (pixels outside range darkened)
  - Auto mode: 5th/95th percentile computed per-frame from raw Z16

Session output
--------------
captures/depth/session_YYYYMMDD_HHMMSS/
  depth/
    1620000000.000000.png/.exr  (one file per frame)
  rgb/                          (only if vertex colour enabled)
    1620000000.000000.png
  depth.txt
  rgb.txt                       (only if vertex colour enabled)
  associations.txt              (only if vertex colour enabled)
  camera_info.yaml
  metadata.json

Intrinsics auto-loaded on camera open (same format as depthcolorize):
  Single camera : calibration/<safe_cam>.json → camera_matrix
  Dual camera   : stereo/<safe_depth>__<safe_color>.json → KL/KR/R/T
"""

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
from .servo_client import GRBLClient

_DEPTH_DIR  = os.path.join(CAPTURE_DIR, "depth")
_CALIB_DIR  = os.path.join(CAPTURE_DIR, "calibration")
_STEREO_DIR = os.path.join(CAPTURE_DIR, "stereo")

_ST_IDLE      = "idle"
_ST_RECORDING = "recording"
_ST_SAVING    = "saving"
_ST_SERVO     = "servo_running"
_ST_ELEVATION = "waiting_elevation"
_ST_ERROR     = "error"
_ST_DONE      = "done"

_ACTIVE_STATES = (_ST_RECORDING, _ST_SAVING, _ST_SERVO, _ST_ELEVATION)


def _safe(s: str) -> str:
    return s.replace("/", "_").replace(":", "_").replace(" ", "_")





class CloudRecord(PluginBase):

    _instances: dict = {}

    def __init__(self):
        self._state      = None
        self._sio        = None
        self._emit_state = None

        self._lock   = threading.Lock()
        self._cam_id = ""

        # ── Config ────────────────────────────────────────────────────────
        self._mode       = "free"
        self._depth_scale = 0.125
        self._capture_fps = 1.0
        self._color_cam        = ""
        self._color_cam_source = "pipeline"   # "pipeline" | "display"

        # Depth range filter (metres)
        self._depth_range_mode = "manual"   # manual | auto
        self._depth_min_m = 0.1
        self._depth_max_m = 5.0

        # Depth intrinsics (SR300 nominal)
        self._fx = 460.0; self._fy = 460.0
        self._cx = 320.0; self._cy = 240.0

        # Colour camera intrinsics + extrinsics
        self._color_fx = 616.0; self._color_fy = 616.0
        self._color_cx = 320.0; self._color_cy = 240.0
        self._ext_R    = np.eye(3, dtype=np.float64)
        self._ext_tx   = 25.0; self._ext_ty = 0.0; self._ext_tz = 0.0

        # ── Servo config (UI hidden, code kept for future use) ─────────────
        self._step_angle = 10.0
        self._elevations = [0.0]
        self._scan_steps: list = []
        self._step_idx   = 0

        self._servo_ip      = "192.168.1.100"
        self._servo_port    = 23
        self._servo_axis    = "A"
        self._servo_feed    = 500.0
        self._servo_dwell   = 0.5
        self._servo_timeout = 10.0
        self._servo_log_max = 50

        # ── Runtime state ──────────────────────────────────────────────────
        self._scan_state = _ST_IDLE
        self._error_msg  = ""

        # ── Session management ─────────────────────────────────────────────
        self._session_dir = ""
        self._frame_idx   = 0
        self._metadata: list = []

        self._calib_written = False

        # ── Threads / sync ─────────────────────────────────────────────────
        self._elevation_event = threading.Event()
        self._error_action    = None
        self._error_event     = threading.Event()
        self._worker_stop     = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_capture    = 0.0

        # ── GRBL ───────────────────────────────────────────────────────────
        self._servo = GRBLClient(log_max=50)
        self._servo.set_log_callback(self._on_servo_log)

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def name(self)        -> str: return "RTAB-Map_record"
    @property
    def version(self)     -> str: return "1.4.0"
    @property
    def description(self) -> str: return "RTAB-Map RGB-D dataset recorder with depth filter and GRBL turntable support"

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def on_load(self):
        os.makedirs(_DEPTH_DIR, exist_ok=True)

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        CloudRecord._instances[cam_id] = self
        intr = self._load_lens_intrinsics(cam_id)
        if intr is not None:
            fx, fy, cx, cy = intr
            with self._lock:
                self._fx = fx; self._fy = fy
                self._cx = cx; self._cy = cy
            log(f"[RTAB-Map_record] Auto-loaded depth intrinsics for {cam_id}")
            if self._sio:
                self._sio.emit("cr_params_event", {
                    "cam_id": cam_id, "ok": True, "source": "lens_cal",
                    "depth_fx": fx, "depth_fy": fy,
                    "depth_cx": cx, "depth_cy": cy,
                })

    def on_camera_close(self, cam_id: str = ""):
        CloudRecord._instances.pop(cam_id, None)
        self._stop_worker()

    # ── Intrinsics loading ─────────────────────────────────────────────────

    @staticmethod
    def _load_lens_intrinsics(cam_id: str):
        if not cam_id:
            return None
        path = os.path.join(_CALIB_DIR, f"{_safe(cam_id)}.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            K = data["camera_matrix"]
            return float(K[0][0]), float(K[1][1]), float(K[0][2]), float(K[1][2])
        except Exception:
            return None

    @staticmethod
    def _stereo_params_path(depth_cam: str, color_cam: str) -> str:
        return os.path.join(_STEREO_DIR,
                            f"{_safe(depth_cam)}__{_safe(color_cam)}.json")

    def _load_stereo_params(self, depth_cam: str, color_cam: str):
        try:
            with open(self._stereo_params_path(depth_cam, color_cam),
                      encoding="utf-8") as f:
                data = json.load(f)
            if "KL" not in data or "KR" not in data or "T" not in data:
                return None
            return data
        except Exception:
            return None

    def _auto_load_for_color_cam(self, color_cam: str):
        if not color_cam:
            return
        params = self._load_stereo_params(self._cam_id, color_cam)
        if not params:
            return
        KL = params["KL"]; KR = params["KR"]
        T  = params["T"]
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
        log(f"[RTAB-Map_record] Auto-loaded stereo params {self._cam_id}→{color_cam}")
        if self._sio:
            with self._lock:
                evt = {
                    "cam_id": self._cam_id, "ok": True, "source": "stereo_cal",
                    "depth_fx":  self._fx,       "depth_fy":  self._fy,
                    "depth_cx":  self._cx,       "depth_cy":  self._cy,
                    "color_fx":  self._color_fx, "color_fy":  self._color_fy,
                    "color_cx":  self._color_cx, "color_cy":  self._color_cy,
                    "ext_tx":    self._ext_tx,   "ext_ty":    self._ext_ty,
                    "ext_tz":    self._ext_tz,
                }
            self._sio.emit("cr_params_event", evt)

    # ── Frame hook ─────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        if cam_id != self._cam_id:
            return None
        with self._lock:
            state = self._scan_state

        out = self._apply_depth_mask(frame)

        if state in _ACTIVE_STATES:
            self._draw_hud(out, state)
        return out

    def _apply_depth_mask(self, frame: np.ndarray) -> np.ndarray:
        """Darken pixels outside the depth range for live visual feedback."""
        if self._state is None:
            return frame.copy()
        drv = self._state.get_driver(self._cam_id)
        if drv is None or drv.raw_frame_format != "Z16":
            return frame.copy()
        raw = drv.latest_raw_frame
        if raw is None:
            return frame.copy()
        fh, fw = frame.shape[:2]
        if raw.shape[0] != fh or raw.shape[1] != fw:
            return frame.copy()
        with self._lock:
            ds   = self._depth_scale
            mode = self._depth_range_mode
            dmin = self._depth_min_m
            dmax = self._depth_max_m
        z_m = raw.astype(np.float32) * (ds / 1000.0)
        if mode == "auto":
            z_v = z_m[raw > 0]
            if len(z_v) >= 10:
                dmin = float(np.percentile(z_v, 5))
                dmax = float(np.percentile(z_v, 95))
        invalid = (raw == 0) | (z_m < dmin) | (z_m > dmax)
        out     = frame.copy()
        out[invalid] = out[invalid] // 3
        return out

    # ── Frame payload (stream-thread delivery) ─────────────────────────────

    def frame_payload(self, cam_id: str = "") -> dict:
        if cam_id != self._cam_id:
            return {}
        with self._lock:
            state  = self._scan_state
            idx    = self._step_idx
            steps  = self._scan_steps
            fidx   = self._frame_idx
            sdir   = self._session_dir
            err    = self._error_msg

        total = len(steps)
        done  = sum(1 for s in steps if s.get("done"))
        ring  = steps[idx]["ring"] + 1 if steps else 0
        rings = len(set(s["ring"] for s in steps)) if steps else 0

        return {
            "cr_status": {
                "cam_id":       self._cam_id,
                "state":        state,
                "step_idx":     idx,
                "step_total":   total,
                "step_done":    done,
                "ring":         ring,
                "rings_total":  rings,
                "frame_idx":    fidx,
                "session_name": os.path.basename(sdir) if sdir else "",
                "error_msg":    err,
            }
        }

    def _draw_hud(self, img: np.ndarray, state: str):
        fidx  = self._frame_idx
        h, w  = img.shape[:2]
        scale = max(0.7, w / 640)

        overlay = img.copy()
        cv2.rectangle(overlay, (0, 0), (w, int(30 * scale + 6)), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.5, img, 0.5, 0, img)

        dot_r = max(5, int(6 * scale))
        dot_x = int(10 * scale)
        dot_y = int(18 * scale)
        dot_color = {
            _ST_RECORDING: (0,  40, 220),
            _ST_SAVING:    (0, 200, 220),
            _ST_SERVO:     (0, 140, 220),
        }.get(state, (130, 130, 130))
        cv2.circle(img, (dot_x, dot_y), dot_r, dot_color, -1)

        label = {
            _ST_RECORDING: "REC",
            _ST_SAVING:    "SAVING",
            _ST_SERVO:     "SERVO",
            _ST_ELEVATION: "ELEV",
            _ST_DONE:      "DONE",
            _ST_ERROR:     "ERROR",
        }.get(state, state.upper())

        cv2.putText(img, label,
                    (dot_x + dot_r + 6, int(24 * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, scale * 0.62,
                    (220, 220, 220), 1, cv2.LINE_AA)
        cv2.putText(img, f"{fidx} frames",
                    (int(w * 0.60), int(24 * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, scale * 0.62,
                    (160, 200, 255), 1, cv2.LINE_AA)

    # ── Worker threads ─────────────────────────────────────────────────────

    def _start_worker(self, target):
        self._worker_stop.clear()
        self._worker = threading.Thread(target=target, daemon=True)
        self._worker.start()

    def _stop_worker(self):
        self._worker_stop.set()
        self._elevation_event.set()
        self._error_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=3.0)
        self._worker = None

    def _free_worker(self):
        while not self._worker_stop.is_set():
            with self._lock:
                fps = self._capture_fps
            interval = 1.0 / max(fps, 0.1)
            now = time.monotonic()
            if now - self._last_capture < interval:
                time.sleep(min(0.02, interval / 4))
                continue
            self._last_capture = now
            self._set_state(_ST_SAVING)
            self._do_capture(az=0.0, el=0.0)
            self._set_state(_ST_RECORDING)

    def _servo_worker(self):
        """Turntable servo worker — UI hidden, kept for future use."""
        ok, msg = self._servo.send_and_wait("G90", timeout=self._servo_timeout)
        if not ok:
            self._handle_error(f"GRBL init G90 failed: {msg}")
            return
        ok, msg = self._servo.send_and_wait(
            f"G92 {self._servo_axis}0", timeout=self._servo_timeout)
        if not ok:
            self._handle_error(f"GRBL init G92 failed: {msg}")
            return

        for i, step in enumerate(self._scan_steps):
            if self._worker_stop.is_set():
                break

            if i > 0 and step["ring"] != self._scan_steps[i - 1]["ring"]:
                self._servo.send_and_wait(
                    f"G1 {self._servo_axis}0 F{self._servo_feed:.0f}",
                    timeout=self._servo_timeout)
                self._set_state(_ST_ELEVATION)
                self._emit_elevation_prompt(step)
                self._elevation_event.clear()
                self._elevation_event.wait()
                if self._worker_stop.is_set():
                    break
                self._servo.send_and_wait(
                    f"G92 {self._servo_axis}0", timeout=self._servo_timeout)

            with self._lock:
                self._step_idx = i
            self._set_state(_ST_SERVO)
            self._emit_status()

            cmd     = f"G1 {self._servo_axis}{step['az']:.3f} F{self._servo_feed:.0f}"
            move_ok = False
            while not self._worker_stop.is_set():
                ok, msg = self._servo.send_and_wait(cmd, timeout=self._servo_timeout)
                if ok:
                    move_ok = True
                    break
                action = self._handle_error(f"GRBL: {msg}")
                if action == "retry":   continue
                elif action == "skip":  break
                else:                   return

            if not move_ok:
                continue

            time.sleep(self._servo_dwell)
            self._set_state(_ST_SAVING)
            self._do_capture(az=step["az"], el=step["elev"])
            with self._lock:
                self._scan_steps[i]["done"] = True
            self._emit_status()

        if not self._worker_stop.is_set():
            self._set_state(_ST_DONE)
            self._emit_status()

    # ── Capture pipeline ───────────────────────────────────────────────────

    def _do_capture(self, az: float, el: float):
        ts_ns = time.time_ns()
        if self._state is None:
            return
        drv = self._state.get_driver(self._cam_id)
        if drv is None or drv.raw_frame_format != "Z16":
            return
        raw = drv.latest_raw_frame
        if raw is None:
            return
        raw = raw.copy()
        self._save_rtabmap_frame(raw, ts_ns, az, el)

    def _register_color(self, raw: np.ndarray, color: np.ndarray,
                        valid_mask: np.ndarray,
                        fx: float, fy: float, cx: float, cy: float,
                        cfx: float, cfy: float, ccx: float, ccy: float,
                        R: np.ndarray, tx: float, ty: float, tz: float,
                        ds: float) -> np.ndarray:
        """Warp color image into depth camera frame via extrinsics."""
        h, w = raw.shape
        uu, vv = np.meshgrid(np.arange(w, dtype=np.float32),
                             np.arange(h, dtype=np.float32))
        z_m = raw.astype(np.float32) * (ds / 1000.0)
        x_m = (uu - cx) * z_m / fx
        y_m = (vv - cy) * z_m / fy

        T_vec = np.array([tx / 1000.0, ty / 1000.0, tz / 1000.0], dtype=np.float64)
        pts   = np.stack([x_m.ravel().astype(np.float64),
                          y_m.ravel().astype(np.float64),
                          z_m.ravel().astype(np.float64)], axis=1)
        pts_c = pts @ R.T + T_vec

        z_c  = pts_c[:, 2]
        safe = z_c > 0
        denom = np.where(safe, z_c, 1.0)
        uc = np.where(safe, cfx * pts_c[:, 0] / denom + ccx, 0.0).reshape(h, w)
        vc = np.where(safe, cfy * pts_c[:, 1] / denom + ccy, 0.0).reshape(h, w)

        ch, cw = color.shape[:2]
        ui = np.clip(np.round(uc).astype(np.int32), 0, cw - 1)
        vi = np.clip(np.round(vc).astype(np.int32), 0, ch - 1)

        registered = color[vi, ui].copy()
        registered[~valid_mask] = 0
        registered[raw == 0]    = 0
        return registered

    def _save_rtabmap_frame(self, raw: np.ndarray, ts_ns: int,
                            az: float, el: float):
        """Save one RGB-D frame in RTAB-Map / TUM-format dataset."""
        ts_s   = ts_ns / 1e9
        ts_str = f"{ts_s:.6f}"

        with self._lock:
            sdir        = self._session_dir
            ccam        = self._color_cam
            ccam_src    = self._color_cam_source
            ds          = self._depth_scale
            mode        = self._depth_range_mode
            dmin        = self._depth_min_m
            dmax        = self._depth_max_m
            fx          = self._fx;  fy = self._fy
            cx          = self._cx;  cy = self._cy
            calib_done  = self._calib_written

        if not sdir:
            return

        # ── Depth range filter (out-of-range → 0 = invalid) ───────────────
        z_mm = raw.astype(np.float32) * ds
        if mode == "auto":
            z_v = z_mm[raw > 0]
            if len(z_v) >= 10:
                dmin = float(np.percentile(z_v, 5))  / 1000.0
                dmax = float(np.percentile(z_v, 95)) / 1000.0
        valid_mask = (raw > 0) & (z_mm / 1000.0 >= dmin) & (z_mm / 1000.0 <= dmax)

        # ── Write calibration on first frame (image size now known) ────────
        if not calib_done:
            h, w = raw.shape
            self._write_calibration(sdir, fx, fy, cx, cy, w, h)
            os.makedirs(os.path.join(sdir, "rgb"), exist_ok=True)
            for fname in ("rgb.txt", "associations.txt"):
                hdr = "# timestamp filename\n" if fname == "rgb.txt" \
                      else "# ts_rgb rgb_file ts_depth depth_file\n"
                try:
                    with open(os.path.join(sdir, fname), "w") as f:
                        f.write(hdr)
                except Exception:
                    pass
            with self._lock:
                self._calib_written = True

        # ── Save depth image ───────────────────────────────────────────────
        depth_dir = os.path.join(sdir, "depth")
        try:
            depth_u16 = z_mm.clip(0, 65535).astype(np.uint16)
            depth_u16[~valid_mask] = 0
            depth_fname = f"{ts_str}.png"
            cv2.imwrite(os.path.join(depth_dir, depth_fname), depth_u16)
        except Exception as e:
            log(f"[RTAB-Map_record] depth save failed: {e}")
            return

        with open(os.path.join(sdir, "depth.txt"), "a") as f:
            f.write(f"{ts_str} depth/{depth_fname}\n")

        # ── Save registered RGB frame ──────────────────────────────────────
        rgb_saved = False
        if ccam and self._state:
            if ccam_src == "display":
                cf = self._state.get_display_frame(ccam)
            else:
                cf = self._state.get_latest_frame(ccam)
            if cf is not None:
                with self._lock:
                    cfx = self._color_fx; cfy = self._color_fy
                    ccx = self._color_cx; ccy = self._color_cy
                    tx  = self._ext_tx;   ty  = self._ext_ty;  tz = self._ext_tz
                    R   = self._ext_R.copy()
                registered = self._register_color(
                    raw, cf, valid_mask,
                    fx, fy, cx, cy, cfx, cfy, ccx, ccy, R, tx, ty, tz, ds)
                rgb_fname = f"{ts_str}.png"
                try:
                    cv2.imwrite(os.path.join(sdir, "rgb", rgb_fname), registered)
                    with open(os.path.join(sdir, "rgb.txt"), "a") as f:
                        f.write(f"{ts_str} rgb/{rgb_fname}\n")
                    with open(os.path.join(sdir, "associations.txt"), "a") as f:
                        f.write(f"{ts_str} rgb/{rgb_fname} "
                                f"{ts_str} depth/{depth_fname}\n")
                    rgb_saved = True
                except Exception as e:
                    log(f"[RTAB-Map_record] RGB save failed: {e}")

        # ── metadata.json ──────────────────────────────────────────────────
        n_valid = int(valid_mask.sum())
        record  = {
            "frame":         self._frame_idx,
            "timestamp_ns":  ts_ns,
            "timestamp_s":   round(ts_s, 6),
            "azimuth_deg":   round(az, 4),
            "elevation_deg": round(el, 4),
            "n_valid_pixels": n_valid,
            "depth_file":    f"depth/{depth_fname}",
        }
        if rgb_saved:
            record["rgb_file"] = f"rgb/{ts_str}.png"
        with self._lock:
            self._frame_idx += 1
            self._metadata.append(record)
        self._flush_metadata()

    def _write_calibration(self, sdir: str,
                           fx: float, fy: float, cx: float, cy: float,
                           w: int, h: int):
        """Write OpenCV FileStorage YAML readable by RTAB-Map calibration loader."""
        content = (
            f"%YAML:1.0\n"
            f"---\n"
            f"image_width: {w}\n"
            f"image_height: {h}\n"
            f"camera_name: depth\n"
            f"camera_matrix:\n"
            f"   rows: 3\n"
            f"   cols: 3\n"
            f"   dt: d\n"
            f"   data: [ {fx:.8e}, 0., {cx:.8e}, 0., {fy:.8e}, {cy:.8e}, 0., 0., 1. ]\n"
            f"distortion_model: plumb_bob\n"
            f"distortion_coefficients:\n"
            f"   rows: 1\n"
            f"   cols: 5\n"
            f"   dt: d\n"
            f"   data: [ 0., 0., 0., 0., 0. ]\n"
            f"rectification_matrix:\n"
            f"   rows: 3\n"
            f"   cols: 3\n"
            f"   dt: d\n"
            f"   data: [ 1., 0., 0., 0., 1., 0., 0., 0., 1. ]\n"
            f"projection_matrix:\n"
            f"   rows: 3\n"
            f"   cols: 4\n"
            f"   dt: d\n"
            f"   data: [ {fx:.8e}, 0., {cx:.8e}, 0., 0., {fy:.8e}, {cy:.8e}, 0., 0., 0., 1., 0. ]\n"
            f"depth_factor: 1\n"
        )
        try:
            with open(os.path.join(sdir, "camera_info.yaml"), "w",
                      encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            log(f"[RTAB-Map_record] calibration write failed: {e}")

    # ── Session / PLY save ─────────────────────────────────────────────────

    def _start_session(self):
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        sdir = os.path.join(_DEPTH_DIR, f"session_{ts}")
        try:
            os.makedirs(os.path.join(sdir, "depth"), exist_ok=True)
        except Exception as e:
            log(f"[RTAB-Map_record] cannot create session dir: {e}")
            sdir = ""
        if sdir:
            try:
                with open(os.path.join(sdir, "depth.txt"), "w") as f:
                    f.write("# timestamp filename\n")
            except Exception:
                pass
        with self._lock:
            self._session_dir   = sdir
            self._frame_idx     = 0
            self._metadata      = []
            self._calib_written = False
        if sdir:
            log(f"[RTAB-Map_record] session started: {sdir}")

    def _flush_metadata(self):
        with self._lock:
            sdir = self._session_dir
            meta = list(self._metadata)
        if not sdir:
            return
        try:
            with open(os.path.join(sdir, "metadata.json"), "w",
                      encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            log(f"[RTAB-Map_record] metadata flush failed: {e}")

    # ── State helpers ──────────────────────────────────────────────────────

    def _set_state(self, s: str):
        with self._lock:
            self._scan_state = s

    def _build_scan_plan(self):
        steps = []
        for ring_idx, elev in enumerate(self._elevations):
            az = 0.0
            while az < 360.0 - self._step_angle * 0.1:
                steps.append({"ring": ring_idx, "elev": float(elev),
                               "az": round(az, 4), "done": False})
                az += self._step_angle
        return steps

    # ── Error flow ─────────────────────────────────────────────────────────

    def _handle_error(self, msg: str) -> str:
        with self._lock:
            self._scan_state = _ST_ERROR
            self._error_msg  = msg
        self._error_event.clear()
        self._emit_event("error", msg)
        self._error_event.wait()
        with self._lock:
            action = self._error_action or "abort"
        return action

    # ── Servo log callback ─────────────────────────────────────────────────

    def _on_servo_log(self, log_list: list):
        if self._sio:
            try:
                self._sio.emit("cr_servo_log",
                               {"cam_id": self._cam_id, "lines": log_list})
            except Exception:
                pass

    # ── Emit helpers ───────────────────────────────────────────────────────

    def _emit_status(self):
        if not self._sio:
            return
        payload = self.frame_payload(self._cam_id).get("cr_status", {})
        try:
            self._sio.emit("cr_status", payload)
        except Exception:
            pass

    def _emit_event(self, kind: str, msg: str):
        if self._sio:
            try:
                self._sio.emit("cr_event",
                               {"cam_id": self._cam_id, "kind": kind, "msg": msg})
            except Exception:
                pass

    def _emit_elevation_prompt(self, step: dict):
        if self._sio:
            try:
                self._sio.emit("cr_elevation_prompt", {
                    "cam_id":    self._cam_id,
                    "elevation": step["elev"],
                    "ring":      step["ring"] + 1,
                })
            except Exception:
                pass

    # ── Routes ─────────────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        from flask import request as _req

        is_admin = ctx["is_admin"]

        @sio.on("cr_start")
        def _cr_start(data):
            if not is_admin(): return
            inst = CloudRecord._instances.get(data.get("cam_id", ""))
            if inst: inst._do_start()

        @sio.on("cr_capture")
        def _cr_capture(data):
            if not is_admin(): return
            inst = CloudRecord._instances.get(data.get("cam_id", ""))
            if inst: inst._do_single_capture()

        @sio.on("cr_save_params")
        def _cr_save_params(data):
            if not is_admin(): return
            inst = CloudRecord._instances.get(data.get("cam_id", ""))
            if inst: inst._do_save_params()

        @sio.on("cr_stop")
        def _cr_stop(data):
            if not is_admin(): return
            inst = CloudRecord._instances.get(data.get("cam_id", ""))
            if inst:
                inst._stop_worker()
                inst._set_state(_ST_IDLE)
                inst._emit_status()

        @sio.on("cr_confirm_elevation")
        def _cr_elev(data):
            if not is_admin(): return
            inst = CloudRecord._instances.get(data.get("cam_id", ""))
            if inst:
                inst._set_state(_ST_SERVO)
                inst._elevation_event.set()

        @sio.on("cr_error_action")
        def _cr_err(data):
            if not is_admin(): return
            cam_id = data.get("cam_id", "")
            action = data.get("action", "abort")
            inst   = CloudRecord._instances.get(cam_id)
            if inst:
                with inst._lock:
                    inst._error_action = action
                    if action == "abort":
                        inst._scan_state = _ST_IDLE
                inst._error_event.set()
                if action == "abort":
                    inst._stop_worker()
                    inst._set_state(_ST_IDLE)
                    inst._emit_status()

        @sio.on("cr_servo_connect")
        def _cr_conn(data):
            if not is_admin(): return
            inst = CloudRecord._instances.get(data.get("cam_id", ""))
            if inst is None: return
            ok, msg = inst._servo.connect(
                data.get("ip", inst._servo_ip),
                int(data.get("port", inst._servo_port)))
            sio.emit("cr_servo_status",
                     {"cam_id": inst._cam_id, "connected": ok, "msg": msg},
                     to=_req.sid)

        @sio.on("cr_servo_disconnect")
        def _cr_disc(data):
            if not is_admin(): return
            inst = CloudRecord._instances.get(data.get("cam_id", ""))
            if inst:
                inst._servo.disconnect()
                sio.emit("cr_servo_status",
                         {"cam_id": inst._cam_id, "connected": False,
                          "msg": "Disconnected"},
                         to=_req.sid)

    # ── Start / single capture ────────────────────────────────────────────

    def _validate_color_cam(self) -> str:
        with self._lock:
            cam = self._color_cam
        if not cam:
            return "Select colour camera first"
        return ""

    def _do_save_params(self):
        """Write current intrinsics/extrinsics to calibration JSON files."""
        with self._lock:
            cam_id    = self._cam_id
            color_cam = self._color_cam
            fx  = self._fx;  fy  = self._fy;  cx  = self._cx;  cy  = self._cy
            cfx = self._color_fx; cfy = self._color_fy
            ccx = self._color_cx; ccy = self._color_cy
            R   = self._ext_R.tolist()
            tx  = self._ext_tx; ty = self._ext_ty; tz = self._ext_tz
        try:
            os.makedirs(_CALIB_DIR, exist_ok=True)
            depth_path = os.path.join(_CALIB_DIR, f"{_safe(cam_id)}.json")
            with open(depth_path, "w", encoding="utf-8") as f:
                json.dump({"camera_matrix": [[fx,0.0,cx],[0.0,fy,cy],[0.0,0.0,1.0]]},
                          f, indent=2)
            log(f"[RTAB-Map_record] Saved depth intrinsics → {depth_path}")
        except Exception as e:
            self._emit_event("warn", f"Depth save failed: {e}")
            return
        if color_cam:
            try:
                os.makedirs(_STEREO_DIR, exist_ok=True)
                stereo_path = self._stereo_params_path(cam_id, color_cam)
                with open(stereo_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "KL": [[fx,0.0,cx],[0.0,fy,cy],[0.0,0.0,1.0]],
                        "KR": [[cfx,0.0,ccx],[0.0,cfy,ccy],[0.0,0.0,1.0]],
                        "R":  R,
                        "T":  [tx, ty, tz],
                    }, f, indent=2)
                log(f"[RTAB-Map_record] Saved stereo params → {stereo_path}")
            except Exception as e:
                self._emit_event("warn", f"Stereo save failed: {e}")
                return
        self._emit_event("info", "Parameters saved")

    def _do_single_capture(self):
        msg = self._validate_color_cam()
        if msg:
            self._emit_event("warn", msg)
            return
        with self._lock:
            sdir  = self._session_dir
            state = self._scan_state
        if not sdir or state in (_ST_DONE, _ST_ERROR, _ST_IDLE):
            self._start_session()
        self._do_capture(az=0.0, el=0.0)
        self._emit_status()

    def _do_start(self):
        msg = self._validate_color_cam()
        if msg:
            self._emit_event("warn", msg)
            return

        with self._lock:
            mode = self._mode
            if self._scan_state not in (_ST_IDLE, _ST_DONE, _ST_ERROR):
                log(f"[RTAB-Map_record] Start ignored — state is {self._scan_state}")
                return

        self._stop_worker()

        with self._lock:
            self._scan_steps = self._build_scan_plan()
            self._step_idx   = 0

        self._start_session()
        log(f"[RTAB-Map_record] Starting mode={mode}")

        if mode == "free":
            self._last_capture = 0.0
            self._set_state(_ST_RECORDING)
            self._start_worker(self._free_worker)

        elif mode == "turntable_servo":
            if not self._servo.is_connected():
                self._emit_event("error", "GRBL not connected.")
                return
            self._set_state(_ST_SERVO)
            self._start_worker(self._servo_worker)

        self._emit_status()

    # ── Parameter dispatch ─────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        def _f(v, lo=None): return max(lo, float(v)) if lo is not None else float(v)
        def _b(v): return bool(v)
        def _i(v): return int(v)

        if key == "cr_elevations":
            try:
                if isinstance(value, str):
                    els = [float(x.strip()) for x in value.split(",") if x.strip()]
                else:
                    els = [float(x) for x in value]
                with self._lock:
                    self._elevations = els if els else [0.0]
            except Exception:
                pass
            return True

        if key == "cr_color_cam":
            with self._lock:
                self._color_cam = str(value)
            self._auto_load_for_color_cam(str(value))
            return True

        if key == "cr_color_cam_source":
            v = str(value)
            with self._lock:
                self._color_cam_source = v if v in ("pipeline", "display") else "pipeline"
            return True

        mapping = {
            "cr_mode":               ("_mode",               str),
            "cr_depth_scale":        ("_depth_scale",        lambda v: float(v) if float(v) > 0 else 0.125),
            "cr_capture_fps":        ("_capture_fps",        lambda v: _f(v, 0.1)),
            "cr_depth_range_mode":   ("_depth_range_mode",   str),
            "cr_depth_min":          ("_depth_min_m",        lambda v: max(0.0,  float(v)) / 1000.0),
            "cr_depth_max":          ("_depth_max_m",        lambda v: max(0.01, float(v)) / 1000.0),
            "cr_step_angle":         ("_step_angle",         lambda v: max(1.0, _f(v))),
            "cr_fx":                 ("_fx",                 lambda v: _f(v, 1.0)),
            "cr_fy":                 ("_fy",                 lambda v: _f(v, 1.0)),
            "cr_cx":                 ("_cx",                 float),
            "cr_cy":                 ("_cy",                 float),
            "cr_color_fx":           ("_color_fx",           lambda v: _f(v, 1.0)),
            "cr_color_fy":           ("_color_fy",           lambda v: _f(v, 1.0)),
            "cr_color_cx":           ("_color_cx",           float),
            "cr_color_cy":           ("_color_cy",           float),
            "cr_ext_tx":             ("_ext_tx",             float),
            "cr_ext_ty":             ("_ext_ty",             float),
            "cr_ext_tz":             ("_ext_tz",             float),
            "cr_servo_ip":           ("_servo_ip",           str),
            "cr_servo_port":         ("_servo_port",         lambda v: max(1, _i(v))),
            "cr_servo_axis":         ("_servo_axis",         str),
            "cr_servo_feed":         ("_servo_feed",         lambda v: _f(v, 1.0)),
            "cr_servo_dwell":        ("_servo_dwell",        lambda v: _f(v, 0.0)),
            "cr_servo_timeout":      ("_servo_timeout",      lambda v: _f(v, 1.0)),
            "cr_servo_log_max":      ("_servo_log_max",      lambda v: max(10, _i(v))),
        }
        if key in mapping:
            attr, conv = mapping[key]
            with self._lock:
                setattr(self, attr, conv(value))
            if key == "cr_servo_log_max":
                self._servo.set_log_max(self._servo_log_max)
            return True
        return False

    # ── State snapshot ─────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        with self._lock:
            steps    = self._scan_steps
            elev_str = ",".join(str(e) for e in self._elevations)
            return {
                "cr_mode":               self._mode,
                "cr_depth_scale":        self._depth_scale,
                "cr_capture_fps":        self._capture_fps,
                "cr_color_cam":          self._color_cam,
                "cr_color_cam_source":   self._color_cam_source,
                "cr_depth_range_mode":   self._depth_range_mode,
                "cr_depth_min":          round(self._depth_min_m  * 1000, 1),
                "cr_depth_max":          round(self._depth_max_m  * 1000, 1),
                "cr_step_angle":         self._step_angle,
                "cr_elevations":         elev_str,
                "cr_fx":                 self._fx,
                "cr_fy":                 self._fy,
                "cr_cx":                 self._cx,
                "cr_cy":                 self._cy,
                "cr_color_fx":           self._color_fx,
                "cr_color_fy":           self._color_fy,
                "cr_color_cx":           self._color_cx,
                "cr_color_cy":           self._color_cy,
                "cr_ext_tx":             self._ext_tx,
                "cr_ext_ty":             self._ext_ty,
                "cr_ext_tz":             self._ext_tz,
                "cr_servo_ip":           self._servo_ip,
                "cr_servo_port":         self._servo_port,
                "cr_servo_axis":         self._servo_axis,
                "cr_servo_feed":         self._servo_feed,
                "cr_servo_dwell":        self._servo_dwell,
                "cr_servo_timeout":      self._servo_timeout,
                "cr_servo_log_max":      self._servo_log_max,
                "cr_scan_state":         self._scan_state,
                "cr_step_total":         len(steps),
                "cr_frame_idx":          self._frame_idx,
                "cr_session_name":       os.path.basename(self._session_dir) if self._session_dir else "",
                "cr_servo_connected":    self._servo.is_connected(),
            }
