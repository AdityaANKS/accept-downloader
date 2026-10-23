"""
VAULT Web UI — Flask Backend
Wraps yt-dlp directly with REST API + SSE progress.
Supports ALL sites that yt-dlp supports (1800+).
"""

import os
import sys
import json
import time
import uuid
import threading
import re
from queue import Queue
from datetime import datetime

# Add parent dir so we can import local modules
PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)

from flask import Flask, request, jsonify, render_template, Response, send_file

# ---- Import yt-dlp directly ----
try:
    import yt_dlp
    YTDLP_OK = True
    YTDLP_VERSION = getattr(yt_dlp.version, '__version__', 'unknown')
except ImportError:
    YTDLP_OK = False
    YTDLP_VERSION = None

# ---- Import project modules ----
from config import Config
Config.init_directories()

try:
    from database import DatabaseManager
    db = DatabaseManager()
except Exception:
    db = None

from utils import format_size, check_ffmpeg, format_duration

try:
    from spotiflac import SpotiFLACDownloader, SpotifyMetadataResolver, SpotifyTrack, SpotifyCollection
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
    from universal_downloader import UniversalMediaDownloader, WebpageStreamSniffer, StreamCandidate, PLAYWRIGHT_AVAILABLE
    UNIVERSAL_AVAILABLE = True
except ImportError:
    UNIVERSAL_AVAILABLE = False
    PLAYWRIGHT_AVAILABLE = False



class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


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


def _detect_js_runtimes():
    """Detect available JS runtimes for yt-dlp PhantomJS/Node challenges."""
    import shutil
    runtimes = []
    for name in ['phantomjs', 'node', 'nodejs', 'deno', 'bun']:
        if shutil.which(name):
            runtimes.append(name)
    return runtimes


def _apply_js_runtimes(opts):
    """Apply JS runtime paths if available."""
    pass  # yt-dlp auto-detects; keeping this as extension point


# ============================================================================
# APP INIT
# ============================================================================

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = 'vault-downloader-secret'

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
    return response

# Active download jobs
_jobs = {}
_jobs_lock = threading.Lock()


# ============================================================================
# CORE: GET INFO (supports ALL yt-dlp sites)
# ============================================================================

def get_video_info(url: str) -> dict:
    """Fetch video info using yt-dlp or SpotiFLAC. Works with ANY supported URL (1800+ sites)."""
    if SPOTIFLAC_AVAILABLE and SpotiFLACDownloader.is_spotify_url(url):
        try:
            info_dict = SpotiFLACDownloader.get_info(url)
            if info_dict:
                is_coll = info_dict.get('type') in ('album', 'playlist', 'artist')
                dur = info_dict.get('duration_s', 0) if not is_coll else 0
                dur_str = info_dict.get('duration_str', '') if not is_coll else f"{info_dict.get('total_tracks', 0)} tracks"
                return {
                    'title': info_dict.get('title') or info_dict.get('name', 'Unknown Track'),
                    'uploader': info_dict.get('artist') or info_dict.get('owner', 'Spotify'),
                    'duration': dur,
                    'duration_str': dur_str,
                    'thumbnail': info_dict.get('cover_url', ''),
                    'url': url,
                    'platform': 'Spotify',
                    'is_music': True,
                    'is_playlist': is_coll,
                    'playlist_count': info_dict.get('total_tracks', 0) if is_coll else 0,
                    'video_formats': [],
                    'audio_formats': [
                        {'label': 'FLAC (Lossless 16/24-bit Audio)', 'ext': 'flac', 'abr': 1411, 'format_id': 'flac', 'filesize_str': 'Lossless'},
                        {'label': 'MP3 (320 kbps Extreme Quality)', 'ext': 'mp3', 'abr': 320, 'format_id': '320k', 'filesize_str': '320 kbps'},
                    ],
                    'subtitles': ['Synchronized Lyrics (.lrc)'],
                }
        except Exception as e:
            return {'error': f'Error resolving Spotify music info: {str(e)}'}

    if not YTDLP_OK:
        return {'error': 'yt-dlp is not installed. Run: pip install yt-dlp'}

    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'socket_timeout': 15,
        'extractor_retries': 2,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'ignore_no_formats_error': True,
        'logger': _SilentLogger(),
    }

    # YouTube-specific optimizations
    is_yt = any(d in url.lower() for d in ['youtube.com', 'youtu.be', 'music.youtube.com'])
    if is_yt:
        opts['extractor_args'] = {
            'youtube': {'player_client': ['web_safari', 'android_vr', 'android', 'ios', 'web']}
        }

    info = None

    # Strategy 1: Try with browser cookies (YouTube primarily) — silenced
    if is_yt:
        for browser in ['chrome', 'firefox', 'edge']:
            try:
                co = {**opts, 'cookiesfrombrowser': (browser,)}
                with _suppress_stderr():
                    with yt_dlp.YoutubeDL(co) as ydl:
                        info = ydl.extract_info(url, download=False)
                        if info:
                            break
            except Exception:
                continue

    # Strategy 2: Direct (no cookies) — works for most non-YouTube sites
    if not info:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            pass

    # Strategy 3: Fallback with process=False (gets basic metadata)
    if not info:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
        except Exception as e:
            return {'error': f'Could not fetch info: {str(e)[:200]}'}

    if not info:
        return {'error': 'Could not extract video information. The URL may be invalid or geo-restricted.'}

    # Build structured response
    formats_raw = info.get('formats') or []

    # Video formats (deduplicate by height, pick best per height)
    video_formats = []
    height_best = {}
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
        
        filesize = f.get('filesize') or f.get('filesize_approx') or 0
        fps = f.get('fps') or 0
        
        # Keep best (largest file) per height
        key = h
        if key not in height_best or filesize > height_best[key].get('_size', 0):
            height_best[key] = {
                'height': h,
                'fps': fps,
                'vcodec': vc.split('.')[0],
                'ext': ext,
                'filesize': filesize,
                'filesize_str': format_size(filesize) if filesize else '',
                'format_id': f.get('format_id', ''),
                '_size': filesize,
            }

    for h in sorted(height_best.keys(), reverse=True):
        entry = height_best[h]
        label_map = {4320: '8K', 2160: '4K', 1440: '2K', 1080: 'Full HD', 720: 'HD', 480: 'SD', 360: '360p', 240: '240p', 144: '144p'}
        entry['label'] = label_map.get(h, f'{h}p')
        del entry['_size']
        video_formats.append(entry)

    # Audio formats (deduplicate, show bitrate + size)
    audio_formats = []
    audio_seen = set()
    for f in formats_raw:
        ac = f.get('acodec')
        if not ac or ac == 'none':
            continue
        vc = f.get('vcodec')
        if vc and vc != 'none':
            continue

        abr = int(f.get('abr') or f.get('tbr') or 0)
        ext = (f.get('ext') or '').lower()
        channels = f.get('audio_channels') or 2
        lang = f.get('language') or 'default'
        filesize = f.get('filesize') or f.get('filesize_approx') or 0

        key = f"{lang}_{abr}_{channels}"
        if key in audio_seen:
            continue
        audio_seen.add(key)

        channel_label = '5.1 Surround' if channels >= 6 else 'Stereo' if channels >= 2 else 'Mono'
        audio_formats.append({
            'language': lang,
            'channels': channels,
            'channel_label': channel_label,
            'bitrate': abr,
            'codec': ac.split('.')[0],
            'ext': ext,
            'filesize': filesize,
            'filesize_str': format_size(filesize) if filesize else '',
            'format_id': f.get('format_id', ''),
        })

    audio_formats.sort(key=lambda t: (-t['channels'], -t['bitrate']))

    # Subtitles
    manual_subs = info.get('subtitles') or {}
    auto_subs = info.get('automatic_captions') or {}

    # Thumbnail
    thumb = info.get('thumbnail') or ''
    if not thumb:
        thumbs = info.get('thumbnails') or []
        if thumbs:
            thumb = thumbs[-1].get('url', '')

    # Detect platform from extractor
    extractor = info.get('extractor', '') or info.get('extractor_key', '') or ''

    return {
        'title': info.get('title', 'Unknown'),
        'uploader': info.get('uploader') or info.get('channel') or info.get('uploader_id') or '',
        'duration': info.get('duration') or 0,
        'thumbnail': thumb,
        'platform': extractor.replace(':', '').replace('Generic', 'Other'),
        'url': url,
        'view_count': info.get('view_count'),
        'upload_date': info.get('upload_date'),
        'description': (info.get('description') or '')[:300],
        'video_formats': video_formats,
        'audio_formats': audio_formats,
        'subtitles': {
            'manual': [{'code': k, 'name': k} for k in sorted(manual_subs.keys())],
            'auto_count': len(auto_subs),
            'auto_langs': list(auto_subs.keys())[:20],
        },
        'is_live': info.get('is_live', False),
    }


# ============================================================================
# CORE: DOWNLOAD (with progress callback)
# ============================================================================

def do_download(url: str, options: dict, progress_callback=None) -> dict:
    """Download media using yt-dlp. Works with ALL supported sites."""
    if not YTDLP_OK:
        return {'status': 'failed', 'error': 'yt-dlp not installed'}

    mode = options.get('mode', 'video')
    quality = options.get('quality', '1080p')
    audio_format = options.get('audio_format', 'mp3')
    subtitle_langs = options.get('subtitles')
    custom_args = options.get('custom_args', '')

    if SPOTIFLAC_AVAILABLE and SpotiFLACDownloader.is_spotify_url(url):
        if progress_callback:
            progress_callback({'type': 'status', 'message': 'Connecting to Spotify & resolving metadata...'})
        
        target_quality = "flac" if (audio_format.lower() in ("flac", "wav") or quality.lower() == "flac") else "320k"
        downloader = SpotiFLACDownloader()
        
        def _sf_progress(p):
            if progress_callback:
                progress_callback({
                    'type': 'progress',
                    'percent': p.get('progress', 0),
                    'status': p.get('status', 'downloading'),
                    'message': p.get('message', ''),
                    'speed_str': 'High Speed',
                    'eta': '0',
                    'downloaded_str': format_size(p.get('filesize', 0)) if p.get('filesize') else '',
                    'total_str': '',
                })
        
        try:
            results = downloader.download_url(url, quality=target_quality, progress_callback=_sf_progress)
            if results:
                first = results[0]
                title = first.get('title', 'Unknown')
                filepath = first.get('filepath', '')
                filesize = first.get('filesize', 0)
                
                # Record in database
                if db:
                    for r in results:
                        try:
                            db.add_download(
                                url=url,
                                platform='Spotify',
                                title=r.get('title', 'Unknown'),
                                filesize=r.get('filesize', 0),
                                filepath=r.get('filepath', ''),
                                media_type='audio',
                                status='complete'
                            )
                        except Exception:
                            pass
                
                if progress_callback:
                    progress_callback({
                        'type': 'complete',
                        'filepath': filepath,
                        'title': title,
                        'filesize': filesize,
                        'filesize_str': format_size(filesize),
                    })
                return {
                    'status': 'complete',
                    'filepath': filepath,
                    'filesize': filesize,
                    'title': title,
                    'total_downloaded': len(results),
                }
            else:
                if progress_callback:
                    progress_callback({'type': 'error', 'message': 'No tracks downloaded.'})
                return {'status': 'failed', 'error': 'No tracks downloaded', 'title': 'Spotify Download'}
        except Exception as e:
            if progress_callback:
                progress_callback({'type': 'error', 'message': str(e)})
            return {'status': 'failed', 'error': str(e), 'title': 'Spotify Download'}

    out_dir = Config.AUDIO_DIR if mode == 'audio' else Config.VIDEO_DIR
    os.makedirs(out_dir, exist_ok=True)

    def _hook(d):
        if not progress_callback:
            return
        st = d.get('status', '')
        if st == 'downloading':
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
        elif st == 'finished':
            progress_callback({
                'type': 'postprocessing',
                'filename': os.path.basename(d.get('filename', '')),
            })

    # Build yt-dlp options
    opts = {
        'outtmpl': os.path.join(out_dir, Config.OUTPUT_TEMPLATE),
        'progress_hooks': [_hook],
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'retries': 3,
        'socket_timeout': 15,
        'fragment_retries': 3,
        'overwrites': False,
        'windowsfilenames': True,
        'geo_bypass': True,
        'nocheckcertificate': True,
        'logger': _SilentLogger(),
    }

    if mode == 'audio':
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': audio_format,
            'preferredquality': '192',
        }]
        if Config.EMBED_THUMBNAIL:
            opts['writethumbnail'] = True
            opts['postprocessors'].append({'key': 'EmbedThumbnail'})
        if Config.EMBED_METADATA:
            opts['postprocessors'].append({'key': 'FFmpegMetadata', 'add_metadata': True})
    else:
        height_map = {'8k': 4320, '4k': 2160, '1440p': 1440, '1080p': 1080,
                      '720p': 720, '480p': 480, '360p': 360, 'best': 9999}
        h = height_map.get(quality.lower(), 1080)
        if h >= 9999:
            opts['format'] = 'bestvideo+bestaudio/best'
        else:
            opts['format'] = f'bestvideo[height<={h}]+bestaudio/best[height<={h}]/best'
        opts['merge_output_format'] = 'mp4'
        if Config.EMBED_THUMBNAIL:
            opts['writethumbnail'] = True
            opts.setdefault('postprocessors', []).append({'key': 'EmbedThumbnail'})
        if Config.EMBED_METADATA:
            opts.setdefault('postprocessors', []).append({'key': 'FFmpegMetadata', 'add_metadata': True})

    if subtitle_langs:
        opts['writesubtitles'] = True
        opts['writeautomaticsub'] = True
        opts['subtitleslangs'] = subtitle_langs

    # YouTube-specific
    is_yt = any(d in url.lower() for d in ['youtube.com', 'youtu.be', 'music.youtube.com'])
    if is_yt:
        opts['extractor_args'] = {
            'youtube': {'player_client': ['web_safari', 'android_vr', 'android', 'ios', 'web']}
        }

    if progress_callback:
        progress_callback({'type': 'status', 'message': 'Connecting...'})

    # Try with cookies then direct (cookie errors silenced)
    title = 'Unknown'
    strategies = []
    if is_yt:
        strategies = [(b,) for b in ['chrome', 'firefox', 'edge']]
    strategies.append(None)

    downloaded_ok = False
    for cookies in strategies:
        dl_opts = {**opts}
        if cookies:
            dl_opts['cookiesfrombrowser'] = cookies
        try:
            _cm = _suppress_stderr() if cookies else contextlib.nullcontext()
            with _cm:
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info:
                        title = info.get('title', 'Unknown')
                    if progress_callback:
                        progress_callback({'type': 'status', 'message': f'Downloading: {title[:60]}...'})
                    dl_opts['progress_hooks'] = [_hook]
                
                # Re-create with progress hooks for actual download
                with yt_dlp.YoutubeDL(dl_opts) as ydl:
                    ydl.download([url])
                    downloaded_ok = True
                    break
        except Exception as e:
            err_str = str(e).lower()
            if 'requested format' in err_str or 'no video' in err_str:
                # Fallback format
                dl_opts['format'] = 'bestvideo+bestaudio/best' if mode != 'audio' else 'bestaudio/best'
                try:
                    with yt_dlp.YoutubeDL(dl_opts) as ydl:
                        ydl.download([url])
                        downloaded_ok = True
                        break
                except Exception:
                    continue
            continue

    if not downloaded_ok:
        if progress_callback:
            progress_callback({'type': 'error', 'message': 'Download failed after all strategies.'})
        return {'status': 'failed', 'error': 'Download failed', 'title': title}

    # Find downloaded file
    exts = ('.mp3', '.m4a', '.opus', '.flac', '.ogg', '.wav') if mode == 'audio' else ('.mp4', '.mkv', '.webm', '.avi', '.mov')
    filepath = None
    try:
        for f in sorted(os.listdir(out_dir), key=lambda x: os.path.getmtime(os.path.join(out_dir, x)), reverse=True):
            if f.lower().endswith(exts):
                fp = os.path.join(out_dir, f)
                if time.time() - os.path.getmtime(fp) < 300:
                    filepath = fp
                    break
    except Exception:
        pass

    filesize = os.path.getsize(filepath) if filepath and os.path.exists(filepath) else 0

    # Save to database
    if db and filepath:
        try:
            db.add_download(
                url=url, platform='', title=title,
                filesize=filesize, filepath=filepath,
                media_type=mode, status='complete'
            )
        except Exception:
            pass

    result = {
        'status': 'complete',
        'filepath': filepath or out_dir,
        'filename': os.path.basename(filepath) if filepath else '',
        'filesize': filesize,
        'filesize_str': format_size(filesize),
        'title': title,
    }

    if progress_callback:
        progress_callback({'type': 'complete', **result})

    return result


# ============================================================================
# ROUTES
# ============================================================================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/info')
def api_info():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    result = get_video_info(url)
    if 'error' in result:
        return jsonify(result), 404
    return jsonify(result)


@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    job_id = str(uuid.uuid4())[:8]
    q = Queue()

    options = {
        'mode': data.get('mode', 'video'),
        'quality': data.get('quality', '1080p'),
        'audio_format': data.get('audio_format', 'mp3'),
        'subtitles': data.get('subtitles'),
        'custom_args': data.get('custom_args', ''),
    }

    def _worker():
        try:
            result = do_download(url, options, progress_callback=lambda e: q.put(e))
            q.put({'type': 'done', **result})
        except Exception as e:
            q.put({'type': 'error', 'message': str(e)[:200]})

    t = threading.Thread(target=_worker, daemon=True)
    with _jobs_lock:
        _jobs[job_id] = {'queue': q, 'thread': t, 'url': url, 'started': time.time()}
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/progress/<job_id>')
def api_progress(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    def generate():
        q = job['queue']
        while True:
            try:
                event = q.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get('type') in ('done', 'error', 'complete'):
                    with _jobs_lock:
                        _jobs.pop(job_id, None)
                    break
            except Exception:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/history')
def api_history():
    limit = request.args.get('limit', 50, type=int)
    if not db:
        return jsonify({'downloads': []})
    try:
        downloads = db.get_downloads(limit=limit)
        return jsonify({'downloads': [{
            'title': getattr(d, 'title', 'Unknown'),
            'platform': getattr(d, 'platform', ''),
            'url': getattr(d, 'url', ''),
            'filepath': getattr(d, 'filepath', ''),
            'filesize': getattr(d, 'filesize', 0) or 0,
            'filesize_str': format_size(getattr(d, 'filesize', 0) or 0),
            'status': getattr(d, 'status', 'unknown'),
            'media_type': getattr(d, 'media_type', 'video'),
            'created_at': str(getattr(d, 'download_date', '')),
        } for d in downloads]})
    except Exception:
        return jsonify({'downloads': []})


@app.route('/api/stats')
def api_stats():
    if not db:
        return jsonify({'total_downloads': 0, 'total_size_human': '0 B', 'failed_downloads': 0, 'by_platform': {}})
    try:
        return jsonify(db.get_statistics())
    except Exception:
        return jsonify({'total_downloads': 0, 'total_size_human': '0 B', 'failed_downloads': 0, 'by_platform': {}})


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    return jsonify({
        'output_template': Config.OUTPUT_TEMPLATE,
        'embed_thumbnail': Config.EMBED_THUMBNAIL,
        'embed_metadata': Config.EMBED_METADATA,
        'video_dir': Config.VIDEO_DIR,
        'audio_dir': Config.AUDIO_DIR,
        'yt_dlp_version': YTDLP_VERSION,
        'ffmpeg_available': check_ffmpeg(),
    })


@app.route('/api/settings', methods=['POST'])
def api_set_settings():
    data = request.get_json(force=True, silent=True) or {}
    if 'output_template' in data:
        Config.OUTPUT_TEMPLATE = data['output_template']
    if 'embed_thumbnail' in data:
        Config.EMBED_THUMBNAIL = bool(data['embed_thumbnail'])
    if 'embed_metadata' in data:
        Config.EMBED_METADATA = bool(data['embed_metadata'])
    try:
        Config.save_settings()
    except Exception:
        pass
    return jsonify({'status': 'ok'})


@app.route('/api/search/online')
def api_search_online():
    query = request.args.get('q', '').strip()
    search_type = request.args.get('type', 'all').strip()
    limit = request.args.get('limit', 20, type=int)
    if not query:
        return jsonify({'results': []})
    if not SEARCH_ENGINE_AVAILABLE:
        return jsonify({'error': 'Search engine module not available'}), 500
    try:
        results = MediaSearchEngine.search(query, search_type=search_type, limit=limit)
        return jsonify({'results': [r.to_dict() for r in results]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/inspect')
def api_inspect():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not SEARCH_ENGINE_AVAILABLE:
        return jsonify({'error': 'Search engine module not available'}), 500
    try:
        data = MediaSearchEngine.inspect_metadata(url)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/playlist/tracks')
def api_playlist_tracks():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not SEARCH_ENGINE_AVAILABLE:
        return jsonify({'error': 'Search engine module not available'}), 500
    try:
        tracks = MediaSearchEngine.fetch_playlist_tracks(url)
        return jsonify({'tracks': tracks, 'total': len(tracks)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/playlist/download-selected', methods=['POST'])
def api_download_selected():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get('items', [])
    quality = data.get('quality', 'flac')
    if not items:
        return jsonify({'error': 'No items selected'}), 400

    job_id = str(uuid.uuid4())[:8]
    q = Queue()

    def _worker():
        try:
            total = len(items)
            q.put({'type': 'status', 'message': f'Starting batch download of {total} track(s)...'})
            downloader = SpotiFLACDownloader() if SPOTIFLAC_AVAILABLE else None

            downloaded_count = 0
            for idx, item in enumerate(items, 1):
                track_url = item.get('url')
                track_title = item.get('title', f'Track {idx}')
                q.put({
                    'type': 'progress',
                    'percent': round((idx - 1) / total * 100, 1),
                    'status': 'downloading',
                    'message': f'[{idx}/{total}] Downloading: {track_title}...',
                })

                if downloader and SpotiFLACDownloader.is_spotify_url(track_url):
                    res = downloader.download_url(track_url, quality=quality)
                    if res:
                        downloaded_count += 1
                        if db:
                            for r in res:
                                try:
                                    db.add_download(
                                        url=track_url, platform='Spotify',
                                        title=r.get('title', track_title),
                                        filesize=r.get('filesize', 0),
                                        filepath=r.get('filepath', ''),
                                        media_type='audio', status='complete'
                                    )
                                except Exception:
                                    pass
                else:
                    # Fallback to standard yt-dlp download
                    dl_res = do_download(track_url, {'mode': 'audio', 'audio_format': 'flac' if quality == 'flac' else 'mp3'})
                    if dl_res.get('status') == 'complete':
                        downloaded_count += 1

            q.put({
                'type': 'complete',
                'title': f'{downloaded_count}/{total} Tracks Downloaded',
                'filesize': 0,
                'filesize_str': f'{downloaded_count} track(s)',
                'filepath': Config.MUSIC_DIR,
            })
            q.put({'type': 'done', 'status': 'complete', 'downloaded_count': downloaded_count})
        except Exception as e:
            q.put({'type': 'error', 'message': str(e)[:200]})

    t = threading.Thread(target=_worker, daemon=True)
    with _jobs_lock:
        _jobs[job_id] = {'queue': q, 'thread': t, 'url': 'batch-playlist', 'started': time.time()}
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/search')
def api_search():
    query = request.args.get('q', '').strip()
    if not query or not db:
        return jsonify({'results': []})
    try:
        results = db.search_downloads(query)
        return jsonify({'results': [{
            'title': getattr(r, 'title', 'Unknown'),
            'platform': getattr(r, 'platform', ''),
            'url': getattr(r, 'url', ''),
            'filesize_str': format_size(getattr(r, 'filesize', 0) or 0),
        } for r in results]})
    except Exception:
        return jsonify({'results': []})


# ============================================================================
# UNIVERSAL MEDIA DOWNLOADER & STREAM SNIFFER ROUTES
# ============================================================================

@app.route('/api/universal/status')
def api_universal_status():
    return jsonify({
        'available': UNIVERSAL_AVAILABLE,
        'playwright': PLAYWRIGHT_AVAILABLE,
        'ffmpeg': check_ffmpeg(),
        'default_dir': getattr(Config, 'UNKNOWN_DIR', os.path.join(Config.BASE_DIR, 'unknown')),
    })


@app.route('/api/universal/sniff', methods=['POST'])
def api_universal_sniff():
    if not UNIVERSAL_AVAILABLE:
        return jsonify({'error': 'Universal Downloader module not available on server.'}), 503
    data = request.get_json(force=True, silent=True) or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No webpage or stream URL provided.'}), 400

    timeout_s = int(data.get('timeout', 25))
    downloader = UniversalMediaDownloader()

    try:
        candidates = downloader.analyze_url(url, timeout_s=timeout_s)
        return jsonify({
            'success': True,
            'url': url,
            'count': len(candidates),
            'candidates': [c.to_dict() for c in candidates],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/universal/download', methods=['POST'])
def api_universal_download():
    if not UNIVERSAL_AVAILABLE:
        return jsonify({'error': 'Universal Downloader module not available.'}), 503
    data = request.get_json(force=True, silent=True) or {}
    stream_url = (data.get('url') or data.get('stream_url') or '').strip()
    if not stream_url:
        return jsonify({'error': 'Stream URL is required.'}), 400

    job_id = str(uuid.uuid4())[:8]
    q = Queue()

    def _uni_worker():
        try:
            downloader = UniversalMediaDownloader()
            from universal_downloader import StreamCandidate, SubtitleTrackOption
            subtitles = []
            for s in data.get('subtitles_info', []):
                subtitles.append(SubtitleTrackOption(
                    language=s.get('language', 'und'),
                    name=s.get('name', ''),
                    url=s.get('url', ''),
                    ext=s.get('ext', 'vtt'),
                    is_external=s.get('is_external', False),
                    headers=data.get('headers', {})
                ))

            candidate = StreamCandidate(
                url=stream_url,
                type=data.get('type', 'hls'),
                headers=data.get('headers', {}),
                title=data.get('title', 'Unknown Media Stream'),
                page_url=data.get('page_url', stream_url),
                is_drm=data.get('is_drm', False),
                drm_notice=data.get('drm_notice', ''),
                duration_s=float(data.get('duration_s', 0.0)),
                thumbnail=data.get('thumbnail', ''),
                subtitles=subtitles,
                local_manifest_path=data.get('local_manifest_path', ''),
            )

            res_val = data.get('resolution')
            sel_res = int(res_val) if (res_val and str(res_val).isdigit()) else None

            def _uni_prog(p):
                status = p.get('status')
                pct = p.get('percent', 0.0)
                if status == 'downloading':
                    q.put({
                        'type': 'progress',
                        'percent': pct,
                        'speed_str': p.get('speed_str', ''),
                        'downloaded_str': p.get('downloaded_str', ''),
                        'total_str': p.get('total_str', ''),
                        'eta_str': p.get('eta_str', ''),
                    })
                elif status == 'muxing':
                    q.put({
                        'type': 'progress',
                        'percent': pct,
                        'speed_str': 'FFmpeg muxing...',
                        'downloaded_str': p.get('message', 'Muxing tracks...'),
                        'total_str': 'Muxing',
                        'eta_str': '--:--',
                    })

            success, res = downloader.download_candidate(
                candidate=candidate,
                selected_resolution=sel_res,
                selected_format_id=data.get('format_id'),
                selected_audio_id=data.get('audio_id'),
                audio_only=bool(data.get('audio_only', False)),
                selected_subtitles=data.get('selected_subtitles'),
                output_format=data.get('output_format', 'mkv'),
                progress_cb=_uni_prog
            )

            if success and os.path.exists(res):
                filesize = os.path.getsize(res)
                q.put({
                    'type': 'done',
                    'status': 'complete',
                    'filepath': res,
                    'filesize': filesize,
                    'filesize_str': format_size(filesize),
                    'title': os.path.basename(res),
                })
            else:
                q.put({'type': 'error', 'message': res or 'Universal download failed'})
        except Exception as e:
            q.put({'type': 'error', 'message': str(e)})

    t = threading.Thread(target=_uni_worker, daemon=True)
    with _jobs_lock:
        _jobs[job_id] = {'queue': q, 'thread': t, 'url': stream_url, 'started': time.time()}
    t.start()
    return jsonify({'job_id': job_id})


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    print("\n  ==========================================")
    print("       VAULT — Media Downloader Web UI")
    print(f"    yt-dlp: {'✓ ' + YTDLP_VERSION if YTDLP_OK else '✗ NOT FOUND':>24}")
    print(f"    FFmpeg: {'✓ Available' if check_ffmpeg() else '✗ Not found':>24}")
    print("          http://localhost:5000")
    print("  ==========================================\n")

    if not YTDLP_OK:
        print("  [!] yt-dlp not found. Install: pip install yt-dlp")
        print("  [!] Downloads will NOT work without yt-dlp.\n")

    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
