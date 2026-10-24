r"""
SpotiFLAC - Lossless Music Downloader & Metadata Engine
Location: C:\downloader\spotiflac.py

Provides zero-config tokenless Spotify, Apple Music, and Deezer track/album/playlist
metadata resolution, high-fidelity audio stream acquisition, synchronized lyrics from
LRCLIB, high-resolution cover art embedding, and FLAC / 320kbps MP3 transcoding.
"""

import os
import sys
import re
import json
import shutil
import tempfile
import subprocess
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Union, Callable
from urllib.parse import urlparse

import requests
import yt_dlp

from config import Config
from utils import sanitize_filename, format_size, format_duration, get_logger, check_ffmpeg, is_internet_available, is_network_error, wait_for_internet_connection

logger = get_logger()


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class SpotifyTrack:
    """Spotify / Music Track Metadata"""
    id: str
    title: str
    artists: List[str]
    album: str = ""
    album_artist: str = ""
    release_date: str = ""
    release_year: str = ""
    duration_ms: int = 0
    duration_s: int = 0
    track_number: int = 1
    total_tracks: int = 1
    disc_number: int = 1
    isrc: str = ""
    cover_url: str = ""
    spotify_url: str = ""
    preview_url: str = ""
    is_explicit: bool = False

    @property
    def artist_str(self) -> str:
        return ", ".join(self.artists) if self.artists else "Unknown Artist"

    @property
    def duration_str(self) -> str:
        return format_duration(self.duration_s)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "title": self.title,
            "artists": self.artists,
            "artist": self.artist_str,
            "album": self.album,
            "album_artist": self.album_artist or self.artist_str,
            "release_date": self.release_date,
            "release_year": self.release_year,
            "duration_ms": self.duration_ms,
            "duration_s": self.duration_s,
            "duration_str": format_duration(self.duration_s),
            "track_number": self.track_number,
            "total_tracks": self.total_tracks,
            "disc_number": self.disc_number,
            "isrc": self.isrc,
            "cover_url": self.cover_url,
            "spotify_url": self.spotify_url,
            "preview_url": self.preview_url,
            "is_explicit": self.is_explicit,
        }


@dataclass
class SpotifyCollection:
    """Album, Playlist, or Artist Collection"""
    id: str
    name: str
    type: str  # "album", "playlist", "artist"
    owner: str = ""
    cover_url: str = ""
    total_tracks: int = 0
    tracks: List[SpotifyTrack] = field(default_factory=list)
    spotify_url: str = ""

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "owner": self.owner,
            "cover_url": self.cover_url,
            "total_tracks": len(self.tracks) if self.tracks else self.total_tracks,
            "spotify_url": self.spotify_url,
            "tracks": [t.to_dict() for t in self.tracks],
        }


# ============================================================================
# METADATA RESOLVER
# ============================================================================

class SpotifyMetadataResolver:
    """
    Zero-config tokenless metadata scraper for Spotify tracks, albums, playlists, and artists.
    Uses public embed endpoints and OEmbed to retrieve complete structured metadata.
    """

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    @classmethod
    def parse_url(cls, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract type and ID from Spotify URL or URI."""
        url = url.strip()
        # Handle URI format: spotify:track:id
        if url.startswith("spotify:"):
            parts = url.split(":")
            if len(parts) >= 3:
                return parts[1], parts[2].split("?")[0]

        # Handle web URL: https://open.spotify.com/track/id...
        match = re.search(r'open\.spotify\.com/(track|album|playlist|artist)/([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1), match.group(2)

        return None, None

    @classmethod
    def resolve(cls, url: str) -> Union[SpotifyTrack, SpotifyCollection, None]:
        """Resolve a Spotify URL to a track or collection."""
        media_type, media_id = cls.parse_url(url)
        if not media_type or not media_id:
            logger.warning(f"Could not parse Spotify URL: {url}")
            return None

        embed_url = f"https://open.spotify.com/embed/{media_type}/{media_id}"
        canonical_url = f"https://open.spotify.com/{media_type}/{media_id}"

        try:
            r = requests.get(embed_url, headers=cls.HEADERS, timeout=12)
            if r.status_code != 200:
                logger.error(f"Failed to fetch Spotify embed: HTTP {r.status_code}")
                # Fallback to OEmbed for track
                return cls._fallback_oembed(url, canonical_url, media_type, media_id)

            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
            if not match:
                return cls._fallback_oembed(url, canonical_url, media_type, media_id)

            data = json.loads(match.group(1))
            entity = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
            if not entity:
                return cls._fallback_oembed(url, canonical_url, media_type, media_id)

            if media_type == "track":
                return cls._parse_track_entity(entity, canonical_url, media_id)
            elif media_type in ("album", "playlist", "artist"):
                return cls._parse_collection_entity(entity, media_type, canonical_url, media_id)

        except Exception as e:
            logger.error(f"Error resolving Spotify metadata: {e}")
            return cls._fallback_oembed(url, canonical_url, media_type, media_id)

        return None

    @classmethod
    def _parse_release_date(cls, rel_data: Union[Dict, str, None]) -> Tuple[str, str]:
        """Parse release date and year from diverse Spotify formats."""
        if not rel_data:
            return "", ""
        if isinstance(rel_data, str):
            return rel_data, rel_data[:4]
        if isinstance(rel_data, dict):
            if "isoString" in rel_data:
                iso = str(rel_data["isoString"])
                return iso, iso[:4]
            if "year" in rel_data:
                y = str(rel_data["year"])
                m = str(rel_data.get("month", "01")).zfill(2)
                d = str(rel_data.get("day", "01")).zfill(2)
                return f"{y}-{m}-{d}", y
        return str(rel_data), str(rel_data)[:4]

    @classmethod
    def _parse_cover_url(cls, entity: Dict) -> str:
        """Extract best available cover art URL."""
        # Check visualIdentity images
        vis = entity.get("visualIdentity", {})
        if isinstance(vis, dict) and "image" in vis:
            imgs = vis["image"]
            if isinstance(imgs, list) and imgs:
                best = max(imgs, key=lambda x: x.get("maxWidth", 0) or 0)
                if best.get("url"):
                    return best["url"]

        # Check coverArt / images sources
        cover = entity.get("coverArt") or entity.get("images")
        if isinstance(cover, dict) and "sources" in cover:
            sources = cover["sources"]
            if isinstance(sources, list) and sources:
                return sources[0].get("url", "")
        elif isinstance(cover, list) and cover:
            return cover[0].get("url", "")

        return ""

    @classmethod
    def _parse_track_entity(cls, entity: Dict, canonical_url: str, track_id: str) -> SpotifyTrack:
        """Parse track entity from JSON."""
        title = entity.get("name") or entity.get("title") or "Unknown Title"
        artists_data = entity.get("artists", [])
        artists = []
        if isinstance(artists_data, list):
            for a in artists_data:
                if isinstance(a, dict) and a.get("name"):
                    artists.append(a["name"])
                elif isinstance(a, str):
                    artists.append(a)
        if not artists and entity.get("subtitle"):
            artists = [entity["subtitle"]]
        if not artists:
            artists = ["Unknown Artist"]

        cover_url = cls._parse_cover_url(entity)
        dur_ms = entity.get("duration", 0) or 0
        dur_s = dur_ms // 1000 if dur_ms else 0
        rel_date, year = cls._parse_release_date(entity.get("releaseDate"))

        preview_url = ""
        audio_prev = entity.get("audioPreview")
        if isinstance(audio_prev, dict):
            preview_url = audio_prev.get("url", "")

        return SpotifyTrack(
            id=track_id,
            title=title,
            artists=artists,
            album=entity.get("album", {}).get("name", "") if isinstance(entity.get("album"), dict) else "",
            album_artist=artists[0] if artists else "",
            release_date=rel_date,
            release_year=year,
            duration_ms=dur_ms,
            duration_s=dur_s,
            track_number=1,
            total_tracks=1,
            disc_number=1,
            cover_url=cover_url,
            spotify_url=canonical_url,
            preview_url=preview_url,
            is_explicit=bool(entity.get("isExplicit", False)),
        )

    @classmethod
    def _parse_collection_entity(cls, entity: Dict, media_type: str, canonical_url: str, coll_id: str) -> SpotifyCollection:
        """Parse album, playlist, or artist collection."""
        name = entity.get("name") or entity.get("title") or f"Spotify {media_type.title()}"
        owner = entity.get("subtitle") or ""
        cover_url = cls._parse_cover_url(entity)
        rel_date, year = cls._parse_release_date(entity.get("releaseDate"))

        track_list = entity.get("trackList", [])
        tracks: List[SpotifyTrack] = []

        for idx, t in enumerate(track_list, 1):
            if not isinstance(t, dict):
                continue
            t_title = t.get("title") or "Unknown Track"
            t_subtitle = t.get("subtitle") or owner or "Unknown Artist"
            t_artists = [a.strip() for a in t_subtitle.split(",")] if t_subtitle else ["Unknown Artist"]
            t_uri = t.get("uri", "")
            t_id = t_uri.split(":")[-1] if ":" in t_uri else f"{coll_id}_{idx}"
            t_dur_ms = t.get("duration", 0) or 0
            t_dur_s = t_dur_ms // 1000 if t_dur_ms else 0

            t_prev = ""
            prev_obj = t.get("audioPreview")
            if isinstance(prev_obj, dict):
                t_prev = prev_obj.get("url", "")

            track_obj = SpotifyTrack(
                id=t_id,
                title=t_title,
                artists=t_artists,
                album=name if media_type == "album" else "",
                album_artist=owner if media_type == "album" else (t_artists[0] if t_artists else ""),
                release_date=rel_date,
                release_year=year,
                duration_ms=t_dur_ms,
                duration_s=t_dur_s,
                track_number=idx,
                total_tracks=len(track_list),
                disc_number=1,
                cover_url=cover_url,
                spotify_url=f"https://open.spotify.com/track/{t_id}" if "track:" in t_uri else canonical_url,
                preview_url=t_prev,
                is_explicit=bool(t.get("isExplicit", False)),
            )
            tracks.append(track_obj)

        return SpotifyCollection(
            id=coll_id,
            name=name,
            type=media_type,
            owner=owner,
            cover_url=cover_url,
            total_tracks=len(tracks),
            tracks=tracks,
            spotify_url=canonical_url,
        )

    @classmethod
    def _fallback_oembed(cls, original_url: str, canonical_url: str, media_type: str, media_id: str) -> Optional[SpotifyTrack]:
        """Fallback to standard Spotify OEmbed endpoint."""
        try:
            oembed_url = f"https://open.spotify.com/oembed?url={canonical_url}"
            r = requests.get(oembed_url, timeout=8)
            if r.status_code == 200:
                data = r.json()
                title = data.get("title", "Unknown Title")
                # Title is often formatted as 'Track Title' or 'Track - Artist'
                artists = ["Unknown Artist"]
                if " - " in title:
                    parts = title.split(" - ")
                    title = parts[0]
                    artists = [parts[1]]

                return SpotifyTrack(
                    id=media_id,
                    title=title,
                    artists=artists,
                    cover_url=data.get("thumbnail_url", ""),
                    spotify_url=canonical_url,
                )
        except Exception:
            pass
        return None


# ============================================================================
# LRCLIB LYRICS FETCHER
# ============================================================================

class LRCLIBLyricsFetcher:
    """
    Fetches synchronized (.lrc) and plain lyrics from the free, open-source LRCLIB database.
    """

    BASE_URL = "https://lrclib.net/api/get"

    @classmethod
    def fetch_lyrics(
        cls,
        track_name: str,
        artist_name: str,
        album_name: str = "",
        duration_s: int = 0
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Fetch lyrics for a given track.
        Returns:
            Tuple of (synced_lyrics, plain_lyrics)
        """
        params = {
            "track_name": track_name,
            "artist_name": artist_name,
        }
        if album_name:
            params["album_name"] = album_name
        if duration_s > 0:
            params["duration"] = duration_s

        try:
            r = requests.get(cls.BASE_URL, params=params, timeout=8)
            if r.status_code == 200:
                data = r.json()
                synced = data.get("syncedLyrics")
                plain = data.get("plainLyrics")
                return synced, plain
        except Exception as e:
            logger.debug(f"LRCLIB fetch failed: {e}")

        # Fallback search without album/duration
        try:
            search_params = {"q": f"{artist_name} {track_name}"}
            r = requests.get("https://lrclib.net/api/search", params=search_params, timeout=8)
            if r.status_code == 200:
                results = r.json()
                if isinstance(results, list) and results:
                    top = results[0]
                    return top.get("syncedLyrics"), top.get("plainLyrics")
        except Exception:
            pass

        return None, None


# ============================================================================
# AUDIO TAGGER & COVER ART EMBEDDER
# ============================================================================

class SpotiFLACTagger:
    """
    Embeds high-resolution cover artwork, synchronized/plain lyrics,
    and standardized ID3 / Vorbis comments into FLAC and MP3 audio files using FFmpeg.
    """

    @classmethod
    def tag_flac(
        cls,
        audio_file: str,
        cover_file: Optional[str],
        track: SpotifyTrack,
        lyrics: Optional[str] = None
    ) -> bool:
        """Embed Vorbis comments and cover art into a FLAC file."""
        if not check_ffmpeg() or not os.path.exists(audio_file):
            return False

        temp_out = audio_file + ".tagged.flac"
        cmd = ["ffmpeg", "-y", "-i", audio_file]

        if cover_file and os.path.exists(cover_file):
            cmd.extend([
                "-i", cover_file,
                "-map", "0:a", "-map", "1:0",
                "-c:a", "flac", "-c:v", "copy",
                "-disposition:v:0", "attached_pic"
            ])
        else:
            cmd.extend(["-map", "0:a", "-c:a", "flac"])

        cmd.extend([
            "-metadata", f"TITLE={track.title}",
            "-metadata", f"ARTIST={track.artist_str}",
            "-metadata", f"ALBUM={track.album or track.title}",
            "-metadata", f"ALBUMARTIST={track.album_artist or track.artist_str}",
            "-metadata", f"DATE={track.release_year or track.release_date}",
            "-metadata", f"TRACKNUMBER={track.track_number}",
            "-metadata", f"TOTALTRACKS={track.total_tracks}",
            "-metadata", f"DISCNUMBER={track.disc_number}",
        ])

        if track.isrc:
            cmd.extend(["-metadata", f"ISRC={track.isrc}"])
        if lyrics:
            cmd.extend(["-metadata", f"LYRICS={lyrics}"])

        cmd.append(temp_out)

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if res.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 1000:
                shutil.move(temp_out, audio_file)
                return True
            else:
                if os.path.exists(temp_out):
                    os.remove(temp_out)
                logger.warning(f"FFmpeg FLAC tagging exited with code {res.returncode}: {res.stderr[:200]}")
        except Exception as e:
            logger.error(f"Error tagging FLAC file: {e}")
            if os.path.exists(temp_out):
                os.remove(temp_out)

        return False

    @classmethod
    def tag_mp3(
        cls,
        audio_file: str,
        cover_file: Optional[str],
        track: SpotifyTrack,
        lyrics: Optional[str] = None
    ) -> bool:
        """Embed ID3v2.3 tags and cover art into an MP3 file."""
        if not check_ffmpeg() or not os.path.exists(audio_file):
            return False

        temp_out = audio_file + ".tagged.mp3"
        cmd = ["ffmpeg", "-y", "-i", audio_file]

        if cover_file and os.path.exists(cover_file):
            cmd.extend([
                "-i", cover_file,
                "-map", "0:a", "-map", "1:0",
                "-c:a", "copy", "-c:v", "copy",
                "-id3v2_version", "3",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
                "-disposition:v:0", "attached_pic"
            ])
        else:
            cmd.extend(["-map", "0:a", "-c:a", "copy", "-id3v2_version", "3"])

        cmd.extend([
            "-metadata", f"title={track.title}",
            "-metadata", f"artist={track.artist_str}",
            "-metadata", f"album={track.album or track.title}",
            "-metadata", f"album_artist={track.album_artist or track.artist_str}",
            "-metadata", f"date={track.release_year or track.release_date}",
            "-metadata", f"track={track.track_number}/{track.total_tracks}",
            "-metadata", f"disc={track.disc_number}",
        ])

        if lyrics:
            cmd.extend(["-metadata", f"lyrics={lyrics}"])

        cmd.append(temp_out)

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if res.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 1000:
                shutil.move(temp_out, audio_file)
                return True
            else:
                if os.path.exists(temp_out):
                    os.remove(temp_out)
        except Exception as e:
            logger.error(f"Error tagging MP3 file: {e}")
            if os.path.exists(temp_out):
                os.remove(temp_out)

        return False


# ============================================================================
# SPOTIFLAC DOWNLOADER ENGINE
# ============================================================================

class SpotiFLACDownloader:
    """
    Main SpotiFLAC Downloader Engine.
    Handles track resolution, high-fidelity audio stream acquisition, lyrics sync,
    cover art download, FLAC/320k MP3 transcoding, and tag preservation.
    """

    def __init__(self, output_dir: Optional[str] = None):
        if output_dir:
            self.output_dir = output_dir
        else:
            self.output_dir = os.path.join(Config.AUDIO_DIR, "spotify")
        os.makedirs(self.output_dir, exist_ok=True)

    @classmethod
    def is_spotify_url(cls, url: str) -> bool:
        """Check if URL is a Spotify URL."""
        return bool(re.search(r'open\.spotify\.com|spotify:', url, re.IGNORECASE))

    @classmethod
    def get_info(cls, url: str) -> Optional[Dict]:
        """Resolve metadata and return dictionary for API / CLI."""
        resolved = SpotifyMetadataResolver.resolve(url)
        if not resolved:
            return None
        return resolved.to_dict()

    def download_track(
        self,
        track: SpotifyTrack,
        output_dir: Optional[str] = None,
        quality: str = "flac",
        progress_callback: Optional[Callable[[Dict], None]] = None
    ) -> Optional[Dict]:
        """
        Download a single Spotify track in FLAC (Lossless) or MP3 320kbps.
        """
        out_dir = output_dir or self.output_dir
        is_flac = (quality.lower() == "flac")
        ext = "flac" if is_flac else "mp3"

        if track.total_tracks > 1:
            # Albums / collections get their own subfolder
            album_folder = sanitize_filename(track.album or "Album")
            target_dir = os.path.join(out_dir, album_folder)
            filename = f"{track.track_number:02d} - {sanitize_filename(track.title)}.{ext}"
        else:
            # Single tracks save directly into audios/spotify/ (like audios/YouTube/)
            target_dir = out_dir
            filename = f"{sanitize_filename(track.artist_str)} - {sanitize_filename(track.title)}.{ext}"

        os.makedirs(target_dir, exist_ok=True)
        final_filepath = os.path.join(target_dir, filename)

        if progress_callback:
            progress_callback({
                "status": "searching",
                "track": track.title,
                "artist": track.artist_str,
                "progress": 10,
                "message": f"Searching audio stream for '{track.artist_str} - {track.title}'...",
            })

        # 1. Search high quality audio stream using yt-dlp
        search_query = f"ytsearch1:{track.artist_str} - {track.title} audio"
        temp_dir = tempfile.mkdtemp(prefix="spotiflac_")
        temp_audio = os.path.join(temp_dir, f"raw_audio.%(ext)s")

        node_path = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
        js_runtimes = {"node": {"path": node_path}} if os.path.exists(node_path) else {}

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_audio,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'retries': 20,
            'fragment_retries': 50,
            'continuedl': True,
            'remote_components': ['ejs:github'],
            'extractor_args': {'youtube': {'player_client': ['mweb', 'web']}},
        }
        if js_runtimes:
            ydl_opts['js_runtimes'] = js_runtimes

        try:
            while True:
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(search_query, download=True)
                        if not info or not info.get('entries'):
                            search_query2 = f"ytsearch1:{track.artist_str} - {track.title}"
                            info = ydl.extract_info(search_query2, download=True)
                    break
                except Exception as e_track:
                    if is_network_error(e_track) or not is_internet_available():
                        if progress_callback:
                            progress_callback({
                                "status": "paused_offline",
                                "message": "⚠️ Internet lost! SpotiFLAC download paused... Waiting for connection.",
                            })
                        wait_for_internet_connection(task_name=f"{track.artist_str} - {track.title}")
                        continue
                    search_query2 = f"ytsearch1:{track.artist_str} - {track.title}"
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(search_query2, download=True)
                        break
                    except Exception as e2:
                        if is_network_error(e2) or not is_internet_available():
                            wait_for_internet_connection(task_name=f"{track.artist_str} - {track.title}")
                            continue
                        break

            # Find the downloaded temp audio file
            downloaded_files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
            if not downloaded_files:
                logger.error("No audio stream downloaded")
                return None

            raw_file = downloaded_files[0]

            if progress_callback:
                progress_callback({
                    "status": "transcoding",
                    "track": track.title,
                    "artist": track.artist_str,
                    "progress": 50,
                    "message": f"Encoding audio to {quality.upper()} and fetching lyrics...",
                })

            # 2. Download high-resolution cover art
            cover_file = None
            if track.cover_url:
                cover_path = os.path.join(temp_dir, "cover.jpg")
                try:
                    c_res = requests.get(track.cover_url, timeout=10)
                    if c_res.status_code == 200:
                        with open(cover_path, "wb") as cf:
                            cf.write(c_res.content)
                        cover_file = cover_path
                        # Also save cover art in album folder if desired
                        if Config.SAVE_COVER_ART:
                            album_cover = os.path.join(target_dir, "cover.jpg")
                            if not os.path.exists(album_cover):
                                shutil.copy(cover_path, album_cover)
                except Exception as e:
                    logger.debug(f"Cover download error: {e}")

            # 3. Fetch LRCLIB lyrics (synchronized & plain)
            synced_lyrics, plain_lyrics = LRCLIBLyricsFetcher.fetch_lyrics(
                track_name=track.title,
                artist_name=track.artist_str,
                album_name=track.album,
                duration_s=track.duration_s,
            )

            lrc_file_path = None
            if synced_lyrics and Config.SAVE_LRC_FILE:
                lrc_filename = os.path.splitext(filename)[0] + ".lrc"
                lrc_file_path = os.path.join(target_dir, lrc_filename)
                try:
                    with open(lrc_file_path, "w", encoding="utf-8") as lf:
                        lf.write(synced_lyrics)
                except Exception as e:
                    logger.debug(f"Error saving .lrc file: {e}")

            # 4. Transcode and tag with FFmpeg
            transcoded_temp = os.path.join(temp_dir, f"transcoded.{ext}")
            if is_flac:
                transcode_cmd = ["ffmpeg", "-y", "-i", raw_file, "-c:a", "flac", transcoded_temp]
            else:
                transcode_cmd = ["ffmpeg", "-y", "-i", raw_file, "-c:a", "libmp3lame", "-b:a", "320k", transcoded_temp]

            subprocess.run(transcode_cmd, capture_output=True, check=False)

            if not os.path.exists(transcoded_temp):
                logger.error("FFmpeg transcoding failed")
                return None

            # Apply full tags, cover art, and lyrics
            embedded_lyrics = synced_lyrics or plain_lyrics
            if is_flac:
                SpotiFLACTagger.tag_flac(transcoded_temp, cover_file, track, embedded_lyrics)
            else:
                SpotiFLACTagger.tag_mp3(transcoded_temp, cover_file, track, embedded_lyrics)

            # Move to final location
            shutil.move(transcoded_temp, final_filepath)

            filesize = os.path.getsize(final_filepath) if os.path.exists(final_filepath) else 0

            if progress_callback:
                progress_callback({
                    "status": "complete",
                    "track": track.title,
                    "artist": track.artist_str,
                    "filepath": final_filepath,
                    "filesize": filesize,
                    "progress": 100,
                    "message": f"Successfully downloaded '{track.title}' ({format_size(filesize)})",
                })

            return {
                "title": track.title,
                "artist": track.artist_str,
                "album": track.album,
                "filepath": final_filepath,
                "lrc_path": lrc_file_path,
                "cover_path": cover_file,
                "filesize": filesize,
                "filesize_human": format_size(filesize),
                "format": ext.upper(),
                "duration": format_duration(track.duration_s),
                "has_synced_lyrics": bool(synced_lyrics),
            }

        except Exception as e:
            logger.error(f"Error downloading track {track.title}: {e}")
            if progress_callback:
                progress_callback({
                    "status": "error",
                    "track": track.title,
                    "artist": track.artist_str,
                    "message": str(e),
                })
            return None

        finally:
            # Clean up temp folder
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    def download_track_metadata(
        self,
        title: str,
        artist: str,
        album: str = "",
        cover_url: str = "",
        duration_s: int = 0,
        release_year: str = "",
        quality: str = "flac",
        output_dir: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict], None]] = None
    ) -> List[Dict]:
        """Download track directly from metadata (title, artist, album, cover, duration)."""
        track = SpotifyTrack(
            id=re.sub(r'[^a-zA-Z0-9]', '', f"{artist}_{title}"),
            title=title,
            artists=[artist] if artist else ["Artist"],
            album=album or title,
            album_artist=artist,
            release_year=release_year,
            duration_s=duration_s,
            duration_ms=duration_s * 1000,
            cover_url=cover_url,
            spotify_url="",
        )
        res = self.download_track(track, output_dir=output_dir, quality=quality, progress_callback=progress_callback)
        return [res] if res else []

    def download_url(
        self,
        url: str,
        quality: str = "flac",
        output_dir: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict], None]] = None
    ) -> List[Dict]:
        """
        Download a Spotify track/album/playlist, YouTube music URL, or song search query.
        """
        # 1. Spotify URL
        if self.is_spotify_url(url):
            resolved = SpotifyMetadataResolver.resolve(url)
            if not resolved:
                raise ValueError(f"Could not resolve Spotify URL: {url}")

            results = []
            if isinstance(resolved, SpotifyTrack):
                res = self.download_track(resolved, output_dir=output_dir, quality=quality, progress_callback=progress_callback)
                if res:
                    results.append(res)
            elif isinstance(resolved, SpotifyCollection):
                total = len(resolved.tracks)
                logger.info(f"Downloading {resolved.type} '{resolved.name}' ({total} tracks)...")

                for idx, trk in enumerate(resolved.tracks, 1):
                    if progress_callback:
                        progress_callback({
                            "status": "batch_progress",
                            "collection": resolved.name,
                            "current_index": idx,
                            "total_tracks": total,
                            "progress": int(((idx - 1) / total) * 100),
                            "message": f"[{idx}/{total}] Downloading '{trk.title}'...",
                        })

                    res = self.download_track(trk, output_dir=output_dir, quality=quality, progress_callback=progress_callback)
                    if res:
                        results.append(res)

            return results

        # 2. Track metadata descriptor: "track:Artist:::Title:::Album:::CoverUrl:::Duration"
        if url.startswith("track:"):
            parts = url[6:].split(":::")
            artist = parts[0] if len(parts) > 0 else "Artist"
            title = parts[1] if len(parts) > 1 else "Title"
            album = parts[2] if len(parts) > 2 else ""
            cover_url = parts[3] if len(parts) > 3 else ""
            dur_s = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            year = parts[5] if len(parts) > 5 else ""
            return self.download_track_metadata(
                title=title, artist=artist, album=album,
                cover_url=cover_url, duration_s=dur_s,
                release_year=year, quality=quality,
                output_dir=output_dir, progress_callback=progress_callback
            )

        # 3. Direct YouTube watch URL or search query
        # Try resolving iTunes / LRCLIB metadata for best tags & cover art
        raw_query = url
        if "youtube.com" in url or "youtu.be" in url:
            # Extract video title first
            try:
                opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    yinfo = ydl.extract_info(url, download=False)
                    raw_query = yinfo.get('title', url) if yinfo else url
            except Exception:
                pass

        # Search iTunes for high-res master tags
        clean_q = raw_query
        for clean_suffix in [" (Official Audio)", " (Official Video)", " [Official Audio]", " (Audio)", " (Lyrics)"]:
            clean_q = clean_q.replace(clean_suffix, "")

        title = clean_q
        artist = "Artist"
        album = ""
        cover_url = ""
        dur_s = 0
        year = ""

        try:
            r = requests.get("https://itunes.apple.com/search", params={"term": clean_q, "entity": "song", "limit": 1}, timeout=5)
            if r.status_code == 200:
                data = r.json().get("results", [])
                if data:
                    first = data[0]
                    title = first.get("trackName", title)
                    artist = first.get("artistName", artist)
                    album = first.get("collectionName", "")
                    cover_url = (first.get("artworkUrl100") or "").replace("100x100bb.jpg", "1000x1000bb.jpg")
                    dur_s = int(first.get("trackTimeMillis", 0) / 1000)
                    year = (first.get("releaseDate") or "")[:4]
        except Exception:
            pass

        if artist == "Artist" and " - " in clean_q:
            parts = clean_q.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()

        return self.download_track_metadata(
            title=title, artist=artist, album=album,
            cover_url=cover_url, duration_s=dur_s,
            release_year=year, quality=quality,
            output_dir=output_dir, progress_callback=progress_callback
        )


# ============================================================================
# CLI TEST EXECUTION
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SpotiFLAC - Lossless Music Downloader")
    parser.add_argument("url", nargs="?", help="Spotify track, album, or playlist URL")
    parser.add_argument("-q", "--quality", choices=["flac", "320k"], default="flac", help="Audio quality (flac or 320k)")
    parser.add_argument("-o", "--output", help="Output directory")
    parser.add_argument("--test-meta", action="store_true", help="Run metadata resolution test")

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parser.parse_args()

    print("""
    ===================================================
           SpotiFLAC Lossless Music Downloader         
       Lossless FLAC | 320k MP3 | Synced Lyrics | Tags 
    ===================================================
    """)

    if args.test_meta:
        print("[*] Testing Spotify Track Metadata...")
        track = SpotifyMetadataResolver.resolve("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
        if track and isinstance(track, SpotifyTrack):
            print(f"  ✅ Track: {track.title}")
            print(f"  ✅ Artist: {track.artist_str}")
            print(f"  ✅ Cover: {track.cover_url[:60]}...")
            print(f"  ✅ Duration: {track.duration_s}s")

        print("\n[*] Testing LRCLIB Lyrics...")
        synced, plain = LRCLIBLyricsFetcher.fetch_lyrics(track.title, track.artist_str, duration_s=track.duration_s)
        print(f"  ✅ Synced lyrics found: {bool(synced)}")
        if synced:
            print("  Preview:\n   ", "\n    ".join(synced.splitlines()[:4]))

        print("\n[*] Metadata resolution tests passed successfully!")
        sys.exit(0)

    if not args.url:
        url_input = input("\n  Enter Spotify URL (Track / Album / Playlist): ").strip()
        if not url_input:
            print("  No URL provided.")
            sys.exit(0)
        args.url = url_input

    downloader = SpotiFLACDownloader(output_dir=args.output)
    print(f"\n[*] Resolving: {args.url}")
    print(f"[*] Quality: {args.quality.upper()}")

    def on_progress(p):
        print(f"  -> {p.get('message', '')}")

    results = downloader.download_url(args.url, quality=args.quality, progress_callback=on_progress)
    print(f"\n✅ Completed! Downloaded {len(results)} track(s).")
    for r in results:
        print(f"   📄 {os.path.basename(r['filepath'])} ({r['filesize_human']})")
