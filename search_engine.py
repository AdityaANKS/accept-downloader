"""
search_engine.py — Interactive Media Search Engine & Playlist Explorer
========================================================================
Provides unified searching, deep metadata inspection, subtitle/lyrics
availability verification, and playlist/album track extraction across
Spotify, YouTube, YouTube Music, and LRCLIB.
"""

import os
import sys
import time
import json
import requests
import shutil
import urllib.parse
from typing import List, Dict, Optional, Tuple, Any, Union
from dataclasses import dataclass, field, asdict

import yt_dlp
from config import Config
from platforms import Platform, detect_platform
from spotiflac import SpotifyMetadataResolver, LRCLIBLyricsFetcher, SpotiFLACDownloader, SpotifyTrack, SpotifyCollection


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class SearchResultItem:
    id: str
    title: str
    artist: str
    album: str = ""
    duration_s: int = 0
    duration_str: str = "0:00"
    thumbnail: str = ""
    url: str = ""
    media_type: str = "track"       # "track", "album", "playlist", "video", "artist"
    platform: str = "Spotify"       # "Spotify", "YouTube", "YouTube Music", "Direct"
    has_synced_lyrics: bool = False
    has_plain_lyrics: bool = False
    subtitles: List[str] = field(default_factory=list)
    track_count: int = 1
    release_year: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# SEARCH ENGINE CORE
# ============================================================================

class MediaSearchEngine:
    """
    Unified search engine supporting music (Spotify / LRCLIB / YT Music)
    and general video/media exploration with subtitle & lyrics checking.
    """

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if not seconds or seconds <= 0:
            return "0:00"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @classmethod
    def check_lyrics_fast(cls, artist: str, title: str) -> Tuple[bool, bool]:
        """
        Fast lookup to check if LRCLIB has synced or plain lyrics available.
        Returns (has_synced, has_plain).
        """
        if not artist or not title:
            return False, False
        try:
            r = requests.get(
                "https://lrclib.net/api/search",
                params={"q": f"{artist} {title}"},
                timeout=4
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    first = data[0]
                    return bool(first.get("syncedLyrics")), bool(first.get("plainLyrics"))
        except Exception:
            pass
        return False, False

    @classmethod
    def search_music(cls, query: str, limit: int = 15) -> List[SearchResultItem]:
        """
        Search for music tracks with rich metadata, HD album art, and synced lyrics.
        Combines iTunes official metadata index, LRCLIB synced lyrics, and YouTube Music.
        """
        items: List[SearchResultItem] = []
        seen_keys = set()

        # 1. Search iTunes Store API (Ultra fast, HD 1000x1000 cover art, clean metadata)
        try:
            r = requests.get(
                "https://itunes.apple.com/search",
                params={"term": query, "entity": "song", "limit": limit},
                timeout=5
            )
            if r.status_code == 200:
                for entry in r.json().get("results", []):
                    title = entry.get("trackName") or ""
                    artist = entry.get("artistName") or ""
                    album = entry.get("collectionName") or ""
                    dur_s = int(entry.get("trackTimeMillis", 0) / 1000)
                    hd_cover = (entry.get("artworkUrl100") or "").replace("100x100bb.jpg", "1000x1000bb.jpg")
                    year = (entry.get("releaseDate") or "")[:4]

                    if not title:
                        continue

                    key = f"{artist.lower()}:{title.lower()}"
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    has_synced, has_plain = cls.check_lyrics_fast(artist, title)
                    track_url = f"track:{artist}:::{title}:::{album}:::{hd_cover}:::{dur_s}:::{year}"

                    items.append(SearchResultItem(
                        id=str(entry.get("trackId", len(items) + 1)),
                        title=title,
                        artist=artist,
                        album=album,
                        duration_s=dur_s,
                        duration_str=cls._format_duration(dur_s),
                        thumbnail=hd_cover,
                        url=track_url,
                        media_type="track",
                        platform="Music/FLAC",
                        has_synced_lyrics=has_synced,
                        has_plain_lyrics=has_plain,
                        release_year=year,
                    ))
        except Exception:
            pass

        # 2. Search LRCLIB for verified synced lyrics tracks
        if len(items) < limit:
            try:
                r = requests.get(
                    "https://lrclib.net/api/search",
                    params={"q": query},
                    timeout=5
                )
                if r.status_code == 200:
                    lrclib_data = r.json()
                    if isinstance(lrclib_data, list):
                        for entry in lrclib_data:
                            title = entry.get("trackName") or entry.get("name") or ""
                            artist = entry.get("artistName") or ""
                            album = entry.get("albumName") or ""
                            dur_s = int(entry.get("duration") or 0)
                            has_synced = bool(entry.get("syncedLyrics"))
                            has_plain = bool(entry.get("plainLyrics"))

                            if not title:
                                continue

                            key = f"{artist.lower()}:{title.lower()}"
                            if key in seen_keys:
                                continue
                            seen_keys.add(key)

                            track_url = f"track:{artist}:::{title}:::{album}:::::::{dur_s}:::"

                            items.append(SearchResultItem(
                                id=str(entry.get("id", len(items) + 1)),
                                title=title,
                                artist=artist,
                                album=album,
                                duration_s=dur_s,
                                duration_str=cls._format_duration(dur_s),
                                thumbnail="",
                                url=track_url,
                                media_type="track",
                                platform="Music/FLAC",
                                has_synced_lyrics=has_synced,
                                has_plain_lyrics=has_plain,
                                release_year="",
                            ))
                            if len(items) >= limit:
                                break
            except Exception:
                pass

        # 3. Search YouTube Music with flat extraction for any remaining slots
        if len(items) < limit:
            opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'noplaylist': True,
            }
            try:
                search_query = f"ytsearch{limit}:{query} audio"
                with yt_dlp.YoutubeDL(opts) as ydl:
                    yt_res = ydl.extract_info(search_query, download=False)
                    entries = (yt_res.get('entries') or []) if yt_res else []
                    for e in entries:
                        if not e:
                            continue
                        vid_id = e.get('id') or ''
                        raw_title = e.get('title') or ''
                        uploader = e.get('uploader') or e.get('channel') or 'Artist'
                        dur_s = int(e.get('duration') or 0)
                        thumb = e.get('thumbnail') or f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else ""
                        url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else e.get('url', '')

                        artist = uploader
                        title = raw_title
                        if " - " in raw_title:
                            parts = raw_title.split(" - ", 1)
                            artist = parts[0].strip()
                            title = parts[1].strip()

                        for clean_suffix in [" (Official Audio)", " (Official Video)", " [Official Audio]", " (Audio)", " (Lyrics)"]:
                            title = title.replace(clean_suffix, "")

                        key = f"{artist.lower()}:{title.lower()}"
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)

                        has_synced, has_plain = cls.check_lyrics_fast(artist, title)

                        items.append(SearchResultItem(
                            id=vid_id or str(len(items) + 1),
                            title=title,
                            artist=artist,
                            album="",
                            duration_s=dur_s,
                            duration_str=cls._format_duration(dur_s),
                            thumbnail=thumb,
                            url=url,
                            media_type="track",
                            platform="YouTube Music",
                            has_synced_lyrics=has_synced,
                            has_plain_lyrics=has_plain,
                        ))
                        if len(items) >= limit:
                            break
            except Exception:
                pass

        return items[:limit]

    @classmethod
    def search_video(cls, query: str, limit: int = 15) -> List[SearchResultItem]:
        """
        Search for video media via yt-dlp.
        """
        items: List[SearchResultItem] = []
        opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
        }
        try:
            search_query = f"ytsearch{limit}:{query}"
            with yt_dlp.YoutubeDL(opts) as ydl:
                yt_res = ydl.extract_info(search_query, download=False)
                entries = (yt_res.get('entries') or []) if yt_res else []
                for e in entries:
                    if not e:
                        continue
                    vid_id = e.get('id') or ''
                    title = e.get('title') or 'Unknown Title'
                    uploader = e.get('uploader') or e.get('channel') or 'Uploader'
                    dur_s = int(e.get('duration') or 0)
                    thumb = e.get('thumbnail') or f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg" if vid_id else ""
                    url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else e.get('url', '')

                    items.append(SearchResultItem(
                        id=vid_id or str(len(items) + 1),
                        title=title,
                        artist=uploader,
                        duration_s=dur_s,
                        duration_str=cls._format_duration(dur_s),
                        thumbnail=thumb,
                        url=url,
                        media_type="video",
                        platform="YouTube",
                    ))
        except Exception:
            pass
        return items[:limit]

    @classmethod
    def search(cls, query: str, search_type: str = "spotify", limit: int = 20) -> List[SearchResultItem]:
        """
        Spotify-dedicated search endpoint returning Spotify music tracks, albums, and artists.
        """
        return cls.search_music(query, limit=limit)

    # ========================================================================
    # DEEP METADATA INSPECTION
    # ========================================================================

    @classmethod
    def inspect_metadata(cls, url: str) -> Dict[str, Any]:
        """
        Deeply inspect any URL or track to return comprehensive details:
        - Artist, Title, Album, Release Year, Duration
        - LRCLIB Synced Lyrics status & live preview snippet
        - Subtitle languages list
        - Video heights and audio formats available
        """
        info: Dict[str, Any] = {
            "url": url,
            "title": "Unknown",
            "artist": "Unknown",
            "album": "",
            "duration_s": 0,
            "duration_str": "0:00",
            "thumbnail": "",
            "platform": "Unknown",
            "media_type": "track",
            "is_playlist": False,
            "track_count": 1,
            "has_synced_lyrics": False,
            "has_plain_lyrics": False,
            "lyrics_preview": "",
            "subtitles": [],
            "video_formats": [],
            "audio_formats": [],
        }

        # 1. Check if track descriptor URL (from iTunes / LRCLIB search)
        if url.startswith("track:"):
            parts = url[6:].split(":::")
            artist = parts[0] if len(parts) > 0 else "Artist"
            title = parts[1] if len(parts) > 1 else "Title"
            album = parts[2] if len(parts) > 2 else ""
            cover_url = parts[3] if len(parts) > 3 else ""
            dur_s = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            year = parts[5] if len(parts) > 5 else ""

            synced, plain = LRCLIBLyricsFetcher.fetch_lyrics(
                track_name=title,
                artist_name=artist,
                album_name=album,
                duration_s=dur_s
            )

            lyrics_preview = ""
            if synced:
                lyrics_preview = "\n".join([l for l in synced.splitlines() if l.strip()][:6])
            elif plain:
                lyrics_preview = "\n".join([l for l in plain.splitlines() if l.strip()][:6])

            info.update({
                "title": title,
                "artist": artist,
                "album": album,
                "duration_s": dur_s,
                "duration_str": cls._format_duration(dur_s),
                "thumbnail": cover_url,
                "platform": "Music/FLAC",
                "media_type": "track",
                "release_year": year,
                "has_synced_lyrics": bool(synced),
                "has_plain_lyrics": bool(plain),
                "lyrics_preview": lyrics_preview,
                "audio_formats": ["FLAC Lossless (Studio Master)", "MP3 320 kbps (Extreme)", "AAC 256 kbps"],
            })
            return info

        # 2. Check if Spotify URL
        if SpotiFLACDownloader.is_spotify_url(url):
            resolved = SpotifyMetadataResolver.resolve(url)
            if resolved:
                if isinstance(resolved, SpotifyTrack):
                    info.update({
                        "title": resolved.title,
                        "artist": resolved.artist_str,
                        "album": resolved.album,
                        "duration_s": resolved.duration_s,
                        "duration_str": resolved.duration_str,
                        "thumbnail": resolved.cover_url,
                        "platform": "Spotify",
                        "media_type": "track",
                        "release_year": resolved.release_year,
                    })
                    # Fetch LRCLIB lyrics preview
                    synced, plain = LRCLIBLyricsFetcher.fetch_lyrics(
                        track_name=resolved.title,
                        artist_name=resolved.artists[0] if resolved.artists else "",
                        album_name=resolved.album,
                        duration_s=resolved.duration_s
                    )
                    info["has_synced_lyrics"] = bool(synced)
                    info["has_plain_lyrics"] = bool(plain)
                    if synced:
                        lines = [l for l in synced.splitlines() if l.strip()]
                        info["lyrics_preview"] = "\n".join(lines[:6])
                    elif plain:
                        lines = [l for l in plain.splitlines() if l.strip()]
                        info["lyrics_preview"] = "\n".join(lines[:6])

                elif isinstance(resolved, SpotifyCollection):
                    info.update({
                        "title": resolved.name,
                        "artist": resolved.owner or "Spotify",
                        "duration_str": f"{resolved.total_tracks} tracks",
                        "thumbnail": resolved.cover_url,
                        "platform": "Spotify",
                        "media_type": resolved.collection_type,
                        "is_playlist": True,
                        "track_count": resolved.total_tracks,
                        "tracks": [t.to_dict() for t in resolved.tracks],
                    })
                return info

        # 2. General URL inspection via yt-dlp
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                yt_info = ydl.extract_info(url, download=False)
                if yt_info:
                    raw_title = yt_info.get('title', 'Unknown')
                    uploader = yt_info.get('uploader') or yt_info.get('channel') or 'Artist'
                    dur_s = int(yt_info.get('duration') or 0)
                    thumb = yt_info.get('thumbnail') or ''

                    # Extract subtitles list
                    subs_dict = yt_info.get('subtitles') or {}
                    auto_subs_dict = yt_info.get('automatic_captions') or {}
                    all_sub_codes = list(subs_dict.keys()) + [f"{c} (auto)" for c in auto_subs_dict.keys()]

                    # Extract video formats
                    formats = yt_info.get('formats') or []
                    heights = sorted(list(set(f.get('height') for f in formats if f.get('height'))), reverse=True)
                    abrs = sorted(list(set(int(f.get('abr') or 0) for f in formats if f.get('abr'))), reverse=True)

                    info.update({
                        "title": raw_title,
                        "artist": uploader,
                        "duration_s": dur_s,
                        "duration_str": cls._format_duration(dur_s),
                        "thumbnail": thumb,
                        "platform": detect_platform(url).value.capitalize(),
                        "media_type": "playlist" if yt_info.get('_type') == 'playlist' else "video",
                        "is_playlist": yt_info.get('_type') == 'playlist',
                        "track_count": len(yt_info.get('entries', [])) if yt_info.get('_type') == 'playlist' else 1,
                        "subtitles": all_sub_codes[:10],
                        "video_formats": [f"{h}p" for h in heights if h][:6],
                        "audio_formats": [f"{a} kbps" for a in abrs if a > 0][:4],
                    })

                    # If music-like, also probe lyrics from LRCLIB
                    artist_clean = uploader
                    title_clean = raw_title
                    if " - " in raw_title:
                        p = raw_title.split(" - ", 1)
                        artist_clean = p[0].strip()
                        title_clean = p[1].strip()
                    for clean_suffix in [" (Official Audio)", " (Official Video)", " [Official Audio]", " (Audio)", " (Lyrics)"]:
                        title_clean = title_clean.replace(clean_suffix, "")

                    synced, plain = cls.check_lyrics_fast(artist_clean, title_clean)
                    info["has_synced_lyrics"] = synced
                    info["has_plain_lyrics"] = plain

        except Exception as e:
            info["error"] = str(e)

        return info

    # ========================================================================
    # PLAYLIST / ALBUM SONG EXPLORER
    # ========================================================================

    @classmethod
    def fetch_playlist_tracks(cls, url: str) -> List[Dict[str, Any]]:
        """
        Extracts a clean, structured list of all songs in a Spotify album/playlist
        or YouTube playlist for individual track inspection and selection.
        """
        tracks: List[Dict[str, Any]] = []

        # 1. Spotify Album / Playlist
        if SpotiFLACDownloader.is_spotify_url(url):
            resolved = SpotifyMetadataResolver.resolve(url)
            if isinstance(resolved, SpotifyCollection):
                for idx, t in enumerate(resolved.tracks, 1):
                    tracks.append({
                        "index": idx,
                        "track_number": t.track_number,
                        "title": t.title,
                        "artist": t.artist_str,
                        "album": t.album or resolved.name,
                        "duration_s": t.duration_s,
                        "duration_str": t.duration_str,
                        "cover_url": t.cover_url or resolved.cover_url,
                        "url": t.spotify_url,
                        "isrc": t.isrc,
                        "has_synced_lyrics": None,  # Can be checked on-demand
                    })
                return tracks
            elif isinstance(resolved, SpotifyTrack):
                return [{
                    "index": 1,
                    "track_number": resolved.track_number,
                    "title": resolved.title,
                    "artist": resolved.artist_str,
                    "album": resolved.album,
                    "duration_s": resolved.duration_s,
                    "duration_str": resolved.duration_str,
                    "cover_url": resolved.cover_url,
                    "url": resolved.spotify_url,
                    "isrc": resolved.isrc,
                    "has_synced_lyrics": None,
                }]

        # 2. YouTube Playlist
        opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and 'entries' in info:
                    for idx, e in enumerate(info['entries'], 1):
                        if not e:
                            continue
                        vid_id = e.get('id') or ''
                        raw_title = e.get('title') or 'Unknown'
                        uploader = e.get('uploader') or e.get('channel') or 'Artist'
                        dur_s = int(e.get('duration') or 0)
                        track_url = f"https://www.youtube.com/watch?v={vid_id}" if vid_id else e.get('url', '')

                        artist = uploader
                        title = raw_title
                        if " - " in raw_title:
                            p = raw_title.split(" - ", 1)
                            artist = p[0].strip()
                            title = p[1].strip()

                        tracks.append({
                            "index": idx,
                            "track_number": idx,
                            "title": title,
                            "artist": artist,
                            "album": info.get('title', 'Playlist'),
                            "duration_s": dur_s,
                            "duration_str": cls._format_duration(dur_s),
                            "cover_url": e.get('thumbnail') or f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                            "url": track_url,
                            "isrc": "",
                            "has_synced_lyrics": None,
                        })
        except Exception:
            pass

        return tracks
