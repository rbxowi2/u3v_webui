"""
state.py — AppState: multi-camera streaming core (6.7.2).

Manages a dict of CameraDriver instances, per-camera frame storage,
session tokens, and viewer tracking.

Ring buffer removed (6.7.0):
  The ring buffer previously stored up to 300 pipeline frames but only the
  most recent one was ever consumed.  Replaced by a single
  _pipeline_frames[cam_id] entry per camera.

Multi-user isolation (6.6.0):
  - _sid_selected_cam  : per-SID camera selection (not global)
  - _stream_sizes      : (cam_id, sid) → (w, h), per-viewer stream size
"""

import secrets
import threading
from typing import Optional

import numpy as np

from .config import CAM_JOIN_TIMEOUT
from .drivers import CameraDriver, DRIVER_MAP, DRIVERS, scan_all_devices
from .utils import log


class AppState:
    """Central runtime state.  One instance lives for the lifetime of the server."""

    def __init__(self):
        self._lock = threading.Lock()

        # ── Multi-camera state ─────────────────────────────────────────────────
        self._cameras: dict   = {}   # cam_id → CameraDriver
        self._cam_infos: dict = {}   # cam_id → info dict

        # Per-SID camera UI selection (which camera the right panel shows).
        # Each connected session maintains its own selection independently.
        self._sid_selected_cam: dict = {}  # sid → cam_id

        # Pending native mode changes: cam_id → {width, height, fps}
        self._pending_native_modes: dict = {}

        # ── Latest frames per camera ───────────────────────────────────────────
        # pipeline frame: output of plugin pipeline (used by recording / photo)
        # display frame:  pipeline frame + display-only plugin effects (used by streaming)
        self._pipeline_frames: dict = {}  # cam_id → np.ndarray
        self._display_frames:  dict = {}  # cam_id → np.ndarray

        # ── Camera scan results ────────────────────────────────────────────────
        self.available_cameras: list = []

        # cam_ids that admin explicitly opened (not manually closed).
        # Preserved through auto-close so the camera auto-reopens on next viewer connect.
        self._auto_reopen_cams: set = set()

        # ── Session tokens ─────────────────────────────────────────────────────
        self._valid_tokens:   set  = set()
        self._token_is_admin: dict = {}
        self._sid_token:      dict = {}
        self._viewers:        set  = set()

        # Per-viewer stream size: (cam_id, sid) → (w, h).
        # Set by the frontend when MDI / mobile viewer resizes.
        # streaming.py uses this to downscale frames per-viewer before JPEG encode.
        self._stream_sizes: dict = {}

        # Per-viewer stream pause: (cam_id, sid) → bool.
        # True = skip sending frames for this (camera, viewer) pair.
        # Used by minimized MDI windows and mobile single-camera mode.
        self._stream_paused: dict = {}

        # Deferred SocketIO reference (set by app.py after sio is created)
        self._sio = None

    # ── Token management ───────────────────────────────────────────────────────

    def create_token(self, is_admin: bool = False) -> str:
        token = secrets.token_hex(16)
        with self._lock:
            self._valid_tokens.add(token)
            self._token_is_admin[token] = is_admin
        return token

    def revoke_token(self, token: str) -> list:
        with self._lock:
            self._valid_tokens.discard(token)
            self._token_is_admin.pop(token, None)
            sids = [sid for sid, t in self._sid_token.items() if t == token]
        return sids

    def is_valid_token(self, token: str) -> bool:
        return token in self._valid_tokens

    # ── Viewer tracking ────────────────────────────────────────────────────────

    def add_viewer(self, sid: str, token: str):
        with self._lock:
            self._viewers.add(sid)
            self._sid_token[sid] = token

    def remove_viewer(self, sid: str):
        with self._lock:
            self._viewers.discard(sid)
            self._sid_token.pop(sid, None)
            self._sid_selected_cam.pop(sid, None)
            # Clean up per-viewer stream sizes for all cameras
            stale = [k for k in self._stream_sizes if k[1] == sid]
            for k in stale:
                del self._stream_sizes[k]
            # Clean up per-viewer stream paused state
            stale_p = [k for k in self._stream_paused if k[1] == sid]
            for k in stale_p:
                del self._stream_paused[k]

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    # ── Per-SID camera selection ───────────────────────────────────────────────

    def get_selected_cam(self, sid: str) -> str:
        """Return the camera currently selected by this SID.

        Falls back to the first open camera only if the previously selected
        camera is no longer present anywhere (neither open nor available).
        Selecting a camera that is available-but-not-open is valid.
        """
        sel = self._sid_selected_cam.get(sid, "")
        if sel:
            known = (
                {c["device_id"] for c in self.available_cameras}
                | set(self._cameras.keys())
            )
            if sel not in known:
                sel = next(iter(self._cameras), "")
                with self._lock:
                    if sel:
                        self._sid_selected_cam[sid] = sel
                    else:
                        self._sid_selected_cam.pop(sid, None)
        return sel

    def set_selected_cam(self, sid: str, cam_id: str):
        """Set the camera selection for a specific SID."""
        with self._lock:
            self._sid_selected_cam[sid] = cam_id

    # ── Auto-close logic ───────────────────────────────────────────────────────

    def try_auto_close_all(self):
        """Auto-close cameras when no viewers remain, per-camera busy check."""
        if self.viewer_count > 0:
            return
        from .plugins import manager
        from .app import stream_mgr
        cam_ids = list(self._cameras.keys())
        if not cam_ids:
            return

        # Cameras with at least one busy plugin stay open unconditionally.
        busy_cams = {c for c in cam_ids if manager.collect_busy_for_camera(c)}

        # Source cameras are protected only when their consumer is also busy.
        # A camera with only MultiView (no busy plugins) provides no protection
        # for its sources — both it and its sources should be auto-closed.
        held: set = set()
        for cam_id in busy_cams:
            held |= manager.collect_held_cam_ids_for_camera(cam_id)

        keep = busy_cams | held
        closed_any = False
        for cam_id in cam_ids:
            if cam_id in keep:
                continue
            log(f"No viewers — auto-closing camera [{cam_id}]")
            stream_mgr.remove_stream(cam_id)
            self._close_camera_internal(cam_id, notify_plugins=True)
            closed_any = True
        if closed_any and self._sio:
            from .app import _emit_state_all
            _emit_state_all()

    def _auto_close_camera(self, cam_id: str):
        """Auto-close one camera after its plugin becomes idle (no viewers)."""
        if self.viewer_count > 0:
            return
        if cam_id not in self._cameras:
            return
        from .plugins import manager
        if manager.collect_busy_for_camera(cam_id):
            return
        log(f"Plugin idle, no viewers — auto-closing camera [{cam_id}]")
        from .app import stream_mgr
        stream_mgr.remove_stream(cam_id)
        self._close_camera_internal(cam_id, notify_plugins=True)
        if self._sio:
            from .app import _emit_state_all
            _emit_state_all()

    def _handle_disconnect(self, cam_id: str):
        """Called from driver thread when camera hardware disconnects unexpectedly."""
        log(f"Camera hardware disconnected [{cam_id}]")
        with self._lock:
            self._auto_reopen_cams.discard(cam_id)
        if cam_id not in self._cameras:
            return
        from .app import stream_mgr
        stream_mgr.remove_stream(cam_id)
        self._close_camera_internal(cam_id, notify_plugins=True)
        if self._sio:
            from .app import _emit_state_all
            _emit_state_all()

    # ── Camera scan ────────────────────────────────────────────────────────────

    def scan_cameras(self) -> list:
        try:
            fresh = scan_all_devices()
        except Exception as e:
            log(f"Camera scan failed: {e}")
            return self.available_cameras

        fresh_ids = {c["device_id"] for c in fresh}
        with self._lock:
            open_ids = set(self._cameras.keys())
        for cam_id in open_ids:
            if cam_id not in fresh_ids:
                old_entry = next(
                    (c for c in self.available_cameras if c["device_id"] == cam_id),
                    None,
                )
                if old_entry:
                    fresh.append(old_entry)
                    log(f"Camera scan: preserved open camera [{cam_id}]")

        self.available_cameras = fresh
        log(f"Camera scan: {len(fresh)} found")
        return fresh

    # ── Camera open / close ────────────────────────────────────────────────────

    def open_camera(self, cam_id: str) -> tuple:
        """Open a specific camera by device_id.  Idempotent (no-op if already open)."""
        with self._lock:
            if cam_id in self._cameras:
                return False, "Camera already open"

        device_entry = next(
            (c for c in self.available_cameras if c["device_id"] == cam_id), None
        )
        if device_entry is None:
            return False, f"Unknown camera: {cam_id}"

        driver_name = device_entry.get("driver", "")
        drv_cls = DRIVER_MAP.get(driver_name) or (DRIVERS[0] if DRIVERS else None)
        if drv_cls is None:
            return False, "No camera driver available"

        drv = drv_cls()
        drv._init_params = dict(drv.DEFAULT_PARAMS)

        with self._lock:
            pending = self._pending_native_modes.pop(cam_id, None)
        if pending:
            drv._init_params.update(pending)

        try:
            info = drv.open(cam_id)
        except Exception as e:
            return False, f"Open failed: {e}"

        try:
            native_modes = drv.query_native_modes()
        except Exception:
            native_modes = []
        info["native_modes"]     = native_modes
        info["supports_audio"]   = drv.supports_audio
        info["default_params"]   = dict(drv.DEFAULT_PARAMS)
        info["supported_params"] = list(drv.SUPPORTED_PARAMS)
        info["cam_id"]           = cam_id

        with self._lock:
            self._cameras[cam_id]   = drv
            self._cam_infos[cam_id] = info

        drv.on_frame      = lambda frame, ts: self._on_frame(frame, ts, cam_id)
        drv.on_disconnect = lambda: threading.Thread(
            target=self._handle_disconnect, args=(cam_id,), daemon=True
        ).start()
        drv.start()

        with self._lock:
            self._auto_reopen_cams.add(cam_id)

        from .plugins import manager
        manager.notify_camera_open(cam_id, info)

        log(f"Camera open  [{cam_id}]  {info['model']}  {info['width']}x{info['height']}")
        return True, f"Connected: {info['model']}"

    def close_camera(self, cam_id: str) -> str:
        """Close a specific camera (admin-initiated) and notify plugins."""
        with self._lock:
            self._auto_reopen_cams.discard(cam_id)
        self._close_camera_internal(cam_id, notify_plugins=True)
        return f"Camera {cam_id} closed"

    def close_all_cameras(self) -> str:
        """Close every open camera (admin-initiated: clears auto-reopen intent)."""
        cam_ids = list(self._cameras.keys())
        with self._lock:
            self._auto_reopen_cams.clear()
        for cid in cam_ids:
            self._close_camera_internal(cid, notify_plugins=True)
        return f"Closed {len(cam_ids)} camera(s)"

    def _close_camera_internal(self, cam_id: str, notify_plugins: bool = True):
        if notify_plugins:
            from .plugins import manager
            manager.notify_camera_close(cam_id)

        with self._lock:
            drv = self._cameras.pop(cam_id, None)
            self._cam_infos.pop(cam_id,       None)
            self._pipeline_frames.pop(cam_id, None)
            self._display_frames.pop(cam_id,  None)

            # Clean up per-viewer stream sizes for this camera
            stale = [k for k in self._stream_sizes if k[0] == cam_id]
            for k in stale:
                del self._stream_sizes[k]
            # Clean up per-viewer stream paused state for this camera
            stale_p = [k for k in self._stream_paused if k[0] == cam_id]
            for k in stale_p:
                del self._stream_paused[k]

            # Reassign per-SID selections that referenced this camera
            remaining = list(self._cameras.keys())
            fallback  = remaining[0] if remaining else ""
            for sid, sel in list(self._sid_selected_cam.items()):
                if sel == cam_id:
                    self._sid_selected_cam[sid] = fallback

        if drv:
            drv.stop()
            drv.join(timeout=CAM_JOIN_TIMEOUT)
        log(f"Camera closed [{cam_id}]")

    # ── Camera accessors ───────────────────────────────────────────────────────

    def get_driver(self, cam_id: str) -> Optional[CameraDriver]:
        return self._cameras.get(cam_id)

    @property
    def open_cam_ids(self) -> list:
        return list(self._cameras.keys())

    # ── Frame callback (called from driver thread) ─────────────────────────────

    def _on_frame(self, frame: np.ndarray, hw_ts_ns: int, cam_id: str):
        from .plugins import manager
        pipeline_frame, display_frame = manager.process_frame_for_camera(
            frame, hw_ts_ns, cam_id
        )
        # Store latest frames — atomic reference replacement, GIL-safe
        self._pipeline_frames[cam_id] = pipeline_frame
        self._display_frames[cam_id]  = display_frame

    # ── Frame accessors ────────────────────────────────────────────────────────

    def get_latest_frame(self, cam_id: str) -> Optional[np.ndarray]:
        """Return the latest pipeline-processed frame (after pipeline plugins)."""
        f = self._pipeline_frames.get(cam_id)
        if f is not None:
            return f.copy()
        drv = self._cameras.get(cam_id)
        return drv.latest_frame if drv else None

    def get_display_frame(self, cam_id: str) -> Optional[np.ndarray]:
        """Return the display frame (pipeline + display-only effects).
        Falls back to pipeline frame if not yet available."""
        f = self._display_frames.get(cam_id)
        if f is not None:
            return f.copy()
        return self.get_latest_frame(cam_id)
