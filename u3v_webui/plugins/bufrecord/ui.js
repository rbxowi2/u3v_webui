// bufrecord/ui.js — Buffer-record plugin frontend (1.0.0)
// Multi-camera safe: NO getElementById. All queries scoped to .plugin-ui-block.

function _bufBlk(el) { return el.closest('.plugin-ui-block'); }
function _bufQ(b, k) { return b ? b.querySelector('[data-id="' + k + '"]') : null; }

function bufToggleBuf(el) {
  const cam_id = (_bufBlk(el) || {}).dataset?.cam || window._selectedCamId || "";
  socket.emit("plugin_action", { cam_id, action: "toggle_buf_record" });
}

function bufOnFmt(el) {
  const cam_id = (_bufBlk(el) || {}).dataset?.cam || window._selectedCamId || "";
  socket.emit("set_param", { cam_id, key: "buf_fmt", value: el.value });
}

function _applyBufRecordState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="BasicBufRecord"]').forEach(block => {
    const cid = block.dataset.cam;
    const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
    if (!cs) return;

    const btnBuf = _bufQ(block, "btn-buf");
    if (btnBuf) {
      if (cs.buf_saving) {
        btnBuf.textContent = "Saving...";        btnBuf.className = "btn btn-gray";
      } else if (cs.buf_recording) {
        btnBuf.textContent = "Stop Buffer Rec";  btnBuf.className = "btn btn-red-dim";
      } else {
        btnBuf.textContent = "Buffer Rec (RAM)"; btnBuf.className = "btn btn-purple";
      }
    }

    block.querySelectorAll("input[name='buf_fmt']").forEach(r => {
      r.checked = (r.value === cs.buf_fmt);
    });
  });
}

socket.on("state", _applyBufRecordState);
window.addEventListener("plugin-state-update", (e) => _applyBufRecordState(e.detail));

// Show buffer status in the status bar for the selected camera
socket.on("frame", (data) => {
  if (!data.buf_status) return;
  if (data.cam_id === window._selectedCamId) setStatus(data.buf_status);
});
