// cloudrecord/ui.js — RTAB-Map_record plugin frontend (1.5.0)
// All DOM queries scoped to .plugin-ui-block to support multi-camera.

function _crBlock(el) { return el.closest('.plugin-ui-block'); }
function _crCamId(el) { var b = _crBlock(el); return b ? b.dataset.cam : ''; }

// ── Param helpers ────────────────────────────────────────────────────────────

function crParam(el, key, conv) {
  socket.emit('set_param', { cam_id: _crCamId(el), key: key, value: conv ? conv(el) : el.value });
}

// ── Scan controls ─────────────────────────────────────────────────────────────

function _crValidate(block) {
  var ccSel = block.querySelector('.cr-color-cam');
  if (!ccSel || !ccSel.value) {
    var msg = block.querySelector('.cr-msg');
    if (msg) { msg.textContent = 'Select colour camera first'; setTimeout(function(){ msg.textContent = ''; }, 3000); }
    return false;
  }
  return true;
}

function crToggleScan(el) {
  var block    = _crBlock(el);
  var btn      = block.querySelector('.cr-btn-toggle');
  var scanning = btn && btn.dataset.scanning === '1';
  if (scanning) {
    socket.emit('cr_stop', { cam_id: _crCamId(el) });
  } else {
    if (!_crValidate(block)) return;
    socket.emit('cr_start', { cam_id: _crCamId(el) });
  }
}

function crSaveParams(el) {
  socket.emit('cr_save_params', { cam_id: _crCamId(el) });
}

function crSetExtR(el, i, j) {
  socket.emit('set_param', { cam_id: _crCamId(el), key: 'cr_ext_r' + i + '' + j, value: parseFloat(el.value) });
}

function _crSetExtRMatrix(block, flatR) {
  if (!flatR || flatR.length !== 9) return;
  flatR.forEach(function(v, idx) {
    var i = Math.floor(idx / 3), j = idx % 3;
    var el = block.querySelector('.cr-ext-r' + i + '' + j);
    if (el) el.value = parseFloat(v.toFixed(6));
  });
}

function crSetColorCamSource(el, src) {
  var block = _crBlock(el);
  if (!block) return;
  socket.emit('set_param', { cam_id: _crCamId(el), key: 'cr_color_cam_source', value: src });
  _crUpdateColorSrcBtns(block, src);
}

function _crUpdateColorSrcBtns(block, src) {
  ['pipeline', 'display'].forEach(function(s) {
    var b = block.querySelector('.cr-color-src-' + s);
    if (!b) return;
    var on = (s === src);
    b.style.background = on ? '#2a5a8c' : '#2a2a2a';
    b.style.color      = on ? '#90ccf0' : '#888';
    b.style.border     = on ? '1px solid #3a7abc' : '1px solid #444';
  });
}

function _crSetToggleBtn(btn, scanning) {
  if (!btn) return;
  btn.dataset.scanning = scanning ? '1' : '0';
  if (scanning) {
    btn.textContent      = 'Stop';
    btn.style.background = '#2a1a1a';
    btn.style.color      = '#e07070';
    btn.style.border     = '1px solid #8a2a2a';
  } else {
    btn.textContent      = 'Start';
    btn.style.background = '#1a2a1a';
    btn.style.color      = '#5a9a5a';
    btn.style.border     = '1px solid #2a6a2a';
  }
}

// ── Depth range mode + sliders ────────────────────────────────────────────────

function crOnDepthRangeMode(el) {
  var mode = el.value;
  socket.emit('set_param', { cam_id: _crCamId(el), key: 'cr_depth_range_mode', value: mode });
  _crSyncDepthRange(_crBlock(el), mode);
}

function _crSyncDepthRange(block, mode) {
  var r = block.querySelector('.cr-manual-range');
  if (r) r.style.display = (mode === 'manual') ? 'flex' : 'none';
}

function crOnMinInput(el) {
  var l = _crBlock(el).querySelector('.cr-min-label');
  if (l) l.textContent = el.value;
}
function crOnMinChange(el) {
  var l = _crBlock(el).querySelector('.cr-min-label');
  if (l) l.textContent = el.value;
  socket.emit('set_param', { cam_id: _crCamId(el), key: 'cr_depth_min', value: parseFloat(el.value) });
}
function crOnMaxInput(el) {
  var l = _crBlock(el).querySelector('.cr-max-label');
  if (l) l.textContent = el.value;
}
function crOnMaxChange(el) {
  var l = _crBlock(el).querySelector('.cr-max-label');
  if (l) l.textContent = el.value;
  socket.emit('set_param', { cam_id: _crCamId(el), key: 'cr_depth_max', value: parseFloat(el.value) });
}

// ── Elevation prompt ──────────────────────────────────────────────────────────

function crConfirmElevation(el) {
  socket.emit('cr_confirm_elevation', { cam_id: _crCamId(el) });
  var prompt = _crBlock(el).querySelector('.cr-elevation-prompt');
  if (prompt) prompt.style.display = 'none';
}

// ── Error prompt ──────────────────────────────────────────────────────────────

function crErrorAction(el, action) {
  socket.emit('cr_error_action', { cam_id: _crCamId(el), action: action });
  var prompt = _crBlock(el).querySelector('.cr-error-prompt');
  if (prompt) prompt.style.display = 'none';
}

// ── Servo connect (kept for future use) ───────────────────────────────────────

function crServoConnect(el) {
  var block = _crBlock(el);
  var ip    = (block.querySelector('.cr-servo-ip')   || {}).value || '';
  var port  = parseInt((block.querySelector('.cr-servo-port') || {}).value || '23');
  socket.emit('cr_servo_connect', { cam_id: _crCamId(el), ip: ip, port: port });
}

function crServoDisconnect(el) {
  socket.emit('cr_servo_disconnect', { cam_id: _crCamId(el) });
}

// ── Status update (shared by cr_status + frame event) ────────────────────────

function _crApplyStatus(block, data) {
  var state      = data.state        || 'idle';
  var stepTotal  = data.step_total   || 0;
  var stepDone   = data.step_done    || 0;
  var ring       = data.ring         || 0;
  var ringsTotal = data.rings_total  || 0;
  var frameIdx   = data.frame_idx    || 0;
  var sessName   = data.session_name || '';
  var errMsg     = data.error_msg    || '';

  var stateMap = {
    idle: 'Idle', recording: 'Recording', saving: 'Saving',
    servo_running: 'Servo', waiting_elevation: 'Elevation Change',
    error: 'Error', done: 'Done'
  };
  var stateColors = {
    idle: '#888', recording: '#5aba5a', saving: '#5ae0e0',
    servo_running: '#e3a05a', waiting_elevation: '#c8c85a',
    error: '#c85a5a', done: '#5a9a5a'
  };

  var stateLabel = block.querySelector('.cr-state-label');
  if (stateLabel) {
    stateLabel.textContent = stateMap[state] || state;
    stateLabel.style.color = stateColors[state] || '#888';
  }

  var stepLabel = block.querySelector('.cr-step-label');
  if (stepLabel) {
    stepLabel.textContent = stepTotal > 0
      ? 'Ring ' + ring + '/' + ringsTotal + '  ' + stepDone + '/' + stepTotal : '';
  }

  var frameCount = block.querySelector('.cr-frame-count');
  if (frameCount) frameCount.textContent = frameIdx + ' frames';

  var sessEl = block.querySelector('.cr-session-name');
  if (sessEl) sessEl.textContent = sessName;

  var isActive  = (state !== 'idle' && state !== 'done' && state !== 'error');
  var btnToggle = block.querySelector('.cr-btn-toggle');
  if (btnToggle && btnToggle.dataset.scanning !== (isActive ? '1' : '0')) {
    _crSetToggleBtn(btnToggle, isActive);
  }

  if (state === 'error' && errMsg) {
    var errPrompt = block.querySelector('.cr-error-prompt');
    var errMsgEl  = block.querySelector('.cr-error-msg');
    if (errMsgEl)  errMsgEl.textContent = errMsg;
    if (errPrompt && errPrompt.style.display === 'none')
      errPrompt.style.display = 'flex';
  }
}

// ── Socket events ─────────────────────────────────────────────────────────────

socket.on('cr_status', function(data) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="RTAB-Map_record"]').forEach(function(block) {
    if (block.dataset.cam !== data.cam_id) return;
    _crApplyStatus(block, data);
  });
});

socket.on('frame', function(data) {
  if (!data.cr_status) return;
  var st = data.cr_status;
  document.querySelectorAll('.plugin-ui-block[data-plugin="RTAB-Map_record"]').forEach(function(block) {
    if (block.dataset.cam !== st.cam_id) return;
    _crApplyStatus(block, st);
  });
});

socket.on('cr_event', function(data) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="RTAB-Map_record"]').forEach(function(block) {
    if (block.dataset.cam !== data.cam_id) return;
    var msg = block.querySelector('.cr-msg');
    if (data.kind === 'error') {
      var errPrompt = block.querySelector('.cr-error-prompt');
      var errMsgEl  = block.querySelector('.cr-error-msg');
      if (errMsgEl)  errMsgEl.textContent = data.msg || 'Unknown error';
      if (errPrompt) errPrompt.style.display = 'flex';
      if (msg) msg.textContent = '';
    } else {
      if (msg && data.msg) {
        msg.textContent = data.msg;
        setTimeout(function(){ msg.textContent = ''; }, 3000);
      }
    }
  });
});

socket.on('cr_elevation_prompt', function(data) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="RTAB-Map_record"]').forEach(function(block) {
    if (block.dataset.cam !== data.cam_id) return;
    var prompt = block.querySelector('.cr-elevation-prompt');
    var msgEl  = block.querySelector('.cr-elevation-msg');
    if (msgEl)  msgEl.textContent = 'Ring ' + data.ring + ': adjust to ' + data.elevation + '°, then confirm.';
    if (prompt) prompt.style.display = 'flex';
  });
});

socket.on('cr_servo_status', function(data) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="RTAB-Map_record"]').forEach(function(block) {
    if (block.dataset.cam !== data.cam_id) return;
    var el = block.querySelector('.cr-servo-conn-status');
    if (!el) return;
    el.textContent = data.connected ? 'Connected' : (data.msg || 'Disconnected');
    el.style.color = data.connected ? '#5a9a5a' : '#888';
  });
});

socket.on('cr_servo_log', function(data) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="RTAB-Map_record"]').forEach(function(block) {
    if (block.dataset.cam !== data.cam_id) return;
    var logEl = block.querySelector('.cr-servo-log');
    if (!logEl) return;
    logEl.innerHTML = '';
    (data.lines || []).forEach(function(entry) {
      var line = document.createElement('div');
      line.style.color = entry.ok ? (entry.dir === '>>' ? '#d4d4d4' : '#5a9a5a') : '#c85a5a';
      line.textContent = '[' + entry.ts + '] ' + entry.dir + ' ' + entry.msg;
      logEl.appendChild(line);
    });
    logEl.scrollTop = logEl.scrollHeight;
  });
});

// cr_params_event: intrinsics auto-loaded from calibration files
socket.on('cr_params_event', function(data) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="RTAB-Map_record"]').forEach(function(block) {
    if (block.dataset.cam !== data.cam_id) return;
    var _v = function(sel, val) {
      var el = block.querySelector(sel);
      if (el && val !== undefined && val !== null) el.value = (+val).toFixed(2);
    };
    var srcEl = block.querySelector('.cr-intr-source');
    if (srcEl) srcEl.textContent = data.ok ? '(' + (data.source || '') + ')' : '';
    if (!data.ok) return;
    _v('.cr-fx', data.depth_fx); _v('.cr-fy', data.depth_fy);
    _v('.cr-cx', data.depth_cx); _v('.cr-cy', data.depth_cy);
    if (data.source === 'stereo_cal') {
      var colorSrcEl = block.querySelector('.cr-color-intr-source');
      if (colorSrcEl) colorSrcEl.textContent = '(stereo_cal)';
      _v('.cr-color-fx', data.color_fx); _v('.cr-color-fy', data.color_fy);
      _v('.cr-color-cx', data.color_cx); _v('.cr-color-cy', data.color_cy);
      _v('.cr-ext-tx', data.ext_tx);
      _v('.cr-ext-ty', data.ext_ty);
      _v('.cr-ext-tz', data.ext_tz);
      _crSetExtRMatrix(block, data.ext_R);
    }
  });
});

// ── State sync ────────────────────────────────────────────────────────────────

socket.on('state', function(data) {
  var allCamIds = data.cameras ? Object.keys(data.cameras) : [];

  document.querySelectorAll('.plugin-ui-block[data-plugin="RTAB-Map_record"]').forEach(function(block) {
    var camId = block.dataset.cam;
    var st    = (data.cameras && camId) ? data.cameras[camId] : null;
    if (!st) return;

    var _set = function(sel, val) {
      if (val === undefined || val === null) return;
      var el = block.querySelector(sel);
      if (!el) return;
      if (el.type === 'checkbox') el.checked = !!val;
      else el.value = val;
    };

    _set('.cr-capture-fps',      st.cr_capture_fps);
    _set('.cr-depth-scale',      st.cr_depth_scale);
    _set('.cr-depth-range-mode', st.cr_depth_range_mode);

    if (st.cr_depth_min !== undefined) {
      var minSl  = block.querySelector('.cr-min-slider');
      var minLbl = block.querySelector('.cr-min-label');
      if (minSl) minSl.value = st.cr_depth_min;
      if (minLbl) minLbl.textContent = st.cr_depth_min;
    }
    if (st.cr_depth_max !== undefined) {
      var maxSl  = block.querySelector('.cr-max-slider');
      var maxLbl = block.querySelector('.cr-max-label');
      if (maxSl) maxSl.value = st.cr_depth_max;
      if (maxLbl) maxLbl.textContent = st.cr_depth_max;
    }
    _set('.cr-fx',       st.cr_fx);
    _set('.cr-fy',       st.cr_fy);
    _set('.cr-cx',       st.cr_cx);
    _set('.cr-cy',       st.cr_cy);
    _set('.cr-color-fx', st.cr_color_fx);
    _set('.cr-color-fy', st.cr_color_fy);
    _set('.cr-color-cx', st.cr_color_cx);
    _set('.cr-color-cy', st.cr_color_cy);
    _set('.cr-ext-tx',   st.cr_ext_tx);
    _set('.cr-ext-ty',   st.cr_ext_ty);
    _set('.cr-ext-tz',   st.cr_ext_tz);
    _crSetExtRMatrix(block, st.cr_ext_R);
    _set('.cr-servo-ip',      st.cr_servo_ip);
    _set('.cr-servo-port',    st.cr_servo_port);
    _set('.cr-servo-axis',    st.cr_servo_axis);
    _set('.cr-servo-feed',    st.cr_servo_feed);
    _set('.cr-servo-dwell',   st.cr_servo_dwell);
    _set('.cr-servo-timeout', st.cr_servo_timeout);

    // Colour camera source buttons
    if (st.cr_color_cam_source !== undefined) {
      _crUpdateColorSrcBtns(block, st.cr_color_cam_source);
    }

    // Colour camera list — default empty, no pre-selection
    var ccSel = block.querySelector('.cr-color-cam');
    if (ccSel) {
      while (ccSel.options.length > 1) ccSel.remove(1);
      allCamIds.forEach(function(id) {
        if (id === camId) return;
        var opt = document.createElement('option');
        opt.value = id; opt.textContent = id;
        ccSel.appendChild(opt);
      });
      if (st.cr_color_cam) ccSel.value = st.cr_color_cam;
    }

    // Servo connection status
    var connSt = block.querySelector('.cr-servo-conn-status');
    if (connSt) {
      connSt.textContent = st.cr_servo_connected ? 'Connected' : 'Not connected';
      connSt.style.color = st.cr_servo_connected ? '#5a9a5a' : '#888';
    }

    // Depth range mode visibility
    _crSyncDepthRange(block, st.cr_depth_range_mode || 'auto');

    // Restore scan state
    if (st.cr_scan_state) {
      _crApplyStatus(block, {
        state:        st.cr_scan_state,
        frame_idx:    st.cr_frame_idx    || 0,
        session_name: st.cr_session_name || '',
        step_total:   st.cr_step_total   || 0,
      });
    }
  });
});
