// anaglyph/ui.js — Anaglyph Stereo plugin frontend (1.0.0)
// Depends on: socket, mdiStartDrag, mdiStartResize, _mdiCounter (from index.html)

const _ANAGLYPH_WIN = "__anaglyph__";

// ── MDI window ────────────────────────────────────────────────────────────────

function anaglyphShowWindow() {
  _anaglyphEnsureWindow();
  // Bring to front
  const win = document.querySelector(`.mdi-win[data-win="${_ANAGLYPH_WIN}"]`);
  if (win) win.style.zIndex = 30;
}

function _anaglyphEnsureWindow() {
  if (document.querySelector(`.mdi-win[data-win="${_ANAGLYPH_WIN}"]`)) return;

  const ws  = document.getElementById("workspace");
  const off = (typeof _mdiCounter !== "undefined" ? (_mdiCounter++ % 8) : 0) * 22;
  const x   = Math.min(16 + off, Math.max(0, ws.clientWidth  - 480 - 16));
  const y   = Math.min(16 + off, Math.max(0, ws.clientHeight - 360 - 16));

  const win = document.createElement("div");
  win.className  = "mdi-win";
  win.dataset.win = _ANAGLYPH_WIN;
  win.style.cssText = `left:${x}px;top:${y}px;width:480px;height:360px;`;

  win.innerHTML =
    `<div class="mdi-titlebar">
       <span class="mdi-label">Anaglyph Stereo</span>
       <div class="mdi-btns">
         <button class="mdi-btn anaglyph-min-btn" title="Minimize" onclick="_anaglyphToggleMin()">&#8722;</button>
         <button class="mdi-btn anaglyph-max-btn" title="Maximize" onclick="_anaglyphToggleMax()">&#9723;</button>
         <button class="mdi-btn" title="Close"    onclick="_anaglyphCloseWindow()">&#10005;</button>
       </div>
     </div>
     <div class="mdi-content" style="background:#000;display:flex;align-items:center;justify-content:center;overflow:hidden;">
       <span class="anaglyph-placeholder" style="color:#555;font-size:12px;position:absolute;">Select cameras in sidebar</span>
       <img class="anaglyph-stream" alt="anaglyph"
            style="display:none;width:100%;height:100%;object-fit:contain;">
     </div>
     <div class="mdi-resize"></div>`;

  const tb = win.querySelector(".mdi-titlebar");
  tb.addEventListener("mousedown", e => {
    if (e.target.closest(".mdi-btns")) return;
    e.preventDefault();
    mdiStartDrag(e, win);
  });
  tb.addEventListener("touchstart", e => {
    if (e.target.closest(".mdi-btns")) return;
    mdiStartDragTouch(e.touches[0], win);
  }, { passive: true });

  win.querySelector(".mdi-resize").addEventListener("mousedown", e => {
    mdiStartResize(e, win); e.stopPropagation();
  });
  win.querySelector(".mdi-resize").addEventListener("touchstart", e => {
    mdiStartResizeTouch(e.touches[0], win); e.stopPropagation();
  }, { passive: true });

  ws.appendChild(win);
}

function _anaglyphCloseWindow() {
  const win = document.querySelector(`.mdi-win[data-win="${_ANAGLYPH_WIN}"]`);
  if (win) win.remove();
}

let _anaglyphMinimized = false;
let _anaglyphMaxState  = null;

function _anaglyphToggleMin() {
  const win = document.querySelector(`.mdi-win[data-win="${_ANAGLYPH_WIN}"]`);
  if (!win) return;
  _anaglyphMinimized = !_anaglyphMinimized;
  win.classList.toggle("minimized", _anaglyphMinimized);
  win.querySelector(".anaglyph-min-btn").innerHTML = _anaglyphMinimized ? "&#9723;" : "&#8722;";
}

function _anaglyphToggleMax() {
  const win = document.querySelector(`.mdi-win[data-win="${_ANAGLYPH_WIN}"]`);
  if (!win) return;
  const ws  = document.getElementById("workspace");
  const btn = win.querySelector(".anaglyph-max-btn");
  if (_anaglyphMaxState) {
    const s = _anaglyphMaxState;
    win.style.left = s.left; win.style.top  = s.top;
    win.style.width = s.w;   win.style.height = s.h;
    _anaglyphMaxState = null;
    btn.innerHTML = "&#9723;";
  } else {
    _anaglyphMaxState = { left: win.style.left, top: win.style.top,
                          w: win.style.width,   h: win.style.height };
    win.style.left = "0"; win.style.top = "0";
    win.style.width  = ws.clientWidth  + "px";
    win.style.height = ws.clientHeight + "px";
    btn.innerHTML = "&#9724;";
  }
}

// ── Socket events ─────────────────────────────────────────────────────────────

socket.on("anaglyph_frame", (data) => {
  const win = document.querySelector(`.mdi-win[data-win="${_ANAGLYPH_WIN}"]`);
  if (!win) return;
  const img = win.querySelector(".anaglyph-stream");
  const ph  = win.querySelector(".anaglyph-placeholder");
  if (!img) return;
  img.src = "data:image/jpeg;base64," + data.img;
  img.style.display = "";
  if (ph) ph.style.display = "none";
});

// ── Sidebar controls ──────────────────────────────────────────────────────────

function _anaglyphBlock(el) { return el.closest(".plugin-ui-block"); }

function anaglyphOnCam(el, side) {
  const block  = _anaglyphBlock(el);
  const cam_id = block ? block.dataset.cam : "";
  if (!cam_id) return;
  const key = side === "left" ? "anaglyph_left_cam" : "anaglyph_right_cam";
  socket.emit("set_param", { cam_id, key, value: el.value });
}

function anaglyphOnChannel(el) {
  const block  = _anaglyphBlock(el);
  const cam_id = block ? block.dataset.cam : "";
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: "anaglyph_left_is_red", value: el.value === "1" });
}

function anaglyphOnParallax(el) {
  const block  = _anaglyphBlock(el);
  const cam_id = block ? block.dataset.cam : "";
  if (!cam_id) return;
  const v = parseInt(el.value);
  const lbl = el.closest(".collapsible-body")?.querySelector(".anaglyph-parallax-label");
  if (lbl) lbl.textContent = v;
  socket.emit("set_param", { cam_id, key: "anaglyph_parallax", value: v });
}

// ── State sync ────────────────────────────────────────────────────────────────

function _applyAnaglyphState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="Anaglyph"]').forEach(block => {
    // Global plugin: read state from any open camera
    const cams   = Object.keys(s.cameras || {});
    const cs     = cams.length ? (s.cameras[cams[0]] || {}) : {};

    const leftSel  = block.querySelector(".anaglyph-left-cam");
    const rightSel = block.querySelector(".anaglyph-right-cam");

    [leftSel, rightSel].forEach(sel => {
      if (!sel) return;
      const cur = sel.value;
      sel.innerHTML = '<option value="">-- none --</option>' +
        cams.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
      sel.value = cur;   // restore after repopulate
    });

    if (leftSel  && cs.anaglyph_left_cam  != null) leftSel.value  = cs.anaglyph_left_cam;
    if (rightSel && cs.anaglyph_right_cam != null) rightSel.value = cs.anaglyph_right_cam;

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
