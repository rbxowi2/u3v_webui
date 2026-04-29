# Camera Calibration User Guide

> Plugin versions: LensCalibrate 2.3.2 · StereoCalibrate 1.0.1 · StereoRectify 1.1.0 · StereoMatch & Depth 1.0.2

---

## Workflow Overview

```
LensCalibrate (left camera)  ──┐
                                ├──► StereoCalibrate ──► StereoRectify ──► Disparity / Depth
LensCalibrate (right camera) ──┘
```

| Step | Plugin | Output file |
|------|--------|------------|
| 1 | LensCalibrate (left) | `captures/calibration/<cam_L>.json` |
| 2 | LensCalibrate (right) | `captures/calibration/<cam_R>.json` |
| 3 | StereoCalibrate | `captures/stereo/<cam_L>__<cam_R>.json` |
| 4 | StereoRectify | `captures/rectify/<cam_L>__<cam_R>.json` |
| 5 | StereoMatch & Depth | `captures/depth/….ply` (optional) |

Each downstream plugin depends on the saved output of the step above it. If any stage is recalibrated and saved, all subsequent steps must be redone.

---

## Part 1: Monocular Lens Calibration (LensCalibrate)

### 1.1 Calibration Board

Use a standard chessboard calibration target. Field descriptions:

| Field | Description |
|-------|-------------|
| Board cols | Number of **inner corners** along the horizontal axis (squares − 1). A 9-column board → enter 8 |
| Board rows | Number of **inner corners** along the vertical axis (squares − 1). A 6-row board → enter 5 |
| Square size | Physical side length of one square (mm). Measure the actual printed size |

> **Important:** Incorrect cols / rows prevents corner detection and all shots will be rejected.

### 1.2 Lens Type

| Type | Description |
|------|-------------|
| Normal | Standard lens (pinhole model) — single-stage calibration |
| Fisheye | Fisheye lens (equidistant projection model) — two-stage calibration |

The lens type cannot be changed once a session has started. Use **Cancel** to restart if you selected the wrong type.

---

### 1.3 Normal Lens Calibration

1. Select **Normal** in the sidebar and confirm the board settings.
2. Click **Start** to begin the calibration session.
3. Enable **Auto** capture or click **Capture** manually.
4. Move the board to cover different positions, angles, and distances in the frame.
5. Once at least **3 shots** have been accepted, click **Save**. Lower RMS is better; aim for below **1.0 px**.
6. The session ends automatically after saving.

---

### 1.4 Fisheye Lens Calibration (Two Stages)

The two-stage approach lets K converge without distortion interference before D is solved freely.

#### Stage 1 — Solve K (D frozen at zero)

1. Select **Fisheye** and confirm the board settings.
2. Click **Start**. The system enters Stage 1 (D is fixed at `[0, 0, 0, 0]`; only K is solved).
3. Enable Auto or capture manually. Move the board widely across the frame.
4. When RMS stabilises (no longer decreasing noticeably), click **Enter Stage 2**.

> The goal of Stage 1 is to bring K (focal lengths fx/fy, principal point cx/cy) into the correct range — a perfect RMS is not required here.

#### Stage 2 — Solve K + D (D free)

1. After entering Stage 2, the HUD shows Stage 1's K and `D = [0, 0, 0, 0]` as starting values.
2. Capture again, covering all regions of the frame. Edge coverage is especially important.
3. Both K and D update in real time after each accepted shot.
4. When RMS stabilises, click **Save**.

> Stage 2 initial values: K₀ = Stage 1 result, D₀ always starts from `[0, 0, 0, 0]`.

#### Skip to Stage 2

If a saved calibration already exists and you only need to re-solve D:

1. Click **Skip to Stage 2**. The system uses the saved K as K₀ and enters Stage 2 directly.
2. The HUD shows the saved K and saved D (for reference only; D computation still starts from zero).
3. Capture normally, then Save.

> Use "Skip to Stage 2" when the lens geometry is unchanged but a slight D drift has occurred (e.g. due to temperature change). If the camera or lens has been physically altered, redo from Stage 1.

---

### 1.5 K / D Value Reference

| Symbol | Meaning |
|--------|---------|
| K | 3×3 camera intrinsic matrix (fx, fy, cx, cy) |
| D | Distortion coefficients: Normal = 5 values; Fisheye = 4 values (k1–k4) |
| RMS | Reprojection error (pixels) |

Fisheye D source at each point in time:

| Point in time | D displayed (HUD) | D computation start |
|---------------|-------------------|---------------------|
| During Stage 1 | Updates after each shot (always zero — D is frozen) | N/A (frozen) |
| Enter Stage 2 | Stage 1 final D (= zero) | `[0, 0, 0, 0]` |
| Skip to Stage 2 | Saved D (may be non-zero) | `[0, 0, 0, 0]` |
| During Stage 2 | Updates in real time after each shot | Previous result |

---

### 1.6 Capture Tips

- The board should fill at least **one-third** of the frame area. Corners are hard to detect when the board is too far away.
- Cover all four quadrants of the frame (top-left, top-right, bottom-left, bottom-right, centre).
- Varied tilt angles (left, right, tilted toward/away from the camera) help D converge.
- Use stable, diffuse lighting. Avoid reflective or shiny board surfaces.
- Auto mode has a **1.5-second** cooldown between captures. If the background is unstable, disable Auto and capture manually.
- Rejected shots (RMS did not improve) do not count and leave the data unchanged.

### 1.7 Reset and Remove

- **Reset**: Discards all captured shots and returns to Stage 1. Saved calibration files are not affected.
- **Remove**: Removes the last accepted shot and immediately recomputes RMS.
- Both Reset and Remove completely clear all in-memory shot data — no cache is retained.

### 1.8 Notes

- The board settings (cols, rows, square_size) apply for the entire session and cannot be changed mid-session.
- A maximum of **30** accepted shots are kept. Older shots are automatically discarded when this limit is exceeded.
- The application does not need to be restarted between calibrations, but a Reset before each new session is recommended.
- The slowdown-over-time issue with many shots has been fixed (v2.3.1). The 30-shot cap ensures stable performance regardless of session length.

---

## Part 2: Stereo Camera Calibration (StereoCalibrate)

### 2.1 Prerequisites

**Both cameras must have completed and saved their monocular calibration.** StereoCalibrate reads:

- `captures/calibration/<cam_L>.json`
- `captures/calibration/<cam_R>.json`

Calibration will fail if either file is missing or if the lens type setting does not match.

### 2.2 Settings

| Field | Description |
|-------|-------------|
| Left camera | Select the left camera (must match the Left selection used later in StereoMatch) |
| Right camera | Select the right camera |
| Lens type | Must match the monocular calibration (normal / fisheye) |
| Board cols / rows / square size | Must be **identical** to the monocular calibration settings |

> Mismatched board settings cause a different world coordinate system to be detected, leading to incorrect R / T values.

### 2.3 Calibration Procedure

1. Set the L/R cameras and board parameters, then click **Calibrate** to open the calibration modal.
2. Hold the calibration board so it is clearly visible in both camera frames simultaneously.
3. The system auto-captures when both cameras detect the board (Auto Cooldown = **2 seconds**).
4. A minimum of **4 shots** is required before computation begins; **5 shots** are required to Save.
5. When RMS stabilises, click **Save**.

### 2.4 Capture Tips

- The board must appear **simultaneously** in both frames and be clearly visible in each.
- Move slowly to allow both cameras to detect the same stationary board position.
- Cover a range of depths (near and far) and positions to improve R / T accuracy.
- Aim for at least **15–20 accepted shots** for robust results.
- If a shot is rejected (RMS did not improve), move the board to a new position and try again.

### 2.5 Notes

- StereoCalibrate uses the monocular K and D values as fixed intrinsics and solves only the extrinsics R and T.
- Restarting the application clears in-memory data, but **saved files are not affected**.
- A maximum of **30** accepted shots are kept.
- If either camera disconnects during the session, the session ends automatically and must be restarted.

---

## Part 3: Stereo Rectification (StereoRectify)

### 3.1 Prerequisites

Requires the StereoCalibrate output file:

- `captures/stereo/<cam_L>__<cam_R>.json`

### 3.2 Procedure

1. Select the L / R camera pair in the sidebar, then click **Rectify** to open the modal.
2. The system automatically loads the StereoCalibrate file, computes the rectification maps, and shows a live preview.
3. The preview overlays horizontal **epipolar lines** (green). Verify that corresponding features in the left and right frames fall on the same horizontal line.
4. Adjust the **Alpha** value:
   - `0` — crops to the valid (no-black-border) region, reducing the field of view
   - `1` — keeps the full image; edges may contain black areas
   - Start at `1` to verify quality, then reduce as needed
5. Once the epipolar lines are well aligned, click **Save**.

### 3.3 Output

The saved file `captures/rectify/<cam_L>__<cam_R>.json` contains the R1, R2, P1, P2, Q matrices and ROI data. StereoMatch & Depth uses these to recompute remap maps at runtime.

### 3.4 Notes

- StereoRectify is a **single-shot computation** — no capture loop is involved.
- If the epipolar lines are clearly non-horizontal or significantly offset, the StereoCalibrate quality is insufficient and stereo calibration must be redone.
- Whenever StereoCalibrate is updated and saved, StereoRectify must be redone and saved again.

---

## Part 4: Disparity and Depth (StereoMatch & Depth)

### 4.1 Prerequisites

Both of the following saved files must exist:

- `captures/stereo/<cam_L>__<cam_R>.json` (StereoCalibrate)
- `captures/rectify/<cam_L>__<cam_R>.json` (StereoRectify)

### 4.2 Enabling

1. Select the L / R cameras in the sidebar and click **Enable** to start processing (5 fps).
2. The main viewer overlays the disparity or depth map on the selected camera's feed in real time.

### 4.3 Sidebar Options

| Option | Description |
|--------|-------------|
| Display on | Overlay on the L or R camera's main viewer |
| View | Disp = disparity map; Depth = depth map |
| Algorithm | BM = faster, lower quality; SGBM = slower, higher quality |
| Resolution | 1x / ½ / ¼ — reduces processing resolution to save CPU (session-only, not saved) |

**Resolution note:** The scale factor only affects internal computation. Depth values remain **fully accurate** because the K and Q matrices scale in proportion and the scale factor cancels out in the depth formula (`Z = (f/s)·T/(d/s) = f·T/d`). The output image retains the reduced size and is not upscaled.

### 4.4 Setting Modal Parameters

| Parameter | Description | Suggested starting value |
|-----------|-------------|--------------------------|
| numDisparities | Disparity search range (must be a multiple of 16) | 64 |
| blockSize | Matching window size (must be odd) | 9 |
| P1 / P2 | SGBM smoothness penalties (auto = computed from blockSize) | auto |
| uniquenessRatio | Uniqueness filter — higher values are more strict | 5 |
| speckleWindow | Speckle filter window size | 100 |
| speckleRange | Speckle depth range | 2 |
| Clip min / max | Valid depth range (mm) | 200 / 5000 |

Parameter changes take effect immediately. When satisfied, click **Save** to write to `captures/match/<cam_L>__<cam_R>.json`.

### 4.5 Stats Panel

| Field | Description |
|-------|-------------|
| Disparity avg | Mean disparity across the frame (px) |
| Disparity valid | Fraction of pixels with valid disparity (below 50% usually indicates a parameter issue) |
| Depth valid | Fraction of pixels within the Clip range |
| Near / Median / Far | Valid depth range (mm) |

### 4.6 Save PLY

Click **Save PLY** to save the current point cloud as `captures/depth/<cam_L>__<cam_R>_<timestamp>.ply` and trigger an automatic browser download. Each click captures the point cloud from the current frame.

### 4.7 Notes

- All-black disparity map: typically caused by numDisparities being too small, a poorly rectified camera pair, or insufficient lighting.
- No valid depth pixels: check that Clip min/max are reasonable for the scene (unit is mm).
- For performance issues, try Resolution ½ or ¼ first — this significantly reduces CPU load without affecting depth accuracy.
- Resolution resets to 1x on each application restart (session-only setting).

---

## Appendix A: LensUndistort (Live Correction)

The LensUndistort plugin reads a LensCalibrate saved file and applies undistortion to **live camera frames**.

- Select the camera in the sidebar and click **Enable**.
- If the saved lens type matches the current lens type setting, undistortion is applied automatically; otherwise an error is shown.
- This plugin operates independently of the stereo pipeline.

---

## Appendix B: File Paths

| Plugin | Path |
|--------|------|
| LensCalibrate | `captures/calibration/<cam_id>.json` |
| StereoCalibrate | `captures/stereo/<cam_L>__<cam_R>.json` |
| StereoRectify | `captures/rectify/<cam_L>__<cam_R>.json` |
| StereoMatch & Depth | `captures/match/<cam_L>__<cam_R>.json` |
| PLY point cloud | `captures/depth/<cam_L>__<cam_R>_<timestamp>.ply` |

Characters `/`, `:`, and spaces in `<cam_id>`, `<cam_L>`, and `<cam_R>` are automatically replaced with `_`.

---

## Appendix C: Troubleshooting

**Q: Corner detection always fails?**
Verify that Board cols/rows are set to the number of **inner corners** (squares − 1), not the number of squares. Also ensure adequate, diffuse lighting with no reflections on the board surface.

**Q: RMS won't decrease no matter how many shots I take?**
Try capturing at different tilt angles and near the frame edges — avoid all shots being straight-on at the same distance. For Normal lenses, RMS < 1.0 px is ideal. For Fisheye Stage 2, RMS < 2.0 is generally acceptable.

**Q: StereoCalibrate reports that the monocular calibration file is not found?**
Confirm that LensCalibrate has been completed and saved for both cameras, and that the Lens type setting in StereoCalibrate matches the monocular calibration.

**Q: StereoMatch disparity map has large black regions?**
Try increasing numDisparities (must be a multiple of 16) or lowering uniquenessRatio. Verify that StereoRectify has been run and that the epipolar lines are well aligned.

**Q: What unit are the depth values in?**
The same unit as the Square size entered during calibration. If Square size was set to 25 (mm), depth values are in mm.

**Q: I changed or refocused the lens — which steps need to be redone?**
Any change to the lens or focal length invalidates all calibration data. All steps must be redone: LensCalibrate → StereoCalibrate → StereoRectify.

**Q: Is data lost when the application is restarted?**
Saved JSON files are unaffected. Only in-session shots that have not yet been saved will be lost on restart.
