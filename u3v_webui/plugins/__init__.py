"""
plugins/__init__.py — PluginManager: runtime plugin registry (6.13.0).

Plugin discovery contract
-------------------------
Each plugin subfolder must contain a plugin.py that exposes:

    PLUGIN_CLASS          : type[PluginBase]
    PLUGIN_NAME           : str               — unique identifier
    PLUGIN_VERSION        : str               (optional, default "0.1.0")
    PLUGIN_DESCRIPTION    : str               (optional, default "")
    PLUGIN_ALLOW_MULTIPLE : bool              (optional, default False)

Plugin pipeline
---------------
Every camera has an ordered pipeline list.  Each entry has:

    name         : str                     — plugin class name
    instance_key : str                     — unique key within _local[cam_id]
                                             equals name for single-instance plugins
    mode         : "pipeline" | "display"

Two-phase execution
-------------------
Frames are processed in two sequential passes over the ordered pipeline list:

  Phase 1 — pipeline plugins (in list order):
    on_frame modifies the shared pipeline_frame.  The result is stored and
    used for recording / photo.

  Phase 2 — display plugins (in list order):
    on_frame starts from a copy of the final pipeline_frame and applies
    display-only effects.  The result is used only for streaming.

This guarantees display plugins always see the fully-processed pipeline_frame
as their base, regardless of how pipeline and display plugins are interleaved
in the pipeline list.

Cross-camera frame access
-------------------------
Plugins may declare `self._state = None`.  PluginManager injects the
AppState instance, giving direct access to get_latest_frame() and
get_display_frame() for any open camera.  A plugin added to one camera can
read frames from all other cameras without being replicated across pipelines.

Frame payload
-------------
Plugins can attach per-frame metadata to the "frame" SocketIO event by
implementing frame_payload(cam_id) → dict.

Users can drag-reorder plugins in the sidebar, which changes the execution
order within each phase.  The backend stores this order per camera in
_pipeline[cam_id].
"""

import importlib
import inspect
import os
import pathlib
import threading
from typing import Dict, List, Optional

import numpy as np

from .base import PluginBase
from ..utils import log


class PluginManager:
    """Runtime plugin registry for the 6.5.0 multi-camera architecture."""

    def __init__(self):
        self._lock = threading.Lock()

        # Scanned (available) plugins: name → {cls, version, description, dir, ...}
        self._available: dict = {}

        # Active instances: cam_id → {instance_key → inst}
        self._local:  Dict[str, Dict[str, PluginBase]] = {}

        # Per-camera ordered pipeline:
        # cam_id → [{"name": str, "instance_key": str, "mode": "pipeline"|"display"}]
        self._pipeline: Dict[str, list] = {}

        # Injected at register_routes time
        self._sio         = None
        self._emit_state  = None
        self._state       = None

    # ── Discovery ──────────────────────────────────────────────────────────────

    def scan(self):
        """Scan plugins/*/plugin.py and collect available plugin metadata."""
        plugins_dir = pathlib.Path(__file__).parent
        for plugin_dir in sorted(plugins_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("_"):
                continue
            if not (plugin_dir / "plugin.py").exists():
                continue
            mod_name = f"{__package__}.{plugin_dir.name}.plugin"
            try:
                mod            = importlib.import_module(mod_name)
                cls            = getattr(mod, "PLUGIN_CLASS",          None)
                name           = getattr(mod, "PLUGIN_NAME",           None)
                ver            = getattr(mod, "PLUGIN_VERSION",        "0.1.0")
                desc           = getattr(mod, "PLUGIN_DESCRIPTION",    "")
                allow_multiple = getattr(mod, "PLUGIN_ALLOW_MULTIPLE", False)
                default_mode   = getattr(mod, "PLUGIN_MODE",           "pipeline")
                if default_mode not in ("pipeline", "display"):
                    default_mode = "pipeline"
                if cls is None or name is None:
                    log(f"[Plugin] {plugin_dir.name}: missing PLUGIN_CLASS/PLUGIN_NAME — skipped")
                    continue
                cls_dir = str(os.path.dirname(os.path.abspath(inspect.getfile(cls))))
                self._available[name] = {
                    "cls": cls, "version": ver,
                    "description": desc, "dir": cls_dir,
                    "allow_multiple": allow_multiple,
                    "default_mode":   default_mode,
                }
                log(f"[Plugin] Available: {name} v{ver}")
            except Exception as e:
                log(f"[Plugin] Failed to scan {plugin_dir.name}: {e}")

    # ── Plugin add / remove ────────────────────────────────────────────────────

    def add_plugin(self, plugin_name: str, cam_id: str = "") -> tuple:
        """
        Instantiate and activate a plugin for cam_id.

        cam_id is required for all plugins.  Plugins that need cross-camera
        data access declare self._state = None; PluginManager injects the
        AppState instance so the plugin can call get_latest_frame() and
        get_display_frame() for any camera.

        Plugins with PLUGIN_ALLOW_MULTIPLE=True may be added multiple times
        per camera; each gets an auto-generated instance_key.
        """
        info = self._available.get(plugin_name)
        if info is None:
            return False, f"Unknown plugin: {plugin_name}"
        if not cam_id:
            return False, "cam_id required"
        cls            = info["cls"]
        allow_multiple = info.get("allow_multiple", False)
        default_mode   = info.get("default_mode", "pipeline")

        with self._lock:
            self._local.setdefault(cam_id, {})
            if not allow_multiple:
                if plugin_name in self._local[cam_id]:
                    return False, f"{plugin_name} already active for {cam_id}"
                instance_key = plugin_name
            else:
                existing = set(self._local[cam_id].keys())
                if plugin_name not in existing:
                    instance_key = plugin_name
                else:
                    n = 2
                    while f"{plugin_name}_{n}" in existing:
                        n += 1
                    instance_key = f"{plugin_name}_{n}"
            instance = cls()
            instance._instance_key = instance_key
            self._local[cam_id][instance_key] = instance
            if cam_id not in self._pipeline:
                self._pipeline[cam_id] = []
            self._pipeline[cam_id].append({
                "name": plugin_name, "instance_key": instance_key,
                "mode": default_mode,
            })

        self._inject_ctx(instance)

        try:
            instance.on_load()
        except Exception as e:
            log(f"[Plugin] {plugin_name} on_load error: {e}")

        if self._state is not None and cam_id in self._state._cam_infos:
            drv = self._state.get_driver(cam_id)
            try:
                instance.on_camera_open(self._state._cam_infos[cam_id], cam_id, drv)
            except Exception as e:
                log(f"[Plugin] {plugin_name} on_camera_open error: {e}")

        log(f"[Plugin] Added: {plugin_name} [{cam_id}]")
        return True, f"Plugin {plugin_name} added"

    def remove_plugin(self, plugin_name: str, cam_id: str = "",
                      instance_key: str = "") -> tuple:
        """Remove and deactivate a plugin instance."""
        ikey = instance_key or plugin_name

        with self._lock:
            instance = self._local.get(cam_id, {}).pop(ikey, None)
            if cam_id in self._pipeline:
                self._pipeline[cam_id] = [
                    e for e in self._pipeline[cam_id]
                    if e.get("instance_key", e["name"]) != ikey
                ]

        if instance is None:
            return False, f"{plugin_name} ({ikey}) not active"

        if self._state is not None and cam_id:
            try:
                instance.on_camera_close(cam_id)
            except Exception as e:
                log(f"[Plugin] {plugin_name} on_camera_close error: {e}")
        try:
            instance.on_unload()
        except Exception as e:
            log(f"[Plugin] {plugin_name} on_unload error: {e}")

        log(f"[Plugin] Removed: {ikey} [{cam_id}]")
        return True, f"Plugin {ikey} removed"

    def unregister_all(self):
        """Unload all active plugins (server shutdown)."""
        with self._lock:
            all_instances = []
            for d in self._local.values():
                all_instances.extend(d.values())
        for p in reversed(all_instances):
            try:
                p.on_unload()
            except Exception as e:
                log(f"[Plugin] {p.name} on_unload error: {e}")
        with self._lock:
            self._local.clear()
            self._pipeline.clear()

    # ── Camera lifecycle notifications ────────────────────────────────────────

    def notify_camera_open(self, cam_id: str, cam_info: dict):
        """Notify all active plugins that cam_id has opened."""
        with self._lock:
            if cam_id not in self._pipeline:
                self._pipeline[cam_id] = []

        driver = self._state.get_driver(cam_id) if self._state else None
        for p in self._iter_instances(cam_id):
            try:
                p.on_camera_open(cam_info, cam_id, driver)
            except Exception as e:
                log(f"[Plugin] {p.name} on_camera_open error: {e}")

    def notify_camera_close(self, cam_id: str):
        """
        Notify all active plugins that cam_id is closing.

        Plugin instances are KEPT (not unloaded) so they survive a
        close/reopen cycle.  The pipeline is also preserved.
        """
        for p in self._iter_instances(cam_id):
            try:
                p.on_camera_close(cam_id)
            except Exception as e:
                log(f"[Plugin] {p.name} on_camera_close error: {e}")

    # ── Frame pipeline ────────────────────────────────────────────────────────

    def process_frame_for_camera(
        self, frame: np.ndarray, hw_ts_ns: int, cam_id: str
    ) -> tuple:
        """
        Pass frame through the ordered pipeline for cam_id.

        Two-phase execution
        -------------------
        Phase 1 — pipeline plugins (in list order):
            on_frame modifies the shared pipeline_frame.  Affects recording
            and photo capture.

        Phase 2 — display plugins (in list order):
            on_frame starts from a copy of the final pipeline_frame.
            Modifications reach only the stream; recording is unaffected.

        Returns (pipeline_frame, display_frame).
        """
        pipeline_frame = frame

        # Phase 1: pipeline plugins
        for inst, mode in self._iter_pipeline(cam_id):
            if mode != "pipeline":
                continue
            try:
                result = inst.on_frame(pipeline_frame, hw_ts_ns, cam_id)
                if result is not None:
                    pipeline_frame = result
            except Exception as e:
                log(f"[Plugin] {inst.name} on_frame error: {e}")

        # Phase 2: display plugins
        display_frame: Optional[np.ndarray] = None
        for inst, mode in self._iter_pipeline(cam_id):
            if mode != "display":
                continue
            try:
                if display_frame is None:
                    display_frame = pipeline_frame.copy()
                result = inst.on_frame(display_frame, hw_ts_ns, cam_id)
                if result is not None:
                    display_frame = result
            except Exception as e:
                log(f"[Plugin] {inst.name} on_frame error: {e}")

        final_display = display_frame if display_frame is not None else pipeline_frame
        return pipeline_frame, final_display

    # ── State collection ──────────────────────────────────────────────────────

    def collect_state_for_camera(self, cam_id: str) -> dict:
        """Merge get_state() from all active plugins for cam_id."""
        result = {}
        for p in self._iter_instances(cam_id):
            try:
                result.update(p.get_state(cam_id))
            except Exception as e:
                log(f"[Plugin] {p.name} get_state error: {e}")
        return result

    def collect_frame_payload_for_camera(self, cam_id: str) -> dict:
        """Merge frame_payload() from all active plugins for cam_id."""
        result = {}
        for p in self._iter_instances(cam_id):
            try:
                result.update(p.frame_payload(cam_id))
            except Exception as e:
                log(f"[Plugin] {p.name} frame_payload error: {e}")
        return result

    # ── Action / parameter dispatch ───────────────────────────────────────────

    def dispatch_action_for_camera(self, cam_id: str, action: str,
                                    data: dict, driver) -> tuple:
        """Route action to first plugin that handles it."""
        for p in self._iter_instances(cam_id):
            try:
                result = p.handle_action(action, data, driver)
                if result is not None:
                    return result
            except Exception as e:
                log(f"[Plugin] {p.name} handle_action({action}) error: {e}")
        return False, f"No handler for action: {action}"

    def dispatch_set_param_for_camera(self, cam_id: str, key: str,
                                       value, driver) -> bool:
        """Route parameter change to first plugin that claims it."""
        for p in self._iter_instances(cam_id):
            try:
                if p.handle_set_param(key, value, driver):
                    return True
            except Exception as e:
                log(f"[Plugin] {p.name} handle_set_param({key}) error: {e}")
        return False

    # ── Pipeline ordering / mode ──────────────────────────────────────────────

    def reorder_plugins(self, cam_id: str, names: List[str]):
        """Reorder the pipeline for cam_id.  names is a list of instance_keys."""
        with self._lock:
            pipeline = self._pipeline.get(cam_id)
            if pipeline is None:
                return
            key_to_entry = {e.get("instance_key", e["name"]): e for e in pipeline}
            new_pl = [key_to_entry[n] for n in names if n in key_to_entry]
            mentioned = set(names)
            for e in pipeline:
                if e.get("instance_key", e["name"]) not in mentioned:
                    new_pl.append(e)
            self._pipeline[cam_id] = new_pl
        log(f"[Plugin] Pipeline reordered [{cam_id}]: {names}")

    def set_plugin_mode(self, cam_id: str, instance_key: str, mode: str):
        """Set pipeline mode ("pipeline" or "display") for a plugin instance."""
        if mode not in ("pipeline", "display"):
            return
        with self._lock:
            for entry in self._pipeline.get(cam_id, []):
                if entry.get("instance_key", entry["name"]) == instance_key:
                    entry["mode"] = mode
                    break
        log(f"[Plugin] Mode [{cam_id}] {instance_key} → {mode}")

    # ── Busy guard ────────────────────────────────────────────────────────────

    def collect_busy_any(self) -> bool:
        """Return True if any plugin on any camera is busy."""
        with self._lock:
            all_instances = []
            for d in self._local.values():
                all_instances.extend(d.values())
        return any(p.is_busy() for p in all_instances)

    def collect_busy_for_camera(self, cam_id: str) -> bool:
        """Return True if any plugin assigned to cam_id is busy."""
        return any(p.is_busy(cam_id) for p in self._iter_instances(cam_id))

    def _on_plugin_idle(self, cam_id: str = ""):
        """Called by a plugin when it transitions from busy to idle."""
        if self._state is None or self._state.viewer_count > 0:
            return
        if cam_id and not self.collect_busy_for_camera(cam_id):
            threading.Thread(
                target=self._state._auto_close_camera,
                args=(cam_id,),
                daemon=True,
            ).start()

    # ── Introspection ─────────────────────────────────────────────────────────

    def list_available(self) -> list:
        return [
            {
                "name": n, "version": i["version"],
                "description": i["description"],
                "allow_multiple": i.get("allow_multiple", False),
            }
            for n, i in self._available.items()
        ]

    def get_assignments(self) -> dict:
        """Return current plugin assignments: {cam_id: [instance_keys]}."""
        with self._lock:
            return {cam_id: list(d.keys()) for cam_id, d in self._local.items()}

    def get_pipeline_state(self) -> dict:
        """Return full ordered pipeline state per camera.

        Format::
            {cam_id: [{"name": str, "instance_key": str, "mode": str}, ...], ...}
        """
        with self._lock:
            return {cam_id: [dict(e) for e in pl]
                    for cam_id, pl in self._pipeline.items()}

    # ── Route registration ────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        """
        Register Flask routes and SocketIO handlers.

        Flask:    /plugin/<slug>/ui.js  and  /plugin/<slug>/ui.html
        SocketIO: plugin_action, set_param, reorder_plugins, set_plugin_mode
        """
        from flask import abort, send_file, Response

        self._sio        = sio
        self._state      = ctx["state"]
        self._emit_state = ctx["emit_state"]

        is_admin   = ctx["is_admin"]
        emit_state = ctx["emit_state"]
        state      = ctx["state"]

        # Re-inject state into all already-active instances
        for d in self._local.values():
            for inst in d.values():
                self._inject_ctx(inst)

        # ── Static plugin assets ───────────────────────────────────────────

        @app.route("/plugin/<plugin_name>/ui.js", endpoint="plugin_js")
        def _plugin_js(plugin_name):
            info = self._find_by_slug(plugin_name)
            if info:
                path = os.path.join(info["dir"], "ui.js")
                if os.path.exists(path):
                    return send_file(path, mimetype="application/javascript")
            abort(404)

        @app.route("/plugin/<plugin_name>/ui.html", endpoint="plugin_html")
        def _plugin_html(plugin_name):
            info = self._find_by_slug(plugin_name)
            if info:
                path = os.path.join(info["dir"], "ui.html")
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        return Response(f.read(), mimetype="text/html")
            abort(404)

        # ── Generic SocketIO handlers ──────────────────────────────────────

        @sio.on("plugin_action")
        def _plugin_action(data):
            if not is_admin():
                return
            from flask import request as _req
            cam_id = data.get("cam_id") or state.get_selected_cam(_req.sid)
            action = data.get("action", "")
            driver = state.get_driver(cam_id)
            ok, msg = self.dispatch_action_for_camera(cam_id, action, data, driver)
            sio.emit("status", {"msg": msg})
            if ok:
                emit_state()

        @sio.on("set_param")
        def _set_param(data):
            if not is_admin():
                return
            from flask import request as _req
            cam_id = data.get("cam_id") or state.get_selected_cam(_req.sid)
            key    = data.get("key")
            value  = data.get("value")
            if key is None or value is None:
                return
            driver = state.get_driver(cam_id)
            self.dispatch_set_param_for_camera(cam_id, key, value, driver)

        @sio.on("reorder_plugins")
        def _reorder_plugins(data):
            if not is_admin():
                return
            cam_id = data.get("cam_id", "")
            names  = data.get("names", [])
            if cam_id and isinstance(names, list):
                self.reorder_plugins(cam_id, names)
                emit_state()

        @sio.on("set_plugin_mode")
        def _set_plugin_mode(data):
            if not is_admin():
                return
            cam_id       = data.get("cam_id", "")
            instance_key = data.get("instance_key") or data.get("plugin_name", "")
            mode         = data.get("mode", "pipeline")
            if cam_id and instance_key:
                self.set_plugin_mode(cam_id, instance_key, mode)
                emit_state()

        # Call each plugin class's register_routes once for HTTP endpoints
        _reg: set = set()
        for _pname, _pinfo in self._available.items():
            _pcls = _pinfo["cls"]
            if id(_pcls) in _reg:
                continue
            _reg.add(id(_pcls))
            _tmp = _pcls()
            self._inject_ctx(_tmp)
            try:
                _tmp.register_routes(app, sio, ctx)
            except Exception as _e:
                log(f"[Plugin] {_pname} register_routes error: {_e}")

    def list_js_urls(self) -> list:
        """Return JS URLs for all available plugins (pre-loaded at page startup)."""
        urls = []
        for name, info in self._available.items():
            path = os.path.join(info["dir"], "ui.js")
            if os.path.exists(path):
                slug = name.lower().replace(" ", "")
                urls.append(f"/plugin/{slug}/ui.js")
        return urls

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _iter_pipeline(self, cam_id: str) -> list:
        """Return snapshot [(inst, mode), ...] in pipeline order for cam_id."""
        with self._lock:
            pipeline   = list(self._pipeline.get(cam_id, []))
            local_snap = dict(self._local.get(cam_id, {}))
        result = []
        for entry in pipeline:
            name = entry["name"]
            mode = entry["mode"]
            ikey = entry.get("instance_key", name)
            inst = local_snap.get(ikey)
            if inst is not None:
                result.append((inst, mode))
        return result

    def _iter_instances(self, cam_id: str) -> list:
        """Return ordered plugin instances for cam_id (no mode info)."""
        return [inst for inst, _mode in self._iter_pipeline(cam_id)]

    def _find_by_slug(self, slug: str) -> Optional[dict]:
        """Find available plugin by URL slug (normalised lowercase)."""
        slug_norm = slug.lower().replace("-", "").replace("_", "")
        for name, info in self._available.items():
            name_norm = name.lower().replace(" ", "").replace("-", "").replace("_", "")
            if name_norm == slug_norm:
                return info
        return None

    def _inject_ctx(self, instance: PluginBase):
        """Inject sio / emit_state / state / idle callback into plugin instances."""
        if self._sio is not None and hasattr(instance, "_sio"):
            instance._sio = self._sio
        if self._emit_state is not None and hasattr(instance, "_emit_state"):
            instance._emit_state = self._emit_state
        if self._state is not None and hasattr(instance, "_state"):
            instance._state = self._state
        instance._notify_idle = self._on_plugin_idle


# Singleton used by the application
manager = PluginManager()

__all__ = ["PluginBase", "PluginManager", "manager"]
