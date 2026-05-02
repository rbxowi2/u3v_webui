"""cloudrecord/servo_client.py — TCP/GRBL client (1.0.0)"""

import socket
import threading
import time
from datetime import datetime
from typing import Callable, Optional


class GRBLClient:
    """Non-blocking TCP client for GRBL motion controller."""

    def __init__(self, log_max: int = 50):
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._connected = False
        self._log: list = []
        self._log_max = log_max
        self._on_log: Optional[Callable] = None
        self._recv_buf = ""

    def connect(self, ip: str, port: int, timeout: float = 5.0) -> tuple:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, int(port)))
            with self._lock:
                self._sock = sock
                self._connected = True
                self._recv_buf = ""
            time.sleep(0.15)
            self._drain()
            self._log_entry(">>", f"Connected to {ip}:{port}", True)
            return True, "Connected"
        except Exception as e:
            return False, str(e)

    def disconnect(self):
        with self._lock:
            self._connected = False
            s = self._sock
            self._sock = None
        if s:
            try: s.close()
            except Exception: pass
        self._log_entry(">>", "Disconnected", True)

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def send_and_wait(self, cmd: str, timeout: float = 10.0) -> tuple:
        with self._lock:
            if not self._connected or self._sock is None:
                return False, "Not connected"
            sock = self._sock
        line = cmd.strip() + "\n"
        self._log_entry(">>", cmd.strip(), True)
        try:
            sock.sendall(line.encode())
        except Exception as e:
            self._mark_disconnected()
            return False, f"Send error: {e}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                sock.settimeout(min(0.5, deadline - time.monotonic()))
                chunk = sock.recv(256).decode(errors="replace")
                if not chunk:
                    self._mark_disconnected()
                    return False, "Connection closed"
                with self._lock:
                    self._recv_buf += chunk
                while "\n" in self._recv_buf:
                    with self._lock:
                        nl   = self._recv_buf.index("\n")
                        resp = self._recv_buf[:nl].strip()
                        self._recv_buf = self._recv_buf[nl + 1:]
                    if not resp:
                        continue
                    is_ok  = (resp == "ok")
                    is_err = resp.lower().startswith("error") or resp.lower().startswith("alarm")
                    self._log_entry("<<", resp, not is_err)
                    if is_ok:  return True,  resp
                    if is_err: return False, resp
            except socket.timeout:
                continue
            except Exception as e:
                self._mark_disconnected()
                return False, f"Recv error: {e}"
        return False, "Timeout"

    def send_raw(self, cmd: str):
        with self._lock:
            if not self._connected or self._sock is None: return
            sock = self._sock
        try:
            sock.sendall((cmd.strip() + "\n").encode())
            self._log_entry(">>", cmd.strip(), True)
        except Exception as e:
            self._log_entry(">>", f"Send error: {e}", False)

    def _drain(self):
        with self._lock:
            if self._sock is None: return
            s = self._sock
        try:
            s.settimeout(0.3)
            while True:
                if not s.recv(256): break
        except Exception:
            pass

    def _mark_disconnected(self):
        with self._lock:
            self._connected = False
            self._sock = None
        self._log_entry("<<", "Connection lost", False)

    def _log_entry(self, direction: str, msg: str, ok: bool):
        entry = {"ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                 "dir": direction, "msg": msg, "ok": ok}
        with self._lock:
            self._log.append(entry)
            if len(self._log) > self._log_max:
                self._log = self._log[-self._log_max:]
        if self._on_log:
            try: self._on_log(list(self._log))
            except Exception: pass

    def get_log(self) -> list:
        with self._lock: return list(self._log)

    def set_log_callback(self, cb: Callable): self._on_log = cb

    def set_log_max(self, n: int):
        with self._lock: self._log_max = max(10, int(n))
