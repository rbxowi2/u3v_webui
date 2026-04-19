// overlaytext/ui.js — OverlayText plugin frontend (1.1.0)
// Depends on: socket (global, defined in index.html)
//
// Multi-camera safety: never use getElementById or fixed IDs.
// cam_id is read from closest('.plugin-ui-block').dataset.cam
// so each camera's block is independent.

// Per-camera debounce timers: { cam_id: timerHandle }
const _overlayTextTimers = {};

// ── Helpers ───────────────────────────────────────────────────────────────────

function _overlayTextCamId(el) {
  const block = el.closest(".plugin-ui-block");
  return block ? block.dataset.cam : "";
}

// ── Input / Clear ─────────────────────────────────────────────────────────────

function overlayTextOnInput(el) {
  const cam_id = _overlayTextCamId(el);
  if (!cam_id) return;
  clearTimeout(_overlayTextTimers[cam_id]);
  _overlayTextTimers[cam_id] = setTimeout(() => {
    socket.emit("set_param", { cam_id, key: "overlay_text", value: el.value });
  }, 300);
}

function overlayTextClear(el) {
  const block = el.closest(".plugin-ui-block");
  if (!block) return;
  const cam_id = block.dataset.cam;
  clearTimeout(_overlayTextTimers[cam_id]);
  const ta = block.querySelector(".overlaytext-input");
  if (ta) ta.value = "";
  if (cam_id) socket.emit("set_param", { cam_id, key: "overlay_text", value: "" });
}

// ── State sync ────────────────────────────────────────────────────────────────
// Iterates ALL OverlayText blocks in the DOM so every camera stays in sync.

function _applyOverlayTextState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="OverlayText"]').forEach(block => {
    const cid = block.dataset.cam;
    const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
    if (!cs) return;
    const ta = block.querySelector(".overlaytext-input");
    if (ta && document.activeElement !== ta) {
      ta.value = cs.overlay_text != null ? cs.overlay_text : "";
    }
  });
}

socket.on("state", _applyOverlayTextState);
window.addEventListener("plugin-state-update", (e) => _applyOverlayTextState(e.detail));
