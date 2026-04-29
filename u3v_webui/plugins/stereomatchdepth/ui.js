// stereomatchdepth/ui.js — StereoMatch & Depth plugin frontend (1.0.0)

(function () {
  'use strict';

  // ── Module state ──────────────────────────────────────────────────────────
  let _modal  = null;
  let _canvas = null;
  let _ctx    = null;
  let _camId  = '';
  let _camLeft  = '';
  let _camRight = '';

  // Session-only (not saved)
  let _viewMode = 'disparity';   // 'disparity' | 'depth'
  let _algo     = 'sgbm';        // 'bm' | 'sgbm'

  // Saved numeric params
  let _nd  = 64;
  let _bs  = 9;
  let _p1  = -1;
  let _p2  = -1;
  let _ur  = 5;
  let _sws = 100;
  let _sr  = 2;
  let _clipMin = 200;
  let _clipMax = 5000;

  let _running = false;

  let _pollTimer = null;
  let _fetching  = false;

  // ── DOM refs ──────────────────────────────────────────────────────────────
  let _elStatus     = null;
  // Disparity stats
  let _elDAvg = null;
  let _elDVld = null;
  // Depth stats
  let _elZVld = null;
  let _elZMin = null;
  let _elZMed = null;
  let _elZMax = null;
  // Modal view/algo toggles
  let _elMViewDisp  = null;
  let _elMViewDepth = null;
  let _elMAlgoBM    = null;
  let _elMAlgoSGBM  = null;
  // Algorithm params
  let _elNdSel     = null;
  let _elBsSel     = null;
  let _elP1Row     = null;
  let _elP1Input   = null;
  let _elP1Auto    = null;
  let _elP2Row     = null;
  let _elP2Input   = null;
  let _elP2Auto    = null;
  let _elUrSlider  = null; let _elUrVal  = null;
  let _elSwsSlider = null; let _elSwsVal = null;
  let _elSrSlider  = null; let _elSrVal  = null;
  // Depth clip sliders
  let _elClipMinSlider = null; let _elClipMinVal = null;
  let _elClipMaxSlider = null; let _elClipMaxVal = null;
  // Buttons
  let _elBtnSave   = null;
  let _elBtnSavePly = null;
  let _elBtnCancel = null;

  // ── DOM helpers ───────────────────────────────────────────────────────────
  function _blk(el)    { return el.closest('.plugin-ui-block'); }
  function _blkCam(el) { const b = _blk(el); return b ? b.dataset.cam : ''; }

  function _mkBtn(text, bg, fg, border, onClick) {
    const b = document.createElement('button');
    b.textContent = text; b.className = 'btn';
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
    b.textContent = text; b.className = 'btn';
    Object.assign(b.style, {
      flex: '1', fontSize: '11px', padding: '3px 0',
      borderRadius: '3px', cursor: 'pointer',
    });
    b._setActive = v => {
      b.style.background = v ? '#2a5a8c' : '#2a2a2a';
      b.style.color       = v ? '#90ccf0' : '#888';
      b.style.border      = v ? '1px solid #3a7abc' : '1px solid #444';
    };
    b._setActive(active);
    b.addEventListener('click', onClick);
    return b;
  }

  function _setBtnEnabled(btn, enabled) {
    if (!btn) return;
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

  function _mkSelect(options, value, onChange) {
    const sel = document.createElement('select');
    Object.assign(sel.style, {
      background: '#2a2a2a', color: '#d4d4d4',
      border: '1px solid #444', borderRadius: '3px',
      padding: '2px 4px', fontSize: '11px', cursor: 'pointer',
    });
    options.forEach(v => {
      const o = document.createElement('option');
      o.value = o.textContent = String(v);
      sel.appendChild(o);
    });
    sel.value = String(value);
    sel.addEventListener('change', () => onChange(Number(sel.value)));
    return sel;
  }

  function _mkSliderRow(label, min, max, step, value, unit, onChange) {
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
    valEl.textContent = value + (unit ? ' ' + unit : '');
    Object.assign(valEl.style, {
      fontSize: '11px', color: '#aaa', minWidth: '52px', textAlign: 'right',
    });
    topRow.append(lbl, valEl);
    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = min; slider.max = max; slider.step = step; slider.value = value;
    Object.assign(slider.style, { width: '100%', accentColor: '#3a7abc' });
    slider.addEventListener('input', () => {
      valEl.textContent = slider.value + (unit ? ' ' + unit : '');
      onChange(Number(slider.value));
    });
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
    title.textContent = 'Stereo Match & Depth';
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
      width: '250px', flexShrink: '0', background: '#1a1a1a',
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
    const dispHdr = document.createElement('div');
    dispHdr.textContent = 'Disparity';
    Object.assign(dispHdr.style, { color: '#555', fontSize: '9px', textTransform: 'uppercase', marginBottom: '1px' });
    _elDAvg = document.createElement('div'); _elDAvg.textContent = '  平均 — px';
    _elDVld = document.createElement('div'); _elDVld.textContent = '  有效 —%';
    const depthHdr = document.createElement('div');
    depthHdr.textContent = 'Depth';
    Object.assign(depthHdr.style, { color: '#555', fontSize: '9px', textTransform: 'uppercase', marginTop: '4px', marginBottom: '1px' });
    _elZVld = document.createElement('div'); _elZVld.textContent = '  有效 —%';
    _elZMin = document.createElement('div'); _elZMin.textContent = '  最近 — mm';
    _elZMed = document.createElement('div'); _elZMed.textContent = '  中位 — mm';
    _elZMax = document.createElement('div'); _elZMax.textContent = '  最遠 — mm';
    statsBox.append(dispHdr, _elDAvg, _elDVld, depthHdr, _elZVld, _elZMin, _elZMed, _elZMax);

    // Divider
    const mkDiv = () => {
      const d = document.createElement('div');
      d.style.cssText = 'border-top:1px solid #333;';
      return d;
    };

    // ── View toggle ────────────────────────────────────────────────────────
    const viewRow = document.createElement('div');
    Object.assign(viewRow.style, { display: 'flex', gap: '6px' });
    _elMViewDisp  = _mkSmBtn('Disparity', _viewMode === 'disparity', () => _applyView('disparity', true));
    _elMViewDepth = _mkSmBtn('Depth',     _viewMode === 'depth',     () => _applyView('depth',     true));
    viewRow.append(_elMViewDisp, _elMViewDepth);

    // ── Algorithm toggle ───────────────────────────────────────────────────
    const algoRow = document.createElement('div');
    Object.assign(algoRow.style, { display: 'flex', gap: '6px' });
    _elMAlgoBM   = _mkSmBtn('BM',   _algo === 'bm',   () => _applyAlgo('bm',   true));
    _elMAlgoSGBM = _mkSmBtn('SGBM', _algo === 'sgbm', () => _applyAlgo('sgbm', true));
    algoRow.append(_elMAlgoBM, _elMAlgoSGBM);

    // ── numDisparities / blockSize ─────────────────────────────────────────
    _elNdSel = _mkSelect([16,32,48,64,80,96,112,128,160,192], _nd, v => {
      _nd = v; _sendParams();
    });
    _elBsSel = _mkSelect([5,7,9,11,13,15,17,19,21], _bs, v => {
      _bs = v;
      if (_elP1Auto?.checked && _elP1Input) _elP1Input.value = String(8  * 3 * _bs * _bs);
      if (_elP2Auto?.checked && _elP2Input) _elP2Input.value = String(32 * 3 * _bs * _bs);
      _sendParams();
    });
    const ndRow = _mkLabelRow('numDisparities', _elNdSel);
    const bsRow = _mkLabelRow('blockSize',      _elBsSel);

    // ── P1 ────────────────────────────────────────────────────────────────
    _elP1Row = document.createElement('div');
    Object.assign(_elP1Row.style, { display: 'flex', alignItems: 'center', gap: '4px' });
    const p1Lbl = document.createElement('span');
    p1Lbl.textContent = 'P1';
    Object.assign(p1Lbl.style, { fontSize: '11px', color: '#777', flex: '1' });
    _elP1Auto = document.createElement('input');
    _elP1Auto.type = 'checkbox'; _elP1Auto.checked = (_p1 < 0); _elP1Auto.title = 'auto';
    const autoLbl1 = document.createElement('span');
    autoLbl1.textContent = 'auto';
    Object.assign(autoLbl1.style, { fontSize: '10px', color: '#555' });
    _elP1Input = document.createElement('input');
    _elP1Input.type = 'number'; _elP1Input.min = '1'; _elP1Input.step = '1';
    _elP1Input.value = String(_p1 > 0 ? _p1 : 8 * 3 * _bs * _bs);
    _elP1Input.disabled = _elP1Auto.checked;
    Object.assign(_elP1Input.style, {
      width: '60px', background: '#2a2a2a', color: '#d4d4d4',
      border: '1px solid #444', borderRadius: '3px', padding: '2px 4px', fontSize: '11px',
      opacity: _elP1Auto.checked ? '0.4' : '1',
    });
    _elP1Auto.addEventListener('change', () => {
      _p1 = _elP1Auto.checked ? -1 : (Number(_elP1Input.value) || 1);
      _elP1Input.disabled = _elP1Auto.checked;
      _elP1Input.style.opacity = _elP1Auto.checked ? '0.4' : '1';
      _sendParams();
    });
    _elP1Input.addEventListener('change', () => {
      if (!_elP1Auto.checked) { _p1 = Number(_elP1Input.value) || 1; _sendParams(); }
    });
    _elP1Row.append(p1Lbl, _elP1Auto, autoLbl1, _elP1Input);

    // ── P2 ────────────────────────────────────────────────────────────────
    _elP2Row = document.createElement('div');
    Object.assign(_elP2Row.style, { display: 'flex', alignItems: 'center', gap: '4px' });
    const p2Lbl = document.createElement('span');
    p2Lbl.textContent = 'P2';
    Object.assign(p2Lbl.style, { fontSize: '11px', color: '#777', flex: '1' });
    _elP2Auto = document.createElement('input');
    _elP2Auto.type = 'checkbox'; _elP2Auto.checked = (_p2 < 0); _elP2Auto.title = 'auto';
    const autoLbl2 = document.createElement('span');
    autoLbl2.textContent = 'auto';
    Object.assign(autoLbl2.style, { fontSize: '10px', color: '#555' });
    _elP2Input = document.createElement('input');
    _elP2Input.type = 'number'; _elP2Input.min = '1'; _elP2Input.step = '1';
    _elP2Input.value = String(_p2 > 0 ? _p2 : 32 * 3 * _bs * _bs);
    _elP2Input.disabled = _elP2Auto.checked;
    Object.assign(_elP2Input.style, {
      width: '60px', background: '#2a2a2a', color: '#d4d4d4',
      border: '1px solid #444', borderRadius: '3px', padding: '2px 4px', fontSize: '11px',
      opacity: _elP2Auto.checked ? '0.4' : '1',
    });
    _elP2Auto.addEventListener('change', () => {
      _p2 = _elP2Auto.checked ? -1 : (Number(_elP2Input.value) || 1);
      _elP2Input.disabled = _elP2Auto.checked;
      _elP2Input.style.opacity = _elP2Auto.checked ? '0.4' : '1';
      _sendParams();
    });
    _elP2Input.addEventListener('change', () => {
      if (!_elP2Auto.checked) { _p2 = Number(_elP2Input.value) || 1; _sendParams(); }
    });
    _elP2Row.append(p2Lbl, _elP2Auto, autoLbl2, _elP2Input);

    // ── Sliders ───────────────────────────────────────────────────────────
    const urSl  = _mkSliderRow('uniquenessRatio', 0,   25,  1,   _ur,  '',   v => { _ur  = v; _sendParams(); });
    const swsSl = _mkSliderRow('speckleWindow',   0,   200, 10,  _sws, '',   v => { _sws = v; _sendParams(); });
    const srSl  = _mkSliderRow('speckleRange',    1,   32,  1,   _sr,  '',   v => { _sr  = v; _sendParams(); });
    _elUrSlider  = urSl.slider;  _elUrVal  = urSl.valEl;
    _elSwsSlider = swsSl.slider; _elSwsVal = swsSl.valEl;
    _elSrSlider  = srSl.slider;  _elSrVal  = srSl.valEl;

    // ── Clip sliders ──────────────────────────────────────────────────────
    const clipMinSl = _mkSliderRow('Clip min', 0,   2000,  50,  _clipMin, 'mm', v => { _clipMin = v; _sendParams(); });
    const clipMaxSl = _mkSliderRow('Clip max', 500, 20000, 500, _clipMax, 'mm', v => { _clipMax = v; _sendParams(); });
    _elClipMinSlider = clipMinSl.slider; _elClipMinVal = clipMinSl.valEl;
    _elClipMaxSlider = clipMaxSl.slider; _elClipMaxVal = clipMaxSl.valEl;

    // ── Buttons ───────────────────────────────────────────────────────────
    _elBtnSave    = _mkBtn('Save',     '#1a3a1a', '#7dcf7d', '#2a6a2a', _onSave);
    _elBtnSavePly = _mkBtn('Save PLY', '#1a2a3a', '#5a90c0', '#2a5a8c', _onSavePly);
    _elBtnCancel  = _mkBtn('Close',    '#2a2a2a', '#888',    '#444',    _onCancel);
    _setBtnEnabled(_elBtnSave,    false);
    _setBtnEnabled(_elBtnSavePly, false);

    panel.append(
      _elStatus, statsBox, mkDiv(),
      viewRow, algoRow, mkDiv(),
      ndRow, bsRow, _elP1Row, _elP2Row,
      urSl.wrap, swsSl.wrap, srSl.wrap, mkDiv(),
      clipMinSl.wrap, clipMaxSl.wrap,
      _elBtnSave, _elBtnSavePly, _elBtnCancel,
    );

    body.append(canvasWrap, panel);
    _modal.appendChild(body);
    document.body.appendChild(_modal);

    _updateAlgoUI();
  }

  // ── View / Algo sync (sidebar ↔ modal) ───────────────────────────────────
  function _applyView(view, sendParam) {
    _viewMode = view;
    // Sidebar
    document.querySelectorAll('.plugin-ui-block[data-plugin="StereoMatch & Depth"]').forEach(block => {
      _updateViewBtns(block, view);
    });
    // Modal
    if (_elMViewDisp)  _elMViewDisp._setActive(view === 'disparity');
    if (_elMViewDepth) _elMViewDepth._setActive(view === 'depth');
    if (sendParam && _camId)
      socket.emit('set_param', { cam_id: _camId, key: 'smd_view_mode', value: view });
  }

  function _applyAlgo(algo, sendParam) {
    _algo = algo;
    // Sidebar
    document.querySelectorAll('.plugin-ui-block[data-plugin="StereoMatch & Depth"]').forEach(block => {
      _updateAlgoBtns(block, algo);
    });
    // Modal
    if (_elMAlgoBM)   _elMAlgoBM._setActive(algo === 'bm');
    if (_elMAlgoSGBM) _elMAlgoSGBM._setActive(algo === 'sgbm');
    _updateAlgoUI();
    if (sendParam && _camId)
      socket.emit('set_param', { cam_id: _camId, key: 'smd_algorithm', value: algo });
  }

  function _updateAlgoUI() {
    const sgbm = _algo === 'sgbm';
    if (_elP1Row) _elP1Row.style.display = sgbm ? 'flex' : 'none';
    if (_elP2Row) _elP2Row.style.display = sgbm ? 'flex' : 'none';
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
    const ep = _viewMode === 'depth' ? 'depth' : 'disparity';
    img.src = `/plugin/stereomatchdepth/${ep}?cam_id=${encodeURIComponent(_camId)}&_t=${Date.now()}`;
  }

  // ── Actions ───────────────────────────────────────────────────────────────
  function _sendParams() {
    if (!_camId) return;
    socket.emit('smd_set_params', {
      cam_id:                _camId,
      smd_num_disparities:   _nd,
      smd_block_size:        _bs,
      smd_p1:                _p1,
      smd_p2:                _p2,
      smd_uniqueness_ratio:  _ur,
      smd_speckle_window:    _sws,
      smd_speckle_range:     _sr,
      smd_clip_min:          _clipMin,
      smd_clip_max:          _clipMax,
    });
  }

  function _onSave() {
    if (!_camId) return;
    socket.emit('smd_save', { cam_id: _camId });
  }

  function _onSavePly() {
    if (!_camId) return;
    _setBtnEnabled(_elBtnSavePly, false);
    if (_elStatus) { _elStatus.textContent = '生成 PLY…'; _elStatus.style.color = '#aaa'; }
    socket.emit('smd_save_ply', { cam_id: _camId });
  }

  function _onCancel() {
    if (!_camId) return;
    socket.emit('smd_cancel', { cam_id: _camId });
    _closeModal();
  }

  function _closeModal() {
    _stopPoll();
    _running = false;
    if (_modal) _modal.style.display = 'none';
  }

  // ── Open modal ────────────────────────────────────────────────────────────
  window.smdOpenModal = function (btnEl) {
    const block = btnEl.closest('.plugin-ui-block');
    const camId = block ? block.dataset.cam : '';
    if (!camId) return;

    _buildModal();
    _camId    = camId;
    _camLeft  = block.querySelector('.smd-cam-left')?.value  || '';
    _camRight = block.querySelector('.smd-cam-right')?.value || '';

    _running = false;
    if (_elStatus)  { _elStatus.textContent = '等待中…'; _elStatus.style.color = '#888'; }
    if (_elDAvg)    _elDAvg.textContent = '  平均 — px';
    if (_elDVld)    _elDVld.textContent  = '  有效 —%';
    if (_elZVld)    _elZVld.textContent  = '  有效 —%';
    if (_elZMin)    _elZMin.textContent  = '  最近 — mm';
    if (_elZMed)    _elZMed.textContent  = '  中位 — mm';
    if (_elZMax)    _elZMax.textContent  = '  最遠 — mm';
    _setBtnEnabled(_elBtnSave,    false);
    _setBtnEnabled(_elBtnSavePly, false);

    _modal.style.display = 'flex';
    _startPoll();

    socket.emit('smd_open', {
      cam_id:    camId,
      cam_left:  _camLeft,
      cam_right: _camRight,
    });
  };

  // ── Sidebar: Save PLY ─────────────────────────────────────────────────────
  window.smdSavePly = function (btnEl) {
    const block = _blk(btnEl);
    if (!block) return;
    const c = block.dataset.cam;
    if (c) socket.emit('smd_save_ply', { cam_id: c });
  };

  // ── Socket handlers ───────────────────────────────────────────────────────
  socket.on('smd_event', (data) => {
    if (data.type === 'started') {
      _running = true;

      // Sync numeric params to modal DOM
      if (data.num_disparities !== undefined) {
        _nd = data.num_disparities;
        if (_elNdSel) _elNdSel.value = String(_nd);
      }
      if (data.block_size !== undefined) {
        _bs = data.block_size;
        if (_elBsSel) _elBsSel.value = String(_bs);
      }
      if (data.p1 !== undefined) {
        _p1 = data.p1;
        if (_elP1Auto)  _elP1Auto.checked      = (_p1 < 0);
        if (_elP1Input) {
          _elP1Input.value    = String(_p1 > 0 ? _p1 : 8  * 3 * _bs * _bs);
          _elP1Input.disabled = (_p1 < 0);
          _elP1Input.style.opacity = _p1 < 0 ? '0.4' : '1';
        }
      }
      if (data.p2 !== undefined) {
        _p2 = data.p2;
        if (_elP2Auto)  _elP2Auto.checked      = (_p2 < 0);
        if (_elP2Input) {
          _elP2Input.value    = String(_p2 > 0 ? _p2 : 32 * 3 * _bs * _bs);
          _elP2Input.disabled = (_p2 < 0);
          _elP2Input.style.opacity = _p2 < 0 ? '0.4' : '1';
        }
      }
      if (data.uniqueness_ratio !== undefined) {
        _ur = data.uniqueness_ratio;
        if (_elUrSlider) _elUrSlider.value    = String(_ur);
        if (_elUrVal)    _elUrVal.textContent = String(_ur);
      }
      if (data.speckle_window !== undefined) {
        _sws = data.speckle_window;
        if (_elSwsSlider) _elSwsSlider.value    = String(_sws);
        if (_elSwsVal)    _elSwsVal.textContent = String(_sws);
      }
      if (data.speckle_range !== undefined) {
        _sr = data.speckle_range;
        if (_elSrSlider) _elSrSlider.value    = String(_sr);
        if (_elSrVal)    _elSrVal.textContent = String(_sr);
      }
      if (data.clip_min !== undefined) {
        _clipMin = data.clip_min;
        if (_elClipMinSlider) _elClipMinSlider.value    = String(_clipMin);
        if (_elClipMinVal)    _elClipMinVal.textContent = _clipMin + ' mm';
      }
      if (data.clip_max !== undefined) {
        _clipMax = data.clip_max;
        if (_elClipMaxSlider) _elClipMaxSlider.value    = String(_clipMax);
        if (_elClipMaxVal)    _elClipMaxVal.textContent = _clipMax + ' mm';
      }

      if (_elStatus) {
        _elStatus.textContent = `執行中 — ${_algo.toUpperCase()} / ${_viewMode === 'depth' ? 'Depth' : 'Disp'}`;
        _elStatus.style.color = '#7dcf7d';
      }
      _setBtnEnabled(_elBtnSave,    true);
      _setBtnEnabled(_elBtnSavePly, true);

    } else if (data.type === 'save_result') {
      if (data.ok) {
        _setBtnEnabled(_elBtnSave, false);
        if (_elStatus) {
          _elStatus.textContent = `已儲存 ${data.saved_at || ''}`;
          _elStatus.style.color = '#7dcf7d';
        }
      } else {
        if (_elStatus) {
          _elStatus.textContent = `儲存失敗: ${data.error}`;
          _elStatus.style.color = '#c87070';
        }
      }

    } else if (data.type === 'ply_result') {
      if (data.ok) {
        if (_elStatus) {
          _elStatus.textContent = `PLY 已儲存 (${data.n_points} pts)`;
          _elStatus.style.color = '#7dcf7d';
        }
        const a = document.createElement('a');
        a.href = `/plugin/stereomatchdepth/download_ply?cam_id=${encodeURIComponent(_camId)}&_t=${Date.now()}`;
        a.download = data.filename || 'pointcloud.ply';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        _setBtnEnabled(_elBtnSavePly, true);
      } else {
        if (_elStatus) {
          _elStatus.textContent = `PLY 失敗: ${data.error}`;
          _elStatus.style.color = '#c87070';
        }
        _setBtnEnabled(_elBtnSavePly, true);
      }

    } else if (data.type === 'error') {
      _running = false;
      if (_elStatus) {
        _elStatus.textContent = data.msg || 'Error';
        _elStatus.style.color = '#c87070';
      }
      _setBtnEnabled(_elBtnSave,    false);
      _setBtnEnabled(_elBtnSavePly, false);
    }
  });

  socket.on('smd_stats', (data) => {
    if (data.cam_id !== _camId) return;
    if (_elDAvg) { _elDAvg.textContent = `  平均 ${data.avg_disp} px`; _elDAvg.style.color = '#aaa'; }
    if (_elDVld) {
      _elDVld.textContent = `  有效 ${data.valid_pct_d}%`;
      _elDVld.style.color = data.valid_pct_d > 50 ? '#7dcf7d' : '#c87070';
    }
    if (_elZVld) {
      _elZVld.textContent = `  有效 ${data.valid_pct_z}%`;
      _elZVld.style.color = data.valid_pct_z > 50 ? '#7dcf7d' : '#c87070';
    }
    if (_elZMin) _elZMin.textContent = `  最近 ${data.min_depth} mm`;
    if (_elZMed) _elZMed.textContent = `  中位 ${data.med_depth} mm`;
    if (_elZMax) _elZMax.textContent = `  最遠 ${data.max_depth} mm`;
  });

  // ── Sidebar param handlers ────────────────────────────────────────────────
  window.smdOnCamLeft = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'smd_cam_left', value: el.value });
    _updateInfoRow(_blk(el));
  };

  window.smdOnCamRight = function (el) {
    const c = _blkCam(el);
    if (c) socket.emit('set_param', { cam_id: c, key: 'smd_cam_right', value: el.value });
    _updateInfoRow(_blk(el));
  };

  window.smdSetSide = function (btnEl, side) {
    const block = _blk(btnEl);
    if (!block) return;
    const c = block.dataset.cam;
    if (c) socket.emit('set_param', { cam_id: c, key: 'smd_display_side', value: side });
    _updateSideBtns(block, side);
  };

  window.smdSetView = function (btnEl, view) {
    const block = _blk(btnEl);
    const c = block ? block.dataset.cam : _camId;
    _applyView(view, false);
    if (c) socket.emit('set_param', { cam_id: c, key: 'smd_view_mode', value: view });
  };

  window.smdSetAlgo = function (btnEl, algo) {
    const block = _blk(btnEl);
    const c = block ? block.dataset.cam : _camId;
    _applyAlgo(algo, false);
    if (c) socket.emit('set_param', { cam_id: c, key: 'smd_algorithm', value: algo });
  };

  window.smdToggleEnable = function (btnEl) {
    const block = _blk(btnEl);
    if (!block) return;
    const c = block.dataset.cam;
    if (!c) return;
    const next = !(btnEl.dataset.enabled === 'true');
    socket.emit('set_param', { cam_id: c, key: 'smd_enabled', value: next });
    _setEnableBtn(btnEl, next);
  };

  // ── Sidebar button style helpers ──────────────────────────────────────────
  function _updateSideBtns(block, side) {
    ['L', 'R'].forEach(s => {
      const b = block.querySelector(`.smd-side-${s}`);
      if (!b) return;
      const on = s === side;
      b.style.background = on ? '#2a5a8c' : '#2a2a2a';
      b.style.color       = on ? '#90ccf0' : '#888';
      b.style.border      = on ? '1px solid #3a7abc' : '1px solid #444';
    });
  }

  function _updateViewBtns(block, view) {
    [['disparity', 'Disp'], ['depth', 'Depth']].forEach(([v]) => {
      const b = block.querySelector(`.smd-view-${v}`);
      if (!b) return;
      const on = v === view;
      b.style.background = on ? '#2a5a8c' : '#2a2a2a';
      b.style.color       = on ? '#90ccf0' : '#888';
      b.style.border      = on ? '1px solid #3a7abc' : '1px solid #444';
    });
  }

  function _updateAlgoBtns(block, algo) {
    [['bm', 'BM'], ['sgbm', 'SGBM']].forEach(([v]) => {
      const b = block.querySelector(`.smd-algo-${v}`);
      if (!b) return;
      const on = v === algo;
      b.style.background = on ? '#2a5a8c' : '#2a2a2a';
      b.style.color       = on ? '#90ccf0' : '#888';
      b.style.border      = on ? '1px solid #3a7abc' : '1px solid #444';
    });
  }

  function _setEnableBtn(btn, enabled) {
    btn.dataset.enabled  = String(enabled);
    btn.textContent      = enabled ? 'Disable' : 'Enable';
    btn.style.background = enabled ? '#3a1a1a' : '#2a2a2a';
    btn.style.color      = enabled ? '#c87070' : '#888';
    btn.style.border     = enabled ? '1px solid #7a3030' : '1px solid #444';
  }

  function _updateInfoRow(block) {
    if (!block) return;
    const infoEl = block.querySelector('.smd-info-row');
    if (!infoEl || infoEl.dataset.hasData === 'true') return;
    const L = block.querySelector('.smd-cam-left')?.value  || '';
    const R = block.querySelector('.smd-cam-right')?.value || '';
    infoEl.textContent = (L && R) ? `${L} ↔ ${R}` : '未設定相機';
    infoEl.style.color = '#666';
  }

  // ── State sync ────────────────────────────────────────────────────────────
  function _applySmdState(s) {
    const cameras = s.cameras || {};
    const camIds  = Object.keys(cameras);

    document.querySelectorAll('.plugin-ui-block[data-plugin="StereoMatch & Depth"]').forEach(block => {
      const cid = block.dataset.cam;
      const cs  = cameras[cid] || null;
      if (!cs) return;

      // Camera selects
      ['smd-cam-left', 'smd-cam-right'].forEach(cls => {
        const sel = block.querySelector(`.${cls}`);
        if (!sel) return;
        const cur = sel.value;
        sel.innerHTML = '<option value="">— select —</option>';
        camIds.forEach(id => {
          const opt = document.createElement('option');
          opt.value = id; opt.textContent = id;
          sel.appendChild(opt);
        });
        sel.value = (cls === 'smd-cam-left' ? cs.smd_cam_left : cs.smd_cam_right) || cur || '';
      });

      _updateSideBtns(block, cs.smd_display_side || 'L');
      _updateViewBtns(block, cs.smd_view_mode    || 'disparity');
      _updateAlgoBtns(block, cs.smd_algorithm    || 'sgbm');

      // Keep module vars in sync with server state (for polling URL etc.)
      if (cs.smd_view_mode) _viewMode = cs.smd_view_mode;
      if (cs.smd_algorithm) _algo     = cs.smd_algorithm;

      const enableBtn = block.querySelector('.smd-btn-enable');
      if (enableBtn) {
        _setEnableBtn(enableBtn, !!cs.smd_enabled);
        const canEnable = !!cs.smd_has_data;
        enableBtn.disabled      = !canEnable;
        enableBtn.style.opacity = canEnable ? '1' : '0.4';
        enableBtn.style.cursor  = canEnable ? 'pointer' : 'not-allowed';
      }

      const plyBtn = block.querySelector('.smd-btn-save-ply');
      if (plyBtn) plyBtn.style.display = cs.smd_enabled ? '' : 'none';

      const infoRow = block.querySelector('.smd-info-row');
      if (infoRow) {
        const L = block.querySelector('.smd-cam-left')?.value  || cs.smd_cam_left  || '';
        const R = block.querySelector('.smd-cam-right')?.value || cs.smd_cam_right || '';
        const pair = (L && R) ? `${L} ↔ ${R}` : '';
        if (cs.smd_has_data) {
          const date = cs.smd_saved_at ? cs.smd_saved_at.slice(0, 10) : '';
          infoRow.innerHTML = [
            pair ? `<span style="color:#888;">${pair}</span>&nbsp;|&nbsp;` : '',
            `<span style="color:#7dcf7d;font-weight:600;">已儲存</span>`,
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

  socket.on('state', _applySmdState);
  window.addEventListener('plugin-state-update', e => _applySmdState(e.detail));

}());
