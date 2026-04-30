// depthcolorize/ui.js — DepthColorize plugin frontend (1.3.0)
// Multi-camera safe: all DOM queries scoped to .plugin-ui-block.

function _dcBlock(el) { return el.closest('.plugin-ui-block'); }
function _dcCamId(el) { const b = _dcBlock(el); return b ? b.dataset.cam : ''; }

function _dcSyncManualRange(block, auto) {
  const r = block.querySelector('.dc-manual-range');
  if (r) r.style.display = auto ? 'none' : 'flex';
}

function _dcSyncColorSection(block, on) {
  const s = block.querySelector('.dc-color-section');
  if (s) s.style.display = on ? 'flex' : 'none';
}

// ── Depth display ────────────────────────────────────────────────────────

function dcOnEnable(el) {
  socket.emit('set_param', { cam_id: _dcCamId(el), key: 'dc_enabled', value: el.checked });
}
function dcOnAutoRange(el) {
  socket.emit('set_param', { cam_id: _dcCamId(el), key: 'dc_auto_range', value: el.checked });
  _dcSyncManualRange(_dcBlock(el), el.checked);
}
function dcOnColormap(el) {
  socket.emit('set_param', { cam_id: _dcCamId(el), key: 'dc_colormap', value: parseInt(el.value, 10) });
}
function dcOnMinInput(el) {
  const l = _dcBlock(el).querySelector('.dc-min-label'); if (l) l.textContent = el.value;
}
function dcOnMinChange(el) {
  const l = _dcBlock(el).querySelector('.dc-min-label'); if (l) l.textContent = el.value;
  socket.emit('set_param', { cam_id: _dcCamId(el), key: 'dc_clip_min', value: parseFloat(el.value) });
}
function dcOnMaxInput(el) {
  const l = _dcBlock(el).querySelector('.dc-max-label'); if (l) l.textContent = el.value;
}
function dcOnMaxChange(el) {
  const l = _dcBlock(el).querySelector('.dc-max-label'); if (l) l.textContent = el.value;
  socket.emit('set_param', { cam_id: _dcCamId(el), key: 'dc_clip_max', value: parseFloat(el.value) });
}
function dcOnScaleChange(el) {
  const v = parseFloat(el.value);
  if (v > 0) socket.emit('set_param', { cam_id: _dcCamId(el), key: 'dc_depth_scale', value: v });
}
function dcOnIntrinsic(el, key) {
  const v = parseFloat(el.value);
  if (!isNaN(v)) socket.emit('set_param', { cam_id: _dcCamId(el), key, value: v });
}

// ── Vertex colour ────────────────────────────────────────────────────────

function dcOnVertexColor(el) {
  socket.emit('set_param', { cam_id: _dcCamId(el), key: 'dc_vertex_color', value: el.checked });
  _dcSyncColorSection(_dcBlock(el), el.checked);
}
function dcOnColorCam(el) {
  socket.emit('set_param', { cam_id: _dcCamId(el), key: 'dc_color_cam', value: el.value });
}

// ── Params save ──────────────────────────────────────────────────────────

function dcSaveParams(el) {
  socket.emit('dc_save_params', { cam_id: _dcCamId(el) });
}

socket.on('dc_params_event', function (data) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="DepthColorize"]').forEach(function (block) {
    if (block.dataset.cam !== data.cam_id) return;
    const status = block.querySelector('.dc-status');
    if (!data.ok) {
      if (status) status.textContent = 'Error: ' + (data.error || 'unknown');
      return;
    }
    const _setNum = function (sel, v) {
      const el = block.querySelector(sel);
      if (el && v !== undefined) el.value = parseFloat(v.toFixed(4));
    };
    _setNum('.dc-fx-input',  data.depth_fx);
    _setNum('.dc-fy-input',  data.depth_fy);
    _setNum('.dc-cx-input',  data.depth_cx);
    _setNum('.dc-cy-input',  data.depth_cy);
    _setNum('.dc-cfx-input', data.color_fx);
    _setNum('.dc-cfy-input', data.color_fy);
    _setNum('.dc-ccx-input', data.color_cx);
    _setNum('.dc-ccy-input', data.color_cy);
    _setNum('.dc-tx-input',  data.ext_tx);
    _setNum('.dc-ty-input',  data.ext_ty);
    _setNum('.dc-tz-input',  data.ext_tz);
    if (status) {
      if (data.source === 'saved')    status.textContent = 'Params saved: ' + (data.filename || '');
      else if (data.source === 'stereo_cal') status.textContent = 'Params loaded (stereo_cal)';
      else if (data.source === 'lens_cal')  status.textContent = 'Intrinsics loaded (lens_cal)';
      else if (data.source)           status.textContent = 'Params loaded (' + data.source + ')';
    }
  });
});

// ── PLY export ───────────────────────────────────────────────────────────

function dcSavePly(el) {
  const block  = _dcBlock(el);
  const camId  = _dcCamId(el);
  const btn    = block.querySelector('.dc-btn-ply');
  const status = block.querySelector('.dc-status');
  if (btn)    { btn.disabled = true; btn.textContent = 'Saving…'; }
  if (status) status.textContent = 'Generating point cloud…';
  socket.emit('dc_save_ply', { cam_id: camId });
}

socket.on('dc_event', function (data) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="DepthColorize"]').forEach(function (block) {
    const btn    = block.querySelector('.dc-btn-ply');
    const status = block.querySelector('.dc-status');
    if (btn) { btn.disabled = false; btn.textContent = 'Save PLY'; }

    if (!data.ok) {
      if (status) status.textContent = 'Error: ' + (data.error || 'unknown');
      return;
    }

    const colorTag = data.has_color ? ' [RGB]' : '';
    if (status) status.textContent = data.filename + colorTag +
                                     '  (' + data.n_points.toLocaleString() + ' pts)';

    const camId = block.dataset.cam;
    const a = document.createElement('a');
    a.href = '/plugin/depthcolorize/download_ply?cam_id=' + encodeURIComponent(camId) +
             '&_t=' + Date.now();
    a.download = data.filename || 'pointcloud.ply';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  });
});

// ── State sync ───────────────────────────────────────────────────────────

function _dcUpdateColorCamList(block, allCamIds, currentVal, autoVal) {
  const sel = block.querySelector('.dc-color-cam');
  if (!sel) return;
  const camId = block.dataset.cam;
  // Rebuild options
  while (sel.options.length > 1) sel.remove(1);   // keep "Auto" at index 0
  allCamIds.forEach(function (id) {
    if (id === camId) return;                       // skip self (depth cam)
    const opt = document.createElement('option');
    opt.value       = id;
    opt.textContent = id + (id === autoVal ? ' (auto)' : '');
    sel.appendChild(opt);
  });
  sel.value = currentVal || '';
}

socket.on('state', function (data) {
  const allCamIds = data.cameras ? Object.keys(data.cameras) : [];

  document.querySelectorAll('.plugin-ui-block[data-plugin="DepthColorize"]').forEach(function (block) {
    const camId    = block.dataset.cam;
    const st       = (data.cameras && camId) ? data.cameras[camId] : null;
    if (!st) return;

    const enCb   = block.querySelector('.dc-enable');
    const autoCb = block.querySelector('.dc-auto-range');
    const cmSel  = block.querySelector('.dc-colormap');
    const minSl  = block.querySelector('.dc-min-slider');
    const minLbl = block.querySelector('.dc-min-label');
    const maxSl  = block.querySelector('.dc-max-slider');
    const maxLbl = block.querySelector('.dc-max-label');
    const vcCb   = block.querySelector('.dc-vertex-color');

    if (enCb   && st.dc_enabled    !== undefined) enCb.checked   = !!st.dc_enabled;
    if (autoCb && st.dc_auto_range !== undefined) autoCb.checked = !!st.dc_auto_range;
    if (cmSel  && st.dc_colormap   !== undefined) cmSel.value    = String(st.dc_colormap);
    if (minSl  && st.dc_clip_min   !== undefined) { minSl.value = st.dc_clip_min;  if (minLbl) minLbl.textContent = st.dc_clip_min; }
    if (maxSl  && st.dc_clip_max   !== undefined) { maxSl.value = st.dc_clip_max;  if (maxLbl) maxLbl.textContent = st.dc_clip_max; }
    if (vcCb   && st.dc_vertex_color !== undefined) vcCb.checked = !!st.dc_vertex_color;

    const _setNum = (sel, v) => { const el = block.querySelector(sel); if (el && v !== undefined) el.value = v; };
    _setNum('.dc-scale-input', st.dc_depth_scale);
    _setNum('.dc-fx-input',    st.dc_fx);
    _setNum('.dc-fy-input',    st.dc_fy);
    _setNum('.dc-cx-input',    st.dc_cx);
    _setNum('.dc-cy-input',    st.dc_cy);
    _setNum('.dc-cfx-input',   st.dc_color_fx);
    _setNum('.dc-cfy-input',   st.dc_color_fy);
    _setNum('.dc-ccx-input',   st.dc_color_cx);
    _setNum('.dc-ccy-input',   st.dc_color_cy);
    _setNum('.dc-tx-input',    st.dc_ext_tx);
    _setNum('.dc-ty-input',    st.dc_ext_ty);
    _setNum('.dc-tz-input',    st.dc_ext_tz);

    _dcUpdateColorCamList(block, allCamIds, st.dc_color_cam, st.dc_color_cam_auto);
    _dcSyncManualRange(block,   autoCb ? autoCb.checked : true);
    _dcSyncColorSection(block,  vcCb   ? vcCb.checked   : false);
  });
});
