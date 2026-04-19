"""
streaming.py — StreamManager: per-camera JPEG push threads (6.7.0).

Plugin/core separation (6.7.0):
  streaming.py knows nothing about plugin-specific fields.
  Each plugin contributes extra frame payload fields via frame_payload(cam_id),
  collected by manager.collect_frame_payload_for_camera(cam_id) and merged in.

Per-viewer stream size (6.6.0):
  Each viewer can have a different target resolution.
  Frames with the same target size are encoded once and shared (encode cache).
"""

import base64
import threading
import time

import cv2

from .config import ADAPTIVE_STREAM, STREAM_FPS, STREAM_JPEG_Q


class _CamStreamThread(threading.Thread):
    """Push thread for a single camera."""

    def __init__(self, cam_id: str, state, sio, manager):
        super().__init__(daemon=True)
        self._cam_id  = cam_id
        self._state   = state
        self._sio     = sio
        self._manager = manager
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        interval = 1.0 / STREAM_FPS
        cam_id   = self._cam_id
        state    = self._state
        sio      = self._sio
        manager  = self._manager

        while self._running:
            time.sleep(interval)

            viewers = list(state._viewers)
            if not viewers:
                continue

            drv = state.get_driver(cam_id)
            if drv is None:
                break

            frame = state.get_display_frame(cam_id)
            if frame is None:
                continue

            fh, fw = frame.shape[:2]

            # Plugin-contributed frame metadata — plugins declare their own fields
            frame_meta = manager.collect_frame_payload_for_camera(cam_id) if manager else {}

            # Core payload fields (streaming concerns only)
            base_payload = {
                "cam_id":  cam_id,
                "cap_fps": round(drv.cap_fps, 2),
                **frame_meta,
            }

            # Encode cache: (nw, nh) → base64 string  (encode each unique size once)
            encode_cache: dict = {}

            for sid in viewers:
                if state._stream_paused.get((cam_id, sid), False):
                    continue

                # Determine this viewer's target size
                size_key = (fw, fh)
                if ADAPTIVE_STREAM:
                    target = state._stream_sizes.get((cam_id, sid))
                    if target:
                        tw, th = target
                        if tw < fw or th < fh:
                            scale    = min(tw / fw, th / fh)
                            size_key = (max(1, int(fw * scale)),
                                        max(1, int(fh * scale)))

                if size_key not in encode_cache:
                    nw, nh = size_key
                    scaled = (cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
                              if size_key != (fw, fh) else frame)
                    ok, buf = cv2.imencode(
                        ".jpg", scaled, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_Q]
                    )
                    encode_cache[size_key] = (
                        base64.b64encode(buf.tobytes()).decode("ascii") if ok else None
                    )

                img_b64 = encode_cache[size_key]
                if img_b64 is None:
                    continue

                payload = dict(base_payload)
                payload["img"] = img_b64
                sio.emit("frame", payload, to=sid)


class StreamManager:
    """
    Manages one push thread per open camera.

    Usage::

        stream_mgr = StreamManager()
        stream_mgr.add_stream(cam_id, state, sio, manager)
        stream_mgr.remove_stream(cam_id)
    """

    def __init__(self):
        self._streams: dict = {}  # cam_id → _CamStreamThread
        self._lock = threading.Lock()

    def add_stream(self, cam_id: str, state, sio, manager) -> None:
        """Start a push thread for cam_id.  Replaces a dead thread if present."""
        with self._lock:
            existing = self._streams.get(cam_id)
            if existing is not None and existing.is_alive():
                return
            t = _CamStreamThread(cam_id, state, sio, manager)
            self._streams[cam_id] = t
        t.start()

    def remove_stream(self, cam_id: str) -> None:
        """Stop and discard the push thread for cam_id."""
        with self._lock:
            t = self._streams.pop(cam_id, None)
        if t:
            t.stop()

    def remove_all(self) -> None:
        """Stop all push threads."""
        with self._lock:
            threads = list(self._streams.values())
            self._streams.clear()
        for t in threads:
            t.stop()
