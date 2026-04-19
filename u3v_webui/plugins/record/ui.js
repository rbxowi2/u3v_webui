// record/ui.js — Continuous recording plugin frontend (1.0.0)
// Multi-camera safe: NO getElementById. All queries scoped to .plugin-ui-block.

function _recBlk(el) { return el.closest('.plugin-ui-block'); }
function _recQ(b, k) { return b ? b.querySelector('[data-id="' + k + '"]') : null; }

function recToggleRecord(el) {
  const cam_id = (_recBlk(el) || {}).dataset?.cam || window._selectedCamId || "";
  socket.emit("plugin_action", { cam_id, action: "toggle_record" });
}

function recOnFmt(el) {
  const cam_id = (_recBlk(el) || {}).dataset?.cam || window._selectedCamId || "";
  socket.emit("set_param", { cam_id, key: "rec_fmt", value: el.value });
}

function recOnAudio(el) {
  const cam_id = (_recBlk(el) || {}).dataset?.cam || window._selectedCamId || "";
  socket.emit("set_param", { cam_id, key: "audio_record", value: el.checked });
}

function _applyRecordState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="BasicRecord"]').forEach(block => {
    const cid = block.dataset.cam;
    const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
    if (!cs) return;

    const btnRec = _recQ(block, "btn-record");
    if (btnRec) {
      btnRec.textContent = cs.recording ? "Stop Record" : "Record";
      btnRec.className   = "btn " + (cs.recording ? "btn-red-dim" : "btn-red");
    }

    block.querySelectorAll("input[name='rec_fmt']").forEach(r => {
      r.checked = (r.value === cs.rec_fmt);
    });

    const audioRow = _recQ(block, "audio-row");
    if (audioRow) audioRow.style.display = cs.audio_available ? "flex" : "none";
  });
}

socket.on("state", _applyRecordState);
window.addEventListener("plugin-state-update", (e) => _applyRecordState(e.detail));

// Show record status in the status bar for the selected camera
socket.on("frame", (data) => {
  if (!data.rec_status) return;
  if (data.cam_id === window._selectedCamId) setStatus(data.rec_status);
});

// Keyboard shortcut: 'r' toggles recording for the selected camera
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "r") {
    const sel = window._selectedCamId;
    const blk = document.querySelector(`.plugin-ui-block[data-plugin="BasicRecord"][data-cam="${CSS.escape(sel)}"]`);
    if (blk) recToggleRecord(blk);
  }
});
