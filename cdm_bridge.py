r"""
CrossPlatformDownloadManager (CDM) Bridge & Queue Engine
Location: C:\downloader\cdm_bridge.py

Inspired by adel-bakhshi/CrossPlatformDownloadManager.
Features:
- Browser Extension API Listener (GET /cdm/download/filetypes/, POST /cdm/download/add/)
- Automatic categorization by file extension (Videos, Musics, Documents, Compressed, Programs, Images, General)
- Multi-queue download scheduler with priority sorting, pause/resume, and concurrency control
- Duplicate file collision resolver (Number suffix, Overwrite, Resume)
- Engine Router: automatically selects ChunkedDownloader (direct files),
  SpotiFLAC (Spotify), StreamRecorder (M3U8), or yt-dlp (YouTube, etc.)
"""

import os
import sys
import re
import json
import time
import uuid
import threading
from typing import Optional, Dict, List, Tuple, Any, Callable
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse, unquote

from config import Config
from utils import get_logger, sanitize_filename, format_size, format_duration
from database import DatabaseManager

logger = get_logger()

# ============================================================================
# CDM CATEGORIES & EXTENSIONS (Exact match from CDM categories.json)
# ============================================================================

CDM_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "Videos": {
        "dir_name": "Videos",
        "extensions": [
            ".avi", ".mpg", ".mpe", ".mpeg", ".asf", ".wmv", ".mov", ".qt",
            ".rm", ".mp4", ".flv", ".m4v", ".webm", ".ogv", ".ogg", ".mkv",
            ".ts", ".tsv", ".m3u8",
        ],
    },
    "Musics": {
        "dir_name": "Musics",
        "extensions": [
            ".mp3", ".wav", ".wma", ".mpa", ".ram", ".ra", ".aac", ".aif",
            ".m4a", ".tsa", ".flac", ".opus", ".ogg",
        ],
    },
    "Compressed": {
        "dir_name": "Compressed",
        "extensions": [
            ".zip", ".rar", ".gz", ".7z", ".s7z", ".arj", ".bz2", ".iso",
            ".tar", ".tgz", ".xz",
        ],
    },
    "Documents": {
        "dir_name": "Documents",
        "extensions": [
            ".doc", ".docx", ".pdf", ".ppt", ".pps", ".pptx", ".xls", ".xlsx",
            ".txt", ".rtf", ".odt", ".csv", ".epub",
        ],
    },
    "Programs": {
        "dir_name": "Programs",
        "extensions": [
            ".exe", ".msi", ".msix", ".apk", ".app", ".dmg", ".pkg", ".deb",
            ".rpm", ".AppImage", ".flatpak",
        ],
    },
    "Images": {
        "dir_name": "Images",
        "extensions": [
            ".png", ".jpeg", ".jpg", ".gif", ".tiff", ".psd", ".eps", ".ai",
            ".indd", ".svg", ".webp", ".bmp", ".ico",
        ],
    },
    "General": {
        "dir_name": "General",
        "extensions": [],
    },
}

ALL_CDM_FILE_EXTENSIONS: List[str] = []
for cat_data in CDM_CATEGORIES.values():
    ALL_CDM_FILE_EXTENSIONS.extend(cat_data["extensions"])
ALL_CDM_FILE_EXTENSIONS = sorted(list(set(ALL_CDM_FILE_EXTENSIONS)))


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class ExtensionRequest:
    """Request payload sent from CDM browser extension"""
    url: str
    referer: Optional[str] = None
    pageAddress: Optional[str] = None
    description: Optional[str] = None

    def clean(self):
        if self.url:
            self.url = self.url.replace('\\', '/').strip()
        if self.referer:
            self.referer = self.referer.replace('\\', '/').strip()
        if self.pageAddress:
            self.pageAddress = self.pageAddress.replace('\\', '/').strip()
        if self.description:
            self.description = self.description.replace('\n', ' ').strip()


@dataclass
class ExtensionResponse:
    """Response payload returned to CDM browser extension"""
    isSuccessful: bool
    message: str = ""
    data: Any = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"isSuccessful": self.isSuccessful, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


@dataclass
class CDMQueueTask:
    id: str
    url: str
    referer: Optional[str] = None
    page_address: Optional[str] = None
    description: Optional[str] = None
    category: str = "General"
    target_dir: str = ""
    target_filename: str = ""
    priority: int = 1
    status: str = "pending"  # pending, downloading, paused, completed, failed
    progress_pct: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_str: str = ""
    added_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "referer": self.referer,
            "page_address": self.page_address,
            "description": self.description,
            "category": self.category,
            "target_dir": self.target_dir,
            "target_filename": self.target_filename,
            "priority": self.priority,
            "status": self.status,
            "progress_pct": round(self.progress_pct, 1),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "filesize_str": format_size(self.total_bytes) if self.total_bytes else "Unknown",
            "speed_str": self.speed_str,
            "added_at": datetime.fromtimestamp(self.added_at).strftime("%Y-%m-%d %H:%M:%S") if self.added_at else "",
            "error": self.error,
        }


from datetime import datetime


# ============================================================================
# CATEGORY MANAGER
# ============================================================================

class CDMCategoryManager:
    """Classifies files into categories and resolves destination folders."""

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or getattr(Config, "CDM_DIR", os.path.join(Config.BASE_DIR, "cdm"))
        self._ensure_dirs()

    def _ensure_dirs(self):
        for cat_info in CDM_CATEGORIES.values():
            cat_dir = os.path.join(self.base_dir, cat_info["dir_name"])
            os.makedirs(cat_dir, exist_ok=True)

    def get_category_for_filename(self, filename_or_url: str) -> str:
        """Determine category from filename or url extension."""
        return CDMCategoryManager.categorize(filename_or_url)

    @staticmethod
    def categorize(filename_or_url: str) -> str:
        """Classify URL or filename into one of the CDM categories."""
        clean = unquote(filename_or_url.split("?")[0].split("#")[0]).lower()
        ext = os.path.splitext(clean)[1]
        if not ext and "." in clean:
            ext = "." + clean.rsplit(".", 1)[-1]

        for cat_name, cat_info in CDM_CATEGORIES.items():
            if ext in cat_info["extensions"]:
                return cat_name
        return "General"

    def get_destination_dir(self, category: str) -> str:
        cat_info = CDM_CATEGORIES.get(category, CDM_CATEGORIES["General"])
        cat_dir = os.path.join(self.base_dir, cat_info["dir_name"])
        os.makedirs(cat_dir, exist_ok=True)
        return cat_dir


# ============================================================================
# QUEUE MANAGER & SCHEDULER
# ============================================================================

class CDMQueueManager:
    """Manages active downloads, concurrency, duplicate handling and engine dispatch."""
    _instance: Optional["CDMQueueManager"] = None
    _lock = threading.Lock()

    def __init__(self):
        self.category_mgr = CDMCategoryManager()
        self.tasks: Dict[str, CDMQueueTask] = {}
        self.max_concurrent: int = 3
        self.is_paused: bool = False
        self._worker_thread: Optional[threading.Thread] = None
        self._running: bool = True
        self.db = DatabaseManager()
        self._start_worker()

    @classmethod
    def get_instance(cls) -> "CDMQueueManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _start_worker(self):
        self._worker_thread = threading.Thread(target=self._process_queue_loop, daemon=True)
        self._worker_thread.start()

    def add_url(
        self,
        url: str,
        referer: Optional[str] = None,
        page_address: Optional[str] = None,
        description: Optional[str] = None,
        category: Optional[str] = None,
        auto_start: bool = True,
        priority: int = 1,
    ) -> CDMQueueTask:
        """Add a download URL to the CDM queue."""
        with self._lock:
            task_id = str(uuid.uuid4())[:8]
            cat = category or self.category_mgr.get_category_for_filename(description or url)
            target_dir = self.category_mgr.get_destination_dir(cat)

            # Suggest filename
            clean_url = unquote(url.split("?")[0].split("#")[0])
            suggested_name = os.path.basename(clean_url) or f"download_{task_id}"
            if description and "." in description:
                suggested_name = description
            suggested_name = sanitize_filename(suggested_name)

            task = CDMQueueTask(
                id=task_id,
                url=url.strip(),
                referer=referer,
                page_address=page_address,
                description=description or suggested_name,
                category=cat,
                target_dir=target_dir,
                target_filename=suggested_name,
                priority=priority,
                status="pending" if auto_start else "paused",
            )
            self.tasks[task_id] = task
            logger.info(f"Added task {task_id} ({task.description}) to CDM queue in category '{cat}'")
            return task

    def list_tasks(self) -> List[CDMQueueTask]:
        with self._lock:
            return sorted(list(self.tasks.values()), key=lambda x: (-x.priority, x.added_at))

    def pause_task(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if task and task.status in ("pending", "downloading"):
                task.status = "paused"
                logger.info(f"Paused CDM task {task_id}")
                return True
        return False

    def resume_task(self, task_id: str) -> bool:
        with self._lock:
            task = self.tasks.get(task_id)
            if task and task.status in ("paused", "failed"):
                task.status = "pending"
                logger.info(f"Resumed CDM task {task_id}")
                return True
        return False

    def remove_task(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self.tasks:
                del self.tasks[task_id]
                logger.info(f"Removed CDM task {task_id}")
                return True
        return False

    def pause_all(self):
        with self._lock:
            self.is_paused = True
            for t in self.tasks.values():
                if t.status == "pending":
                    t.status = "paused"

    def resume_all(self):
        with self._lock:
            self.is_paused = False
            for t in self.tasks.values():
                if t.status == "paused":
                    t.status = "pending"

    # ==================== ENGINE ROUTER ====================

    def _process_queue_loop(self):
        """Continuously schedules and executes pending tasks."""
        while self._running:
            try:
                time.sleep(1.0)
                if self.is_paused:
                    continue

                active_count = sum(1 for t in self.tasks.values() if t.status == "downloading")
                if active_count >= self.max_concurrent:
                    continue

                # Find next top priority pending task
                next_task = None
                with self._lock:
                    pending = [t for t in self.tasks.values() if t.status == "pending"]
                    if pending:
                        pending.sort(key=lambda x: (-x.priority, x.added_at))
                        next_task = pending[0]
                        next_task.status = "downloading"

                if next_task:
                    threading.Thread(target=self._execute_task, args=(next_task,), daemon=True).start()

            except Exception as e:
                logger.error(f"Error in CDM queue scheduler: {e}")

    def _execute_task(self, task: CDMQueueTask):
        """Route task to the appropriate download engine."""
        url = task.url
        logger.info(f"Starting CDM download {task.id}: {url}")

        try:
            # 1. Spotify check
            if "spotify.com" in url.lower():
                self._download_spotify(task)
            # 3. M3U8 stream check
            elif ".m3u8" in url.lower() or "m3u8" in url.lower():
                self._download_stream(task)
            # 4. Standard Direct File URL or general site
            else:
                self._download_direct_or_media(task)

        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            logger.error(f"Task {task.id} failed: {e}")

    def _download_direct_or_media(self, task: CDMQueueTask):
        """Download using ChunkedDownloader if direct file, else yt-dlp."""
        # Try ChunkedDownloader first
        from chunked_downloader import ChunkedDownloader

        def progress_cb(p):
            task.progress_pct = getattr(p, "percent", 0.0)
            task.downloaded_bytes = getattr(p, "downloaded_bytes", 0)
            task.total_bytes = getattr(p, "total_bytes", 0)
            speed = getattr(p, "speed_bps", 0)
            task.speed_str = f"{format_size(speed)}/s" if speed else ""

        # Check duplicate handling
        dest_path = os.path.join(task.target_dir, task.target_filename)
        action = getattr(Config, "CDM_DUPLICATE_ACTION", "duplicate_number")
        if os.path.exists(dest_path):
            if action == "duplicate_number":
                base, ext = os.path.splitext(task.target_filename)
                idx = 1
                while os.path.exists(os.path.join(task.target_dir, f"{base} ({idx}){ext}")):
                    idx += 1
                task.target_filename = f"{base} ({idx}){ext}"
                dest_path = os.path.join(task.target_dir, task.target_filename)
            elif action == "resume":
                pass  # ChunkedDownloader will attempt resume

        downloader = ChunkedDownloader(threads=getattr(Config, "DOWNLOAD_THREADS", 8))
        res = downloader.download(
            url=task.url,
            output_dir=task.target_dir,
            filename=task.target_filename,
            progress_callback=progress_cb,
        )

        if res and res.get("status") == "complete":
            task.status = "completed"
            task.progress_pct = 100.0
            task.completed_at = time.time()
            final_file = res.get("filepath") or dest_path
            # Log in database
            filesize = res.get("filesize", 0) or (os.path.getsize(final_file) if os.path.exists(final_file) else 0)
            self.db.add_download(
                url=task.url,
                platform="CDM",
                title=task.description or task.target_filename,
                filesize=filesize,
                filepath=final_file,
                status="complete",
            )
            logger.info(f"Task {task.id} completed via ChunkedDownloader: {final_file}")
            return

        # Fallback to yt-dlp if direct chunked download failed or not a direct link
        logger.info(f"Direct chunked download for {task.id} falling back to yt-dlp...")
        self._download_via_ytdlp(task)

    def _download_via_ytdlp(self, task: CDMQueueTask):
        """Fallback to yt-dlp for media pages."""
        import yt_dlp

        def ytdlp_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                speed = d.get("speed", 0)
                if total > 0:
                    task.progress_pct = (downloaded / total) * 100.0
                task.downloaded_bytes = downloaded
                task.total_bytes = total
                task.speed_str = f"{format_size(speed)}/s" if speed else ""
            elif d.get("status") == "finished":
                task.progress_pct = 100.0

        class _SilentLogger:
            def debug(self, msg): pass
            def info(self, msg): pass
            def warning(self, msg): pass
            def error(self, msg): pass

        ydl_opts = {
            "outtmpl": os.path.join(task.target_dir, "%(title)s.%(ext)s"),
            "progress_hooks": [ytdlp_hook],
            "quiet": True,
            "no_warnings": True,
            "logger": _SilentLogger(),
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(task.url, download=True)
            filename = ydl.prepare_filename(info)
            task.status = "completed"
            task.completed_at = time.time()
            filesize = os.path.getsize(filename) if os.path.exists(filename) else 0
            self.db.add_download(
                url=task.url,
                platform="CDM-Media",
                title=info.get("title", task.description),
                filesize=filesize,
                filepath=filename,
                status="complete",
            )
            logger.info(f"Task {task.id} completed via yt-dlp: {filename}")

    def _download_spotify(self, task: CDMQueueTask):
        from spotiflac import SpotiFLACDownloader
        spot = SpotiFLACDownloader()
        res = spot.download(task.url, output_dir=task.target_dir, quality="flac")
        if res:
            task.status = "completed"
            task.progress_pct = 100.0
            task.completed_at = time.time()
        else:
            task.status = "failed"
            task.error = "Spotify extraction failed"

    def _download_stream(self, task: CDMQueueTask):
        from stream_recorder import StreamRecorder
        rec = StreamRecorder(output_dir=task.target_dir)
        def stream_progress(pct, speed, size):
            task.progress_pct = pct
            task.downloaded_bytes = size
            task.speed_str = f"{format_size(speed)}/s" if speed else ""

        filepath = rec.record_vod(task.url, title=task.description or "stream", progress_callback=stream_progress)
        if filepath and os.path.exists(filepath):
            task.status = "completed"
            task.progress_pct = 100.0
            task.completed_at = time.time()
        else:
            task.status = "failed"
            task.error = "Stream recording failed"


# ============================================================================
# CDM BRIDGE (HANDLERS FOR BROWSER EXTENSION)
# ============================================================================

class CDMBridge:
    """Provides exact endpoint handlers conforming to CDM browser extension API."""

    @staticmethod
    def handle_get_filetypes() -> Dict[str, Any]:
        """
        Handler for GET /cdm/download/filetypes/
        Returns list of supported file extensions to intercept.
        """
        return ExtensionResponse(
            isSuccessful=True,
            message="",
            data=ALL_CDM_FILE_EXTENSIONS,
        ).to_dict()

    @staticmethod
    def handle_add_download(data: Any) -> Dict[str, Any]:
        """
        Handler for POST /cdm/download/add/
        Accepts single ExtensionRequest dict or list of ExtensionRequest dicts.
        """
        if not data:
            return ExtensionResponse(
                isSuccessful=False,
                message="Invalid data. Please retry.",
            ).to_dict()

        requests_list: List[ExtensionRequest] = []
        if isinstance(data, list):
            for item in data:
                req = ExtensionRequest(
                    url=item.get("url", ""),
                    referer=item.get("referer"),
                    pageAddress=item.get("pageAddress"),
                    description=item.get("description"),
                )
                req.clean()
                if req.url:
                    requests_list.append(req)
        elif isinstance(data, dict):
            req = ExtensionRequest(
                url=data.get("url", ""),
                referer=data.get("referer"),
                pageAddress=data.get("pageAddress"),
                description=data.get("description"),
            )
            req.clean()
            if req.url:
                requests_list.append(req)

        if not requests_list:
            return ExtensionResponse(
                isSuccessful=False,
                message="CDM can't accept this URL or empty URL list.",
            ).to_dict()

        queue_mgr = CDMQueueManager.get_instance()
        for r in requests_list:
            queue_mgr.add_url(
                url=r.url,
                referer=r.referer,
                page_address=r.pageAddress,
                description=r.description,
                auto_start=getattr(Config, "CDM_AUTO_START", True),
            )

        count = len(requests_list)
        msg = f"{count} Links added to CDM." if count > 1 else "Link added to CDM."
        return ExtensionResponse(
            isSuccessful=True,
            message=msg,
        ).to_dict()
