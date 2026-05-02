// lensundistort/ui.js — LensUndistort plugin frontend (1.2.0)
// v1.2.0: Add scale buttons — 1×, ½×, ¼× pipeline output resolution.
// v1.1.0: Add FOV Out mode — specify rectilinear output H-FOV instead of balance.

(function () {
  'use strict';

  function _blk(el)    { return el.closest('.plugin-ui-block'); }
  function _blkCam(el) { const b = _blk(el); return b ? b.dataset.cam : ''; }

  // ── Scale helper ───────────────────────────────────────────────────────────

  function _setScaleBtn(block, scale) {
    block.querySelectorAll('.undistort-scale-btn').forEach(btn => {
      const active = (parseFloat(btn.dataset.scale) === scale);
      Object.assign(btn.style, active
        ? { background: '#1a3a1a', color: '#5a9a5a', borderColor: '#2a6a2a' }
        : { background: '#2a2a2a', color: '#888',    borderColor: '#444'    });
    });
  }

  window.undistortOnScale = function (el, scale) {
    const block  = _blk(el);
    const cam_id = _blkCam(el);
    if (!block || !cam_id) return;
    _setScaleBtn(block, scale);
    socket.emit('set_param', { cam_id, key: 'undistort_scale', value: scale });
  };

  // ── Mode helper ────────────────────────────────────────────────────────────

  function _setMode(block, mode) {
    const isFov = (mode === 'fov');
    block.querySelectorAll('.undistort-mode-btn').forEach(btn => {
      const active = (btn.dataset.mode === mode);
      Object.assign(btn.style, active
        ? { background: '#1a2a4a', color: '#6ab0d4', borderColor: '#3a7bd5' }
        : { background: '#2a2a2a', color: '#666',    borderColor: '#444'    });
    });
    const balPanel = block.querySelector('.undistort-balance-panel');
    const fovPanel = block.querySelector('.undistort-fov-panel');
    if (balPanel) balPanel.style.display = isFov ? 'none'  : 'block';
    if (fovPanel) fovPanel.style.display = isFov ? 'flex'  : 'none';
  }

  // ── Sidebar param handlers ─────────────────────────────────────────────────

  window.undistortOnEnable = function (el) {
    const cam_id = _blkCam(el);
    if (cam_id) socket.emit('set_param', { cam_id, key: 'undistort_enabled', value: el.checked });
  };

  window.undistortOnBalanceInput = function (el) {
    const block = _blk(el);
    if (!block) return;
    const lbl = block.querySelector('.undistort-balance-label');
    if (lbl) lbl.textContent = parseFloat(el.value).toFixed(2);
  };

  window.undistortOnBalanceChange = function (el) {
    const cam_id = _blkCam(el);
    if (cam_id) socket.emit('set_param', { cam_id, key: 'undistort_balance', value: parseFloat(el.value) });
  };

  window.undistortOnModeSwitch = function (el, mode) {
    const block = _blk(el);
    if (!block) return;
    const cam_id = block.dataset.cam;
    _setMode(block, mode);
    if (mode === 'balance') {
      if (cam_id) socket.emit('set_param', { cam_id, key: 'undistort_fov_out', value: 0 });
    } else {
      const inp = block.querySelector('.undistort-fov-input');
      const fov = inp ? (parseFloat(inp.value) || 90) : 90;
      if (cam_id) socket.emit('set_param', { cam_id, key: 'undistort_fov_out', value: fov });
    }
  };

  window.undistortOnFovChange = function (el) {
    const cam_id = _blkCam(el);
    const fov = Math.max(10, Math.min(179, parseFloat(el.value) || 90));
    el.value = fov;
    if (cam_id) socket.emit('set_param', { cam_id, key: 'undistort_fov_out', value: fov });
  };

  window.undistortOnReload = function (el) {
    const cam_id = _blkCam(el);
    if (cam_id) socket.emit('plugin_action', { cam_id, action: 'undistort_reload' });
  };

  // ── State sync ─────────────────────────────────────────────────────────────

  function _applyUndistortState(s) {
    document.querySelectorAll('.plugin-ui-block[data-plugin="LensUndistort"]').forEach(block => {
      const cid = block.dataset.cam;
      const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
      if (!cs) return;

      const tog    = block.querySelector('.undistort-enable');
      const slider = block.querySelector('.undistort-balance-slider');
      const lbl    = block.querySelector('.undistort-balance-label');
      const fovInp = block.querySelector('.undistort-fov-input');
      const status = block.querySelector('.undistort-cal-status');

      if (tog)    tog.checked   = !!cs.undistort_enabled;
      if (slider) slider.value  = cs.undistort_balance ?? 0;
      if (lbl)    lbl.textContent = (cs.undistort_balance ?? 0).toFixed(2);

      const fovOut = cs.undistort_fov_out ?? 0;
      if (fovInp && fovOut > 0) fovInp.value = fovOut;
      _setMode(block, fovOut > 0 ? 'fov' : 'balance');

      _setScaleBtn(block, cs.undistort_scale ?? 1.0);

      if (status) {
        if (!cs.undistort_has_cal) {
          status.innerHTML = '<span style="color:#c0392b;">No calibration data — run LensCalibrate first</span>';
        } else {
          const rms    = cs.undistort_rms;
          const rmsCol = rms < 0.5 ? '#7dcf7d' : rms < 1.0 ? '#e8a43c' : '#e74c3c';
          const date   = cs.undistort_calibrated_at ? cs.undistort_calibrated_at.slice(0, 10) : '';
          const maps   = cs.undistort_maps_ready
            ? '<span style="color:#7dcf7d;">Maps ready</span>'
            : '<span style="color:#888;">Maps build on first frame</span>';
          status.innerHTML = [
            `${cs.undistort_lens_type === 'fisheye' ? 'Fisheye' : 'Normal'} &nbsp;|&nbsp;` +
            ` RMS <span style="color:${rmsCol};font-weight:600;">${rms.toFixed(3)}</span>` +
            (date ? ` &nbsp;|&nbsp; ${date}` : ''),
            maps,
          ].join('<br>');
        }
      }
    });
  }

  socket.on('state', _applyUndistortState);
  window.addEventListener('plugin-state-update', e => _applyUndistortState(e.detail));

}());
