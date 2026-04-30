# Intel RealSense SR300 / SR305 Setup Guide

> Driver: SR300Driver v1.0.0 (`drivers/sr300_driver.py`)

---

## Overview

The SR300Driver provides access to the Intel RealSense SR300 and SR305 depth cameras via Linux's built-in `uvcvideo` kernel driver.  It does **not** require the Intel RealSense SDK (`pyrealsense2` / `librealsense2`) to be installed.

Each physical camera appears as up to three virtual cameras in the device list:

| Stream | Format | Resolution | Frame rate |
|--------|--------|-----------|-----------|
| `[depth]` | Z16 → colourised BGR | 640 × 480 | up to 30 fps |
| `[infrared]` | Y10 → greyscale BGR | 640 × 480 | up to 60 fps |
| `[color]` | YUYV → BGR | 640 × 480 (up to 1920 × 1080) | up to 30 fps |

> SR300 depth and infrared streams share the same V4L2 node (`/dev/videoN`) with different pixel formats.  Opening both simultaneously on the same device will cause the later-opened stream to override the format; open one at a time for reliable results.

---

## Requirements

| Requirement | Notes |
|---|---|
| Linux kernel with `uvcvideo` | Standard on all modern distributions; no extra kernel modules needed |
| OpenCV (with V4L2 backend) | Already a dependency of this application |
| NumPy | Already a dependency of this application |
| User in `plugdev` or `video` group | Required to access `/dev/video*` nodes |

**Not required:** `pyrealsense2`, `librealsense2`, Intel apt repository, DKMS modules.

---

## One-Time Setup

### 1. Add udev rules (recommended)

This grants all users read/write access to the SR300's device nodes without needing group membership.  Run once with sudo:

```bash
sudo tee /etc/udev/rules.d/99-realsense.rules << 'EOF'
SUBSYSTEMS=="usb", ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0aa5", MODE:="0666", GROUP:="plugdev"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0b07", MODE:="0666", GROUP:="plugdev"
EOF
sudo udevadm control --reload-rules
```

Then unplug and replug the camera.

### 2. (Alternative) Add user to the video group

If you prefer not to install custom udev rules, add the user to the `plugdev` (or `video`) group instead:

```bash
sudo usermod -aG plugdev $USER
```

Log out and back in for the change to take effect.

### 3. Verify

```bash
python3 -c "
from u3v_webui.drivers.sr300_driver import SR300Driver
devs = SR300Driver.scan_devices()
print('Found', len(devs), 'streams:')
for d in devs: print(' ', d['label'])
"
```

Expected output (example):

```
Found 3 streams:
  RealSense SR300 [2-1] [depth]
  RealSense SR300 [2-1] [infrared]
  RealSense SR300 [2-1] [color]
```

---

## Troubleshooting

**No streams found (`Found 0 streams`)**

1. Confirm the camera is listed in `lsusb`:
   ```bash
   lsusb | grep 8086
   # Expected: Bus 002 Device XXX: ID 8086:0aa5 Intel Corp. RealSense SR300
   ```
2. Check that `/dev/video*` nodes exist and are accessible:
   ```bash
   ls -la /dev/video*
   ```
   Nodes must be readable by the current user (group `plugdev`/`video`, or `MODE=0666`).
3. Confirm `uvcvideo` is loaded:
   ```bash
   lsmod | grep uvcvideo
   ```
   If absent, load it: `sudo modprobe uvcvideo`

---

**Green or solid-colour depth image**

The SR300Driver normalises depth values to the actual scene range before colourising, so a uniform-depth scene (e.g. a flat wall) still shows full colour variation.  If the image is solid black, no depth data is being received — check that the IR emitter is not blocked and that the scene is within the camera's working range (~0.2 m – 1.5 m for SR300).

---

**Depth and infrared both show blank frames**

Both streams use the same V4L2 node with different pixel formats.  Only one can be active at a time on the same physical device.  Close the depth stream before opening infrared, or vice versa.

---

**`pyrealsense2` finds 0 devices even though the camera is connected**

`pyrealsense2` (pip) v2.50+ dropped SR300 device enumeration.  This application's SR300Driver does not use pyrealsense2 at all — this warning can be ignored.

---

## Adjustable Parameters

The following parameters are available in the camera sidebar when an SR300 stream is selected:

| Parameter | Description |
|-----------|-------------|
| `exposure_auto` | Enable / disable auto-exposure |
| `exposure` | Manual exposure time (µs); disables auto-exposure |
| `gain` | Analogue gain (0–255) |
| `rs_depth_colorizer` | Depth colour scheme (0–8): Jet, Bone, Hot, Pink, Ocean, Winter, Autumn, Rainbow, Inferno |

> `rs_depth_colorizer` only affects the `[depth]` stream display; it has no effect on `[infrared]` or `[color]`.

---

## Notes

- The SR300 exposes its streams through the kernel UVC driver (`uvcvideo`), which creates `/dev/videoN` nodes automatically when the camera is plugged in.
- The device ID format used internally is `<usb_sysfs_path>:<stream>` (e.g. `2-1:depth`).  The sysfs path may change if the camera is plugged into a different USB port.
- All depth values are in the same unit as the scene (the camera reports raw Z16 units; physical distance in mm is not calibrated by this driver).  For metric depth, use the stereo calibration pipeline.
