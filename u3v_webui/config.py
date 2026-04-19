"""
config.py — All application-wide constants and paths.

Edit WEB_USERS and WEB_PORT here to configure the server.
All other constants can be tuned as needed.
"""

import os
import secrets

# ── Version ────────────────────────────────────────────────────────────────────
VERSION = "6.11.0"

# ── User accounts: (username, password, is_admin) ─────────────────────────────
# Admin: full camera control. Viewer: stream only.
WEB_USERS = [
    ("admin",  "1234",  True),
    ("viewer", "view1", False),
]
WEB_PORT       = 45221
WEB_SECRET_KEY = secrets.token_hex(32)   # randomised each startup

# ── Streaming output ───────────────────────────────────────────────────────────
STREAM_JPEG_Q = 60   # streaming JPEG quality (does not affect saved files)
STREAM_FPS    = 30   # max stream push rate (frames/sec)

# ── Camera acquisition ─────────────────────────────────────────────────────────
STREAM_PREBUF     = 10
BUF_TIMEOUT_US    = 1_000_000
FPS_SAMPLE_FRAMES = 30

# ── Thread shutdown timeouts ───────────────────────────────────────────────────
CAM_JOIN_TIMEOUT = 2

# ── Paths ──────────────────────────────────────────────────────────────────────
# PROJECT_ROOT resolves to the u3v_webui_v2/ directory regardless of cwd.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE_DIR  = os.path.join(PROJECT_ROOT, "captures")
TEMP_DIR     = os.path.join(PROJECT_ROOT, "temp")
CERT_FILE    = os.path.join(TEMP_DIR, "cert.pem")
KEY_FILE     = os.path.join(TEMP_DIR, "key.pem")

os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR,    exist_ok=True)

# ── Security ───────────────────────────────────────────────────────────────────
# ── Adaptive streaming ─────────────────────────────────────────────────────────
# When True, frames are downscaled to match each MDI / mobile viewer window
# before JPEG encoding.  Recorded frames and plugin on_frame data are unaffected.
ADAPTIVE_STREAM = True

# ── Security ───────────────────────────────────────────────────────────────────
SECURITY_BLACKLIST_FILE = os.path.join(PROJECT_ROOT, "security_blacklist.json")
SECURITY_LOG_FILE       = os.path.join(PROJECT_ROOT, "security_log.json")
FAIL_MAX_ATTEMPTS       = 3   # lifetime cumulative failure limit per IP
