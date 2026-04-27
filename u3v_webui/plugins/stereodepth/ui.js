// stereodepth/ui.js — StereoDepth plugin frontend (1.0.0)

(function () {
  'use strict';

  // ── Module state ─────────────────────────────────────────────────────────
  let _modal  = null;
  let _canvas = null;
  let _ctx    = null;
  let _camId  = '';

  let _camLeft  = '';
  let _camRight = '';

  let _clipMin = 200;
  let _clipMax = 5000;

  let _running = false;

  let _pollTimer = null;
  let _fetching  = false;

  // DOM refs
  let _elStatus     = null;
  let _elStatsVld   = null;
  let _elStatsMin   = null;
  let _elStatsMax   = null;
  let _elStatsMed   = null;
  let _elBtnSavePly = null;
  let _elBtnCancel  = null;
  let _elClipMinVal = null;
  let _elClipMaxVal = null;

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

  function _setBtnEnabled(btn, enabled) {
    btn.disabled = !enabled;
    btn.style.opacity = enabled ? '1' : '0.4';
    btn.style.cursor  = enabled ? 'pointer' : 'not-allowed';
  }

  function _mkLabelRow(label, rightEl) {
    const row = document.createElement('div');
    Object.assign(row.style, { display: 'flex', alignItems: 'center', gap: '6px' });
    const lbl = document.createElement('span');
    lbl.textContent = label;
    Object.assign(lbl.style, { fontSize: '11px', color: '#777', flex: '1' });
    row.append(lbl, rightEl);
    return row;
  }

  function _mkSliderRow(label, min, max, step, value, onChange) {
    const wrap = document.createElement('div');
    Object.assign(wrap.style, { display: 'flex', flexDirection: 'column', gap: '2px' });
    const topRow = document.createElement('div');
    Object.assign(topRow.style, {
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    });
    const lbl = document.createElement('span');
    lbl.textContent = label;
    Object.assign(lbl.style, { fontSize: '11px', color: '#777' });
    const valEl = document.createElement('span');
    valEl.textContent = String(value);
    Object.assign(valEl.style, {
      fontSize: '11px', color: '#aaa', minWidth: '48px', textAlign: 'right',
    });
    topRow.append(lbl, valEl);
    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = min; slider.max = max; slider.step = step; slider.value = value;
    Object.assign(slider.style, { width: '100%', accentColor: '#3a7abc' });
    slider.addEventListener('input', () => {
      valEl.textContent = slider.value + ' mm';
      onChange(Number(slider.value));
    });
    valEl.textContent = value + ' mm';
    wrap.append(topRow, slider);
    return { wrap, slider, valEl };
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
    title.textContent = 'Stereo Depth';
    Object.assign(title.style, {
      flex: '1', textAlign: 'center', fontSize: '14px',
      color: '#d4d4d4', fontWeight: '600',
    });
    hdr.appendChild(title);
    _modal.appendChild(hdr);

    // Body
    const body = document.createElement('div');
    Object.assign(body.style, {
      flex: '1', display: 'flex', overflow: 'hidden', minHeight: '0',
    });

    // Canvas area
    const canvasWrap = document.createElement('div');
    Object.assign(canvasWrap.style, {
      flex: '1', display: 'flex', flexDirection: 'column',
      overflow: 'hidden', background: '#000',
    });
    const canvasInner = document.createElement('div');
    Object.assign(canvasInner.style, {
      flex: '1', minHeight: '0', display: 'flex',
      alignItems: 'center', justifyContent: 'center', overflow: 'hidden',
    });
    _canvas = document.createElement('canvas');
    Object.assign(_canvas.style, { maxWidth: '100%', maxHeight: '100%', display: 'block' });
    _ctx = _canvas.getContext('2d');
    canvasInner.appendChild(_canvas);
    canvasWrap.appendChild(canvasInner);

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
    _elStatus.textContent = '等待中…';

    // Stats box
    const statsBox = document.createElement('div');
    Object.assign(statsBox.style, {
      background: '#222', border: '1px solid #333', borderRadius: '4px',
      padding: '6px 8px', fontSize: '10px', color: '#777', lineHeight: '1.7',
    });
    _elStatsVld = document.createElement('div'); _elStatsVld.textContent = '有效 —%';
    _elStatsMin = document.createElement('div'); _elStatsMin.textContent = '最近 — mm';
    _elStatsMed = document.createElement('div'); _elStatsMed.textContent = '中位 — mm';
    _elStatsMax = document.createElement('div'); _elStatsMax.textContent = '最遠 — mm';
    statsBox.append(_elStatsVld, _elStatsMin, _elStatsMed, _elStatsMax);

    // Divider
    const div1 = document.createElement('div');
    div1.style.cssText = 'border-top:1px solid #333;';

    // Clip sliders
    const clipMinSl = _mkSliderRow('Clip min', 0, 2000, 50, _clipMin, v => {
      _clipMin = v;
      _sendParams();
    });
    const clipMaxSl = _mkSliderRow('Clip max', 500, 20000, 500, _clipMax, v => {
      _clipMax = v;
      _sendParams();
    });
    _elClipMinVal = clipMinSl.valEl;
    _elClipMaxVal = clipMaxSl.valEl;

    // Buttons
    _elBtnSavePly = _mkBtn('Save PLY', '#1a2a3a', '#5a90c0', '#2a5a8c', _onSavePly);
    _elBtnCancel  = _mkBtn('Close',    '#2a2a2a', '#888',    '#444',    _onCancel);
    _setBtnEnabled(_elBtnSavePly, false);

    panel.append(
      _elStatus, statsBox, div1,
      clipMinSl.wrap, clipMaxSl.wrap,
      _elBtnSavePly, _elBtnCancel,
    );

    body.append(canvasWrap, panel);
    _modal.appendChild(body);
    document.body.appendChild(_modal);
  }

  // ── Canvas polling ────────────────────────────────────────────────────────
  function _startPoll() {
    _stopPoll();
    _pollTimer = setInterval(_pollFrame, 200);
  }

  function _stopPoll() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  }

  function _pollFrame() {
    if (_fetching || !_running) return;
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
    };
    img.onerror = () => { _fetching = false; };
    img.src = `/plugin/stereodepth/depth?cam_id=${encodeURIComponent(_camId)}&_t=${Date.now()}`;
  }

  // ── Actions ───────────────────────────────────────────────────────────────
  function _sendParams() {
    if (!_camId) return;
    socket.emit('depth_set_params', {
      cam_id:          _camId,
      depth_clip_min:  _clipMin,
      depth_clip_max:  _clipMax,
    });
  }

  function _onSavePly() {
    if (!_camId) return;
    _setBtnEnabled(_elBtnSavePly, false);
    if (_elStatus) { _elStatus.textContent = '生成 PLY…'; _elStatus.style.color = '#aaa'; }
    socket.emit('depth_save_ply', { cam_id: _camId });
  }

  function _onCancel() {
    if (!_camId) return;
    socket.emit('depth_cancel', { cam_id: _camId });
    _closeModal();
  }

  function _closeModal() {
    _stopPoll();
    _running = false;
    if (_modal) _modal.style.display = 'none';
  }

  // ── Open modal ────────────────────────────────────────────────────────────
  window.depthOpenModal = function (btnEl) {
    const block  = btnEl.closest('.plugin-ui-block');
    const camId  = block ? block.dataset.cam : '';
    if (!camId) return;

    _buildModal();
    _camId    = camId;
    _camLeft  = block.querySelector('.depth-cam-left')?.value  || '';
    _camRight = block.querySelector('.depth-cam-right')?.value || '';

    _running = false;
    if (_elStatus)   { _elStatus.textContent = '等待中…'; _elStatus.style.color = '#888'; }
    if (_elStatsVld) _elStatsVld.textContent = '有效 —%';
    if (_elStatsMin) _elStatsMin.textContent = '最近 — mm';
    if (_elStatsMed) _elStatsMed.textContent = '中位 — mm';
    if (_elStatsMax) _elStatsMax.textContent = '最遠 — mm';
    _setBtnEnabled(_elBtnSavePly, false);

    _modal.style.display = 'flex';
    _startPoll();

    socket.emit('depth_open', {
      cam_id:    camId,
      cam_left:  _camLeft,
      cam_right: _camRight,
    });
  };

  // ── Sidebar: Save PLY (outside modal) ────────────────────────────────────
  window.depthSavePly = function (btnEl) {
    const block = _blk(btnEl);
    if (!block) return;
    const c = block.dataset.cam;
    if (!c) return;
    socket.emit('depth_save_ply', { cam_id: c });
  };

  // ── Socket handlers ───────────────────────────────────────────────────────
  socket.on('depth_event', (data) => {
    if (data.type === 'started') {
      _running = true;
      if (_elStatus) {
        _elStatus.textContent = '執行中…';
        _elStatus.style.color = '#7dcf7d';
      }
      _setBtnEnabled(_elBtnSavePly, true);
    } else if (data.type === 'ply_result') {
      if (data.ok) {
        if (_elStatus) {
          _elStatus.textContent = `PLY 已儲存 (${data.n_points} pts)`;
          _elStatus.style.color = '#7dcf7d';
        }
        // Trigger browser download
        const a = document.createElement('a');
        a.href = `/plugin/stereodepth/download_ply?cam_id=${encodeURIComponent(_camId)}&_t=${Date.now()}`;
        a.download = data.filename || 'pointcloud.ply';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        if (_elBtnSavePly) _setBtnEnabled(_elBtnSavePly, true);
      } else {
        if (_elStatus) {
          _elStatus.textContent = `PLY 失敗: ${data.error}`;
          _elStatus.style.color = '#c87070';
        }
        if (_elBtnSavePly) _setBtnEnabled(_elBtnSavePly, true);
      }
    } else if (data.type === 'error') {
      _running = false;
      if (_elStatus) {
        _elStatus.textContent = data.msg || 'Error';
        _elStatus.style.color = '#c87070';
      }
      _setBtnEnabled(_elBtnSavePly, false);
    }
  });

  socket.on('depth_stats', (data) => {
    if (data.cam_id !== _camId) return;
    if (_elStatsVld) {
      _elStatsVld.textContent = `有效 ${data.valid_pct}%`;
      _elStatsVld.style.color = data.valid_pct > 50 ? '#7dcf7d' : '#c87070';
    }
    if (_elStatsMin) _elStatsMin.textContent = `最近 ${data.min_d} mm`;
    if (_elStatsMed) _elStatsMed.textContent = `中位 ${data.med_d} mm`;
    if (_elStatsMax) _elStatsMax.textContent = `最遠 ${data.max_d} mm`;
  });

  // ── Sidebar param handlers ────────────────────────────────────────────────
  window.depthOnCamLeft = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'depth_cam_left', value: el.value });
    _updateSidebarInfoRow(_blk(el));
  };

  window.depthOnCamRight = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'depth_cam_right', value: el.value });
    _updateSidebarInfoRow(_blk(el));
  };

  window.depthSetSide = function (btnEl, side) {
    const block = _blk(btnEl);
    if (!block) return;
    const c = block.dataset.cam;
    if (c) socket.emit('set_param', { cam_id: c, key: 'depth_display_side', value: side });
    _updateSideBtns(block, side);
  };

  window.depthToggleEnable = function (btnEl) {
    const block = _blk(btnEl);
    if (!block) return;
    const c = block.dataset.cam;
    if (!c) return;
    const isEnabled = btnEl.dataset.enabled === 'true';
    const next = !isEnabled;
    socket.emit('set_param', { cam_id: c, key: 'depth_enabled', value: next });
    _setEnableBtn(btnEl, next);
  };

  function _updateSideBtns(block, side) {
    const btnL = block.querySelector('.depth-side-L');
    const btnR = block.querySelector('.depth-side-R');
    [btnL, btnR].forEach(b => {
      if (!b) return;
      const active = b.classList.contains(`depth-side-${side}`);
      b.style.background = active ? '#2a5a8c' : '#2a2a2a';
      b.style.color       = active ? '#90ccf0' : '#888';
      b.style.border      = active ? '1px solid #3a7abc' : '1px solid #444';
    });
  }

  function _setEnableBtn(btn, enabled) {
    btn.dataset.enabled  = String(enabled);
    btn.textContent      = enabled ? 'Disable' : 'Enable';
    btn.style.background = enabled ? '#3a1a1a' : '#2a2a2a';
    btn.style.color      = enabled ? '#c87070' : '#888';
    btn.style.border     = enabled ? '1px solid #7a3030' : '1px solid #444';
  }

  function _updateSidebarInfoRow(block) {
    if (!block) return;
    const infoEl = block.querySelector('.depth-info-row');
    if (!infoEl || infoEl.dataset.hasData === 'true') return;
    const L = block.querySelector('.depth-cam-left')?.value  || '';
    const R = block.querySelector('.depth-cam-right')?.value || '';
    infoEl.textContent = (L && R) ? `${L} ↔ ${R}` : '未設定相機';
    infoEl.style.color = '#666';
  }

  // ── State sync ────────────────────────────────────────────────────────────
  function _applyDepthState(s) {
    const cameras = s.cameras || {};
    const camIds  = Object.keys(cameras);

    document.querySelectorAll('.plugin-ui-block[data-plugin="StereoDepth"]').forEach(block => {
      const cid = block.dataset.cam;
      const cs  = cameras[cid] || null;
      if (!cs) return;

      ['depth-cam-left', 'depth-cam-right'].forEach(cls => {
        const sel = block.querySelector(`.${cls}`);
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = '<option value="">— select —</option>';
        camIds.forEach(id => {
          const opt = document.createElement('option');
          opt.value = id; opt.textContent = id;
          sel.appendChild(opt);
        });
        const stateVal = cls === 'depth-cam-left'
          ? cs.depth_cam_left
          : cs.depth_cam_right;
        sel.value = stateVal || cur || '';
      });

      // L/R side buttons
      const side = cs.depth_display_side || 'L';
      _updateSideBtns(block, side);

      // Enable button
      const enableBtn = block.querySelector('.depth-btn-enable');
      if (enableBtn) {
        _setEnableBtn(enableBtn, !!cs.depth_enabled);
        const canEnable = !!cs.depth_has_data;
        enableBtn.disabled      = !canEnable;
        enableBtn.style.opacity = canEnable ? '1' : '0.4';
        enableBtn.style.cursor  = canEnable ? 'pointer' : 'not-allowed';
      }

      // Save PLY button in sidebar
      const plyBtn = block.querySelector('.depth-btn-save-ply');
      if (plyBtn) {
        const show = !!cs.depth_enabled;
        plyBtn.style.display = show ? '' : 'none';
      }

      const infoRow = block.querySelector('.depth-info-row');
      if (infoRow) {
        const L = block.querySelector('.depth-cam-left')?.value  || cs.depth_cam_left  || '';
        const R = block.querySelector('.depth-cam-right')?.value || cs.depth_cam_right || '';
        const pair = (L && R) ? `${L} ↔ ${R}` : '';
        if (cs.depth_has_data) {
          infoRow.innerHTML = [
            pair ? `<span style="color:#888;">${pair}</span>&nbsp;|&nbsp;` : '',
            `<span style="color:#7dcf7d;font-weight:600;">已設定</span>`,
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

  socket.on('state', _applyDepthState);
  window.addEventListener('plugin-state-update', e => _applyDepthState(e.detail));

}());
