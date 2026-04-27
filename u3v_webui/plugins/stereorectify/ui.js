// stereorectify/ui.js — StereoRectify plugin frontend (1.0.0)

(function () {
  'use strict';

  // ── Module state ─────────────────────────────────────────────────────────
  let _modal  = null;
  let _canvas = null;
  let _ctx    = null;
  let _camId  = '';

  let _camLeft  = '';
  let _camRight = '';
  let _alpha    = 1.0;
  let _showSide = 'L';

  let _computed   = false;
  let _lensType   = 'normal';
  let _imgSize    = null;   // [w, h]
  let _roi1       = null;   // [x, y, w, h]
  let _roi2       = null;
  let _saved      = false;

  let _pollTimer = null;
  let _fetching  = false;

  // DOM refs
  let _elStatus      = null;
  let _elRoiL        = null;
  let _elRoiR        = null;
  let _elAlphaInput  = null;
  let _elToggleSideL = null;
  let _elToggleSideR = null;
  let _elBtnShowRect = null;
  let _elBtnSave     = null;
  let _elBtnCancel   = null;

  let _showRectified = true;

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
    title.textContent = 'Stereo Rectify';
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
      flex: '1', display: 'flex', flexDirection: 'column', overflow: 'hidden', background: '#000',
    });

    // L/R toggle bar
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
      width: '240px', flexShrink: '0', background: '#1a1a1a',
      borderLeft: '1px solid #333', display: 'flex', flexDirection: 'column',
      padding: '12px 10px', gap: '8px', overflowY: 'auto',
    });

    // Status
    _elStatus = document.createElement('div');
    Object.assign(_elStatus.style, {
      fontSize: '11px', color: '#888', textAlign: 'center',
      padding: '4px 0', lineHeight: '1.5',
    });
    _elStatus.textContent = 'Computing…';

    // Alpha row
    const alphaRow = document.createElement('div');
    Object.assign(alphaRow.style, { display: 'flex', alignItems: 'center', gap: '6px' });
    const alphaLbl = document.createElement('span');
    alphaLbl.textContent = 'Alpha';
    Object.assign(alphaLbl.style, { fontSize: '11px', color: '#777', flex: '1' });
    _elAlphaInput = document.createElement('input');
    _elAlphaInput.type  = 'number';
    _elAlphaInput.min   = '0'; _elAlphaInput.max = '1'; _elAlphaInput.step = '0.05';
    _elAlphaInput.value = '1';
    Object.assign(_elAlphaInput.style, {
      width: '54px', background: '#2a2a2a', color: '#d4d4d4',
      border: '1px solid #444', borderRadius: '3px', padding: '3px 4px',
      fontSize: '11px', textAlign: 'center',
    });
    _elAlphaInput.addEventListener('change', () => {
      if (!_camId) return;
      const v = Math.max(0, Math.min(1, parseFloat(_elAlphaInput.value) || 0));
      _elAlphaInput.value = v;
      socket.emit('rectify_set_alpha', { cam_id: _camId, alpha: v });
    });
    const alphaHint = document.createElement('span');
    alphaHint.textContent = '0=crop  1=full';
    Object.assign(alphaHint.style, { fontSize: '10px', color: '#555' });
    alphaRow.append(alphaLbl, _elAlphaInput);

    // ROI info
    const roiBox = document.createElement('div');
    Object.assign(roiBox.style, {
      background: '#222', border: '1px solid #333', borderRadius: '4px',
      padding: '6px 8px', fontSize: '10px', color: '#777', lineHeight: '1.7',
    });
    _elRoiL = document.createElement('div');
    _elRoiR = document.createElement('div');
    roiBox.append(_elRoiL, _elRoiR);
    _elRoiL.textContent = 'ROI L: —';
    _elRoiR.textContent = 'ROI R: —';

    // Toggle raw/rectified
    _elBtnShowRect = _mkBtn('Show Raw', '#1a1a2a', '#7070c8', '#2a2a5a', _onToggleView);

    // Save / Close
    _elBtnSave   = _mkBtn('Save', '#1a3a1a', '#7dcf7d', '#2a6a2a', _onSave);
    _elBtnCancel = _mkBtn('Close', '#2a2a2a', '#888', '#444', _onCancel);
    _setBtnEnabled(_elBtnSave, false);

    panel.append(
      _elStatus, alphaRow, alphaHint, roiBox,
      _elBtnShowRect, _elBtnSave, _elBtnCancel,
    );

    body.append(canvasWrap, panel);
    _modal.appendChild(body);
    document.body.appendChild(_modal);
  }

  // ── Canvas polling ────────────────────────────────────────────────────────
  function _startPoll() {
    _stopPoll();
    _pollTimer = setInterval(_pollFrame, 130);
  }

  function _stopPoll() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  }

  function _pollFrame() {
    if (_fetching) return;
    const src = _showSide === 'L' ? _camLeft : _camRight;
    if (!src) return;
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
      _drawEpipolarLines();
      _drawRoiBox();
    };
    img.onerror = () => { _fetching = false; };
    const rect = (_showRectified && _computed) ? 1 : 0;
    img.src = `/plugin/stereorectify/snapshot?cam_id=${encodeURIComponent(_camId)}&side=${_showSide}&rectified=${rect}&_t=${Date.now()}`;
  }

  // ── Overlay drawing ───────────────────────────────────────────────────────
  function _drawEpipolarLines() {
    if (!_computed || !_showRectified || !_canvas) return;
    const W = _canvas.width, H = _canvas.height;
    if (!W || !H) return;
    _ctx.strokeStyle = 'rgba(0,200,255,0.3)';
    _ctx.lineWidth = 0.8;
    const step = Math.round(H / 12);
    for (let y = step; y < H; y += step) {
      _ctx.beginPath();
      _ctx.moveTo(0, y);
      _ctx.lineTo(W, y);
      _ctx.stroke();
    }
  }

  function _drawRoiBox() {
    if (!_computed || !_showRectified || !_canvas) return;
    const roi = _showSide === 'L' ? _roi1 : _roi2;
    if (!roi) return;
    const W = _canvas.width, H = _canvas.height;
    const iw = _imgSize ? _imgSize[0] : W;
    const ih = _imgSize ? _imgSize[1] : H;
    const sx = W / iw, sy = H / ih;
    const [rx, ry, rw, rh] = roi;
    if (rw < 10 || rh < 10) return;
    _ctx.strokeStyle = 'rgba(125,207,125,0.5)';
    _ctx.lineWidth   = 1.5;
    _ctx.setLineDash([6, 4]);
    _ctx.strokeRect(rx * sx, ry * sy, rw * sx, rh * sy);
    _ctx.setLineDash([]);
  }

  // ── UI updates ────────────────────────────────────────────────────────────
  function _setSide(side) {
    _showSide = side;
    if (_elToggleSideL) _elToggleSideL._setActive(side === 'L');
    if (_elToggleSideR) _elToggleSideR._setActive(side === 'R');
    _updateRoiDisplay();
  }

  function _updateRoiDisplay() {
    if (!_elRoiL || !_elRoiR) return;
    const fmtRoi = roi => roi ? `${roi[2]}×${roi[3]} @ (${roi[0]},${roi[1]})` : '—';
    _elRoiL.textContent = `ROI L: ${fmtRoi(_roi1)}`;
    _elRoiR.textContent = `ROI R: ${fmtRoi(_roi2)}`;
    _elRoiL.style.color = (_roi1 && _roi1[2] > 0) ? '#7dcf7d' : '#888';
    _elRoiR.style.color = (_roi2 && _roi2[2] > 0) ? '#7dcf7d' : '#888';
  }

  function _onToggleView() {
    _showRectified = !_showRectified;
    if (_elBtnShowRect)
      _elBtnShowRect.textContent = _showRectified ? 'Show Raw' : 'Show Rectified';
  }

  function _onSave() {
    if (!_camId) return;
    socket.emit('rectify_save', { cam_id: _camId });
  }

  function _onCancel() {
    if (!_camId) return;
    socket.emit('rectify_cancel', { cam_id: _camId });
    _closeModal();
  }

  function _closeModal() {
    _stopPoll();
    if (_modal) _modal.style.display = 'none';
  }

  // ── Open modal ────────────────────────────────────────────────────────────
  window.rectifyOpenModal = function (btnEl) {
    const block  = btnEl.closest('.plugin-ui-block');
    const camId  = block ? block.dataset.cam : '';
    if (!camId) return;

    _buildModal();
    _camId = camId;

    _camLeft  = block.querySelector('.rectify-cam-left')?.value  || '';
    _camRight = block.querySelector('.rectify-cam-right')?.value || '';

    _computed     = false;
    _saved        = false;
    _showRectified = true;
    _showSide      = 'L';
    _roi1 = null;  _roi2 = null;
    _imgSize = null;

    if (_elStatus)    _elStatus.textContent = 'Computing…';
    if (_elRoiL)      _elRoiL.textContent   = 'ROI L: —';
    if (_elRoiR)      _elRoiR.textContent   = 'ROI R: —';
    if (_elBtnShowRect) _elBtnShowRect.textContent = 'Show Raw';
    if (_elAlphaInput)  _elAlphaInput.value  = String(_alpha);
    _setBtnEnabled(_elBtnSave, false);
    _setSide('L');

    _modal.style.display = 'flex';
    _startPoll();

    socket.emit('rectify_open', {
      cam_id:    camId,
      cam_left:  _camLeft,
      cam_right: _camRight,
      alpha:     _alpha,
    });
  };

  // ── Socket handlers ───────────────────────────────────────────────────────
  socket.on('rectify_event', (data) => {
    if (data.type === 'computed') {
      _computed  = data.ok;
      _lensType  = data.lens_type || 'normal';
      _imgSize   = data.image_size || null;
      _roi1      = data.roi1 || null;
      _roi2      = data.roi2 || null;
      _alpha     = data.alpha != null ? data.alpha : _alpha;
      if (_elAlphaInput) _elAlphaInput.value = _alpha.toFixed(2);
      if (_elStatus) {
        _elStatus.textContent = `Done — ${_lensType} | alpha ${_alpha.toFixed(2)}`;
        _elStatus.style.color = '#7dcf7d';
      }
      _updateRoiDisplay();
      _setBtnEnabled(_elBtnSave, _computed && !_saved);
    } else if (data.type === 'save_result') {
      if (data.ok) {
        _saved = true;
        _setBtnEnabled(_elBtnSave, false);
        if (_elStatus) {
          _elStatus.textContent = `Saved ${data.calibrated_at || ''}`;
          _elStatus.style.color = '#7dcf7d';
        }
      } else {
        if (_elStatus) {
          _elStatus.textContent = `Save failed: ${data.error}`;
          _elStatus.style.color = '#c87070';
        }
      }
    } else if (data.type === 'error') {
      _computed = false;
      if (_elStatus) {
        _elStatus.textContent = data.msg || 'Error';
        _elStatus.style.color = '#c87070';
      }
      _setBtnEnabled(_elBtnSave, false);
    }
  });

  // ── Sidebar param handlers ────────────────────────────────────────────────
  window.rectifyOnCamLeft = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'rectify_cam_left', value: el.value });
    _updateSidebarInfoRow(_blk(el));
  };

  window.rectifyOnCamRight = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'rectify_cam_right', value: el.value });
    _updateSidebarInfoRow(_blk(el));
  };

  function _updateSidebarInfoRow(block) {
    if (!block) return;
    const infoEl = block.querySelector('.rectify-info-row');
    if (!infoEl || infoEl.dataset.hasData === 'true') return;
    const L = block.querySelector('.rectify-cam-left')?.value  || '';
    const R = block.querySelector('.rectify-cam-right')?.value || '';
    infoEl.textContent = (L && R) ? `${L} ↔ ${R}` : '未設定相機';
    infoEl.style.color = '#666';
  }

  // ── State sync ────────────────────────────────────────────────────────────
  function _applyRectifyState(s) {
    const cameras = s.cameras || {};
    const camIds  = Object.keys(cameras);

    document.querySelectorAll('.plugin-ui-block[data-plugin="StereoRectify"]').forEach(block => {
      const cid = block.dataset.cam;
      const cs  = cameras[cid] || null;
      if (!cs) return;

      ['rectify-cam-left', 'rectify-cam-right'].forEach(cls => {
        const sel = block.querySelector(`.${cls}`);
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = '<option value="">— select —</option>';
        camIds.forEach(id => {
          const opt = document.createElement('option');
          opt.value = id; opt.textContent = id;
          sel.appendChild(opt);
        });
        const stateVal = cls === 'rectify-cam-left' ? cs.rectify_cam_left : cs.rectify_cam_right;
        sel.value = stateVal || cur || '';
      });

      const infoRow = block.querySelector('.rectify-info-row');
      if (infoRow) {
        const L = block.querySelector('.rectify-cam-left')?.value  || cs.rectify_cam_left  || '';
        const R = block.querySelector('.rectify-cam-right')?.value || cs.rectify_cam_right || '';
        const pair = (L && R) ? `${L} ↔ ${R}` : '';
        if (cs.rectify_has_data) {
          const date = cs.rectify_calibrated_at ? cs.rectify_calibrated_at.slice(0, 10) : '';
          infoRow.innerHTML = [
            pair ? `<span style="color:#888;">${pair}</span>&nbsp;|&nbsp;` : '',
            `<span style="color:#7dcf7d;font-weight:600;">已校正</span>`,
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

  socket.on('state', _applyRectifyState);
  window.addEventListener('plugin-state-update', e => _applyRectifyState(e.detail));

}());
