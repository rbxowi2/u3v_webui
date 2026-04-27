// stereocalibrate/ui.js — StereoCalibrate plugin frontend (1.0.0)

(function () {
  'use strict';

  const SAVE_MIN_SHOTS = 5;

  // ── Module state ─────────────────────────────────────────────────────────
  let _modal  = null;
  let _canvas = null;
  let _ctx    = null;
  let _camId  = '';   // cam_id of the plugin instance that owns this session

  let _lensType = 'normal';
  let _camLeft  = '';
  let _camRight = '';
  let _showSide = 'L';   // which camera is displayed on canvas

  let _shotCenters  = [];
  let _accepted     = 0;
  let _rejected     = 0;
  let _lastRms      = null;
  let _lastAction   = '';
  let _lastReason   = '';
  let _sessionSaved = false;
  let _missingCal   = [];

  let _autoEnabled = false;

  let _detectFoundL   = false;
  let _detectFoundR   = false;
  let _detectCornersL = null;
  let _detectCornersR = null;
  let _detectCols     = 0;
  let _detectRows     = 0;
  let _autoFlashEnd   = 0;

  let _pollTimer = null;
  let _fetching  = false;

  // DOM refs
  let _elDetectIndL    = null;
  let _elDetectIndR    = null;
  let _elToggleSideL   = null;
  let _elToggleSideR   = null;
  let _elAccepted      = null;
  let _elRejected      = null;
  let _elRms           = null;
  let _elLastAction    = null;
  let _elMissingCal    = null;
  let _elBtnStartPause = null;
  let _elBtnReset      = null;
  let _elBtnToggle     = null;
  let _elBtnSave       = null;
  let _elBtnCancel     = null;
  let _elBtnRemoveShot = null;
  let _coverageCanvas  = null;

  // ── DOM helpers ───────────────────────────────────────────────────────────
  function _blk(el)    { return el.closest('.plugin-ui-block'); }
  function _blkCam(el) { const b = _blk(el); return b ? b.dataset.cam : ''; }

  function _mkBtn(text, bg, fg, border, onClick) {
    const b = document.createElement('button');
    b.textContent = text;
    b.className = 'btn';
    Object.assign(b.style, {
      background: bg, color: fg, border: `1px solid ${border}`,
      borderRadius: '3px', padding: '5px 10px', fontSize: '12px',
      cursor: 'pointer', width: '100%',
    });
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }

  function _mkSmBtn(text, active, onClick) {
    const b = document.createElement('button');
    b.textContent = text;
    b.className = 'btn';
    Object.assign(b.style, {
      flex: '1', fontSize: '11px', padding: '3px 0',
      borderRadius: '3px', cursor: 'pointer',
    });
    b._setActive = (v) => {
      b.style.background = v ? '#2a5a8c' : '#2a2a2a';
      b.style.color       = v ? '#90ccf0' : '#888';
      b.style.border      = v ? '1px solid #3a7abc' : '1px solid #444';
    };
    b._setActive(active);
    b.addEventListener('click', onClick);
    return b;
  }

  function _setBtnEnabled(btn, enabled) {
    btn.disabled = !enabled;
    btn.style.opacity = enabled ? '1' : '0.4';
    btn.style.cursor  = enabled ? 'pointer' : 'not-allowed';
  }

  // ── Modal builder ─────────────────────────────────────────────────────────
  function _buildModal() {
    if (_modal) return;

    _modal = document.createElement('div');
    Object.assign(_modal.style, {
      display: 'none', position: 'fixed', inset: '0', zIndex: '9500',
      background: 'rgba(0,0,0,0.93)', flexDirection: 'column',
      fontFamily: '"Helvetica Neue",Helvetica,Arial,sans-serif',
    });

    // Header
    const hdr = document.createElement('div');
    Object.assign(hdr.style, {
      display: 'flex', alignItems: 'center', padding: '8px 16px',
      background: '#181818', borderBottom: '1px solid #333', flexShrink: '0',
    });
    const title = document.createElement('span');
    title.textContent = 'Stereo Calibration — Auto';
    Object.assign(title.style, {
      flex: '1', textAlign: 'center', fontSize: '14px',
      color: '#d4d4d4', fontWeight: '600',
    });
    hdr.appendChild(title);
    _modal.appendChild(hdr);

    // Body
    const body = document.createElement('div');
    Object.assign(body.style, { flex: '1', display: 'flex', overflow: 'hidden', minHeight: '0' });

    // Canvas area
    const canvasWrap = document.createElement('div');
    Object.assign(canvasWrap.style, {
      flex: '1', display: 'flex', flexDirection: 'column',
      overflow: 'hidden', background: '#000',
    });

    // L/R toggle bar above canvas
    const sideBar = document.createElement('div');
    Object.assign(sideBar.style, {
      display: 'flex', gap: '6px', padding: '6px 8px',
      background: '#111', flexShrink: '0',
    });
    _elToggleSideL = _mkSmBtn('Left (L)', true,  () => _setSide('L'));
    _elToggleSideR = _mkSmBtn('Right (R)', false, () => _setSide('R'));
    sideBar.append(_elToggleSideL, _elToggleSideR);

    const canvasInner = document.createElement('div');
    Object.assign(canvasInner.style, {
      flex: '1', minHeight: '0', display: 'flex',
      alignItems: 'center', justifyContent: 'center', overflow: 'hidden',
    });
    _canvas = document.createElement('canvas');
    Object.assign(_canvas.style, { maxWidth: '100%', maxHeight: '100%', display: 'block' });
    _ctx = _canvas.getContext('2d');
    canvasInner.appendChild(_canvas);

    canvasWrap.append(sideBar, canvasInner);

    // Right panel
    const panel = document.createElement('div');
    Object.assign(panel.style, {
      width: '268px', flexShrink: '0', background: '#1a1a1a',
      borderLeft: '1px solid #333', display: 'flex', flexDirection: 'column',
      padding: '12px 10px', gap: '8px', overflowY: 'auto',
    });

    // Detection indicators
    _elDetectIndL = document.createElement('div');
    _elDetectIndR = document.createElement('div');
    for (const [el, lbl] of [[_elDetectIndL, 'L'], [_elDetectIndR, 'R']]) {
      el.textContent = `${lbl}: —`;
      Object.assign(el.style, {
        fontSize: '11px', color: '#666', fontStyle: 'italic',
        textAlign: 'center', padding: '1px 0',
      });
    }

    // Missing cal warning
    _elMissingCal = document.createElement('div');
    Object.assign(_elMissingCal.style, {
      fontSize: '10px', color: '#c07040', display: 'none',
      background: '#2a1a00', border: '1px solid #6a3a00',
      borderRadius: '3px', padding: '4px 6px', lineHeight: '1.5',
    });

    // Stats box
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
    Object.assign(rmsRow.style, { display: 'flex', alignItems: 'center', marginTop: '4px' });
    const rmsLbl = document.createElement('span');
    rmsLbl.textContent = 'RMS';
    Object.assign(rmsLbl.style, { fontSize: '11px', color: '#777', flex: '1' });
    _elRms = document.createElement('span');
    Object.assign(_elRms.style, {
      fontSize: '13px', color: '#7dcf7d', fontWeight: '700',
    });
    rmsRow.append(rmsLbl, _elRms);
    statsWrap.appendChild(rmsRow);

    _elLastAction = document.createElement('div');
    Object.assign(_elLastAction.style, {
      fontSize: '10px', color: '#666', lineHeight: '1.5', marginTop: '2px',
    });

    // Shot coverage mini-map
    _coverageCanvas = document.createElement('canvas');
    _coverageCanvas.width  = 200;
    _coverageCanvas.height = 60;
    Object.assign(_coverageCanvas.style, {
      width: '100%', height: '60px', border: '1px solid #333',
      borderRadius: '2px', background: '#111', display: 'block',
    });

    // Auto-capture toggle
    const autoRow = document.createElement('div');
    Object.assign(autoRow.style, { display: 'flex', gap: '4px' });
    _elBtnStartPause = _mkSmBtn('開始', false, _onToggleAuto);
    autoRow.appendChild(_elBtnStartPause);

    // Action buttons
    _elBtnReset      = _mkBtn('Reset',       '#2a1a1a', '#c87070', '#5a2a2a', _onReset);
    _elBtnRemoveShot = _mkBtn('Remove Last', '#2a2a1a', '#c8b070', '#5a4a1a', _onRemoveShot);
    _elBtnToggle     = _mkBtn('Show Right',  '#1a1a2a', '#7070c8', '#2a2a5a', _onToggleView);
    _elBtnSave       = _mkBtn('Save',        '#1a3a1a', '#7dcf7d', '#2a6a2a', _onSave);
    _elBtnCancel     = _mkBtn('Close',       '#2a2a2a', '#888',    '#444',    _onCancel);
    _setBtnEnabled(_elBtnSave, false);

    panel.append(
      _elDetectIndL, _elDetectIndR, _elMissingCal,
      statsWrap, _elLastAction, _coverageCanvas,
      autoRow,
      _elBtnReset, _elBtnRemoveShot, _elBtnToggle, _elBtnSave, _elBtnCancel,
    );

    body.append(canvasWrap, panel);
    _modal.appendChild(body);
    document.body.appendChild(_modal);
  }

  // ── Coverage map ──────────────────────────────────────────────────────────
  function _drawCoverage() {
    if (!_coverageCanvas) return;
    const c = _coverageCanvas.getContext('2d');
    const W = _coverageCanvas.width, H = _coverageCanvas.height;
    c.clearRect(0, 0, W, H);
    c.fillStyle = '#111';
    c.fillRect(0, 0, W, H);
    _shotCenters.forEach((sc, i) => {
      const age = (i + 1) / Math.max(_shotCenters.length, 1);
      c.fillStyle = `rgba(125,207,125,${0.35 + 0.65 * age})`;
      c.beginPath();
      c.arc(sc[0] * W, sc[1] * H, 4, 0, Math.PI * 2);
      c.fill();
    });
    if (Date.now() < _autoFlashEnd) {
      c.fillStyle = 'rgba(125,207,125,0.18)';
      c.fillRect(0, 0, W, H);
    }
  }

  // ── Camera frame polling ──────────────────────────────────────────────────
  function _startPoll() {
    _stopPoll();
    _pollTimer = setInterval(_pollFrame, 120);
  }

  function _stopPoll() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  }

  function _pollFrame() {
    if (_fetching) return;
    const camId = _showSide === 'L' ? _camLeft : _camRight;
    if (!camId) return;
    _fetching = true;
    const img = new Image();
    img.onload = () => {
      _fetching = false;
      if (!_canvas) return;
      if (_canvas.width !== img.naturalWidth || _canvas.height !== img.naturalHeight) {
        _canvas.width  = img.naturalWidth;
        _canvas.height = img.naturalHeight;
      }
      _ctx.drawImage(img, 0, 0);
      _drawOverlay();
    };
    img.onerror = () => { _fetching = false; };
    img.src = `/plugin/stereocalibrate/snapshot?cam_id=${encodeURIComponent(camId)}&_t=${Date.now()}`;
  }

  // ── Canvas overlay ────────────────────────────────────────────────────────
  function _drawOverlay() {
    if (!_canvas || !_ctx) return;
    const W = _canvas.width, H = _canvas.height;
    if (!W || !H) return;

    const isL     = _showSide === 'L';
    const found   = isL ? _detectFoundL : _detectFoundR;
    const corners = isL ? _detectCornersL : _detectCornersR;

    if (!found || !corners || !corners.length) return;

    const cols  = _detectCols, rows = _detectRows;
    const pts   = corners.map(c => [c[0] * W, c[1] * H]);
    const flash = Date.now() < _autoFlashEnd;

    _ctx.strokeStyle = flash ? 'rgba(125,207,125,0.95)' : 'rgba(90,200,90,0.7)';
    _ctx.lineWidth   = 1.5;

    for (let r = 0; r < rows; r++) {
      _ctx.beginPath();
      for (let c = 0; c < cols; c++) {
        const pt = pts[r * cols + c];
        if (c === 0) _ctx.moveTo(pt[0], pt[1]);
        else         _ctx.lineTo(pt[0], pt[1]);
      }
      _ctx.stroke();
    }
    for (let c = 0; c < cols; c++) {
      _ctx.beginPath();
      for (let r = 0; r < rows; r++) {
        const pt = pts[r * cols + c];
        if (r === 0) _ctx.moveTo(pt[0], pt[1]);
        else         _ctx.lineTo(pt[0], pt[1]);
      }
      _ctx.stroke();
    }

    _ctx.fillStyle = flash ? 'rgba(255,255,100,0.9)' : 'rgba(255,220,80,0.75)';
    pts.forEach(pt => {
      _ctx.beginPath();
      _ctx.arc(pt[0], pt[1], 2.5, 0, Math.PI * 2);
      _ctx.fill();
    });

    _drawCoverage();
  }

  // ── UI updates ────────────────────────────────────────────────────────────
  function _updateDetectInds() {
    if (_elDetectIndL) {
      _elDetectIndL.textContent = _detectFoundL ? 'L: Board detected' : 'L: —';
      _elDetectIndL.style.color  = _detectFoundL ? '#7dcf7d' : '#666';
    }
    if (_elDetectIndR) {
      _elDetectIndR.textContent = _detectFoundR ? 'R: Board detected' : 'R: —';
      _elDetectIndR.style.color  = _detectFoundR ? '#7dcf7d' : '#666';
    }
  }

  function _updateStats() {
    if (_elAccepted) _elAccepted.textContent = String(_accepted);
    if (_elRejected) _elRejected.textContent = String(_rejected);
    if (_elRms) {
      if (_lastRms !== null) {
        _elRms.textContent  = _lastRms.toFixed(4);
        _elRms.style.color  = _lastRms < 1.0 ? '#7dcf7d' : (_lastRms < 2.0 ? '#c8b070' : '#c87070');
      } else {
        _elRms.textContent  = '—';
        _elRms.style.color  = '#888';
      }
    }
    if (_elLastAction) {
      let txt = _lastAction || '';
      if (_lastReason) txt += ` — ${_lastReason}`;
      _elLastAction.textContent = txt;
      _elLastAction.style.color = _lastAction === 'accepted' ? '#7dcf7d'
        : (_lastAction === 'rejected' ? '#c87070' : '#666');
    }
    _updateSaveBtn();
    _drawCoverage();
  }

  function _updateSaveBtn() {
    if (!_elBtnSave) return;
    _setBtnEnabled(_elBtnSave, _accepted >= SAVE_MIN_SHOTS && !_sessionSaved);
  }

  function _setSide(side) {
    _showSide = side;
    if (_elToggleSideL) _elToggleSideL._setActive(side === 'L');
    if (_elToggleSideR) _elToggleSideR._setActive(side === 'R');
    if (_elBtnToggle)   _elBtnToggle.textContent = side === 'L' ? 'Show Right' : 'Show Left';
  }

  function _updateAutoBtn() {
    if (!_elBtnStartPause) return;
    _elBtnStartPause.textContent = _autoEnabled ? '暫停' : '開始';
    _elBtnStartPause._setActive(_autoEnabled);
  }

  function _showMissingCalWarning() {
    if (!_elMissingCal) return;
    if (_missingCal.length) {
      _elMissingCal.textContent = `No LensCalibrate data: ${_missingCal.join(', ')}`;
      _elMissingCal.style.display = 'block';
    } else {
      _elMissingCal.style.display = 'none';
    }
  }

  // ── Actions ───────────────────────────────────────────────────────────────
  function _onToggleAuto() {
    if (!_camId) return;
    socket.emit('stereo_toggle_auto', { cam_id: _camId });
  }

  function _onReset() {
    if (!_camId) return;
    socket.emit('stereo_reset', { cam_id: _camId });
    _accepted = 0; _rejected = 0;
    _shotCenters = []; _lastRms = null;
    _lastAction = ''; _lastReason = '';
    _updateStats();
  }

  function _onRemoveShot() {
    if (!_camId) return;
    socket.emit('stereo_remove_shot', { cam_id: _camId });
  }

  function _onToggleView() {
    _setSide(_showSide === 'L' ? 'R' : 'L');
  }

  function _onSave() {
    if (!_camId) return;
    socket.emit('stereo_save', { cam_id: _camId });
  }

  function _onCancel() {
    if (!_camId) return;
    socket.emit('stereo_cancel', { cam_id: _camId });
    _closeModal();
  }

  function _closeModal() {
    _stopPoll();
    if (_modal) _modal.style.display = 'none';
    _autoEnabled = false;
    _updateAutoBtn();
  }

  // ── Open modal ────────────────────────────────────────────────────────────
  window.stereoOpenModal = function (btnEl) {
    const block  = _blk(btnEl);
    const camId  = block ? block.dataset.cam : '';
    if (!camId) return;

    _buildModal();
    _camId = camId;

    _lensType = block.querySelector('.stereo-lens-type')?.value  || 'normal';
    _camLeft  = block.querySelector('.stereo-cam-left')?.value   || '';
    _camRight = block.querySelector('.stereo-cam-right')?.value  || '';

    // Reset state
    _accepted = 0; _rejected = 0;
    _shotCenters = []; _lastRms = null;
    _lastAction = ''; _lastReason = '';
    _sessionSaved = false;
    _autoEnabled  = false;
    _detectFoundL = false; _detectFoundR = false;
    _detectCornersL = null; _detectCornersR = null;
    _missingCal = [];
    _showSide = 'L';

    _updateStats();
    _updateAutoBtn();
    _updateDetectInds();
    _setSide('L');
    _showMissingCalWarning();

    _modal.style.display = 'flex';
    _startPoll();

    socket.emit('stereo_start', {
      cam_id:    camId,
      cam_left:  _camLeft,
      cam_right: _camRight,
      lens_type: _lensType,
    });
  };

  // ── Socket handlers ───────────────────────────────────────────────────────
  socket.on('stereo_event', (data) => {
    if (data.type === 'started') {
      _missingCal = data.missing_cal || [];
      _showMissingCalWarning();
    } else if (data.type === 'save_result') {
      if (data.ok) {
        _sessionSaved = true;
        _updateSaveBtn();
        if (_elLastAction) {
          _elLastAction.textContent = `Saved — RMS ${data.rms?.toFixed(4)} (${data.shot_count} shots)`;
          _elLastAction.style.color = '#7dcf7d';
        }
        setTimeout(_closeModal, 1200);
      } else {
        if (_elLastAction) {
          _elLastAction.textContent = `Save failed: ${data.error}`;
          _elLastAction.style.color = '#c87070';
        }
      }
    } else if (data.type === 'error') {
      if (_elLastAction) {
        _elLastAction.textContent = data.msg || 'Error';
        _elLastAction.style.color = '#c87070';
      }
    }
  });

  socket.on('stereo_auto_toggled', (data) => {
    if (data.cam_id !== _camId) return;
    _autoEnabled = data.enabled;
    _updateAutoBtn();
  });

  socket.on('stereo_detect', (data) => {
    if (data.cam_id !== _camId) return;
    _detectFoundL   = data.found_L;
    _detectFoundR   = data.found_R;
    _detectCornersL = data.corners_L || null;
    _detectCornersR = data.corners_R || null;
    _detectCols     = data.cols || 0;
    _detectRows     = data.rows || 0;
    if (data.auto_triggered) _autoFlashEnd = Date.now() + 400;
    _updateDetectInds();
  });

  socket.on('stereo_auto_status', (data) => {
    if (data.cam_id !== _camId) return;
    if (data.action === 'reset') {
      _accepted = 0; _rejected = 0;
      _shotCenters = []; _lastRms = null;
      _lastAction = 'reset'; _lastReason = '';
    } else if (data.action === 'accepted') {
      _accepted    = data.accepted;
      _rejected    = data.rejected;
      _lastRms     = data.rms !== null ? data.rms : _lastRms;
      _lastAction  = 'accepted';
      _lastReason  = data.reason || '';
      _shotCenters = data.shot_centers || [];
    } else if (data.action === 'rejected') {
      _rejected   = data.rejected;
      _accepted   = data.accepted;
      _lastAction = 'rejected';
      _lastReason = data.reason || '';
    } else if (data.action === 'remove') {
      _accepted    = data.accepted;
      _rejected    = data.rejected;
      _lastRms     = data.rms !== null ? data.rms : _lastRms;
      _lastAction  = 'removed';
      _lastReason  = '';
      _shotCenters = data.shot_centers || [];
    }
    _updateStats();
  });

  // ── Sidebar param handlers ────────────────────────────────────────────────
  window.stereoOnCamLeft = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'stereo_cam_left', value: el.value });
    _updateSidebarInfoRow(_blk(el));
  };

  window.stereoOnCamRight = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'stereo_cam_right', value: el.value });
    _updateSidebarInfoRow(_blk(el));
  };

  window.stereoOnLensType = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'stereo_lens_type', value: el.value });
  };

  window.stereoOnBoardCols = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'stereo_board_cols', value: parseInt(el.value, 10) });
  };

  window.stereoOnBoardRows = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'stereo_board_rows', value: parseInt(el.value, 10) });
  };

  window.stereoOnSquareSize = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'stereo_square_size', value: parseFloat(el.value) });
  };

  function _updateSidebarInfoRow(block) {
    if (!block) return;
    const infoEl = block.querySelector('.stereo-info-row');
    if (!infoEl || infoEl.dataset.hasData === 'true') return;
    const L = block.querySelector('.stereo-cam-left')?.value  || '';
    const R = block.querySelector('.stereo-cam-right')?.value || '';
    infoEl.textContent = (L && R) ? `${L} ↔ ${R}` : '未設定相機';
    infoEl.style.color = '#666';
  }

  // ── State sync ────────────────────────────────────────────────────────────
  function _applyStereoState(s) {
    const cameras = s.cameras || {};
    const camIds  = Object.keys(cameras);

    document.querySelectorAll('.plugin-ui-block[data-plugin="StereoCalibrate"]').forEach(block => {
      const cid = block.dataset.cam;
      const cs  = cameras[cid] || null;
      if (!cs) return;

      // Populate L/R dropdowns with all available cameras
      ['stereo-cam-left', 'stereo-cam-right'].forEach(cls => {
        const sel = block.querySelector(`.${cls}`);
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = '<option value="">— select —</option>';
        camIds.forEach(id => {
          const opt = document.createElement('option');
          opt.value = id; opt.textContent = id;
          sel.appendChild(opt);
        });
        // Restore saved or state value
        const stateVal = cls === 'stereo-cam-left' ? cs.stereo_cam_left : cs.stereo_cam_right;
        sel.value = stateVal || cur || '';
      });

      const selLens = block.querySelector('.stereo-lens-type');
      const inpCols = block.querySelector('.stereo-board-cols');
      const inpRows = block.querySelector('.stereo-board-rows');
      const inpSq   = block.querySelector('.stereo-square-size');
      const infoRow = block.querySelector('.stereo-info-row');

      if (selLens && cs.stereo_lens_type  !== undefined) selLens.value = cs.stereo_lens_type;
      if (inpCols && cs.stereo_board_cols !== undefined) inpCols.value = cs.stereo_board_cols;
      if (inpRows && cs.stereo_board_rows !== undefined) inpRows.value = cs.stereo_board_rows;
      if (inpSq   && cs.stereo_square_size!== undefined) inpSq.value   = cs.stereo_square_size;

      if (infoRow) {
        const L = block.querySelector('.stereo-cam-left')?.value  || cs.stereo_cam_left || '';
        const R = block.querySelector('.stereo-cam-right')?.value || cs.stereo_cam_right || '';
        const pair = (L && R) ? `${L} ↔ ${R}` : '';
        if (cs.stereo_has_data) {
          const rms    = cs.stereo_rms;
          const rmsCol = rms < 0.5 ? '#7dcf7d' : rms < 1.0 ? '#e8a43c' : '#e74c3c';
          const date   = cs.stereo_calibrated_at ? cs.stereo_calibrated_at.slice(0, 10) : '';
          infoRow.innerHTML = [
            pair ? `<span style="color:#888;">${pair}</span>&nbsp;|&nbsp;` : '',
            `RMS <span style="color:${rmsCol};font-weight:600;">${rms.toFixed(3)}</span>`,
            `&nbsp;|&nbsp;${cs.stereo_shot_count} shots`,
            date ? `&nbsp;|&nbsp;${date}` : '',
          ].join('');
          infoRow.dataset.hasData = 'true';
        } else {
          infoRow.textContent     = pair || '未設定相機';
          infoRow.style.color     = '#666';
          infoRow.dataset.hasData = 'false';
        }
      }
    });
  }

  socket.on('state', _applyStereoState);
  window.addEventListener('plugin-state-update', e => _applyStereoState(e.detail));

}());
