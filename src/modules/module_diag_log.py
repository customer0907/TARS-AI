"""
module_diag_log.py

Append-only diagnostic log for long-run STT/audio debugging (separate from console).
"""

import os
import threading
from datetime import datetime

_lock = threading.Lock()
_log_path = None


def init_diag_log(base_dir=None):
    """Create logs/tars_diag.log under src (or base_dir). Returns absolute path."""
    global _log_path
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    _log_path = os.path.join(log_dir, "tars_diag.log")
    diag_log(f"=== diag log started pid={os.getpid()} ===")
    return _log_path


def get_diag_log_path():
    return _log_path


def diag_log(message: str):
    if not _log_path:
        return
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    with _lock:
        with open(_log_path, "a", encoding="utf-8") as handle:
            handle.write(line)
