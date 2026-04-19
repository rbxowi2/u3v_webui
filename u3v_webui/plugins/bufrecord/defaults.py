"""plugins/bufrecord/defaults.py — Adjustable defaults for BasicBufRecord."""

JPEG_QUALITY     = 85           # buffer-record save JPEG quality (1–100)
BUF_MAX_BYTES    = 4 * 1024**3  # max RAM for buffer accumulation (4 GB)
BUF_SAVE_WORKERS = None         # ThreadPoolExecutor workers (None = auto)
