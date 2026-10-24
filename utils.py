import os
import sys
import re
import shutil
import subprocess
import time
import logging
import socket
import threading
from typing import Optional, List, Tuple, Union, Callable
from urllib.parse import urlparse

# ============================================================================
# CONSTANTS
# ============================================================================

INVALID_FILENAME_CHARS = '<>:"/\\|?*'
INVALID_FILENAME_CHARS_REGEX = re.compile(r'[<>:"/\\|?*]')
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE
)

SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

# ============================================================================
# LOGGING UTILITIES
# ============================================================================

class LoggerSetup:
    """Setup and manage logging"""
    
    _logger = None
    
    @classmethod
    def get_logger(cls, name: str = "downloader", 
                   log_file: str = None,
                   level: str = "INFO",
                   log_to_file: bool = True,
                   log_to_console: bool = False) -> logging.Logger:
        """Get or create logger instance"""
        if cls._logger is not None:
            return cls._logger
        
        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.handlers = []
        
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(funcName)s:%(lineno)d | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        if log_to_file and log_file:
            try:
                log_dir = os.path.dirname(log_file)
                if log_dir:
                    os.makedirs(log_dir, exist_ok=True)
                file_handler = logging.FileHandler(log_file, encoding='utf-8')
                file_handler.setLevel(logging.DEBUG)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except Exception as e:
                print(f"Warning: Could not create log file: {e}")
        
        if log_to_console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
            logger.addHandler(console_handler)
        
        cls._logger = logger
        return logger


def get_logger() -> logging.Logger:
    """Get the default logger"""
    return LoggerSetup.get_logger()


# ============================================================================
# STRING UTILITIES
# ============================================================================

def sanitize_filename(filename: str, 
                      max_length: int = 200,
                      replace_char: str = '_',
                      remove_emojis: bool = True,
                      ascii_only: bool = True) -> str:
    """Sanitize a string for use as a filename."""
    if not filename:
        return 'untitled'
    
    result = INVALID_FILENAME_CHARS_REGEX.sub(replace_char, filename)
    result = ''.join(char for char in result if ord(char) >= 32)
    
    if remove_emojis:
        result = EMOJI_PATTERN.sub('', result)
    
    if ascii_only:
        result = result.encode('ascii', 'ignore').decode('ascii')
    
    result = re.sub(r'[_\s]+', ' ', result)
    result = result.strip(' .')
    
    reserved_names = {'CON', 'PRN', 'AUX', 'NUL', 
                      'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
                      'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}
    name_upper = result.upper().split('.')[0]
    if name_upper in reserved_names:
        result = f"_{result}"
    
    if len(result) > max_length:
        name, ext = os.path.splitext(result)
        max_name_len = max_length - len(ext)
        result = name[:max_name_len].rstrip() + ext
    
    if not result or result == '.':
        result = 'untitled'
    
    return result


# ============================================================================
# SIZE & TIME FORMATTING
# ============================================================================

def format_size(size_bytes: Union[int, float], precision: int = 2) -> str:
    """Format bytes to human-readable size."""
    if size_bytes is None or size_bytes < 0:
        return "Unknown"
    
    if size_bytes == 0:
        return "0 B"
    
    size = float(size_bytes)
    for unit in SIZE_UNITS:
        if size < 1024:
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    
    return f"{size:.{precision}f} PB"


def format_duration(seconds: Union[int, float], 
                    compact: bool = False,
                    include_ms: bool = False) -> str:
    """Format seconds to duration string."""
    if seconds is None or seconds < 0:
        return "00:00" if not compact else "0s"
    
    total_seconds = int(seconds)
    ms = int((seconds - total_seconds) * 1000) if include_ms else 0
    
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    
    if compact:
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if secs > 0 or not parts:
            parts.append(f"{secs}s")
        if include_ms and ms > 0:
            parts.append(f"{ms}ms")
        return ' '.join(parts)
    else:
        if hours > 0:
            result = f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            result = f"{minutes:02d}:{secs:02d}"
        
        if include_ms:
            result += f".{ms:03d}"
        
        return result


# ============================================================================
# FILE SYSTEM & SYSTEM UTILITIES
# ============================================================================

def ensure_directory(path: str) -> bool:
    """Ensure a directory exists, create if necessary"""
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception:
        return False


def get_free_space(path: str) -> Tuple[int, int, int]:
    """Get disk space info for a path. Returns (total, used, free) in bytes."""
    try:
        total, used, free = shutil.disk_usage(path)
        return total, used, free
    except Exception:
        return 0, 0, 0


def get_free_space_mb(path: str) -> int:
    """Get free disk space in MB"""
    _, _, free = get_free_space(path)
    return free // (1024 * 1024)


def find_file(directory: str, 
              name_contains: str = None,
              extensions: List[str] = None,
              newest_first: bool = True) -> Optional[str]:
    """Find a file in a directory."""
    if not os.path.exists(directory):
        return None
    
    files = []
    for f in os.listdir(directory):
        filepath = os.path.join(directory, f)
        if not os.path.isfile(filepath):
            continue
        
        if extensions:
            _, ext = os.path.splitext(f)
            if ext.lower() not in [e.lower() for e in extensions]:
                continue
        
        if name_contains:
            if name_contains.lower() not in f.lower():
                continue
        
        files.append((filepath, os.path.getmtime(filepath)))
    
    if not files:
        return None
    
    files.sort(key=lambda x: x[1], reverse=newest_first)
    return files[0][0]


def check_ffmpeg() -> bool:
    """Check if FFmpeg is available"""
    try:
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            startupinfo=startupinfo
        )
        return result.returncode == 0
    except Exception:
        return False


def clear_screen():
    """Clear the console screen"""
    os.system('cls' if os.name == 'nt' else 'clear')


def is_valid_url(url: str) -> bool:
    """Check if a string is a valid URL"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False


# ============================================================================
# PROGRESS UTILITIES
# ============================================================================

class ProgressTracker:
    """Track download/operation progress"""
    
    def __init__(self, total: int = 0, description: str = ""):
        self.total = total
        self.current = 0
        self.description = description
        self.start_time = time.time()
        self.last_update = 0
        self.update_interval = 0.5
    
    def update(self, amount: int = 1) -> None:
        self.current += amount
    
    def set_progress(self, current: int) -> None:
        self.current = current
    
    @property
    def percentage(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(100.0, (self.current / self.total) * 100)
    
    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time
    
    @property
    def speed(self) -> float:
        elapsed = self.elapsed
        if elapsed <= 0:
            return 0.0
        return self.current / elapsed
    
    @property
    def eta(self) -> float:
        if self.speed <= 0 or self.current >= self.total:
            return 0.0
        remaining = self.total - self.current
        return remaining / self.speed
    
    def get_progress_bar(self, width: int = 40) -> str:
        filled = int(width * self.percentage / 100)
        bar = '█' * filled + '░' * (width - filled)
        return f"[{bar}] {self.percentage:.1f}%"
    
    def should_update_display(self) -> bool:
        now = time.time()
        if now - self.last_update >= self.update_interval:
            self.last_update = now
            return True
        return False
    
    def get_status_line(self) -> str:
        bar = self.get_progress_bar()
        speed_str = format_size(self.speed) + "/s" if self.speed > 0 else "---"
        eta_str = format_duration(self.eta, compact=True) if self.eta > 0 else "--"
        return f"{bar} | {speed_str} | ETA: {eta_str}"


# ============================================================================
# NETWORK RESILIENCE & DISCONNECT WATCHDOG
# ============================================================================

def is_internet_available(
    hosts: Tuple[Tuple[str, int], ...] = (
        ("1.1.1.1", 53),
        ("8.8.8.8", 53),
        ("1.0.0.1", 53),
        ("8.8.4.4", 53),
    ),
    timeout: float = 1.5,
) -> bool:
    """
    Fast socket-level probe to check internet connectivity.
    Probes public Anycast DNS servers (Cloudflare / Google) on port 53 directly by IP,
    bypassing OS DNS resolution so it won't hang when local DNS is dead.
    """
    for host, port in hosts:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            s.close()
            return True
        except (socket.timeout, OSError):
            continue
    return False


def is_network_error(exc: Optional[BaseException]) -> bool:
    """
    Determine if an exception is caused by an internet cut, socket timeout,
    connection reset, DNS lookup failure, or CDN reachability drop.
    """
    if exc is None:
        return False

    from urllib.error import URLError
    network_exc_types = (
        ConnectionError, ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError,
        TimeoutError, socket.timeout, socket.gaierror, socket.herror, URLError
    )
    if isinstance(exc, network_exc_types):
        return True

    err_str = str(exc).lower()
    net_keywords = (
        "connection", "network", "offline", "socket", "timeout", "timed out",
        "name resolution", "getaddrinfo", "econnreset", "econnrefused", "enotfound",
        "winerror 10054", "winerror 10053", "winerror 10060", "winerror 10061", "winerror 10065",
        "temporary failure in name resolution", "nodename nor servname provided",
        "remote end closed connection", "curl: (28)", "curl: (7)", "curl: (6)",
        "curl: (55)", "curl: (56)", "tls connect error", "broken pipe",
        "max retries exceeded", "failed to establish a new connection",
        "read timed out", "connection reset", "network is unreachable",
        "host is unreachable", "unable to connect", "service unavailable",
    )
    return any(kw in err_str for kw in net_keywords)


def safe_print(text: str, end: str = "\n", flush: bool = True):
    """Safely print text handling Windows cp1252 / legacy console encoding."""
    try:
        print(text, end=end, flush=flush)
    except (UnicodeEncodeError, OSError):
        try:
            encoding = getattr(sys.stdout, 'encoding', None) or 'ascii'
            clean_text = text.encode(encoding, errors='replace').decode(encoding, errors='replace')
            print(clean_text, end=end, flush=flush)
        except Exception:
            pass


def wait_for_internet_connection(
    status_callback: Optional[Callable[[str], None]] = None,
    check_interval: float = 2.5,
    stop_event: Optional[threading.Event] = None,
    max_wait_seconds: Optional[int] = None,
    task_name: str = "",
) -> bool:
    """
    Monitors internet connectivity. If disconnected, pauses execution,
    periodically notifies the user/UI, and blocks until connection is restored.
    
    Returns:
        True if connection is online/restored, False if cancelled or max_wait expired.
    """
    if is_internet_available():
        return True

    task_desc = f" ({task_name})" if task_name else ""
    offline_msg = f"[⚠️ OFFLINE] Internet connection lost! Download paused{task_desc}. Waiting to reconnect..."

    logger = get_logger()
    logger.warning(offline_msg)

    if status_callback:
        try:
            status_callback(offline_msg)
        except Exception:
            pass
    else:
        safe_print(f"\n  \033[93m{offline_msg}\033[0m", flush=True)

    start_wait = time.time()
    spinner = ["|", "/", "-", "\\"]
    idx = 0

    while True:
        if stop_event and stop_event.is_set():
            logger.info("Download cancelled by user while waiting for internet reconnection.")
            return False

        if max_wait_seconds and (time.time() - start_wait) > max_wait_seconds:
            logger.error(f"Internet connection did not recover within {max_wait_seconds}s limit.")
            return False

        # Sleep in small increments for prompt cancellation responsiveness
        for _ in range(int(check_interval * 10)):
            if stop_event and stop_event.is_set():
                return False
            time.sleep(0.1)

        idx += 1
        elapsed = int(time.time() - start_wait)
        spin = spinner[idx % len(spinner)]

        if is_internet_available():
            break

        # Periodic status ping
        waiting_status = f"\r  \033[93m[⚠️ OFFLINE]\033[0m Waiting for internet connection... {spin} ({elapsed}s elapsed)"
        if status_callback:
            try:
                status_callback(waiting_status)
            except Exception:
                pass
        else:
            safe_print(waiting_status, end="", flush=True)

    online_msg = f"\n  \033[92m[🌐 ONLINE] Internet connection restored! Resuming download{task_desc}...\033[0m\n"
    logger.info(f"Internet restored after {int(time.time() - start_wait)}s. Resuming download.")

    if status_callback:
        try:
            status_callback(online_msg)
        except Exception:
            pass
    else:
        safe_print(online_msg, flush=True)

    return True


if __name__ == "__main__":
    print("Utils Module - Testing")
    print("=" * 50)
    print("Sanitize filename:", sanitize_filename("🎵 Music Video: Special Edition? 🎶.mp4"))
    print("Format size:", format_size(1073741824))
    print("Format duration:", format_duration(3665))
    print("FFmpeg available:", check_ffmpeg())
    print("Done!")
