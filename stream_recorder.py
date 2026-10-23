"""
M3U8/HLS Stream Recorder & MPEG-DASH Downloader
live stream recording with real-time decryption.

Features:
  - Parse M3U8 master & media playlists
  - Download & decrypt AES-128 encrypted segments
  - Concurrent segment downloading
  - Live stream recording (refreshing playlist)
  - MPEG-DASH (MPD) support
  - Merge segments into MP4/MKV via FFmpeg
"""

import os
import re
import sys
import time
import json
import struct
import hashlib
import logging
import tempfile
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Callable, Any
from urllib.parse import urljoin, urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import Config as _Cfg
_TEMP_DIR = _Cfg.TEMP_DIR
_VIDEO_DIR = _Cfg.VIDEO_DIR

from utils import format_size, format_duration, sanitize_filename, get_logger, check_ffmpeg

logger = get_logger()


# ============================================================================
# M3U8 PARSER
# ============================================================================

@dataclass
class M3U8Segment:
    """A single segment in an M3U8 media playlist."""
    uri: str
    duration: float = 0.0
    title: str = ""
    index: int = 0
    byterange: Optional[Tuple[int, int]] = None  # (length, offset)
    encryption: Optional[Dict] = None  # {method, uri, iv}
    discontinuity: bool = False


@dataclass
class M3U8Stream:
    """A stream variant from a master playlist."""
    uri: str
    bandwidth: int = 0
    resolution: str = ""
    width: int = 0
    height: int = 0
    codecs: str = ""
    name: str = ""
    
    @property
    def label(self) -> str:
        if self.height:
            return f"{self.height}p ({format_size(self.bandwidth)}/s)"
        elif self.name:
            return self.name
        return f"{format_size(self.bandwidth)}/s"


@dataclass
class M3U8Playlist:
    """Parsed M3U8 playlist."""
    is_master: bool = False
    # Master playlist fields
    streams: List[M3U8Stream] = field(default_factory=list)
    # Media playlist fields
    segments: List[M3U8Segment] = field(default_factory=list)
    target_duration: float = 0.0
    media_sequence: int = 0
    is_endlist: bool = False  # False = live stream
    total_duration: float = 0.0
    encryption: Optional[Dict] = None


class M3U8Parser:
    """Parse M3U8 playlists."""
    
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    
    @classmethod
    def parse(cls, content: str, base_url: str = "") -> M3U8Playlist:
        """Parse M3U8 content string."""
        playlist = M3U8Playlist()
        lines = content.strip().splitlines()
        
        if not lines or '#EXTM3U' not in lines[0]:
            raise ValueError("Not a valid M3U8 playlist")
        
        # Check if master playlist
        if any('#EXT-X-STREAM-INF' in line for line in lines):
            playlist.is_master = True
            cls._parse_master(lines, base_url, playlist)
        else:
            cls._parse_media(lines, base_url, playlist)
        
        return playlist
    
    @classmethod
    def _parse_master(cls, lines: List[str], base_url: str, playlist: M3U8Playlist):
        """Parse a master playlist."""
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF:'):
                attrs = cls._parse_attributes(line.split(':', 1)[1])
                
                # Next non-comment line is the URI
                uri = ""
                for j in range(i + 1, len(lines)):
                    if lines[j].strip() and not lines[j].startswith('#'):
                        uri = lines[j].strip()
                        break
                
                if uri:
                    uri = urljoin(base_url, uri)
                    resolution = attrs.get('RESOLUTION', '')
                    width, height = 0, 0
                    if 'x' in resolution:
                        parts = resolution.split('x')
                        try:
                            width, height = int(parts[0]), int(parts[1])
                        except ValueError:
                            pass
                    
                    stream = M3U8Stream(
                        uri=uri,
                        bandwidth=int(attrs.get('BANDWIDTH', 0)),
                        resolution=resolution,
                        width=width,
                        height=height,
                        codecs=attrs.get('CODECS', ''),
                        name=attrs.get('NAME', ''),
                    )
                    playlist.streams.append(stream)
        
        # Sort by bandwidth (highest first)
        playlist.streams.sort(key=lambda s: s.bandwidth, reverse=True)
    
    @classmethod
    def _parse_media(cls, lines: List[str], base_url: str, playlist: M3U8Playlist):
        """Parse a media playlist."""
        current_encryption = None
        segment_index = 0
        current_duration = 0.0
        current_title = ""
        current_byterange = None
        discontinuity = False
        
        for line in lines:
            line = line.strip()
            
            if line.startswith('#EXT-X-TARGETDURATION:'):
                playlist.target_duration = float(line.split(':')[1])
            
            elif line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                playlist.media_sequence = int(line.split(':')[1])
            
            elif line.startswith('#EXT-X-ENDLIST'):
                playlist.is_endlist = True
            
            elif line.startswith('#EXT-X-KEY:'):
                attrs = cls._parse_attributes(line.split(':', 1)[1])
                method = attrs.get('METHOD', 'NONE')
                if method == 'NONE':
                    current_encryption = None
                else:
                    key_uri = attrs.get('URI', '').strip('"')
                    if key_uri:
                        key_uri = urljoin(base_url, key_uri)
                    iv = attrs.get('IV', '')
                    current_encryption = {
                        'method': method,
                        'uri': key_uri,
                        'iv': iv,
                    }
                playlist.encryption = current_encryption
            
            elif line.startswith('#EXTINF:'):
                parts = line.split(':', 1)[1].split(',', 1)
                current_duration = float(parts[0])
                current_title = parts[1] if len(parts) > 1 else ""
            
            elif line.startswith('#EXT-X-BYTERANGE:'):
                br = line.split(':')[1]
                parts = br.split('@')
                length = int(parts[0])
                offset = int(parts[1]) if len(parts) > 1 else None
                current_byterange = (length, offset)
            
            elif line.startswith('#EXT-X-DISCONTINUITY'):
                discontinuity = True
            
            elif line and not line.startswith('#'):
                # Segment URI
                uri = urljoin(base_url, line)
                segment = M3U8Segment(
                    uri=uri,
                    duration=current_duration,
                    title=current_title,
                    index=segment_index,
                    byterange=current_byterange,
                    encryption=dict(current_encryption) if current_encryption else None,
                    discontinuity=discontinuity,
                )
                playlist.segments.append(segment)
                playlist.total_duration += current_duration
                segment_index += 1
                
                # Reset per-segment state
                current_duration = 0.0
                current_title = ""
                current_byterange = None
                discontinuity = False
    
    @classmethod
    def _parse_attributes(cls, attr_string: str) -> Dict[str, str]:
        """Parse M3U8 attribute string like KEY=VALUE,KEY2="VALUE2"."""
        attrs = {}
        # Handle quoted and unquoted values
        pattern = r'([A-Z0-9_-]+)\s*=\s*(?:"([^"]*)"|((?:[^",]|(?:"[^"]*"))+))'
        for match in re.finditer(pattern, attr_string, re.IGNORECASE):
            key = match.group(1).upper()
            value = match.group(2) if match.group(2) is not None else match.group(3)
            attrs[key] = value.strip()
        return attrs
    
    @classmethod
    def fetch_and_parse(cls, url: str, headers: Dict = None) -> M3U8Playlist:
        """Fetch an M3U8 URL and parse it."""
        session = requests.Session()
        req_headers = {'User-Agent': cls.USER_AGENT}
        if headers:
            req_headers.update(headers)
        
        resp = session.get(url, headers=req_headers, timeout=15)
        resp.raise_for_status()
        
        return cls.parse(resp.text, url)


# ============================================================================
# M3U8 DOWNLOADER
# ============================================================================

class M3U8Downloader:
    """
    Download M3U8/HLS streams.
    
    Supports:
    - VOD (finite playlists)
    - Live streams (refreshing playlists)
    - AES-128 encryption (real-time decryption)
    - Concurrent segment downloading
    - FFmpeg merging to MP4
    """
    
    DEFAULT_THREADS = 4
    SEGMENT_TIMEOUT = 30
    LIVE_POLL_INTERVAL = 5.0  # seconds between playlist refreshes
    
    def __init__(self, threads: int = None, output_dir: str = None):
        self.threads = threads or self.DEFAULT_THREADS
        self.output_dir = output_dir or _VIDEO_DIR
        self._stop_event = threading.Event()
        self._encryption_keys: Dict[str, bytes] = {}  # Cache decryption keys
        self._session: Optional[requests.Session] = None
    
    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            retry = Retry(total=3, backoff_factor=0.5)
            adapter = HTTPAdapter(max_retries=retry)
            self._session.mount('http://', adapter)
            self._session.mount('https://', adapter)
            self._session.headers['User-Agent'] = M3U8Parser.USER_AGENT
        return self._session
    
    def download(self,
                 url: str,
                 filename: str = None,
                 quality: str = "best",
                 headers: Dict = None,
                 progress_callback: Callable = None,
                 max_duration: float = 0) -> Dict[str, Any]:
        """
        Download an M3U8 stream.
        
        Args:
            url: M3U8 playlist URL
            filename: Output filename (without extension)
            quality: "best", "worst", or resolution like "1080p"
            headers: Extra HTTP headers
            max_duration: Max recording duration in seconds (0 = unlimited, for live)
            progress_callback: Called with progress dict
        
        Returns:
            Dict with: status, filepath, duration, segments_count
        """
        self._stop_event.clear()
        
        if not check_ffmpeg():
            return {'status': 'failed', 'error': 'FFmpeg is required for M3U8 downloads'}
        
        print(f"\n  Fetching playlist...")
        playlist = M3U8Parser.fetch_and_parse(url, headers)
        
        # If master playlist, select quality
        if playlist.is_master:
            stream = self._select_quality(playlist, quality)
            if not stream:
                return {'status': 'failed', 'error': 'No suitable stream found'}
            
            print(f"  Selected: {stream.label}")
            playlist = M3U8Parser.fetch_and_parse(stream.uri, headers)
        
        # Set up output
        if not filename:
            filename = f"stream_{hashlib.md5(url.encode()).hexdigest()[:8]}"
        filename = sanitize_filename(filename)
        
        os.makedirs(self.output_dir, exist_ok=True)
        
        # Create temp directory for segments
        temp_dir = os.path.join(_TEMP_DIR, f"m3u8_{hashlib.md5(url.encode()).hexdigest()[:8]}")
        os.makedirs(temp_dir, exist_ok=True)
        
        print(f"\n  {'═' * 55}")
        print(f"  Stream:    {filename[:50]}")
        print(f"  Segments:  {len(playlist.segments)}")
        print(f"  Duration:  {format_duration(playlist.total_duration)}")
        print(f"  Encrypted: {'Yes' if playlist.encryption else 'No'}")
        print(f"  Live:      {'Yes' if not playlist.is_endlist else 'No'}")
        print(f"  Threads:   {self.threads}")
        print(f"  {'═' * 55}\n")
        
        try:
            if playlist.is_endlist:
                # VOD — download all segments
                segment_files = self._download_vod(
                    playlist, temp_dir, headers, progress_callback
                )
            else:
                # Live — record until stopped or max_duration
                segment_files = self._download_live(
                    url, temp_dir, headers, progress_callback, max_duration
                )
            
            if self._stop_event.is_set() and not segment_files:
                return {'status': 'cancelled'}
            
            if not segment_files:
                return {'status': 'failed', 'error': 'No segments downloaded'}
            
            # Merge segments with FFmpeg
            output_path = os.path.join(self.output_dir, f"{filename}.mp4")
            print(f"\n  Merging {len(segment_files)} segments...")
            
            success = self._merge_segments(segment_files, output_path, temp_dir)
            
            if success and os.path.exists(output_path):
                filesize = os.path.getsize(output_path)
                print(f"\n  ✓ Complete: {filename}.mp4")
                print(f"  ✓ Size: {format_size(filesize)}")
                print(f"  ✓ Segments: {len(segment_files)}")
                
                # Cleanup temp
                self._cleanup_temp(temp_dir)
                
                return {
                    'status': 'complete',
                    'filepath': output_path,
                    'filesize': filesize,
                    'segments_count': len(segment_files),
                    'duration': playlist.total_duration,
                }
            else:
                return {'status': 'failed', 'error': 'FFmpeg merge failed'}
        
        except Exception as e:
            logger.error(f"M3U8 download failed: {e}")
            return {'status': 'failed', 'error': str(e)}
    
    def stop(self):
        """Stop recording."""
        self._stop_event.set()
    
    # ================================================================
    # INTERNAL
    # ================================================================
    
    def _select_quality(self, playlist: M3U8Playlist, quality: str) -> Optional[M3U8Stream]:
        """Select a stream variant based on quality preference."""
        if not playlist.streams:
            return None
        
        if quality == "worst":
            return playlist.streams[-1]
        
        if quality == "best":
            return playlist.streams[0]
        
        # Try to match resolution (e.g., "1080p")
        target_height = 0
        match = re.match(r'(\d+)p?', quality)
        if match:
            target_height = int(match.group(1))
        
        if target_height:
            # Find closest match
            best = None
            best_diff = float('inf')
            for stream in playlist.streams:
                if stream.height > 0:
                    diff = abs(stream.height - target_height)
                    if diff < best_diff:
                        best_diff = diff
                        best = stream
            if best:
                return best
        
        # Default to best
        return playlist.streams[0]
    
    def _download_vod(self, playlist: M3U8Playlist, temp_dir: str,
                      headers: Dict = None,
                      progress_callback: Callable = None) -> List[str]:
        """Download all segments of a VOD playlist."""
        total = len(playlist.segments)
        downloaded = 0
        failed = 0
        segment_files = []
        
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            futures = {}
            for seg in playlist.segments:
                seg_path = os.path.join(temp_dir, f"seg_{seg.index:06d}.ts")
                segment_files.append(seg_path)
                
                if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
                    downloaded += 1
                    continue
                
                future = pool.submit(
                    self._download_segment, seg, seg_path, headers
                )
                futures[future] = seg
            
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    for f in futures:
                        f.cancel()
                    break
                
                seg = futures[future]
                try:
                    future.result()
                    downloaded += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"Segment {seg.index} failed: {e}")
                
                # Progress
                pct = downloaded / total * 100
                bar_len = 35
                filled = int(bar_len * pct / 100)
                bar = '█' * filled + '░' * (bar_len - filled)
                print(f"\r  [{bar}] {pct:5.1f}% | "
                      f"{downloaded}/{total} segments"
                      f"{f' ({failed} failed)' if failed else ''}  ",
                      end='', flush=True)
                
                if progress_callback:
                    progress_callback({
                        'type': 'progress',
                        'percent': pct,
                        'downloaded': downloaded,
                        'total': total,
                        'failed': failed,
                    })
        
        return [f for f in segment_files if os.path.exists(f) and os.path.getsize(f) > 0]
    
    def _download_live(self, playlist_url: str, temp_dir: str,
                       headers: Dict = None,
                       progress_callback: Callable = None,
                       max_duration: float = 0) -> List[str]:
        """Record a live stream by polling the playlist."""
        seen_uris = set()
        segment_files = []
        segment_index = 0
        total_duration = 0.0
        start_time = time.monotonic()
        
        print("  🔴 Recording live stream (Ctrl+C to stop)...\n")
        
        while not self._stop_event.is_set():
            try:
                playlist = M3U8Parser.fetch_and_parse(playlist_url, headers)
            except Exception as e:
                logger.warning(f"Playlist refresh failed: {e}")
                time.sleep(self.LIVE_POLL_INTERVAL)
                continue
            
            new_segments = [
                s for s in playlist.segments
                if s.uri not in seen_uris
            ]
            
            for seg in new_segments:
                if self._stop_event.is_set():
                    break
                
                seen_uris.add(seg.uri)
                seg_path = os.path.join(temp_dir, f"seg_{segment_index:06d}.ts")
                
                try:
                    self._download_segment(seg, seg_path, headers)
                    segment_files.append(seg_path)
                    segment_index += 1
                    total_duration += seg.duration
                    
                    elapsed = time.monotonic() - start_time
                    print(f"\r  🔴 Recording: {format_duration(total_duration)} | "
                          f"{segment_index} segments | "
                          f"Elapsed: {format_duration(elapsed)}  ",
                          end='', flush=True)
                except Exception as e:
                    logger.warning(f"Live segment failed: {e}")
            
            # Check max duration
            if max_duration > 0 and total_duration >= max_duration:
                print(f"\n  Max duration reached ({format_duration(max_duration)})")
                break
            
            # Check if stream ended
            if playlist.is_endlist:
                print(f"\n  Stream ended")
                break
            
            # Wait before refreshing
            time.sleep(self.LIVE_POLL_INTERVAL)
        
        return segment_files
    
    def _download_segment(self, segment: M3U8Segment, output_path: str,
                          headers: Dict = None):
        """Download and optionally decrypt a single segment."""
        session = self._get_session()
        req_headers = dict(headers or {})
        
        if segment.byterange:
            length, offset = segment.byterange
            if offset is not None:
                req_headers['Range'] = f'bytes={offset}-{offset + length - 1}'
        
        resp = session.get(segment.uri, headers=req_headers, 
                          timeout=self.SEGMENT_TIMEOUT)
        resp.raise_for_status()
        
        data = resp.content
        
        # Decrypt if needed
        if segment.encryption and segment.encryption.get('method') == 'AES-128':
            data = self._decrypt_aes128(data, segment.encryption, segment.index)
        
        with open(output_path, 'wb') as f:
            f.write(data)
    
    def _decrypt_aes128(self, data: bytes, encryption: Dict, 
                        segment_index: int) -> bytes:
        """Decrypt AES-128 encrypted segment data."""
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import padding
        except ImportError:
            # Fallback: try PyCryptodome
            try:
                from Crypto.Cipher import AES
                from Crypto.Util.Padding import unpad
                
                key = self._get_encryption_key(encryption['uri'])
                iv = self._get_iv(encryption.get('iv', ''), segment_index)
                
                cipher = AES.new(key, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(data)
                return unpad(decrypted, AES.block_size)
            except ImportError:
                logger.error("Neither cryptography nor PyCryptodome installed. "
                           "Install with: pip install cryptography")
                raise RuntimeError("Encryption library not available")
        
        key = self._get_encryption_key(encryption['uri'])
        iv = self._get_iv(encryption.get('iv', ''), segment_index)
        
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(data) + decryptor.finalize()
        
        # Unpad PKCS7
        padder = padding.PKCS7(128)
        unpadder = padder.unpadder()
        return unpadder.update(decrypted) + unpadder.finalize()
    
    def _get_encryption_key(self, key_uri: str) -> bytes:
        """Fetch and cache an encryption key."""
        if key_uri in self._encryption_keys:
            return self._encryption_keys[key_uri]
        
        session = self._get_session()
        resp = session.get(key_uri, timeout=10)
        resp.raise_for_status()
        key = resp.content
        
        if len(key) != 16:
            raise ValueError(f"Invalid AES key length: {len(key)} (expected 16)")
        
        self._encryption_keys[key_uri] = key
        return key
    
    def _get_iv(self, iv_hex: str, segment_index: int) -> bytes:
        """Get initialization vector."""
        if iv_hex:
            # Remove 0x prefix
            iv_hex = iv_hex.lower().replace('0x', '')
            return bytes.fromhex(iv_hex.zfill(32))
        else:
            # Default IV is the segment sequence number
            return segment_index.to_bytes(16, byteorder='big')
    
    def _merge_segments(self, segment_files: List[str], output_path: str,
                        temp_dir: str) -> bool:
        """Merge .ts segments into a single MP4 using FFmpeg."""
        if not segment_files:
            return False
        
        # Create concat file
        concat_path = os.path.join(temp_dir, "concat.txt")
        with open(concat_path, 'w', encoding='utf-8') as f:
            for seg_file in segment_files:
                # FFmpeg concat requires forward slashes and escaping
                safe_path = seg_file.replace('\\', '/')
                f.write(f"file '{safe_path}'\n")
        
        # Run FFmpeg
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_path,
            '-c', 'copy',
            '-movflags', '+faststart',
            output_path,
        ]
        
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=600,
                creationflags=0x08000000 if sys.platform == 'win32' else 0
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg merge timed out")
            return False
        except Exception as e:
            logger.error(f"FFmpeg merge failed: {e}")
            return False
    
    def _cleanup_temp(self, temp_dir: str):
        """Remove temporary segment files."""
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


# ============================================================================
# URL DETECTION
# ============================================================================

def is_m3u8_url(url: str) -> bool:
    """Check if URL is an M3U8 playlist."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    return (
        path.endswith('.m3u8') or
        path.endswith('.m3u') or
        'format=m3u8' in url.lower() or
        '/hls/' in url.lower()
    )


def is_dash_url(url: str) -> bool:
    """Check if URL is an MPEG-DASH manifest."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    return (
        path.endswith('.mpd') or
        'format=mpd' in url.lower()
    )


def is_stream_url(url: str) -> bool:
    """Check if URL is a streaming media URL."""
    return is_m3u8_url(url) or is_dash_url(url)


# ============================================================================
# CONVENIENCE
# ============================================================================

def quick_record(url: str, filename: str = None, quality: str = "best",
                 output_dir: str = None) -> Dict:
    """Quick M3U8 stream recording."""
    dl = M3U8Downloader(output_dir=output_dir)
    return dl.download(url, filename=filename, quality=quality)


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    url = input("M3U8 URL: ").strip()
    if not url:
        print("No URL provided")
        sys.exit(1)
    
    filename = input("Output name (auto): ").strip() or None
    quality = input("Quality (best/worst/1080p): ").strip() or "best"
    
    dl = M3U8Downloader()
    result = dl.download(url, filename=filename, quality=quality)
    
    if result['status'] == 'complete':
        print(f"\n✓ Saved to: {result['filepath']}")
    else:
        print(f"\n✗ Failed: {result.get('error', 'Unknown error')}")
