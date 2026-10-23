# Personal Media Downloader

A modern, high-performance **media & lossless music download manager** with both an interactive **CLI menu** and a rich **Web Dashboard**. Downloads videos, audio, playlists, synchronized lyrics, and multi-track streams from 1000+ platforms — plus a headless Playwright sniffer for downloading video, audio, and subtitles from any website or embedded player.

Built on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp), with native integration for **SpotiFLAC lossless music extraction** and **Playwright-powered media stream sniffing**.

---

## Key Features

### 🌐 Universal Webpage Media & Stream Sniffer (Option 24 / Web Sniffer Tab)
- **Effortless "Paste Any URL & Download"** — downloads video, audio, and subtitle tracks from **any webpage URL**, including complex sites with embedded or iframe players.
- **Headless Playwright Discovery** — automatically inspects DOM trees, traverses nested `<iframe>` players, dismisses intrusive popups, and triggers HTML5, Video.js, JWPlayer, Plyr, Artplayer, and DPlayer instances.
- **In-Browser WebCrypto Segment Unscrambler** — intercepts in-browser cryptographic keystreams (e.g. AES-CTR) and decrypts protected HLS streaming segments on-the-fly (tested on complex anime and streaming portals).
- **Master Manifest & Track Interception** — intercepts Master HLS (`.m3u8`), DASH (`.mpd`), direct streams (`.mp4`), and external subtitle tracks (`.vtt`, `.srt`).
- **Clean Ephemeral Netscape Cookie Jar** — generates domain-scoped cookie files directly from browser sessions, completely eliminating cookie header security warnings and CDN leakage.
- **Smooth Non-Jitter Progress Bar** — fixed-width, flicker-free terminal progress bar with accurate byte counters, live transfer speeds, and ETA.
- **Selectable Tracks & Lossless Muxing** — pick resolution (4K, 1440p, 1080p, 720p, 480p, 360p), audio track (or audio-only extraction), and subtitle languages; muxes cleanly via FFmpeg (`-c copy`) into `.mkv` or `.mp4`.
- **Dedicated Storage** — saves all downloaded streams into `~/downloader/unknown/` with full SQLite database history.

### 🎵 SpotiFLAC — Lossless Music Downloader (Option 22)
- **Direct Spotify Scraping** — downloads tracks, albums, playlists, and artists directly without needing Spotify developer API keys.
- **Studio Quality Audio** — extracts **Lossless FLAC** (16-bit / 44.1kHz) or **320kbps MP3**.
- **Synchronized Lyrics (.lrc)** — automatically fetches time-synced lyrics from LRCLIB and saves `.lrc` files alongside your audio.
- **Embedded Cover Art & Lyrics** — embeds high-resolution album artwork and lyrics directly into audio metadata tags.
- **Complete Tagging** — preserves full ID3v2 & Vorbis tags (title, artist, album, album artist, release year, track number, disc number, and ISRC).

### 🔍 Spotify Search & Playlist Song Explorer (Option 23 / Web Search Tab)
- **Interactive Search Engine** — search songs, albums, artists, playlists, and videos across Spotify, LRCLIB, and YouTube without API tokens.
- **Pre-Download Inspection** — check duration, bitrate, and whether synchronized lyrics (`.lrc`) or subtitles are available before downloading.
- **Selective Track Downloading** — browse every song in a playlist or album, view durations, and select specific tracks (e.g. `1, 3, 5-8` or `all`).
- **Seamless Navigation** — quick `[b]` back options at every step in terminal and drawer modals in the Web UI.

### 🎬 Standard Media Downloads (Options 1–7)
- **Video** — up to 8K@60fps with automatic FFmpeg audio/video merging.
- **Audio** — MP3, M4A, AAC, FLAC, WAV, OPUS, OGG at up to 320kbps / lossless.
- **Playlists** — full or partial range downloads with duplicate skip.
- **Batch Downloads** — download multiple URLs in sequence from file or terminal.
- **Thumbnails & Subtitles** — separate cover art extraction and multi-language subtitle fetching.
- **1000+ Platforms Supported** — YouTube, Instagram, Twitter/X, TikTok, Reddit, Facebook, Vimeo, Twitch, SoundCloud, Dailymotion, Bilibili, and all yt-dlp supported sites.

### 💻 Modern Web UI Dashboard (`web/app.py`)
- **Fast, Dark-Themed UI** — built with modern Tailwind CSS and vanilla JavaScript.
- **Tabbed Interface**:
  - **Direct Link**: Instant video/audio download with format and quality selectors.
  - **Search Explorer**: Live search for music and videos with card previews and tracklist inspection.
  - **Universal Sniffer**: Live webpage sniffer with stream candidate cards, resolution badges, audio selectors, and real-time SSE progress streaming.

### ⚡ Smart Productivity Features
- **Clipboard Auto-Paste** — automatically detects valid URLs on your clipboard when opening the menu.
- **Smart URL Auto-Detection**:
  - Spotify URLs → automatically routes to the **SpotiFLAC** engine.
  - Streaming manifests (`.m3u8`, `.mpd`) or streaming webpages → automatically routes to the **Universal Sniffer**.
  - General video URLs → standard high-speed download pipeline.
- **Download Archive** — SQLite database prevents accidental duplicate re-downloads.
- **Desktop Notifications** — Windows toast notifications with click-to-open-folder functionality.
- **ML Training Data Logger** — structured JSONL telemetry (`ml_training.jsonl`) for tracking speeds, strategies, and download performance.

---

## Requirements

- Python 3.10+
- [FFmpeg](https://ffmpeg.org/download.html) (must be on your system `PATH`)
- [Playwright](https://playwright.dev/) (for Universal Webpage Sniffer)

---

## Installation & Setup

```bash
git clone https://github.com/AdityaANKS/downloader.git
cd downloader
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser engines (for Universal Sniffer)
playwright install chromium
```

---

## How to Use

### 1. Interactive CLI Menu

```bash
python downloader.py
```

The CLI features a clean, numbered menu:

```text
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
```

> **Tip:** You can paste any URL directly at the `Enter choice:` prompt! The downloader will automatically identify the URL type and launch the appropriate download flow.

---

### 2. Web UI Dashboard

```bash
python web/app.py
```

Open your browser to:
```text
http://localhost:5000
```

---

## Where Files Are Saved

By default, files are organized into dedicated category directories inside your user profile:

| OS | Default Base Directory |
|---|---|
| **Windows** | `C:\Users\<user>\downloader\` (or `Downloads\downloader\`) |
| **macOS** | `~/downloader/` (or `~/Downloads/downloader/`) |
| **Linux** | `~/downloader/` (or `~/Downloads/downloader/`) |

### Folder Structure
```text
~/downloader/
├── videos/             ← Standard video downloads (organized by platform)
│   ├── YouTube/
│   ├── Instagram/
│   └── ...
├── audios/             ← Audio-only extractions (MP3, M4A, etc.)
├── music/              ← SpotiFLAC lossless audio (.flac, .mp3, .lrc)
├── unknown/            ← Universal Media Sniffer downloads (.mkv, .mp4)
├── thumbnails/         ← Downloaded artwork & cover files
├── subtitles/          ← Standalone subtitle files (.srt, .vtt)
├── cookies/            ← Ephemeral Netscape cookies
├── downloader.db       ← SQLite history database
└── downloader.log      ← Application logs
```

### Changing the Download Directory
You can set a custom storage directory in the CLI settings (Option 9) or by creating a `~/.downloader_settings.json` file:

```json
{
    "base_dir": "D:\\Media\\Downloads"
}
```

---

## Project Structure

```text
downloader.py          — Interactive CLI menu and main download engine
config.py              — Configuration dataclass, directories, and quality defaults
database.py            — SQLite database manager for history and tracking
quality_manager.py     — Quality presets, codec negotiations, and format selectors
platforms.py           — Platform detection, URL patterns, and video ID resolution
utils.py               — Helpers: sanitization, size formatting, notifications, FFmpeg checks
spotiflac.py           — SpotiFLAC lossless music engine (FLAC / 320k / LRCLIB lyrics)
search_engine.py       — Unified search engine & playlist track explorer
universal_downloader.py — Universal sniffer & unscrambler for unknown web players & iframes
ml_logger.py           — Structured JSONL telemetry logger for machine learning
web/                   — Modern Flask Web UI
├── app.py             — Backend API server & SSE progress broadcaster
├── templates/         — Responsive HTML5 dashboard (Tailwind CSS)
└── static/            — Stylesheets and vanilla JavaScript frontend controller
```

---

## Cookie & Anti-Bot Bypass Strategy

For protected platforms, the downloader automatically and silently tests multiple strategies:
1. **Installed browser cookie extraction** — Chrome, Firefox, Edge, Brave, Opera
2. **Client signature impersonation** — `web_safari`, `ios`, `android`, `web`
3. **Ephemeral Netscape cookie generation** — isolates cookies per domain during sniffing

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
