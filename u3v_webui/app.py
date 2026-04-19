"""
app.py — Flask application, HTTP routes, SocketIO events (6.6.0).

Multi-camera architecture:
  - Each open camera has its own MDI sub-window and stream thread.
  - Plugins are added/removed at runtime per camera (local) or globally.
  - No auto-open on connect; cameras are opened explicitly by admin.

Multi-user isolation:
  - _build_state(sid) returns personalized state with per-SID selected_cam_id.
  - _emit_state_all() sends personalized state to every connected viewer.
  - select_camera event only emits back to the requesting SID (no broadcast).
  - set_stream_size uses (cam_id, sid) key so viewers do not overwrite each other.
  - Per-camera operation locks prevent concurrent open/close/apply_mode races.
"""

import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit  # emit used in SocketIO event handlers

from .config import (
    CERT_FILE, FAIL_MAX_ATTEMPTS, KEY_FILE,
    SECURITY_LOG_FILE,
    TEMP_DIR, VERSION, WEB_PORT, WEB_SECRET_KEY, WEB_USERS,
)
from .security import SecurityManager
from .state import AppState
from .streaming import StreamManager
from .utils import log


# ── Application globals ────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="templates")
app.secret_key = WEB_SECRET_KEY

sio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
               ping_interval=10, ping_timeout=20)

state      = AppState()
security   = SecurityManager()
stream_mgr = StreamManager()

# Wire sio back into state (needed for background-thread emits)
state._sio = sio

# Per-camera operation locks — prevent concurrent open/close/apply_mode on same cam
_cam_op_locks: dict          = {}
_cam_op_locks_meta           = threading.Lock()


@app.after_request
def strip_server_header(resp):
    resp.headers["Server"] = ""
    return resp


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_admin_sid() -> bool:
    token = state._sid_token.get(request.sid, "")
    return state._token_is_admin.get(token, False)


def _get_admin_sids() -> list:
    with state._lock:
        return [sid for sid, tok in state._sid_token.items()
                if state._token_is_admin.get(tok, False)]


def _push_new_notification_to_admins(notif: dict):
    for sid in _get_admin_sids():
        sio.emit("security_notification", notif, to=sid)


def _get_cam_op_lock(cam_id: str) -> threading.Lock:
    """Return (creating if needed) the operation lock for cam_id."""
    with _cam_op_locks_meta:
        if cam_id not in _cam_op_locks:
            _cam_op_locks[cam_id] = threading.Lock()
        return _cam_op_locks[cam_id]


def _do_auto_reopen():
    """Open all cameras that were previously admin-opened, if not already open."""
    from .plugins import manager
    with state._lock:
        to_open = list(state._auto_reopen_cams - set(state._cameras.keys()))
    for cam_id in to_open:
        ok, msg = state.open_camera(cam_id)
        if ok:
            stream_mgr.add_stream(cam_id, state, sio, manager)
            log(f"Auto-reopen [{cam_id}]: {msg}")
        else:
            log(f"Auto-reopen [{cam_id}] failed: {msg}")


def _build_state(sid: str = None) -> dict:
    """Build the full state dict.  sid selects the per-SID camera selection."""
    from .plugins import manager

    cameras_data = {}
    for cam_id in state.open_cam_ids:
        drv      = state.get_driver(cam_id)
        cam_info = state._cam_infos.get(cam_id, {})
        plugin_s = manager.collect_state_for_camera(cam_id)
        entry = {
            "cam_info":         cam_info,
            "cap_fps":          round(drv.cap_fps, 2) if drv else 0.0,
            "current_gain":     round(drv.current_gain, 2) if drv else 0.0,
            "current_exposure": round(drv.current_exposure, 1) if drv else 0.0,
        }
        entry.update(plugin_s)
        cameras_data[cam_id] = entry

    selected = state.get_selected_cam(sid) if sid else ""

    return {
        "selected_cam_id":       selected,
        "cameras":               cameras_data,
        "available_cameras":     state.available_cameras,
        "plugin_assignments":    manager.get_assignments(),
        "plugin_pipeline":       manager.get_pipeline_state(),
        "available_plugins":     manager.list_available(),
        "viewer_count":          state.viewer_count,
        "pending_notifications": security.get_pending_notifications(),
    }


def _emit_state_all():
    """Emit personalized state to every connected viewer."""
    for sid in list(state._viewers):
        sio.emit("state", _build_state(sid), to=sid)


def _read_log() -> list:
    if not os.path.exists(SECURITY_LOG_FILE):
        return []
    try:
        with open(SECURITY_LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


# ── SSL cert generation ────────────────────────────────────────────────────────

def _ensure_ssl_cert(local_ip: str) -> bool:
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return True
    log("Generating self-signed SSL certificate...")
    san      = f"subjectAltName=IP:{local_ip},IP:127.0.0.1"
    base_cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", KEY_FILE, "-out", CERT_FILE,
        "-days", "3650", "-nodes",
        f"-subj=/CN={local_ip}",
        "-addext", san,
    ]
    try:
        subprocess.run(base_cmd, check=True, capture_output=True)
        log(f"SSL certificate generated -> {CERT_FILE}")
        return True
    except Exception:
        pass
    try:
        cfg_path = os.path.join(TEMP_DIR, "_ssl_tmp.cnf")
        with open(cfg_path, "w") as f:
            f.write(f"[req]\ndistinguished_name=req\n[SAN]\n{san}\n")
        alt_cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", KEY_FILE, "-out", CERT_FILE,
            "-days", "3650", "-nodes", f"-subj=/CN={local_ip}",
            "-extensions", "SAN", "-config", cfg_path,
        ]
        subprocess.run(alt_cmd, check=True, capture_output=True)
        os.remove(cfg_path)
        log(f"SSL certificate generated -> {CERT_FILE}")
        return True
    except Exception:
        pass
    log("SSL certificate generation failed — running without HTTPS")
    return False


# ── Login decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── HTTP routes ────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr

    if request.method == "GET":
        session["csrf_token"] = secrets.token_urlsafe(32)
        return render_template("login.html", csrf_token=session["csrf_token"])

    time.sleep(1)

    form_token = request.form.get("csrf_token", "")
    sess_token = session.get("csrf_token", "")
    if not sess_token or not hmac.compare_digest(form_token, sess_token):
        return jsonify({"ok": False, "error": "Invalid request", "count": 0}), 403

    if security.is_blacklisted(ip):
        security.record_blacklisted_attempt(ip)
        return jsonify({"ok": False, "error": "Access denied", "count": 0}), 403

    u = request.form.get("username", "")
    p = request.form.get("password", "")

    user_record = next((rec for rec in WEB_USERS if rec[0] == u and rec[1] == p), None)
    if user_record:
        _, _, is_admin = user_record
        security.record_success(ip)
        token_val = state.create_token(is_admin)
        session["logged_in"]  = True
        session["is_admin"]   = is_admin
        session["token"]      = token_val
        session["csrf_token"] = secrets.token_urlsafe(32)
        return jsonify({"ok": True, "redirect": "/" if is_admin else "/viewer"})

    count, new_notif = security.record_fail(ip)
    if new_notif:
        _push_new_notification_to_admins(new_notif)
    if count >= FAIL_MAX_ATTEMPTS:
        msg = "Too many failures. Your IP has been blocked."
    else:
        msg = f"Wrong username or password. Attempt {count}/{FAIL_MAX_ATTEMPTS}."
    return jsonify({"ok": False, "error": msg, "count": count})


@app.route("/logout")
def logout():
    token = session.get("token")
    ip    = request.remote_addr
    if token:
        sids = state.revoke_token(token)
        security.record_logout(ip)
        log(f"Logout <- {ip}  kicked connections: {len(sids)}")
        def _kick(sids):
            for sid in sids:
                try:
                    sio.disconnect(sid)
                except Exception:
                    pass
        threading.Thread(target=_kick, args=(sids,), daemon=True).start()
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    if not session.get("is_admin"):
        return redirect(url_for("viewer"))
    from .plugins import manager
    return render_template("index.html",
                           token=session.get("token", ""),
                           version=VERSION,
                           plugin_js_urls=manager.list_js_urls(),
                           is_admin=True)


@app.route("/viewer")
@login_required
def viewer():
    if session.get("is_admin"):
        return redirect(url_for("index"))
    from .plugins import manager
    return render_template("index.html",
                           token=session.get("token", ""),
                           version=VERSION,
                           plugin_js_urls=manager.list_js_urls(),
                           is_admin=False)


# ── SocketIO events ────────────────────────────────────────────────────────────

@sio.on("connect")
def on_connect():
    token = request.args.get("token", "")
    if not state.is_valid_token(token):
        log(f"Connection rejected (invalid token) <- {request.remote_addr}")
        return False
    state.add_viewer(request.sid, token)
    log(f"Connected <- {request.remote_addr}  online: {state.viewer_count}")
    _do_auto_reopen()
    emit("state", _build_state(request.sid))
    sio.emit("viewer_count", state.viewer_count)


@sio.on("disconnect")
def on_disconnect():
    state.remove_viewer(request.sid)
    log(f"Disconnected <- {request.remote_addr}  online: {state.viewer_count}")
    sio.emit("viewer_count", state.viewer_count)
    threading.Thread(target=state.try_auto_close_all, daemon=True).start()


# ── Camera management ──────────────────────────────────────────────────────────

@sio.on("scan_cameras")
def on_scan_cameras():
    if not _is_admin_sid():
        return
    state.scan_cameras()
    _emit_state_all()


@sio.on("open_camera")
def on_open_camera(data: dict):
    if not _is_admin_sid():
        return
    cam_id = data.get("cam_id", "")
    if not cam_id:
        emit("status", {"msg": "No camera selected"})
        return
    lock = _get_cam_op_lock(cam_id)
    if not lock.acquire(timeout=5):
        emit("status", {"msg": "Camera operation in progress, please try again"})
        return
    try:
        ok, msg = state.open_camera(cam_id)
        emit("status", {"msg": msg})
        if ok:
            from .plugins import manager
            stream_mgr.add_stream(cam_id, state, sio, manager)
        _emit_state_all()
    finally:
        lock.release()


@sio.on("close_camera")
def on_close_camera(data: dict):
    if not _is_admin_sid():
        return
    cam_id = data.get("cam_id", "")
    if not cam_id:
        return
    lock = _get_cam_op_lock(cam_id)
    if not lock.acquire(timeout=5):
        emit("status", {"msg": "Camera operation in progress, please try again"})
        return
    try:
        stream_mgr.remove_stream(cam_id)
        msg = state.close_camera(cam_id)
        emit("status", {"msg": msg})
        _emit_state_all()
    finally:
        lock.release()


@sio.on("close_all_cameras")
def on_close_all_cameras():
    if not _is_admin_sid():
        return
    for cam_id in list(state.open_cam_ids):
        stream_mgr.remove_stream(cam_id)
    msg = state.close_all_cameras()
    emit("status", {"msg": msg})
    _emit_state_all()


@sio.on("select_camera")
def on_select_camera(data: dict):
    """Set which camera this SID's sidebar currently shows (per-session, not broadcast)."""
    cam_id = data.get("cam_id", "")
    valid  = {c["device_id"] for c in state.available_cameras} | set(state._cameras.keys())
    if cam_id in valid:
        state.set_selected_cam(request.sid, cam_id)
    # Emit personalized state only back to the requesting SID
    emit("state", _build_state(request.sid))


@sio.on("apply_native_mode")
def on_apply_native_mode(data: dict):
    """Close and reopen a camera with a specific native mode."""
    if not _is_admin_sid():
        return
    from .plugins import manager
    cam_id = data.get("cam_id") or state.get_selected_cam(request.sid)
    index  = int(data.get("index", 0))

    cam_state    = manager.collect_state_for_camera(cam_id)
    native_modes = cam_state.get("native_modes", [])
    if not native_modes or not (0 <= index < len(native_modes)):
        emit("status", {"msg": "Invalid native mode selection"})
        return

    mode = native_modes[index]
    emit("status", {"msg": f"Applying {mode['width']}x{mode['height']} @ {mode['fps']} fps..."})

    lock = _get_cam_op_lock(cam_id)
    if not lock.acquire(timeout=5):
        emit("status", {"msg": "Camera operation in progress, please try again"})
        return
    try:
        stream_mgr.remove_stream(cam_id)
        state.close_camera(cam_id)
        with state._lock:
            state._pending_native_modes[cam_id] = mode
        ok, msg = state.open_camera(cam_id)
        if ok:
            stream_mgr.add_stream(cam_id, state, sio, manager)
        emit("status", {"msg": msg})
        _emit_state_all()
    finally:
        lock.release()


# ── Adaptive stream size ───────────────────────────────────────────────────────

@sio.on("set_stream_size")
def on_set_stream_size(data: dict):
    """Store the viewer's display dimensions per (cam_id, sid) for personalized scaling."""
    cam_id = data.get("cam_id", "")
    w = int(data.get("w", 0))
    h = int(data.get("h", 0))
    if cam_id and w > 0 and h > 0:
        state._stream_sizes[(cam_id, request.sid)] = (w, h)


@sio.on("set_stream_paused")
def on_set_stream_paused(data: dict):
    """Pause or resume frame delivery for a specific (camera, viewer) pair.

    Used by minimized MDI windows and mobile single-camera mode.
    Only affects the requesting SID — other viewers are unaffected.
    """
    cam_id = data.get("cam_id", "")
    paused = bool(data.get("paused", False))
    if cam_id:
        state._stream_paused[(cam_id, request.sid)] = paused


# ── Plugin management ──────────────────────────────────────────────────────────

@sio.on("add_plugin")
def on_add_plugin(data: dict):
    if not _is_admin_sid():
        return
    from .plugins import manager
    plugin_name = data.get("plugin_name", "")
    cam_id      = data.get("cam_id", "")
    ok, msg = manager.add_plugin(plugin_name, cam_id)
    emit("status", {"msg": msg})
    _emit_state_all()


@sio.on("remove_plugin")
def on_remove_plugin(data: dict):
    if not _is_admin_sid():
        return
    from .plugins import manager
    plugin_name = data.get("plugin_name", "")
    cam_id      = data.get("cam_id", "")
    ok, msg = manager.remove_plugin(plugin_name, cam_id)
    emit("status", {"msg": msg})
    _emit_state_all()


# ── Security / notification events ────────────────────────────────────────────

@sio.on("confirm_notification")
def on_confirm_notification(data: dict):
    if not _is_admin_sid():
        return
    notif_id = data.get("id", "")
    if notif_id:
        security.confirm_notification(notif_id)
    emit("notifications_update", {"notifications": security.get_pending_notifications()})


@sio.on("confirm_all_notifications")
def on_confirm_all_notifications():
    if not _is_admin_sid():
        return
    security.confirm_all_notifications()
    emit("notifications_update", {"notifications": []})


@sio.on("get_security_records")
def on_get_security_records():
    if not _is_admin_sid():
        return
    emit("security_records", {
        "blacklist": security.get_blacklist_info(),
        "log":       _read_log(),
    })


@sio.on("clear_blacklist")
def on_clear_blacklist():
    if not _is_admin_sid():
        return
    ip = request.environ.get("REMOTE_ADDR", "unknown")
    security.clear_blacklist()
    security._write_log("Blacklist cleared by admin", ip)
    emit("security_records", {
        "blacklist": {},
        "log":       _read_log(),
    })


@sio.on("clear_notifications")
def on_clear_notifications():
    if not _is_admin_sid():
        return
    ip = request.environ.get("REMOTE_ADDR", "unknown")
    security.clear_notifications()
    security._write_log("Notifications cleared by admin", ip)
    emit("notifications_update", {"notifications": []})


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    from .plugins import manager

    ctx = {
        "sio":        sio,
        "state":      state,
        "is_admin":   _is_admin_sid,
        "emit_state": _emit_state_all,
    }
    manager.scan()
    manager.register_routes(app, sio, ctx)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    ssl_ok  = _ensure_ssl_cert(local_ip)
    ssl_ctx = (CERT_FILE, KEY_FILE) if ssl_ok else None
    proto   = "https" if ssl_ctx else "http"

    print("=" * 52)
    print(f"  U3V WebUI  v{VERSION}")
    print(f"  Local:   {proto}://127.0.0.1:{WEB_PORT}")
    print(f"  Network: {proto}://{local_ip}:{WEB_PORT}")
    if ssl_ctx:
        print("  Encryption: HTTPS (self-signed — trust on first visit)")
    else:
        print("  Warning: SSL cert not available, running HTTP")
    admins = [u[0] for u in WEB_USERS if u[2]]
    print(f"  Admin accounts: {', '.join(admins)}")
    print("=" * 52)

    state.scan_cameras()

    sio.run(app, host="0.0.0.0", port=WEB_PORT, debug=False,
            allow_unsafe_werkzeug=True, ssl_context=ssl_ctx)
