"""
ML Training Data Logger
=======================
Structured JSONL telemetry for training ML models on download behaviour.

Event types:
    U  — Usage      (menu selections, feature toggles, settings)
    D  — Download   (start, done, pause, fail, resume)
    S  — Security   (cookie attempts, bypass strategies, captcha, proxy)
    P  — Platform   (HTTP responses, probes, API calls)
    E  — Error      (retries, failures, fallbacks)
    X  — Performance (chunk stats, thread scaling, speed metrics)

Record format (one JSON object per line):
    {"ts": 1721500000.1, "t": "U", "ev": "menu", "d": {"opt": 1}}

Storage optimizations:
    - Short field names (~40% savings)
    - URL hashing — SHA-256 first 12 hex chars
    - Epoch floats instead of ISO strings
    - Summary events only (no per-chunk ticks)
    - Gzip rotation at 25 MB, keep 10 files
"""

import os
import sys
import json
import gzip
import time
import hashlib
import threading
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ─── Constants ──────────────────────────────────────────────────────────
_MAX_FILE_BYTES = 25 * 1024 * 1024   # 25 MB before rotation
_MAX_ROTATED    = 10                  # keep last 10 gzipped files
_URL_HASH_LEN   = 12                 # first 12 hex chars of SHA-256

# Event type codes
TYPE_USAGE       = "U"
TYPE_DOWNLOAD    = "D"
TYPE_SECURITY    = "S"
TYPE_PLATFORM    = "P"
TYPE_ERROR       = "E"
TYPE_PERFORMANCE = "X"


def url_hash(url: str) -> str:
    """Compact, privacy-safe URL fingerprint (12-char hex)."""
    return hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:_URL_HASH_LEN]


class MLLogger:
    """
    Thread-safe, rotation-aware JSONL logger for ML training data.

    Usage::

        ml = get_ml_logger()            # or MLLogger("/path/to/file.jsonl")
        ml.log_usage("menu", opt=22)
        ml.log_download("start", url, pl="youtube", q="1080p")
    """

    def __init__(self, filepath: str, enabled: bool = True):
        self._filepath = filepath
        self._enabled = enabled
        self._lock = threading.Lock()
        self._file = None
        self._bytes_written = 0

        if self._enabled:
            self._open()

    # ── low-level I/O ───────────────────────────────────────────────────

    def _open(self):
        """Open (or create) the JSONL log file in append mode."""
        try:
            os.makedirs(os.path.dirname(self._filepath) or ".", exist_ok=True)
            self._file = open(self._filepath, "a", encoding="utf-8")
            # Track current file size for rotation check
            try:
                self._bytes_written = os.path.getsize(self._filepath)
            except OSError:
                self._bytes_written = 0
        except Exception as e:
            logger.warning(f"MLLogger: cannot open {self._filepath}: {e}")
            self._enabled = False

    def _rotate(self):
        """Rotate the current log: close → gzip → reopen."""
        if self._file:
            self._file.close()
            self._file = None

        # Compress current file
        gz_path = f"{self._filepath}.{int(time.time())}.gz"
        try:
            with open(self._filepath, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                while True:
                    chunk = f_in.read(1024 * 1024)
                    if not chunk:
                        break
                    f_out.write(chunk)
            os.remove(self._filepath)
        except Exception as e:
            logger.warning(f"MLLogger: rotation failed: {e}")

        # Prune old rotated files
        self._prune_rotated()

        # Reopen fresh
        self._bytes_written = 0
        self._open()

    def _prune_rotated(self):
        """Keep only the newest _MAX_ROTATED gzipped files."""
        directory = os.path.dirname(self._filepath) or "."
        base = os.path.basename(self._filepath)
        gz_files = sorted(
            [f for f in os.listdir(directory) if f.startswith(base) and f.endswith(".gz")],
            key=lambda f: os.path.getmtime(os.path.join(directory, f)),
            reverse=True,
        )
        for old in gz_files[_MAX_ROTATED:]:
            try:
                os.remove(os.path.join(directory, old))
            except OSError:
                pass

    # ── core write ──────────────────────────────────────────────────────

    def log(self, event_type: str, event: str, data: Dict[str, Any] = None):
        """
        Write one JSONL record.

        Args:
            event_type: Single-char type code (U/D/S/P/E/X).
            event:      Short event name (e.g. "menu", "start", "done").
            data:       Arbitrary dict of event-specific fields.
        """
        if not self._enabled:
            return

        record = {
            "ts": round(time.time(), 3),
            "t": event_type,
            "ev": event,
        }
        if data:
            # Strip None values to save space
            record["d"] = {k: v for k, v in data.items() if v is not None}

        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        line_bytes = len(line.encode("utf-8"))

        with self._lock:
            if self._file is None:
                return
            try:
                self._file.write(line)
                self._file.flush()
                self._bytes_written += line_bytes

                if self._bytes_written >= _MAX_FILE_BYTES:
                    self._rotate()
            except Exception as e:
                logger.warning(f"MLLogger: write error: {e}")

    # ── convenience methods ─────────────────────────────────────────────

    def log_usage(self, event: str, **data):
        """Log a usage event (menu selections, feature toggles, settings)."""
        self.log(TYPE_USAGE, event, data)

    def log_download(self, event: str, url: str = None, **data):
        """Log a download lifecycle event (start/done/pause/fail/resume)."""
        if url:
            data["h"] = url_hash(url)
        self.log(TYPE_DOWNLOAD, event, data)

    def log_security(self, event: str, **data):
        """Log a security event (cookie try, bypass, captcha, proxy)."""
        self.log(TYPE_SECURITY, event, data)

    def log_platform(self, event: str, url: str = None, **data):
        """Log a platform interaction event (HTTP response, probe, API)."""
        if url:
            data["h"] = url_hash(url)
        self.log(TYPE_PLATFORM, event, data)

    def log_error(self, event: str, url: str = None, **data):
        """Log an error/retry event."""
        if url:
            data["h"] = url_hash(url)
        self.log(TYPE_ERROR, event, data)

    def log_performance(self, event: str, url: str = None, **data):
        """Log a performance metric event (chunk stats, thread scaling)."""
        if url:
            data["h"] = url_hash(url)
        self.log(TYPE_PERFORMANCE, event, data)

    # ── lifecycle ───────────────────────────────────────────────────────

    def close(self):
        """Flush and close the log file."""
        with self._lock:
            if self._file:
                try:
                    self._file.flush()
                    self._file.close()
                except Exception:
                    pass
                self._file = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def filepath(self) -> str:
        return self._filepath

    @property
    def size_bytes(self) -> int:
        return self._bytes_written


# ─── Singleton ──────────────────────────────────────────────────────────

_instance: Optional[MLLogger] = None
_instance_lock = threading.Lock()


def get_ml_logger(filepath: str = None, enabled: bool = True) -> MLLogger:
    """
    Get or create the global MLLogger singleton.

    First call sets the filepath; subsequent calls return the existing instance.
    If filepath is None on first call, tries to use Config.ML_LOG_PATH.
    """
    global _instance
    if _instance is not None:
        return _instance

    with _instance_lock:
        if _instance is not None:
            return _instance

        if filepath is None:
            # Try to get path from config
            try:
                from config import Config
                filepath = Config.ML_LOG_PATH
            except (ImportError, AttributeError):
                # Fallback: same directory as this script
                filepath = os.path.join(
                    os.path.expanduser("~"), "Downloads", "downloader", "ml_training.jsonl"
                )

        _instance = MLLogger(filepath, enabled=enabled)
        return _instance
