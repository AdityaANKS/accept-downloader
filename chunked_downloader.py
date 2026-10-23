"""
Multi-Threaded Chunked Downloader with 
AI Smart Acceleration intelligent chunking.

Features:
  - Split files into N chunks, download in parallel with Range headers
  - AI Smart Acceleration: dynamic thread count + work stealing
  - Pause / resume with .download state files
  - Speed limiting (global token bucket)
  - Automatic fallback to single-thread if server doesn't support ranges
  - Real-time progress tracking
"""

import os
import sys
import json
import time
import threading
import hashlib
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple, Callable, Any
from concurrent.futures import ThreadPoolExecutor, Future
from urllib.parse import urlparse, unquote
from enum import Enum

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ML training logger (optional)
try:
    from ml_logger import get_ml_logger as _get_ml_logger
    _ML_AVAILABLE = True
except ImportError:
    _ML_AVAILABLE = False

from config import Config as _Cfg
_DEFAULT_FILES_DIR = _Cfg.FILES_DIR

from utils import format_size, format_duration, sanitize_filename, get_logger

logger = get_logger()


# ============================================================================
# DATA CLASSES
# ============================================================================

class TaskStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    MERGING = "merging"


@dataclass
class ChunkState:
    """State of a single download chunk."""
    index: int
    start: int           # byte offset start
    end: int             # byte offset end (inclusive)
    downloaded: int = 0  # bytes downloaded so far
    status: str = "pending"  # pending, downloading, complete, failed
    retries: int = 0
    speed: float = 0.0   # current speed in bytes/sec
    
    @property
    def total(self) -> int:
        return self.end - self.start + 1
    
    @property
    def remaining(self) -> int:
        return max(0, self.total - self.downloaded)
    
    @property
    def progress(self) -> float:
        return (self.downloaded / self.total * 100) if self.total > 0 else 0
    
    @property
    def current_offset(self) -> int:
        """Current byte offset for resume."""
        return self.start + self.downloaded

    def to_dict(self) -> Dict:
        return {
            'index': self.index, 'start': self.start, 'end': self.end,
            'downloaded': self.downloaded, 'status': self.status,
            'retries': self.retries,
        }
    
    @classmethod
    def from_dict(cls, d: Dict) -> 'ChunkState':
        return cls(
            index=d['index'], start=d['start'], end=d['end'],
            downloaded=d.get('downloaded', 0), status=d.get('status', 'pending'),
            retries=d.get('retries', 0),
        )


@dataclass
class DownloadProgress:
    """Aggregate progress for UI updates."""
    total_size: int = 0
    downloaded: int = 0
    speed: float = 0.0
    eta: float = 0.0
    percent: float = 0.0
    active_threads: int = 0
    status: str = "pending"
    filename: str = ""
    chunks: List[Dict] = field(default_factory=list)


@dataclass
class DownloadTask:
    """A pausable/resumable download task."""
    url: str
    filepath: str
    total_size: int = 0
    downloaded: int = 0
    status: str = "pending"
    chunks: List[ChunkState] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    supports_ranges: bool = False
    filename: str = ""
    content_type: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    thread_count: int = 8
    etag: str = ""
    
    def to_dict(self) -> Dict:
        return {
            'url': self.url,
            'filepath': self.filepath,
            'total_size': self.total_size,
            'downloaded': self.downloaded,
            'status': self.status,
            'chunks': [c.to_dict() for c in self.chunks],
            'headers': self.headers,
            'supports_ranges': self.supports_ranges,
            'filename': self.filename,
            'content_type': self.content_type,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'thread_count': self.thread_count,
            'etag': self.etag,
        }
    
    @classmethod
    def from_dict(cls, d: Dict) -> 'DownloadTask':
        task = cls(
            url=d['url'],
            filepath=d['filepath'],
            total_size=d.get('total_size', 0),
            downloaded=d.get('downloaded', 0),
            status=d.get('status', 'pending'),
            headers=d.get('headers', {}),
            supports_ranges=d.get('supports_ranges', False),
            filename=d.get('filename', ''),
            content_type=d.get('content_type', ''),
            created_at=d.get('created_at', 0),
            updated_at=d.get('updated_at', 0),
            thread_count=d.get('thread_count', 8),
            etag=d.get('etag', ''),
        )
        task.chunks = [ChunkState.from_dict(c) for c in d.get('chunks', [])]
        return task
    
    @property
    def state_file(self) -> str:
        """Path to the .download state file for pause/resume."""
        return self.filepath + ".download"
    
    def save_state(self):
        """Save current state to .download file."""
        self.updated_at = time.time()
        self.downloaded = sum(c.downloaded for c in self.chunks)
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save state: {e}")
    
    @classmethod
    def load_state(cls, state_file: str) -> Optional['DownloadTask']:
        """Load state from .download file."""
        if not os.path.exists(state_file):
            return None
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return cls.from_dict(json.load(f))
        except Exception as e:
            logger.warning(f"Failed to load state: {e}")
            return None
    
    def cleanup_state(self):
        """Remove the .download state file after completion."""
        try:
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
        except Exception:
            pass


# ============================================================================
# SPEED LIMITER (Token Bucket)
# ============================================================================

class SpeedLimiter:
    """Global speed limiter using token bucket algorithm."""
    
    def __init__(self, max_speed_bps: int = 0):
        """
        Args:
            max_speed_bps: Maximum speed in bytes/sec. 0 = unlimited.
        """
        self._max_speed = max_speed_bps
        self._tokens = 0.0
        self._last_fill = time.monotonic()
        self._lock = threading.Lock()
    
    @property
    def max_speed(self) -> int:
        return self._max_speed
    
    @max_speed.setter
    def max_speed(self, value: int):
        with self._lock:
            self._max_speed = max(0, value)
            self._tokens = 0.0
            self._last_fill = time.monotonic()
    
    def acquire(self, nbytes: int) -> float:
        """
        Request permission to transfer nbytes.
        Returns the number of seconds to sleep before transferring.
        """
        if self._max_speed <= 0:
            return 0.0
        
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_fill
            self._last_fill = now
            
            # Refill tokens
            self._tokens += elapsed * self._max_speed
            # Cap at 2 seconds worth of burst
            self._tokens = min(self._tokens, self._max_speed * 2)
            
            if self._tokens >= nbytes:
                self._tokens -= nbytes
                return 0.0
            else:
                deficit = nbytes - self._tokens
                self._tokens = 0.0
                return deficit / self._max_speed


# ============================================================================
# AI SMART ACCELERATOR
# ============================================================================

class AIAccelerator:
    """
    AI Smart Acceleration — Ghost Downloader 3's key feature.
    
    Dynamically adjusts download behavior based on real-time metrics:
    1. Dynamic thread count: increase/decrease threads based on speed gains
    2. Work stealing: fast-finishing chunks take over slow chunks' remaining work
    3. Slow chunk detection: restart chunks that are significantly slower than peers
    """
    
    def __init__(self, min_threads: int = 2, max_threads: int = 32,
                 initial_threads: int = 8):
        self.min_threads = min_threads
        self.max_threads = max_threads
        self.current_threads = initial_threads
        self._speed_history: List[Tuple[float, float]] = []  # (timestamp, speed)
        self._adjustment_interval = 5.0  # seconds between adjustments
        self._last_adjustment = 0.0
        self._lock = threading.Lock()
        self._enabled = True
    
    def record_speed(self, speed: float):
        """Record current aggregate speed."""
        now = time.monotonic()
        with self._lock:
            self._speed_history.append((now, speed))
            # Keep last 30 seconds of history
            cutoff = now - 30
            self._speed_history = [(t, s) for t, s in self._speed_history if t > cutoff]
    
    def should_adjust(self) -> bool:
        """Check if it's time for a thread count adjustment."""
        now = time.monotonic()
        return self._enabled and (now - self._last_adjustment) >= self._adjustment_interval
    
    def suggest_thread_count(self) -> int:
        """
        Analyze speed trends and suggest optimal thread count.
        
        Strategy:
        - If speed is increasing with more threads → add more
        - If speed plateaued or decreased → reduce
        - Never go below min_threads or above max_threads
        """
        with self._lock:
            if len(self._speed_history) < 4:
                return self.current_threads
            
            self._last_adjustment = time.monotonic()
            
            # Compare recent speed (last 5s) vs previous (5-10s ago)
            now = time.monotonic()
            recent = [s for t, s in self._speed_history if t > now - 5]
            previous = [s for t, s in self._speed_history if now - 10 < t <= now - 5]
            
            if not recent or not previous:
                return self.current_threads
            
            avg_recent = sum(recent) / len(recent)
            avg_previous = sum(previous) / len(previous)
            
            if avg_previous <= 0:
                return self.current_threads
            
            ratio = avg_recent / avg_previous
            
            if ratio > 1.1:
                # Speed increased >10% → try adding threads
                new = min(self.current_threads + 2, self.max_threads)
            elif ratio < 0.85:
                # Speed decreased >15% → reduce threads
                new = max(self.current_threads - 2, self.min_threads)
            else:
                # Stable — keep current
                new = self.current_threads
            
            if new != self.current_threads:
                logger.debug(f"AI Accelerator: {self.current_threads} → {new} threads "
                           f"(speed ratio: {ratio:.2f})")
                self.current_threads = new
            
            return self.current_threads
    
    def find_steal_candidates(self, chunks: List[ChunkState]) -> List[Tuple[int, int]]:
        """
        Work stealing: find chunks where a completed chunk can take over
        remaining work from a slow chunk.
        
        Returns list of (slow_chunk_index, new_split_point) tuples.
        """
        active = [c for c in chunks if c.status == 'downloading' and c.remaining > 0]
        completed = [c for c in chunks if c.status == 'complete']
        
        if not active or not completed:
            return []
        
        # Find the slowest active chunk with significant remaining work
        steal_ops = []
        for slow in active:
            if slow.remaining < 1024 * 1024:  # Less than 1MB remaining, not worth splitting
                continue
            
            if slow.speed > 0:
                # Estimated time remaining for this chunk
                eta = slow.remaining / slow.speed
                if eta < 10:  # Less than 10 seconds, not worth it
                    continue
            
            # Split the remaining work in half
            split_point = slow.current_offset + slow.remaining // 2
            steal_ops.append((slow.index, split_point))
        
        return steal_ops


# ============================================================================
# CHUNKED DOWNLOADER
# ============================================================================

class ChunkedDownloader:
    """
    Multi-threaded chunked file downloader with AI smart acceleration.
    
    Usage:
        dl = ChunkedDownloader(threads=8)
        result = dl.download(
            url="https://example.com/file.zip",
            output_dir="./downloads/",
            progress_callback=lambda p: print(f"{p.percent:.1f}%")
        )
    """
    
    # Default settings
    DEFAULT_THREADS = 8
    DEFAULT_CHUNK_SIZE_MB = 10
    MAX_RETRIES = 5
    RETRY_DELAY = 2.0
    STATE_SAVE_INTERVAL = 3.0  # Save state every N seconds
    BUFFER_SIZE = 64 * 1024    # 64KB read buffer
    
    def __init__(self,
                 threads: int = None,
                 chunk_size_mb: int = None,
                 speed_limit_bps: int = 0,
                 ai_acceleration: bool = True,
                 max_threads: int = 32,
                 user_agent: str = None):
        """
        Args:
            threads: Number of download threads (default 8)
            chunk_size_mb: Minimum chunk size in MB (default 10)
            speed_limit_bps: Speed limit in bytes/sec (0 = unlimited)
            ai_acceleration: Enable AI smart acceleration
            max_threads: Maximum threads for AI acceleration
            user_agent: Custom user agent string
        """
        self.initial_threads = threads or self.DEFAULT_THREADS
        self.chunk_size_mb = chunk_size_mb or self.DEFAULT_CHUNK_SIZE_MB
        self.speed_limiter = SpeedLimiter(speed_limit_bps)
        self.ai_enabled = ai_acceleration
        self.max_threads = max_threads
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        
        # Runtime state
        self._task: Optional[DownloadTask] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Not paused initially
        self._lock = threading.Lock()
        self._speed_tracker = _SpeedTracker()
        self._ai = AIAccelerator(
            min_threads=2,
            max_threads=max_threads,
            initial_threads=self.initial_threads
        ) if ai_acceleration else None
    
    def _create_session(self) -> requests.Session:
        """Create a requests session with retry logic."""
        session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5,
                      status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=self.max_threads + 5)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        session.headers['User-Agent'] = self.user_agent
        return session
    
    def probe(self, url: str, headers: Dict = None) -> Dict[str, Any]:
        """
        Probe a URL to get file info without downloading.
        
        Returns:
            Dict with: filename, total_size, content_type, supports_ranges,
                       etag, status_code
        """
        session = self._create_session()
        req_headers = dict(headers or {})
        
        # FTP URLs: probe via ftplib
        parsed = urlparse(url)
        if parsed.scheme == 'ftp':
            return self._probe_ftp(url, parsed)
        
        try:
            # HEAD request first (fast)
            resp = session.head(url, headers=req_headers, allow_redirects=True, timeout=15)
            
            # Some servers don't support HEAD — fallback to GET with Range: bytes=0-0
            if resp.status_code >= 400:
                req_headers['Range'] = 'bytes=0-0'
                resp = session.get(url, headers=req_headers, stream=True, 
                                  allow_redirects=True, timeout=15)
                resp.close()
            
            # Parse info
            total_size = int(resp.headers.get('Content-Length', 0))
            content_type = resp.headers.get('Content-Type', '')
            accept_ranges = resp.headers.get('Accept-Ranges', '').lower()
            content_range = resp.headers.get('Content-Range', '')
            etag = resp.headers.get('ETag', '')
            
            # Check range support
            supports_ranges = (
                accept_ranges == 'bytes' or
                resp.status_code == 206 or
                bool(content_range)
            )
            
            # If Content-Range is present, parse total size from it
            if content_range and 'bytes' in content_range:
                try:
                    total_str = content_range.split('/')[-1].strip()
                    if total_str != '*':
                        total_size = int(total_str)
                except (ValueError, IndexError):
                    pass
            
            # Extract filename
            filename = self._extract_filename(resp, url)
            
            result = {
                'filename': filename,
                'total_size': total_size,
                'content_type': content_type,
                'supports_ranges': supports_ranges,
                'etag': etag,
                'status_code': resp.status_code,
                'final_url': str(resp.url),
            }
            # ML: log platform probe response
            if _ML_AVAILABLE:
                _get_ml_logger().log_platform("probe", url, sc=resp.status_code,
                                              sz=total_size, ranges=supports_ranges,
                                              ct=content_type[:30] if content_type else '')
            return result
        except Exception as e:
            logger.error(f"Probe failed: {e}")
            return {
                'filename': self._filename_from_url(url),
                'total_size': 0,
                'content_type': '',
                'supports_ranges': False,
                'etag': '',
                'status_code': 0,
                'error': str(e),
            }
        finally:
            session.close()
    
    def download(self,
                 url: str,
                 output_dir: str = None,
                 filename: str = None,
                 headers: Dict = None,
                 progress_callback: Callable = None,
                 resume: bool = True) -> Dict[str, Any]:
        """
        Download a file with multi-threaded chunking.
        
        Args:
            url: URL to download
            output_dir: Output directory (default: files dir)
            filename: Override filename (auto-detected if None)
            headers: Extra HTTP headers
            progress_callback: Called with DownloadProgress object
            resume: Try to resume from previous state
        
        Returns:
            Dict with: status, filepath, filesize, filename, error
        """
        self._stop_event.clear()
        self._pause_event.set()
        
        output_dir = output_dir or _DEFAULT_FILES_DIR
        os.makedirs(output_dir, exist_ok=True)
        
        # Probe the file
        probe_info = self.probe(url, headers)
        if probe_info.get('error'):
            return {'status': 'failed', 'error': probe_info['error']}
        
        # Determine filename
        final_filename = filename or probe_info['filename']
        final_filename = sanitize_filename(final_filename)
        filepath = os.path.join(output_dir, final_filename)
        
        # Check for existing state file (resume)
        state_file = filepath + ".download"
        task = None
        if resume and os.path.exists(state_file):
            task = DownloadTask.load_state(state_file)
            if task and task.url == url and task.total_size == probe_info['total_size']:
                logger.info(f"Resuming download: {final_filename}")
                # Restore incomplete chunks to pending
                for chunk in task.chunks:
                    if chunk.status == 'downloading':
                        chunk.status = 'pending'
            else:
                task = None  # State doesn't match, start fresh
        
        if not task:
            # Create new task
            task = DownloadTask(
                url=url,
                filepath=filepath,
                total_size=probe_info['total_size'],
                supports_ranges=probe_info['supports_ranges'],
                filename=final_filename,
                content_type=probe_info.get('content_type', ''),
                headers=headers or {},
                created_at=time.time(),
                thread_count=self.initial_threads,
                etag=probe_info.get('etag', ''),
            )
            
            # Create chunks
            if probe_info['supports_ranges'] and probe_info['total_size'] > 0:
                task.chunks = self._create_chunks(
                    probe_info['total_size'], self.initial_threads
                )
            else:
                # Single chunk (no range support)
                task.chunks = [ChunkState(
                    index=0, start=0,
                    end=max(0, probe_info['total_size'] - 1)
                )]
        
        self._task = task
        
        # Pre-allocate file if we know the size and it's a new download
        if task.total_size > 0 and not os.path.exists(filepath):
            try:
                with open(filepath, 'wb') as f:
                    f.seek(task.total_size - 1)
                    f.write(b'\0')
            except Exception as e:
                logger.warning(f"Pre-allocation failed: {e}")
        
        # Download
        print(f"\n  {'═' * 55}")
        print(f"  File:     {final_filename[:50]}")
        if task.total_size > 0:
            print(f"  Size:     {format_size(task.total_size)}")
        print(f"  Threads:  {len(task.chunks)}"
              f"{'  (AI acceleration ON)' if self.ai_enabled else ''}")
        print(f"  Ranges:   {'Yes' if task.supports_ranges else 'No (single thread)'}")
        print(f"  {'═' * 55}\n")
        
        task.status = 'downloading'
        start_time = time.monotonic()
        
        try:
            if task.supports_ranges and len(task.chunks) > 1:
                self._download_chunked(task, progress_callback)
            elif urlparse(url).scheme == 'ftp':
                self._download_ftp(task, progress_callback)
            else:
                self._download_single(task, progress_callback)
            
            elapsed = time.monotonic() - start_time
            
            if self._stop_event.is_set():
                task.status = 'paused'
                task.save_state()
                return {
                    'status': 'paused', 'filepath': filepath,
                    'filesize': task.downloaded, 'filename': final_filename,
                }
            
            # Verify file size
            if os.path.exists(filepath):
                actual_size = os.path.getsize(filepath)
                if task.total_size > 0 and actual_size != task.total_size:
                    logger.warning(f"Size mismatch: expected {task.total_size}, "
                                 f"got {actual_size}")
            
            task.status = 'complete'
            task.cleanup_state()
            
            avg_speed = task.total_size / elapsed if elapsed > 0 else 0
            
            # ML: log chunk performance summary
            if _ML_AVAILABLE:
                chunk_speeds = [c.speed for c in task.chunks if c.speed > 0]
                _get_ml_logger().log_performance("chunk_perf", url,
                    ch=len(task.chunks),
                    th=self.threads,
                    sz=task.total_size,
                    dur=round(elapsed, 2),
                    avg_spd=int(avg_speed),
                    fast=int(max(chunk_speeds)) if chunk_speeds else 0,
                    slow=int(min(chunk_speeds)) if chunk_speeds else 0,
                    ai=self._ai_adjustment_count if hasattr(self, '_ai_adjustment_count') else 0,
                    ftp=urlparse(url).scheme == 'ftp')
            
            print(f"\n  ✓ Complete: {final_filename[:50]}")
            print(f"  ✓ Size: {format_size(task.total_size)}")
            print(f"  ✓ Time: {format_duration(elapsed)} "
                  f"({format_size(avg_speed)}/s avg)")
            
            return {
                'status': 'complete', 'filepath': filepath,
                'filesize': task.total_size, 'filename': final_filename,
                'elapsed': elapsed, 'avg_speed': avg_speed,
            }
        
        except Exception as e:
            task.status = 'failed'
            task.save_state()
            logger.error(f"Download failed: {e}")
            # ML: log download error
            if _ML_AVAILABLE:
                _get_ml_logger().log_error("dl_fail", url,
                    err=str(e)[:100],
                    sz_partial=task.downloaded)
            return {
                'status': 'failed', 'filepath': filepath,
                'error': str(e), 'filename': final_filename,
            }
    
    def pause(self):
        """Pause the current download."""
        self._pause_event.clear()
        if self._task:
            self._task.save_state()
            logger.info("Download paused")
    
    def resume_paused(self):
        """Resume a paused download."""
        self._pause_event.set()
        logger.info("Download resumed")
    
    def stop(self):
        """Stop the current download."""
        self._stop_event.set()
        self._pause_event.set()  # Unblock any paused threads
        if self._task:
            self._task.save_state()
    
    def edit_task(self, task: DownloadTask, new_url: str = None,
                  new_headers: Dict = None) -> bool:
        """
        Edit a paused task's URL or headers without losing chunk progress.
        
        Validates that the new resource matches (same Content-Length / ETag)
        so partial chunks remain valid.
        
        Returns True if the edit succeeded, False on validation failure.
        """
        if task.status not in ('paused', 'failed', 'pending'):
            logger.warning("Can only edit paused/failed/pending tasks")
            return False
        
        if new_url and new_url != task.url:
            # Validate new URL returns compatible resource
            try:
                session = self._create_session()
                resp = session.head(new_url, allow_redirects=True, timeout=15,
                                    headers=new_headers or task.headers or {})
                resp.raise_for_status()
                
                new_size = int(resp.headers.get('Content-Length', 0))
                new_etag = resp.headers.get('ETag', '')
                
                # Size must match if we have existing chunks
                if task.total_size > 0 and new_size > 0 and new_size != task.total_size:
                    logger.error(
                        f"Size mismatch: original={task.total_size}, "
                        f"new={new_size}. Chunk offsets would be invalid."
                    )
                    return False
                
                # ETag should match if both exist (resource identity)
                if task.etag and new_etag and task.etag != new_etag:
                    logger.warning(
                        f"ETag mismatch: original={task.etag}, new={new_etag}. "
                        f"Resource may have changed."
                    )
                    # Allow it but warn — user explicitly asked to edit
                
                task.url = new_url
                if new_etag:
                    task.etag = new_etag
            except Exception as e:
                logger.error(f"Failed to validate new URL: {e}")
                return False
        
        if new_headers is not None:
            task.headers = new_headers
        
        task.updated_at = time.time()
        task.save_state()
        logger.info(f"Task edited successfully: {task.filename}")
        return True
    
    # ================================================================
    # INTERNAL METHODS
    # ================================================================
    
    def _create_chunks(self, total_size: int, thread_count: int) -> List[ChunkState]:
        """Split file into chunks for parallel download."""
        min_chunk = self.chunk_size_mb * 1024 * 1024  # Minimum chunk size
        
        # Don't create more chunks than needed
        num_chunks = min(thread_count, max(1, total_size // min_chunk))
        num_chunks = max(1, num_chunks)
        
        chunk_size = total_size // num_chunks
        chunks = []
        
        for i in range(num_chunks):
            start = i * chunk_size
            end = start + chunk_size - 1 if i < num_chunks - 1 else total_size - 1
            chunks.append(ChunkState(index=i, start=start, end=end))
        
        return chunks
    
    def _download_chunked(self, task: DownloadTask, 
                          progress_callback: Callable = None):
        """Download file using multiple chunks in parallel."""
        
        num_workers = len([c for c in task.chunks if c.status != 'complete'])
        if num_workers == 0:
            return  # All chunks already complete
        
        last_state_save = time.monotonic()
        last_progress_update = time.monotonic()
        
        with ThreadPoolExecutor(max_workers=min(num_workers, self.max_threads)) as pool:
            # Submit all pending chunks
            futures: Dict[Future, ChunkState] = {}
            for chunk in task.chunks:
                if chunk.status in ('pending', 'failed'):
                    chunk.status = 'downloading'
                    future = pool.submit(self._download_chunk, task, chunk)
                    futures[future] = chunk
            
            # Monitor progress
            while futures:
                now = time.monotonic()
                
                # Check for stop
                if self._stop_event.is_set():
                    # Cancel remaining futures
                    for f in futures:
                        f.cancel()
                    break
                
                # Wait for pause
                self._pause_event.wait()
                
                # Update progress
                if now - last_progress_update >= 0.3:
                    self._update_progress(task, progress_callback)
                    last_progress_update = now
                
                # Save state periodically
                if now - last_state_save >= self.STATE_SAVE_INTERVAL:
                    task.save_state()
                    last_state_save = now
                
                # AI acceleration: check for thread adjustments
                if self._ai and self._ai.should_adjust():
                    total_speed = sum(c.speed for c in task.chunks)
                    self._ai.record_speed(total_speed)
                    suggested = self._ai.suggest_thread_count()
                    
                    # Work stealing
                    steal_ops = self._ai.find_steal_candidates(task.chunks)
                    for slow_idx, split_point in steal_ops:
                        # Create a new chunk from the split
                        slow_chunk = task.chunks[slow_idx]
                        if slow_chunk.status == 'downloading' and slow_chunk.remaining > 1024 * 1024:
                            new_chunk = ChunkState(
                                index=len(task.chunks),
                                start=split_point,
                                end=slow_chunk.end,
                            )
                            # Shrink the slow chunk
                            slow_chunk.end = split_point - 1
                            task.chunks.append(new_chunk)
                            new_chunk.status = 'downloading'
                            future = pool.submit(self._download_chunk, task, new_chunk)
                            futures[future] = new_chunk
                            logger.debug(f"Work stealing: split chunk {slow_idx} at {split_point}")
                
                # Check completed futures
                done = [f for f in futures if f.done()]
                for f in done:
                    chunk = futures.pop(f)
                    try:
                        f.result()
                    except Exception as e:
                        if chunk.retries < self.MAX_RETRIES:
                            chunk.retries += 1
                            chunk.status = 'pending'
                            logger.warning(f"Chunk {chunk.index} failed (retry {chunk.retries}): {e}")
                            time.sleep(self.RETRY_DELAY * chunk.retries)
                            chunk.status = 'downloading'
                            new_future = pool.submit(self._download_chunk, task, chunk)
                            futures[new_future] = chunk
                        else:
                            chunk.status = 'failed'
                            logger.error(f"Chunk {chunk.index} permanently failed: {e}")
                
                if not done:
                    time.sleep(0.1)
        
        # Final progress update
        self._update_progress(task, progress_callback)
    
    def _download_chunk(self, task: DownloadTask, chunk: ChunkState):
        """Download a single chunk."""
        session = self._create_session()
        
        try:
            headers = dict(task.headers)
            
            # Range header for resume
            range_start = chunk.current_offset
            range_end = chunk.end
            headers['Range'] = f'bytes={range_start}-{range_end}'
            
            resp = session.get(task.url, headers=headers, stream=True, timeout=30)
            resp.raise_for_status()
            
            # Open file and seek to chunk position
            with open(task.filepath, 'r+b') as f:
                f.seek(range_start)
                
                last_speed_calc = time.monotonic()
                speed_bytes = 0
                
                for data in resp.iter_content(chunk_size=self.BUFFER_SIZE):
                    # Check stop/pause
                    if self._stop_event.is_set():
                        return
                    self._pause_event.wait()
                    
                    # Speed limiting
                    sleep_time = self.speed_limiter.acquire(len(data))
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    
                    f.write(data)
                    chunk.downloaded += len(data)
                    speed_bytes += len(data)
                    
                    # Update chunk speed (every 0.5s)
                    now = time.monotonic()
                    elapsed = now - last_speed_calc
                    if elapsed >= 0.5:
                        chunk.speed = speed_bytes / elapsed
                        speed_bytes = 0
                        last_speed_calc = now
            
            chunk.status = 'complete'
        
        except Exception as e:
            chunk.speed = 0
            raise
        finally:
            session.close()
    
    def _download_single(self, task: DownloadTask,
                         progress_callback: Callable = None):
        """Download file in a single stream (no range support)."""
        session = self._create_session()
        chunk = task.chunks[0] if task.chunks else ChunkState(index=0, start=0, end=0)
        
        try:
            headers = dict(task.headers)
            resp = session.get(task.url, headers=headers, stream=True, timeout=30)
            resp.raise_for_status()
            
            # Update total size if we got it from the response
            content_length = resp.headers.get('Content-Length')
            if content_length:
                task.total_size = int(content_length)
                chunk.end = task.total_size - 1
            
            last_update = time.monotonic()
            last_speed_calc = time.monotonic()
            speed_bytes = 0
            
            with open(task.filepath, 'wb') as f:
                for data in resp.iter_content(chunk_size=self.BUFFER_SIZE):
                    if self._stop_event.is_set():
                        return
                    self._pause_event.wait()
                    
                    sleep_time = self.speed_limiter.acquire(len(data))
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    
                    f.write(data)
                    chunk.downloaded += len(data)
                    task.downloaded = chunk.downloaded
                    speed_bytes += len(data)
                    
                    now = time.monotonic()
                    elapsed = now - last_speed_calc
                    if elapsed >= 0.5:
                        chunk.speed = speed_bytes / elapsed
                        speed_bytes = 0
                        last_speed_calc = now
                    
                    if now - last_update >= 0.3:
                        self._update_progress(task, progress_callback)
                        last_update = now
            
            chunk.status = 'complete'
            if task.total_size == 0:
                task.total_size = chunk.downloaded
        
        finally:
            session.close()
    def _probe_ftp(self, url: str, parsed=None) -> Dict[str, Any]:
        """Probe an FTP URL for file info."""
        import ftplib
        if parsed is None:
            parsed = urlparse(url)
        host = parsed.hostname or ''
        port = parsed.port or 21
        user = parsed.username or 'anonymous'
        passwd = parsed.password or 'anonymous@'
        path = unquote(parsed.path)
        
        try:
            ftp = ftplib.FTP()
            ftp.connect(host, port, timeout=15)
            ftp.login(user, passwd)
            
            # Get file size
            total_size = 0
            try:
                total_size = ftp.size(path)
            except ftplib.error_perm:
                pass
            
            # Check REST (resume) support
            supports_resume = False
            try:
                ftp.sendcmd('REST 0')
                supports_resume = True
            except ftplib.error_perm:
                pass
            
            filename = os.path.basename(path) or 'ftp_download'
            ftp.quit()
            
            return {
                'filename': filename,
                'total_size': total_size or 0,
                'content_type': 'application/octet-stream',
                'supports_ranges': supports_resume,
                'etag': '',
                'status_code': 200,
                'final_url': url,
            }
        except Exception as e:
            logger.error(f"FTP probe failed: {e}")
            return {
                'filename': os.path.basename(path) if path else 'ftp_download',
                'total_size': 0,
                'content_type': '',
                'supports_ranges': False,
                'etag': '',
                'status_code': 0,
                'error': str(e),
            }
    
    def _download_ftp(self, task: DownloadTask,
                      progress_callback: Callable = None):
        """Download file via FTP with resume support (single-threaded)."""
        import ftplib
        parsed = urlparse(task.url)
        host = parsed.hostname or ''
        port = parsed.port or 21
        user = parsed.username or 'anonymous'
        passwd = parsed.password or 'anonymous@'
        path = unquote(parsed.path)
        
        chunk = task.chunks[0] if task.chunks else ChunkState(index=0, start=0, end=0)
        
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(user, passwd)
        ftp.voidcmd('TYPE I')  # Binary mode
        
        try:
            # Resume from where we left off
            start_offset = chunk.downloaded
            mode = 'ab' if start_offset > 0 else 'wb'
            
            if start_offset > 0:
                try:
                    ftp.sendcmd(f'REST {start_offset}')
                except ftplib.error_perm:
                    start_offset = 0
                    mode = 'wb'
            
            last_update = time.monotonic()
            last_speed_calc = time.monotonic()
            speed_bytes = 0
            
            with open(task.filepath, mode) as f:
                def write_callback(data: bytes):
                    nonlocal last_update, last_speed_calc, speed_bytes
                    
                    if self._stop_event.is_set():
                        ftp.abort()
                        return
                    self._pause_event.wait()
                    
                    sleep_time = self.speed_limiter.acquire(len(data))
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    
                    f.write(data)
                    chunk.downloaded += len(data)
                    task.downloaded = chunk.downloaded
                    speed_bytes += len(data)
                    
                    now = time.monotonic()
                    elapsed = now - last_speed_calc
                    if elapsed >= 0.5:
                        chunk.speed = speed_bytes / elapsed
                        speed_bytes = 0
                        last_speed_calc = now
                    
                    if now - last_update >= 0.3:
                        self._update_progress(task, progress_callback)
                        last_update = now
                
                ftp.retrbinary(f'RETR {path}', write_callback,
                               blocksize=self.BUFFER_SIZE)
            
            chunk.status = 'complete'
            if task.total_size == 0:
                task.total_size = chunk.downloaded
        
        except ftplib.error_temp:
            if not self._stop_event.is_set():
                raise
        finally:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass
    
    def _update_progress(self, task: DownloadTask,
                         callback: Callable = None):
        """Update and display download progress."""
        downloaded = sum(c.downloaded for c in task.chunks)
        total_speed = sum(c.speed for c in task.chunks if c.status == 'downloading')
        active = sum(1 for c in task.chunks if c.status == 'downloading')
        
        task.downloaded = downloaded
        
        if task.total_size > 0:
            percent = downloaded / task.total_size * 100
            remaining = task.total_size - downloaded
            eta = remaining / total_speed if total_speed > 0 else 0
        else:
            percent = 0
            eta = 0
        
        # AI: record speed for tuning
        if self._ai:
            self._ai.record_speed(total_speed)
        
        # Terminal progress bar
        if task.total_size > 0:
            bar_len = 35
            filled = int(bar_len * percent / 100)
            bar = '█' * filled + '░' * (bar_len - filled)
            speed_str = format_size(total_speed) + "/s" if total_speed else "---"
            eta_str = format_duration(eta) if eta and eta < 86400 else "--:--"
            
            threads_str = f"T:{active}" if active > 1 else ""
            ai_str = "⚡" if self._ai and self._ai._enabled else ""
            
            print(f"\r  [{bar}] {percent:5.1f}% | "
                  f"{format_size(downloaded)}/{format_size(task.total_size)} | "
                  f"{speed_str} | ETA: {eta_str} {threads_str}{ai_str}  ",
                  end='', flush=True)
        else:
            speed_str = format_size(total_speed) + "/s" if total_speed else "---"
            print(f"\r  Downloaded: {format_size(downloaded)} | {speed_str}  ",
                  end='', flush=True)
        
        # Callback
        if callback:
            progress = DownloadProgress(
                total_size=task.total_size,
                downloaded=downloaded,
                speed=total_speed,
                eta=eta,
                percent=percent,
                active_threads=active,
                status=task.status,
                filename=task.filename,
            )
            try:
                callback(progress)
            except Exception:
                pass
    
    def _extract_filename(self, response, url: str) -> str:
        """Extract filename from response headers or URL."""
        # Try Content-Disposition header
        cd = response.headers.get('Content-Disposition', '')
        if cd:
            import re
            # filename*=UTF-8''encoded_name
            match = re.search(r"filename\*\s*=\s*UTF-8''(.+?)(?:;|$)", cd, re.IGNORECASE)
            if match:
                return unquote(match.group(1).strip())
            # filename="name" or filename=name
            match = re.search(r'filename\s*=\s*"?([^";]+)', cd)
            if match:
                return match.group(1).strip().strip('"')
        
        return self._filename_from_url(url)
    
    def _filename_from_url(self, url: str) -> str:
        """Extract filename from URL path."""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        filename = os.path.basename(path)
        
        if not filename or filename == '/':
            # Generate from URL hash
            h = hashlib.md5(url.encode()).hexdigest()[:8]
            filename = f"download_{h}"
        
        return filename


# ============================================================================
# SPEED TRACKER (internal)
# ============================================================================

class _SpeedTracker:
    """Track download speed over time."""
    
    def __init__(self, window: float = 3.0):
        self._window = window
        self._samples: List[Tuple[float, int]] = []
        self._lock = threading.Lock()
    
    def record(self, nbytes: int):
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, nbytes))
            cutoff = now - self._window
            self._samples = [(t, b) for t, b in self._samples if t > cutoff]
    
    def speed(self) -> float:
        """Current speed in bytes/sec."""
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            total_bytes = sum(b for _, b in self._samples)
            elapsed = self._samples[-1][0] - self._samples[0][0]
            return total_bytes / elapsed if elapsed > 0 else 0.0


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def quick_download(url: str, output_dir: str = None, threads: int = 8) -> Dict:
    """Quick download with defaults."""
    dl = ChunkedDownloader(threads=threads)
    return dl.download(url, output_dir=output_dir)


def probe_url(url: str) -> Dict:
    """Probe a URL without downloading."""
    dl = ChunkedDownloader()
    return dl.probe(url)


# ============================================================================
# CLI INTERFACE (standalone usage)
# ============================================================================

if __name__ == "__main__":
    url = input("URL: ").strip()
    if not url:
        print("No URL provided")
        sys.exit(1)
    
    threads = input("Threads (8): ").strip()
    threads = int(threads) if threads.isdigit() else 8
    
    dl = ChunkedDownloader(threads=threads, ai_acceleration=True)
    
    # Probe first
    print("\nProbing URL...")
    info = dl.probe(url)
    print(f"  Filename:  {info['filename']}")
    print(f"  Size:      {format_size(info['total_size'])}")
    print(f"  Ranges:    {'Yes' if info['supports_ranges'] else 'No'}")
    print(f"  Type:      {info.get('content_type', 'unknown')}")
    
    confirm = input("\nDownload? (y/n) [y]: ").strip().lower()
    if confirm == 'n':
        print("Cancelled.")
        sys.exit(0)
    
    result = dl.download(url)
    
    if result['status'] == 'complete':
        print(f"\n✓ Saved to: {result['filepath']}")
    else:
        print(f"\n✗ Failed: {result.get('error', 'Unknown error')}")
