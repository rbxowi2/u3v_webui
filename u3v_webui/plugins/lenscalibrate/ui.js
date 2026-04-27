// lenscalibrate/ui.js — LensCalibrate plugin frontend (2.2.0)
// v2.2.0: Remove K₀ image-circle section; add two-stage fisheye calibration.
//         Stage 1 (pinhole/SB) → RMS < 0.5 → Enter Stage 2 (fisheye/Classic).
//         Always preload K on session open; D only if lens type matches.
//         Remove min-shots UI; add Jump-to-Stage-2 button (uses saved K).

(function () {
  'use strict';

  const SAVE_MIN_SHOTS = 3;

  // ── Module state ─────────────────────────────────────────────────────────────
  let _modal  = null;
  let _canvas = null;
  let _ctx    = null;
  let _camId  = '';

  let _lensType       = 'normal';
  let _stage          = 1;   // current calibration stage (1 or 2)
  let _hasPreloadedK  = false;   // true if saved K was preloaded at session open

  let _shotCenters  = [];
  let _accepted     = 0;
  let _rejected     = 0;
  let _lastRms      = null;
  let _lastAction   = '';
  let _lastReason   = '';
  let _sessionSaved = false;

  let _matK = null;
  let _matD = null;

  let _showCorrected = false;
  let _autoEnabled   = false;

  let _detectCorners     = null;
  let _detectCornersCorr = null;
  let _detectCols    = 0;
  let _detectRows    = 0;
  let _detectOn      = false;
  let _autoFlashEnd  = 0;

  let _pollTimer = null;
  let _fetching  = false;

  // DOM refs
  let _elDetectInd     = null;
  // Detection method toggle (now in main panel, outside K₀)
  let _elBtnDetectSb      = null;
  let _elBtnDetectClassic = null;
  // Stage section (fisheye only)
  let _elStageSection   = null;
  let _elStageInd       = null;
  let _elBtnStage2      = null;
  let _elBtnJumpStage2  = null;
  // Stats / controls
  let _elAccepted      = null;
  let _elRejected      = null;
  let _elRms           = null;
  let _elRmsTrend      = null;
  let _elLastAction    = null;
  let _elBtnStartPause = null;
  let _elBtnReset      = null;
  let _elBtnToggle     = null;
  let _elBtnSave       = null;
  let _elBtnCancel     = null;
  let _elBtnRemoveShot = null;

  // ── Modal builder ────────────────────────────────────────────────────────────
  function _buildModal() {
    if (_modal) return;

    _modal = document.createElement('div');
    Object.assign(_modal.style, {
      display: 'none', position: 'fixed', inset: '0', zIndex: '9500',
      background: 'rgba(0,0,0,0.93)', flexDirection: 'column',
      fontFamily: '"Helvetica Neue",Helvetica,Arial,sans-serif',
    });

    // ── Header
    const hdr = document.createElement('div');
    Object.assign(hdr.style, {
      display: 'flex', alignItems: 'center', padding: '8px 16px',
      background: '#181818', borderBottom: '1px solid #333', flexShrink: '0',
    });
    const title = document.createElement('span');
    title.textContent = 'Lens Calibration — Auto';
    Object.assign(title.style, {
      flex: '1', textAlign: 'center', fontSize: '14px',
      color: '#d4d4d4', fontWeight: '600',
    });
    hdr.appendChild(title);
    _modal.appendChild(hdr);

    // ── Body
    const body = document.createElement('div');
    Object.assign(body.style, { flex: '1', display: 'flex', overflow: 'hidden', minHeight: '0' });

    // Canvas
    const canvasWrap = document.createElement('div');
    Object.assign(canvasWrap.style, {
      flex: '1', display: 'flex', alignItems: 'center', justifyContent: 'center',
      overflow: 'hidden', background: '#000',
    });
    _canvas = document.createElement('canvas');
    Object.assign(_canvas.style, { maxWidth: '100%', maxHeight: '100%', display: 'block' });
    _ctx = _canvas.getContext('2d');
    canvasWrap.appendChild(_canvas);

    // Right panel
    const panel = document.createElement('div');
    Object.assign(panel.style, {
      width: '268px', flexShrink: '0', background: '#1a1a1a',
      borderLeft: '1px solid #333', display: 'flex', flexDirection: 'column',
      padding: '12px 10px', gap: '8px', overflowY: 'auto',
    });

    // 1. Detection indicator
    _elDetectInd = document.createElement('div');
    Object.assign(_elDetectInd.style, {
      fontSize: '11px', color: '#666', fontStyle: 'italic',
      textAlign: 'center', padding: '2px 0',
    });

    // 2. Detection method toggle (SB / Classic)
    const detectMethodRow = document.createElement('div');
    Object.assign(detectMethodRow.style, { display: 'flex', gap: '4px' });
    _elBtnDetectSb      = _mkSmBtn('SB',      true,  () => _onDetectMethod(true));
    _elBtnDetectClassic = _mkSmBtn('Classic',  false, () => _onDetectMethod(false));
    detectMethodRow.append(_elBtnDetectSb, _elBtnDetectClassic);

    // 3. Stage section (fisheye only)
    _elStageSection = document.createElement('div');
    Object.assign(_elStageSection.style, {
      display: 'none', flexDirection: 'column', gap: '6px',
      background: '#1e2a1e', borderRadius: '4px',
      border: '1px solid #2a4a2a', padding: '8px',
    });

    _elStageInd = document.createElement('div');
    Object.assign(_elStageInd.style, {
      fontSize: '11px', color: '#888', lineHeight: '1.5',
    });

    _elBtnStage2 = _mkBtn('Enter Stage 2 →', '#1a3a1a', '#7dcf7d', '#2a5a2a', _onEnterStage2);
    _elBtnStage2.style.cssText += ';font-size:11px;font-weight:600;';
    _setBtnEnabled(_elBtnStage2, false);

    _elBtnJumpStage2 = _mkBtn('Jump to Stage 2 (saved K)', '#1a2a3a', '#6ab0d4', '#2a4a6a', _onJumpStage2);
    _elBtnJumpStage2.style.cssText += ';font-size:11px;';
    _setBtnEnabled(_elBtnJumpStage2, false);

    _elStageSection.append(_elStageInd, _elBtnStage2, _elBtnJumpStage2);

    // 4. Stats box
    const statsWrap = document.createElement('div');
    Object.assign(statsWrap.style, {
      background: '#222', borderRadius: '4px', padding: '8px',
      border: '1px solid #333', display: 'flex', flexDirection: 'column', gap: '5px',
    });

    function _mkStatRow(label) {
      const row = document.createElement('div');
      Object.assign(row.style, { display: 'flex', alignItems: 'center' });
      const lbl = document.createElement('span');
      lbl.textContent = label;
      Object.assign(lbl.style, { fontSize: '11px', color: '#777', flex: '1' });
      const val = document.createElement('span');
      Object.assign(val.style, { fontSize: '12px', color: '#d4d4d4', fontWeight: '600' });
      row.append(lbl, val);
      statsWrap.appendChild(row);
      return val;
    }
    _elAccepted = _mkStatRow('Accepted');
    _elRejected = _mkStatRow('Rejected');

    const rmsDivEl = document.createElement('div');
    rmsDivEl.style.cssText = 'border-top:1px solid #2a2a2a;';
    statsWrap.appendChild(rmsDivEl);

    const rmsRow = document.createElement('div');
    Object.assign(rmsRow.style, { display: 'flex', alignItems: 'baseline', gap: '5px' });
    const rmsLbl = document.createElement('span');
    rmsLbl.textContent = 'RMS';
    Object.assign(rmsLbl.style, { fontSize: '11px', color: '#777' });
    _elRms = document.createElement('span');
    Object.assign(_elRms.style, { fontSize: '15px', fontWeight: '700', color: '#888' });
    _elRmsTrend = document.createElement('span');
    Object.assign(_elRmsTrend.style, { fontSize: '11px', color: '#555' });
    rmsRow.append(rmsLbl, _elRms, _elRmsTrend);
    statsWrap.appendChild(rmsRow);

    // 5. Last action
    _elLastAction = document.createElement('div');
    Object.assign(_elLastAction.style, {
      fontSize: '10px', color: '#666', lineHeight: '1.6', minHeight: '28px',
    });

    // 7. Start/Pause + Reset
    const ctrlRow = document.createElement('div');
    Object.assign(ctrlRow.style, { display: 'flex', gap: '6px' });

    _elBtnStartPause = document.createElement('button');
    _elBtnStartPause.textContent = 'Start';
    Object.assign(_elBtnStartPause.style, {
      flex: '1', background: '#1a3a1a', color: '#7dcf7d',
      border: '1px solid #2a5a2a', borderRadius: '4px',
      padding: '7px 0', fontSize: '12px', fontWeight: '600', cursor: 'pointer',
    });
    _elBtnStartPause.onclick = _onStartPause;

    _elBtnReset = document.createElement('button');
    _elBtnReset.textContent = 'Reset';
    Object.assign(_elBtnReset.style, {
      flex: '1', background: '#2a2a2a', color: '#e8a43c',
      border: '1px solid #4a3a1a', borderRadius: '4px',
      padding: '7px 0', fontSize: '12px', cursor: 'pointer',
    });
    _elBtnReset.onclick = _onReset;
    ctrlRow.append(_elBtnStartPause, _elBtnReset);

    // 8. Remove last shot
    _elBtnRemoveShot = _mkBtn('Remove Last Shot', '#2a1a1a', '#e86c3c', '#5a2a1a', _onRemoveShot);
    _elBtnRemoveShot.style.cssText += ';font-size:11px;';
    _setBtnEnabled(_elBtnRemoveShot, false);

    // 9. Toggle / Save / Cancel
    _elBtnToggle  = _mkBtn('Show Corrected', '#1a2a3a', '#6ab0d4', '#2a4a6a', _onToggleView);
    _elBtnSave    = _mkBtn('Save', '#1a3a1a', '#7dcf7d', '#2a5a2a', _onSave);
    _elBtnSave.style.cssText += ';font-weight:600;padding:9px;font-size:12px;';
    _elBtnCancel  = _mkBtn('Cancel', '#2a2a2a', '#888', '#444', calibCancel);
    _elBtnCancel.style.cssText += ';padding:7px;font-size:11px;';

    panel.append(
      _elDetectInd,
      detectMethodRow,
      _mkDivider(),
      _elStageSection,
      statsWrap,
      _elLastAction,
      _mkDivider(),
      ctrlRow,
      _elBtnRemoveShot,
      _mkDivider(),
      _elBtnToggle,
      _mkDivider(),
      _elBtnSave,
      _elBtnCancel,
    );

    body.append(canvasWrap, panel);
    _modal.appendChild(body);
    document.body.appendChild(_modal);
  }

  // ── Widget helpers ────────────────────────────────────────────────────────────
  function _mkBtn(text, bg, fg, border, onClick) {
    const b = document.createElement('button');
    b.textContent = text;
    Object.assign(b.style, {
      background: bg, color: fg, border: `1px solid ${border}`,
      borderRadius: '4px', padding: '6px 14px', fontSize: '12px',
      cursor: 'pointer', width: '100%',
    });
    b.onclick = onClick;
    return b;
  }

  function _mkSmBtn(text, active, onClick) {
    const b = document.createElement('button');
    b.textContent = text;
    const on  = { background: '#1a3a5c', color: '#6ab0d4', border: '1px solid #2a5a8c' };
    const off = { background: '#2a2a2a', color: '#666',    border: '1px solid #444'    };
    Object.assign(b.style, {
      ...(active ? on : off),
      flex: '1', borderRadius: '3px', padding: '3px 0',
      fontSize: '10px', cursor: 'pointer',
    });
    b.onclick = onClick;
    b._setActive = (v) => Object.assign(b.style, v ? on : off);
    return b;
  }

  function _mkDivider() {
    const d = document.createElement('div');
    d.style.cssText = 'border-top:1px solid #2a2a2a;flex-shrink:0;';
    return d;
  }

  function _mkNumRow(label, min, max, step, onChange) {
    const row = document.createElement('div');
    Object.assign(row.style, { display: 'flex', alignItems: 'center', gap: '4px' });
    const lbl = document.createElement('span');
    lbl.textContent = label;
    Object.assign(lbl.style, { fontSize: '11px', color: '#777', flex: '1' });
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.min  = String(min);
    inp.max  = String(max);
    inp.step = String(step);
    Object.assign(inp.style, {
      width: '72px', background: '#2a2a2a', color: '#d4d4d4',
      border: '1px solid #444', borderRadius: '3px',
      padding: '3px 4px', fontSize: '11px', textAlign: 'center',
    });
    inp.oninput = () => onChange(inp.value);
    row.append(lbl, inp);
    return row;
  }

  // ── Stage section helper ──────────────────────────────────────────────────────
  function _updateStageUI() {
    if (_lensType !== 'fisheye') {
      if (_elStageSection) _elStageSection.style.display = 'none';
      return;
    }
    if (_elStageSection) _elStageSection.style.display = 'flex';
    if (_stage === 1) {
      const rmsOk = _lastRms !== null && _lastRms < 0.5;
      _elStageInd.innerHTML =
        `<span style="color:#6ab0d4;font-weight:600;">Stage 1</span>` +
        ` <span style="color:#777;">Pinhole · SB detection</span>` +
        (_lastRms !== null
          ? `<br><span style="color:${_lastRms < 0.5 ? '#7dcf7d' : _lastRms < 1.0 ? '#e8a43c' : '#e74c3c'};">RMS ${_lastRms.toFixed(4)}</span>` +
            (!rmsOk ? `<span style="color:#555;"> — need &lt; 0.5</span>` : `<span style="color:#7dcf7d;"> — ready</span>`)
          : '');
      _elBtnStage2.textContent = 'Enter Stage 2 →';
      _setBtnEnabled(_elBtnStage2, rmsOk && _accepted > 0);
      _elBtnJumpStage2.style.display = _hasPreloadedK ? 'block' : 'none';
      _setBtnEnabled(_elBtnJumpStage2, _hasPreloadedK);
    } else {
      _elStageInd.innerHTML =
        `<span style="color:#7dcf7d;font-weight:600;">Stage 2</span>` +
        ` <span style="color:#777;">Fisheye · Classic detection</span>`;
      _setBtnEnabled(_elBtnStage2, false);
      _elBtnStage2.textContent = 'Stage 2 active';
      _elBtnJumpStage2.style.display = 'none';
    }
  }

  // ── Open modal ───────────────────────────────────────────────────────────────
  window.calibOpenModal = function (el) {
    const block  = el.closest('.plugin-ui-block');
    const cam_id = block ? block.dataset.cam : '';
    if (!cam_id) return;

    const infoRow  = block ? block.querySelector('.calib-info-row') : null;
    const hasCalib = infoRow && infoRow.dataset.hasData === 'true';

    function _doOpen() {
      _buildModal();
      _camId         = cam_id;
      _lensType       = block.querySelector('.calib-lens-type')?.value || 'normal';
      _stage          = 1;
      _hasPreloadedK  = false;
      _shotCenters   = [];
      _accepted      = 0;
      _rejected      = 0;
      _lastRms       = null;
      _lastAction    = '';
      _lastReason    = '';
      _sessionSaved  = false;
      _matK          = null;
      _matD          = null;
      _showCorrected = false;
      _autoEnabled   = false;
      _detectCorners     = null;
      _detectCornersCorr = null;
      _detectOn      = false;
      _autoFlashEnd  = 0;
      _fetching      = false;

      _canvas.width  = 1280;
      _canvas.height = 720;
      _ctx.fillStyle = '#000';
      _ctx.fillRect(0, 0, 1280, 720);

      _elAccepted.textContent   = '0';
      _elRejected.textContent   = '0';
      _elRms.textContent        = '—';
      _elRms.style.color        = '#888';
      _elRmsTrend.textContent   = '';
      _elLastAction.textContent = 'Paused — press Start when ready.';
      _elDetectInd.textContent  = 'Board not detected';
      _elDetectInd.style.color  = '#666';
      _elBtnToggle.textContent  = 'Show Corrected';
      _elBtnStartPause.textContent = 'Start';
      Object.assign(_elBtnStartPause.style, {
        background: '#1a3a1a', color: '#7dcf7d', border: '1px solid #2a5a2a',
      });
      _elBtnCancel.textContent = 'Cancel';
      Object.assign(_elBtnCancel.style, {
        background: '#2a2a2a', color: '#888', border: '1px solid #444',
        padding: '7px', fontWeight: '',
      });
      _setBtnEnabled(_elBtnSave, false);
      _setBtnEnabled(_elBtnReset, true);
      _setBtnEnabled(_elBtnRemoveShot, false);

      // Both normal and fisheye Stage 1 start with SB
      _elBtnDetectSb._setActive(true);
      _elBtnDetectClassic._setActive(false);
      if (_camId) socket.emit('calib_set_detect_method', { cam_id: _camId, use_sb: true });

      // Stage section (fisheye only)
      _updateStageUI();

      _modal.style.display = 'flex';
      socket.emit('calib_start', { cam_id });
      _startPoll();
    }

    if (hasCalib) {
      if (confirm('This camera already has calibration data.\nContinue and overwrite?')) _doOpen();
    } else {
      _doOpen();
    }
  };

  // ── Snapshot polling ─────────────────────────────────────────────────────────
  function _startPoll() {
    _stopPoll();
    _pollTimer = setInterval(_fetchFrame, 150);
  }

  function _stopPoll() {
    if (_pollTimer !== null) { clearInterval(_pollTimer); _pollTimer = null; }
  }

  function _fetchFrame() {
    if (!_camId || _fetching) return;
    _fetching = true;
    const corr = _showCorrected ? 1 : 0;
    const url  = `/plugin/lenscalibrate/snapshot?cam_id=${encodeURIComponent(_camId)}&corrected=${corr}&_t=${Date.now()}`;
    fetch(url)
      .then(r => r.ok ? r.blob() : null)
      .then(blob => {
        _fetching = false;
        if (!blob || !_canvas || !_ctx) return;
        const objUrl = URL.createObjectURL(blob);
        const img    = new Image();
        img.onload = function () {
          URL.revokeObjectURL(objUrl);
          if (!_canvas || !_ctx) return;
          if (_canvas.width !== img.naturalWidth || _canvas.height !== img.naturalHeight) {
            _canvas.width  = img.naturalWidth;
            _canvas.height = img.naturalHeight;
          }
          _ctx.drawImage(img, 0, 0);
          _drawOverlays();
        };
        img.src = objUrl;
      })
      .catch(() => { _fetching = false; });
  }

  // ── Canvas overlays ──────────────────────────────────────────────────────────
  function _drawOverlays() {
    const W = _canvas.width, H = _canvas.height;
    _drawRefGrid(W, H);
    _drawCoverageDots(W, H);
    if (_detectOn) {
      const pts = _showCorrected ? _detectCornersCorr : _detectCorners;
      if (pts && pts.length > 0)
        _drawCorners(pts, _detectCols, _detectRows, W, H);
    }
    _drawBorder(W, H);
    _drawAutoFlash(W, H);
    _drawMatrixHUD(W, H);
  }

  // ── K/D matrix HUD ───────────────────────────────────────────────────────────
  function _fmtK(v)  { return v.toFixed(1); }
  function _fmtD(v)  { return (v >= 0 ? ' ' : '') + v.toFixed(5); }

  function _drawMatrixHUD(W, H) {
    if (!_matK) return;
    const ctx = _ctx;
    const fx = _matK[0][0], fy = _matK[1][1];
    const cx = _matK[0][2], cy = _matK[1][2];
    const D  = _matD || [];
    const isFish = (_lensType === 'fisheye' && _stage === 2);
    const dLbl = isFish ? ['k1','k2','k3','k4'] : ['k1','k2','p1','p2'];

    const lines = [
      `K  fx:${_fmtK(fx)}  fy:${_fmtK(fy)}`,
      `   cx:${_fmtK(cx)}  cy:${_fmtK(cy)}`,
      `D  ${dLbl[0]}:${_fmtD(D[0]||0)}  ${dLbl[1]}:${_fmtD(D[1]||0)}`,
    ];
    if (D.length >= 4)
      lines.push(`   ${dLbl[2]}:${_fmtD(D[2]||0)}  ${dLbl[3]}:${_fmtD(D[3]||0)}`);

    const fs  = Math.max(14, Math.round(W / 60));
    const lh  = fs * 1.6;
    const pad = Math.round(fs * 0.55);
    ctx.save();
    ctx.font = `${fs}px "Courier New",monospace`;

    let tw = 0;
    lines.forEach(l => { tw = Math.max(tw, ctx.measureText(l).width); });
    const bw = tw + pad * 2;
    const bh = lines.length * lh + pad * 2;
    const x0 = 10, y0 = H - bh - 10;

    ctx.fillStyle = 'rgba(0,0,0,0.62)';
    ctx.fillRect(x0, y0, bw, bh);
    ctx.fillStyle = 'rgba(160,215,160,0.95)';
    lines.forEach((l, i) => {
      ctx.fillText(l, x0 + pad, y0 + pad + (i + 0.82) * lh);
    });
    ctx.restore();
  }

  function _drawRefGrid(W, H) {
    const ctx = _ctx;
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    [W / 3, W * 2 / 3].forEach(x => {
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    });
    [H / 3, H * 2 / 3].forEach(y => {
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    });
    ctx.restore();
  }

  function _drawCoverageDots(W, H) {
    if (_shotCenters.length === 0) return;
    const ctx = _ctx;
    ctx.save();
    _shotCenters.forEach(c => {
      const x = c[0] * W, y = c[1] * H;
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fillStyle   = 'rgba(70,210,90,0.80)';
      ctx.strokeStyle = 'rgba(30,120,50,0.90)';
      ctx.lineWidth   = 1.5;
      ctx.fill(); ctx.stroke();
    });
    ctx.restore();
  }

  function _drawBorder(W, H) {
    if (!_detectOn) return;
    const ctx   = _ctx;
    const alpha = 0.45 + 0.44 * Math.abs(Math.sin(Date.now() / 250 * Math.PI));
    ctx.save();
    ctx.strokeStyle = `rgba(60,210,90,${alpha.toFixed(2)})`;
    ctx.lineWidth   = 14;
    ctx.strokeRect(7, 7, W - 14, H - 14);
    ctx.restore();
  }

  function _drawAutoFlash(W, H) {
    const remaining = _autoFlashEnd - Date.now();
    if (remaining <= 0) return;
    const alpha = Math.min(1, remaining / 200) * 0.35;
    const ctx = _ctx;
    ctx.save();
    ctx.fillStyle = `rgba(255,255,255,${alpha.toFixed(2)})`;
    ctx.fillRect(0, 0, W, H);
    ctx.restore();
  }

  function _drawCorners(corners, cols, rows, W, H) {
    const n   = corners.length;
    const ctx = _ctx;
    function px(i) { return [corners[i][0] * W, corners[i][1] * H]; }
    function hsl(i) { return `hsl(${Math.round(i / Math.max(n - 1, 1) * 300)},100%,60%)`; }
    ctx.lineWidth = 2;
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols - 1; c++) {
        const i = r * cols + c, p1 = px(i), p2 = px(i + 1);
        ctx.beginPath(); ctx.strokeStyle = hsl(i);
        ctx.moveTo(p1[0], p1[1]); ctx.lineTo(p2[0], p2[1]); ctx.stroke();
      }
    }
    for (let c = 0; c < cols; c++) {
      for (let r = 0; r < rows - 1; r++) {
        const i = r * cols + c, p1 = px(i), p2 = px((r + 1) * cols + c);
        ctx.beginPath(); ctx.strokeStyle = hsl(i);
        ctx.moveTo(p1[0], p1[1]); ctx.lineTo(p2[0], p2[1]); ctx.stroke();
      }
    }
    corners.forEach((co, i) => {
      ctx.beginPath();
      ctx.arc(co[0] * W, co[1] * H, 4, 0, Math.PI * 2);
      ctx.fillStyle = hsl(i); ctx.fill();
    });
  }

  // ── SocketIO: detection ───────────────────────────────────────────────────────
  socket.on('calib_detect', function (data) {
    if (!_modal || _modal.style.display === 'none') return;
    if (data.cam_id !== _camId) return;
    if (data.detected) {
      _detectCorners     = data.corners;
      _detectCornersCorr = data.corners_corrected || null;
      _detectCols    = data.cols;
      _detectRows    = data.rows;
      _detectOn      = true;
      if (data.auto_triggered) _autoFlashEnd = Date.now() + 300;
      _elDetectInd.textContent = 'Board detected';
      _elDetectInd.style.color = '#5cba5c';
    } else {
      _detectOn = false;
      _detectCornersCorr = null;
      _elDetectInd.textContent = 'Board not detected';
      _elDetectInd.style.color = '#666';
    }
  });

  // ── SocketIO: auto toggle ─────────────────────────────────────────────────────
  socket.on('calib_auto_toggled', function (data) {
    if (!_modal || _modal.style.display === 'none') return;
    if (data.cam_id !== _camId) return;
    _autoEnabled = data.enabled;
    if (_autoEnabled) {
      _elBtnStartPause.textContent = 'Pause';
      Object.assign(_elBtnStartPause.style, {
        background: '#3a2a1a', color: '#e8a43c', border: '1px solid #6a4a2a',
      });
    } else {
      _elBtnStartPause.textContent = 'Start';
      Object.assign(_elBtnStartPause.style, {
        background: '#1a3a1a', color: '#7dcf7d', border: '1px solid #2a5a2a',
      });
    }
  });

  // ── SocketIO: auto status ────────────────────────────────────────────────────
  socket.on('calib_auto_status', function (data) {
    if (!_modal || _modal.style.display === 'none') return;
    if (data.cam_id !== _camId) return;

    if (data.action === 'stage2') {
      _stage = 2;
      _shotCenters = []; _accepted = 0; _rejected = 0; _lastRms = null; _autoEnabled = false;
      _matK = null; _matD = null;
      _elAccepted.textContent   = '0';
      _elRejected.textContent   = '0';
      _elRms.textContent        = '—';
      _elRms.style.color        = '#888';
      _elRmsTrend.textContent   = '';
      _elLastAction.innerHTML =
        `<span style="color:#7dcf7d;">Stage 2 started</span>` +
        (data.reason ? ` <span style="color:#777;">(${_esc(data.reason)})</span>` : '');
      _elBtnStartPause.textContent = 'Start';
      Object.assign(_elBtnStartPause.style, {
        background: '#1a3a1a', color: '#7dcf7d', border: '1px solid #2a5a2a',
      });
      _setBtnEnabled(_elBtnSave, false);
      _setBtnEnabled(_elBtnRemoveShot, false);
      // Stage 2 always uses Classic
      _elBtnDetectSb._setActive(false);
      _elBtnDetectClassic._setActive(true);
      _updateStageUI();
      return;
    }

    if (data.action === 'reset') {
      _stage = 1;
      _shotCenters = []; _accepted = 0; _rejected = 0; _lastRms = null; _autoEnabled = false;
      _matK = null; _matD = null;
      _elAccepted.textContent   = '0';
      _elRejected.textContent   = '0';
      _elRms.textContent        = '—';
      _elRms.style.color        = '#888';
      _elRmsTrend.textContent   = '';
      _elLastAction.textContent = 'Reset — press Start when ready.';
      _elBtnStartPause.textContent = 'Start';
      Object.assign(_elBtnStartPause.style, {
        background: '#1a3a1a', color: '#7dcf7d', border: '1px solid #2a5a2a',
      });
      _setBtnEnabled(_elBtnSave, false);
      _setBtnEnabled(_elBtnRemoveShot, false);
      _elBtnStage2.textContent = 'Enter Stage 2 →';
      // Restore stage 1 detection method (SB for both normal and fisheye stage 1)
      _elBtnDetectSb._setActive(true);
      _elBtnDetectClassic._setActive(false);
      if (_camId) socket.emit('calib_set_detect_method', { cam_id: _camId, use_sb: true });
      _updateStageUI();
      return;
    }

    const prevRms = _lastRms;
    _lastRms = data.rms; _lastAction = data.action;
    _lastReason = data.reason || '';
    _accepted = data.accepted; _rejected = data.rejected;
    _shotCenters = data.shot_centers || [];
    if (data.K) _matK = data.K;
    if (data.D) _matD = data.D;

    _elAccepted.textContent = String(_accepted);
    _elRejected.textContent = String(_rejected);

    if (_lastRms !== null && _lastRms !== undefined) {
      const rmsCol = _lastRms < 0.5 ? '#7dcf7d' : _lastRms < 1.0 ? '#e8a43c' : '#e74c3c';
      _elRms.textContent = _lastRms.toFixed(4);
      _elRms.style.color = rmsCol;
      const oldRms = data.old_rms !== null ? data.old_rms : prevRms;
      if (oldRms !== null && oldRms !== undefined) {
        const delta = _lastRms - oldRms;
        if (delta < -0.0001)     { _elRmsTrend.textContent = ' ↓'; _elRmsTrend.style.color = '#7dcf7d'; }
        else if (delta > 0.0001) { _elRmsTrend.textContent = ' ↑'; _elRmsTrend.style.color = '#e74c3c'; }
        else                     { _elRmsTrend.textContent = ' —'; _elRmsTrend.style.color = '#555'; }
      }
    }

    if (data.action === 'accepted') {
      _elLastAction.innerHTML =
        `<span style="color:#5cba5c;">&#10003; Accepted</span>` +
        (data.rms !== null ? ` <span style="color:#777;">RMS ${data.rms.toFixed(4)}</span>` : '');
      if (_accepted === 1 && !_showCorrected) {
        _showCorrected = true;
        _elBtnToggle.textContent = 'Show Raw';
      }
    } else if (data.action === 'remove') {
      _elLastAction.innerHTML =
        `<span style="color:#e8a43c;">&#8592; Shot removed</span>` +
        (_lastRms !== null ? ` <span style="color:#777;">RMS ${_lastRms.toFixed(4)}</span>` : '');
      if (_accepted === 0) {
        _matK = null; _matD = null;
        _showCorrected = false;
        _elBtnToggle.textContent = 'Show Corrected';
        _elRms.textContent = '—'; _elRms.style.color = '#888';
        _elRmsTrend.textContent = '';
      }
    } else {
      _elLastAction.innerHTML =
        `<span style="color:#e74c3c;">&#10007; Rejected</span>` +
        (_lastReason ? ` <span style="color:#777;">${_esc(_lastReason)}</span>` : '');
    }

    // Save enabled: normal lens any stage, fisheye only in stage 2
    const saveOk = _accepted >= SAVE_MIN_SHOTS &&
                   (_lensType !== 'fisheye' || _stage === 2);
    _setBtnEnabled(_elBtnSave, saveOk);
    _setBtnEnabled(_elBtnRemoveShot, _accepted > 0);
    _updateStageUI();
  });

  // ── SocketIO: calib_event ────────────────────────────────────────────────────
  socket.on('calib_event', function (data) {
    switch (data.type) {
      case 'started':
        if (_modal && _modal.style.display !== 'none') {
          if (data.preloaded_K) {
            _hasPreloadedK = true;
            _preloadKD(data.preloaded_K, data.preloaded_D || null);
            _updateStageUI();
          }
        }
        break;
      case 'save_result': _handleSaveResult(data); break;
      case 'error':
        if (_modal && _modal.style.display !== 'none')
          _elLastAction.innerHTML = `<span style="color:#e74c3c;">Error: ${_esc(data.msg)}</span>`;
        break;
    }
  });

  function _handleSaveResult(data) {
    _sessionSaved = true;
    _stopPoll();
    if (!data.ok) {
      _elLastAction.innerHTML = `<span style="color:#e74c3c;">Save failed: ${_esc(data.error)}</span>`;
      _setBtnEnabled(_elBtnSave, _accepted >= SAVE_MIN_SHOTS && (_lensType !== 'fisheye' || _stage === 2));
      return;
    }
    const rmsCol = data.rms < 0.5 ? '#7dcf7d' : data.rms < 1.0 ? '#e8a43c' : '#e74c3c';
    const rmsLbl = data.rms < 0.5 ? 'Excellent' : data.rms < 1.0 ? 'Acceptable' : 'Poor';
    _elLastAction.innerHTML = [
      `<b style="color:#6ab0d4;">Calibration saved</b>`,
      `RMS <span style="color:${rmsCol};font-weight:600;">${data.rms} px — ${rmsLbl}</span>`,
      `${data.shot_count} shots &nbsp;${data.image_size[0]}×${data.image_size[1]}`,
    ].join('<br>');
    _setBtnEnabled(_elBtnSave, false);
    _setCloseBtn();
  }

  // ── Button handlers ───────────────────────────────────────────────────────────
  function _onEnterStage2() {
    if (!_camId || _stage !== 1) return;
    socket.emit('calib_enter_stage2', { cam_id: _camId });
  }

  function _onJumpStage2() {
    if (!_camId || !_hasPreloadedK) return;
    socket.emit('calib_skip_to_stage2', { cam_id: _camId });
  }

  function _onRemoveShot() {
    if (!_camId || _accepted === 0) return;
    socket.emit('calib_remove_shot', { cam_id: _camId });
  }

  function _onDetectMethod(useSb) {
    _elBtnDetectSb._setActive(useSb);
    _elBtnDetectClassic._setActive(!useSb);
    if (_camId) socket.emit('calib_set_detect_method', { cam_id: _camId, use_sb: useSb });
  }

  function _preloadKD(K, D) {
    _matK = K;
    _matD = D ? (D.slice ? D.slice(0, 4) : D) : null;

    if (!_showCorrected && D !== null) {
      _showCorrected = true;
      _elBtnToggle.textContent = 'Show Raw';
    }
  }

  function _onStartPause() {
    if (!_camId) return;
    socket.emit('calib_toggle_auto', { cam_id: _camId });
  }

  function _onReset() {
    if (!_camId) return;
    socket.emit('calib_reset', { cam_id: _camId });
  }

  function _onToggleView() {
    _showCorrected           = !_showCorrected;
    _elBtnToggle.textContent = _showCorrected ? 'Show Raw' : 'Show Corrected';
  }

  function _onSave() {
    if (!_camId || _sessionSaved || _accepted < SAVE_MIN_SHOTS) return;
    if (_lensType === 'fisheye' && _stage === 1) {
      _elLastAction.innerHTML =
        '<span style="color:#e8a43c;">Fisheye: enter Stage 2 before saving.</span>';
      return;
    }
    _setBtnEnabled(_elBtnSave, false);
    socket.emit('calib_save', { cam_id: _camId });
  }

  window.calibCancel = function () {
    _stopPoll();
    if (_camId && !_sessionSaved) socket.emit('calib_cancel', { cam_id: _camId });
    if (_modal) _modal.style.display = 'none';
    _detectOn = false; _detectCorners = null;
    _camId = ''; _sessionSaved = false;
  };

  // ── UI helpers ─────────────────────────────────────────────────────────────
  function _setCloseBtn() {
    _elBtnCancel.textContent = 'Close';
    Object.assign(_elBtnCancel.style, {
      background: '#3a3a3a', color: '#d4d4d4', border: '1px solid #555',
      padding: '9px', fontWeight: '600', opacity: '1', cursor: 'pointer',
    });
    _elBtnCancel.disabled = false;
  }

  function _setBtnEnabled(btn, enabled) {
    if (!btn) return;
    btn.disabled      = !enabled;
    btn.style.opacity = enabled ? '1' : '0.4';
    btn.style.cursor  = enabled ? 'pointer' : 'not-allowed';
  }

  function _esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ── Sidebar param handlers ───────────────────────────────────────────────────
  function _blk(el)    { return el.closest('.plugin-ui-block'); }
  function _blkCam(el) { const b = _blk(el); return b ? b.dataset.cam : ''; }

  window.calibOnLensType = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'calib_lens_type', value: el.value });
    const block   = _blk(el);
    const fishOpt = block ? block.querySelector('.calib-fisheye-opts') : null;
    if (fishOpt) fishOpt.style.display = el.value === 'fisheye' ? 'flex' : 'none';
  };

  window.calibOnBoardCols = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'calib_board_cols', value: parseInt(el.value) });
  };
  window.calibOnBoardRows = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'calib_board_rows', value: parseInt(el.value) });
  };
  window.calibOnSquareSize = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'calib_square_size', value: parseFloat(el.value) });
  };

  // ── State sync ───────────────────────────────────────────────────────────────
  function _applyCalibState(s) {
    document.querySelectorAll('.plugin-ui-block[data-plugin="LensCalibrate"]').forEach(block => {
      const cid = block.dataset.cam;
      const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
      if (!cs) return;

      const selLens  = block.querySelector('.calib-lens-type');
      const inpCols  = block.querySelector('.calib-board-cols');
      const inpRows  = block.querySelector('.calib-board-rows');
      const inpSq    = block.querySelector('.calib-square-size');
      const infoRow  = block.querySelector('.calib-info-row');

      if (selLens && cs.calib_lens_type !== undefined) selLens.value = cs.calib_lens_type;
      if (inpCols && cs.calib_board_cols  !== undefined) inpCols.value = cs.calib_board_cols;
      if (inpRows && cs.calib_board_rows  !== undefined) inpRows.value = cs.calib_board_rows;
      if (inpSq   && cs.calib_square_size !== undefined) inpSq.value   = cs.calib_square_size;

      if (infoRow) {
        if (cs.calib_has_data && cs.calib_k0_only) {
          const dateStr = cs.calib_calibrated_at ? cs.calib_calibrated_at.slice(0, 10) : '';
          infoRow.innerHTML = [
            `<span style="color:#e8a43c;font-weight:600;">K₀ only</span>`,
            dateStr ? `&nbsp;|&nbsp;${dateStr}` : '',
          ].join('');
          infoRow.dataset.hasData = 'true';
        } else if (cs.calib_has_data) {
          const rms    = cs.calib_rms;
          const rmsCol = rms < 0.5 ? '#7dcf7d' : rms < 1.0 ? '#e8a43c' : '#e74c3c';
          const dateStr = cs.calib_calibrated_at ? cs.calib_calibrated_at.slice(0, 10) : '';
          infoRow.innerHTML = [
            `RMS <span style="color:${rmsCol};font-weight:600;">${rms.toFixed(3)}</span>`,
            `&nbsp;|&nbsp;${cs.calib_shot_count} shots`,
            dateStr ? `&nbsp;|&nbsp;${dateStr}` : '',
          ].join('');
          infoRow.dataset.hasData = 'true';
        } else {
          infoRow.textContent     = 'No calibration data';
          infoRow.dataset.hasData = 'false';
        }
      }
    });
  }

  socket.on('state', _applyCalibState);
  window.addEventListener('plugin-state-update', e => _applyCalibState(e.detail));

}());
