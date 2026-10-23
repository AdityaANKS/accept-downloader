r"""
Universal Media Downloader & Webpage Stream Sniffer
Location: C:\downloader\universal_downloader.py

Extracts, lists, and downloads video, audio, and subtitle tracks across selectable
qualities from ANY webpage URL—even when the URL is a webpage containing an embedded
player, nested iframes, or dynamic JavaScript loaders.

Features:
- Automated stream discovery via headless browser (Playwright) or static fallback
- Traverses all frames (including nested <iframe> elements)
- Automatically dismisses popups and advertising tabs
- Triggers lazy-loaded video players (Play buttons, JWPlayer, Video.js, Plyr, HTML5 Video)
- Captures Master HLS (.m3u8), DASH (.mpd), direct video/audio, and subtitles (.vtt, .srt)
- Captures request headers (Referer, User-Agent, Origin, cookies) for CDN bypass
- DRM Detection (Widevine, PlayReady, FairPlay) with clear user notifications
- Interactive video quality, audio rendition, and subtitle selection
- Lossless stream muxing via FFmpeg (-c copy) into MP4 or MKV with real-time progress
"""

import os
import re
import sys
import json
import time
import uuid
import shutil
import asyncio
import tempfile
import threading
import subprocess
import pathlib
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Tuple, Any, Callable
from urllib.parse import urlparse, urljoin, unquote

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

import requests
import yt_dlp

from config import Config
from utils import (
    get_logger, sanitize_filename, format_size, format_duration,
    check_ffmpeg
)
from database import DatabaseManager

logger = get_logger()

# Optional Playwright import
try:
    from playwright.async_api import async_playwright, Response as PwResponse, Request as PwRequest
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from yt_dlp.networking.impersonate import ImpersonateTarget
    HAVE_IMPERSONATE = True
except Exception:
    HAVE_IMPERSONATE = False



# ============================================================================
# REGEX & FILTERS
# ============================================================================

MANIFEST_EXT = re.compile(r"\.(m3u8|mpd)(\?|$)", re.IGNORECASE)
MEDIA_EXT = re.compile(r"\.(mp4|webm|mkv|m4a|mp3|flv|mov|avi|m4v|ogg|ogv|opus|wav)(\?|$)", re.IGNORECASE)
SUB_EXT = re.compile(r"\.(vtt|srt|ttml|ass|dfxp)(\?|$)", re.IGNORECASE)
IGNORE_SEGMENTS = re.compile(
    r"(\.(ts|m4s|tmp|f4m|m4f)(\?|$))|(/seg(ment)?[-_0-9]+)|(/frag(ment)?[-_0-9]+)|(/range/)|(bytes=[0-9]+-[0-9]+)",
    re.IGNORECASE
)
DRM_PATTERNS = re.compile(
    r"(edef8ba9-79d6-4ace-a3c8-27dcd51d21ed|9a04f079-9840-4286-ab92-e65be0885f95|94ce86fb-07ff-4f43-adb8-93d2fa968ca2|SAMPLE-AES|cenc:pssh|widevine|playready|fairplay)",
    re.IGNORECASE
)

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class VideoTrackOption:
    format_id: str
    resolution: str
    height: int
    fps: Optional[int] = None
    vcodec: str = ""
    tbr: Optional[float] = None
    filesize_approx: int = 0

    @property
    def display_label(self) -> str:
        parts = [f"{self.height}p"]
        if self.fps and self.fps > 30:
            parts.append(f"{self.fps}fps")
        if self.vcodec and self.vcodec != "none":
            clean_codec = self.vcodec.split(".")[0]
            parts.append(f"[{clean_codec}]")
        if self.tbr:
            parts.append(f"{int(self.tbr)}kbps")
        return " ".join(parts)


@dataclass
class AudioTrackOption:
    format_id: str
    language: str = "und"
    acodec: str = ""
    abr: Optional[float] = None
    channels: Optional[int] = 2

    @property
    def display_label(self) -> str:
        parts = []
        lang_str = self.language.upper() if self.language and self.language != "und" else "Default Audio"
        parts.append(lang_str)
        if self.acodec and self.acodec != "none":
            clean_codec = self.acodec.split(".")[0]
            parts.append(f"[{clean_codec}]")
        if self.abr:
            parts.append(f"{int(self.abr)}kbps")
        return " ".join(parts)


@dataclass
class SubtitleTrackOption:
    language: str
    name: str = ""
    url: str = ""
    ext: str = "vtt"
    is_external: bool = False
    headers: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.ext or len(self.ext) > 5 or not self.ext.isalnum() or self.ext.lower() in ("unknown", "unknown_video"):
            self.ext = "vtt"

    @property
    def display_label(self) -> str:
        name_str = f" ({self.name})" if self.name else ""
        clean_ext = self.ext if (1 <= len(self.ext) <= 5 and self.ext.isalnum()) else "VTT"
        ext_str = f" [{clean_ext.upper()}]"
        source_str = " (Captured)" if self.is_external else " (Embedded)"
        return f"{self.language.upper()}{name_str}{ext_str}{source_str}"



@dataclass
class StreamCandidate:
    url: str
    type: str  # 'hls', 'dash', 'direct'
    headers: Dict[str, str] = field(default_factory=dict)
    title: str = "Discovered Media Stream"
    page_url: str = ""
    is_drm: bool = False
    drm_notice: str = ""
    duration_s: float = 0.0
    thumbnail: str = ""
    video_tracks: List[VideoTrackOption] = field(default_factory=list)
    audio_tracks: List[AudioTrackOption] = field(default_factory=list)
    subtitles: List[SubtitleTrackOption] = field(default_factory=list)
    raw_info: Dict[str, Any] = field(default_factory=dict)
    local_manifest_path: str = ""
    manifest_content: str = ""
    cipher_info: Optional[Dict[str, Any]] = None
    media_manifests: Dict[str, str] = field(default_factory=dict)
    cookies: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def unique_resolutions(self) -> List[int]:
        heights = sorted({vt.height for vt in self.video_tracks if vt.height > 0}, reverse=True)
        return heights

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "type": self.type,
            "title": self.title,
            "page_url": self.page_url,
            "is_drm": self.is_drm,
            "drm_notice": self.drm_notice,
            "duration_s": self.duration_s,
            "duration_str": format_duration(self.duration_s),
            "thumbnail": self.thumbnail,
            "video_tracks": [asdict(vt) for vt in self.video_tracks],
            "audio_tracks": [asdict(at) for at in self.audio_tracks],
            "audio_options": [
                {
                    "format_id": at.format_id,
                    "language": at.language,
                    "label": at.display_label,
                    "codec": at.acodec,
                    "bitrate": at.abr,
                }
                for at in self.audio_tracks
            ],
            "subtitles": [
                {
                    "language": st.language,
                    "name": st.name,
                    "url": st.url,
                    "ext": st.ext,
                    "is_external": st.is_external,
                    "label": st.display_label,
                }
                for st in self.subtitles
            ],
            "unique_resolutions": self.unique_resolutions,
            "headers": self.headers,
            "local_manifest_path": self.local_manifest_path,
        }


# ============================================================================
# ASYNC UTILITY HELPER
# ============================================================================

def _run_async(coro):
    """Run an async coroutine safely whether in main thread, worker, or existing loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def sanitize_manifest_text(manifest_text: str, base_url: str = "") -> str:
    """
    Sanitizes HLS manifest text:
    1. Converts relative URIs to absolute URIs using base_url.
    2. Rewrites subtitle URIs lacking standard extensions by appending '#playlist.m3u8'
       to avoid yt-dlp _UnsafeExtensionError and ensure m3u8_native VTT handling.
    """
    if not manifest_text or ("#EXTM3U" not in manifest_text and "<MPD" not in manifest_text):
        return manifest_text

    lines = []
    for line in manifest_text.splitlines():
        trimmed = line.strip()
        if not trimmed:
            lines.append(line)
            continue

        if trimmed.startswith("#EXT-X-MEDIA:"):
            def _fix_media_uri(match):
                pre = match.group(1)
                uri = match.group(2)
                post = match.group(3)
                if base_url and not uri.startswith(("http://", "https://")):
                    uri = urljoin(base_url, uri)
                if "TYPE=SUBTITLES" in trimmed:
                    clean = uri.split("?")[0].split("#")[0]
                    ext = os.path.splitext(clean)[1].lower()
                    if ext not in (".vtt", ".srt", ".ttml", ".m3u8", ".ass", ".dfxp"):
                        base_uri = uri.split("#")[0]
                        uri = f"{base_uri}#playlist.m3u8"
                return f"{pre}{uri}{post}"

            line = re.sub(r'(URI=")([^"]+)(")', _fix_media_uri, line)

        elif not trimmed.startswith("#"):
            if base_url and not trimmed.startswith(("http://", "https://")):
                line = urljoin(base_url, trimmed)

        lines.append(line)

    return "\n".join(lines)


def parse_hls_manifest_tracks(
    manifest_text: str,
    base_url: str = ""
) -> Tuple[List[VideoTrackOption], List[AudioTrackOption], List[SubtitleTrackOption]]:
    """
    Direct in-memory M3U8 Master Manifest parser.
    Extracts video resolutions, codecs, audio renditions, and subtitles directly
    from manifest text without triggering additional HTTP requests.
    """
    video_tracks: List[VideoTrackOption] = []
    audio_tracks: List[AudioTrackOption] = []
    subtitles: List[SubtitleTrackOption] = []

    seen_v = set()
    seen_a = set()
    seen_s = set()

    for line in manifest_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            bw_m = re.search(r"BANDWIDTH=(\d+)", line)
            fps_m = re.search(r"FRAME-RATE=([\d\.]+)", line)
            codecs_m = re.search(r'CODECS="([^"]+)"', line)

            h = int(res_m.group(2)) if res_m else 0
            w = int(res_m.group(1)) if res_m else 0
            bw = int(bw_m.group(1)) if bw_m else 0
            fps = int(float(fps_m.group(1))) if fps_m else None
            codecs = codecs_m.group(1) if codecs_m else ""

            fid = str(bw) if bw else f"{h}p"
            vkey = (h, fps, codecs)
            if h > 0 and vkey not in seen_v:
                seen_v.add(vkey)
                video_tracks.append(
                    VideoTrackOption(
                        format_id=fid,
                        resolution=f"{w}x{h}" if w and h else f"{h}p",
                        height=h,
                        fps=fps,
                        vcodec=codecs.split(",")[0] if codecs else "",
                        tbr=round(bw / 1000.0, 1) if bw else None,
                    )
                )

        elif line.startswith("#EXT-X-MEDIA:"):
            type_m = re.search(r"TYPE=(AUDIO|SUBTITLES)", line)
            name_m = re.search(r'NAME="([^"]+)"', line)
            lang_m = re.search(r'LANGUAGE="([^"]+)"', line)
            uri_m = re.search(r'URI="([^"]+)"', line)

            m_type = type_m.group(1) if type_m else ""
            m_name = name_m.group(1) if name_m else ""
            m_lang = lang_m.group(1) if lang_m else (m_name.lower()[:3] if m_name else "und")
            m_uri = uri_m.group(1) if uri_m else ""
            if base_url and m_uri and not m_uri.startswith(("http://", "https://")):
                m_uri = urljoin(base_url, m_uri)

            if m_type == "AUDIO":
                akey = (m_lang, m_name)
                if akey not in seen_a:
                    seen_a.add(akey)
                    audio_tracks.append(
                        AudioTrackOption(
                            format_id=f"audio-{m_lang}",
                            language=m_lang,
                            acodec="aac",
                        )
                    )
            elif m_type == "SUBTITLES":
                skey = (m_lang, m_name)
                if skey not in seen_s:
                    seen_s.add(skey)
                    subtitles.append(
                        SubtitleTrackOption(
                            language=m_lang,
                            name=m_name,
                            url=m_uri,
                            ext="vtt",
                            is_external=False,
                        )
                    )

    video_tracks.sort(key=lambda vt: (vt.height, vt.fps or 0), reverse=True)

    return video_tracks, audio_tracks, subtitles


def parse_dash_manifest_tracks(
    manifest_text: str,
    base_url: str = ""
) -> Tuple[List[VideoTrackOption], List[AudioTrackOption], List[SubtitleTrackOption]]:
    """
    Direct in-memory MPEG-DASH (.mpd) XML parser.
    Extracts video resolutions, codecs, audio renditions, and subtitle tracks
    from MPD XML text without triggering additional HTTP requests.
    """
    video_tracks: List[VideoTrackOption] = []
    audio_tracks: List[AudioTrackOption] = []
    subtitles: List[SubtitleTrackOption] = []
    if not manifest_text or "<MPD" not in manifest_text:
        return video_tracks, audio_tracks, subtitles

    try:
        import xml.etree.ElementTree as ET
        # Strip default XML namespaces for uniform querying across different MPD versions
        xml_clean = re.sub(r' xmlns="[^"]+"', '', manifest_text, count=1)
        root = ET.fromstring(xml_clean)

        seen_v = set()
        seen_a = set()
        seen_s = set()

        for aset in root.iter("AdaptationSet"):
            mime = (aset.attrib.get("mimeType") or "").lower()
            content_type = (aset.attrib.get("contentType") or "").lower()
            lang = (aset.attrib.get("lang") or "und").lower()

            for rep in aset.iter("Representation"):
                rep_id = rep.attrib.get("id") or ""
                bw = int(rep.attrib.get("bandwidth", 0) or 0)
                h = int(rep.attrib.get("height", 0) or 0)
                w = int(rep.attrib.get("width", 0) or 0)
                codecs = rep.attrib.get("codecs") or aset.attrib.get("codecs") or ""
                fps_str = rep.attrib.get("frameRate") or ""
                fps = int(float(fps_str.split("/")[0])) if fps_str else None

                is_video = "video" in mime or content_type == "video" or h > 0
                is_audio = "audio" in mime or content_type == "audio" or bool(rep.attrib.get("audioSamplingRate"))
                is_sub = "text" in mime or "subtitles" in mime or content_type == "text" or "vtt" in mime

                if is_video and h > 0:
                    vkey = (h, fps, codecs)
                    if vkey not in seen_v:
                        seen_v.add(vkey)
                        video_tracks.append(
                            VideoTrackOption(
                                format_id=rep_id or f"{h}p",
                                resolution=f"{w}x{h}" if w and h else f"{h}p",
                                height=h,
                                fps=fps,
                                vcodec=codecs.split(".")[0] if codecs else "",
                                tbr=round(bw / 1000.0, 1) if bw else None,
                            )
                        )
                elif is_audio:
                    akey = (lang, rep_id)
                    if akey not in seen_a:
                        seen_a.add(akey)
                        audio_tracks.append(
                            AudioTrackOption(
                                format_id=rep_id or f"audio-{lang}",
                                language=lang,
                                acodec=codecs.split(".")[0] if codecs else "aac",
                                abr=round(bw / 1000.0, 1) if bw else None,
                            )
                        )
                elif is_sub:
                    skey = (lang, rep_id)
                    if skey not in seen_s:
                        seen_s.add(skey)
                        subtitles.append(
                            SubtitleTrackOption(
                                language=lang,
                                name=f"Subtitle [{lang}]",
                                url="",
                                ext="vtt",
                                is_external=False,
                            )
                        )

        video_tracks.sort(key=lambda vt: (vt.height, vt.fps or 0), reverse=True)
    except Exception as e:
        logger.debug(f"Direct MPD XML parse notice: {e}")

    return video_tracks, audio_tracks, subtitles


# ============================================================================
# WEBPAGE STREAM SNIFFER
# ============================================================================

class WebpageStreamSniffer:
    """
    Automated stream discovery engine.
    Uses Playwright headless browser to inspect all frames, trigger lazy players,
    and capture network requests (HLS, DASH, direct video/audio, and subtitles).
    Provides static fallback for lightweight or direct parsing.
    """

    def __init__(self):
        self.captured_streams: Dict[str, Dict[str, str]] = {}
        self.captured_subtitles: Dict[str, Dict[str, str]] = {}
        self.captured_dom_media: List[str] = []
        self.captured_manifest_texts: Dict[str, str] = {}
        self.captured_page_title: str = ""
        self.captured_ciphers: List[Dict[str, Any]] = []
        self.captured_media_manifests: Dict[str, str] = {}
        self.captured_cookies: List[Dict[str, Any]] = []

    async def _on_response(self, response: Any):
        try:
            url = response.url
            if not url or url.startswith(("blob:", "data:", "javascript:")):
                return

            # Skip individual segment files
            if IGNORE_SEGMENTS.search(url):
                return

            ctype = response.headers.get("content-type", "").lower()
            req_headers = {}
            for k, v in response.request.headers.items():
                if k.startswith(":"):
                    continue
                if k.lower() == "sec-ch-ua" and "HeadlessChrome" in v:
                    v = v.replace("HeadlessChrome", "Google Chrome")
                req_headers[k] = v

            # Ensure Referer and Origin are well formed for CDN bypass
            frame_url = getattr(response.frame, "url", None)
            if "Referer" not in req_headers and frame_url and frame_url.startswith("http"):
                req_headers["Referer"] = frame_url

            if "Referer" in req_headers and "Origin" not in req_headers:
                try:
                    p = urlparse(req_headers["Referer"])
                    req_headers["Origin"] = f"{p.scheme}://{p.netloc}"
                except Exception:
                    pass

            # 1. Manifest detection (HLS / DASH)
            if (
                "mpegurl" in ctype
                or "dash+xml" in ctype
                or MANIFEST_EXT.search(url)
            ):
                self.captured_streams[url] = req_headers
                try:
                    body_text = await response.text()
                    if body_text and ("#EXTM3U" in body_text or "<MPD" in body_text):
                        self.captured_manifest_texts[url] = sanitize_manifest_text(body_text, base_url=url)
                except Exception:
                    pass

            # 2. Subtitle detection
            elif (
                "text/vtt" in ctype
                or "subrip" in ctype
                or SUB_EXT.search(url)
            ):
                self.captured_subtitles[url] = req_headers

            # 3. Direct video or audio stream
            elif (
                ctype.startswith(("video/", "audio/"))
                or MEDIA_EXT.search(url)
            ):
                content_length = int(response.headers.get("content-length", 0) or 0)
                if content_length == 0 or content_length > 100 * 1024:
                    self.captured_streams[url] = req_headers

        except Exception as e:
            logger.debug(f"Sniffer response parsing error: {e}")

    def _on_request(self, request: Any):
        try:
            url = request.url
            if not url or url.startswith(("blob:", "data:")) or IGNORE_SEGMENTS.search(url):
                return

            req_headers = {k: v for k, v in request.headers.items() if not k.startswith(":")}
            if MANIFEST_EXT.search(url):
                self.captured_streams[url] = req_headers
            elif SUB_EXT.search(url):
                self.captured_subtitles[url] = req_headers
            elif MEDIA_EXT.search(url):
                self.captured_streams[url] = req_headers
        except Exception:
            pass

    async def _sniff_with_playwright(
        self,
        page_url: str,
        timeout_s: int = 25,
        progress_cb: Optional[Callable[[str], None]] = None
    ):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is not installed.")

        if progress_cb:
            progress_cb("Launching headless browser...")

        self.captured_streams.clear()
        self.captured_subtitles.clear()
        self.captured_dom_media.clear()
        self.captured_manifest_texts.clear()
        self.captured_page_title = ""
        self.captured_ciphers.clear()
        self.captured_media_manifests.clear()
        self.captured_cookies.clear()

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-web-security",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                    "--mute-audio",
                ]
            )

            context = await browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=1,
                locale="en-US",
            )

            # Evasion init script and in-browser WebCrypto stream manifest & cipher hook
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.navigator.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

                window.__captured_manifests = [];
                window.__captured_ciphers = [];

                try {
                    const origDecrypt = crypto.subtle.decrypt;
                    crypto.subtle.decrypt = async function(algo, key, data) {
                        const res = await origDecrypt.apply(this, arguments);
                        try {
                            if (res) {
                                const text = new TextDecoder().decode(res);
                                if (text.includes("#EXTM3U") || text.includes("<MPD")) {
                                    window.__captured_manifests.push({
                                        text: text,
                                        url: window.location.href,
                                        is_master: text.includes("#EXT-X-STREAM-INF")
                                    });
                                }
                            }
                        } catch(e) {}
                        return res;
                    };
                } catch(e) {}

                try {
                    const origImportKey = crypto.subtle.importKey;
                    crypto.subtle.importKey = async function(format, keyData, algo, extractable, keyUsages) {
                        try {
                            if (format === 'raw' && keyData) {
                                const hex = Array.from(new Uint8Array(keyData)).map(b => b.toString(16).padStart(2, '0')).join('');
                                const algoName = typeof algo === 'string' ? algo : (algo ? algo.name : '');
                                window.__captured_ciphers.push({
                                    algo: algoName,
                                    keyHex: hex,
                                    usages: keyUsages
                                });
                            }
                        } catch(e) {}
                        return origImportKey.apply(this, arguments);
                    };
                } catch(e) {}
            """)

            # Create main page FIRST so popup listener doesn't dismiss the main tab
            page = await context.new_page()

            # Automatically close intrusive ad / redirect popups
            context.on("page", lambda popup: asyncio.create_task(self._safely_close_popup(popup, main_page=page)))

            page.on("response", lambda res: asyncio.create_task(self._on_response(res)))
            page.on("request", self._on_request)

            if progress_cb:
                progress_cb(f"Navigating to webpage: {page_url[:65]}...")

            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                try:
                    pt = await page.title()
                    if pt and pt.strip():
                        self.captured_page_title = pt.strip()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Initial page navigation timeout or error: {e}")

            if progress_cb:
                progress_cb("Inspecting webpage structure & lazy loading...")

            # Scroll to trigger lazy loading & allow embedded players to initialize
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
                await asyncio.sleep(2.5)
            except Exception:
                pass

            if progress_cb:
                progress_cb("Scanning frames & triggering media players...")

            # Click common video player play buttons across main page and all nested iframes
            selectors = [
                "button[aria-label*='Play' i]",
                "button[title*='Play' i]",
                "button[aria-label*='reproduce' i]",
                "button[aria-label*='lire' i]",
                "button[aria-label*='regarder' i]",
                ".vjs-big-play-button",
                ".jw-icon-display",
                ".jw-display-icon-container",
                ".plyr__control--overlaid",
                ".play-button",
                ".btn-play",
                ".play-btn",
                ".click-to-play",
                "[class*='play-button' i]",
                "[class*='play_button' i]",
                "[class*='play-icon' i]",
                "[id*='player' i] button",
                "[class*='player' i] button",
                "button",
                "video",
                ".fp-ui",
                ".mejs__overlay-play",
                ".dplayer-play-icon",
                ".art-state",
            ]

            for frame in page.frames:
                # 1. Try clicking play buttons
                for sel in selectors:
                    try:
                        btn = frame.locator(sel).first
                        if await btn.is_visible(timeout=250):
                            await btn.click(timeout=500, force=True)
                            await asyncio.sleep(0.3)
                    except Exception:
                        pass

                # 2. Try evaluating video.play() on HTML5 video elements & custom players in frame
                try:
                    await frame.evaluate("""() => {
                        document.querySelectorAll('video, audio, media-player').forEach(el => {
                            try {
                                el.muted = true;
                                el.playsInline = true;
                                const p = el.play();
                                if (p && p.catch) p.catch(() => {});
                            } catch(e) {}
                        });
                        // Vidstack / custom HLS provider integration
                        document.querySelectorAll('media-player').forEach(mp => {
                            try {
                                if (mp.provider && mp.provider.hls && mp.provider.hls.levels) {
                                    const lvls = mp.provider.hls.levels;
                                    if (lvls && lvls.length > 0) {
                                        let maxIdx = 0, maxH = 0;
                                        lvls.forEach((l, i) => {
                                            if (l.height > maxH) { maxH = l.height; maxIdx = i; }
                                        });
                                        mp.provider.hls.currentLevel = maxIdx;
                                    }
                                }
                            } catch(e) {}
                        });
                    }""")
                except Exception:
                    pass

                # 3. Collect media and streams from DOM attributes & JavaScript player instances
                try:
                    dom_sources = await frame.evaluate("""() => {
                        const urls = [];
                        const attrs = ['src', 'data-src', 'data-url', 'data-stream', 'data-hls', 'data-dash', 'data-video', 'data-mp4'];
                        document.querySelectorAll('video, audio, source, media-player, [data-src], [data-url], [data-stream], [data-hls], [data-dash]').forEach(el => {
                            attrs.forEach(a => {
                                const v = el.getAttribute ? el.getAttribute(a) : el[a];
                                if (v && typeof v === 'string' && !v.startsWith('blob:') && !v.startsWith('data:') && !v.startsWith('javascript:')) {
                                    urls.push(v);
                                } else if (v && typeof v === 'object' && v.src && typeof v.src === 'string') {
                                    urls.push(v.src);
                                }
                            });
                        });
                        // Video.js data-setup or JSON configuration
                        document.querySelectorAll('[data-setup]').forEach(el => {
                            try {
                                const json = JSON.parse(el.getAttribute('data-setup'));
                                if (json && json.sources) {
                                    json.sources.forEach(s => { if (s && s.src) urls.push(s.src); });
                                }
                            } catch(e) {}
                        });
                        // Common player runtime instances on window
                        try {
                            if (window.hls && window.hls.url) urls.push(window.hls.url);
                            if (window.player && window.player.src) urls.push(window.player.src);
                            if (window.dp && window.dp.video && window.dp.video.src) urls.push(window.dp.video.src);
                            if (window.art && window.art.option && window.art.option.url) urls.push(window.art.option.url);
                            if (typeof window.jwplayer === 'function') {
                                const jw = window.jwplayer();
                                if (jw && jw.getPlaylist) {
                                    const pl = jw.getPlaylist();
                                    if (pl && pl[0] && pl[0].file) urls.push(pl[0].file);
                                }
                            }
                        } catch(e) {}
                        return urls;
                    }""")
                    if dom_sources:
                        self.captured_dom_media.extend(dom_sources)
                except Exception:
                    pass

            # Wait for manifest and segment requests to fire over the wire
            if progress_cb:
                progress_cb("Capturing streaming manifest & subtitle network traffic...")
            await asyncio.sleep(5.0)

            # Harvest in-browser decrypted manifests and cryptographic keys across all frames
            for frame in page.frames:
                try:
                    manifests = await frame.evaluate("() => window.__captured_manifests || []")
                    for m in manifests:
                        txt = m.get("text", "")
                        if txt and ("#EXTM3U" in txt or "<MPD" in txt):
                            m_url = m.get("url") or page_url
                            if m.get("is_master"):
                                self.captured_manifest_texts[m_url] = sanitize_manifest_text(txt, base_url=m_url)
                                self.captured_streams[m_url] = {"User-Agent": DEFAULT_USER_AGENT, "Referer": page_url}
                            else:
                                self.captured_media_manifests[m_url] = txt
                                if m_url not in self.captured_manifest_texts:
                                    self.captured_manifest_texts[m_url] = sanitize_manifest_text(txt, base_url=m_url)
                                    self.captured_streams[m_url] = {"User-Agent": DEFAULT_USER_AGENT, "Referer": page_url}

                    ciphers = await frame.evaluate("() => window.__captured_ciphers || []")
                    for c in ciphers:
                        if c.get("keyHex"):
                            self.captured_ciphers.append(c)
                except Exception:
                    pass

            # Capture context cookies safely (including Cloudflare clearance, tokens, etc.)
            try:
                browser_cookies = await context.cookies()
                if browser_cookies:
                    self.captured_cookies = list(browser_cookies)
            except Exception:
                pass

            await browser.close()

    async def _safely_close_popup(self, popup: Any, main_page: Any = None):
        """Silently dismiss advertising / redirect popup tabs."""
        if main_page is not None and popup == main_page:
            return
        try:
            await asyncio.sleep(0.4)
            await popup.close()
            logger.info("Automatically closed intrusive popup tab.")
        except Exception:
            pass

    def _sniff_static_fallback(self, page_url: str) -> Dict[str, Dict[str, str]]:
        """Static scraping fallback when Playwright is unavailable or finds no streams."""
        found: Dict[str, Dict[str, str]] = {}
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": page_url}

        try:
            resp = None
            try:
                from curl_cffi import requests as cffi_requests
                resp = cffi_requests.get(page_url, headers=headers, impersonate="chrome", timeout=12)
            except Exception:
                resp = requests.get(page_url, headers=headers, timeout=12)

            if resp and resp.status_code == 200:
                html = resp.text
                clean_html = html.replace(r"\/", "/")

                # 1. Direct Regex for m3u8 and mpd
                for m in re.finditer(r'https?://[^\s"\'<>]+\.(m3u8|mpd)[^\s"\'<>]*', clean_html):
                    found[m.group(0)] = headers

                # 2. Direct Regex for mp4/webm/m4v
                for m in re.finditer(r'https?://[^\s"\'<>]+\.(mp4|webm|m4v)[^\s"\'<>]*', clean_html):
                    found[m.group(0)] = headers

                # 3. Search for base64 encoded media streams
                for b64_match in re.findall(r'[A-Za-z0-9+/=]{24,}', html):
                    try:
                        import base64
                        dec = base64.b64decode(b64_match).decode('utf-8', errors='ignore')
                        if dec.startswith(('http://', 'https://')) and ('.m3u8' in dec or '.mpd' in dec or '.mp4' in dec):
                            found[dec] = headers
                    except Exception:
                        pass

                # 4. Check for iframe sources
                iframe_srcs = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
                for isrc in iframe_srcs[:4]:
                    full_iframe = urljoin(page_url, isrc)
                    try:
                        try:
                            from curl_cffi import requests as cffi_requests
                            sub_r = cffi_requests.get(full_iframe, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": page_url}, impersonate="chrome", timeout=8)
                        except Exception:
                            sub_r = requests.get(full_iframe, headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": page_url}, timeout=8)
                        if sub_r and sub_r.status_code == 200:
                            sub_html = sub_r.text.replace(r"\/", "/")
                            for m in re.finditer(r'https?://[^\s"\'<>]+\.(m3u8|mpd|mp4)[^\s"\'<>]*', sub_html):
                                found[m.group(0)] = {"User-Agent": DEFAULT_USER_AGENT, "Referer": full_iframe}
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Static sniffer fallback failed: {e}")

        return found

    def discover_streams(
        self,
        target_url: str,
        timeout_s: int = 25,
        progress_cb: Optional[Callable[[str], None]] = None
    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
        """
        Discovers media streams and subtitles for any target URL.
        Returns:
            (stream_candidates, subtitle_candidates)
        """
        target_url = target_url.strip()

        # Step 0: If target_url is ALREADY a direct manifest or media URL, bypass browser
        if MANIFEST_EXT.search(target_url) or MEDIA_EXT.search(target_url):
            if progress_cb:
                progress_cb("Direct media manifest or file recognized. Bypassing browser.")
            headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": target_url}
            return {target_url: headers}, {}

        # Step 1: Run Playwright sniffer
        if PLAYWRIGHT_AVAILABLE:
            try:
                _run_async(self._sniff_with_playwright(target_url, timeout_s=timeout_s, progress_cb=progress_cb))
            except Exception as e:
                logger.warning(f"Playwright sniffer error: {e}. Falling back to static analyzer.")
                if progress_cb:
                    progress_cb(f"Browser scan notice: {e}. Checking fallback...")

        # Step 2: Merge DOM media items
        for durl in self.captured_dom_media:
            if durl not in self.captured_streams and not IGNORE_SEGMENTS.search(durl):
                self.captured_streams[durl] = {"User-Agent": DEFAULT_USER_AGENT, "Referer": target_url}

        # Step 3: If no streams found yet, run static fallback
        if not self.captured_streams:
            if progress_cb:
                progress_cb("Running deep HTML parser fallback...")
            fallback_found = self._sniff_static_fallback(target_url)
            self.captured_streams.update(fallback_found)

        return dict(self.captured_streams), dict(self.captured_subtitles)


# ============================================================================
# UNIVERSAL MEDIA DOWNLOADER ENGINE
# ============================================================================

class UniversalMediaDownloader:
    """
    Main manager for Unknown Media (Video+Audio+Subtitle) extraction,
    quality negotiation, DRM detection, and FFmpeg muxing.
    """

    def __init__(self, output_dir: Optional[str] = None):
        if output_dir:
            self.output_dir = output_dir
        else:
            self.output_dir = getattr(Config, "UNKNOWN_DIR", os.path.join(Config.BASE_DIR, "unknown"))
        os.makedirs(self.output_dir, exist_ok=True)
        self.sniffer = WebpageStreamSniffer()
        self.db = DatabaseManager()

    @staticmethod
    def _write_netscape_cookiefile(cookies: List[Dict[str, Any]]) -> Optional[str]:
        """Writes browser cookies into a standard Netscape cookie file for yt-dlp."""
        if not cookies:
            return None
        try:
            f = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8")
            f.write("# Netscape HTTP Cookie File\n")
            for c in cookies:
                domain = c.get("domain", "")
                if not domain:
                    continue
                if not domain.startswith(".") and not domain.startswith("http"):
                    domain = "." + domain
                flag = "TRUE" if domain.startswith(".") else "FALSE"
                path = c.get("path", "/") or "/"
                secure = "TRUE" if c.get("secure") else "FALSE"
                expiry = str(int(c.get("expires", 0) or 2147483647))
                name = c.get("name", "")
                val = c.get("value", "")
                if name:
                    f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{val}\n")
            f.close()
            return f.name
        except Exception as e:
            logger.warning(f"Failed to create temporary cookiefile: {e}")
            return None

    def analyze_url(
        self,
        url: str,
        timeout_s: int = 25,
        progress_cb: Optional[Callable[[str], None]] = None
    ) -> List[StreamCandidate]:
        """
        Analyze any webpage or stream URL, discover all available media streams,
        parse their manifest qualities, audio tracks, and subtitles.
        """
        streams, subtitles = self.sniffer.discover_streams(url, timeout_s=timeout_s, progress_cb=progress_cb)

        candidates: List[StreamCandidate] = []

        if not streams:
            # Last ditch attempt: let yt-dlp try extracting directly from the webpage
            if progress_cb:
                progress_cb("Checking generic media extractor directly on URL...")
            streams[url] = {"User-Agent": DEFAULT_USER_AGENT, "Referer": url}

        # Inspect stream candidates (prioritize captured manifests and HLS/DASH)
        sorted_stream_urls = sorted(
            streams.keys(),
            key=lambda u: (
                0 if (u in self.sniffer.captured_manifest_texts or ".m3u8" in u.lower()) else (1 if ".mpd" in u.lower() else 2)
            )
        )

        for stream_url in sorted_stream_urls:
            headers = streams.get(stream_url, {})
            candidate = self._inspect_stream(
                stream_url=stream_url,
                page_url=url,
                headers=headers,
                captured_subtitles=subtitles,
                progress_cb=progress_cb
            )
            if candidate:
                candidates.append(candidate)

        # Filter out bare sub-playlists (e.g. single subtitle or audio segments) if a full video stream exists
        has_playable = any(len(c.video_tracks) > 0 for c in candidates)
        if has_playable:
            candidates = [c for c in candidates if len(c.video_tracks) > 0 or len(c.audio_tracks) > 0]

        # Sort so master stream with most video resolutions is at index 0
        candidates.sort(
            key=lambda c: (
                1 if not c.is_drm else 0,
                len(c.video_tracks),
                max(c.unique_resolutions) if c.unique_resolutions else 0,
                len(c.subtitles),
            ),
            reverse=True
        )

        return candidates

    def _inspect_stream(
        self,
        stream_url: str,
        page_url: str,
        headers: Dict[str, str],
        captured_subtitles: Dict[str, Dict[str, str]],
        progress_cb: Optional[Callable[[str], None]] = None
    ) -> Optional[StreamCandidate]:
        """Inspect a stream using yt-dlp with captured headers to extract available tracks."""
        if progress_cb:
            progress_cb(f"Inspecting stream format tracks: {stream_url[:60]}...")

        # Check if we intercepted the manifest body in memory
        manifest_text = self.sniffer.captured_manifest_texts.get(stream_url, "")
        local_manifest_path = ""
        target_probe_url = stream_url

        # Determine stream type
        lower_url = stream_url.lower()
        if (manifest_text and "#EXTM3U" in manifest_text) or ".m3u8" in lower_url or "mpegurl" in lower_url:
            stype = "hls"
        elif (manifest_text and "<MPD" in manifest_text) or ".mpd" in lower_url or "dash" in lower_url:
            stype = "dash"
        else:
            stype = "direct"

        if not manifest_text and stype == "hls" and stream_url.startswith(("http://", "https://")):
            try:
                r_m = None
                try:
                    from curl_cffi import requests as cffi_requests
                    r_m = cffi_requests.get(stream_url, headers=headers, impersonate="chrome", timeout=8)
                except Exception:
                    r_m = requests.get(stream_url, headers=headers, timeout=8)
                if r_m and r_m.status_code == 200 and "#EXTM3U" in r_m.text:
                    manifest_text = r_m.text
            except Exception as e:
                logger.debug(f"Direct manifest fetch probe skipped: {e}")

        if manifest_text:
            try:
                manifest_text = sanitize_manifest_text(manifest_text, base_url=stream_url)
                self.sniffer.captured_manifest_texts[stream_url] = manifest_text
                suffix = ".m3u8" if (stype == "hls" or "EXTM3U" in manifest_text) else ".mpd"
                tmp_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode="w", encoding="utf-8")
                tmp_file.write(manifest_text)
                tmp_file.close()
                local_manifest_path = tmp_file.name
                target_probe_url = pathlib.Path(local_manifest_path).resolve().as_uri()
            except Exception as e:
                logger.warning(f"Could not write captured manifest to temp file: {e}")

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "http_headers": headers,
            "extract_flat": False,
            "enable_file_urls": True,
        }
        if HAVE_IMPERSONATE:
            try:
                ydl_opts["impersonate"] = ImpersonateTarget("chrome")
            except Exception:
                pass

        cookiefile_path = self._write_netscape_cookiefile(self.sniffer.captured_cookies)
        if cookiefile_path:
            ydl_opts["cookiefile"] = cookiefile_path

        info = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_probe_url, download=False)
        except Exception as e:
            err_msg = str(e)
            logger.warning(f"yt-dlp stream inspection error for {target_probe_url}: {e}")
        finally:
            if cookiefile_path and os.path.exists(cookiefile_path):
                try:
                    os.remove(cookiefile_path)
                except Exception:
                    pass

        # Parse in-memory manifest tracks as direct fallback / augment
        parsed_v: List[VideoTrackOption] = []
        parsed_a: List[AudioTrackOption] = []
        parsed_s: List[SubtitleTrackOption] = []
        if manifest_text:
            if "<MPD" in manifest_text or stype == "dash":
                parsed_v, parsed_a, parsed_s = parse_dash_manifest_tracks(manifest_text, base_url=stream_url)
            else:
                parsed_v, parsed_a, parsed_s = parse_hls_manifest_tracks(manifest_text, base_url=stream_url)


        # If yt-dlp failed completely but we have parsed manifest tracks, construct candidate directly
        cipher_info = None
        aes_ctr = next((c for c in self.sniffer.captured_ciphers if c.get("algo") == "AES-CTR"), None)
        if aes_ctr and HAVE_CRYPTO:
            cipher_info = {
                "type": "aes-ctr-xor",
                "keyHex": aes_ctr["keyHex"],
                "segmentBytes": 8192
            }

        if not info and (parsed_v or parsed_a or parsed_s):
            title = self.sniffer.captured_page_title or os.path.splitext(os.path.basename(urlparse(stream_url).path))[0] or "Discovered Media Stream"
            all_subs = list(parsed_s)
            seen_sub_urls = {s.url for s in all_subs if s.url}
            for ext_sub_url, ext_headers in captured_subtitles.items():
                if ext_sub_url not in seen_sub_urls:
                    all_subs.append(
                        SubtitleTrackOption(
                            language="und",
                            name=os.path.basename(urlparse(ext_sub_url).path),
                            url=ext_sub_url,
                            ext="vtt" if ".vtt" in ext_sub_url.lower() else "srt",
                            is_external=True,
                            headers=ext_headers,
                        )
                    )
            return StreamCandidate(
                url=stream_url,
                type=stype,
                headers=headers,
                title=title,
                page_url=page_url,
                video_tracks=parsed_v,
                audio_tracks=parsed_a,
                subtitles=all_subs,
                local_manifest_path=local_manifest_path,
                manifest_content=manifest_text,
                cipher_info=cipher_info,
                media_manifests=dict(self.sniffer.captured_media_manifests),
                cookies=list(self.sniffer.captured_cookies),
            )

        if not info:
            if "403" in str(err_msg) or "401" in str(err_msg):
                return StreamCandidate(
                    url=stream_url,
                    type=stype,
                    headers=headers,
                    title="Access Restricted or Expired Token Stream",
                    page_url=page_url,
                    is_drm=False,
                    drm_notice="HTTP 403 Forbidden: Stream token or CDN signature may have expired.",
                )
            return None

        raw_title = info.get("title") or ""
        if not raw_title or raw_title.startswith("tmp") or raw_title.lower() in ("master", "playlist", "video") or len(raw_title) < 4:
            title = self.sniffer.captured_page_title or os.path.splitext(os.path.basename(urlparse(stream_url).path))[0] or "Discovered Media Stream"
        else:
            title = raw_title
        duration_s = float(info.get("duration") or 0.0)
        thumbnail = info.get("thumbnail") or ""

        # Check for DRM protection
        is_drm = False
        drm_notice = ""
        formats = info.get("formats", [])

        # 1. Manifest text check for DRM keys
        if stype in ("hls", "dash"):
            try:
                probe_text = manifest_text
                if not probe_text:
                    r_probe = requests.get(stream_url, headers=headers, timeout=6)
                    if r_probe.status_code == 200:
                        probe_text = r_probe.text
                if probe_text and DRM_PATTERNS.search(probe_text):
                    is_drm = True
                    drm_notice = "Protected with DRM encryption (Widevine / PlayReady / FairPlay). DRM-protected streams cannot be decrypted."
            except Exception:
                pass

        # 2. Check format info
        for f in formats:
            if f.get("has_drm") or f.get("vcodec") in ("encv", "enca"):
                is_drm = True
                drm_notice = "Protected with DRM encryption (Widevine / PlayReady / FairPlay). DRM-protected streams cannot be decrypted."
                break

        # Video tracks
        video_tracks: List[VideoTrackOption] = []
        seen_v_keys = set()
        for f in formats:
            h = f.get("height")
            if h and h > 0:
                fid = str(f.get("format_id", ""))
                vkey = (h, f.get("fps"), f.get("vcodec"))
                if vkey not in seen_v_keys:
                    seen_v_keys.add(vkey)
                    video_tracks.append(
                        VideoTrackOption(
                            format_id=fid,
                            resolution=f.get("resolution") or f"{f.get('width', 0)}x{h}",
                            height=int(h),
                            fps=f.get("fps"),
                            vcodec=f.get("vcodec") or "",
                            tbr=f.get("tbr") or f.get("vbr"),
                            filesize_approx=f.get("filesize") or f.get("filesize_approx") or 0,
                        )
                    )

        # Merge parsed manifest tracks if yt-dlp extracted no video tracks
        if not video_tracks and parsed_v:
            video_tracks = parsed_v

        # Sort video tracks by height descending
        video_tracks.sort(key=lambda vt: (vt.height, vt.fps or 0), reverse=True)

        # Audio tracks
        audio_tracks: List[AudioTrackOption] = []
        seen_a_keys = set()
        for f in formats:
            if f.get("vcodec") == "none" and f.get("acodec") != "none":
                fid = str(f.get("format_id", ""))
                lang = f.get("language") or f.get("format_note") or "und"
                akey = (lang, f.get("acodec"), f.get("abr"))
                if akey not in seen_a_keys:
                    seen_a_keys.add(akey)
                    audio_tracks.append(
                        AudioTrackOption(
                            format_id=fid,
                            language=lang,
                            acodec=f.get("acodec") or "",
                            abr=f.get("abr") or f.get("tbr"),
                            channels=f.get("audio_channels") or 2,
                        )
                    )

        if not audio_tracks and parsed_a:
            audio_tracks = parsed_a

        # Subtitles (Embedded + Captured External)
        subtitles: List[SubtitleTrackOption] = []
        seen_subs = set()

        # 1. Embedded subtitles in manifest
        embedded_subs = info.get("subtitles") or {}
        for lang_code, sub_list in embedded_subs.items():
            if sub_list and isinstance(sub_list, list):
                first_sub = sub_list[0]
                sub_url = first_sub.get("url") or ""
                sub_ext = first_sub.get("ext") or "vtt"
                if not sub_ext or len(sub_ext) > 5 or not sub_ext.isalnum() or sub_ext.lower() in ("unknown", "unknown_video"):
                    sub_ext = "vtt"
                sub_name = first_sub.get("name") or ""
                skey = (lang_code.lower(), sub_ext.lower())
                if skey not in seen_subs:
                    seen_subs.add(skey)
                    subtitles.append(
                        SubtitleTrackOption(
                            language=lang_code,
                            name=sub_name,
                            url=sub_url,
                            ext=sub_ext,
                            is_external=False,
                            headers=headers,
                        )
                    )

        if not subtitles and parsed_s:
            subtitles = list(parsed_s)
            seen_subs = {(st.language.lower(), st.ext.lower()) for st in subtitles}

        # 2. Captured external subtitles (.vtt / .srt)
        for ext_sub_url, ext_headers in captured_subtitles.items():
            guess_lang = "und"
            lower_surl = ext_sub_url.lower()
            for common_l in ("en", "eng", "es", "spa", "fr", "de", "ja", "ko", "pt", "it", "ru", "hi", "zh"):
                if f"_{common_l}." in lower_surl or f".{common_l}." in lower_surl or f"-{common_l}." in lower_surl or f"/{common_l}/" in lower_surl:
                    guess_lang = common_l
                    break
            ext = "srt" if ".srt" in lower_surl else "vtt"
            skey = (guess_lang, ext, ext_sub_url)
            if skey not in seen_subs:
                seen_subs.add(skey)
                subtitles.append(
                    SubtitleTrackOption(
                        language=guess_lang,
                        name=os.path.basename(urlparse(ext_sub_url).path),
                        url=ext_sub_url,
                        ext=ext,
                        is_external=True,
                        headers=ext_headers,
                    )
                )

        return StreamCandidate(
            url=stream_url,
            type=stype,
            headers=headers,
            title=title,
            page_url=page_url,
            is_drm=is_drm,
            drm_notice=drm_notice,
            duration_s=duration_s,
            thumbnail=thumbnail,
            video_tracks=video_tracks,
            audio_tracks=audio_tracks,
            subtitles=subtitles,
            raw_info=info,
            local_manifest_path=local_manifest_path,
            manifest_content=manifest_text,
            cipher_info=cipher_info,
            media_manifests=dict(self.sniffer.captured_media_manifests),
            cookies=list(self.sniffer.captured_cookies),
        )

    def download_candidate(
        self,
        candidate: StreamCandidate,
        selected_resolution: Optional[int] = None,
        selected_format_id: Optional[str] = None,
        selected_audio_id: Optional[str] = None,
        audio_only: bool = False,
        selected_subtitles: Optional[List[str]] = None,
        output_format: str = "mkv",
        output_dir: Optional[str] = None,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[bool, str]:
        """
        Downloads the selected video resolution, audio track, and subtitles concurrently,
        using captured HTTP headers, then muxes them cleanly with FFmpeg (-c copy).
        Returns:
            (success: bool, output_file_or_error: str)
        """
        if candidate.is_drm:
            return False, f"Cannot download stream: {candidate.drm_notice}"

        out_dir = output_dir or self.output_dir
        os.makedirs(out_dir, exist_ok=True)

        if candidate.cipher_info and HAVE_CRYPTO:
            return self._download_unscrambled_hls(
                candidate=candidate,
                selected_resolution=selected_resolution,
                selected_format_id=selected_format_id,
                selected_audio_id=selected_audio_id,
                audio_only=audio_only,
                selected_subtitles=selected_subtitles,
                output_format=output_format,
                out_dir=out_dir,
                progress_cb=progress_cb,
            )

        clean_title = sanitize_filename(candidate.title) or f"media_{int(time.time())}"
        res_tag = f"[{selected_resolution}p]" if selected_resolution else ""
        audio_tag = "[Audio]" if audio_only else ""
        base_name = f"{clean_title} {res_tag}{audio_tag}".strip()

        # Build format selection string
        if audio_only:
            if selected_audio_id:
                fmt_str = selected_audio_id
            else:
                fmt_str = "bestaudio/best"
            target_ext = "m4a" if output_format == "mp4" else "mka"
        elif selected_format_id:
            if selected_audio_id:
                fmt_str = f"{selected_format_id}+{selected_audio_id}"
            else:
                fmt_str = f"{selected_format_id}+bestaudio/best"
            target_ext = output_format
        elif selected_resolution:
            fmt_str = f"bestvideo[height<={selected_resolution}]+bestaudio/best[height<={selected_resolution}]/best"
            target_ext = output_format
        else:
            fmt_str = "bestvideo+bestaudio/best"
            target_ext = output_format

        outtmpl = os.path.join(out_dir, f"{base_name}.%(ext)s")

        # Subtitle selection
        write_subs = False
        sub_langs: List[str] = []
        if selected_subtitles:
            if "all" in [s.lower() for s in selected_subtitles]:
                write_subs = True
                sub_langs = ["all"]
            else:
                write_subs = True
                sub_langs = selected_subtitles

        # Setup Progress Hook with smooth monotonic percentage and rate limiting
        last_update = [0.0]
        last_pct = [0.0]

        def ytdlp_hook(d: Dict[str, Any]):
            if not progress_cb:
                return
            status = d.get("status")
            if status == "downloading":
                now = time.time()
                downloaded = d.get("downloaded_bytes") or 0
                speed = d.get("speed") or 0
                eta = d.get("eta") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                fi = d.get("fragment_index")
                fc = d.get("fragment_count")

                if fc is not None and fi is not None and fc > 0:
                    pct = (fi / fc) * 100.0
                    if downloaded > 0 and fi > 0:
                        est_total = int((downloaded / fi) * fc)
                        total_display = format_size(est_total)
                    elif total > 0:
                        total_display = format_size(total)
                    else:
                        total_display = f"frag {fi}/{fc}"
                elif total > 0:
                    pct = (downloaded / total) * 100.0
                    total_display = format_size(total)
                else:
                    pct = 0.0
                    total_display = "--"

                pct = min(100.0, max(last_pct[0], pct))
                last_pct[0] = pct

                if now - last_update[0] < 0.08 and pct < 100.0:
                    return
                last_update[0] = now

                progress_cb({
                    "status": "downloading",
                    "percent": round(pct, 1),
                    "downloaded": downloaded,
                    "total": total or (downloaded if pct >= 100 else 0),
                    "downloaded_str": format_size(downloaded),
                    "total_str": total_display,
                    "speed": speed,
                    "speed_str": f"{format_size(speed)}/s" if speed else "0 B/s",
                    "eta": eta,
                    "eta_str": format_duration(eta) if (eta and eta < 86400) else "--:--",
                })
            elif status == "finished":
                progress_cb({
                    "status": "muxing",
                    "percent": 99.0,
                    "message": "Stream downloaded. Muxing video, audio, and subtitle tracks with FFmpeg...",
                })

        postprocessors = [{"key": "FFmpegMetadata"}]

        # Clean & sanitize headers for CDN bypass
        sanitized_headers = {}
        for k, v in candidate.headers.items():
            if not v or not str(v).strip():
                continue
            if k.lower() == "sec-ch-ua" and "HeadlessChrome" in v:
                v = v.replace("HeadlessChrome", "Google Chrome")
            if k.startswith(":") or (k.lower() == "referer" and not str(v).strip()):
                continue
            sanitized_headers[k] = v

        # Remove raw Cookie header from http_headers so yt-dlp uses cookiefile cleanly without warnings
        sanitized_headers.pop("Cookie", None)
        sanitized_headers.pop("cookie", None)

        if "Referer" in sanitized_headers:
            sanitized_headers["Referer"] = sanitized_headers["Referer"].split("#")[0]
        elif "referer" in sanitized_headers:
            ref_val = sanitized_headers.pop("referer").split("#")[0]
            sanitized_headers["Referer"] = ref_val

        if "Referer" in sanitized_headers and "Origin" not in sanitized_headers and "origin" not in sanitized_headers:
            try:
                p_ref = urlparse(sanitized_headers["Referer"])
                sanitized_headers["Origin"] = f"{p_ref.scheme}://{p_ref.netloc}"
            except Exception:
                pass

        cookiefile_path = self._write_netscape_cookiefile(candidate.cookies)

        ydl_opts: Dict[str, Any] = {
            "format": fmt_str,
            "http_headers": sanitized_headers,
            "outtmpl": outtmpl,
            "merge_output_format": target_ext,
            "concurrent_fragment_downloads": 4,
            "socket_timeout": 15,
            "fragment_retries": 15,
            "retries": 10,
            "file_access_retries": 3,
            "enable_file_urls": True,
            "hls_prefer_native": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [ytdlp_hook],
            "postprocessors": postprocessors,
        }
        if cookiefile_path:
            ydl_opts["cookiefile"] = cookiefile_path

        if progress_cb:
            progress_cb({
                "status": "starting",
                "percent": 0.0,
                "message": f"Starting download with format '{fmt_str}'...",
            })

        # Determine target download URL (use local sanitized manifest file if available)
        target_download_url = candidate.url
        manifest_to_write = candidate.manifest_content or self.sniffer.captured_manifest_texts.get(candidate.url, "")

        if not manifest_to_write and (candidate.type == "hls" or ".m3u8" in candidate.url):
            try:
                try:
                    from curl_cffi import requests as cffi_requests
                    r_dl = cffi_requests.get(candidate.url, headers=sanitized_headers, impersonate="chrome", timeout=12)
                except Exception:
                    r_dl = requests.get(candidate.url, headers=sanitized_headers, timeout=12)
                if r_dl and r_dl.status_code == 200 and "#EXTM3U" in r_dl.text:
                    manifest_to_write = r_dl.text
            except Exception:
                pass

        if manifest_to_write:
            try:
                sanitized_text = sanitize_manifest_text(manifest_to_write, base_url=candidate.url)
                candidate.manifest_content = sanitized_text
                suffix = ".m3u8" if candidate.type == "hls" or "EXTM3U" in sanitized_text else ".mpd"
                tmp_m = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, mode="w", encoding="utf-8")
                tmp_m.write(sanitized_text)
                tmp_m.close()
                candidate.local_manifest_path = tmp_m.name
                target_download_url = pathlib.Path(tmp_m.name).resolve().as_uri()
            except Exception as e:
                logger.warning(f"Could not prepare local sanitized manifest: {e}")
        elif candidate.local_manifest_path and os.path.exists(candidate.local_manifest_path):
            target_download_url = pathlib.Path(candidate.local_manifest_path).resolve().as_uri()

        final_file = ""
        try:
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(target_download_url, download=True)
                    if info:
                        raw_dest = ydl.prepare_filename(info)
                        dest_no_ext, _ = os.path.splitext(raw_dest)
                        candidate_dest = f"{dest_no_ext}.{target_ext}"
                        if os.path.exists(candidate_dest):
                            final_file = candidate_dest
                        elif os.path.exists(raw_dest):
                            final_file = raw_dest
            except Exception as e:
                logger.error(f"Universal download failed: {e}")
                return False, f"Download failed: {e}"

            # If final_file still not identified, search output_dir for newest file matching base_name
            if not final_file or not os.path.exists(final_file):
                for f in os.listdir(out_dir):
                    if f.startswith(clean_title[:30]):
                        candidate_path = os.path.join(out_dir, f)
                        if os.path.isfile(candidate_path) and time.time() - os.path.getmtime(candidate_path) < 300:
                            final_file = candidate_path
                            break

            if not final_file or not os.path.exists(final_file):
                return False, "Download completed but output media file could not be located."

            # Ensure all requested subtitles are cleanly muxed
            if write_subs and check_ffmpeg():
                if progress_cb:
                    progress_cb({
                        "status": "muxing",
                        "percent": 99.5,
                        "message": "Verifying and muxing subtitle tracks with FFmpeg...",
                    })
                final_file = self._ensure_subtitles_muxed(
                    media_filepath=final_file,
                    candidate=candidate,
                    selected_subtitles=sub_langs,
                    clean_title=clean_title,
                    out_dir=out_dir,
                )

            # Record in database
            filesize = os.path.getsize(final_file) if os.path.exists(final_file) else 0
            try:
                self.db.add_download(
                    url=candidate.page_url or candidate.url,
                    platform="Universal",
                    title=candidate.title,
                    duration=candidate.duration_s,
                    quality=f"{selected_resolution}p" if selected_resolution else ("audio_only" if audio_only else "best"),
                    format=target_ext,
                    filesize=filesize,
                    filepath=final_file,
                    status="complete",
                    media_type="audio" if audio_only else "video",
                )
            except Exception as e:
                logger.warning(f"Could not record universal download to DB: {e}")

            if progress_cb:
                progress_cb({
                    "status": "complete",
                    "percent": 100.0,
                    "filepath": final_file,
                    "filesize": filesize,
                    "filesize_str": format_size(filesize),
                    "message": f"Saved successfully to {os.path.basename(final_file)} ({format_size(filesize)})",
                })

            return True, final_file
        finally:
            if cookiefile_path and os.path.exists(cookiefile_path):
                try:
                    os.remove(cookiefile_path)
                except Exception:
                    pass

    def _download_unscrambled_hls(
        self,
        candidate: StreamCandidate,
        selected_resolution: Optional[int] = None,
        selected_format_id: Optional[str] = None,
        selected_audio_id: Optional[str] = None,
        audio_only: bool = False,
        selected_subtitles: Optional[List[str]] = None,
        output_format: str = "mkv",
        out_dir: Optional[str] = None,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[bool, str]:
        dest_dir = out_dir or self.output_dir
        os.makedirs(dest_dir, exist_ok=True)

        clean_title = sanitize_filename(candidate.title) or f"media_{int(time.time())}"
        res_tag = f"[{selected_resolution}p]" if selected_resolution else ""
        audio_tag = "[Audio]" if audio_only else ""
        base_name = f"{clean_title} {res_tag}{audio_tag}".strip()
        target_ext = output_format if not audio_only else ("m4a" if output_format == "mp4" else "mka")
        final_file = os.path.join(dest_dir, f"{base_name}.{target_ext}")

        # 1. Locate media playlist containing segment entries
        media_m3u8 = ""
        if candidate.media_manifests:
            for m_url, m_txt in candidate.media_manifests.items():
                if "#EXTINF" in m_txt:
                    media_m3u8 = m_txt
                    break
        if not media_m3u8 and candidate.manifest_content and "#EXTINF" in candidate.manifest_content:
            media_m3u8 = candidate.manifest_content

        if not media_m3u8:
            return False, "Could not locate unencrypted media playlist with segments."

        # 2. Extract segments
        segment_urls = []
        for line in media_m3u8.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if not line.startswith(("http://", "https://")):
                    line = urljoin(candidate.url, line)
                segment_urls.append(line)

        if not segment_urls:
            return False, "No media segment URLs found in playlist."

        total_segments = len(segment_urls)
        if progress_cb:
            progress_cb({
                "status": "starting",
                "percent": 0.0,
                "message": f"Starting multi-threaded segment download ({total_segments} fragments)...",
            })

        cipher = candidate.cipher_info or {}
        key_hex = cipher.get("keyHex", "")
        key = bytes.fromhex(key_hex) if key_hex else None
        seg_bytes = cipher.get("segmentBytes", 8192)

        sanitized_headers = {}
        for k, v in candidate.headers.items():
            if not v or not str(v).strip():
                continue
            if k.lower() == "sec-ch-ua" and "HeadlessChrome" in v:
                v = v.replace("HeadlessChrome", "Google Chrome")
            if k.startswith(":"):
                continue
            sanitized_headers[k] = v

        if "Referer" in sanitized_headers:
            sanitized_headers["Referer"] = sanitized_headers["Referer"].split("#")[0]
        elif "referer" in sanitized_headers:
            sanitized_headers["Referer"] = sanitized_headers.pop("referer").split("#")[0]
        elif candidate.page_url:
            sanitized_headers["Referer"] = candidate.page_url.split("#")[0]

        temp_ts = os.path.join(dest_dir, f"temp_{int(time.time())}_{uuid.uuid4().hex[:6]}.ts")

        # Worker for segment fetch & unscramble
        def fetch_segment(idx: int, url: str) -> Tuple[int, bytearray]:
            for attempt in range(3):
                try:
                    try:
                        from curl_cffi import requests as cffi_requests
                        r = cffi_requests.get(url, headers=sanitized_headers, impersonate="chrome", timeout=15)
                    except Exception:
                        r = requests.get(url, headers=sanitized_headers, timeout=15)
                    if r.status_code == 200:
                        data = bytearray(r.content)
                        if key and seg_bytes > 0:
                            seg_id = url.split("/")[-1].split(".")[0]
                            h = hashlib.sha256(seg_id.encode("utf-8")).digest()
                            counter = h[:8] + b"\x00" * 8
                            c_aes = Cipher(algorithms.AES(key), modes.CTR(counter), backend=default_backend())
                            enc = c_aes.encryptor()
                            keystream = enc.update(b"\x00" * seg_bytes) + enc.finalize()
                            for i in range(min(seg_bytes, len(data))):
                                data[i] ^= keystream[i]
                        return idx, data
                except Exception:
                    time.sleep(0.5)
            raise RuntimeError(f"Failed to download fragment {idx} from {url}")

        downloaded_bytes = 0
        completed_count = 0
        start_time = time.time()
        last_update = 0.0
        ready_segments: Dict[int, bytearray] = {}
        next_write_idx = 0

        with open(temp_ts, "wb") as ts_file:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(fetch_segment, i, u): i for i, u in enumerate(segment_urls)}
                for fut in as_completed(futures):
                    idx, data = fut.result()
                    ready_segments[idx] = data
                    completed_count += 1
                    downloaded_bytes += len(data)

                    # Flush contiguous segments to disk in order
                    while next_write_idx in ready_segments:
                        ts_file.write(ready_segments.pop(next_write_idx))
                        next_write_idx += 1

                    elapsed = max(0.001, time.time() - start_time)
                    speed = downloaded_bytes / elapsed
                    pct = (completed_count / total_segments) * 100.0
                    rem_count = total_segments - completed_count
                    avg_frag_size = downloaded_bytes / max(1, completed_count)
                    rem_bytes = rem_count * avg_frag_size
                    eta = int(rem_bytes / max(1, speed))
                    est_total = int(total_segments * avg_frag_size)

                    now = time.time()
                    if progress_cb and (now - last_update > 0.08 or completed_count == total_segments):
                        last_update = now
                        progress_cb({
                            "status": "downloading",
                            "percent": round(pct, 1),
                            "downloaded": downloaded_bytes,
                            "total": est_total,
                            "downloaded_str": format_size(downloaded_bytes),
                            "total_str": format_size(est_total),
                            "speed": speed,
                            "speed_str": f"{format_size(speed)}/s",
                            "eta": eta,
                            "eta_str": format_duration(eta),
                        })

        if progress_cb:
            progress_cb({
                "status": "muxing",
                "percent": 99.0,
                "message": "Stream downloaded. Muxing video and audio with FFmpeg...",
            })

        # Mux to destination
        if check_ffmpeg():
            cmd = ["ffmpeg", "-y", "-i", temp_ts]
            if audio_only:
                cmd.extend(["-vn", "-c:a", "copy", final_file])
            else:
                cmd.extend(["-c", "copy", final_file])

            run_res = subprocess.run(cmd, capture_output=True, text=True)
            if run_res.returncode != 0:
                logger.error(f"FFmpeg muxing failed: {run_res.stderr[-300:]}")
                if os.path.exists(temp_ts):
                    shutil.move(temp_ts, final_file)
            else:
                if os.path.exists(temp_ts):
                    os.remove(temp_ts)
        else:
            if os.path.exists(temp_ts):
                shutil.move(temp_ts, final_file)

        if not os.path.exists(final_file):
            return False, "Failed to create final output file."

        # Subtitle selection & muxing
        write_subs = False
        sub_langs: List[str] = []
        if selected_subtitles:
            if "all" in [s.lower() for s in selected_subtitles]:
                write_subs = True
                sub_langs = ["all"]
            else:
                write_subs = True
                sub_langs = selected_subtitles

        if write_subs and check_ffmpeg():
            if progress_cb:
                progress_cb({
                    "status": "muxing",
                    "percent": 99.5,
                    "message": "Verifying and muxing subtitle tracks with FFmpeg...",
                })
            final_file = self._ensure_subtitles_muxed(
                media_filepath=final_file,
                candidate=candidate,
                selected_subtitles=sub_langs,
                clean_title=clean_title,
                out_dir=dest_dir,
            )

        # Record in database
        filesize = os.path.getsize(final_file) if os.path.exists(final_file) else 0
        try:
            self.db.add_download(
                url=candidate.page_url or candidate.url,
                platform="Universal",
                title=candidate.title,
                duration=candidate.duration_s,
                quality=f"{selected_resolution}p" if selected_resolution else ("audio_only" if audio_only else "best"),
                format=target_ext,
                filesize=filesize,
                filepath=final_file,
                status="complete",
                media_type="audio" if audio_only else "video",
            )
        except Exception as e:
            logger.warning(f"Could not record universal download to DB: {e}")

        if progress_cb:
            progress_cb({
                "status": "complete",
                "percent": 100.0,
                "filepath": final_file,
                "filesize": filesize,
                "filesize_str": format_size(filesize),
                "message": f"Saved successfully to {os.path.basename(final_file)} ({format_size(filesize)})",
            })

        return True, final_file

    def _fetch_subtitle_file(
        self,
        sub_track: SubtitleTrackOption,
        target_path: str,
        base_url: str = ""
    ) -> bool:
        """
        Fetches a subtitle track directly to disk (handling both direct VTT/SRT
        and HLS subtitle playlists containing WebVTT segments).
        """
        sub_url = sub_track.url.split("#")[0] if sub_track.url else ""
        if not sub_url:
            return False
        if not sub_url.startswith(("http://", "https://")) and base_url:
            sub_url = urljoin(base_url, sub_url)

        raw_headers = dict(sub_track.headers) if sub_track.headers else {"User-Agent": DEFAULT_USER_AGENT}
        clean_headers = {}
        for k, v in raw_headers.items():
            if not v or not str(v).strip() or k.startswith(":"):
                continue
            if k.lower() == "sec-ch-ua" and "HeadlessChrome" in v:
                v = v.replace("HeadlessChrome", "Google Chrome")
            clean_headers[k] = v

        if "Referer" in clean_headers:
            clean_headers["Referer"] = clean_headers["Referer"].split("#")[0]
        elif "referer" in clean_headers:
            clean_headers["Referer"] = clean_headers.pop("referer").split("#")[0]
        elif base_url:
            clean_headers["Referer"] = base_url.split("#")[0]

        if "Referer" in clean_headers and "Origin" not in clean_headers and "origin" not in clean_headers:
            try:
                p_ref = urlparse(clean_headers["Referer"])
                clean_headers["Origin"] = f"{p_ref.scheme}://{p_ref.netloc}"
            except Exception:
                pass

        def _do_get(u: str):
            try:
                from curl_cffi import requests as cffi_requests
                return cffi_requests.get(u, headers=clean_headers, impersonate="chrome", timeout=15)
            except Exception:
                return requests.get(u, headers=clean_headers, timeout=15)

        try:
            r = _do_get(sub_url)
            if not r or r.status_code != 200:
                logger.warning(f"Failed to fetch subtitle from {sub_url} (status: {getattr(r, 'status_code', 'none')})")
                return False

            text = r.text
            # If it's an HLS subtitle playlist
            if "#EXTM3U" in text:
                seg_lines = [
                    line.strip() for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                if not seg_lines:
                    return False
                vtt_contents = []
                for s_line in seg_lines:
                    s_url = s_line if s_line.startswith(("http://", "https://")) else urljoin(sub_url, s_line)
                    r_s = _do_get(s_url)
                    if r_s and r_s.status_code == 200:
                        vtt_contents.append(r_s.text)
                if not vtt_contents:
                    return False
                # Merge VTT contents
                merged = vtt_contents[0]
                for vc in vtt_contents[1:]:
                    lines_c = [l for l in vc.splitlines() if not l.startswith("WEBVTT") and not l.startswith("X-TIMESTAMP-MAP")]
                    merged += "\n" + "\n".join(lines_c)
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(merged)
                return True
            else:
                with open(target_path, "wb") as f:
                    f.write(r.content)
                return True
        except Exception as e:
            logger.warning(f"Error fetching subtitle {sub_url}: {e}")
            return False

    def _ensure_subtitles_muxed(
        self,
        media_filepath: str,
        candidate: StreamCandidate,
        selected_subtitles: List[str],
        clean_title: str,
        out_dir: str
    ) -> str:
        """
        Ensures all selected subtitles (embedded or external) are present inside
        the final media container. If yt-dlp did not embed them, retrieves them
        and muxes them via FFmpeg (-c copy).
        """
        if not check_ffmpeg() or not selected_subtitles or not os.path.exists(media_filepath):
            return media_filepath

        # Check existing subtitle streams in media_filepath using ffprobe
        existing_sub_langs = set()
        try:
            probe_cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", media_filepath
            ]
            p_res = subprocess.run(probe_cmd, capture_output=True, text=True, check=False)
            if p_res.returncode == 0:
                data = json.loads(p_res.stdout)
                for s in data.get("streams", []):
                    if s.get("codec_type") == "subtitle":
                        tags = s.get("tags") or {}
                        lang = tags.get("language") or "und"
                        existing_sub_langs.add(lang.lower())
        except Exception as e:
            logger.debug(f"ffprobe subtitle check error: {e}")

        # Determine which subtitle tracks need to be added
        sub_options_to_add: List[Tuple[str, str]] = []  # (file_path, lang)
        temp_dir = tempfile.mkdtemp(prefix="subs_to_mux_")

        try:
            # Check for any .vtt or .srt files yt-dlp already saved to disk
            downloaded_vtt_files = {}
            for fname in os.listdir(out_dir):
                if fname.startswith(clean_title[:25]) and (fname.endswith(".vtt") or fname.endswith(".srt")):
                    parts = fname.rsplit(".", 2)
                    if len(parts) >= 3:
                        lang_part = parts[-2].lower()
                        downloaded_vtt_files[lang_part] = os.path.join(out_dir, fname)

            for idx, st in enumerate(candidate.subtitles):
                is_selected = (
                    "all" in [s.lower() for s in selected_subtitles]
                    or (st.language and st.language.lower() in [s.lower() for s in selected_subtitles])
                    or str(idx + 1) in selected_subtitles
                    or str(idx) in selected_subtitles
                )
                if not is_selected:
                    continue

                lang = st.language.lower() if st.language else "und"
                # If already embedded by FFmpegEmbedSubtitle, do not duplicate
                if lang in existing_sub_langs:
                    continue

                # 1. Did yt-dlp already save this file to disk?
                existing_file = downloaded_vtt_files.get(lang)
                if existing_file and os.path.exists(existing_file) and os.path.getsize(existing_file) > 100:
                    sub_options_to_add.append((existing_file, lang))
                    continue

                # 2. Otherwise download it directly
                target_sub_path = os.path.join(temp_dir, f"track_{idx}_{lang}.{st.ext}")
                if not st.headers and candidate.headers:
                    st.headers = candidate.headers
                base_ref = candidate.url or candidate.page_url
                if self._fetch_subtitle_file(st, target_sub_path, base_url=base_ref):
                    sub_options_to_add.append((target_sub_path, lang))

            if not sub_options_to_add:
                return media_filepath

            # Mux with FFmpeg (-c copy)
            base, ext = os.path.splitext(media_filepath)
            out_muxed = f"{base}_muxed{ext}"
            cmd = ["ffmpeg", "-y", "-i", media_filepath]
            for sp, _ in sub_options_to_add:
                cmd.extend(["-i", sp])

            cmd.extend(["-map", "0"])
            for i in range(1, len(sub_options_to_add) + 1):
                cmd.extend(["-map", f"{i}:0"])

            if ext.lower() == ".mp4":
                cmd.extend(["-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text"])
            else:
                cmd.extend(["-c", "copy"])

            for i, (_, l) in enumerate(sub_options_to_add):
                cmd.extend([f"-metadata:s:s:{len(existing_sub_langs) + i}", f"language={l}"])

            cmd.append(out_muxed)
            logger.info(f"Running subtitle mux command: {' '.join(cmd)}")
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and os.path.exists(out_muxed) and os.path.getsize(out_muxed) > 1000:
                os.replace(out_muxed, media_filepath)
                # Clean up any leftover vtt files matching the base title
                for sp, _ in sub_options_to_add:
                    if sp.startswith(out_dir) and os.path.exists(sp):
                        try:
                            os.remove(sp)
                        except Exception:
                            pass
                return media_filepath
            else:
                logger.warning(f"FFmpeg subtitle muxing warning: {res.stderr}")
                if os.path.exists(out_muxed):
                    os.remove(out_muxed)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        return media_filepath

    def _mux_external_subtitles(
        self,
        media_filepath: str,
        external_subs: List[SubtitleTrackOption]
    ) -> str:
        """Legacy helper maintained for backward compatibility."""
        return self._ensure_subtitles_muxed(
            media_filepath=media_filepath,
            candidate=StreamCandidate(url="", type="direct", subtitles=external_subs),
            selected_subtitles=["all"],
            clean_title=os.path.splitext(os.path.basename(media_filepath))[0],
            out_dir=os.path.dirname(media_filepath),
        )

    # ==================== CLI INTERACTIVE WORKFLOW ====================

    def interactive_cli(self, target_url: Optional[str] = None):
        """Interactive terminal CLI for Paste URL & Download workflow."""
        print("\n" + "=" * 65)
        print("  UNKNOWN MEDIA (VIDEO+AUDIO+SUBTITLES) DOWNLOADER")
        print("  Extracts & downloads from any webpage, player, or iframe")
        print("=" * 65)

        url = target_url
        if not url:
            url = input("\n  Paste any webpage or media URL: ").strip()

        if not url:
            print("  [!] No URL entered. Aborting.")
            return

        print(f"\n[*] Scanning & sniffing media streams from: {url}")

        def print_sniff_progress(msg: str):
            print(f"  -> {msg}")

        candidates = self.analyze_url(url, progress_cb=print_sniff_progress)

        if not candidates:
            print("\n  [!] No playable media streams found on this webpage.")
            print("      The player might use unsupported proprietary DRM or canvas streaming.")
            input("\n  Press Enter to continue...")
            return

        # Pick candidate stream if multiple discovered
        candidate: StreamCandidate
        if len(candidates) == 1:
            candidate = candidates[0]
        else:
            print(f"\nDiscovered {len(candidates)} Media Stream(s):")
            for idx, c in enumerate(candidates, 1):
                drm_str = " [DRM PROTECTED]" if c.is_drm else ""
                res_str = f"({', '.join(str(h) + 'p' for h in c.unique_resolutions[:3])})" if c.unique_resolutions else ""
                print(f"  [{idx}] {c.title[:45]} [{c.type.upper()}]{res_str}{drm_str}")
            pick = input(f"\nSelect stream [1-{len(candidates)}] (Default 1): ").strip()
            sel_idx = int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(candidates) else 0
            candidate = candidates[sel_idx]

        if candidate.is_drm:
            print(f"\n  [!] {candidate.drm_notice}")
            input("\n  Press Enter to return...")
            return

        # 1. Quality Selection
        print(f"\n--- Stream: {candidate.title} ---")
        if candidate.duration_s:
            print(f"  Duration: {format_duration(candidate.duration_s)}")
        print(f"  Type:     {candidate.type.upper()}")

        heights = candidate.unique_resolutions
        sel_height: Optional[int] = None
        audio_only = False

        if heights:
            print("\nAvailable Video Qualities:")
            for idx, h in enumerate(heights, 1):
                # find first track with this height for details
                trk = next((t for t in candidate.video_tracks if t.height == h), None)
                label = trk.display_label if trk else f"{h}p"
                print(f"  [{idx}] {label}")
            print(f"  [{len(heights) + 1}] Audio Only (Lossless / Best Audio)")

            choice = input(f"\nSelect quality [1-{len(heights) + 1}] (Default highest 1): ").strip()
            if choice.isdigit() and int(choice) == len(heights) + 1:
                audio_only = True
            elif choice.isdigit() and 1 <= int(choice) <= len(heights):
                sel_height = heights[int(choice) - 1]
            else:
                sel_height = heights[0]
        else:
            print("\n  [i] Single combined video stream available.")

        # 2. Audio Track Selection (if multiple audio renditions present)
        sel_audio_id: Optional[str] = None
        if not audio_only and len(candidate.audio_tracks) > 1:
            print("\nAvailable Audio Renditions:")
            for idx, at in enumerate(candidate.audio_tracks, 1):
                print(f"  [{idx}] {at.display_label}")
            a_choice = input(f"Select audio track [1-{len(candidate.audio_tracks)}] (Default 1): ").strip()
            if a_choice.isdigit() and 1 <= int(a_choice) <= len(candidate.audio_tracks):
                sel_audio_id = candidate.audio_tracks[int(a_choice) - 1].format_id

        # 3. Subtitles Selection
        selected_subs: List[str] = []
        if candidate.subtitles:
            print(f"\nAvailable Subtitles ({len(candidate.subtitles)} track(s)):")
            print("  [0] None")
            print("  [all] Embed All Subtitles")
            for idx, st in enumerate(candidate.subtitles, 1):
                print(f"  [{idx}] {st.display_label}")
            sub_choice = input("Select subtitles (e.g. '1', '1,2', 'all', or 0 for none): ").strip()
            if sub_choice.lower() == "all":
                selected_subs = ["all"]
            elif sub_choice and sub_choice != "0":
                for part in sub_choice.split(","):
                    p = part.strip()
                    if p.isdigit() and 1 <= int(p) <= len(candidate.subtitles):
                        selected_subs.append(candidate.subtitles[int(p) - 1].language)

        # 4. Output Container
        out_fmt = "mkv"
        if not audio_only:
            fmt_in = input("\nContainer format (1 for MKV [recommended], 2 for MP4): ").strip()
            if fmt_in == "2":
                out_fmt = "mp4"

        print("\n[*] Starting download and muxing...")

        def print_dl_progress(p: Dict[str, Any]):
            status = p.get("status")
            if status == "downloading":
                pct = min(100.0, max(0.0, float(p.get("percent") or 0.0)))
                bar_len = 24
                filled = int(round(bar_len * pct / 100.0))
                filled = min(bar_len, max(0, filled))
                if filled == bar_len:
                    bar = "=" * bar_len
                elif filled > 0:
                    bar = "=" * (filled - 1) + ">" + " " * (bar_len - filled)
                else:
                    bar = " " * bar_len

                speed_val = p.get("speed_str") or "0 B/s"
                speed_str = f"{speed_val:>11}"
                eta_val = p.get("eta_str") or "--:--"
                eta_str = f"{eta_val:<6}"
                dl_str = (p.get("downloaded_str") or "0 B").rjust(10)
                tot_str = (p.get("total_str") or "0 B").ljust(10)
                dl_tot = f"{dl_str} / {tot_str}"
                line = f"  [{bar}] {pct:5.1f}% | {dl_tot} | {speed_str} | ETA: {eta_str}"
                sys.stdout.write(f"\r{line.ljust(95)}")
                sys.stdout.flush()
            elif status == "starting":
                print(f"  -> {p.get('message', 'Starting download...')}")
            elif status == "muxing":
                sys.stdout.write("\r" + " " * 95 + "\r")
                sys.stdout.flush()
                print(f"  [FFmpeg] {p.get('message', 'Muxing tracks...')}")

        success, res = self.download_candidate(
            candidate=candidate,
            selected_resolution=sel_height,
            selected_audio_id=sel_audio_id,
            audio_only=audio_only,
            selected_subtitles=selected_subs,
            output_format=out_fmt,
            progress_cb=print_dl_progress,
        )

        if success:
            file_size_str = format_size(os.path.getsize(res)) if os.path.exists(res) else "Unknown"
            print(f"\n\n  ================================================================")
            print(f"  [SUCCESS] DOWNLOAD COMPLETE!")
            print(f"  ================================================================")
            print(f"  File Name: {os.path.basename(res)}")
            print(f"  File Size: {file_size_str}")
            print(f"  Saved To:  {res}")
            print(f"  Directory: {os.path.dirname(res)}")
            print(f"  ================================================================")
            try:
                # Desktop notification
                from downloader import _send_notification
                _send_notification("Download Complete", os.path.basename(res), res)
            except Exception:
                pass
        else:
            print(f"\n\n  [ERROR] {res}")

        input("\n  Press Enter to return to menu...")


# ============================================================================
# STANDALONE CLI ENTRYPOINT
# ============================================================================

if __name__ == "__main__":
    cli_app = UniversalMediaDownloader()
    url_arg = sys.argv[1] if len(sys.argv) > 1 else None
    cli_app.interactive_cli(url_arg)
