r"""
Platform Detection and Handling for Personal Media Downloader
Location: C:\downloader\platforms.py
"""

import re
from enum import Enum
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs


# ============================================================================
# PLATFORM ENUM
# ============================================================================

class Platform(Enum):
    """Supported platforms"""
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
    BILIBILI = "Bilibili"
    PINTEREST = "Pinterest"
    LINKEDIN = "LinkedIn"
    TUMBLR = "Tumblr"
    GITHUB = "GitHub"
    SPOTIFY = "Spotify"
    APPLE_MUSIC = "Apple Music"
    DEEZER = "Deezer"
    TIDAL = "Tidal"
    CRUNCHYROLL = "Crunchyroll"
    UNKNOWN = "Unknown"
    
    @property
    def display_name(self) -> str:
        return self.value
    
    @property
    def color(self) -> str:
        """Get platform brand color (hex)"""
        colors = {
            Platform.YOUTUBE: "#FF0000",
            Platform.INSTAGRAM: "#E4405F",
            Platform.TWITTER: "#1DA1F2",
            Platform.FACEBOOK: "#1877F2",
            Platform.REDDIT: "#FF4500",
            Platform.TIKTOK: "#000000",
            Platform.VIMEO: "#1AB7EA",
            Platform.TWITCH: "#9146FF",
            Platform.SOUNDCLOUD: "#FF5500",
            Platform.DAILYMOTION: "#0066DC",
            Platform.GITHUB: "#181717",
            Platform.SPOTIFY: "#1DB954",
            Platform.APPLE_MUSIC: "#FA243C",
            Platform.DEEZER: "#A238FF",
            Platform.TIDAL: "#000000",
            Platform.CRUNCHYROLL: "#F47521",
        }
        return colors.get(self, "#808080")


# ============================================================================
# PLATFORM INFO
# ============================================================================

@dataclass
class PlatformInfo:
    """Platform information and capabilities"""
    platform: Platform
    name: str
    domains: List[str]
    supports_video: bool = True
    supports_audio: bool = True
    supports_playlist: bool = True
    supports_stories: bool = False
    supports_live: bool = False
    supports_subtitles: bool = True
    max_resolution: str = "4K"
    requires_auth: bool = False
    has_age_restriction: bool = True
    has_geo_restriction: bool = True
    
    @property
    def capabilities(self) -> Dict[str, bool]:
        return {
            "video": self.supports_video,
            "audio": self.supports_audio,
            "playlist": self.supports_playlist,
            "stories": self.supports_stories,
            "live": self.supports_live,
            "subtitles": self.supports_subtitles,
        }


# Platform registry
PLATFORM_INFO: Dict[Platform, PlatformInfo] = {
    Platform.YOUTUBE: PlatformInfo(
        platform=Platform.YOUTUBE,
        name="YouTube",
        domains=["youtube.com", "youtu.be", "youtube-nocookie.com", "music.youtube.com"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_live=True,
        supports_subtitles=True,
        max_resolution="8K",
        has_age_restriction=True,
    ),
    Platform.INSTAGRAM: PlatformInfo(
        platform=Platform.INSTAGRAM,
        name="Instagram",
        domains=["instagram.com", "instagr.am"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_stories=True,
        supports_live=True,
        supports_subtitles=False,
        max_resolution="1080p",
        requires_auth=False,  # For private content
    ),
    Platform.TWITTER: PlatformInfo(
        platform=Platform.TWITTER,
        name="Twitter/X",
        domains=["twitter.com", "x.com", "t.co"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=False,
        supports_subtitles=False,
        max_resolution="1080p",
    ),
    Platform.FACEBOOK: PlatformInfo(
        platform=Platform.FACEBOOK,
        name="Facebook",
        domains=["facebook.com", "fb.com", "fb.watch", "fbcdn.net"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_stories=True,
        supports_live=True,
        max_resolution="4K",
    ),
    Platform.REDDIT: PlatformInfo(
        platform=Platform.REDDIT,
        name="Reddit",
        domains=["reddit.com", "redd.it", "v.redd.it", "i.redd.it"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_subtitles=False,
        max_resolution="1080p",
    ),
    Platform.TIKTOK: PlatformInfo(
        platform=Platform.TIKTOK,
        name="TikTok",
        domains=["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_subtitles=False,
        max_resolution="1080p",
        has_geo_restriction=True,
    ),
    Platform.VIMEO: PlatformInfo(
        platform=Platform.VIMEO,
        name="Vimeo",
        domains=["vimeo.com", "player.vimeo.com"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_subtitles=True,
        max_resolution="8K",
    ),
    Platform.TWITCH: PlatformInfo(
        platform=Platform.TWITCH,
        name="Twitch",
        domains=["twitch.tv", "clips.twitch.tv"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_live=True,
        supports_subtitles=False,
        max_resolution="1080p60",
    ),
    Platform.SOUNDCLOUD: PlatformInfo(
        platform=Platform.SOUNDCLOUD,
        name="SoundCloud",
        domains=["soundcloud.com", "snd.sc"],
        supports_video=False,
        supports_audio=True,
        supports_playlist=True,
        supports_subtitles=False,
        max_resolution="N/A",
    ),
    Platform.DAILYMOTION: PlatformInfo(
        platform=Platform.DAILYMOTION,
        name="Dailymotion",
        domains=["dailymotion.com", "dai.ly"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_subtitles=True,
        max_resolution="4K",
    ),
    Platform.CRUNCHYROLL: PlatformInfo(
        platform=Platform.CRUNCHYROLL,
        name="Crunchyroll",
        domains=["crunchyroll.com"],
        supports_video=True,
        supports_audio=True,
        supports_playlist=True,
        supports_subtitles=True,
        max_resolution="1080p",
        requires_auth=True,
    ),
}


# ============================================================================
# URL PATTERNS
# ============================================================================

PLATFORM_PATTERNS: Dict[Platform, List[str]] = {
    Platform.YOUTUBE: [
        r"(?:https?://)?(?:www\.)?youtube\.com/watch\?v=[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/playlist\?list=[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/shorts/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/embed/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/v/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/live/[\w-]+",
        r"(?:https?://)?youtu\.be/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/@[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/channel/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/c/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/user/[\w-]+",
        r"(?:https?://)?music\.youtube\.com/watch\?v=[\w-]+",
    ],
    Platform.INSTAGRAM: [
        r"(?:https?://)?(?:www\.)?instagram\.com/p/[\w-]+",
        r"(?:https?://)?(?:www\.)?instagram\.com/reel/[\w-]+",
        r"(?:https?://)?(?:www\.)?instagram\.com/reels/[\w-]+",
        r"(?:https?://)?(?:www\.)?instagram\.com/tv/[\w-]+",
        r"(?:https?://)?(?:www\.)?instagram\.com/stories/[\w.]+/\d+",
        r"(?:https?://)?(?:www\.)?instagram\.com/[\w.]+/?$",
    ],
    Platform.TWITTER: [
        r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/\w+/status/\d+",
        r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/i/status/\d+",
        r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/\w+/video/\d+",
    ],
    Platform.FACEBOOK: [
        r"(?:https?://)?(?:www\.)?facebook\.com/.+/videos/\d+",
        r"(?:https?://)?(?:www\.)?facebook\.com/watch/?\?v=\d+",
        r"(?:https?://)?(?:www\.)?facebook\.com/reel/\d+",
        r"(?:https?://)?(?:www\.)?facebook\.com/.+/posts/.+",
        r"(?:https?://)?fb\.watch/[\w-]+",
    ],
    Platform.REDDIT: [
        r"(?:https?://)?(?:www\.)?reddit\.com/r/\w+/comments/[\w-]+",
        r"(?:https?://)?(?:www\.)?reddit\.com/r/\w+/?$",
        r"(?:https?://)?v\.redd\.it/[\w-]+",
        r"(?:https?://)?(?:www\.)?reddit\.com/user/[\w-]+",
    ],
    Platform.TIKTOK: [
        r"(?:https?://)?(?:www\.)?tiktok\.com/@[\w.]+/video/\d+",
        r"(?:https?://)?(?:www\.)?tiktok\.com/@[\w.]+/?$",
        r"(?:https?://)?(?:vm|vt)\.tiktok\.com/[\w-]+",
        r"(?:https?://)?(?:www\.)?tiktok\.com/t/[\w-]+",
    ],
    Platform.VIMEO: [
        r"(?:https?://)?(?:www\.)?vimeo\.com/\d+",
        r"(?:https?://)?(?:www\.)?vimeo\.com/channels/[\w-]+/\d+",
        r"(?:https?://)?(?:www\.)?vimeo\.com/groups/[\w-]+/videos/\d+",
        r"(?:https?://)?player\.vimeo\.com/video/\d+",
    ],
    Platform.TWITCH: [
        r"(?:https?://)?(?:www\.)?twitch\.tv/\w+/clip/[\w-]+",
        r"(?:https?://)?(?:www\.)?twitch\.tv/videos/\d+",
        r"(?:https?://)?clips\.twitch\.tv/[\w-]+",
        r"(?:https?://)?(?:www\.)?twitch\.tv/\w+/?$",
    ],
    Platform.SOUNDCLOUD: [
        r"(?:https?://)?(?:www\.)?soundcloud\.com/[\w-]+/[\w-]+",
        r"(?:https?://)?(?:www\.)?soundcloud\.com/[\w-]+/sets/[\w-]+",
        r"(?:https?://)?(?:www\.)?soundcloud\.com/[\w-]+/?$",
    ],
    Platform.DAILYMOTION: [
        r"(?:https?://)?(?:www\.)?dailymotion\.com/video/[\w-]+",
        r"(?:https?://)?dai\.ly/[\w-]+",
        r"(?:https?://)?(?:www\.)?dailymotion\.com/playlist/[\w-]+",
    ],
    Platform.GITHUB: [
        r"(?:https?://)?(?:www\.)?github\.com/[\w.-]+/[\w.-]+/releases",
        r"(?:https?://)?(?:www\.)?github\.com/[\w.-]+/[\w.-]+/releases/tag/[\w.-]+",
        r"(?:https?://)?(?:www\.)?github\.com/[\w.-]+/[\w.-]+/releases/download/.+",
        r"(?:https?://)?(?:www\.)?github\.com/[\w.-]+/[\w.-]+/archive/.+",
    ],
    Platform.SPOTIFY: [
        r"(?:https?://)?open\.spotify\.com/(track|album|playlist|artist)/[\w]+",
        r"(?:https?://)?spotify\.link/[\w]+",
        r"spotify:(track|album|playlist|artist):[\w]+",
    ],
    Platform.APPLE_MUSIC: [
        r"(?:https?://)?music\.apple\.com/[\w-]+/(album|playlist|song)/[\w.-]+(?:/\d+)?",
    ],
    Platform.DEEZER: [
        r"(?:https?://)?(?:www\.)?deezer\.com/(?:\w+/)?(track|album|playlist|artist)/[\d]+",
        r"(?:https?://)?deezer\.page\.link/[\w]+",
    ],
    Platform.TIDAL: [
        r"(?:https?://)?(?:listen\.)?tidal\.com/(browse/)?(track|album|playlist|artist)/[\w-]+",
    ],
    Platform.CRUNCHYROLL: [
        r"(?:https?://)?(?:www\.)?crunchyroll\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:watch|series)/[\w-]+",
        r"(?:https?://)?(?:www\.)?crunchyroll\.com/.*",
    ],
}


# ============================================================================
# CONTENT TYPE DETECTION
# ============================================================================

class ContentType(Enum):
    """Content type"""
    VIDEO = "video"
    AUDIO = "audio"
    PLAYLIST = "playlist"
    TRACK = "track"
    ALBUM = "album"
    ARTIST = "artist"
    CHANNEL = "channel"
    PROFILE = "profile"
    STORY = "story"
    SHORT = "short"
    LIVE = "live"
    M3U8 = "m3u8"
    DASH = "dash"
    DIRECT_FILE = "direct_file"
    GITHUB_RELEASE = "github_release"
    UNKNOWN = "unknown"


def detect_content_type(url: str, platform: Platform = None) -> ContentType:
    """Detect content type from URL"""
    url_lower = url.lower()
    
    # Spotify specific
    if platform == Platform.SPOTIFY or "spotify.com" in url_lower or "spotify:" in url_lower:
        if "/playlist/" in url_lower or ":playlist:" in url_lower:
            return ContentType.PLAYLIST
        if "/album/" in url_lower or ":album:" in url_lower:
            return ContentType.ALBUM
        if "/artist/" in url_lower or ":artist:" in url_lower:
            return ContentType.ARTIST
        return ContentType.TRACK
    
    # Apple Music specific
    if platform == Platform.APPLE_MUSIC or "music.apple.com" in url_lower:
        if "/playlist/" in url_lower:
            return ContentType.PLAYLIST
        if "/album/" in url_lower:
            return ContentType.ALBUM
        return ContentType.TRACK
    
    # Deezer specific
    if platform == Platform.DEEZER or "deezer.com" in url_lower:
        if "/playlist/" in url_lower:
            return ContentType.PLAYLIST
        if "/album/" in url_lower:
            return ContentType.ALBUM
        if "/artist/" in url_lower:
            return ContentType.ARTIST
        return ContentType.TRACK
    
    # YouTube specific
    if platform == Platform.YOUTUBE or "youtube" in url_lower or "youtu.be" in url_lower:
        if "/playlist" in url_lower or "list=" in url_lower:
            return ContentType.PLAYLIST
        if "/shorts/" in url_lower:
            return ContentType.SHORT
        if "/live/" in url_lower:
            return ContentType.LIVE
        if "/@" in url_lower or "/channel/" in url_lower or "/c/" in url_lower or "/user/" in url_lower:
            return ContentType.CHANNEL
        if "/watch" in url_lower or "youtu.be/" in url_lower:
            return ContentType.VIDEO
    
    # Instagram specific
    if platform == Platform.INSTAGRAM or "instagram" in url_lower:
        if "/stories/" in url_lower:
            return ContentType.STORY
        if "/reel" in url_lower:
            return ContentType.SHORT
        if "/p/" in url_lower or "/tv/" in url_lower:
            return ContentType.VIDEO
        # Profile URL
        if re.search(r'instagram\.com/[\w.]+/?$', url_lower):
            return ContentType.PROFILE
    
    # TikTok specific
    if platform == Platform.TIKTOK or "tiktok" in url_lower:
        if "/video/" in url_lower:
            return ContentType.VIDEO
        if re.search(r'tiktok\.com/@[\w.]+/?$', url_lower):
            return ContentType.PROFILE
    
    # Twitch specific
    if platform == Platform.TWITCH or "twitch" in url_lower:
        if "/videos/" in url_lower:
            return ContentType.VIDEO
        if "/clip/" in url_lower or "clips.twitch" in url_lower:
            return ContentType.SHORT
        if re.search(r'twitch\.tv/\w+/?$', url_lower):
            return ContentType.LIVE  # Could be live or profile
    
    # SoundCloud (audio platform)
    if platform == Platform.SOUNDCLOUD or "soundcloud" in url_lower:
        if "/sets/" in url_lower:
            return ContentType.PLAYLIST
        return ContentType.AUDIO
    
    # Crunchyroll specific
    if platform == Platform.CRUNCHYROLL or "crunchyroll.com" in url_lower:
        if "/series/" in url_lower:
            return ContentType.PLAYLIST
        return ContentType.VIDEO
    
    # General playlist detection
    if "playlist" in url_lower or "/sets/" in url_lower:
        return ContentType.PLAYLIST
    
    # M3U8 / HLS streams
    if url_lower.endswith('.m3u8') or url_lower.endswith('.m3u') or '/hls/' in url_lower:
        return ContentType.M3U8
    
    # MPEG-DASH
    if url_lower.endswith('.mpd'):
        return ContentType.DASH
    
    # GitHub releases
    if 'github.com' in url_lower and '/releases' in url_lower:
        return ContentType.GITHUB_RELEASE
    
    return ContentType.VIDEO


# ============================================================================
# PLATFORM DETECTION
# ============================================================================

def detect_platform(url: str) -> Platform:
    """
    Detect platform from URL.
    
    Args:
        url: The URL to analyze
    
    Returns:
        Platform enum value
    """
    if not url:
        return Platform.UNKNOWN
    
    url = url.strip().lower()
    
    # Check against patterns
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, url, re.IGNORECASE):
                return platform
    
    # Fallback: check domain
    try:
        parsed = urlparse(url if url.startswith('http') else f'https://{url}')
        domain = parsed.netloc.lower().replace('www.', '')
        
        for platform, info in PLATFORM_INFO.items():
            for d in info.domains:
                if d in domain:
                    return platform
    except:
        pass
    
    return Platform.UNKNOWN


def get_platform_info(platform: Platform) -> Optional[PlatformInfo]:
    """Get platform information"""
    return PLATFORM_INFO.get(platform)


def get_platform_from_domain(domain: str) -> Platform:
    """Get platform from domain string"""
    domain = domain.lower().replace('www.', '')
    
    for platform, info in PLATFORM_INFO.items():
        for d in info.domains:
            if d in domain or domain in d:
                return platform
    
    return Platform.UNKNOWN


# ============================================================================
# VIDEO ID EXTRACTION
# ============================================================================

def extract_video_id(url: str, platform: Platform = None) -> Optional[str]:
    """
    Extract video ID from URL.
    
    Args:
        url: The URL
        platform: Optional platform hint
    
    Returns:
        Video ID string or None
    """
    if not url:
        return None
    
    platform = platform or detect_platform(url)
    
    try:
        parsed = urlparse(url)
        
        if platform == Platform.YOUTUBE:
            # Standard watch URL
            if 'v' in parse_qs(parsed.query):
                return parse_qs(parsed.query)['v'][0]
            # Shortened URL (youtu.be)
            if 'youtu.be' in parsed.netloc:
                return parsed.path.strip('/').split('/')[0]
            # Shorts
            if '/shorts/' in parsed.path:
                return parsed.path.split('/shorts/')[-1].split('/')[0].split('?')[0]
            # Embed
            if '/embed/' in parsed.path:
                return parsed.path.split('/embed/')[-1].split('/')[0].split('?')[0]
            # Live
            if '/live/' in parsed.path:
                return parsed.path.split('/live/')[-1].split('/')[0].split('?')[0]
        
        elif platform == Platform.INSTAGRAM:
            # Posts, reels, TV
            match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', parsed.path)
            if match:
                return match.group(1)
        
        elif platform == Platform.TWITTER:
            match = re.search(r'/status/(\d+)', parsed.path)
            if match:
                return match.group(1)
        
        elif platform == Platform.TIKTOK:
            match = re.search(r'/video/(\d+)', parsed.path)
            if match:
                return match.group(1)
            # Short URLs might need resolution
            if 'vm.tiktok' in url or 'vt.tiktok' in url:
                # Return the short code
                return parsed.path.strip('/')
        
        elif platform == Platform.VIMEO:
            match = re.search(r'/(\d+)', parsed.path)
            if match:
                return match.group(1)
        
        elif platform == Platform.TWITCH:
            if '/videos/' in parsed.path:
                return parsed.path.split('/videos/')[-1].split('/')[0].split('?')[0]
            if '/clip/' in parsed.path:
                return parsed.path.split('/clip/')[-1].split('/')[0].split('?')[0]
            if 'clips.twitch.tv' in parsed.netloc:
                return parsed.path.strip('/').split('/')[0]
        
        elif platform == Platform.DAILYMOTION:
            match = re.search(r'/video/([a-z0-9]+)', parsed.path)
            if match:
                return match.group(1)
            if 'dai.ly' in parsed.netloc:
                return parsed.path.strip('/')
        
        elif platform == Platform.REDDIT:
            match = re.search(r'/comments/([a-z0-9]+)', parsed.path)
            if match:
                return match.group(1)
            if 'v.redd.it' in parsed.netloc:
                return parsed.path.strip('/')
        
        elif platform == Platform.FACEBOOK:
            # Video ID in path
            match = re.search(r'/videos/(\d+)', parsed.path)
            if match:
                return match.group(1)
            # Video ID in query
            if 'v' in parse_qs(parsed.query):
                return parse_qs(parsed.query)['v'][0]
            # Reel
            match = re.search(r'/reel/(\d+)', parsed.path)
            if match:
                return match.group(1)
        
    except Exception:
        pass
    
    return None


# ============================================================================
# PLAYLIST DETECTION
# ============================================================================

def is_playlist_url(url: str) -> bool:
    """Check if URL is a playlist"""
    if not url:
        return False
    
    url_lower = url.lower()
    
    # Direct indicators
    if "playlist" in url_lower:
        return True
    if "/sets/" in url_lower:  # SoundCloud
        return True
    
    # YouTube playlist parameter
    if "list=" in url_lower and "youtube" in url_lower:
        return True
    
    # Channel/profile URLs (can be treated as playlists)
    content_type = detect_content_type(url)
    return content_type in [ContentType.PLAYLIST, ContentType.CHANNEL, ContentType.PROFILE]


def extract_playlist_id(url: str) -> Optional[str]:
    """Extract playlist ID from URL"""
    if not url:
        return None
    
    platform = detect_platform(url)
    
    try:
        parsed = urlparse(url)
        
        if platform == Platform.YOUTUBE:
            if 'list' in parse_qs(parsed.query):
                return parse_qs(parsed.query)['list'][0]
        
        elif platform == Platform.SOUNDCLOUD:
            if '/sets/' in parsed.path:
                parts = parsed.path.split('/sets/')
                if len(parts) > 1:
                    return parts[1].strip('/').split('/')[0]
        
        elif platform == Platform.VIMEO:
            if '/album/' in parsed.path:
                match = re.search(r'/album/(\d+)', parsed.path)
                if match:
                    return match.group(1)
        
    except Exception:
        pass
    
    return None


# ============================================================================
# URL VALIDATION & NORMALIZATION
# ============================================================================

def is_supported_url(url: str) -> bool:
    """Check if URL is from a supported platform"""
    return detect_platform(url) != Platform.UNKNOWN


def normalize_url(url: str) -> str:
    """Normalize URL format"""
    url = url.strip()
    
    # Add https if missing
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    # Remove tracking parameters
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        
        # Parameters to keep (platform-specific essential params)
        keep_params = {'v', 'list', 'index', 't', 'start', 'end'}
        
        # Filter query parameters
        filtered = {k: v for k, v in query.items() if k in keep_params}
        
        # Rebuild URL
        if filtered:
            from urllib.parse import urlencode
            query_str = urlencode(filtered, doseq=True)
            url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{query_str}"
        else:
            url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except:
        pass
    
    return url.rstrip('/')


def is_crunchyroll_url(url: str) -> bool:
    """Check if URL is a Crunchyroll anime URL"""
    if not url:
        return False
    return "crunchyroll.com" in url.lower()


def get_all_platforms() -> List[Platform]:
    """Get list of all supported platforms"""
    return [p for p in Platform if p != Platform.UNKNOWN]


# ============================================================================
# MAIN (Testing)
# ============================================================================

if __name__ == "__main__":
    print("Platforms Module - Testing")
    print("=" * 50)
    
    test_urls = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/playlist?list=PLtest",
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/reel/XYZ789/",
        "https://twitter.com/user/status/123456789",
        "https://x.com/user/status/123456789",
        "https://www.tiktok.com/@user/video/123456789",
        "https://vm.tiktok.com/ZM123/",
        "https://www.reddit.com/r/test/comments/abc123/title",
        "https://vimeo.com/123456789",
        "https://www.twitch.tv/videos/123456789",
        "https://soundcloud.com/artist/track",
        "https://www.example.com/unknown",
    ]
    
    print("\nPlatform Detection:")
    print("-" * 50)
    for url in test_urls:
        platform = detect_platform(url)
        content_type = detect_content_type(url, platform)
        video_id = extract_video_id(url, platform)
        is_playlist = is_playlist_url(url)
        
        print(f"\n  URL: {url[:50]}...")
        print(f"    Platform: {platform.display_name}")
        print(f"    Content Type: {content_type.value}")
        print(f"    Video ID: {video_id}")
        print(f"    Is Playlist: {is_playlist}")
    
    print("\n" + "=" * 50)
    print("Tests complete!")