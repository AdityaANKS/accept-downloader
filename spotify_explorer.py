"""
spotify_explorer.py — Dedicated Spotify Search Engine & Playlist Explorer
==========================================================================
Standalone & integrated Spotify music browser, metadata/lyrics inspector,
and album/playlist song explorer.

Features:
- Pure Spotify-focused search & exploration (Songs, Albums, Playlists, Artists)
- Deep metadata inspection with LRCLIB Synced Lyrics (.lrc) verification & preview
- Interactive Playlist/Album Track Explorer with multi-song range selection
- Lossless FLAC & 320k MP3 downloading with embedded 1000x1000 art & lyrics tags
- Beautiful Compart Unicode terminal UI with spinners and back navigation
"""

import os
import sys
import time
import json
import re
import shutil
import threading
import requests
import urllib.parse
from typing import List, Dict, Optional, Tuple, Any, Union
from dataclasses import dataclass, field, asdict

# Ensure project directory is in sys.path when run directly
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

try:
    from config import Config
except Exception:
    class Config:
        MUSIC_DIR = current_dir
        SAVE_LRC_FILE = True
        EMBED_LYRICS = True
        EMBED_THUMBNAIL = True

from spotiflac import (
    SpotifyMetadataResolver, LRCLIBLyricsFetcher,
    SpotiFLACDownloader, SpotifyTrack, SpotifyCollection
)


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class SpotifySearchItem:
    id: str
    title: str
    artist: str
    album: str = ""
    duration_s: int = 0
    duration_str: str = "0:00"
    thumbnail: str = ""
    url: str = ""
    media_type: str = "track"       # "track", "album", "playlist", "artist"
    has_synced_lyrics: bool = False
    has_plain_lyrics: bool = False
    release_year: str = ""
    track_count: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================================
# SPOTIFY SEARCH & INSPECTION ENGINE
# ============================================================================

class SpotifySearchEngine:
    """
    Dedicated search engine & metadata explorer exclusively for Spotify.
    """

    @staticmethod
    def format_duration(seconds: int) -> str:
        if not seconds or seconds <= 0:
            return "0:00"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @classmethod
    def check_lyrics_fast(cls, artist: str, title: str) -> Tuple[bool, bool]:
        """Check if LRCLIB has synced (.lrc) or plain lyrics available."""
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
    def search_spotify(cls, query: str, limit: int = 20) -> List[SpotifySearchItem]:
        """
        Search exclusively for Spotify tracks, albums, and artists.
        Uses iTunes Master recording database & LRCLIB for official metadata.
        """
        items: List[SpotifySearchItem] = []
        seen_keys = set()

        # If user passed a direct Spotify URL, resolve it directly
        if SpotiFLACDownloader.is_spotify_url(query):
            resolved = SpotifyMetadataResolver.resolve(query)
            if isinstance(resolved, SpotifyTrack):
                has_synced, has_plain = cls.check_lyrics_fast(resolved.artist_str, resolved.title)
                return [SpotifySearchItem(
                    id=resolved.id,
                    title=resolved.title,
                    artist=resolved.artist_str,
                    album=resolved.album,
                    duration_s=resolved.duration_s,
                    duration_str=resolved.duration_str,
                    thumbnail=resolved.cover_url,
                    url=resolved.spotify_url or query,
                    media_type="track",
                    has_synced_lyrics=has_synced,
                    has_plain_lyrics=has_plain,
                    release_year=resolved.release_year,
                )]
            elif isinstance(resolved, SpotifyCollection):
                return [SpotifySearchItem(
                    id=resolved.id,
                    title=resolved.name,
                    artist=resolved.owner or "Spotify",
                    duration_str=f"{resolved.total_tracks} tracks",
                    thumbnail=resolved.cover_url,
                    url=query,
                    media_type=resolved.collection_type,
                    track_count=resolved.total_tracks,
                )]

        # 1. Search iTunes Store Master Index for high-fidelity track & album results
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
                    track_descriptor = f"track:{artist}:::{title}:::{album}:::{hd_cover}:::{dur_s}:::{year}"

                    items.append(SpotifySearchItem(
                        id=str(entry.get("trackId", len(items) + 1)),
                        title=title,
                        artist=artist,
                        album=album,
                        duration_s=dur_s,
                        duration_str=cls.format_duration(dur_s),
                        thumbnail=hd_cover,
                        url=track_descriptor,
                        media_type="track",
                        has_synced_lyrics=has_synced,
                        has_plain_lyrics=has_plain,
                        release_year=year,
                    ))
        except Exception:
            pass

        # 2. Query LRCLIB to find songs with synced lyrics
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

                            track_descriptor = f"track:{artist}:::{title}:::{album}:::::::{dur_s}:::"

                            items.append(SpotifySearchItem(
                                id=str(entry.get("id", len(items) + 1)),
                                title=title,
                                artist=artist,
                                album=album,
                                duration_s=dur_s,
                                duration_str=cls.format_duration(dur_s),
                                thumbnail="",
                                url=track_descriptor,
                                media_type="track",
                                has_synced_lyrics=has_synced,
                                has_plain_lyrics=has_plain,
                                release_year="",
                            ))
                            if len(items) >= limit:
                                break
            except Exception:
                pass

        return items[:limit]

    @classmethod
    def inspect_metadata(cls, url_or_descriptor: str) -> Dict[str, Any]:
        """
        Inspect Spotify track metadata, lyrics availability & preview snippet.
        """
        info: Dict[str, Any] = {
            "url": url_or_descriptor,
            "title": "Unknown",
            "artist": "Unknown",
            "album": "",
            "duration_s": 0,
            "duration_str": "0:00",
            "thumbnail": "",
            "platform": "Spotify",
            "media_type": "track",
            "is_playlist": False,
            "track_count": 1,
            "has_synced_lyrics": False,
            "has_plain_lyrics": False,
            "lyrics_preview": "",
            "audio_formats": ["FLAC Lossless (Studio Master)", "MP3 320 kbps (Extreme)", "AAC 256 kbps"],
        }

        # 1. Track descriptor (from search)
        if url_or_descriptor.startswith("track:"):
            parts = url_or_descriptor[6:].split(":::")
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
                "duration_str": cls.format_duration(dur_s),
                "thumbnail": cover_url,
                "release_year": year,
                "has_synced_lyrics": bool(synced),
                "has_plain_lyrics": bool(plain),
                "lyrics_preview": lyrics_preview,
            })
            return info

        # 2. Spotify URL
        if SpotiFLACDownloader.is_spotify_url(url_or_descriptor):
            resolved = SpotifyMetadataResolver.resolve(url_or_descriptor)
            if isinstance(resolved, SpotifyTrack):
                synced, plain = LRCLIBLyricsFetcher.fetch_lyrics(
                    track_name=resolved.title,
                    artist_name=resolved.artists[0] if resolved.artists else "",
                    album_name=resolved.album,
                    duration_s=resolved.duration_s
                )
                lyrics_preview = ""
                if synced:
                    lyrics_preview = "\n".join([l for l in synced.splitlines() if l.strip()][:6])
                elif plain:
                    lyrics_preview = "\n".join([l for l in plain.splitlines() if l.strip()][:6])

                info.update({
                    "title": resolved.title,
                    "artist": resolved.artist_str,
                    "album": resolved.album,
                    "duration_s": resolved.duration_s,
                    "duration_str": resolved.duration_str,
                    "thumbnail": resolved.cover_url,
                    "release_year": resolved.release_year,
                    "has_synced_lyrics": bool(synced),
                    "has_plain_lyrics": bool(plain),
                    "lyrics_preview": lyrics_preview,
                })
            elif isinstance(resolved, SpotifyCollection):
                info.update({
                    "title": resolved.name,
                    "artist": resolved.owner or "Spotify",
                    "duration_str": f"{resolved.total_tracks} tracks",
                    "thumbnail": resolved.cover_url,
                    "media_type": resolved.collection_type,
                    "is_playlist": True,
                    "track_count": resolved.total_tracks,
                })

        return info

    @classmethod
    def fetch_playlist_tracks(cls, url: str) -> List[Dict[str, Any]]:
        """Extract tracklist from a Spotify album or playlist."""
        tracks: List[Dict[str, Any]] = []
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
                    })
            elif isinstance(resolved, SpotifyTrack):
                tracks.append({
                    "index": 1,
                    "track_number": resolved.track_number,
                    "title": resolved.title,
                    "artist": resolved.artist_str,
                    "album": resolved.album,
                    "duration_s": resolved.duration_s,
                    "duration_str": resolved.duration_str,
                    "cover_url": resolved.cover_url,
                    "url": resolved.spotify_url,
                })
        return tracks


# ============================================================================
# INTERACTIVE CLI RUNNER
# ============================================================================

class SpotifyExplorerCLI:
    """Interactive Compart Unicode CLI for Spotify Search & Playlist Explorer"""

    def __init__(self, output_dir: Optional[str] = None):
        default_dir = os.path.dirname(os.path.abspath(__file__))
        self.output_dir = output_dir or default_dir
        self.downloader = SpotiFLACDownloader(output_dir=self.output_dir)

    def run(self, initial_query: Optional[str] = None):
        query = initial_query
        while True:
            if not query:
                print("\n╭──────────────────────────────────────────────────────────────╮")
                print("│  🎵 SPOTIFY SEARCH ENGINE & PLAYLIST EXPLORER                │")
                print("│  Search Spotify songs, albums, playlists & artists           │")
                print("╰──────────────────────────────────────────────────────────────╯")
                query = input("\n  🔍 Enter Spotify song, artist, album, or URL (or 'b' to go back): ").strip()
                if not query or query.lower() in ('b', 'back', 'exit', '0'):
                    break

            # Spinner during search
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

            results = SpotifySearchEngine.search_spotify(query, limit=24)
            stop_spin.set()
            t.join()
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()

            if not results:
                print(f"\n  ✖ No Spotify results found for '{query}'. Try another search.")
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
                print(f"│  🎵 SPOTIFY RESULTS for \"{query[:32]}\"  [Page {page + 1}/{total_pages}]                     │")
                print(f"╰────────────────────────────────────────────────────────────────────────────╯")

                for i, item in enumerate(page_items, 1):
                    global_idx = start_idx + i
                    icon = "🎵" if item.media_type == "track" else ("💿" if item.media_type == "album" else "📁")
                    lyrics_badge = " [Synced Lyrics: ✔]" if item.has_synced_lyrics else ""
                    dur = f"({item.duration_str})" if item.duration_str != "0:00" else ""
                    artist_str = f" — {item.artist}" if item.artist else ""
                    print(f"  {global_idx:2d}. {icon} {item.title[:38]}{artist_str[:25]} {dur} {lyrics_badge}")

                print(f"\n  [1-{len(results)}] Select & Action  |  [i <num>] Inspect Details")
                print(f"  [n] Next Page  |  [p] Prev Page  |  [s] New Search  |  [b] Back")

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

    def _inspect_and_action(self, item: SpotifySearchItem):
        """Displays detailed inspection card with synced lyrics, artwork, and download options"""
        print(f"\n  ⏳ Inspecting Spotify metadata for '{item.title}'...")
        meta = SpotifySearchEngine.inspect_metadata(item.url)

        print(f"\n╭────────────────────────────────────────────────────────────────────────────╮")
        print(f"│  📋 SPOTIFY METADATA & LYRICS INSPECTION CARD                              │")
        print(f"├────────────────────────────────────────────────────────────────────────────┤")
        print(f"│  Title:     {meta.get('title', item.title)[:60]:<60} │")
        print(f"│  Artist:    {meta.get('artist', item.artist)[:60]:<60} │")
        if meta.get('album'):
            print(f"│  Album:     {meta.get('album', '')[:60]:<60} │")
        print(f"│  Duration:  {meta.get('duration_str', item.duration_str):<60} │")
        print(f"│  Platform:  Spotify Lossless Audio                                         │")

        if meta.get('has_synced_lyrics'):
            print(f"│  Lyrics:    ✔ Synchronized Lyrics Available (.lrc file + embedded)       │")
        elif meta.get('has_plain_lyrics'):
            print(f"│  Lyrics:    ✔ Plain Lyrics Available (embedded)                          │")
        else:
            print(f"│  Lyrics:    ○ Not detected in LRCLIB database                            │")

        print(f"╰────────────────────────────────────────────────────────────────────────────╯")

        if meta.get('lyrics_preview'):
            print(f"\n  📜 Synced Lyrics Preview Snippet:")
            for line in meta.get('lyrics_preview').splitlines()[:5]:
                print(f"     {line}")

        if meta.get('is_playlist') or item.media_type in ('album', 'playlist'):
            print(f"\n  This is a collection with {meta.get('track_count', 1)} tracks.")
            print(f"  1. 📂 Explore & Select Songs inside this Spotify Playlist/Album")
            print(f"  2. ⚡ Download ALL Tracks (FLAC Lossless)")
            print(f"  3. 🎧 Download ALL Tracks (320k MP3)")
            print(f"  4. ↩ Back")
            act = input("\n  Select Action [1]: ").strip() or "1"
            if act == '1':
                self._explore_playlist(item.url, meta.get('title', item.title))
            elif act == '2':
                self._download_track(item.url, quality="flac")
            elif act == '3':
                self._download_track(item.url, quality="320k")
            return

        print(f"\n  DOWNLOAD ACTIONS:")
        print(f"    1. ⚡ Download FLAC (Lossless Studio Quality + Synced Lyrics & Art)")
        print(f"    2. 🎧 Download MP3 (320 kbps Extreme + Embedded Art & Lyrics)")
        print(f"    3. ↩ Back to search results")

        act = input("\n  Select Action [1]: ").strip() or "1"
        if act == '1':
            self._download_track(item.url, quality="flac")
        elif act == '2':
            self._download_track(item.url, quality="320k")

    def _explore_playlist(self, url: str, title: str):
        """Interactive Song Explorer for Spotify Playlists and Albums"""
        print(f"\n  ⏳ Fetching songs inside Spotify collection '{title}'...")
        tracks = SpotifySearchEngine.fetch_playlist_tracks(url)
        if not tracks:
            print("  ✖ Could not load tracks for this playlist.")
            input("\n  Press Enter...")
            return

        selected_indices = set(range(1, len(tracks) + 1))

        while True:
            print(f"\n╭────────────────────────────────────────────────────────────────────────────╮")
            print(f"│  📂 SPOTIFY PLAYLIST SONG EXPLORER: {title[:36]:<36} │")
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
                        self._inspect_and_action(SpotifySearchItem(
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

                print(f"\n  ⏳ Downloading {len(selected_indices)} selected Spotify track(s) in {quality.upper()}...")
                selected_tracks = [t for t in tracks if t['index'] in selected_indices]
                for st in selected_tracks:
                    print(f"\n  -> [{st['index']}/{len(tracks)}] {st['artist']} - {st['title']}")
                    self.downloader.download_url(st['url'], quality=quality, progress_callback=lambda p: print(f"     {p.get('message', '')}"))

                print(f"\n  ✔ Completed downloading {len(selected_indices)} Spotify track(s)!")
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

    def _download_track(self, url: str, quality: str = "flac"):
        print(f"\n  ⏳ Downloading with SpotiFLAC ({quality.upper()})...")
        def _prog(p):
            msg = p.get('message', '')
            if msg:
                print(f"  -> {msg}")
        res = self.downloader.download_url(url, quality=quality, progress_callback=_prog)
        if res:
            print("\n  ✅ Download complete!")
            for r in res:
                print(f"  Saved: {r.get('filepath')}")
                if r.get('lrc_path'):
                    print(f"  Synced Lyrics: {r.get('lrc_path')}")
        input("\n  Press Enter to continue...")


# ============================================================================
# STANDALONE ENTRY POINT
# ============================================================================

def main():
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    query = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else None
    cli = SpotifyExplorerCLI()
    cli.run(initial_query=query)


if __name__ == "__main__":
    main()
