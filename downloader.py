#!/usr/bin/env python3

import os
import shutil
import sys
import time
import json
import threading
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Dict, List, Any, Tuple, Union, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================================
# CLIPBOARD HELPER (built-in tkinter, no extra deps)
# ============================================================================

def _get_clipboard_url() -> Optional[str]:
    """Try to read a URL from the system clipboard."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        text = root.clipboard_get().strip()
        root.destroy()
        if text and ('://' in text or text.startswith('www.')):
            # Quick sanity check: looks like a URL
            if any(d in text.lower() for d in [
                'youtube.', 'youtu.be', 'instagram.', 'tiktok.', 'twitter.',
                'x.com', 'facebook.', 'reddit.', 'vimeo.', 'dailymotion.',
                'soundcloud.', 'twitch.', 'bilibili.',
            ]) or text.startswith(('http://', 'https://')):
                return text
    except Exception:
        pass
    return None


# ============================================================================
# DESKTOP NOTIFICATION HELPER (Windows)
# ============================================================================

def _send_notification(title: str, message: str, filepath: str = None):
    """Send a Windows toast notification with optional click-to-open-folder. Best-effort, never raises."""
    try:
        import subprocess
        # Build the launch action XML if filepath is provided
        launch_attr = ''
        if filepath:
            folder = os.path.dirname(filepath).replace("'", "''")
            launch_attr = f" activationType='protocol' launch='file:///{folder}'"
        
        # Escape XML-sensitive characters in title and message
        safe_title = str(title).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        safe_msg = str(message).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        
        ps_script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$template = @"<toast{launch_attr}><visual><binding template='ToastText02'><text id='1'>{safe_title}</text><text id='2'>{safe_msg}</text></binding></visual></toast>"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Media Downloader').Show($toast)
'''
        subprocess.Popen(
            ['powershell', '-NoProfile', '-Command', ps_script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000  # CREATE_NO_WINDOW
        )
    except Exception:
        pass

# Ensure stdout/stderr can handle Unicode output safely on Windows consoles
def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_configure_stdio()

# ============================================================================
# DEPENDENCY CHECKS
# ============================================================================

# Check yt-dlp
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
    YTDLP_VERSION = getattr(yt_dlp.version, '__version__', 'unknown')
except ImportError:
    YTDLP_AVAILABLE = False
    YTDLP_VERSION = None
    print("[ERROR] yt-dlp not found. Install with: pip install yt-dlp")

# ============================================================================
# IMPORT LOCAL MODULES
# ============================================================================

from config import Config
CONFIG_AVAILABLE = True

from database import DatabaseManager, DownloadRecord, DownloadStatus
DATABASE_AVAILABLE = True

from platforms import (
    Platform, detect_platform, get_platform_info,
    extract_video_id, is_playlist_url, detect_content_type,
    ContentType, normalize_url, is_supported_url
)
PLATFORMS_AVAILABLE = True

from quality_manager import (
    QualityManager, QualitySelector, QualityPreset,
    VideoResolution, AudioCodec, AudioBitrate
)
QUALITY_AVAILABLE = True

from utils import (
    sanitize_filename, format_size, format_duration,
    check_ffmpeg, get_free_space_mb, find_file,
    clear_screen, get_logger, ensure_directory,
    ProgressTracker, is_valid_url
)
UTILS_AVAILABLE = True

try:
    from chunked_downloader import ChunkedDownloader, probe_url, quick_download
    CHUNKED_AVAILABLE = True
except ImportError:
    CHUNKED_AVAILABLE = False

try:
    from stream_recorder import M3U8Downloader, M3U8Parser, is_m3u8_url, is_stream_url
    STREAM_AVAILABLE = True
except ImportError:
    STREAM_AVAILABLE = False

try:
    from github_releases import GitHubReleaseParser, download_github_release
    GITHUB_AVAILABLE = True
except ImportError:
    GITHUB_AVAILABLE = False

try:
    from ml_logger import get_ml_logger, url_hash
    ML_LOGGER_AVAILABLE = True
except ImportError:
    ML_LOGGER_AVAILABLE = False

try:
    from subtitle_ai import (
        detect_user_language, recommend_languages,
        get_recommendation_label, SubtitleCleaner, clean_subtitle_file
    )
    SUBTITLE_AI_AVAILABLE = True
except ImportError:
    SUBTITLE_AI_AVAILABLE = False

try:
    from spotiflac import (
        SpotiFLACDownloader, SpotifyMetadataResolver,
        SpotifyTrack, SpotifyCollection
    )
    SPOTIFLAC_AVAILABLE = True
except ImportError:
    SPOTIFLAC_AVAILABLE = False

try:
    from search_engine import MediaSearchEngine, SearchResultItem
    SEARCH_ENGINE_AVAILABLE = True
except ImportError:
    SEARCH_ENGINE_AVAILABLE = False

CDM_AVAILABLE = False

try:
    from universal_downloader import (
        UniversalMediaDownloader, WebpageStreamSniffer, StreamCandidate
    )
    UNIVERSAL_AVAILABLE = True
except ImportError:
    UNIVERSAL_AVAILABLE = False


# ============================================================================
# CONSTANTS
# ============================================================================

VERSION = "1.0.0"
APP_NAME = "Personal Media Downloader"

# ============================================================================
# LOGGER SETUP
# ============================================================================

logger = get_logger()

# ============================================================================
# CUSTOM EXCEPTIONS
# ============================================================================

class DownloadError(Exception):
    """Base download error"""
    pass

class NetworkError(DownloadError):
    """Network connectivity issues"""
    pass

class PrivateContentError(DownloadError):
    """Content is private"""
    pass

class GeoBlockedError(DownloadError):
    """Content not available in region"""
    pass

class AgeRestrictedError(DownloadError):
    """Age verification required"""
    pass

class RateLimitError(DownloadError):
    """Too many requests"""
    pass

# ============================================================================
# PROGRESS HOOK
# ============================================================================

class DownloadProgressHook:
    """Progress hook for yt-dlp"""
    
    def __init__(self):
        self.reset()
    
    def reset(self):
        self.downloaded = 0
        self.total = 0
        self.speed = 0
        self.eta = 0
        self.filename = ""
        self.status = ""
        self.last_update = 0
        self.update_interval = 0.3
    
    def __call__(self, d: Dict):
        status = d.get('status', '')
        
        if status == 'downloading':
            now = time.time()
            if now - self.last_update < self.update_interval:
                return
            self.last_update = now
            
            self.downloaded = d.get('downloaded_bytes', 0)
            self.total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            self.speed = d.get('speed', 0) or 0
            self.eta = d.get('eta', 0) or 0
            self.filename = d.get('filename', '')
            self.status = 'downloading'
            
            if self.total > 0:
                pct = (self.downloaded / self.total) * 100
                bar_len = 40
                filled = int(bar_len * pct / 100)
                bar = '█' * filled + '░' * (bar_len - filled)
                
                speed_str = format_size(self.speed) + "/s" if self.speed else "---"
                eta_str = format_duration(self.eta) if self.eta else "--:--"
                dl_str = format_size(self.downloaded)
                total_str = format_size(self.total)
                
                print(f"\r  [{bar}] {pct:5.1f}% | {dl_str}/{total_str} | {speed_str} | ETA: {eta_str}  ", end='', flush=True)
        
        elif status == 'finished':
            self.status = 'finished'
            fname = os.path.basename(d.get('filename', ''))
            print(f"\n  ✓ Downloaded: {fname[:60]}")
        
        elif status == 'error':
            self.status = 'error'
            print("\n  ✗ Download error!")


class _SilentYtdlpLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


class _ArchiveLogger:
    """Logger that detects archive-skip messages."""
    def __init__(self):
        self.skipped = False
    def debug(self, msg):
        if 'has already been recorded' in msg or 'already been downloaded' in msg:
            self.skipped = True
    def warning(self, msg):
        pass
    def error(self, msg):
        # Show actual download errors
        if msg and 'cookie' not in msg.lower() and 'dpapi' not in msg.lower():
            print(f"\n  ⚠ {msg[:100]}")


import contextlib

@contextlib.contextmanager
def _suppress_stderr():
    """Temporarily redirect stderr to devnull to silence yt-dlp's
    internal cookie/DPAPI error messages that bypass the logger."""
    old_stderr = sys.stderr
    try:
        sys.stderr = open(os.devnull, 'w')
        yield
    finally:
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.stderr = old_stderr


def _try_with_cookies(opts: Dict, url: str, *, download: bool = False,
                      process: bool = True,
                      browsers: list = None) -> Optional[Dict]:
    """Try extracting/downloading with browser cookies, silently.
    Falls back to direct (no cookies) if all browser attempts fail.
    Returns info dict on success, None on total failure."""
    if browsers is None:
        browsers = ['chrome', 'firefox', 'edge']
    
    # Strategy 1: Try each browser's cookies (silenced)
    for browser in browsers:
        try:
            cookie_opts = opts.copy()
            if 'postprocessors' in opts:
                cookie_opts['postprocessors'] = opts['postprocessors'].copy()
            cookie_opts['cookiesfrombrowser'] = (browser,)
            cookie_opts['logger'] = _SilentYtdlpLogger()
            if not download:
                cookie_opts.pop('download_archive', None)
            with _suppress_stderr():
                with yt_dlp.YoutubeDL(cookie_opts) as ydl:
                    result = ydl.extract_info(url, download=download, process=process)
                    if result:
                        return result
        except Exception:
            continue
    
    # Strategy 2: Direct (no cookies)
    try:
        direct_opts = opts.copy()
        if 'postprocessors' in opts:
            direct_opts['postprocessors'] = opts['postprocessors'].copy()
        direct_opts['logger'] = _SilentYtdlpLogger()
        if not download:
            direct_opts.pop('download_archive', None)
        with yt_dlp.YoutubeDL(direct_opts) as ydl:
            return ydl.extract_info(url, download=download, process=process)
    except Exception:
        return None


def _get_working_opts_with_cookies(base_opts: Dict, url: str,
                                    browsers: list = None) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Try browser cookies silently. Returns (info, working_opts) tuple.
    The working_opts will have the correct cookiesfrombrowser set if needed."""
    if browsers is None:
        browsers = ['chrome', 'firefox', 'edge']
    
    _ml = get_ml_logger() if ML_LOGGER_AVAILABLE else None
    
    # Try each browser's cookies
    for browser in browsers:
        t0 = time.time()
        try:
            opts = base_opts.copy()
            if 'postprocessors' in base_opts:
                opts['postprocessors'] = base_opts['postprocessors'].copy()
            opts['cookiesfrombrowser'] = (browser,)
            opts['logger'] = _SilentYtdlpLogger()
            probe_opts = opts.copy()
            probe_opts.pop('download_archive', None)
            with _suppress_stderr():
                with yt_dlp.YoutubeDL(probe_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info:
                        if _ml:
                            _ml.log_security("cookie_try", br=browser, ok=True,
                                             ms=int((time.time() - t0) * 1000))
                        return info, opts
        except Exception:
            if _ml:
                _ml.log_security("cookie_try", br=browser, ok=False,
                                 ms=int((time.time() - t0) * 1000))
            continue
    
    # Direct (no cookies)
    t0 = time.time()
    try:
        opts = base_opts.copy()
        if 'postprocessors' in base_opts:
            opts['postprocessors'] = base_opts['postprocessors'].copy()
        opts['logger'] = _SilentYtdlpLogger()
        probe_opts = opts.copy()
        probe_opts.pop('download_archive', None)
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                if _ml:
                    _ml.log_security("cookie_try", br="direct", ok=True,
                                     ms=int((time.time() - t0) * 1000))
                return info, opts
    except Exception:
        if _ml:
            _ml.log_security("cookie_try", br="direct", ok=False,
                             ms=int((time.time() - t0) * 1000))
    
    return None, base_opts

def _clean_error_msg(err_str: str) -> str:
    """Extract a concise human-readable error status from yt-dlp error string."""
    if not err_str:
        return "Failed"
    s = str(err_str).lower()
    if "requested format is not available" in s or "no video formats" in s:
        return "Format unavail"
    if "private video" in s:
        return "Private video"
    if "video unavailable" in s or "is not available" in s:
        return "Video unavail"
    if "members-only" in s:
        return "Members only"
    if "copyright" in s:
        return "Copyright block"
    if "geo" in s and "block" in s:
        return "Geo-blocked"
    if "http error 404" in s or "not found" in s:
        return "HTTP 404"
    if "http error 403" in s or "forbidden" in s:
        return "HTTP 403"
    if "cookie" in s:
        return "Cookie error"
    
    # Strip raw prefixes like "ERROR: [youtube] <id>: "
    clean = re.sub(r'^(?:ERROR:\s*)?(?:\[[^\]]+\]\s*)?(?:[a-zA-Z0-9_-]{11}:\s*)?', '', str(err_str)).strip()
    return clean[:24] if clean else "Error"


def _print_playlist_line(idx: int, total: int, icon: str, title: str,
                         status: str, elapsed: float, end: str = '\n'):
    """Print a compact single-line playlist item status."""
    w = len(str(total))
    safe_title = str(title or 'Unknown')
    title_trunc = safe_title[:42]
    # Elapsed formatting
    m, s = int(elapsed // 60), int(elapsed % 60)
    h = int(m // 60)
    if h:
        time_str = f"{h}h{m%60:02d}m"
    elif m:
        time_str = f"{m}m{s:02d}s"
    else:
        time_str = f"{s}s"
    line_str = f"  [{idx:>{w}}/{total}] {icon} {title_trunc:<42} {status:<16} {time_str:>7}".ljust(80)
    print(f"\r{line_str}", end=end, flush=True)


# ============================================================================
# MEDIA DOWNLOADER CLASS
# ============================================================================

class MediaDownloader:
    """Main media downloader class"""
    
    def __init__(self):
        self.db = DatabaseManager(Config.DATABASE_PATH) if DATABASE_AVAILABLE else DatabaseManager()
        self.quality = QualityManager() if QUALITY_AVAILABLE else QualityManager()
        self.progress = DownloadProgressHook()
        self.js_runtimes = self._detect_js_runtimes()
        self._init_directories()
        # Bootstrap ML logger
        if ML_LOGGER_AVAILABLE:
            self.ml = get_ml_logger(
                filepath=Config.ML_LOG_PATH if CONFIG_AVAILABLE else None,
                enabled=Config.ML_LOG_ENABLED if CONFIG_AVAILABLE else True,
            )
        else:
            self.ml = None
    
    def _init_directories(self):
        """Initialize all directories"""
        Config.init_directories()
    
    def _get_output_dir(self, platform: Platform, media_type: str = "video") -> str:
        """Get output directory for downloads"""
        if media_type == "audio":
            base = Config.AUDIO_DIR
        elif media_type == "thumbnail":
            base = Config.THUMBNAIL_DIR
        elif media_type == "subtitle":
            base = Config.SUBTITLE_DIR
        else:
            base = Config.VIDEO_DIR
        
        if Config.ORGANIZE_BY_PLATFORM:
            out_dir = os.path.join(base, platform.value)
        else:
            out_dir = base
        
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _detect_js_runtimes(self) -> Dict[str, Dict[str, str]]:
        """Detect available JS runtimes for yt-dlp EJS challenges"""
        runtimes = {}
        runtime_execs = {
            "node": "node",
            "deno": "deno",
            "bun": "bun",
            "quickjs": "qjs",
        }
        for runtime, exe in runtime_execs.items():
            path = shutil.which(exe)
            if path:
                runtimes[runtime] = {"path": path}
        return runtimes

    def _apply_js_runtimes(self, opts: Dict) -> None:
        """Attach JS runtimes to yt-dlp options if available"""
        if self.js_runtimes:
            opts["js_runtimes"] = self.js_runtimes

    def _select_video_format_id(self, info: Dict, max_height: Optional[int]) -> Tuple[Optional[str], Optional[int]]:
        """Pick a concrete format id (or video+audio ids) matching max_height"""
        formats = info.get("formats") or []
        if not formats:
            return None, None

        def within_height(f: Dict) -> bool:
            if not max_height:
                return True
            height = f.get("height") or 0
            return height <= max_height

        def video_score(f: Dict) -> Tuple[int, int, float, int]:
            height = f.get("height") or 0
            fps = f.get("fps") or 0
            tbr = f.get("tbr") or 0
            ext = (f.get("ext") or "").lower()
            return (height, fps, tbr, 1 if ext == "mp4" else 0)

        def audio_score(f: Dict) -> Tuple[float, float, int]:
            abr = f.get("abr") or 0
            tbr = f.get("tbr") or 0
            ext = (f.get("ext") or "").lower()
            return (abr, tbr, 1 if ext in ["m4a", "aac"] else 0)

        video_only = [
            f for f in formats
            if f.get("vcodec") not in [None, "none"]
            and f.get("acodec") in [None, "none"]
            and within_height(f)
            and f.get("format_id")
        ]
        audio_only = [
            f for f in formats
            if f.get("acodec") not in [None, "none"]
            and f.get("vcodec") in [None, "none"]
            and f.get("format_id")
        ]
        combined = [
            f for f in formats
            if f.get("vcodec") not in [None, "none"]
            and f.get("acodec") not in [None, "none"]
            and within_height(f)
            and f.get("format_id")
        ]

        if video_only and audio_only:
            v = max(video_only, key=video_score)
            a = max(audio_only, key=audio_score)
            return f"{v['format_id']}+{a['format_id']}", v.get("height")

        if combined:
            c = max(combined, key=video_score)
            return c.get("format_id"), c.get("height")

        return None, None

    def _extract_info(self, url: str, opts: Dict, process: bool = True, silent: bool = True) -> Optional[Dict]:
        """Extract info with minimal errors during probing"""
        info_opts = opts.copy()
        info_opts.pop("format", None)
        info_opts.pop("download_archive", None)
        info_opts["ignore_no_formats_error"] = True
        if silent:
            info_opts["logger"] = _SilentYtdlpLogger()
        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                return ydl.extract_info(url, download=False, process=process)
        except Exception:
            return None

    def _has_video_formats(self, info: Dict) -> bool:
        """Check if extracted info includes any video formats"""
        for f in info.get('formats', []) or []:
            vcodec = f.get('vcodec')
            if vcodec and vcodec != 'none':
                return True
        return False

    def _has_audio_formats(self, info: Dict) -> bool:
        """Check if extracted info includes any audio formats"""
        for f in info.get('formats', []) or []:
            acodec = f.get('acodec')
            if acodec and acodec != 'none':
                return True
        return False

    def _get_max_video_height(self, info: Dict) -> int:
        """Get max available video height from formats"""
        max_height = 0
        for f in info.get('formats', []) or []:
            vcodec = f.get('vcodec')
            if vcodec and vcodec != 'none':
                height = f.get('height') or 0
                if height > max_height:
                    max_height = height
        return max_height
    
    def _build_ydl_opts(self, 
                    output_dir: str,
                    filename: str = None,
                    quality: str = "best",
                    audio_only: bool = False,
                    audio_format: str = "mp3",
                    video_format: str = "mp4",
                    embed_thumbnail: bool = True,
                    embed_metadata: bool = True,
                    subtitles: bool = False,
                    subtitle_langs: List[str] = None,
                    platform: Platform = None) -> Dict:
        """Build yt-dlp options with YouTube bypass"""
        
        # Output template
        if filename:
            safe_name = sanitize_filename(filename)
            outtmpl = os.path.join(output_dir, f"{safe_name}.%(ext)s")
        else:
            outtmpl = os.path.join(output_dir, Config.OUTPUT_TEMPLATE)
        
        opts = {
            'outtmpl': outtmpl,
            'progress_hooks': [self.progress],
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'retries': 10,
            'fragment_retries': 10,
            'socket_timeout': 30,
            'http_chunk_size': 10485760,
            'overwrites': False,
            'windowsfilenames': True,
            'restrictfilenames': False,
            'trim_file_name': Config.MAX_FILENAME_LENGTH,
            'ignoreerrors': False,
            'no_color': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'download_archive': Config.DOWNLOAD_ARCHIVE,
        }
        
        # ===== YOUTUBE OPTIONS =====
        # Let yt-dlp use its own default client order (includes visionos HLS
        # which works without cookies, PO tokens, or JS signature solvers).
        if platform == Platform.YOUTUBE:
            opts.update({
                'age_limit': 99,
                'geo_bypass': True,
                'geo_bypass_country': 'US',
            })
        
        # Audio only
        if audio_only:
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': audio_format,
                'preferredquality': '192',
            }]
        else:
            # Video format
            if platform == Platform.YOUTUBE:
                # Simpler format for YouTube to avoid issues
                height_map = {
                    '8k': 4320, '4k': 2160, '2160p': 2160,
                    '1440p': 1440, '2k': 1440,
                    '1080p': 1080, '720p': 720,
                    '480p': 480, '360p': 360,
                    'best': 9999
                }
                h = height_map.get(quality.lower(), 1080)
                
                if h >= 9999:
                    opts['format'] = 'bestvideo+bestaudio/best'
                else:
                    opts['format'] = f'bestvideo[height<={h}]+bestaudio/best[height<={h}]/best'
            else:
                # Other platforms - use quality manager if available
                if QUALITY_AVAILABLE:
                    opts['format'] = self.quality.get_format_for_quality(quality)
                else:
                    opts['format'] = 'bestvideo+bestaudio/best'
            
            opts['merge_output_format'] = video_format
        
        # Thumbnail
        if embed_thumbnail:
            opts['writethumbnail'] = True
            opts.setdefault('postprocessors', []).append({'key': 'EmbedThumbnail'})
        
        # Metadata
        if embed_metadata:
            opts.setdefault('postprocessors', []).append({
                'key': 'FFmpegMetadata',
                'add_metadata': True
            })
        
        # Subtitles
        if subtitles:
            opts['writesubtitles'] = True
            opts['writeautomaticsub'] = True
            opts['subtitleslangs'] = subtitle_langs or Config.SUBTITLE_LANGUAGES
            opts['subtitlesformat'] = 'srt'
        
        return opts

    def get_info(self, url: str) -> Optional[Dict]:
        """Fetch video info without downloading - FAST version.
        Uses process=False for instant metadata, then quick format probe."""
        platform = detect_platform(url)
        
        # Strip playlist
        if platform == Platform.YOUTUBE and 'list=' in url.lower():
            vid = extract_video_id(url, platform) if PLATFORMS_AVAILABLE else None
            if vid:
                url = f"https://www.youtube.com/watch?v={vid}"
        
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'socket_timeout': 8,
            'extractor_retries': 0,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'ignore_no_formats_error': True,
        }
        self._apply_js_runtimes(opts)
        
        if platform == Platform.YOUTUBE:
            pass  # Let yt-dlp use its default client order
        
        # process=False skips format selection entirely — avoids
        # "Requested format is not available" errors during preview.
        # We still get title, uploader, duration, and raw format list.
        
        # Try with browser cookies first (most reliable for YouTube),
        # then without cookies as fallback (all done silently)
        if platform == Platform.YOUTUBE:
            result = _try_with_cookies(opts, url, download=False, process=False)
            if result:
                return result
        
        # Fallback: try without cookies
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False, process=False)
        except Exception as e:
            logger.error(f"Error fetching info: {e}")
            return None

    # ================================================================
    # WEB API METHODS
    # ================================================================

    def get_full_info(self, url: str) -> Optional[Dict]:
        """Fetch complete video info WITH full format enumeration.
        Returns structured data suitable for the web API."""
        platform = detect_platform(url)

        # Strip playlist from single-video YouTube URLs
        if platform == Platform.YOUTUBE and 'list=' in url.lower():
            vid = extract_video_id(url, platform) if PLATFORMS_AVAILABLE else None
            if vid:
                url = f"https://www.youtube.com/watch?v={vid}"

        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'socket_timeout': 12,
            'extractor_retries': 1,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'ignore_no_formats_error': True,
        }
        self._apply_js_runtimes(opts)

        if platform == Platform.YOUTUBE:
            pass  # Let yt-dlp use its default client order

        info = None
        # Try cookies then direct (silently)
        if platform == Platform.YOUTUBE:
            info, _ = _get_working_opts_with_cookies(opts, url)

        if not info:
            try:
                opts['logger'] = _SilentYtdlpLogger()
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:
                logger.error(f"get_full_info error: {e}")
                return None

        if not info:
            return None

        # Build structured response
        formats_raw = info.get('formats') or []

        # Video formats (deduplicated by height)
        video_formats = []
        seen_heights = set()
        for f in formats_raw:
            vc = f.get('vcodec')
            h = f.get('height')
            if not vc or vc == 'none' or not h or h <= 0:
                continue
            fmt_note = (f.get('format_note') or '').lower()
            proto = (f.get('protocol') or '').lower()
            ext = (f.get('ext') or '').lower()
            if 'storyboard' in fmt_note or proto == 'mhtml' or ext == 'mhtml':
                continue
            if h not in seen_heights:
                seen_heights.add(h)
                label_map = {4320: '8K', 2160: '4K', 1440: '2K', 1080: 'Full HD', 720: 'HD', 480: 'SD'}
                video_formats.append({
                    'height': h,
                    'label': label_map.get(h, f'{h}p'),
                    'fps': f.get('fps') or 0,
                    'vcodec': vc,
                    'ext': ext,
                })
        video_formats.sort(key=lambda x: x['height'], reverse=True)

        # Audio tracks
        audio_tracks = self.get_audio_tracks(info)

        # Subtitles
        manual_subs = info.get('subtitles') or {}
        auto_subs = info.get('automatic_captions') or {}
        subtitle_info = {
            'manual': [{'code': k, 'name': k} for k in sorted(manual_subs.keys())],
            'auto_count': len(auto_subs),
        }

        # Thumbnail
        thumb = info.get('thumbnail') or ''
        if not thumb:
            thumbs = info.get('thumbnails') or []
            if thumbs:
                thumb = thumbs[-1].get('url', '')

        return {
            'title': info.get('title', 'Unknown'),
            'uploader': info.get('uploader') or info.get('channel') or 'Unknown',
            'duration': info.get('duration') or 0,
            'thumbnail': thumb,
            'platform': platform.value,
            'url': url,
            'view_count': info.get('view_count'),
            'upload_date': info.get('upload_date'),
            'description': (info.get('description') or '')[:500],
            'video_formats': video_formats,
            'audio_tracks': audio_tracks,
            'subtitles': subtitle_info,
            'is_live': info.get('is_live', False),
        }

    def get_audio_tracks(self, info: Dict) -> List[Dict]:
        """Extract all audio tracks including multi-language and 5.1 surround."""
        formats_raw = info.get('formats') or []
        tracks = []
        seen = set()

        for f in formats_raw:
            ac = f.get('acodec')
            if not ac or ac == 'none':
                continue
            vc = f.get('vcodec')
            if vc and vc != 'none':
                continue  # combined format, skip for audio-only listing

            lang = f.get('language') or 'default'
            channels = f.get('audio_channels') or 2
            abr = f.get('abr') or f.get('tbr') or 0
            ext = (f.get('ext') or '').lower()

            key = f"{lang}_{channels}_{int(abr)}"
            if key in seen:
                continue
            seen.add(key)

            channel_label = '5.1 Surround' if channels >= 6 else 'Stereo' if channels >= 2 else 'Mono'
            tracks.append({
                'language': lang,
                'channels': channels,
                'channel_label': channel_label,
                'bitrate': int(abr),
                'codec': ac,
                'ext': ext,
                'format_id': f.get('format_id', ''),
            })

        # Sort: default language first, then by channels desc, then bitrate desc
        tracks.sort(key=lambda t: (
            0 if t['language'] == 'default' else 1,
            -t['channels'],
            -t['bitrate'],
        ))
        return tracks

    def api_download(self, url: str, options: Dict, progress_callback=None) -> Dict:
        """Download with callback-based progress reporting for the web API.
        
        Args:
            url: Media URL
            options: Dict with keys: mode ('video'|'audio'), quality, audio_format,
                     subtitles (list of lang codes), format_id (optional specific format)
            progress_callback: callable(event_dict) called with progress events
        
        Returns:
            Dict with 'status', 'filepath', 'filesize', 'title', 'error'
        """
        mode = options.get('mode', 'video')
        quality = options.get('quality', '1080p')
        audio_format = options.get('audio_format', 'mp3')
        subtitle_langs = options.get('subtitles')

        platform = detect_platform(url)
        if platform == Platform.YOUTUBE and "list=" in url.lower():
            video_id = extract_video_id(url, platform) if PLATFORMS_AVAILABLE else None
            if video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"

        if mode == 'audio':
            out_dir = self._get_output_dir(platform, "audio")
        else:
            out_dir = self._get_output_dir(platform, "video")

        # Progress hook that calls the callback
        def _hook(d):
            if not progress_callback:
                return
            status = d.get('status', '')
            if status == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                speed = d.get('speed', 0) or 0
                eta = d.get('eta', 0) or 0
                pct = (downloaded / total * 100) if total > 0 else 0
                progress_callback({
                    'type': 'progress',
                    'percent': round(pct, 1),
                    'downloaded': downloaded,
                    'total': total,
                    'speed': speed,
                    'eta': eta,
                    'speed_str': format_size(speed) + '/s' if speed else '---',
                    'downloaded_str': format_size(downloaded),
                    'total_str': format_size(total) if total else '---',
                })
            elif status == 'finished':
                progress_callback({
                    'type': 'postprocessing',
                    'filename': os.path.basename(d.get('filename', '')),
                })

        # Build yt-dlp options
        base_opts = {
            'outtmpl': os.path.join(out_dir, Config.OUTPUT_TEMPLATE),
            'progress_hooks': [_hook],
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'retries': 3,
            'socket_timeout': 15,
            'extractor_retries': 1,
            'fragment_retries': 3,
            'overwrites': False,
            'windowsfilenames': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'logger': _SilentYtdlpLogger(),
        }
        self._apply_js_runtimes(base_opts)

        if mode == 'audio':
            base_opts['format'] = 'bestaudio/best'
            base_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': audio_format,
                'preferredquality': '192',
            }]
            if Config.EMBED_THUMBNAIL:
                base_opts['writethumbnail'] = True
                base_opts['postprocessors'].append({'key': 'EmbedThumbnail'})
            if Config.EMBED_METADATA:
                base_opts['postprocessors'].append({'key': 'FFmpegMetadata', 'add_metadata': True})
        else:
            height_map = {'8k': 4320, '4k': 2160, '1440p': 1440, '1080p': 1080,
                          '720p': 720, '480p': 480, '360p': 360, 'best': 9999}
            h = height_map.get(quality.lower(), 1080)
            if h >= 9999:
                base_opts['format'] = 'bestvideo+bestaudio/best'
            else:
                base_opts['format'] = f'bestvideo[height<={h}]+bestaudio/best[height<={h}]/best'
            base_opts['merge_output_format'] = 'mp4'
            if Config.EMBED_THUMBNAIL:
                base_opts['writethumbnail'] = True
                base_opts.setdefault('postprocessors', []).append({'key': 'EmbedThumbnail'})
            if Config.EMBED_METADATA:
                base_opts.setdefault('postprocessors', []).append({'key': 'FFmpegMetadata', 'add_metadata': True})

        if subtitle_langs:
            base_opts['writesubtitles'] = True
            base_opts['writeautomaticsub'] = True
            base_opts['subtitleslangs'] = subtitle_langs
            base_opts.setdefault('postprocessors', []).append({'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'})
            base_opts.setdefault('postprocessors', []).append({'key': 'FFmpegEmbedSubtitle'})

        # YouTube-specific options
        if platform == Platform.YOUTUBE:
            pass  # Let yt-dlp use its default client order

        # Try with cookies, then direct (silently)
        info = None
        working_opts = None

        if progress_callback:
            progress_callback({'type': 'status', 'message': 'Connecting...'})

        info, working_opts = _get_working_opts_with_cookies(base_opts, url)

        if not info:
            working_opts = base_opts
            if progress_callback:
                progress_callback({'type': 'status', 'message': 'Direct download mode...'})

        title = info.get('title', 'Unknown') if info else 'Unknown'
        if progress_callback:
            progress_callback({'type': 'status', 'message': f'Downloading: {title[:50]}...'})

        # Select concrete format for video
        if mode != 'audio' and info:
            sel_fmt, sel_h = self._select_video_format_id(
                info, None if quality.lower() == 'best' else height_map.get(quality.lower(), 1080)
            )
            if sel_fmt:
                working_opts = working_opts.copy()
                working_opts['format'] = sel_fmt

        # Download
        working_opts['progress_hooks'] = [_hook]
        working_opts.pop('logger', None)  # allow progress output

        try:
            with yt_dlp.YoutubeDL(working_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            err = str(e).lower()
            if "requested format" in err or "no video formats" in err:
                fallback = working_opts.copy()
                fallback['format'] = 'bestvideo+bestaudio/best' if mode != 'audio' else 'bestaudio/best'
                with yt_dlp.YoutubeDL(fallback) as ydl:
                    ydl.download([url])
            else:
                if progress_callback:
                    progress_callback({'type': 'error', 'message': str(e)[:200]})
                return {'status': 'failed', 'error': str(e)[:200], 'title': title}

        # Find downloaded file
        exts = ('.mp3', '.m4a', '.opus', '.flac', '.ogg') if mode == 'audio' else ('.mp4', '.mkv', '.webm')
        filepath = None
        for f in sorted(os.listdir(out_dir), key=lambda x: os.path.getmtime(os.path.join(out_dir, x)), reverse=True):
            if f.endswith(exts):
                fp = os.path.join(out_dir, f)
                if time.time() - os.path.getmtime(fp) < 300:
                    filepath = fp
                    break

        if filepath and os.path.exists(filepath):
            filesize = os.path.getsize(filepath)
            self.db.add_download(
                url=url, platform=platform.value, title=title,
                filesize=filesize, filepath=filepath,
                media_type=mode, status='complete'
            )
            if progress_callback:
                progress_callback({
                    'type': 'complete',
                    'filepath': filepath,
                    'filename': os.path.basename(filepath),
                    'filesize': filesize,
                    'filesize_str': format_size(filesize),
                    'title': title,
                })
            return {
                'status': 'complete', 'filepath': filepath,
                'filesize': filesize, 'title': title,
            }

        if progress_callback:
            progress_callback({'type': 'complete', 'filepath': out_dir, 'title': title})
        return {'status': 'complete', 'filepath': out_dir, 'title': title}

    def download_spotify(self, url: str, quality: str = "flac", output_dir: str = None) -> Union[Optional[str], List[str]]:
        """Download track, album, or playlist from Spotify / streaming music with SpotiFLAC"""
        if not SPOTIFLAC_AVAILABLE:
            print("  ✗ SpotiFLAC module is not available")
            return None
        
        downloader = SpotiFLACDownloader(output_dir=output_dir or Config.MUSIC_DIR)
        print(f"\n  🎵 SpotiFLAC Lossless Music Downloader")
        print(f"  Resolving metadata for {url}...")
        
        def _cli_progress(p):
            msg = p.get('message', '')
            if msg:
                print(f"  -> {msg}")
        
        try:
            results = downloader.download_url(url, quality=quality, output_dir=output_dir, progress_callback=_cli_progress)
        except Exception as e:
            print(f"  ✗ SpotiFLAC error: {e}")
            return None

        if not results:
            print("  ✗ Failed to download track(s)")
            return None
        
        # Record into database
        for r in results:
            if self.db:
                rec = DownloadRecord(
                    url=url,
                    title=r.get('title', 'Unknown'),
                    platform='Spotify',
                    format=r.get('format', quality.upper()),
                    quality=quality,
                    filesize=r.get('filesize', 0),
                    filepath=r.get('filepath', ''),
                    status=DownloadStatus.COMPLETE,
                    media_type='audio',
                )
                self.db.add_download(rec)
        
        if len(results) == 1:
            return results[0].get('filepath')
        return [r.get('filepath') for r in results]

    def download_video(self,
                    url: str,
                    quality: str = "1080p",
                    output_dir: str = None,
                    filename: str = None,
                    embed_thumbnail: bool = True,
                    _dl_start_time: float = None,  # internal: set by wrapper
                    embed_metadata: bool = True,
                    subtitles: bool = False,
                    subtitle_langs: List[str] = None,
                    prompt_redownload: bool = True) -> Optional[str]:
        """Download video - FAST version with quick timeout"""
        
        _dl_t0 = _dl_start_time or time.time()
        
        self.progress.reset()
        platform = detect_platform(url)
        
        if platform == Platform.SPOTIFY or (SPOTIFLAC_AVAILABLE and SpotiFLACDownloader.is_spotify_url(url)):
            print("  [Spotify detected: downloading high-fidelity audio/FLAC with cover art & lyrics]")
            res = self.download_spotify(url, quality="flac", output_dir=output_dir)
            return res if isinstance(res, str) else (res[0] if res else None)
        
        if self.ml:
            self.ml.log_download("start", url, pl=platform.value if PLATFORMS_AVAILABLE else "unknown",
                                q=quality, m="yt-dlp", mt="video")
        
        if platform == Platform.YOUTUBE and "list=" in url.lower():
            video_id = extract_video_id(url, platform)
            if video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
        out_dir = output_dir or self._get_output_dir(platform, "video")
        
        # Base options
        base_opts = {
            'outtmpl': os.path.join(out_dir, Config.OUTPUT_TEMPLATE),
            'progress_hooks': [self.progress],
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'retries': 3,
            'socket_timeout': 10,  # FAST 10 second timeout
            'extractor_retries': 1,
            'file_access_retries': 1,
            'fragment_retries': 3,
            'overwrites': False,
            'windowsfilenames': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'merge_output_format': 'mp4',
            'download_archive': Config.DOWNLOAD_ARCHIVE,
        }
        self._apply_js_runtimes(base_opts)
        
        # Quality format
        height_map = {'8k': 4320, '4320p': 4320, '4k': 2160, '2160p': 2160, '1440p': 1440, '1080p': 1080, 
                      '720p': 720, '480p': 480, '360p': 360, 'best': 9999}
        h = height_map.get(quality.lower(), 1080)
        base_opts['format'] = f'bestvideo[height<={h}]+bestaudio/best[height<={h}]/best' if h < 9999 else 'bestvideo+bestaudio/best'
        
        # Thumbnail & metadata
        if embed_thumbnail:
            base_opts['writethumbnail'] = True
            base_opts['postprocessors'] = [{'key': 'EmbedThumbnail'}]
        if embed_metadata:
            base_opts.setdefault('postprocessors', []).append({'key': 'FFmpegMetadata', 'add_metadata': True})
        if subtitles:
            base_opts['writesubtitles'] = True
            base_opts['writeautomaticsub'] = True
            if subtitle_langs:
                base_opts['subtitleslangs'] = subtitle_langs
            else:
                base_opts['subtitleslangs'] = Config.SUBTITLE_LANGUAGES if CONFIG_AVAILABLE else ["en", "es", "fr", "de", "it", "ja", "ko", "ru", "pt", "hi", "zh"]
            base_opts['sleep_interval_requests'] = 1  # Helps bypass subtitle rate limiting (429 Too Many Requests)
            base_opts.setdefault('postprocessors', []).append({'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'})
            base_opts.setdefault('postprocessors', []).append({'key': 'FFmpegEmbedSubtitle'})
        
        info = None
        working_opts = None
        direct_download = False
        
        if platform == Platform.YOUTUBE:
            print("  Connecting to YouTube...", end='', flush=True)

            # Let yt-dlp use its default client order (includes visionos HLS)

            # Try browser cookies silently, then direct
            info, working_opts = _get_working_opts_with_cookies(base_opts, url)
            if info:
                working_opts['progress_hooks'] = [self.progress]
                # Determine which strategy worked for label
                label = 'direct'
                cfb = working_opts.get('cookiesfrombrowser')
                if cfb:
                    label = cfb[0] if isinstance(cfb, tuple) else str(cfb)
                print(f" OK ({label})")
            else:
                # Fallback: skip extract_info, go straight to download
                working_opts = base_opts.copy()
                working_opts['logger'] = _SilentYtdlpLogger()
                working_opts['progress_hooks'] = [self.progress]
                direct_download = True
                print(" (direct mode)")
        else:
            # Other platforms (Instagram, etc.) - simpler approach
            print("  Fetching...", end='', flush=True)
            try:
                probe_opts = base_opts.copy()
                probe_opts.pop('download_archive', None)
                with yt_dlp.YoutubeDL(probe_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    print(" OK")
                    working_opts = base_opts
            except Exception as e:
                print(f" Error: {e}")
                raise DownloadError(str(e))
        
        if not info and not direct_download:
            raise DownloadError("Could not get video info")

        # Choose a concrete format id to avoid "Requested format is not available"
        quality_label = quality
        if info:
            selected_format, selected_height = self._select_video_format_id(
                info, None if h >= 9999 else h
            )
            if selected_format:
                working_opts = working_opts.copy()
                working_opts['format'] = selected_format
            if selected_height and h < 9999 and selected_height < h:
                quality_label = f"best (max {selected_height}p)"
            
            # Display info
            title = info.get('title', 'Unknown')
            uploader = info.get('uploader', 'Unknown')
            duration = info.get('duration', 0)
            
            print(f"\n  {'═' * 50}")
            print(f"  Title:    {title[:45]}")
            print(f"  Uploader: {uploader}")
            print(f"  Duration: {format_duration(duration)}")
            print(f"  Quality:  {quality_label}")
            print(f"  {'═' * 50}\n")
        else:
            title = 'Unknown'
            uploader = 'Unknown'
            duration = 0
        # Remove silent logger for actual download so progress/errors are visible
        download_opts = working_opts.copy()
        download_opts.pop('logger', None)
        download_opts['progress_hooks'] = [self.progress]
        download_opts['quiet'] = True
        download_opts['no_warnings'] = True
        
        # Track if video was skipped (archive) 
        archive_logger = _ArchiveLogger()
        download_opts['logger'] = archive_logger
        
        print("  Downloading...")
        try:
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            err = str(e).lower()
            if "requested format is not available" in err or "no video formats found" in err:
                if self.ml:
                    self.ml.log_error("fallback", url, reason="format_unavail", q=quality)
                print("  Format unavailable, retrying with best available...")
                fallback_opts = download_opts.copy()
                fallback_opts['format'] = 'bestvideo+bestaudio/best'
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    ydl.download([url])
            elif 'subtitle' in err or '429' in err:
                print("  Subtitle download failed (rate limited), retrying without subtitles...")
                fallback_opts = download_opts.copy()
                fallback_opts.pop('writesubtitles', None)
                fallback_opts.pop('writeautomaticsub', None)
                fallback_opts.pop('subtitleslangs', None)
                fallback_opts.pop('subtitlesformat', None)
                # Strip subtitle-related postprocessors so the retry doesn't hit the same error
                if 'postprocessors' in fallback_opts:
                    fallback_opts['postprocessors'] = [
                        pp for pp in fallback_opts['postprocessors']
                        if pp.get('key') not in ('FFmpegSubtitlesConvertor', 'FFmpegEmbedSubtitle')
                    ]
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    ydl.download([url])
            else:
                raise
        
        # Check if video was skipped due to archive
        if archive_logger.skipped:
            existing_fp = None
            for f in os.listdir(out_dir):
                if f.endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov')):
                    fp = os.path.join(out_dir, f)
                    if title != 'Unknown' and title[:15].lower() in f.lower():
                        existing_fp = fp
                        break
            
            print(f"\n  ℹ Video already downloaded (found in archive).")
            if existing_fp:
                print(f"  ✓ Existing: {existing_fp}")
                print(f"  ✓ Size:     {format_size(os.path.getsize(existing_fp))}")
            else:
                print(f"  Check: {out_dir}")

            redownload = False
            if prompt_redownload:
                print("\n  Do you want to:")
                print("    1. Skip (keep existing file) [Default]")
                print("    2. Re-download (overwrite)")
                choice = input("\n  Select [1]: ").strip().lower()
                if choice in ('2', 'r', 'redownload', 're-download', 'y', 'yes'):
                    redownload = True

            if redownload:
                print("\n  🔄 Re-downloading video...")
                redl_opts = download_opts.copy()
                redl_opts.pop('download_archive', None)
                redl_opts['overwrites'] = True
                archive_logger.skipped = False
                try:
                    with yt_dlp.YoutubeDL(redl_opts) as ydl:
                        ydl.download([url])
                except Exception as e:
                    print(f"  ⚠ Re-download error: {e}")
            else:
                print("  Skipped.")
                return existing_fp or out_dir
        
        # Find file
        safe_title = sanitize_filename(title) if title != 'Unknown' else ''
        filepath = None
        
        for f in sorted(os.listdir(out_dir), key=lambda x: os.path.getmtime(os.path.join(out_dir, x)), reverse=True):
            if f.endswith(('.mp4', '.mkv', '.webm')):
                if safe_title and safe_title[:15].lower() in f.lower():
                    filepath = os.path.join(out_dir, f)
                    break
                if title != 'Unknown' and title[:15].lower() in f.lower():
                    filepath = os.path.join(out_dir, f)
                    break
                # Check recently modified (within 5 min)
                fp = os.path.join(out_dir, f)
                if time.time() - os.path.getmtime(fp) < 300:
                    filepath = fp
                    break
        
        if filepath and os.path.exists(filepath):
            filesize = os.path.getsize(filepath)
            self.db.add_download(url=url, platform=platform.value, title=title,
                                uploader=uploader, duration=duration, quality=quality_label,
                                filesize=filesize, filepath=filepath, status='complete')
            
            if self.ml:
                self.ml.log_download("done", url, pl=platform.value, q=quality_label,
                                     sz=filesize, dur=round(time.time() - _dl_t0, 1))
            
            print(f"\n  ✓ Complete: {filepath}")
            print(f"  ✓ Size: {format_size(filesize)}")
            return filepath
        
        print(f"\n  ⚠ Check: {out_dir}")
        return out_dir

    def download_audio(self,
                    url: str,
                    audio_format: str = "mp3",
                    output_dir: str = None,
                    filename: str = None,
                    embed_thumbnail: bool = True,
                    embed_metadata: bool = True,
                    prompt_redownload: bool = True) -> Optional[str]:
        """Download audio - FAST version"""
        
        self.progress.reset()
        platform = detect_platform(url)
        
        if platform == Platform.SPOTIFY or (SPOTIFLAC_AVAILABLE and SpotiFLACDownloader.is_spotify_url(url)):
            quality = "flac" if audio_format.lower() in ("flac", "wav") else "320k"
            res = self.download_spotify(url, quality=quality, output_dir=output_dir)
            return res if isinstance(res, str) else (res[0] if res else None)
        
        if platform == Platform.YOUTUBE and "list=" in url.lower():
            video_id = extract_video_id(url, platform)
            if video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
        out_dir = output_dir or self._get_output_dir(platform, "audio")
        
        base_opts = {
            'outtmpl': os.path.join(out_dir, Config.OUTPUT_TEMPLATE),
            'progress_hooks': [self.progress],
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': 10,
            'retries': 3,
            'format': 'bestaudio/best',
            'windowsfilenames': True,
            'geo_bypass': True,
            'download_archive': Config.DOWNLOAD_ARCHIVE,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': audio_format,
                'preferredquality': '192',
            }],
        }
        self._apply_js_runtimes(base_opts)
        
        if embed_thumbnail:
            base_opts['writethumbnail'] = True
            base_opts['postprocessors'].append({'key': 'EmbedThumbnail'})
        if embed_metadata:
            base_opts['postprocessors'].append({'key': 'FFmpegMetadata', 'add_metadata': True})
        
        info = None
        working_opts = None
        
        if platform == Platform.YOUTUBE:
            print("  Connecting...", end='', flush=True)

            # Let yt-dlp use its default client order (includes visionos HLS)
            # Try browser cookies silently, then direct
            info, working_opts = _get_working_opts_with_cookies(base_opts, url)
            if info:
                label = 'direct'
                cfb = working_opts.get('cookiesfrombrowser')
                if cfb:
                    label = cfb[0] if isinstance(cfb, tuple) else str(cfb)
                print(f" OK ({label})")
            else:
                print(" FAILED")
                raise DownloadError("Could not fetch audio info from YouTube")
        else:
            print("  Fetching...", end='', flush=True)
            try:
                probe_opts = base_opts.copy()
                probe_opts.pop('download_archive', None)
                with yt_dlp.YoutubeDL(probe_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    print(" OK")
                    working_opts = base_opts
            except Exception as e:
                print(f" Error")
                raise DownloadError(str(e))
        
        title = info.get('title', 'Unknown')
        uploader = info.get('uploader', 'Unknown')
        duration = info.get('duration', 0)
        
        print(f"\n  Title:    {title[:45]}")
        print(f"  Duration: {format_duration(duration)}")
        print(f"  Format:   {audio_format.upper()}\n")
        
        archive_logger = _ArchiveLogger()
        working_opts['logger'] = archive_logger

        print("  Downloading...")
        try:
            with yt_dlp.YoutubeDL(working_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            err = str(e).lower()
            if "requested format is not available" in err or "no video formats" in err:
                print("  Format unavailable, retrying with best available...")
                fallback_opts = working_opts.copy()
                fallback_opts['format'] = 'bestaudio/best'
                fallback_opts.pop('extractor_args', None)
                try:
                    with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                        ydl.download([url])
                except Exception as e2:
                    print(f"  ⚠ Download error: {e2}")
            elif "403" in err or "forbidden" in err:
                print("  HTTP 403, retrying with fallback format...")
                fallback_opts = working_opts.copy()
                fallback_opts['format'] = 'best'
                fallback_opts.pop('extractor_args', None)
                try:
                    with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                        ydl.download([url])
                except Exception as e2:
                    print(f"  ⚠ Download error: {e2}")
            else:
                print(f"  ⚠ Download error: {e}")
        
        if archive_logger.skipped:
            existing_fp = None
            for f in os.listdir(out_dir):
                if f.endswith(('.mp3', '.m4a', '.opus', '.flac', '.ogg')):
                    fp = os.path.join(out_dir, f)
                    if title != 'Unknown' and title[:15].lower() in f.lower():
                        existing_fp = fp
                        break
            
            print(f"\n  ℹ Audio already downloaded (found in archive).")
            if existing_fp:
                print(f"  ✓ Existing: {existing_fp}")
                print(f"  ✓ Size:     {format_size(os.path.getsize(existing_fp))}")
            else:
                print(f"  Check: {out_dir}")

            redownload = False
            if prompt_redownload:
                print("\n  Do you want to:")
                print("    1. Skip (keep existing file) [Default]")
                print("    2. Re-download (overwrite)")
                choice = input("\n  Select [1]: ").strip().lower()
                if choice in ('2', 'r', 'redownload', 're-download', 'y', 'yes'):
                    redownload = True

            if redownload:
                print("\n  🔄 Re-downloading audio...")
                redl_opts = working_opts.copy()
                redl_opts.pop('download_archive', None)
                redl_opts['overwrites'] = True
                archive_logger.skipped = False
                try:
                    with yt_dlp.YoutubeDL(redl_opts) as ydl:
                        ydl.download([url])
                except Exception as e:
                    print(f"  ⚠ Re-download error: {e}")
            else:
                print("  Skipped.")
                return existing_fp or out_dir
        
        # Find file
        safe_title = sanitize_filename(title)
        filepath = None
        
        for f in sorted(os.listdir(out_dir), key=lambda x: os.path.getmtime(os.path.join(out_dir, x)), reverse=True):
            if f.endswith(('.mp3', '.m4a', '.opus', '.flac', '.ogg')):
                fp = os.path.join(out_dir, f)
                if time.time() - os.path.getmtime(fp) < 120:
                    filepath = fp
                    break
        
        if filepath and os.path.exists(filepath):
            filesize = os.path.getsize(filepath)
            self.db.add_download(url=url, platform=platform.value, title=title,
                                format=audio_format, filesize=filesize, filepath=filepath,
                                media_type='audio', status='complete')
            print(f"\n  ✓ Complete: {filepath}")
            print(f"  ✓ Size: {format_size(filesize)}")
            return filepath
        
        print(f"\n  ⚠ Check: {out_dir}")
        return out_dir
    
    def download_playlist(self,
                          url: str,
                          quality: str = "1080p",
                          audio_only: bool = False,
                          audio_format: str = "mp3",
                          start: int = None,
                          end: int = None,
                          skip_existing: bool = True) -> List[str]:
        """Download playlist — built for 200+ items.
        
        Features:
        - Single cookie probe reused for every item
        - Compact per-item progress with overall playlist progress bar
        - Elapsed time, ETA, average speed tracking
        - Auto-retry with back-off (2 retries per item)
        - Rate-limiting between requests
        - Graceful Ctrl+C to stop early
        - End-of-run summary table
        """

        platform = detect_platform(url)
        if platform == Platform.SPOTIFY or (SPOTIFLAC_AVAILABLE and SpotiFLACDownloader.is_spotify_url(url)):
            quality = "flac" if (audio_only and audio_format.lower() in ("flac", "wav")) else ("320k" if audio_only else "flac")
            res = self.download_spotify(url, quality=quality)
            return res if isinstance(res, list) else ([res] if res else [])

        logger.info(f"Downloading playlist: {url}")
        media_type = "audio" if audio_only else "video"

        # ── 1. Fetch playlist metadata (flat) ──────────────────────────
        flat_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': 'in_playlist',
            'skip_download': True,
            'logger': _SilentYtdlpLogger(),
        }
        self._apply_js_runtimes(flat_opts)

        if platform == Platform.YOUTUBE:
            pass  # Let yt-dlp use its default client order

        print(f"\n  {'═' * 60}")
        print(f"  📋  Fetching playlist info...", end='', flush=True)

        info = _try_with_cookies(flat_opts, url, download=False, process=False)
        if not info:
            try:
                with yt_dlp.YoutubeDL(flat_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:
                print(f" FAILED")
                raise DownloadError(f"Failed to get playlist: {e}")

        if not info:
            print(f" FAILED")
            raise DownloadError("Could not get playlist info")

        playlist_title = sanitize_filename(info.get('title', 'Playlist'))
        entries = list(info.get('entries', []) or [])

        if not entries:
            print(f" empty")
            print(f"  No {'tracks' if audio_only else 'videos'} found in playlist")
            return []

        total_all = len(entries)
        print(f" {total_all} items")

        # ── 2. Apply range ─────────────────────────────────────────────
        if start or end:
            start_idx = (start - 1) if start else 0
            end_idx = end if end else total_all
            entries = entries[start_idx:end_idx]
            range_str = f"#{start_idx + 1} → #{min(end_idx, total_all)}"
        else:
            range_str = f"All {total_all}"

        total = len(entries)

        # ── 3. Print header ────────────────────────────────────────────
        mode_label = f"🎵 Audio ({audio_format.upper()})" if audio_only else f"🎬 Video ({quality})"
        print(f"  {'═' * 60}")
        print(f"  📂  {playlist_title[:50]}")
        print(f"  🌐  {platform.value}")
        print(f"  📊  {range_str} ({total} items)")
        print(f"  🎯  {mode_label}")
        if skip_existing:
            print(f"  ⏭   Skip existing: ON")
        print(f"  {'═' * 60}")

        # ── 4. Create output directory ─────────────────────────────────
        base_dir = self._get_output_dir(platform, media_type)
        if Config.ORGANIZE_BY_PLAYLIST:
            playlist_dir = os.path.join(base_dir, playlist_title)
        else:
            playlist_dir = base_dir
        os.makedirs(playlist_dir, exist_ok=True)

        # ── 5. Probe cookie strategy once ──────────────────────────────
        working_cookie = None  # None = direct (no cookies)
        if platform == Platform.YOUTUBE and entries:
            first_url = None
            for e in entries[:3]:
                eid = e.get('id') or e.get('url')
                if eid:
                    first_url = f"https://www.youtube.com/watch?v={eid}" if len(eid) == 11 else eid
                    break
            if first_url:
                probe_opts = {
                    'quiet': True, 'no_warnings': True, 'skip_download': True,
                    'noplaylist': True, 'socket_timeout': 8,
                    'logger': _SilentYtdlpLogger(),
                }
                self._apply_js_runtimes(probe_opts)
                _, probe_working = _get_working_opts_with_cookies(probe_opts, first_url)
                if probe_working and 'cookiesfrombrowser' in probe_working:
                    working_cookie = probe_working['cookiesfrombrowser']
                    print(f"  🍪  Using {working_cookie[0]} cookies")
                else:
                    print(f"  🔗  Direct mode (no cookies)")

        # ── 6. Download loop ───────────────────────────────────────────
        downloaded = []        # successful file paths
        failed_items = []      # (idx, title, error)
        skipped = 0
        total_bytes = 0
        t_start = time.time()
        interrupted = False

        # Compact progress hook for playlist mode
        class _PlaylistProgressHook:
            def __init__(self):
                self.last_update = 0
            
            def __call__(self, d):
                status = d.get('status', '')
                if status == 'downloading':
                    now = time.time()
                    if now - self.last_update < 0.5:
                        return
                    self.last_update = now
                    dl = d.get('downloaded_bytes', 0)
                    tot = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    speed = d.get('speed', 0) or 0
                    if tot > 0:
                        pct = dl / tot * 100
                        bar_w = 20
                        filled = int(bar_w * pct / 100)
                        bar = '█' * filled + '░' * (bar_w - filled)
                        speed_s = format_size(speed) + "/s" if speed else "---"
                        print(f"\r       [{bar}] {pct:5.1f}% {format_size(dl)}/{format_size(tot)} {speed_s}  ", end='', flush=True)
                elif status == 'finished':
                    print(f"\r       ✓ Post-processing...                                          ", end='', flush=True)

        playlist_hook = _PlaylistProgressHook()

        print(f"\n  Starting downloads...\n")

        for idx, entry in enumerate(entries, 1):
            if interrupted:
                break

            video_title = str(entry.get('title') or f'Item {idx}')
            video_id = entry.get('id') or entry.get('url', '')

            # Build URL
            if video_id and platform == Platform.YOUTUBE and len(str(video_id)) == 11:
                video_url = f"https://www.youtube.com/watch?v={video_id}"
            elif video_id and video_id.startswith('http'):
                video_url = video_id
            elif entry.get('url'):
                video_url = entry['url']
            else:
                # Overall progress bar
                elapsed = time.time() - t_start
                _print_playlist_line(idx, total, '✗', video_title, 'No URL', elapsed)
                failed_items.append((idx, video_title[:40], 'No URL'))
                continue

            # Skip check
            if skip_existing and self.db.is_downloaded(video_url):
                elapsed = time.time() - t_start
                _print_playlist_line(idx, total, '⊘', video_title, 'exists', elapsed)
                skipped += 1
                continue

            # Print header for this item
            elapsed = time.time() - t_start
            _print_playlist_line(idx, total, '⬇', video_title, 'downloading...', elapsed, end='')

            # Build opts for this item
            safe_title = sanitize_filename(video_title)
            indexed_name = f"{idx:03d}. {safe_title}"
            outtmpl = os.path.join(playlist_dir, f"{indexed_name}.%(ext)s")

            item_opts = {
                'outtmpl': outtmpl,
                'progress_hooks': [playlist_hook],
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
                'retries': 3,
                'fragment_retries': 3,
                'socket_timeout': 15,
                'overwrites': False,
                'windowsfilenames': True,
                'geo_bypass': True,
                'nocheckcertificate': True,
                'trim_file_name': Config.MAX_FILENAME_LENGTH,
                'logger': _SilentYtdlpLogger(),
            }
            self._apply_js_runtimes(item_opts)

            if working_cookie:
                item_opts['cookiesfrombrowser'] = working_cookie

            if platform == Platform.YOUTUBE:
                pass  # Let yt-dlp use its default client order

            if audio_only:
                item_opts['format'] = 'bestaudio/best'
                item_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': audio_format,
                    'preferredquality': '192',
                }]
                if Config.EMBED_THUMBNAIL:
                    item_opts['writethumbnail'] = True
                    item_opts['postprocessors'].append({'key': 'EmbedThumbnail'})
                if Config.EMBED_METADATA:
                    item_opts['postprocessors'].append({'key': 'FFmpegMetadata', 'add_metadata': True})
            else:
                height_map = {
                    '8k': 4320, '4k': 2160, '2160p': 2160,
                    '1440p': 1440, '2k': 1440,
                    '1080p': 1080, '720p': 720,
                    '480p': 480, '360p': 360,
                    'best': 9999
                }
                h = height_map.get(quality.lower(), 1080)
                if h >= 9999:
                    item_opts['format'] = 'bestvideo+bestaudio/best'
                else:
                    item_opts['format'] = f'bestvideo[height<={h}]+bestaudio/best[height<={h}]/best'
                item_opts['merge_output_format'] = 'mp4'
                if Config.EMBED_THUMBNAIL:
                    item_opts['writethumbnail'] = True
                    item_opts.setdefault('postprocessors', []).append({'key': 'EmbedThumbnail'})
                if Config.EMBED_METADATA:
                    item_opts.setdefault('postprocessors', []).append({'key': 'FFmpegMetadata', 'add_metadata': True})

            # Download with retry
            max_retries = 2
            success = False
            last_error = ""

            for attempt in range(max_retries + 1):
                try:
                    with _suppress_stderr():
                        with yt_dlp.YoutubeDL(item_opts) as ydl:
                            ydl.download([video_url])
                    success = True
                    break
                except KeyboardInterrupt:
                    interrupted = True
                    print(f"\r  ⚠  Interrupted by user!                                                ")
                    break
                except Exception as e:
                    err_msg = str(e)
                    last_error = err_msg
                    err_lower = err_msg.lower()
                    
                    # Automatic fallback for format/client issues on retry
                    if ("requested format" in err_lower or "no video formats" in err_lower or "400" in err_lower) and attempt < max_retries:
                        item_opts['format'] = 'bestaudio/best' if audio_only else 'bestvideo+bestaudio/best'
                        item_opts.pop('extractor_args', None)

                    if attempt < max_retries:
                        wait = 2 ** attempt
                        print(f"\r       ⟳ Retry {attempt+1}/{max_retries} in {wait}s...                          ", end='', flush=True)
                        time.sleep(wait)
                    continue

            if interrupted:
                break

            if success:
                # Find downloaded file
                fp = None
                exts = ('.mp3', '.m4a', '.opus', '.flac', '.ogg', '.wav') if audio_only else ('.mp4', '.mkv', '.webm')
                try:
                    for f in sorted(os.listdir(playlist_dir),
                                    key=lambda x: os.path.getmtime(os.path.join(playlist_dir, x)),
                                    reverse=True):
                        if f.lower().endswith(exts) and indexed_name[:10] in f:
                            fp = os.path.join(playlist_dir, f)
                            break
                except Exception:
                    pass
                
                fsize = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
                total_bytes += fsize
                downloaded.append(fp or outtmpl)

                elapsed = time.time() - t_start
                size_str = format_size(fsize) if fsize else "complete"
                _print_playlist_line(idx, total, '✓', video_title, size_str, elapsed, end='\n')
            else:
                elapsed = time.time() - t_start
                err_desc = _clean_error_msg(last_error)
                _print_playlist_line(idx, total, '✗', video_title, err_desc, elapsed, end='\n')
                failed_items.append((idx, video_title[:40], last_error or 'ERROR'))

            # Rate limiting — small delay between downloads to avoid throttling
            if idx < total and not interrupted:
                time.sleep(0.5)

        # ── 7. Summary ─────────────────────────────────────────────────
        elapsed_total = time.time() - t_start
        h_e = int(elapsed_total // 3600)
        m_e = int((elapsed_total % 3600) // 60)
        s_e = int(elapsed_total % 60)
        if h_e:
            time_str = f"{h_e}h {m_e}m {s_e}s"
        elif m_e:
            time_str = f"{m_e}m {s_e}s"
        else:
            time_str = f"{s_e}s"

        print(f"\n  {'═' * 60}")
        if interrupted:
            print(f"  ⚠  Playlist Interrupted")
        else:
            print(f"  ✅  Playlist Complete!")
        print(f"  {'─' * 60}")
        print(f"  📂  {playlist_title[:50]}")
        print(f"  ⏱   Time: {time_str}")
        print(f"  {'─' * 60}")
        print(f"  ✓  Downloaded: {len(downloaded):>4}  │  💾 {format_size(total_bytes)}")
        if skipped:
            print(f"  ⊘  Skipped:    {skipped:>4}  │  (already existed)")
        if failed_items:
            print(f"  ✗  Failed:     {len(failed_items):>4}  │")
        remaining = total - len(downloaded) - skipped - len(failed_items)
        if remaining > 0:
            print(f"  ⏸  Remaining:  {remaining:>4}  │  (interrupted)")
        print(f"  {'─' * 60}")
        print(f"  📁  {playlist_dir}")

        if failed_items:
            print(f"\n  Failed items:")
            for fidx, ftitle, ferror in failed_items[:20]:
                print(f"    #{fidx:<4} {ftitle:<35} {ferror[:25]}")
            if len(failed_items) > 20:
                print(f"    ... and {len(failed_items) - 20} more")

        print(f"  {'═' * 60}\n")

        # ML: playlist summary event
        if self.ml:
            self.ml.log_download("playlist_done", url,
                                 total=total, ok=len(downloaded),
                                 fail=len(failed_items), skip=skipped,
                                 dur=round(elapsed, 1), sz=total_bytes)

        # Desktop notification
        _send_notification(
            "Playlist Download Complete" if not interrupted else "Playlist Interrupted",
            f"✓ {len(downloaded)} / ✗ {len(failed_items)} / ⊘ {skipped} — {time_str}"
        )

        return downloaded
    
    def download_thumbnail(self, url: str, output_dir: str = None) -> Optional[str]:
        """Download thumbnail only"""
        
        platform = detect_platform(url)
        if platform == Platform.YOUTUBE and "list=" in url.lower():
            video_id = extract_video_id(url, platform)
            if video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
        out_dir = output_dir or self._get_output_dir(platform, "thumbnail")
        
        opts = {
            'quiet': True,
            'skip_download': True,
            'writethumbnail': True,
            'noplaylist': True,
            'outtmpl': os.path.join(out_dir, '%(title)s.%(ext)s'),
            'windowsfilenames': True,
        }
        self._apply_js_runtimes(opts)
        
        try:
            print("\n  Fetching thumbnail...")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = sanitize_filename(info.get('title', 'thumbnail'))
                
                for ext in ['jpg', 'jpeg', 'png', 'webp']:
                    path = os.path.join(out_dir, f"{title}.{ext}")
                    if os.path.exists(path):
                        print(f"  ✓ Saved: {path}")
                        return path
                
                # Search for file
                for f in os.listdir(out_dir):
                    if title[:20] in f:
                        path = os.path.join(out_dir, f)
                        print(f"  ✓ Saved: {path}")
                        return path
                
                print(f"  ⚠ Check: {out_dir}")
                return None
                
        except Exception as e:
            print(f"  ✗ Error: {e}")
            return None
    
    def download_subtitles(self, url: str, languages: List[str] = None, output_dir: str = None) -> List[str]:
        """Download subtitles only"""
        
        platform = detect_platform(url)
        if platform == Platform.YOUTUBE and "list=" in url.lower():
            video_id = extract_video_id(url, platform)
            if video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
        out_dir = output_dir or self._get_output_dir(platform, "subtitle")
        languages = languages or Config.SUBTITLE_LANGUAGES
        
        opts = {
            'quiet': True,
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': languages,
            'subtitlesformat': 'srt',
            'noplaylist': True,
            'outtmpl': os.path.join(out_dir, '%(title)s.%(ext)s'),
            'windowsfilenames': True,
        }
        self._apply_js_runtimes(opts)
        
        try:
            print(f"\n  Fetching subtitles ({', '.join(languages)})...")
            
            # For YouTube, try with browser cookies first (silently)
            if platform == Platform.YOUTUBE:
                info = _try_with_cookies(opts, url, download=True)
                if not info:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
            else:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            
            title = sanitize_filename(info.get('title', ''))
            
            files = []
            for f in os.listdir(out_dir):
                if title[:20] in f and f.endswith(('.srt', '.vtt', '.ass')):
                    files.append(os.path.join(out_dir, f))
            
            if files:
                print(f"  ✅ Saved: {len(files)} file(s)")
                for f in files:
                    if SUBTITLE_AI_AVAILABLE and f.endswith('.srt'):
                        try:
                            clean_subtitle_file(f, f)
                            print(f"    📄 {os.path.basename(f)} (cleaned with Subtitle AI)")
                        except Exception:
                            print(f"    📄 {os.path.basename(f)}")
                    else:
                        print(f"    📄 {os.path.basename(f)}")
            else:
                print("  ⚠ No subtitles found")
            
            return files
            
        except Exception as e:
            print(f"  ✗ Error: {e}")
            return []
    
    def batch_download(self, urls: List[str], audio_only: bool = False, quality: str = "1080p") -> Dict[str, str]:
        """Download multiple URLs"""
        
        results = {}
        total = len(urls)
        
        print(f"\n  {'═' * 55}")
        print(f"  Batch Download: {total} URLs")
        print(f"  Mode: {'Audio' if audio_only else 'Video'}")
        print(f"  {'═' * 55}")
        
        for idx, url in enumerate(urls, 1):
            print(f"\n  [{idx}/{total}] {url[:50]}...")
            
            try:
                self.progress.reset()
                
                if audio_only:
                    fp = self.download_audio(url, prompt_redownload=False)
                else:
                    fp = self.download_video(url, quality=quality, prompt_redownload=False)
                
                results[url] = fp or "downloaded"
                
            except Exception as e:
                print(f"  ✗ Failed: {e}")
                results[url] = f"failed: {str(e)}"
        
        success = sum(1 for v in results.values() if not str(v).startswith("failed"))
        
        print(f"\n  {'═' * 55}")
        print(f"  Batch Complete: {success}/{total} successful")
        print(f"  {'═' * 55}")
        
        return results


# ============================================================================
# BACKGROUND DOWNLOAD MANAGER
# ============================================================================

@dataclass
class BackgroundJob:
    """Tracks a single background download"""
    url: str
    status: str = "pending"          # pending, downloading, complete, failed
    temp_filepath: Optional[str] = None
    title: str = ""
    error: str = ""
    started_at: float = 0.0
    progress_pct: float = 0.0
    speed: float = 0.0
    eta: float = 0.0
    downloaded: int = 0
    total: int = 0
    thread: Optional[threading.Thread] = None


class BackgroundDownloadManager:
    """Manages speculative background downloads in temp directory."""

    CLEANUP_INTERVAL = 300    # check every 5 minutes
    MAX_AGE_SECONDS = 1800    # delete unclaimed after 30 minutes

    def __init__(self, downloader: MediaDownloader):
        self.downloader = downloader
        self._jobs: Dict[str, BackgroundJob] = {}
        self._lock = threading.Lock()

        # Start cleanup daemon
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True
        )
        self._cleanup_thread.start()

    # ---- helpers to normalize URL keys ----

    @staticmethod
    def _normalize_key(url: str) -> str:
        url = url.strip()
        platform = detect_platform(url)
        if platform == Platform.YOUTUBE:
            vid = None
            if PLATFORMS_AVAILABLE:
                vid = extract_video_id(url, platform)
            if vid:
                return f"yt:{vid}"
        return url.lower().rstrip('/')

    # ---- public API ----

    def start(self, url: str) -> BackgroundJob:
        """Start a background download for *url*. Returns the job."""
        key = self._normalize_key(url)
        with self._lock:
            if key in self._jobs:
                return self._jobs[key]  # already tracked
            job = BackgroundJob(url=url, started_at=time.time())
            self._jobs[key] = job

        t = threading.Thread(target=self._download_worker, args=(key, job), daemon=True)
        job.thread = t
        t.start()
        return job

    def get_job(self, url: str) -> Optional[BackgroundJob]:
        key = self._normalize_key(url)
        with self._lock:
            return self._jobs.get(key)

    def claim(self, url: str, final_dir: str) -> Optional[str]:
        """Move a completed bg download to *final_dir*. Returns final path."""
        key = self._normalize_key(url)
        with self._lock:
            job = self._jobs.get(key)
            if not job or not job.temp_filepath:
                return None
            src = job.temp_filepath
            if not os.path.isfile(src):
                self._jobs.pop(key, None)
                return None
            os.makedirs(final_dir, exist_ok=True)
            dst = os.path.join(final_dir, os.path.basename(src))
            # avoid overwrite
            if os.path.exists(dst):
                name, ext = os.path.splitext(os.path.basename(src))
                dst = os.path.join(final_dir, f"{name}_{int(time.time())}{ext}")
            try:
                shutil.move(src, dst)
            except Exception:
                shutil.copy2(src, dst)
                try:
                    os.remove(src)
                except Exception:
                    pass
            self._jobs.pop(key, None)
            return dst

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values()
                       if j.status in ('pending', 'downloading'))

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def list_jobs(self) -> List[BackgroundJob]:
        with self._lock:
            return list(self._jobs.values())

    # ---- download worker (runs in thread) ----

    def _download_worker(self, key: str, job: BackgroundJob):
        job.status = 'downloading'
        platform = detect_platform(job.url)
        url = job.url

        # Strip playlist from YouTube single-video URLs
        if platform == Platform.YOUTUBE and 'list=' in url.lower():
            vid = None
            if PLATFORMS_AVAILABLE:
                vid = extract_video_id(url, platform)
            if vid:
                url = f"https://www.youtube.com/watch?v={vid}"

        temp_dir = Config.TEMP_DIR
        os.makedirs(temp_dir, exist_ok=True)

        def _progress_hook(d: Dict):
            if d.get('status') == 'downloading':
                job.downloaded = d.get('downloaded_bytes', 0)
                job.total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                job.speed = d.get('speed', 0) or 0
                job.eta = d.get('eta', 0) or 0
                if job.total > 0:
                    job.progress_pct = (job.downloaded / job.total) * 100
            elif d.get('status') == 'finished':
                job.progress_pct = 100
                fname = d.get('filename', '')
                if fname:
                    job.temp_filepath = fname

        opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [_progress_hook],
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'retries': 3,
            'socket_timeout': 15,
            'extractor_retries': 1,
            'fragment_retries': 3,
            'overwrites': False,
            'windowsfilenames': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'merge_output_format': 'mp4',
            'format': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best',
        }
        self.downloader._apply_js_runtimes(opts)

        # YouTube-specific options
        if platform == Platform.YOUTUBE:
            pass  # Let yt-dlp use its default client order

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    job.title = info.get('title', 'Unknown')[:60]
                ydl.download([url])

            # Find downloaded file if progress hook didn't catch it
            if not job.temp_filepath or not os.path.isfile(job.temp_filepath):
                for f in sorted(os.listdir(temp_dir),
                                key=lambda x: os.path.getmtime(os.path.join(temp_dir, x)),
                                reverse=True):
                    fp = os.path.join(temp_dir, f)
                    if os.path.isfile(fp) and f.endswith(('.mp4', '.mkv', '.webm')):
                        if time.time() - os.path.getmtime(fp) < 120:
                            job.temp_filepath = fp
                            break

            job.status = 'complete'
            job.progress_pct = 100
        except Exception as e:
            job.status = 'failed'
            job.error = str(e)[:200]
            logger.error(f"Background download failed: {e}")

    # ---- cleanup loop (daemon thread) ----

    def _cleanup_loop(self):
        while not self._cleanup_stop.wait(self.CLEANUP_INTERVAL):
            now = time.time()
            to_remove = []
            with self._lock:
                for key, job in self._jobs.items():
                    age = now - job.started_at
                    if age > self.MAX_AGE_SECONDS and job.status in ('complete', 'failed'):
                        # Delete temp file
                        if job.temp_filepath and os.path.isfile(job.temp_filepath):
                            try:
                                os.remove(job.temp_filepath)
                            except Exception:
                                pass
                        to_remove.append(key)
                for key in to_remove:
                    self._jobs.pop(key, None)

    def stop(self):
        self._cleanup_stop.set()


# ============================================================================
# CLI INTERFACE
# ============================================================================

class DownloaderCLI:
    """Command Line Interface"""
    
    def __init__(self):
        self.downloader = MediaDownloader()
        self.bg_manager = BackgroundDownloadManager(self.downloader)
        self.running = True
        
        # Feature toggles (all enabled by default)
        self.features = {
            'bg_predownload': True,
            'preview': True,
            'download_archive': True,
            'embed_thumbnail': True,
            'embed_metadata': True,
            'organize_by_platform': True,
            'organize_by_playlist': True,
            'auto_subtitles': False,
            'geo_bypass': True,
        }
        self._load_feature_settings()
    
    def _feature_settings_path(self) -> str:
        return os.path.join(Config.BASE_DIR, 'feature_settings.json')
    
    def _load_feature_settings(self):
        path = self._feature_settings_path()
        if os.path.isfile(path):
            try:
                with open(path, 'r') as f:
                    saved = json.load(f)
                for k in self.features:
                    if k in saved:
                        self.features[k] = bool(saved[k])
            except Exception:
                pass
        self._sync_features_to_config()
    
    def _save_feature_settings(self):
        path = self._feature_settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump(self.features, f, indent=2)
        except Exception:
            pass
        self._sync_features_to_config()
    
    def _sync_features_to_config(self):
        """Apply feature toggles to Config at runtime."""
        # Download archive: None disables it in yt-dlp
        if self.features.get('download_archive', True):
            Config.DOWNLOAD_ARCHIVE = os.path.join(Config.BASE_DIR, 'download_archive.txt')
        else:
            Config.DOWNLOAD_ARCHIVE = None
        
        # Sync embed settings
        Config.EMBED_THUMBNAIL = self.features.get('embed_thumbnail', True)
        Config.EMBED_METADATA = self.features.get('embed_metadata', True)
        Config.ORGANIZE_BY_PLATFORM = self.features.get('organize_by_platform', True)
        Config.ORGANIZE_BY_PLAYLIST = self.features.get('organize_by_playlist', True)
    
    def print_header(self):
        print(f"""
╔══════════════════════════════════════════════════════════════════╗
║           PERSONAL MEDIA DOWNLOADER v{VERSION}                       ║
║                    Professional Edition                          ║
╠══════════════════════════════════════════════════════════════════╣
║  Platforms: YouTube, Instagram, X, Facebook, Reddit, TikTok     ║
║  Quality: Up to 8K@60fps | Audio: Up to 320kbps/FLAC            ║
╚══════════════════════════════════════════════════════════════════╝
""")
    
    def print_menu(self):
        print("""
MAIN MENU:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  DOWNLOAD:
    1.  Download Video
    2.  Download Audio Only
    3.  Download Playlist (Video)
    4.  Download Playlist (Audio)
    5.  Batch Download (multiple URLs)
    6.  Download Thumbnail
    7.  Download Subtitles
    
  OPTIONS:
    8.  Quality Settings
    9.  View Current Settings
    
  TOOLS:
    10. View Download History
    11. Search Downloads
    12. View Statistics
    13. Resume Failed Downloads
    14. Manage Queue
    15. Open Recent Downloads
    16. Open Downloader Folder
    
  SYSTEM:
    17. Check Dependencies
    18. Verify Directories
    19. Clear Temp Files
    20. Update yt-dlp
    21. Feature Settings
    
  ADVANCED & LOSSLESS DOWNLOADS:
    22. SpotiFLAC Lossless Music    🎵 Spotify / FLAC / 320k / Lyrics
    23. Spotify Search & Explorer   🎵 Browse Songs, Albums, Playlists & Synced Lyrics
    24. Unknown Media Sniffer       🌐 Webpage & Embedded Player (Video+Audio+Subs)
    
    0.  Exit
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
        # Show background download status
        if self.features['bg_predownload']:
            jobs = self.bg_manager.list_jobs()
            if jobs:
                active = sum(1 for j in jobs if j.status == 'downloading')
                done = sum(1 for j in jobs if j.status == 'complete')
                parts = []
                if active:
                    parts.append(f"{active} downloading")
                if done:
                    parts.append(f"{done} ready")
                if parts:
                    print(f"  [BG] {', '.join(parts)}")
                for j in jobs:
                    title = j.title[:40] if j.title else j.url[:40]
                    if j.status == 'downloading':
                        print(f"       >> {title}  {j.progress_pct:.0f}%")
                    elif j.status == 'complete':
                        print(f"       ** {title}  READY (paste URL again to save)")
                    elif j.status == 'failed':
                        print(f"       !! {title}  FAILED")
                print()
    
    def get_url(self) -> str:
        """Get URL from user, with clipboard auto-paste."""
        clip_url = _get_clipboard_url()
        if clip_url:
            print(f"\n  Clipboard: {clip_url[:70]}")
            use_clip = input("  Use this URL? (y/n) [y]: ").strip().lower()
            if use_clip != 'n':
                return clip_url
        print("\n  Enter URL (or 'back'):")
        return input("  > ").strip()
    
    def get_quality(self, available_resolutions: list = None) -> str:
        """Get quality choice. If available_resolutions is provided, show interactive picker."""
        if available_resolutions:
            return self._pick_quality_interactive(available_resolutions)
        print("""
  VIDEO QUALITY:
    1. 8K (4320p)     5. 720p
    2. 4K (2160p)     6. 480p
    3. 1440p          7. 360p
    4. 1080p [Default] 8. Best available
""")
        choice = input("  Select [4]: ").strip() or "4"
        return {'1': '4k', '2': '4k', '3': '1440p', '4': '1080p', 
                '5': '720p', '6': '480p', '7': '360p', '8': 'best'}.get(choice, '1080p')
    
    def _pick_quality_interactive(self, resolutions: list) -> str:
        """Show actually available resolutions and let user pick."""
        # Sort descending
        resolutions = sorted(set(resolutions), reverse=True)
        
        # Map heights to labels
        label_map = {
            4320: '8K (4320p)', 2160: '4K (2160p)', 1440: '1440p (2K)',
            1080: '1080p (Full HD)', 720: '720p (HD)', 480: '480p (SD)',
            360: '360p', 240: '240p', 144: '144p',
        }
        quality_map = {
            4320: '8k', 2160: '4k', 1440: '1440p', 1080: '1080p',
            720: '720p', 480: '480p', 360: '360p', 240: '240p', 144: '144p',
        }
        
        print("\n  AVAILABLE QUALITIES:")
        print(f"  {'─' * 35}")
        
        for i, h in enumerate(resolutions, 1):
            label = label_map.get(h, f"{h}p")
            marker = ' [Recommended]' if h == 1080 else ''
            print(f"    {i}. {label}{marker}")
        
        print(f"    {len(resolutions) + 1}. Best available")
        print(f"  {'─' * 35}")
        
        # Find default (1080p or highest below it)
        default_idx = None
        for i, h in enumerate(resolutions):
            if h <= 1080:
                default_idx = i + 1
                break
        if default_idx is None:
            default_idx = 1
        
        choice = input(f"  Select [{default_idx}]: ").strip() or str(default_idx)
        
        try:
            idx = int(choice)
            if idx == len(resolutions) + 1:
                return 'best'
            if 1 <= idx <= len(resolutions):
                h = resolutions[idx - 1]
                return quality_map.get(h, f"{h}p")
        except ValueError:
            pass
        
        return '1080p'
    
    def get_audio_format(self) -> str:
        print("""
  AUDIO FORMAT:
    1. MP3 [Default]  4. WAV
    2. M4A            5. OPUS
    3. FLAC           6. OGG
""")
        choice = input("  Select [1]: ").strip() or "1"
        return {'1': 'mp3', '2': 'm4a', '3': 'flac', '4': 'wav', 
                '5': 'opus', '6': 'vorbis'}.get(choice, 'mp3')
    
    # ==================== PREVIEW ====================
    
    def _preview_media(self, url: str) -> Optional[Dict]:
        """Fast probe: show title, uploader, duration, available resolutions."""
        print("\n  Fetching info...", end='', flush=True)
        info = self.downloader.get_info(url)
        if not info:
            print(" failed")
            return None
        print(" OK")
        
        title = info.get('title', 'Unknown')
        uploader = info.get('uploader') or info.get('channel') or 'Unknown'
        duration = info.get('duration', 0)
        
        # Get available resolutions (filter out storyboard/thumbnail formats)
        resolutions = set()
        for f in info.get('formats', []) or []:
            h = f.get('height')
            if not h or h <= 0:
                continue
            # Skip storyboard / sprite / screenshot formats
            vcodec = f.get('vcodec', '')
            fmt_note = (f.get('format_note') or '').lower()
            proto = (f.get('protocol') or '').lower()
            ext = (f.get('ext') or '').lower()
            if 'storyboard' in fmt_note or proto == 'mhtml' or ext == 'mhtml':
                continue
            # If vcodec info is available, must be a real video codec
            if vcodec and vcodec == 'none':
                continue
            resolutions.add(h)
        res_list = sorted(resolutions, reverse=True)
        res_str = ', '.join(f"{r}p" for r in res_list[:8])
        
        print(f"\n  {'=' * 60}")
        print(f"  Title:       {title[:55]}")
        print(f"  Uploader:    {uploader}")
        if duration:
            print(f"  Duration:    {format_duration(duration)}")
        if res_str:
            print(f"  Available:   {res_str}")
        print(f"  {'=' * 60}")
        
        return info
    
    # ==================== BG HELPER ====================
    
    def _wait_for_bg_job(self, job: BackgroundJob):
        """Block until a background job finishes, showing live progress."""
        while job.status == 'downloading':
            if job.total > 0:
                pct = job.progress_pct
                bar_len = 40
                filled = int(bar_len * pct / 100)
                bar = chr(9608) * filled + chr(9617) * (bar_len - filled)
                speed_str = format_size(job.speed) + "/s" if job.speed else "---"
                eta_str = format_duration(job.eta) if job.eta else "--:--"
                dl_str = format_size(job.downloaded)
                total_str = format_size(job.total)
                print(f"\r  [{bar}] {pct:5.1f}% | {dl_str}/{total_str} | {speed_str} | ETA: {eta_str}  ", end='', flush=True)
            else:
                print(f"\r  Downloading... {format_size(job.downloaded)}  ", end='', flush=True)
            time.sleep(0.5)
        print()  # newline after progress
    
    # ==================== SUBTITLE PICKER ====================
    
    # Language code to name mapping for common languages
    _LANG_NAMES = {
        'en': 'English', 'es': 'Spanish', 'fr': 'French', 'de': 'German',
        'it': 'Italian', 'pt': 'Portuguese', 'ru': 'Russian', 'ja': 'Japanese',
        'ko': 'Korean', 'zh': 'Chinese', 'zh-Hans': 'Chinese (Simplified)',
        'zh-Hant': 'Chinese (Traditional)', 'hi': 'Hindi', 'ar': 'Arabic',
        'nl': 'Dutch', 'pl': 'Polish', 'sv': 'Swedish', 'da': 'Danish',
        'no': 'Norwegian', 'fi': 'Finnish', 'tr': 'Turkish', 'th': 'Thai',
        'vi': 'Vietnamese', 'id': 'Indonesian', 'ms': 'Malay', 'tl': 'Filipino',
        'uk': 'Ukrainian', 'cs': 'Czech', 'el': 'Greek', 'he': 'Hebrew',
        'hu': 'Hungarian', 'ro': 'Romanian', 'bg': 'Bulgarian', 'hr': 'Croatian',
        'sk': 'Slovak', 'sl': 'Slovenian', 'sr': 'Serbian', 'lt': 'Lithuanian',
        'lv': 'Latvian', 'et': 'Estonian', 'bn': 'Bengali', 'ta': 'Tamil',
        'te': 'Telugu', 'mr': 'Marathi', 'gu': 'Gujarati', 'kn': 'Kannada',
        'ml': 'Malayalam', 'pa': 'Punjabi', 'ur': 'Urdu', 'fa': 'Persian',
        'af': 'Afrikaans', 'sw': 'Swahili', 'ca': 'Catalan', 'eu': 'Basque',
        'gl': 'Galician', 'cy': 'Welsh', 'ga': 'Irish', 'is': 'Icelandic',
        'mk': 'Macedonian', 'sq': 'Albanian', 'bs': 'Bosnian', 'mt': 'Maltese',
        'la': 'Latin', 'eo': 'Esperanto', 'jv': 'Javanese', 'su': 'Sundanese',
        'my': 'Burmese', 'km': 'Khmer', 'lo': 'Lao', 'ne': 'Nepali',
        'si': 'Sinhala', 'am': 'Amharic', 'ka': 'Georgian', 'hy': 'Armenian',
        'az': 'Azerbaijani', 'uz': 'Uzbek', 'kk': 'Kazakh', 'mn': 'Mongolian',
        'ky': 'Kyrgyz', 'tg': 'Tajik', 'ps': 'Pashto', 'ku': 'Kurdish',
        'so': 'Somali', 'ha': 'Hausa', 'yo': 'Yoruba', 'ig': 'Igbo',
        'zu': 'Zulu', 'xh': 'Xhosa',
    }
    
    def _get_lang_name(self, code: str) -> str:
        """Get human-readable language name from code."""
        # Try exact match first
        name = self._LANG_NAMES.get(code)
        if name:
            return name
        # Try base code (e.g., 'en-US' -> 'en')
        base = code.split('-')[0]
        name = self._LANG_NAMES.get(base)
        if name:
            variant = code[len(base)+1:] if len(code) > len(base) else ''
            return f"{name} ({variant})" if variant else name
        return code
    
    def _select_subtitles(self, url: str, info: dict = None) -> Optional[List[str]]:
        """Show available subtitles with AI language recommendation and let user select."""
        
        # Get subtitle info from already-fetched info or fetch fresh
        if not info:
            print("\n  Fetching subtitle info...", end='', flush=True)
            info = self.downloader.get_info(url)
            if not info:
                print(" failed")
                return None
            print(" OK")
        
        # Collect available subtitles
        manual_subs = info.get('subtitles') or {}
        auto_subs = info.get('automatic_captions') or {}
        
        if not manual_subs and not auto_subs:
            print("\n  ⚠ No subtitles available for this video.")
            return None
        
        # Detect user's language for smart recommendation
        user_lang = detect_user_language() if SUBTITLE_AI_AVAILABLE else 'en'
        user_lang_name = self._get_lang_name(user_lang)
        
        # Build display list: (code, name, type)
        sub_entries = []
        
        # Manual (human-uploaded) subtitles first
        manual_codes = sorted(manual_subs.keys())
        if SUBTITLE_AI_AVAILABLE:
            manual_codes = recommend_languages(manual_codes, user_lang)
        for code in manual_codes:
            name = self._get_lang_name(code)
            sub_entries.append((code, name, 'manual'))
        
        # Auto-generated subtitles
        auto_codes = sorted(auto_subs.keys())
        if SUBTITLE_AI_AVAILABLE:
            auto_codes = recommend_languages(
                [c for c in auto_codes if c not in manual_subs], user_lang
            )
        else:
            auto_codes = [c for c in auto_codes if c not in manual_subs]
        for code in auto_codes:
            name = self._get_lang_name(code)
            sub_entries.append((code, name, 'auto'))
        
        if not sub_entries:
            print("\n  ⚠ No subtitles available for this video.")
            return None
        
        # Display available subtitles — enhanced UI with emojis
        manual_count = sum(1 for _, _, t in sub_entries if t == 'manual')
        auto_count = sum(1 for _, _, t in sub_entries if t == 'auto')
        
        print(f"\n  {'═' * 58}")
        print(f"  🎬 AVAILABLE SUBTITLES")
        if SUBTITLE_AI_AVAILABLE:
            print(f"  🌐 Your language: {user_lang_name} [{user_lang}]")
        print(f"  {'─' * 58}")
        
        if manual_count > 0:
            print(f"  📝 Manual subtitles ({manual_count}):")
            idx = 1
            for code, name, stype in sub_entries:
                if stype == 'manual':
                    label = get_recommendation_label(code, user_lang) if SUBTITLE_AI_AVAILABLE else ''
                    star = ' ⭐' if '★' in label else ''
                    badge = label.replace(' ★ recommended', '') if label else ''
                    if SUBTITLE_AI_AVAILABLE and code.split('-')[0].lower() == user_lang:
                        print(f"    {idx:3}. [{code:10}]  {name} ⭐ recommended")
                    elif SUBTITLE_AI_AVAILABLE and code.split('-')[0].lower() == 'en' and user_lang != 'en':
                        print(f"    {idx:3}. [{code:10}]  {name} 🌍 universal")
                    else:
                        print(f"    {idx:3}. [{code:10}]  {name}")
                    idx += 1
        
        if auto_count > 0:
            if manual_count > 0:
                print(f"  {'─' * 58}")
            print(f"  🤖 Auto-generated ({auto_count} available):")
            
            # Show recommended + popular auto subs
            auto_entries = [(c, n) for c, n, t in sub_entries if t == 'auto']
            shown_count = 0
            
            # Show user's language first if available
            for c, n in auto_entries:
                base = c.split('-')[0].lower()
                if base == user_lang and shown_count < 12:
                    print(f"       [{c:10}]  {n} ⭐ recommended")
                    shown_count += 1
                    break
            
            # Show popular languages
            popular = ['en', 'es', 'fr', 'de', 'ja', 'ko', 'pt', 'ru', 'hi', 'zh', 'ar', 'it']
            for pc in popular:
                if pc == user_lang:
                    continue  # Already shown
                for c, n in auto_entries:
                    if c == pc or c.startswith(pc + '-'):
                        if shown_count < 12:
                            if pc == 'en' and user_lang != 'en':
                                print(f"       [{c:10}]  {n} 🌍")
                            else:
                                print(f"       [{c:10}]  {n}")
                            shown_count += 1
                        break
            
            remaining = auto_count - shown_count
            if remaining > 0:
                print(f"       ... and {remaining} more languages")
            print(f"    💡 Type any language code (e.g. de, sv, nl)")
        
        print(f"  {'─' * 58}")
        print(f"  📋 Options: Enter numbers (1-{manual_count}), 'all' for all manual,")
        print(f"              or language codes (e.g. en,es,ja)")
        print(f"  {'═' * 58}")
        
        selection = input("\n  Select subtitles: ").strip()
        
        if not selection:
            return None
        
        selected_codes = []
        
        if selection.lower() == 'all':
            # Return all manual subtitle codes
            selected_codes = [code for code, _, stype in sub_entries if stype == 'manual']
        else:
            parts = [p.strip() for p in selection.split(',')]
            for part in parts:
                if part.isdigit():
                    idx = int(part)
                    if 1 <= idx <= manual_count:
                        selected_codes.append(sub_entries[idx - 1][0])
                    else:
                        print(f"  ⚠ Invalid number: {part} (valid: 1-{manual_count})")
                else:
                    # Treat as language code
                    code = part.lower()
                    # Check if it's a valid available code
                    available_codes = [c for c, _, _ in sub_entries]
                    if code in available_codes:
                        selected_codes.append(code)
                    else:
                        # Check partial match (e.g., 'en' matches 'en-US')
                        matches = [c for c in available_codes if c.startswith(code)]
                        if matches:
                            selected_codes.extend(matches)
                        else:
                            print(f"  ⚠ Subtitle not available: {part}")
        
        if selected_codes:
            # Remove duplicates while preserving order
            seen = set()
            unique = []
            for c in selected_codes:
                if c not in seen:
                    seen.add(c)
                    unique.append(c)
            selected_codes = unique
            
            names = [self._get_lang_name(c) for c in selected_codes]
            print(f"\n  ✅ Selected: {', '.join(names)}")
            return selected_codes
        
        return None
    
    # ==================== HANDLERS ====================
    
    def handle_video(self):
        url = self.get_url()
        if url.lower() == 'back':
            return
        
        platform = detect_platform(url)
        
        # Check for existing background download
        job = self.bg_manager.get_job(url)
        if job:
            if job.status == 'complete':
                out_dir = self.downloader._get_output_dir(platform, "video")
                final = self.bg_manager.claim(url, out_dir)
                if final:
                    filesize = os.path.getsize(final) if os.path.exists(final) else 0
                    self.downloader.db.add_download(
                        url=url, platform=platform.value, title=job.title,
                        filesize=filesize, filepath=final, status='complete'
                    )
                    print(f"\n  Background download complete!")
                    print(f"  Saved to: {final}")
                    if filesize:
                        print(f"  Size: {format_size(filesize)}")
                    input("\n  Press Enter to continue...")
                    return
            elif job.status == 'downloading':
                print(f"\n  Background download in progress...")
                self._wait_for_bg_job(job)
                if job.status == 'complete':
                    out_dir = self.downloader._get_output_dir(platform, "video")
                    final = self.bg_manager.claim(url, out_dir)
                    if final:
                        filesize = os.path.getsize(final) if os.path.exists(final) else 0
                        self.downloader.db.add_download(
                            url=url, platform=platform.value, title=job.title,
                            filesize=filesize, filepath=final, status='complete'
                        )
                        print(f"\n  Saved to: {final}")
                        if filesize:
                            print(f"  Size: {format_size(filesize)}")
                        input("\n  Press Enter to continue...")
                        return
                else:
                    print(f"\n  Background download failed, starting fresh...")
        
        # Normal flow
        print(f"\n  Platform: {platform.value}")
        
        # Preview before download — also collect available resolutions
        available_res = None
        preview_info = None
        if self.features.get('preview', True) and Config.PREVIEW_BEFORE_DOWNLOAD:
            preview_info = self._preview_media(url)
            if preview_info:
                # Collect available resolutions for interactive picker
                available_res = []
                for f in preview_info.get('formats', []) or []:
                    vc = f.get('vcodec')
                    if vc and vc != 'none' and f.get('height'):
                        available_res.append(f['height'])
                available_res = sorted(set(available_res), reverse=True) if available_res else None
                
                confirm = input("\n  Download? (y/n) [y]: ").strip().lower()
                if confirm == 'n':
                    print("  Cancelled.")
                    input("\n  Press Enter to continue...")
                    return
        
        quality = self.get_quality(available_res)
        
        # Subtitle selection
        subs = False
        subtitle_langs = None
        want_subs = input("\n  Download subtitles? (y/n) [n]: ").strip().lower() == 'y'
        if want_subs:
            subtitle_langs = self._select_subtitles(url, preview_info)
            if subtitle_langs:
                subs = True
            else:
                print("  No subtitles selected.")
        
        try:
            self.downloader.download_video(url, quality=quality, subtitles=subs, subtitle_langs=subtitle_langs)
        except DownloadError as e:
            print(f"\n  \u2717 Error: {e}")
        
        input("\n  Press Enter to continue...")
    
    def handle_audio(self):
        url = self.get_url()
        if url.lower() == 'back':
            return
        
        platform = detect_platform(url)
        
        # Check for existing background download (bg downloads are video;
        # we can extract audio from the downloaded video via ffmpeg)
        job = self.bg_manager.get_job(url)
        if job:
            if job.status == 'downloading':
                print(f"\n  Background download in progress...")
                self._wait_for_bg_job(job)

            if job.status == 'complete' and job.temp_filepath and os.path.isfile(job.temp_filepath):
                fmt = self.get_audio_format()
                out_dir = self.downloader._get_output_dir(platform, "audio")
                os.makedirs(out_dir, exist_ok=True)

                src = job.temp_filepath
                name_base = os.path.splitext(os.path.basename(src))[0]
                dst = os.path.join(out_dir, f"{name_base}.{fmt}")

                print(f"\n  Extracting audio from background download...")
                try:
                    import subprocess as _sp
                    _sp.run(
                        ['ffmpeg', '-i', src, '-vn', '-acodec',
                         'libmp3lame' if fmt == 'mp3' else 'copy',
                         '-q:a', '2', '-y', dst],
                        capture_output=True, timeout=300
                    )
                    if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                        filesize = os.path.getsize(dst)
                        self.downloader.db.add_download(
                            url=url, platform=platform.value, title=job.title,
                            format=fmt, filesize=filesize, filepath=dst,
                            media_type='audio', status='complete'
                        )
                        # Clean up temp video
                        self.bg_manager.claim(url, out_dir)  # removes from tracker
                        try:
                            os.remove(src)
                        except Exception:
                            pass
                        print(f"  Done! {dst}")
                        print(f"  Size: {format_size(filesize)}")
                        input("\n  Press Enter to continue...")
                        return
                except Exception as e:
                    print(f"  Audio extraction failed: {e}")
                    print("  Falling back to direct audio download...")

        # Normal flow
        print(f"\n  Platform: {platform.value}")
        
        # Preview before download
        if self.features.get('preview', True) and Config.PREVIEW_BEFORE_DOWNLOAD:
            preview_info = self._preview_media(url)
            if preview_info:
                confirm = input("\n  Download? (y/n) [y]: ").strip().lower()
                if confirm == 'n':
                    print("  Cancelled.")
                    input("\n  Press Enter to continue...")
                    return
        
        fmt = self.get_audio_format()
        
        try:
            self.downloader.download_audio(url, audio_format=fmt)
        except DownloadError as e:
            print(f"\n  \u2717 Error: {e}")
        
        input("\n  Press Enter to continue...")
    
    def handle_playlist_video(self):
        url = self.get_url()
        if url.lower() == 'back':
            return
        
        quality = self.get_quality()
        
        print("\n  Range (empty = download all):")
        start = input("  Start #: ").strip()
        end = input("  End #:   ").strip()
        
        start = int(start) if start.isdigit() else None
        end = int(end) if end.isdigit() else None
        skip = input("  Skip already downloaded? (y/n) [y]: ").strip().lower() != 'n'
        
        try:
            self.downloader.download_playlist(
                url, quality=quality, audio_only=False,
                start=start, end=end, skip_existing=skip
            )
        except DownloadError as e:
            print(f"\n  ✗ Error: {e}")
        except KeyboardInterrupt:
            print(f"\n  ⚠ Interrupted by user")
        
        input("\n  Press Enter to continue...")
    
    def handle_playlist_audio(self):
        url = self.get_url()
        if url.lower() == 'back':
            return
        
        print("\n  Audio format:")
        print("    1. MP3  (default)")
        print("    2. M4A/AAC")
        print("    3. FLAC")
        print("    4. OPUS")
        fmt_choice = input("  Choice [1]: ").strip()
        fmt_map = {'1': 'mp3', '2': 'm4a', '3': 'flac', '4': 'opus'}
        audio_format = fmt_map.get(fmt_choice, 'mp3')
        
        print("\n  Range (empty = download all):")
        start = input("  Start #: ").strip()
        end = input("  End #:   ").strip()
        
        start = int(start) if start.isdigit() else None
        end = int(end) if end.isdigit() else None
        skip = input("  Skip already downloaded? (y/n) [y]: ").strip().lower() != 'n'
        
        try:
            self.downloader.download_playlist(
                url, audio_only=True, audio_format=audio_format,
                start=start, end=end, skip_existing=skip
            )
        except DownloadError as e:
            print(f"\n  ✗ Error: {e}")
        except KeyboardInterrupt:
            print(f"\n  ⚠ Interrupted by user")
        
        input("\n  Press Enter to continue...")
    
    def handle_batch(self):
        print("\n  Enter URLs (one per line, empty to finish):")
        urls = []
        while True:
            u = input("  > ").strip()
            if not u:
                break
            urls.append(u)
        
        if not urls:
            return
        
        audio_only = input(f"\n  {len(urls)} URLs. Audio only? (y/n) [n]: ").strip().lower() == 'y'
        quality = self.get_quality() if not audio_only else "best"
        
        try:
            self.downloader.batch_download(urls, audio_only=audio_only, quality=quality)
        except Exception as e:
            print(f"\n  ✗ Error: {e}")
        
        input("\n  Press Enter to continue...")
    
    def handle_thumbnail(self):
        url = self.get_url()
        if url.lower() == 'back':
            return
        
        self.downloader.download_thumbnail(url)
        input("\n  Press Enter to continue...")
    
    def handle_subtitles(self):
        url = self.get_url()
        if url.lower() == 'back':
            return
        
        # Use interactive subtitle picker
        selected = self._select_subtitles(url)
        if not selected:
            print("  No subtitles selected.")
            input("\n  Press Enter to continue...")
            return
        
        self.downloader.download_subtitles(url, languages=selected)
        input("\n  Press Enter to continue...")
    
    def handle_quality_settings(self):
        if QUALITY_AVAILABLE:
            selector = QualitySelector(self.downloader.quality)
            selector.select_preset()
            self.downloader.quality.save_settings()
        else:
            print("\n  Quality manager not available.")
        input("\n  Press Enter to continue...")
    
    def handle_view_settings(self):
        print(f"\n  {'═' * 55}")
        print("  CURRENT SETTINGS")
        print(f"  {'─' * 55}")
        print(f"  Video Directory:      {Config.VIDEO_DIR}")
        print(f"  Audio Directory:      {Config.AUDIO_DIR}")
        print(f"  Thumbnail Directory:  {Config.THUMBNAIL_DIR}")
        print(f"  Subtitle Directory:   {Config.SUBTITLE_DIR}")
        print(f"  {'─' * 55}")
        print(f"  Embed Thumbnail:      {'ON' if Config.EMBED_THUMBNAIL else 'OFF'}")
        print(f"  Embed Metadata:       {'ON' if Config.EMBED_METADATA else 'OFF'}")
        print(f"  Organize by Platform: {'ON' if Config.ORGANIZE_BY_PLATFORM else 'OFF'}")
        print(f"  Organize by Playlist: {'ON' if Config.ORGANIZE_BY_PLAYLIST else 'OFF'}")
        print(f"  Download Archive:     {'ON' if Config.DOWNLOAD_ARCHIVE else 'OFF'}")
        print(f"  Preview Before DL:    {'ON' if Config.PREVIEW_BEFORE_DOWNLOAD else 'OFF'}")
        print(f"  Background Pre-DL:    {'ON' if self.features.get('bg_predownload') else 'OFF'}")
        print(f"  Geo Bypass:           {'ON' if self.features.get('geo_bypass', True) else 'OFF'}")
        print(f"  Auto Subtitles:       {'ON' if self.features.get('auto_subtitles') else 'OFF'}")
        print(f"  Output Template:      {Config.OUTPUT_TEMPLATE}")
        print(f"  {'─' * 55}")
        print(f"  yt-dlp Version:       {YTDLP_VERSION or 'Not installed'}")
        print(f"  Free Disk Space:      {get_free_space_mb(Config.BASE_DIR)} MB")
        print(f"  {'═' * 55}")
        
        if QUALITY_AVAILABLE:
            self.downloader.quality.print_settings()
        
        input("\n  Press Enter to continue...")
    
    def handle_history(self):
        downloads = self.downloader.db.get_downloads(limit=50)
        
        if not downloads:
            print("\n  No downloads found.")
        else:
            print(f"\n  {'═' * 70}")
            print("  DOWNLOAD HISTORY")
            print(f"  {'─' * 70}")
            
            for d in downloads:
                title = getattr(d, 'title', 'Unknown')[:40]
                platform = getattr(d, 'platform', '?')[:10]
                status = getattr(d, 'status', '?')
                icon = '✓' if status == 'complete' else '✗'
                size = format_size(getattr(d, 'filesize', 0) or 0)
                print(f"  {icon} [{platform:10}] {title:42} {size:>10}")
            
            print(f"  {'═' * 70}")
        
        input("\n  Press Enter to continue...")
    
    def handle_search(self):
        query = input("\n  Search: ").strip()
        if not query:
            return
        
        results = self.downloader.db.search_downloads(query)
        
        if not results:
            print(f"\n  No results for '{query}'")
        else:
            print(f"\n  Found {len(results)} result(s):")
            for r in results:
                title = getattr(r, 'title', 'Unknown')
                print(f"  • {title}")
        
        input("\n  Press Enter to continue...")
    
    def handle_statistics(self):
        stats = self.downloader.db.get_statistics()
        
        print(f"\n  {'═' * 45}")
        print("  STATISTICS")
        print(f"  {'─' * 45}")
        print(f"  Total Downloads: {stats.get('total_downloads', 0)}")
        print(f"  Total Size:      {stats.get('total_size_human', '0 B')}")
        print(f"  Failed:          {stats.get('failed_downloads', 0)}")
        print(f"  {'─' * 45}")
        print("  BY PLATFORM:")
        for p, c in stats.get('by_platform', {}).items():
            print(f"    {p}: {c}")
        print(f"  {'─' * 45}")
        print(f"  Free Space: {get_free_space_mb(Config.BASE_DIR)} MB")
        print(f"  {'═' * 45}")
        
        input("\n  Press Enter to continue...")
    
    def handle_failed(self):
        failed = self.downloader.db.get_failed_downloads()
        
        if not failed:
            print("\n  No failed downloads.")
            input("\n  Press Enter to continue...")
            return
        
        print(f"\n  {len(failed)} failed download(s):")
        for i, d in enumerate(failed, 1):
            title = getattr(d, 'title', 'Unknown')[:40]
            print(f"  {i}. {title}")
        
        if input("\n  Retry all? (y/n): ").strip().lower() == 'y':
            for d in failed:
                url = getattr(d, 'url', None)
                if url:
                    try:
                        self.downloader.download_video(url)
                    except:
                        pass
        
        input("\n  Press Enter to continue...")
    
    def handle_queue(self):
        queue = self.downloader.db.get_queue()
        
        print(f"\n  {'═' * 55}")
        print(f"  DOWNLOAD QUEUE ({len(queue)} items)")
        print(f"  {'─' * 55}")
        
        if queue:
            for q in queue[:10]:
                url = getattr(q, 'url', '')[:50]
                print(f"  • {url}")
        else:
            print("  Queue is empty.")
        
        print(f"\n  1. Add URL")
        print("  2. Process queue")
        print("  3. Clear queue")
        print("  4. Back")
        
        choice = input("\n  Select: ").strip()
        
        if choice == '1':
            url = input("  URL: ").strip()
            if url:
                self.downloader.db.add_to_queue(url)
                print("  ✓ Added")
        elif choice == '2':
            for q in queue:
                url = getattr(q, 'url', None)
                qid = getattr(q, 'id', None)
                if url:
                    try:
                        self.downloader.download_video(url)
                        self.downloader.db.update_queue_status(qid, 'complete')
                    except:
                        self.downloader.db.update_queue_status(qid, 'failed')
        elif choice == '3':
            self.downloader.db.clear_queue()
            print("  ✓ Cleared")
        
        input("\n  Press Enter to continue...")
    
    def handle_dependencies(self):
        print(f"\n  {'═' * 45}")
        print("  DEPENDENCIES")
        print(f"  {'─' * 45}")
        
        print(f"  {'✓' if YTDLP_AVAILABLE else '✗'} yt-dlp: {YTDLP_VERSION or 'Not found'}")
        print(f"  {'✓' if check_ffmpeg() else '✗'} FFmpeg")
        print(f"  {'✓' if CONFIG_AVAILABLE else '○'} config.py")
        print(f"  {'✓' if DATABASE_AVAILABLE else '○'} database.py")
        print(f"  {'✓' if PLATFORMS_AVAILABLE else '○'} platforms.py")
        print(f"  {'✓' if QUALITY_AVAILABLE else '○'} quality_manager.py")
        print(f"  {'✓' if UTILS_AVAILABLE else '○'} utils.py")
        print(f"  {'✓' if UNIVERSAL_AVAILABLE else '○'} universal_downloader.py")
        print(f"  {'✓' if SUBTITLE_AI_AVAILABLE else '○'} subtitle_ai.py")
        print(f"  {'✓' if SPOTIFLAC_AVAILABLE else '○'} spotiflac.py")
        print(f"  {'✓' if SEARCH_ENGINE_AVAILABLE else '○'} search_engine.py")
        print(f"  {'─' * 45}")
        print(f"  Python: {sys.version.split()[0]}")
        print(f"  {'═' * 45}")
        
        input("\n  Press Enter to continue...")
    
    def handle_directories(self):
        print(f"\n  {'═' * 55}")
        print("  DIRECTORIES")
        print(f"  {'─' * 55}")
        
        dirs = [
            ("Base", Config.BASE_DIR),
            ("Videos", Config.VIDEO_DIR),
            ("Audio", Config.AUDIO_DIR),
            ("Thumbnails", Config.THUMBNAIL_DIR),
            ("Subtitles", Config.SUBTITLE_DIR),
            ("Temp", Config.TEMP_DIR),
        ]
        
        all_ok = True
        for name, path in dirs:
            exists = os.path.exists(path)
            icon = '✓' if exists else '✗'
            if not exists:
                all_ok = False
            print(f"  {icon} {name}: {path}")
        
        if not all_ok:
            if input("\n  Create missing? (y/n): ").strip().lower() == 'y':
                Config.init_directories()
                print("  ✓ Created!")
        
        print(f"  {'═' * 55}")
        input("\n  Press Enter to continue...")
    
    def handle_clear_temp(self):
        import shutil
        
        temp = Config.TEMP_DIR
        if os.path.exists(temp):
            files = os.listdir(temp)
            if files:
                for f in files:
                    try:
                        p = os.path.join(temp, f)
                        if os.path.isfile(p):
                            os.remove(p)
                        else:
                            shutil.rmtree(p)
                    except:
                        pass
                print(f"\n  ✓ Cleared {len(files)} item(s)")
            else:
                print("\n  Temp is empty")
        else:
            print("\n  Temp directory not found")
        
        input("\n  Press Enter to continue...")
    
    def _find_vlc(self) -> Optional[str]:
        """Find VLC media player executable"""
        vlc_paths = [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\VideoLAN\VLC\vlc.exe"),
        ]
        for p in vlc_paths:
            if os.path.isfile(p):
                return p
        # Try PATH
        vlc_in_path = shutil.which("vlc")
        if vlc_in_path:
            return vlc_in_path
        return None
    
    def _open_in_vlc(self, filepath: str) -> bool:
        """Open a file in VLC. Returns True on success."""
        import subprocess
        vlc = self._find_vlc()
        if not vlc:
            return False
        try:
            subprocess.Popen([vlc, filepath], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False
    
    def handle_open_recent(self):
        """Open a recently downloaded file in VLC"""
        import subprocess
        
        # Gather files from video and audio directories
        all_files = []
        for search_dir in [Config.VIDEO_DIR, Config.AUDIO_DIR]:
            if not os.path.exists(search_dir):
                continue
            for root, dirs, files in os.walk(search_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    _, ext = os.path.splitext(f)
                    if ext.lower() in ['.mp4', '.mkv', '.webm', '.avi', '.mov',
                                       '.mp3', '.m4a', '.flac', '.wav', '.opus', '.ogg']:
                        all_files.append(fp)
        
        if not all_files:
            print("\n  No downloaded files found.")
            input("\n  Press Enter to continue...")
            return
        
        # Sort by modification time (newest first)
        all_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        recent = all_files[:10]
        
        vlc = self._find_vlc()
        player_label = "VLC" if vlc else "Default Player"
        
        print(f"\n  {'═' * 65}")
        print(f"  RECENT DOWNLOADS  (opens with {player_label})")
        print(f"  {'─' * 65}")
        
        for i, fp in enumerate(recent, 1):
            name = os.path.basename(fp)
            size = format_size(os.path.getsize(fp))
            mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%Y-%m-%d %H:%M')
            display_name = name[:45] + '...' if len(name) > 48 else name
            print(f"  {i:2}. {display_name:48} {size:>10}  {mtime}")
        
        print(f"  {'═' * 65}")
        print("\n   0. Back")
        
        choice = input("\n  Open # (or 0 to go back): ").strip()
        
        if choice == '0' or not choice:
            return
        
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(recent):
                filepath = recent[idx]
                print(f"\n  ▶ Opening in {player_label}: {os.path.basename(filepath)}")
                if not self._open_in_vlc(filepath):
                    print("  ⚠ VLC not found, using system default...")
                    os.startfile(filepath)
            else:
                print("\n  Invalid selection.")
        except ValueError:
            print("\n  Invalid input.")
        
        input("\n  Press Enter to continue...")
    
    def handle_open_folder(self):
        """Open the downloader folder in file explorer"""
        import subprocess
        
        print(f"\n  {'═' * 55}")
        print("  OPEN FOLDER")
        print(f"  {'─' * 55}")
        print(f"  1. Base Directory   ({Config.BASE_DIR})")
        print(f"  2. Videos           ({Config.VIDEO_DIR})")
        print(f"  3. Audio            ({Config.AUDIO_DIR})")
        print(f"  4. Thumbnails       ({Config.THUMBNAIL_DIR})")
        print(f"  5. Subtitles        ({Config.SUBTITLE_DIR})")
        print(f"  {'═' * 55}")
        print("\n  0. Back")
        
        choice = input("\n  Select folder [1]: ").strip() or '1'
        
        folder_map = {
            '1': Config.BASE_DIR,
            '2': Config.VIDEO_DIR,
            '3': Config.AUDIO_DIR,
            '4': Config.THUMBNAIL_DIR,
            '5': Config.SUBTITLE_DIR,
        }
        
        if choice == '0':
            return
        
        folder = folder_map.get(choice)
        if folder:
            if not os.path.exists(folder):
                os.makedirs(folder, exist_ok=True)
            print(f"\n  Opening: {folder}")
            os.startfile(folder)
        else:
            print("\n  Invalid selection.")
        
        input("\n  Press Enter to continue...")
    
    def handle_update(self):
        """Update yt-dlp to latest nightly/pre-release"""
        import subprocess
        
        print(f"\n  {'═' * 55}")
        print("  UPDATE YT-DLP")
        print(f"  {'─' * 55}")
        
        # --- Current installed version ---
        current_ver = YTDLP_VERSION or "Not installed"
        print(f"  Installed Version:  {current_ver}")
        
        print(f"  {'─' * 55}")
        print(f"  1. Update to latest nightly (recommended)")
        print(f"  2. Update to latest stable")
        print(f"  3. Force reinstall (nightly)")
        print(f"  0. Back")
        print(f"  {'─' * 55}")
        
        choice = input("\n  Select [1]: ").strip() or '1'
        
        if choice == '0':
            return
        
        if choice == '1':
            pip_args = [sys.executable, '-m', 'pip', 'install', '--pre', '-U', 'yt-dlp']
            label = 'nightly'
        elif choice == '2':
            pip_args = [sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp']
            label = 'stable'
        elif choice == '3':
            pip_args = [sys.executable, '-m', 'pip', 'install', '--pre', '-U', '--force-reinstall', 'yt-dlp']
            label = 'nightly (force reinstall)'
        else:
            print("  Invalid choice.")
            input("\n  Press Enter to continue...")
            return
        
        print(f"\n  ⏳ Updating yt-dlp ({label})...", end="", flush=True)
        
        # Spinner thread
        stop_spinner = threading.Event()
        def spin():
            chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            i = 0
            while not stop_spinner.is_set():
                print(f"\r  {chars[i % len(chars)]} Updating yt-dlp ({label})...", end="", flush=True)
                i += 1
                time.sleep(0.1)
        
        spinner = threading.Thread(target=spin, daemon=True)
        spinner.start()
        
        try:
            result = subprocess.run(
                pip_args,
                capture_output=True,
                text=True,
                timeout=180
            )
            stop_spinner.set()
            spinner.join()
            
            if result.returncode == 0:
                # Get new version
                new_ver = None
                try:
                    ver_result = subprocess.run(
                        [sys.executable, '-c', 'import yt_dlp; print(yt_dlp.version.__version__)'],
                        capture_output=True, text=True, timeout=10
                    )
                    if ver_result.returncode == 0:
                        new_ver = ver_result.stdout.strip()
                except Exception:
                    pass
                
                print(f"\r  {'─' * 55}                    ")
                print(f"  ✓ Update successful!")
                if new_ver:
                    print(f"  Previous Version:   {current_ver}")
                    print(f"  Updated Version:    {new_ver}")
                    if new_ver != current_ver:
                        print(f"\n  ⚠ Restart the downloader to use the new version.")
                    else:
                        print(f"\n  ✓ Already on the latest {label} version.")
                else:
                    print(f"  ✓ yt-dlp has been updated.")
            else:
                print(f"\r  {'─' * 55}                    ")
                err = result.stderr.strip().split('\n')[-1][:80] if result.stderr else "Unknown error"
                print(f"  ✗ Update failed: {err}")
                
        except subprocess.TimeoutExpired:
            stop_spinner.set()
            spinner.join()
            print(f"\r  ✗ Update timed out after 180 seconds.            ")
        except Exception as e:
            stop_spinner.set()
            spinner.join()
            print(f"\r  ✗ Error: {e}                                     ")
        
        print(f"  {'═' * 55}")
        input("\n  Press Enter to continue...")
    def handle_feature_settings(self):
        """Toggle features on/off — comprehensive settings for all downloader features"""
        while True:
            labels = {
                'embed_thumbnail':      'Embed Thumbnail in Files',
                'embed_metadata':       'Embed Metadata (title, artist, etc.)',
                'organize_by_platform': 'Organize Downloads by Platform',
                'organize_by_playlist': 'Organize Playlists in Sub-folders',
                'download_archive':     'Download Archive (skip duplicates)',
                'preview':              'Preview Info Before Download',
                'bg_predownload':       'Background Pre-Download (paste URL at menu)',
                'auto_subtitles':       'Auto-download Subtitles',
                'geo_bypass':           'Geo-restriction Bypass',
            }
            
            keys = list(labels.keys())
            
            print(f"\n  {'═' * 60}")
            print("  FEATURE SETTINGS")
            print(f"  {'═' * 60}")
            
            # Group: Download Options
            print("  DOWNLOAD OPTIONS:")
            dl_keys = ['embed_thumbnail', 'embed_metadata', 'auto_subtitles']
            for key in dl_keys:
                idx = keys.index(key) + 1
                state = 'ON' if self.features.get(key, False) else 'OFF'
                icon = '✓' if self.features.get(key, False) else '✗'
                print(f"    {idx:2}. [{icon}] {labels[key]:45} {state}")
            
            # Group: File Organization
            print("  FILE ORGANIZATION:")
            org_keys = ['organize_by_platform', 'organize_by_playlist', 'download_archive']
            for key in org_keys:
                idx = keys.index(key) + 1
                state = 'ON' if self.features.get(key, False) else 'OFF'
                icon = '✓' if self.features.get(key, False) else '✗'
                print(f"    {idx:2}. [{icon}] {labels[key]:45} {state}")
            
            # Group: Behavior
            print("  BEHAVIOR:")
            beh_keys = ['preview', 'bg_predownload', 'geo_bypass']
            for key in beh_keys:
                idx = keys.index(key) + 1
                state = 'ON' if self.features.get(key, False) else 'OFF'
                icon = '✓' if self.features.get(key, False) else '✗'
                print(f"    {idx:2}. [{icon}] {labels[key]:45} {state}")
            
            total = len(keys)
            print(f"  {'─' * 60}")
            print(f"   {total + 1}. Enable All")
            print(f"   {total + 2}. Disable All")
            print(f"   {total + 3}. Change Output Template")
            print(f"    0. Back")
            print(f"  {'═' * 60}")
            
            choice = input("\n  Toggle # (or 0): ").strip()
            
            if choice == '0' or not choice:
                break
            elif choice == str(total + 1):
                for k in keys:
                    self.features[k] = True
                print("\n  ✓ All features enabled.")
            elif choice == str(total + 2):
                for k in keys:
                    self.features[k] = False
                print("\n  ✓ All features disabled.")
            elif choice == str(total + 3):
                print(f"\n  Current template: {Config.OUTPUT_TEMPLATE}")
                print("  Available fields: %(title)s, %(uploader)s, %(upload_date)s, %(id)s")
                print("  Example: %(uploader)s - %(title)s.%(ext)s")
                new_tmpl = input("  New template (empty to keep): ").strip()
                if new_tmpl:
                    Config.OUTPUT_TEMPLATE = new_tmpl
                    print(f"  ✓ Template set to: {new_tmpl}")
            else:
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(keys):
                        key = keys[idx]
                        self.features[key] = not self.features.get(key, False)
                        state = 'ON' if self.features[key] else 'OFF'
                        print(f"\n  ✓ {labels[key]}: {state}")
                    else:
                        print("\n  Invalid selection.")
                except ValueError:
                    print("\n  Invalid input.")
            
            self._save_feature_settings()
        
        input("\n  Press Enter to continue...")
    
    def handle_spotiflac(self):
        """Interactive SpotiFLAC music downloader"""
        if not SPOTIFLAC_AVAILABLE:
            print("\n  [ERROR] spotiflac.py module not found.")
            input("\n  Press Enter...")
            return

        print(f"\n  {'═' * 60}")
        print("  🎵 SpotiFLAC — Lossless Music Downloader")
        print("  Supports: Spotify Tracks, Albums, Playlists, Artists")
        print("  Features: Lossless FLAC, 320k MP3, Synced Lyrics, Cover Art")
        print(f"  {'─' * 60}")

        url = self.get_url()
        if not url or url.lower() == 'back':
            return
        
        self.handle_spotiflac_with_url(url)

    def handle_spotiflac_with_url(self, url: str):
        """Execute SpotiFLAC download for a given URL"""
        if not SPOTIFLAC_AVAILABLE:
            print("\n  [ERROR] spotiflac.py module not found.")
            input("\n  Press Enter...")
            return

        print("\n  Audio Quality:")
        print("    1. FLAC (Lossless Audio) [Default]")
        print("    2. MP3 (320 kbps Extreme)")
        q_choice = input("  Select [1]: ").strip() or "1"
        quality = "flac" if q_choice == "1" else "320k"

        print(f"\n  ⏳ Downloading with SpotiFLAC ({quality.upper()})...")
        res = self.downloader.download_spotify(url, quality=quality)
        if res:
            print("\n  ✅ Download complete!")
            if isinstance(res, str):
                print(f"  Saved: {res}")
            elif isinstance(res, list):
                print(f"  Saved {len(res)} track(s) to {Config.MUSIC_DIR}")
        
    def handle_search_explorer(self, initial_query: str = None):
        """Interactive Animated Search Engine & Playlist Explorer"""
        if not SEARCH_ENGINE_AVAILABLE:
            print("\n  [ERROR] search_engine.py module not found.")
            input("\n  Press Enter...")
            return

        query = initial_query
        while True:
            if not query:
                print(f"\n╭──────────────────────────────────────────────────────────────╮")
                print(f"│  🎵 SPOTIFY SEARCH ENGINE & PLAYLIST EXPLORER                │")
                print(f"│  Search Spotify songs, albums, playlists & artists           │")
                print(f"╰──────────────────────────────────────────────────────────────╯")
                query = input("\n  🔍 Enter Spotify song, artist, album, or URL (or 'b' to go back): ").strip()
                if not query or query.lower() in ('b', 'back', 'exit', '0'):
                    break

            # Animated search spinner
            stop_spin = threading.Event()
            def _spin():
                chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
                i = 0
                while not stop_spin.is_set():
                    sys.stdout.write(f"\r  {chars[i % len(chars)]} Searching Spotify & LRCLIB for '{query}'...")
                    sys.stdout.flush()
                    i += 1
                    time.sleep(0.08)
            t = threading.Thread(target=_spin, daemon=True)
            t.start()

            results = MediaSearchEngine.search(query, search_type="spotify", limit=24)
            stop_spin.set()
            t.join()
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()

            if not results:
                print(f"\n  ✖ No results found for '{query}'. Try another search.")
                query = None
                continue

            # Paginated browser loop
            page = 0
            page_size = 8
            total_pages = (len(results) + page_size - 1) // page_size

            while True:
                start_idx = page * page_size
                page_items = results[start_idx : start_idx + page_size]

                print(f"\n╭────────────────────────────────────────────────────────────────────────────╮")
                print(f"│  🔍 SEARCH RESULTS for \"{query[:32]}\"  [Page {page + 1}/{total_pages}]                     │")
                print(f"╰────────────────────────────────────────────────────────────────────────────╯")

                for i, item in enumerate(page_items, 1):
                    global_idx = start_idx + i
                    icon = "🎵" if item.media_type in ("track", "song") else ("💿" if item.media_type in ("album", "playlist") else "🎬")
                    lyrics_badge = " [Synced Lyrics: ✔]" if item.has_synced_lyrics else ""
                    dur = f"({item.duration_str})" if item.duration_str != "0:00" else ""
                    artist_str = f" — {item.artist}" if item.artist else ""
                    print(f"  {global_idx:2d}. {icon} {item.title[:38]}{artist_str[:25]} {dur} {lyrics_badge}")

                print(f"\n  [1-{len(results)}] Select & Action  |  [i <num>] Inspect Details")
                print(f"  [n] Next Page  |  [p] Prev Page  |  [s] New Search  |  [b] Back to Main Menu")

                cmd = input("\n  Action > ").strip().lower()

                if cmd in ('b', 'back', 'exit'):
                    query = None
                    break
                elif cmd in ('s', 'search'):
                    query = None
                    break
                elif cmd == 'n':
                    if page < total_pages - 1:
                        page += 1
                    else:
                        print("  (Already on last page)")
                elif cmd == 'p':
                    if page > 0:
                        page -= 1
                    else:
                        print("  (Already on first page)")
                elif cmd.startswith('i ') or (cmd.startswith('i') and len(cmd) > 1 and cmd[1:].isdigit()):
                    num_str = cmd[1:].strip()
                    if num_str.isdigit():
                        idx = int(num_str) - 1
                        if 0 <= idx < len(results):
                            self._inspect_and_action(results[idx])
                elif cmd.isdigit():
                    idx = int(cmd) - 1
                    if 0 <= idx < len(results):
                        self._inspect_and_action(results[idx])
                else:
                    print("  Invalid command. Enter a number or [b]ack.")

    def _inspect_and_action(self, item: SearchResultItem):
        """Displays detailed inspection card with lyrics, subtitles, and download options"""
        print(f"\n  ⏳ Inspecting metadata for '{item.title}'...")
        meta = MediaSearchEngine.inspect_metadata(item.url)

        print(f"\n╭────────────────────────────────────────────────────────────────────────────╮")
        print(f"│  📋 METADATA & MEDIA INSPECTION CARD                                       │")
        print(f"├────────────────────────────────────────────────────────────────────────────┤")
        print(f"│  Title:     {meta.get('title', item.title)[:60]:<60} │")
        print(f"│  Artist:    {meta.get('artist', item.artist)[:60]:<60} │")
        if meta.get('album'):
            print(f"│  Album:     {meta.get('album', '')[:60]:<60} │")
        print(f"│  Duration:  {meta.get('duration_str', item.duration_str):<60} │")
        print(f"│  Platform:  {meta.get('platform', item.platform):<60} │")

        if meta.get('has_synced_lyrics'):
            print(f"│  Lyrics:    ✔ Synchronized Lyrics Available (.lrc file + embedded)       │")
        elif meta.get('has_plain_lyrics'):
            print(f"│  Lyrics:    ✔ Plain Lyrics Available (embedded)                          │")
        else:
            print(f"│  Lyrics:    ○ Not detected in LRCLIB database                            │")

        subs = meta.get('subtitles') or []
        if subs:
            subs_str = ", ".join(subs[:6])
            print(f"│  Subtitles: ✔ Available languages: {subs_str:<39} │")

        v_fmts = meta.get('video_formats') or []
        if v_fmts:
            print(f"│  Video:     {', '.join(v_fmts[:5]):<60} │")

        print(f"╰────────────────────────────────────────────────────────────────────────────╯")

        if meta.get('lyrics_preview'):
            print(f"\n  📜 Lyrics Preview Snippet:")
            for line in meta.get('lyrics_preview').splitlines()[:4]:
                print(f"     {line}")

        if meta.get('is_playlist') or item.media_type in ('album', 'playlist'):
            print(f"\n  This is a collection with {meta.get('track_count', 1)} tracks.")
            print(f"  1. 📂 Explore & Select Songs inside this Playlist/Album")
            print(f"  2. ⚡ Download ALL Tracks (FLAC Lossless)")
            print(f"  3. 🎧 Download ALL Tracks (320k MP3)")
            print(f"  4. ↩ Back")
            act = input("\n  Select Action [1]: ").strip() or "1"
            if act == '1':
                self._explore_playlist(item.url, meta.get('title', item.title))
            elif act == '2':
                self.downloader.download_spotify(item.url, quality="flac")
            elif act == '3':
                self.downloader.download_spotify(item.url, quality="320k")
            return

        print(f"\n  DOWNLOAD ACTIONS:")
        print(f"    1. ⚡ Download FLAC (Lossless Studio Quality + Synced Lyrics & Art)")
        print(f"    2. 🎧 Download MP3 (320 kbps Extreme + Embedded Art & Lyrics)")
        print(f"    3. 🎬 Download Video (Best Quality MP4)")
        print(f"    4. ↩ Back to search results")

        act = input("\n  Select Action [1]: ").strip() or "1"
        if act == '1':
            self.downloader.download_spotify(item.url, quality="flac")
            input("\n  Press Enter to continue...")
        elif act == '2':
            self.downloader.download_spotify(item.url, quality="320k")
            input("\n  Press Enter to continue...")
        elif act == '3':
            self.downloader.download_video(item.url, quality="best")
            input("\n  Press Enter to continue...")

    def _explore_playlist(self, url: str, title: str):
        """Interactive Song Explorer for Playlists and Albums"""
        print(f"\n  ⏳ Fetching songs inside '{title}'...")
        tracks = MediaSearchEngine.fetch_playlist_tracks(url)
        if not tracks:
            print("  ✖ Could not load tracks for this playlist.")
            input("\n  Press Enter...")
            return

        selected_indices = set(range(1, len(tracks) + 1))

        while True:
            print(f"\n╭────────────────────────────────────────────────────────────────────────────╮")
            print(f"│  📂 PLAYLIST SONG EXPLORER: {title[:44]:<44} │")
            print(f"│  {len(selected_indices)}/{len(tracks)} tracks selected                                           │")
            print(f"╰────────────────────────────────────────────────────────────────────────────╯")

            for t in tracks:
                idx = t['index']
                mark = "●" if idx in selected_indices else "○"
                dur = f"({t.get('duration_str', '0:00')})"
                print(f"  [{mark}] {idx:2d}. {t.get('artist', '')[:20]:<20} — {t.get('title', '')[:35]:<35} {dur}")

            print(f"\n  COMMANDS:")
            print(f"    [d] Download selected tracks")
            print(f"    [all] Select all tracks  |  [none] Deselect all")
            print(f"    [1,3,5-8] Toggle / specify tracks to select")
            print(f"    [i <num>] Inspect specific track lyrics/details")
            print(f"    [b] Back to search results")

            cmd = input("\n  Explorer > ").strip().lower()

            if cmd in ('b', 'back'):
                break
            elif cmd in ('all', 'select all'):
                selected_indices = set(range(1, len(tracks) + 1))
            elif cmd in ('none', 'deselect all'):
                selected_indices.clear()
            elif cmd.startswith('i ') or (cmd.startswith('i') and len(cmd) > 1 and cmd[1:].isdigit()):
                num_str = cmd[1:].strip()
                if num_str.isdigit():
                    t_idx = int(num_str)
                    target = next((t for t in tracks if t['index'] == t_idx), None)
                    if target:
                        self._inspect_and_action(SearchResultItem(
                            id=str(t_idx),
                            title=target['title'],
                            artist=target['artist'],
                            album=target.get('album', ''),
                            duration_s=target.get('duration_s', 0),
                            duration_str=target.get('duration_str', '0:00'),
                            url=target.get('url', url),
                            media_type='track'
                        ))
            elif cmd in ('d', 'download'):
                if not selected_indices:
                    print("  ✖ No tracks selected to download.")
                    continue

                print("\n  Audio Quality for Selected Tracks:")
                print("    1. FLAC (Lossless Audio - Synced Lyrics & Art) [Default]")
                print("    2. MP3 (320 kbps Extreme)")
                q_choice = input("  Select [1]: ").strip() or "1"
                quality = "flac" if q_choice == "1" else "320k"

                print(f"\n  ⏳ Downloading {len(selected_indices)} selected track(s) in {quality.upper()}...")
                selected_tracks = [t for t in tracks if t['index'] in selected_indices]
                for st in selected_tracks:
                    print(f"\n  -> [{st['index']}/{len(tracks)}] {st['artist']} - {st['title']}")
                    self.downloader.download_spotify(st['url'], quality=quality)

                print(f"\n  ✔ Completed downloading {len(selected_indices)} track(s)!")
                input("\n  Press Enter to continue...")
                break
            else:
                parsed_nums = set()
                try:
                    parts = cmd.replace(" ", "").split(",")
                    for p in parts:
                        if "-" in p:
                            s, e = p.split("-", 1)
                            for n in range(int(s), int(e) + 1):
                                if 1 <= n <= len(tracks):
                                    parsed_nums.add(n)
                        elif p.isdigit():
                            n = int(p)
                            if 1 <= n <= len(tracks):
                                parsed_nums.add(n)
                    if parsed_nums:
                        selected_indices = parsed_nums
                except Exception:
                    print("  Unrecognized command.")

    def handle_universal_downloader(self):
        """Option 30: Unknown Media (Video+Audio+Subtitle) Downloader."""
        if not UNIVERSAL_AVAILABLE:
            print("\n  [!] Universal Media Downloader module is not available.")
            input("\n  Press Enter...")
            return

        clear_screen()
        self.print_header()
        UniversalMediaDownloader().interactive_cli()

    def handle_universal_with_url(self, url: str):
        """Auto-detected webpage/stream URL routed to Universal Downloader."""
        if not UNIVERSAL_AVAILABLE:
            print("\n  [!] Universal Media Downloader module is not available.")
            input("\n  Press Enter...")
            return

        clear_screen()
        self.print_header()
        UniversalMediaDownloader().interactive_cli(url)

    # ==================== MAIN LOOP ====================
    
    def run(self):
        """Main application loop"""
        
        handlers = {
            '1': self.handle_video,
            '2': self.handle_audio,
            '3': self.handle_playlist_video,
            '4': self.handle_playlist_audio,
            '5': self.handle_batch,
            '6': self.handle_thumbnail,
            '7': self.handle_subtitles,
            '8': self.handle_quality_settings,
            '9': self.handle_view_settings,
            '10': self.handle_history,
            '11': self.handle_search,
            '12': self.handle_statistics,
            '13': self.handle_failed,
            '14': self.handle_queue,
            '15': self.handle_open_recent,
            '16': self.handle_open_folder,
            '17': self.handle_dependencies,
            '18': self.handle_directories,
            '19': self.handle_clear_temp,
            '20': self.handle_update,
            '21': self.handle_feature_settings,
            '22': self.handle_spotiflac,
            '23': self.handle_search_explorer,
            '24': self.handle_universal_downloader,
            '25': self.handle_universal_downloader,
            '26': self.handle_universal_downloader,
            '27': self.handle_search_explorer,
            '30': self.handle_universal_downloader,
        }
        
        while self.running:
            try:
                clear_screen()
                self.print_header()
                self.print_menu()
                
                choice = input("  Enter choice: ").strip()
                
                # ML: log menu selection
                if ML_LOGGER_AVAILABLE and choice in handlers:
                    get_ml_logger().log_usage("menu", opt=int(choice))
                
                if choice == '0':
                    self.bg_manager.stop()
                    if ML_LOGGER_AVAILABLE:
                        get_ml_logger().log_usage("exit")
                        get_ml_logger().close()
                    print("\n  Goodbye!\n")
                    self.running = False
                elif self.features['bg_predownload'] and (is_valid_url(choice) or is_supported_url(choice)):
                    # Auto-detect Spotify / Universal URLs before bg_predownload
                    if SPOTIFLAC_AVAILABLE and SpotiFLACDownloader.is_spotify_url(choice):
                        if ML_LOGGER_AVAILABLE:
                            get_ml_logger().log_usage("auto_detect", kind="spotify")
                        self.handle_spotiflac_with_url(choice)
                        continue
                    elif UNIVERSAL_AVAILABLE and (".mpd" in choice.lower() or ".m3u8" in choice.lower() or detect_platform(choice) == Platform.UNKNOWN):
                        if ML_LOGGER_AVAILABLE:
                            get_ml_logger().log_usage("auto_detect", kind="universal")
                        self.handle_universal_with_url(choice)
                        continue
                    
                    # User pasted a URL at the menu (bg_predownload enabled)
                    existing = self.bg_manager.get_job(choice)

                    if existing and existing.status == 'complete':
                        # Same URL pasted again + already done -> save to default dir
                        platform = detect_platform(choice)
                        final_dir = self.downloader._get_output_dir(platform, "video")
                        final = self.bg_manager.claim(choice, final_dir)
                        if final:
                            filesize = os.path.getsize(final) if os.path.exists(final) else 0
                            self.downloader.db.add_download(
                                url=choice, platform=platform.value,
                                title=existing.title, filesize=filesize,
                                filepath=final, status='complete'
                            )
                            print(f"\n  Saved: {final}")
                            if filesize:
                                print(f"  Size: {format_size(filesize)}")
                        else:
                            print("\n  Could not move file.")

                    elif existing and existing.status == 'downloading':
                        # Same URL pasted again + still downloading -> show progress
                        print(f"\n  Background download in progress...")
                        self._wait_for_bg_job(existing)
                        if existing.status == 'complete':
                            platform = detect_platform(choice)
                            final_dir = self.downloader._get_output_dir(platform, "video")
                            final = self.bg_manager.claim(choice, final_dir)
                            if final:
                                filesize = os.path.getsize(final) if os.path.exists(final) else 0
                                self.downloader.db.add_download(
                                    url=choice, platform=platform.value,
                                    title=existing.title, filesize=filesize,
                                    filepath=final, status='complete'
                                )
                                print(f"\n  Saved: {final}")
                                if filesize:
                                    print(f"  Size: {format_size(filesize)}")
                        else:
                            print(f"\n  Download failed: {existing.error}")

                    elif existing and existing.status == 'failed':
                        # Previous attempt failed -> retry
                        print(f"\n  Previous attempt failed. Retrying...")
                        with self.bg_manager._lock:
                            key = self.bg_manager._normalize_key(choice)
                            self.bg_manager._jobs.pop(key, None)
                        self.bg_manager.start(choice)
                        print("  Background download started")

                    else:
                        # New URL -> start background download
                        job = self.bg_manager.start(choice)
                        print(f"\n  Background download started")
                        if job.title:
                            print(f"  {job.title}")

                    input("\n  Press Enter to continue...")
                elif choice in handlers:
                    handlers[choice]()
                elif choice and len(choice.strip()) >= 2:
                    self.handle_search_explorer(initial_query=choice.strip())
                else:
                    print("\n  Invalid choice.")
                    input("\n  Press Enter...")
                    
            except KeyboardInterrupt:
                self.bg_manager.stop()
                print("\n\n  Goodbye!")
                self.running = False
            except Exception as e:
                print(f"\n  Error: {e}")
                logger.exception("CLI error")
                input("\n  Press Enter...")


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    """Main entry point"""
    
    print("\n  " + "=" * 50)
    print("  Initializing Personal Media Downloader...")
    print("  " + "=" * 50)
    
    # Initialize directories
    print("\n  Creating directories...")
    Config.init_directories()
    print("  ✓ Directories ready")
    
    # Check dependencies
    if not YTDLP_AVAILABLE:
        print("\n  [ERROR] yt-dlp is required.")
        print("  Install: pip install yt-dlp")
        sys.exit(1)
    print(f"  ✓ yt-dlp: {YTDLP_VERSION}")
    
    if not check_ffmpeg():
        print("\n  [WARNING] FFmpeg not found.")
        print("  Some features may not work.")
        input("  Press Enter to continue...")
    else:
        print("  ✓ FFmpeg available")
    
    print("\n  Starting...\n")
    
    # Run CLI
    cli = DownloaderCLI()
    cli.run()


if __name__ == "__main__":
    main()
