## Multi-camera web streaming platform

**Highlights:**
- Multiple cameras streamed simultaneously; each viewer tracks cameras independently
- Plugin pipeline — pipeline / display segment markers, freely combine processing (recording, photo, detection, compositing, etc.)
- Drivers and plugins are independently extensible — drop into the corresponding directory, auto-loaded on startup
- Supports USB3 Vision (Aravis), UVC (V4L2), Raspberry Pi CSI, virtual cameras
## 1. Installation

### System Requirements

| Component | Minimum |
|-----------|---------|
| Python | 3.9+ |
| OS | Linux (recommended), macOS, Windows |
| OpenSSL | Any version (for self-signed TLS cert) |

### 1. System Packages

```bash
sudo apt-get install -y \
    python3-opencv \
    python3-numpy \
    gir1.2-aravis-0.8 aravis-tools-cli \
    python3-gi python3-gi-cairo
```

### 2. Virtual Environment

Create with `--system-site-packages` so the system packages installed above are accessible:

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install flask flask-socketio
```

### 3. Option- USB3 Vision camera

Camera Access Permissions (one-time setup) 
This project is developed and tested with **Hikvision USB3 Vision cameras**

```bash
sudo bash -c 'cat > /etc/udev/rules.d/99-hikvision.rules << EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="2bdf", MODE="0666", GROUP="plugdev"
EOF'

sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev $USER
```

> Group membership takes effect after re-logging in.

### Running the Server

```bash
python run.py
```

The server starts on `https://0.0.0.0:45221` by default.  
A self-signed TLS certificate is auto-generated to `temp/cert.pem` on first run.

### Default Credentials

| Username | Password | Role |
|----------|----------|------|
| `admin`  | `1234`   | Full camera control |
| `viewer` | `view1`  | View-only streaming |

> Edit `u3v_webui/config.py` → `WEB_USERS` to change credentials.

### Configuration Reference

All tunable constants live in `u3v_webui/config.py`:

```python
WEB_PORT           = 45221        # Listening port
STREAM_JPEG_Q      = 60           # Streaming JPEG quality (0-100)
STREAM_FPS         = 30           # Max push rate per viewer (fps)
FAIL_MAX_ATTEMPTS  = 3            # IP block threshold (cumulative fails)
ADAPTIVE_STREAM    = True         # Per-viewer resolution scaling
CAM_JOIN_TIMEOUT   = 2            # Driver thread shutdown timeout (s)
```

---
