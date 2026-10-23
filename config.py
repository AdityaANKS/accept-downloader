import os
import sys
import platform
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from enum import Enum
import json


def _get_downloads_dir() -> str:
    """Get the default OS Downloads directory."""
    home = os.path.expanduser("~")
    
    if sys.platform == "win32":
        # Windows: try the known folder API, fall back to ~\Downloads
        try:
            import ctypes
            from ctypes import wintypes
            CSIDL_PROFILE = 0x0028
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PROFILE, None, 0, buf)
            profile = buf.value or home
            downloads = os.path.join(profile, "Downloads")
        except Exception:
            downloads = os.path.join(home, "Downloads")
    elif sys.platform == "darwin":
        downloads = os.path.join(home, "Downloads")
    else:
        # Linux / other: respect XDG if set, otherwise ~/Downloads
        xdg = os.environ.get("XDG_DOWNLOAD_DIR")
        downloads = xdg if xdg else os.path.join(home, "Downloads")
    
    return downloads


def _get_base_storage_dir() -> str:
    r"""
    Get the default storage directory for downloads.
    Prioritizes the dedicated ~/downloader folder (e.g. C:\Users\<user>\downloader),
    falling back to <Downloads>/downloader if needed.
    """
    home = os.path.expanduser("~")
    user_downloader = os.path.join(home, "downloader")
    if os.path.exists(user_downloader):
        return user_downloader
    return os.path.join(_get_downloads_dir(), "downloader")


# Default base directory: ~/downloader (fallback to <Downloads>/downloader)
_DEFAULT_BASE = _get_base_storage_dir()



class Quality(Enum):
    Q_8K_60 = "4320p60"
    Q_8K_30 = "4320p"
    Q_4K_60 = "2160p60"
    Q_4K_30 = "2160p"
    Q_1440_60 = "1440p60"
    Q_1440_30 = "1440p"
    Q_1080_60 = "1080p60"
    Q_1080_30 = "1080p"
    Q_720_60 = "720p60"
    Q_720_30 = "720p"
    Q_480 = "480p"
    Q_360 = "360p"
    Q_240 = "240p"
    BEST = "best"
    WORST = "worst"


class AudioQuality(Enum):
    Q_320 = "320"
    Q_256 = "256"
    Q_192 = "192"
    Q_128 = "128"
    FLAC = "flac"
    WAV = "wav"
    BEST = "best"


class VideoFormat(Enum):
    MP4_H264 = "mp4"
    MP4_H265 = "mp4_hevc"
    MKV = "mkv"
    WEBM = "webm"
    MOV = "mov"
    AVI = "avi"


class AudioFormat(Enum):
    MP3 = "mp3"
    M4A = "m4a"
    AAC = "aac"
    FLAC = "flac"
    WAV = "wav"
    OPUS = "opus"
    OGG = "ogg"


class Platform(Enum):
    YOUTUBE = "YouTube"
    INSTAGRAM = "Instagram"
    TWITTER = "Twitter"
    FACEBOOK = "Facebook"
    REDDIT = "Reddit"
    TIKTOK = "TikTok"
    VIMEO = "Vimeo"
    TWITCH = "Twitch"
    SOUNDCLOUD = "SoundCloud"
    DAILYMOTION = "Dailymotion"
    CRUNCHYROLL = "Crunchyroll"
    UNKNOWN = "Unknown"


@dataclass
class Config:
    # Download save location: dynamically resolved to <Downloads>/downloader
    BASE_DIR: str = _DEFAULT_BASE
    VIDEO_DIR: str = os.path.join(_DEFAULT_BASE, "videos")
    AUDIO_DIR: str = os.path.join(_DEFAULT_BASE, "audios")
    IMAGE_DIR: str = os.path.join(_DEFAULT_BASE, "images")
    TEXT_DIR: str = os.path.join(_DEFAULT_BASE, "text")
    SUBTITLE_DIR: str = os.path.join(_DEFAULT_BASE, "subtitles")
    THUMBNAIL_DIR: str = os.path.join(_DEFAULT_BASE, "thumbnails")
    TEMP_DIR: str = os.path.join(_DEFAULT_BASE, "temp")
    COOKIE_DIR: str = os.path.join(_DEFAULT_BASE, "cookies")
    DATABASE_PATH: str = os.path.join(_DEFAULT_BASE, "downloader.db")
    LOG_PATH: str = os.path.join(_DEFAULT_BASE, "downloader.log")
    CONFIG_FILE: str = os.path.join(os.path.expanduser("~"), ".downloader_settings.json")
    
    # Default quality settings
    DEFAULT_VIDEO_QUALITY: Quality = Quality.Q_1080_30
    DEFAULT_AUDIO_QUALITY: AudioQuality = AudioQuality.Q_192
    DEFAULT_VIDEO_FORMAT: VideoFormat = VideoFormat.MP4_H264
    DEFAULT_AUDIO_FORMAT: AudioFormat = AudioFormat.MP3
    
    # Download settings
    MAX_CONCURRENT_DOWNLOADS: int = 3
    CHUNK_SIZE: int = 1024 * 1024
    RETRY_ATTEMPTS: int = 3
    RETRY_DELAY: float = 2.0
    REQUEST_TIMEOUT: int = 30
    DOWNLOAD_TIMEOUT: int = 7200
    MIN_FREE_SPACE_MB: int = 500
    
    # File settings
    MAX_FILENAME_LENGTH: int = 200
    EMBED_THUMBNAIL: bool = True
    EMBED_METADATA: bool = True
    SAVE_THUMBNAIL_SEPARATELY: bool = False
    ORGANIZE_BY_PLATFORM: bool = True
    ORGANIZE_BY_PLAYLIST: bool = True
    SANITIZE_FILENAMES: bool = True
    
    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = True
    LOG_TO_CONSOLE: bool = True
    LOG_MAX_SIZE_MB: int = 10
    LOG_BACKUP_COUNT: int = 5
    
    # Subtitles
    SUBTITLE_LANGUAGES = ["en", "es", "fr", "de", "it", "ja", "ko", "ru", "pt", "hi", "zh"]
    AUTO_GENERATED_SUBS: bool = True
    EMBED_SUBTITLES: bool = False
    SUBTITLE_FORMAT: str = "srt"
    
    # FFmpeg
    FFMPEG_PATH: str = "ffmpeg"
    FFPROBE_PATH: str = "ffprobe"
    
    # Download archive (prevents re-downloading)
    DOWNLOAD_ARCHIVE: str = os.path.join(_DEFAULT_BASE, "download_archive.txt")
    
    # Output template (customizable filename pattern)
    # Supports yt-dlp fields: %(title)s, %(uploader)s, %(upload_date)s, %(id)s, etc.
    OUTPUT_TEMPLATE: str = "%(title)s.%(ext)s"
    
    # Preview before download
    PREVIEW_BEFORE_DOWNLOAD: bool = True
    
    # Chunked downloader settings
    FILES_DIR: str = os.path.join(_DEFAULT_BASE, "files")
    STREAM_DIR: str = os.path.join(_DEFAULT_BASE, "streams")
    DOWNLOAD_THREADS: int = 8              # Parallel chunks per file
    CHUNK_SIZE_MB: int = 10                # Minimum chunk size in MB
    MAX_DOWNLOAD_THREADS: int = 32         # Max threads for AI acceleration
    SPEED_LIMIT_KBPS: int = 0              # 0 = unlimited, else KB/s
    AI_ACCELERATION: bool = True           # Dynamic thread tuning + work stealing
    
    # SpotiFLAC / Lossless Music settings
    MUSIC_DIR: str = os.path.join(_DEFAULT_BASE, "music")
    MUSIC_QUALITY: str = "flac"            # "flac" (lossless) or "320k" (high bitrate MP3)
    EMBED_LYRICS: bool = True              # Embed synchronized / plain lyrics in audio metadata
    SAVE_LRC_FILE: bool = True             # Save .lrc synchronized lyrics file alongside audio
    SAVE_COVER_ART: bool = True            # Save high-res cover art alongside audio
    
    # ML training data logger
    ML_LOG_PATH: str = os.path.join(_DEFAULT_BASE, "ml_training.jsonl")
    ML_LOG_ENABLED: bool = True             # Collect structured telemetry for ML

    # CrossPlatformDownloadManager (CDM) Settings
    CDM_ENABLED: bool = True
    CDM_DIR: str = os.path.join(_DEFAULT_BASE, "cdm")
    CDM_AUTO_START: bool = True
    CDM_DEFAULT_QUEUE: str = "Main Queue"
    CDM_DUPLICATE_ACTION: str = "duplicate_number"  # "duplicate_number", "overwrite", "resume", "let_user_choose"
    CDM_PORT: int = 5000

    # Universal Media / Webpage Sniffer Settings
    UNKNOWN_DIR: str = os.path.join(_DEFAULT_BASE, "unknown")
    
    @classmethod
    def init_directories(cls) -> bool:
        dirs = [
            cls.BASE_DIR, cls.VIDEO_DIR, cls.AUDIO_DIR,
            cls.IMAGE_DIR, cls.TEXT_DIR, cls.SUBTITLE_DIR,
            cls.THUMBNAIL_DIR, cls.TEMP_DIR, cls.COOKIE_DIR,
            cls.FILES_DIR, cls.STREAM_DIR, cls.MUSIC_DIR,
            cls.CDM_DIR, cls.UNKNOWN_DIR,
        ]
        try:
            for d in dirs:
                os.makedirs(d, exist_ok=True)
            return True
        except Exception as e:
            print(f"Error creating directories: {e}")
            return False
    
    @classmethod
    def verify_directories(cls) -> Dict[str, bool]:
        dirs = {
            "Base": cls.BASE_DIR,
            "Videos": cls.VIDEO_DIR,
            "Audios": cls.AUDIO_DIR,
            "Images": cls.IMAGE_DIR,
            "Text": cls.TEXT_DIR,
            "Subtitles": cls.SUBTITLE_DIR,
            "Thumbnails": cls.THUMBNAIL_DIR,
            "Temp": cls.TEMP_DIR,
            "Cookies": cls.COOKIE_DIR,
            "Unknown": cls.UNKNOWN_DIR
        }
        results = {}
        for name, path in dirs.items():
            exists = os.path.exists(path)
            writable = os.access(path, os.W_OK) if exists else False
            results[name] = exists and writable
        return results
    
    @classmethod
    def get_platform_dir(cls, platform: Platform, media_type: str = "video") -> str:
        if media_type == "video":
            base = cls.VIDEO_DIR
        elif media_type == "audio":
            base = cls.AUDIO_DIR
        elif media_type == "thumbnail":
            base = cls.THUMBNAIL_DIR
        elif media_type == "subtitle":
            base = cls.SUBTITLE_DIR
        else:
            base = cls.BASE_DIR
        
        if cls.ORGANIZE_BY_PLATFORM:
            platform_dir = os.path.join(base, platform.value)
            os.makedirs(platform_dir, exist_ok=True)
            return platform_dir
        return base
    
    @classmethod
    def save_settings(cls) -> bool:
        settings = {
            "base_dir": cls.BASE_DIR,
            "video_dir": cls.VIDEO_DIR,
            "audio_dir": cls.AUDIO_DIR,
            "thumbnail_dir": cls.THUMBNAIL_DIR,
            "subtitle_dir": cls.SUBTITLE_DIR,
            "text_dir": cls.TEXT_DIR,
            "unknown_dir": cls.UNKNOWN_DIR,
            "cdm_dir": cls.CDM_DIR,
            "music_dir": cls.MUSIC_DIR,
            "files_dir": cls.FILES_DIR,
            "stream_dir": cls.STREAM_DIR,
            "default_video_quality": cls.DEFAULT_VIDEO_QUALITY.value,
            "default_audio_quality": cls.DEFAULT_AUDIO_QUALITY.value,
            "default_video_format": cls.DEFAULT_VIDEO_FORMAT.value,
            "default_audio_format": cls.DEFAULT_AUDIO_FORMAT.value,
            "max_concurrent_downloads": cls.MAX_CONCURRENT_DOWNLOADS,
            "embed_thumbnail": cls.EMBED_THUMBNAIL,
            "embed_metadata": cls.EMBED_METADATA,
            "organize_by_platform": cls.ORGANIZE_BY_PLATFORM,
            "subtitle_languages": cls.SUBTITLE_LANGUAGES,
        }
        try:
            with open(cls.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
            return True
        except Exception as e:
            print(f"Error saving settings: {e}")
            return False
    
    @classmethod
    def load_settings(cls) -> bool:
        if not os.path.exists(cls.CONFIG_FILE):
            return False
        try:
            with open(cls.CONFIG_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
            # If a custom base_dir is saved, update it and rebuild subdirectory paths
            if "base_dir" in settings:
                base = settings["base_dir"]
                cls.BASE_DIR = base
                # Rebuild subdirectory paths relative to the saved base_dir
                cls.VIDEO_DIR = settings.get("video_dir", os.path.join(base, "videos"))
                cls.AUDIO_DIR = settings.get("audio_dir", os.path.join(base, "audios"))
                cls.IMAGE_DIR = settings.get("image_dir", os.path.join(base, "images"))
                cls.TEXT_DIR = settings.get("text_dir", os.path.join(base, "text"))
                cls.SUBTITLE_DIR = settings.get("subtitle_dir", os.path.join(base, "subtitles"))
                cls.THUMBNAIL_DIR = settings.get("thumbnail_dir", os.path.join(base, "thumbnails"))
                cls.UNKNOWN_DIR = settings.get("unknown_dir", os.path.join(base, "unknown"))
                cls.FILES_DIR = settings.get("files_dir", os.path.join(base, "files"))
                cls.STREAM_DIR = settings.get("stream_dir", os.path.join(base, "streams"))
                cls.MUSIC_DIR = settings.get("music_dir", os.path.join(base, "music"))
                cls.CDM_DIR = settings.get("cdm_dir", os.path.join(base, "cdm"))
                cls.TEMP_DIR = os.path.join(base, "temp")
                cls.COOKIE_DIR = os.path.join(base, "cookies")
                cls.DATABASE_PATH = os.path.join(base, "downloader.db")
                cls.LOG_PATH = os.path.join(base, "downloader.log")
                cls.DOWNLOAD_ARCHIVE = os.path.join(base, "download_archive.txt")
                cls.ML_LOG_PATH = os.path.join(base, "ml_training.jsonl")
            else:
                # Load individual dirs if present (without base_dir override)
                if "unknown_dir" in settings:
                    cls.UNKNOWN_DIR = settings["unknown_dir"]
                if "video_dir" in settings:
                    cls.VIDEO_DIR = settings["video_dir"]
                if "audio_dir" in settings:
                    cls.AUDIO_DIR = settings["audio_dir"]
                if "thumbnail_dir" in settings:
                    cls.THUMBNAIL_DIR = settings["thumbnail_dir"]
                if "subtitle_dir" in settings:
                    cls.SUBTITLE_DIR = settings["subtitle_dir"]
                if "text_dir" in settings:
                    cls.TEXT_DIR = settings["text_dir"]
                if "cdm_dir" in settings:
                    cls.CDM_DIR = settings["cdm_dir"]
                if "music_dir" in settings:
                    cls.MUSIC_DIR = settings["music_dir"]
            if "max_concurrent_downloads" in settings:
                cls.MAX_CONCURRENT_DOWNLOADS = settings["max_concurrent_downloads"]
            if "embed_thumbnail" in settings:
                cls.EMBED_THUMBNAIL = settings["embed_thumbnail"]
            if "embed_metadata" in settings:
                cls.EMBED_METADATA = settings["embed_metadata"]
            if "organize_by_platform" in settings:
                cls.ORGANIZE_BY_PLATFORM = settings["organize_by_platform"]
            if "subtitle_languages" in settings:
                cls.SUBTITLE_LANGUAGES = settings["subtitle_languages"]
            return True
        except Exception as e:
            print(f"Error loading settings: {e}")
            return False

    
    @classmethod
    def get_free_space_mb(cls) -> int:
        try:
            import shutil
            total, used, free = shutil.disk_usage(cls.BASE_DIR)
            return free // (1024 * 1024)
        except:
            return -1
    
    @classmethod
    def print_config(cls):
        print("\n" + "=" * 60)
        print("  CURRENT CONFIGURATION")
        print("=" * 60)
        print(f"  Base Directory:     {cls.BASE_DIR}")
        print(f"  Video Directory:    {cls.VIDEO_DIR}")
        print(f"  Audio Directory:    {cls.AUDIO_DIR}")
        print(f"  Thumbnail Dir:      {cls.THUMBNAIL_DIR}")
        print(f"  Subtitle Dir:       {cls.SUBTITLE_DIR}")
        print(f"  Text Directory:     {cls.TEXT_DIR}")
        print(f"  Temp Directory:     {cls.TEMP_DIR}")
        print("-" * 60)
        print(f"  Default Video:      {cls.DEFAULT_VIDEO_QUALITY.value} / {cls.DEFAULT_VIDEO_FORMAT.value}")
        print(f"  Default Audio:      {cls.DEFAULT_AUDIO_QUALITY.value} / {cls.DEFAULT_AUDIO_FORMAT.value}")
        print(f"  Embed Thumbnail:    {cls.EMBED_THUMBNAIL}")
        print(f"  Embed Metadata:     {cls.EMBED_METADATA}")
        print(f"  Organize by Platform: {cls.ORGANIZE_BY_PLATFORM}")
        print("-" * 60)
        print(f"  Bot Module:         {'Enabled' if cls.USE_BOT_MODULE else 'Disabled'}")
        print(f"  Free Disk Space:    {cls.get_free_space_mb()} MB")
        print("=" * 60 + "\n")


# Quality mappings for yt-dlp
QUALITY_FORMAT_MAP = {
    Quality.Q_8K_60: "bestvideo[height<=4320][fps>=50]+bestaudio/best[height<=4320]",
    Quality.Q_8K_30: "bestvideo[height<=4320][fps<50]+bestaudio/best[height<=4320]",
    Quality.Q_4K_60: "bestvideo[height<=2160][fps>=50]+bestaudio/best[height<=2160]",
    Quality.Q_4K_30: "bestvideo[height<=2160][fps<50]+bestaudio/best[height<=2160]",
    Quality.Q_1440_60: "bestvideo[height<=1440][fps>=50]+bestaudio/best[height<=1440]",
    Quality.Q_1440_30: "bestvideo[height<=1440][fps<50]+bestaudio/best[height<=1440]",
    Quality.Q_1080_60: "bestvideo[height<=1080][fps>=50]+bestaudio/best[height<=1080]",
    Quality.Q_1080_30: "bestvideo[height<=1080][fps<50]+bestaudio/best[height<=1080]",
    Quality.Q_720_60: "bestvideo[height<=720][fps>=50]+bestaudio/best[height<=720]",
    Quality.Q_720_30: "bestvideo[height<=720][fps<50]+bestaudio/best[height<=720]",
    Quality.Q_480: "bestvideo[height<=480]+bestaudio/best[height<=480]",
    Quality.Q_360: "bestvideo[height<=360]+bestaudio/best[height<=360]",
    Quality.Q_240: "bestvideo[height<=240]+bestaudio/best[height<=240]",
    Quality.BEST: "bestvideo+bestaudio/best",
    Quality.WORST: "worstvideo+worstaudio/worst",
}

AUDIO_FORMAT_MAP = {
    AudioFormat.MP3: "mp3",
    AudioFormat.M4A: "m4a",
    AudioFormat.AAC: "aac",
    AudioFormat.FLAC: "flac",
    AudioFormat.WAV: "wav",
    AudioFormat.OPUS: "opus",
    AudioFormat.OGG: "vorbis",
}

# Platform URL patterns (centralized in platforms.py)
try:
    from platforms import PLATFORM_PATTERNS
except ImportError:
    PLATFORM_PATTERNS = {}


# Auto-load saved settings on import so user preferences are always applied
Config.load_settings()


if __name__ == "__main__":
    print("Initializing directories...")
    Config.init_directories()
    Config.print_config()
    print("\nVerifying directories:")
    results = Config.verify_directories()
    for name, status in results.items():
        icon = "Y" if status else "X"
        print(f"  [{icon}] {name}")