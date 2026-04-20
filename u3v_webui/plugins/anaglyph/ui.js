// anaglyph/ui.js — Anaglyph Stereo plugin frontend (2.1.0)

function _anaglyphBlock(el) { return el.closest(".plugin-ui-block"); }
function _anaglyphCam(el)   { const b = _anaglyphBlock(el); return b ? b.dataset.cam : ""; }

function anaglyphOnCam(el, side) {
  const cam_id = _anaglyphCam(el);
  if (!cam_id) return;
  const key = side === "left" ? "anaglyph_left_cam" : "anaglyph_right_cam";
  socket.emit("set_param", { cam_id, key, value: el.value });
}

function anaglyphOnSource(el, side) {
  const cam_id = _anaglyphCam(el);
  if (!cam_id) return;
  const key = side === "left" ? "anaglyph_left_source" : "anaglyph_right_source";
  socket.emit("set_param", { cam_id, key, value: el.value });
}

function anaglyphOnColor(el) {
  const cam_id = _anaglyphCam(el);
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: "anaglyph_color_mode", value: el.value });
}

function anaglyphOnChannel(el) {
  const cam_id = _anaglyphCam(el);
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: "anaglyph_left_is_red", value: el.value === "1" });
}

function anaglyphOnParallax(el) {
  const cam_id = _anaglyphCam(el);
  if (!cam_id) return;
  const v = parseInt(el.value);
  const lbl = el.closest(".collapsible-body")?.querySelector(".anaglyph-parallax-label");
  if (lbl) lbl.textContent = v;
  socket.emit("set_param", { cam_id, key: "anaglyph_parallax", value: v });
}

// ── State sync ────────────────────────────────────────────────────────────────

function _applyAnaglyphState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="Anaglyph"]').forEach(block => {
    const cam_id = block.dataset.cam;
    const cs     = (s.cameras || {})[cam_id] || {};
    const cams   = Object.keys(s.cameras || {});

    const leftSel  = block.querySelector(".anaglyph-left-cam");
    const rightSel = block.querySelector(".anaglyph-right-cam");

    [leftSel, rightSel].forEach(sel => {
      if (!sel) return;
      const cur = sel.value;
      sel.innerHTML = '<option value="">-- none --</option>' +
        cams.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
      sel.value = cur;
    });

    if (leftSel  && cs.anaglyph_left_cam  != null) leftSel.value  = cs.anaglyph_left_cam;
    if (rightSel && cs.anaglyph_right_cam != null) rightSel.value = cs.anaglyph_right_cam;

    const leftSrc  = block.querySelector(".anaglyph-left-src");
    const rightSrc = block.querySelector(".anaglyph-right-src");
    if (leftSrc  && cs.anaglyph_left_source  != null) leftSrc.value  = cs.anaglyph_left_source;
    if (rightSrc && cs.anaglyph_right_source != null) rightSrc.value = cs.anaglyph_right_source;

    const colorEl = block.querySelector(".anaglyph-color");
    if (colorEl && cs.anaglyph_color_mode != null) colorEl.value = cs.anaglyph_color_mode;

    const chanEl = block.querySelector(".anaglyph-channel");
    if (chanEl && cs.anaglyph_left_is_red != null)
      chanEl.value = cs.anaglyph_left_is_red ? "1" : "0";

    const parEl = block.querySelector(".anaglyph-parallax");
    if (parEl && cs.anaglyph_parallax != null) {
      parEl.value = cs.anaglyph_parallax;
      const lbl = block.querySelector(".anaglyph-parallax-label");
      if (lbl) lbl.textContent = cs.anaglyph_parallax;
    }
  });
}

socket.on("state", _applyAnaglyphState);
window.addEventListener("plugin-state-update", e => _applyAnaglyphState(e.detail));
