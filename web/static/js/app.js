/* ============================================================================
   VAULT — Frontend Logic
   Handles URL fetching, format selection, SSE progress, history
   ============================================================================ */

// ---- State ----
let currentInfo = null;
let currentMode = 'video';
let currentJobId = null;
let eventSource = null;

// ---- DOM Refs ----
const $ = id => document.getElementById(id);

// ---- Init ----
document.addEventListener('DOMContentLoaded', () => {
    loadStats();
    loadHistory();
    setupPasteListener();
    setupKeyboardShortcuts();
    // loadCrunchyrollStatus();
});

// ============================================================================
// URL FETCHING
// ============================================================================

async function fetchInfo() {
    const url = $('urlInput').value.trim();
    if (!url) return;

    // Reset UI
    hideError();
    showFetching(true);
    $('mainCard').style.display = 'none';
    $('progressSection').style.display = 'none';
    $('completeSection').style.display = 'none';

    try {
        const resp = await fetch(`/api/info?url=${encodeURIComponent(url)}`);
        const data = await resp.json();

        if (!resp.ok || data.error) {
            showError(data.error || 'Could not fetch info');
            showFetching(false);
            return;
        }

        currentInfo = data;
        renderFormats(data);
        
        $('titleText').textContent = data.title || 'Unknown Title';
        
        // Thumbnail
        const thumbEl = $('videoThumb');
        if (thumbEl) {
            thumbEl.src = data.thumbnail || 'https://via.placeholder.com/800x450.png?text=No+Thumbnail';
        }

        $('mainCard').style.display = 'block';

        // Update footer info
        const verResp = await fetch('/api/settings');
        if (verResp.ok) {
            const verData = await verResp.json();
            if (verData.yt_dlp_version) {
                $('footerInfo').textContent = `Powered by yt-dlp ${verData.yt_dlp_version}`;
            }
        }

    } catch (err) {
        showError('Network error. Is the server running?');
    }

    showFetching(false);
}

function renderFormats(info) {
    // 1. Video Formats
    const videoSelect = $('videoFormatSelect');
    videoSelect.innerHTML = '';
    
    const vFormats = info.video_formats || [];
    if (vFormats.length > 0) {
        vFormats.forEach(f => {
            const opt = document.createElement('option');
            opt.value = f.label.toLowerCase(); // e.g. "1080p", "4k"
            
            const codec = f.vcodec ? ` | ${f.vcodec}` : '';
            const size = f.filesize_str ? ` | ${f.filesize_str}` : '';
            const fps = f.fps ? `${f.fps}fps ` : '';
            
            opt.textContent = `${f.label} ${fps}${codec}${size}`;
            videoSelect.appendChild(opt);
        });
    } else {
        // Fallback
        ['1080p', '720p', '480p'].forEach(q => {
            const opt = document.createElement('option');
            opt.value = q;
            opt.textContent = q;
            videoSelect.appendChild(opt);
        });
    }
    // Add Best
    const bestOpt = document.createElement('option');
    bestOpt.value = 'best';
    bestOpt.textContent = 'Best Quality';
    videoSelect.appendChild(bestOpt);

    if (info.is_music) {
        if ($('modeVideo')) $('modeVideo').style.display = 'none';
        if ($('modeAudio')) $('modeAudio').style.display = 'block';
        setMode('audio');

        const audioExtract = $('audioExtractFormat');
        if (audioExtract) {
            audioExtract.innerHTML = `
                <option value="flac" selected>FLAC (Lossless Audio - Synced Lyrics & Art)</option>
                <option value="mp3">MP3 (320 kbps Extreme Quality)</option>
            `;
        }
    } else {
        if ($('modeVideo')) $('modeVideo').style.display = 'block';
        if ($('modeAudio')) $('modeAudio').style.display = 'block';
        setMode('video');

        const audioExtract = $('audioExtractFormat');
        if (audioExtract) {
            audioExtract.innerHTML = `
                <option value="mp3" selected>MP3</option>
                <option value="m4a">M4A</option>
                <option value="opus">OPUS</option>
                <option value="flac">FLAC</option>
                <option value="wav">WAV</option>
            `;
        }
    }

    // 3. Subtitles
    const subs = info.subtitles || {};
    const manualSubs = subs.manual || [];
    const autoCount = subs.auto_count || 0;
    
    if (manualSubs.length > 0 || autoCount > 0) {
        $('subtitleRow').style.display = 'flex';
        const subLangSelect = $('subtitleLang');
        subLangSelect.innerHTML = '';
        
        manualSubs.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.code;
            opt.textContent = s.name;
            subLangSelect.appendChild(opt);
        });
        
        $('subtitleCheck').onchange = (e) => {
            subLangSelect.style.display = e.target.checked && manualSubs.length > 0 ? 'block' : 'none';
        };
        $('subtitleCheck').checked = false;
        subLangSelect.style.display = 'none';
    } else {
        $('subtitleRow').style.display = 'none';
    }
}

// ---- Mode Toggle ----
function setMode(mode) {
    currentMode = mode;
    $('modeVideo').classList.toggle('active', mode === 'video');
    $('modeAudio').classList.toggle('active', mode === 'audio');
    
    // Toggle active tab styling (ytDownloader style)
    if (mode === 'video') {
        $('modeVideo').style.background = '#60a5fa';
        $('modeAudio').style.background = 'transparent';
    } else {
        $('modeAudio').style.background = '#60a5fa';
        $('modeVideo').style.background = 'transparent';
    }

    $('videoSection').style.display = mode === 'video' ? 'block' : 'none';
    $('audioSection').style.display = mode === 'audio' ? 'block' : 'none';
}

function toggleMoreOptions() {
    const el = $('moreOptions');
    if (el.style.display === 'none') {
        el.style.display = 'block';
        $('btnMoreOpts').textContent = 'Hide options';
    } else {
        el.style.display = 'none';
        $('btnMoreOpts').textContent = 'More options';
    }
}

// ============================================================================
// DOWNLOADING
// ============================================================================

async function startDownload() {
    if (!currentInfo) return;

    const url = currentInfo.url || $('urlInput').value.trim();
    if (!url) return;

    hideError();
    $('mainCard').style.display = 'none';
    $('completeSection').style.display = 'none';
    $('progressSection').style.display = 'block';

    $('btnDownload').disabled = true;
    $('progressBarFill').style.width = '0%';
    $('progPercent').textContent = '0%';
    $('progSpeed').textContent = '---';
    $('progSize').textContent = '--- / ---';
    $('progEta').textContent = 'ETA: ---';
    $('progressStatus').textContent = 'Starting download...';

    const body = {
        url: url,
        mode: currentMode,
        quality: currentMode === 'video' ? $('videoFormatSelect').value : 'best',
        audio_format: $('audioExtractFormat').value,
        custom_args: $('customArgs').value,
    };

    if ($('subtitleCheck') && $('subtitleCheck').checked) {
        const lang = $('subtitleLang').value;
        body.subtitles = lang ? [lang] : ['en'];
    }

    try {
        const resp = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        const data = await resp.json();

        if (!resp.ok || data.error) {
            showError(data.error || 'Download failed');
            $('progressSection').style.display = 'none';
            $('mainCard').style.display = 'block';
            $('btnDownload').disabled = false;
            return;
        }

        currentJobId = data.job_id;
        connectProgress(data.job_id);

    } catch (err) {
        showError('Failed to start download');
        $('progressSection').style.display = 'none';
        $('mainCard').style.display = 'block';
        $('btnDownload').disabled = false;
    }
}

function connectProgress(jobId) {
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource(`/api/progress/${jobId}`);

    eventSource.onmessage = (e) => {
        try {
            const data = JSON.parse(e.data);
            handleProgressEvent(data);
        } catch (err) {
            console.warn('Progress parse error', err);
        }
    };

    eventSource.onerror = () => {
        eventSource.close();
        eventSource = null;
        if ($('progressSection').style.display !== 'none') {
            $('progressStatus').textContent = 'Connection lost. Download may still be running...';
        }
    };
}

function handleProgressEvent(data) {
    switch (data.type) {
        case 'progress':
            $('progressBarFill').style.width = data.percent + '%';
            $('progPercent').textContent = data.percent + '%';
            $('progSpeed').textContent = data.speed_str || '---';
            $('progSize').textContent = `${data.downloaded_str || '---'} / ${data.total_str || '---'}`;
            $('progEta').textContent = data.eta > 0 ? `ETA: ${formatDuration(data.eta)}` : 'ETA: ---';
            $('progressStatus').textContent = 'Downloading...';
            break;

        case 'postprocessing':
            $('progressBarFill').style.width = '100%';
            $('progPercent').textContent = '100%';
            $('progressStatus').textContent = '⚙ Processing...';
            $('progSpeed').textContent = '';
            $('progEta').textContent = '';
            break;

        case 'status':
            $('progressStatus').textContent = data.message;
            break;

        case 'complete':
        case 'done':
            if (eventSource) { eventSource.close(); eventSource = null; }
            $('progressSection').style.display = 'none';
            
            if (data.status === 'failed' || data.error) {
                $('mainCard').style.display = 'block';
                $('btnDownload').disabled = false;
                showError(data.error || data.message || 'Download failed');
                showToast('❌ Download failed', 'error');
            } else {
                showComplete(data);
                loadHistory();
                loadStats();
                showToast(`✅ ${(data.title || 'Download')} complete!`, 'success');
            }
            break;

        case 'error':
            if (eventSource) { eventSource.close(); eventSource = null; }
            $('progressSection').style.display = 'none';
            $('mainCard').style.display = 'block';
            $('btnDownload').disabled = false;
            showError(data.message || 'Download failed');
            showToast('❌ Download failed', 'error');
            break;
    }
}

function showComplete(data) {
    $('completeTitle').textContent = data.title ? `${data.title}` : 'Download Complete!';
    const details = [];
    if (data.filesize_str) details.push(data.filesize_str);
    if (data.filename) details.push(data.filename);
    $('completeDetail').textContent = details.join(' · ');
    $('completeSection').style.display = 'block';
}


// ============================================================================
// HISTORY & STATS
// ============================================================================

async function loadHistory() {
    try {
        const resp = await fetch('/api/history?limit=15');
        const data = await resp.json();
        const downloads = data.downloads || [];

        const list = $('historyList');
        $('historyCount').textContent = downloads.length > 0 ? `${downloads.length} recent` : '';

        if (downloads.length === 0) {
            list.innerHTML = '<div class="history-empty">No downloads yet. Paste a URL above to get started!</div>';
            return;
        }

        list.innerHTML = '';
        downloads.forEach(d => {
            const item = document.createElement('div');
            item.className = `flex items-center gap-4 bg-surface-container-low border border-outline-variant p-3 group hover:bg-surface-container transition-colors ${d.status !== 'complete' ? 'opacity-75' : ''}`;

            const isVideo = d.media_type !== 'audio';
            const icon = isVideo ? 'video_file' : 'audio_file';
            const title = d.title || 'Unknown';
            const size = d.status === 'complete' ? (d.filesize_str || '') : 'FAILED';
            const sizeClass = d.status === 'complete' ? 'text-outline' : 'text-error';
            const barClass = d.status === 'complete' ? 'bg-primary w-full' : 'bg-error w-[45%] opacity-50';

            item.innerHTML = `
                <div class="w-10 h-10 rounded bg-slate-800 flex items-center justify-center ${d.status === 'complete' ? 'text-primary' : 'text-outline'}">
                    <span class="material-symbols-outlined" data-icon="${icon}">${icon}</span>
                </div>
                <div class="flex-1 min-w-0">
                    <div class="flex justify-between items-start mb-1">
                        <h4 class="font-body-base font-medium truncate pr-4 text-white">${escHtml(title)}</h4>
                        <span class="text-xs font-mono-data ${sizeClass} whitespace-nowrap">${size}</span>
                    </div>
                    <div class="w-full bg-surface-container-highest h-1 rounded-full overflow-hidden">
                        <div class="${barClass} h-full transition-all"></div>
                    </div>
                </div>
                <div class="flex gap-2">
                    <button class="p-2 text-outline hover:text-primary transition-colors cursor-pointer" title="Re-download">
                        <span class="material-symbols-outlined" data-icon="replay">replay</span>
                    </button>
                </div>
            `;

            // Click to re-download on the button
            const btn = item.querySelector('button');
            if (btn) {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (d.url) {
                        $('urlInput').value = d.url;
                        fetchInfo();
                    }
                });
            }

            list.appendChild(item);
        });

    } catch (err) {
        // Silent fail
    }
}

async function loadStats() {
    try {
        const resp = await fetch('/api/stats');
        const data = await resp.json();

        $('statTotal').textContent = data.total_downloads || 0;
        $('statSize').textContent = data.total_size_human || '0 B';

        const platforms = Object.keys(data.by_platform || {}).length;
        $('statPlatforms').textContent = platforms || 0;

    } catch (err) {
        // Silent fail
    }
}


// ============================================================================
// UI HELPERS
// ============================================================================

function showFetching(show) {
    const btn = $('btnFetch');
    if (show) {
        btn.disabled = true;
        $('loadingSection').style.display = 'flex';
    } else {
        btn.disabled = false;
        $('loadingSection').style.display = 'none';
    }
}

function showError(msg) {
    const el = $('errorMsg');
    el.textContent = msg;
    el.classList.add('active');
}

function hideError() {
    $('errorMsg').classList.remove('active');
}

function resetUI() {
    $('mainCard').style.display = 'none';
    $('progressSection').style.display = 'none';
    $('completeSection').style.display = 'none';
    hideError();
    $('urlInput').value = '';
    $('urlInput').focus();
    $('btnDownload').disabled = false;
    currentInfo = null;
    currentJobId = null;
}

function showToast(message, type = 'success') {
    const toast = $('toast');
    toast.textContent = message;
    toast.className = `toast toast-${type} show`;
    setTimeout(() => toast.classList.remove('show'), 4000);
}

// ---- Paste from Clipboard ----
async function pasteFromClipboard() {
    try {
        const text = await navigator.clipboard.readText();
        if (text) {
            $('urlInput').value = text;
            fetchInfo();
        }
    } catch (err) {
        showError('Could not read clipboard. Paste manually.');
    }
}

// ---- Paste Listener ----
function setupPasteListener() {
    const input = $('urlInput');

    input.addEventListener('paste', (e) => {
        setTimeout(() => {
            const val = input.value.trim();
            if (val && (val.startsWith('http://') || val.startsWith('https://'))) {
                fetchInfo();
            }
        }, 100);
    });

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            fetchInfo();
        }
    });
}

function setupKeyboardShortcuts() {
    document.addEventListener('keydown', (e) => {
        // Ctrl+V
        if (e.ctrlKey && e.key === 'v' && document.activeElement !== $('urlInput')) {
            $('urlInput').focus();
        }
        if (e.key === 'Escape') {
            resetUI();
        }
    });
}


// ============================================================================
// UTILITIES
// ============================================================================

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '0:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    return `${m}:${String(s).padStart(2, '0')}`;
}

function formatDate(dateStr) {
    try {
        const d = new Date(dateStr);
        if (isNaN(d.getTime())) return '';
        const now = new Date();
        const diff = now - d;
        const mins = Math.floor(diff / 60000);
        if (mins < 1) return 'just now';
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        const days = Math.floor(hrs / 24);
        if (days < 7) return `${days}d ago`;
        return d.toLocaleDateString();
    } catch {
        return '';
    }
}

function escHtml(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
}

// ============================================================================
// SEARCH ENGINE & PLAYLIST EXPLORER
// ============================================================================

let currentMainTab = 'url';
let currentSearchFilter = 'all';
let currentPlaylistTracks = [];

function switchMainTab(tab) {
    currentMainTab = tab;
    


    // Toggle container views
    const containers = {
        'url': $('urlModeContainer'),
        'search': $('searchModeContainer'),
        
        'universal': $('universalModeContainer')
    };
    
    Object.keys(containers).forEach(k => {
        if (containers[k]) {
            if (k === tab) {
                containers[k].classList.remove('hidden');
                containers[k].style.display = 'block';
            } else {
                containers[k].classList.add('hidden');
                containers[k].style.display = 'none';
            }
        }
    });

    // Update top tab buttons
    const tabBtns = {
        'url': $('tabUrlMode'),
        'search': $('tabSearchMode'),
        
        'universal': $('tabUniversalMode')
    };

    const activeTopClass = 'px-5 py-2.5 rounded font-bold text-sm bg-primary text-on-primary shadow flex items-center gap-2 transition-all';
    const inactiveTopClass = 'px-5 py-2.5 rounded font-bold text-sm bg-surface-container-highest text-outline hover:text-white flex items-center gap-2 transition-all';

    Object.keys(tabBtns).forEach(k => {
        if (tabBtns[k]) {
            tabBtns[k].className = (k === tab) ? activeTopClass : inactiveTopClass;
        }
    });

    // Update sidebar nav items
    const navItems = {
        'url': $('navDirectLink'),
        'search': $('navSearch'),
        
        'universal': $('navUniversal')
    };

    const activeNavClass = 'w-full text-left bg-slate-800/50 text-emerald-500 border-l-4 border-emerald-500 px-4 py-3 flex items-center gap-3 rounded-r transition-all';
    const inactiveNavClass = 'w-full text-left text-outline hover:text-white hover:bg-slate-800/30 px-4 py-3 flex items-center gap-3 rounded-r transition-all';

    Object.keys(navItems).forEach(k => {
        if (navItems[k]) {
            navItems[k].className = (k === tab) ? activeNavClass : inactiveNavClass;
        }
    });

    // Mode-specific triggers
    if (tab === 'search') {
        setTimeout(() => $('searchInput')?.focus(), 50);


    } else if (tab === 'universal') {
        setTimeout(() => $('uniUrlInput')?.focus(), 50);
    }
}

function setSearchFilter(filter) {
    currentSearchFilter = filter;
    ['filterAll', 'filterMusic', 'filterVideo'].forEach(id => {
        const btn = $(id);
        if (btn) {
            btn.className = 'px-3 py-1.5 rounded font-bold text-outline hover:text-white transition-all';
        }
    });
    if (filter === 'all') $('filterAll').className = 'px-3 py-1.5 rounded font-bold bg-primary text-on-primary transition-all';
    else if (filter === 'music') $('filterMusic').className = 'px-3 py-1.5 rounded font-bold bg-primary text-on-primary transition-all';
    else if (filter === 'video') $('filterVideo').className = 'px-3 py-1.5 rounded font-bold bg-primary text-on-primary transition-all';
}

async function executeSearch() {
    const q = $('searchInput').value.trim();
    if (!q) return;

    const grid = $('searchResultsGrid');
    const sec = $('searchResultsSection');
    sec.style.display = 'block';
    grid.innerHTML = `
        <div class="col-span-full py-12 flex flex-col items-center justify-center gap-3 text-outline">
            <span class="material-symbols-outlined text-4xl animate-spin text-primary" data-icon="autorenew">autorenew</span>
            <span>Searching Spotify, LRCLIB & YouTube...</span>
        </div>
    `;

    try {
        const resp = await fetch(`/api/search/online?q=${encodeURIComponent(q)}&type=${currentSearchFilter}&limit=24`);
        const data = await resp.json();
        const results = data.results || [];

        $('searchResultsCount').textContent = `Found ${results.length} result(s) for "${q}"`;

        if (results.length === 0) {
            grid.innerHTML = `<div class="col-span-full text-center py-12 text-outline">No results found for "${escHtml(q)}". Try different keywords.</div>`;
            return;
        }

        grid.innerHTML = '';
        results.forEach((item) => {
            const isCollection = item.media_type === 'album' || item.media_type === 'playlist';
            const card = document.createElement('div');
            card.className = 'bg-surface-container rounded-lg border border-outline-variant p-4 flex flex-col justify-between space-y-3 hover:border-primary/50 transition-all group';
            
            const thumbUrl = item.thumbnail || 'https://via.placeholder.com/400x400.png?text=Cover+Art';
            const typeBadge = item.media_type.toUpperCase();
            const lyricsBadge = item.has_synced_lyrics ? `<span class="bg-emerald-950 text-emerald-400 border border-emerald-800 text-[10px] px-2 py-0.5 rounded font-bold flex items-center gap-1">🎵 Synced Lyrics</span>` : '';
            
            const safeUrl = encodeURIComponent(item.url);
            const safeTitle = escHtml(item.title).replace(/'/g, "\\'");
            
            card.innerHTML = `
                <div class="flex gap-3">
                    <img class="w-16 h-16 rounded object-cover border border-outline-variant bg-surface-container-lowest shrink-0" src="${thumbUrl}" alt="Thumb"/>
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-1.5 mb-1">
                            <span class="text-[10px] bg-surface-container-highest px-1.5 py-0.5 rounded font-bold text-outline">${typeBadge}</span>
                            ${lyricsBadge}
                        </div>
                        <h4 class="font-bold text-sm text-white line-clamp-1 group-hover:text-primary transition-colors" title="${escHtml(item.title)}">${escHtml(item.title)}</h4>
                        <p class="text-xs text-outline line-clamp-1">${escHtml(item.artist || 'Unknown')}</p>
                        <p class="text-[11px] text-outline/70 font-mono-data mt-0.5">${item.duration_str || ''} • ${item.platform}</p>
                    </div>
                </div>
                <div class="flex gap-2 pt-2 border-t border-outline-variant/50">
                    <button onclick="inspectMedia('${safeUrl}')" class="flex-1 bg-surface-container-highest hover:bg-surface-bright text-xs py-2 rounded text-white font-medium flex items-center justify-center gap-1 transition-colors">
                        <span class="material-symbols-outlined text-xs" data-icon="info">info</span>
                        Inspect
                    </button>
                    ${isCollection ? `
                        <button onclick="openPlaylistExplorer('${safeUrl}', '${safeTitle}', '${thumbUrl}', '${item.media_type}')" class="flex-1 bg-primary text-on-primary text-xs py-2 rounded font-bold hover:brightness-110 flex items-center justify-center gap-1 transition-colors">
                            <span class="material-symbols-outlined text-xs" data-icon="playlist_play">playlist_play</span>
                            Explore Tracks
                        </button>
                    ` : `
                        <button onclick="downloadFromSearch('${safeUrl}', 'flac')" class="flex-1 bg-primary text-on-primary text-xs py-2 rounded font-bold hover:brightness-110 flex items-center justify-center gap-1 transition-colors">
                            <span class="material-symbols-outlined text-xs" data-icon="download">download</span>
                            Download
                        </button>
                    `}
                </div>
            `;
            grid.appendChild(card);
        });

    } catch (err) {
        grid.innerHTML = `<div class="col-span-full text-center py-12 text-error">Search failed: ${err.message}</div>`;
    }
}

async function inspectMedia(encodedUrl) {
    const url = decodeURIComponent(encodedUrl);
    const modal = $('inspectModal');
    const content = $('inspectModalContent');
    modal.classList.remove('hidden');
    content.innerHTML = `
        <div class="py-12 flex flex-col items-center justify-center gap-3 text-outline">
            <span class="material-symbols-outlined text-4xl animate-spin text-primary" data-icon="autorenew">autorenew</span>
            <span>Inspecting metadata, lyrics & subtitles...</span>
        </div>
    `;

    try {
        const resp = await fetch(`/api/inspect?url=${encodeURIComponent(url)}`);
        const data = await resp.json();

        const syncedBadge = data.has_synced_lyrics ? `<span class="text-emerald-400 font-bold">✔ Available (.lrc synchronized format)</span>` : (data.has_plain_lyrics ? `<span class="text-amber-400 font-bold">✔ Plain lyrics available</span>` : `<span class="text-outline">○ Not found in database</span>`);
        const subsList = (data.subtitles && data.subtitles.length > 0) ? data.subtitles.join(', ') : 'None detected';
        const vFmts = (data.video_formats && data.video_formats.length > 0) ? data.video_formats.join(', ') : 'Audio Stream';

        content.innerHTML = `
            <div class="flex gap-4 items-start">
                ${data.thumbnail ? `<img src="${data.thumbnail}" class="w-24 h-24 rounded-lg object-cover border border-outline-variant bg-surface-container-lowest" alt="Cover"/>` : ''}
                <div class="flex-1 min-w-0">
                    <span class="text-xs bg-surface-container-highest px-2 py-0.5 rounded text-primary font-bold uppercase">${data.platform} • ${data.media_type}</span>
                    <h3 class="text-lg font-bold text-white mt-1">${escHtml(data.title)}</h3>
                    <p class="text-sm text-outline">${escHtml(data.artist || 'Unknown')}</p>
                    ${data.album ? `<p class="text-xs text-outline/80 mt-0.5">Album: ${escHtml(data.album)}</p>` : ''}
                    <p class="text-xs text-outline/70 font-mono-data mt-1">Duration: ${data.duration_str} • Year: ${data.release_year || '---'}</p>
                </div>
            </div>

            <div class="p-4 bg-surface-container-lowest rounded-lg border border-outline-variant space-y-2 text-xs">
                <div><span class="text-outline font-bold">Synchronized Lyrics:</span> ${syncedBadge}</div>
                <div><span class="text-outline font-bold">Subtitles:</span> <span class="text-white">${escHtml(subsList)}</span></div>
                <div><span class="text-outline font-bold">Available Formats:</span> <span class="text-white font-mono-data">${escHtml(vFmts)}</span></div>
            </div>

            ${data.lyrics_preview ? `
                <div class="space-y-1.5">
                    <label class="text-xs font-bold text-outline uppercase tracking-wider">📜 Lyrics Preview</label>
                    <div class="p-3 bg-surface-container-lowest rounded border border-outline-variant font-mono-data text-xs text-emerald-300/90 whitespace-pre-line leading-relaxed max-h-32 overflow-y-auto">
                        ${escHtml(data.lyrics_preview)}
                    </div>
                </div>
            ` : ''}

            <div class="flex gap-3 pt-2">
                <button onclick="downloadFromSearch('${encodeURIComponent(url)}', 'flac'); closeInspectModal();" class="flex-1 bg-primary text-on-primary py-3 rounded font-bold hover:brightness-110 flex items-center justify-center gap-2">
                    <span class="material-symbols-outlined text-sm" data-icon="download">download</span>
                    Download Lossless FLAC
                </button>
                <button onclick="downloadFromSearch('${encodeURIComponent(url)}', '320k'); closeInspectModal();" class="flex-1 bg-surface-container-highest text-white py-3 rounded font-bold hover:brightness-110 flex items-center justify-center gap-2">
                    <span class="material-symbols-outlined text-sm" data-icon="headphones">headphones</span>
                    Download 320k MP3
                </button>
            </div>
        `;
    } catch (err) {
        content.innerHTML = `<div class="text-center py-8 text-error">Inspection failed: ${err.message}</div>`;
    }
}

function closeInspectModal() {
    $('inspectModal').classList.add('hidden');
}

async function openPlaylistExplorer(encodedUrl, title, thumbUrl, type) {
    const url = decodeURIComponent(encodedUrl);
    const modal = $('playlistModal');
    modal.classList.remove('hidden');

    $('playlistModalTitle').textContent = decodeURIComponent(title);
    $('playlistModalThumb').src = thumbUrl;
    $('playlistModalType').textContent = (type || 'COLLECTION') + ' EXPLORER';
    $('playlistModalSubtitle').textContent = 'Fetching tracks...';

    const list = $('playlistTrackList');
    list.innerHTML = `
        <div class="py-12 flex flex-col items-center justify-center gap-3 text-outline">
            <span class="material-symbols-outlined text-4xl animate-spin text-primary" data-icon="autorenew">autorenew</span>
            <span>Loading collection tracklist...</span>
        </div>
    `;

    try {
        const resp = await fetch(`/api/playlist/tracks?url=${encodeURIComponent(url)}`);
        const data = await resp.json();
        currentPlaylistTracks = data.tracks || [];

        $('playlistModalSubtitle').textContent = `${currentPlaylistTracks.length} tracks in collection`;
        renderPlaylistTracklist();
    } catch (err) {
        list.innerHTML = `<div class="text-center py-8 text-error">Failed to load tracks: ${err.message}</div>`;
    }
}

function renderPlaylistTracklist() {
    const list = $('playlistTrackList');
    list.innerHTML = '';

    currentPlaylistTracks.forEach((t, i) => {
        const row = document.createElement('label');
        row.className = 'flex items-center gap-3 p-3 rounded-lg hover:bg-surface-container-highest/50 cursor-pointer border border-transparent hover:border-outline-variant transition-all select-none';
        row.innerHTML = `
            <input type="checkbox" data-index="${i}" checked onchange="updatePlaylistSelectionCount()" class="track-checkbox rounded bg-surface-container-lowest border-outline text-primary focus:ring-primary w-4 h-4"/>
            <span class="text-xs font-mono-data text-outline w-6">${t.index || (i+1)}.</span>
            <div class="flex-1 min-w-0">
                <p class="text-sm font-bold text-white line-clamp-1">${escHtml(t.title)}</p>
                <p class="text-xs text-outline line-clamp-1">${escHtml(t.artist || '')}</p>
            </div>
            <span class="text-xs font-mono-data text-outline/80">${t.duration_str || ''}</span>
            <button type="button" onclick="event.stopPropagation(); inspectMedia('${encodeURIComponent(t.url)}')" class="p-1 rounded text-outline hover:text-white" title="Inspect">
                <span class="material-symbols-outlined text-sm" data-icon="info">info</span>
            </button>
        `;
        list.appendChild(row);
    });

    updatePlaylistSelectionCount();
}

function selectAllPlaylistTracks(state) {
    const boxes = document.querySelectorAll('.track-checkbox');
    boxes.forEach(b => b.checked = state);
    updatePlaylistSelectionCount();
}

function updatePlaylistSelectionCount() {
    const selected = document.querySelectorAll('.track-checkbox:checked').length;
    const total = currentPlaylistTracks.length;
    $('playlistSelectionCount').textContent = `${selected}/${total} selected`;
    $('btnDownloadSelected').disabled = (selected === 0);
}

function closePlaylistModal() {
    $('playlistModal').classList.add('hidden');
}

async function downloadSelectedPlaylistTracks() {
    const boxes = document.querySelectorAll('.track-checkbox:checked');
    const items = [];
    boxes.forEach(b => {
        const idx = parseInt(b.getAttribute('data-index'));
        if (currentPlaylistTracks[idx]) {
            items.push(currentPlaylistTracks[idx]);
        }
    });

    if (items.length === 0) return;

    const quality = $('playlistDownloadQuality').value;
    closePlaylistModal();

    // Switch to progress screen
    $('urlModeContainer').style.display = 'none';
    $('searchModeContainer').style.display = 'none';
    $('mainCard').style.display = 'none';
    $('completeSection').style.display = 'none';
    $('progressSection').style.display = 'block';

    $('progressBarFill').style.width = '0%';
    $('progPercent').textContent = '0%';
    $('progressStatus').textContent = `Starting batch download of ${items.length} track(s)...`;

    try {
        const resp = await fetch('/api/playlist/download-selected', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items: items, quality: quality }),
        });
        const data = await resp.json();
        if (data.job_id) {
            connectProgress(data.job_id);
        } else {
            showError(data.error || 'Failed to start batch download');
        }
    } catch (err) {
        showError('Batch download failed: ' + err.message);
    }
}

function downloadFromSearch(encodedUrl, quality) {
    const url = decodeURIComponent(encodedUrl);
    $('urlInput').value = url;
    switchMainTab('url');
    fetchInfo();
}

// Set initial mode
setMode('video');

function copyToClipboard(text, msg) {
    navigator.clipboard.writeText(text).then(() => {
        showToast(msg || 'Copied to clipboard!', 'success');
    }).catch(() => {
        showToast('Could not copy automatically. Select and copy text manually.', 'error');
    });
}


/* ============================================================================
   UNIVERSAL MEDIA DOWNLOADER & STREAM SNIFFER (FRONTEND CONTROLLER)
   ============================================================================ */

let currentUniCandidates = [];
let currentUniIndex = 0;
let currentUniSelectedQuality = null; // height number or null for best
let currentUniSelectedAudioId = null;
let currentUniAudioOnly = false;
let currentUniJobId = null;
let currentUniEventSource = null;

async function pasteToUniversalInput() {
    try {
        const text = await navigator.clipboard.readText();
        if (text) {
            $('uniUrlInput').value = text.trim();
            showToast('Pasted URL from clipboard', 'success');
            sniffUniversal();
        }
    } catch (e) {
        showToast('Please press Ctrl+V to paste', 'error');
    }
}

function resetUniversalUI() {
    $('uniScanningBox').classList.add('hidden');
    $('uniResultsCard').classList.add('hidden');
    $('uniProgressSection').classList.add('hidden');
    $('uniCompleteSection').classList.add('hidden');
    $('uniErrorMsg').classList.add('hidden');
    $('uniUrlInput').value = '';
    $('btnUniSniff').disabled = false;
    currentUniCandidates = [];
}

async function sniffUniversal() {
    const url = $('uniUrlInput').value.trim();
    if (!url) {
        showToast('Please enter a webpage or stream URL', 'error');
        return;
    }

    // UI state
    $('uniErrorMsg').classList.add('hidden');
    $('uniResultsCard').classList.add('hidden');
    $('uniProgressSection').classList.add('hidden');
    $('uniCompleteSection').classList.add('hidden');
    $('uniScanningBox').classList.remove('hidden');
    $('uniScanningStatus').textContent = 'Launching headless browser and inspecting network streams...';
    $('btnUniSniff').disabled = true;
    $('uniBtnIcon').textContent = 'sync';
    $('uniBtnIcon').classList.add('animate-spin');
    $('uniBtnText').textContent = 'Sniffing...';

    // Simulate animated discovery steps if scanning takes a few seconds
    const statusSteps = [
        'Launching headless Chromium...',
        'Navigating to page & dismissing popups...',
        'Scanning DOM & embedded iframe players...',
        'Triggering video player playback...',
        'Sniffing HLS, DASH, and media network requests...',
        'Inspecting manifest tracks & resolutions...'
    ];
    let stepIdx = 0;
    const stepInterval = setInterval(() => {
        if (stepIdx < statusSteps.length) {
            $('uniScanningStatus').textContent = statusSteps[stepIdx++];
        }
    }, 2500);

    try {
        const resp = await fetch('/api/universal/sniff', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url, timeout: 25 })
        });
        clearInterval(stepInterval);

        const data = await resp.json();
        $('uniScanningBox').classList.add('hidden');
        $('btnUniSniff').disabled = false;
        $('uniBtnIcon').textContent = 'travel_explore';
        $('uniBtnIcon').classList.remove('animate-spin');
        $('uniBtnText').textContent = 'Analyze';

        if (!resp.ok || data.error) {
            $('uniErrorText').textContent = data.error || 'Failed to sniff webpage streams.';
            $('uniErrorMsg').classList.remove('hidden');
            return;
        }

        if (!data.candidates || data.candidates.length === 0) {
            $('uniErrorText').textContent = 'No playable media streams found on this webpage. It may use unsupported proprietary DRM or canvas.';
            $('uniErrorMsg').classList.remove('hidden');
            return;
        }

        currentUniCandidates = data.candidates;
        currentUniIndex = 0;

        // Render switcher if multiple
        const switcher = $('uniCandidateSwitcher');
        const sel = $('uniCandidateSelect');
        if (currentUniCandidates.length > 1) {
            switcher.classList.remove('hidden');
            sel.innerHTML = '';
            currentUniCandidates.forEach((c, idx) => {
                const opt = document.createElement('option');
                opt.value = idx;
                opt.textContent = `${idx + 1}. [${c.type.toUpperCase()}] ${c.title.slice(0, 35)}...`;
                sel.appendChild(opt);
            });
        } else {
            switcher.classList.add('hidden');
        }

        renderUniversalCandidate(0);
        $('uniResultsCard').classList.remove('hidden');

    } catch (err) {
        clearInterval(stepInterval);
        $('uniScanningBox').classList.add('hidden');
        $('btnUniSniff').disabled = false;
        $('uniBtnIcon').textContent = 'travel_explore';
        $('uniBtnIcon').classList.remove('animate-spin');
        $('uniBtnText').textContent = 'Analyze';
        $('uniErrorText').textContent = 'Network error while analyzing webpage: ' + err.message;
        $('uniErrorMsg').classList.remove('hidden');
    }
}

function onSwitchUniversalCandidate() {
    const sel = $('uniCandidateSelect');
    const idx = parseInt(sel.value, 10) || 0;
    renderUniversalCandidate(idx);
}

function renderUniversalCandidate(idx) {
    currentUniIndex = idx;
    const c = currentUniCandidates[idx];
    if (!c) return;

    $('uniStreamTitle').textContent = c.title || 'Discovered Media Stream';
    $('uniStreamTypeBadge').textContent = (c.type || 'HLS').toUpperCase();
    $('uniStreamDuration').textContent = `Duration: ${c.duration_str || '--:--'}`;
    $('uniStreamCandidatesCount').textContent = `Streams Found: ${currentUniCandidates.length}`;

    // DRM Banner
    if (c.is_drm) {
        $('uniDrmBanner').classList.remove('hidden');
        $('uniDrmNotice').textContent = c.drm_notice || 'DRM Protection detected. This stream cannot be downloaded.';
        $('uniDownloadControls').classList.add('opacity-40', 'pointer-events-none');
    } else {
        $('uniDrmBanner').classList.add('hidden');
        $('uniDownloadControls').classList.remove('opacity-40', 'pointer-events-none');
    }

    // 1. Video Quality Pills
    const pillsContainer = $('uniQualityPills');
    pillsContainer.innerHTML = '';
    currentUniSelectedQuality = null;
    currentUniAudioOnly = false;

    const heights = c.unique_resolutions || [];
    if (heights.length > 0) {
        // "Best / Highest" pill
        const bestBtn = document.createElement('button');
        bestBtn.type = 'button';
        bestBtn.className = 'uni-q-pill px-4 py-2 rounded font-bold text-xs bg-primary text-on-primary border border-primary transition-all';
        bestBtn.textContent = `⭐ Best (${heights[0]}p)`;
        bestBtn.onclick = () => selectUniversalQuality(null, bestBtn, `Best (${heights[0]}p)`);
        pillsContainer.appendChild(bestBtn);

        // Resolution pills
        heights.forEach(h => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'uni-q-pill px-4 py-2 rounded font-medium text-xs bg-surface-container-lowest text-white border border-outline-variant hover:border-primary transition-all';
            btn.textContent = `${h}p`;
            btn.onclick = () => selectUniversalQuality(h, btn, `${h}p`);
            pillsContainer.appendChild(btn);
        });

        $('uniSelectedQualityLabel').textContent = `Best (${heights[0]}p)`;
    } else {
        const defaultBtn = document.createElement('button');
        defaultBtn.type = 'button';
        defaultBtn.className = 'uni-q-pill px-4 py-2 rounded font-bold text-xs bg-primary text-on-primary border border-primary transition-all';
        defaultBtn.textContent = 'Standard Video Stream';
        defaultBtn.onclick = () => selectUniversalQuality(null, defaultBtn, 'Standard Stream');
        pillsContainer.appendChild(defaultBtn);
        $('uniSelectedQualityLabel').textContent = 'Standard Stream';
    }

    // Audio-Only Pill
    const audioOnlyBtn = document.createElement('button');
    audioOnlyBtn.type = 'button';
    audioOnlyBtn.className = 'uni-q-pill px-4 py-2 rounded font-medium text-xs bg-surface-container-lowest text-outline hover:text-white border border-outline-variant hover:border-primary transition-all';
    audioOnlyBtn.textContent = '🎵 Audio Only';
    audioOnlyBtn.onclick = () => selectUniversalAudioOnly(audioOnlyBtn);
    pillsContainer.appendChild(audioOnlyBtn);

    // 2. Audio Renditions
    const audioSelect = $('uniAudioSelect');
    audioSelect.innerHTML = '<option value="">Default Audio Stream</option>';
    const audioTracks = c.audio_options || [];
    if (audioTracks.length > 0) {
        audioTracks.forEach(at => {
            const opt = document.createElement('option');
            opt.value = at.format_id;
            opt.textContent = at.label;
            audioSelect.appendChild(opt);
        });
        $('uniAudioTrackWrapper').style.display = 'block';
    } else {
        $('uniAudioTrackWrapper').style.display = 'block';
    }

    // 3. Subtitles
    const subContainer = $('uniSubtitlesList');
    subContainer.innerHTML = '';
    const subs = c.subtitles || [];
    if (subs.length > 0) {
        subs.forEach(st => {
            const label = document.createElement('label');
            label.className = 'flex items-center gap-2 text-xs text-white cursor-pointer p-1.5 rounded hover:bg-surface-container';
            label.innerHTML = `
                <input type="checkbox" name="uniSubCheckbox" value="${st.language}" class="rounded bg-background border-outline text-primary focus:ring-primary"/>
                <span class="truncate">${st.label}</span>
            `;
            subContainer.appendChild(label);
        });
    } else {
        subContainer.innerHTML = '<div class="text-xs text-outline italic col-span-3">No embedded or external subtitles found for this stream.</div>';
    }
}

function selectUniversalQuality(height, btnEl, labelText) {
    currentUniSelectedQuality = height;
    currentUniAudioOnly = false;
    $('uniSelectedQualityLabel').textContent = labelText;

    // Reset pills
    document.querySelectorAll('.uni-q-pill').forEach(b => {
        b.className = 'uni-q-pill px-4 py-2 rounded font-medium text-xs bg-surface-container-lowest text-white border border-outline-variant hover:border-primary transition-all';
    });
    btnEl.className = 'uni-q-pill px-4 py-2 rounded font-bold text-xs bg-primary text-on-primary border border-primary transition-all';
}

function selectUniversalAudioOnly(btnEl) {
    currentUniSelectedQuality = null;
    currentUniAudioOnly = true;
    $('uniSelectedQualityLabel').textContent = 'Audio Only (Lossless / Best)';

    document.querySelectorAll('.uni-q-pill').forEach(b => {
        b.className = 'uni-q-pill px-4 py-2 rounded font-medium text-xs bg-surface-container-lowest text-white border border-outline-variant hover:border-primary transition-all';
    });
    btnEl.className = 'uni-q-pill px-4 py-2 rounded font-bold text-xs bg-primary text-on-primary border border-primary transition-all';
}

function setAllUniversalSubs(selectAll) {
    document.querySelectorAll('input[name="uniSubCheckbox"]').forEach(cb => {
        cb.checked = selectAll;
    });
}

async function startUniversalDownload() {
    const c = currentUniCandidates[currentUniIndex];
    if (!c) return;

    if (c.is_drm) {
        showToast('Cannot download: Stream has DRM encryption', 'error');
        return;
    }

    // Collect selected subtitles
    const selectedSubs = [];
    document.querySelectorAll('input[name="uniSubCheckbox"]:checked').forEach(cb => {
        selectedSubs.push(cb.value);
    });

    // Format
    const outputFormat = document.querySelector('input[name="uniOutputFormat"]:checked')?.value || 'mkv';
    const audioId = $('uniAudioSelect').value || null;

    // UI state
    $('uniResultsCard').classList.add('hidden');
    $('uniProgressSection').classList.remove('hidden');
    $('uniCompleteSection').classList.add('hidden');
    $('uniProgressStatus').textContent = 'Connecting and initializing stream download...';
    $('uniProgressBarFill').style.width = '0%';
    $('uniProgPercent').textContent = '0%';
    $('uniProgSpeed').textContent = '---';
    $('uniProgSize').textContent = '--- / ---';
    $('uniProgEta').textContent = 'ETA: ---';

    try {
        const resp = await fetch('/api/universal/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: c.url,
                stream_url: c.url,
                type: c.type,
                headers: c.headers,
                title: c.title,
                page_url: c.page_url,
                is_drm: c.is_drm,
                duration_s: c.duration_s,
                resolution: currentUniSelectedQuality,
                audio_id: audioId,
                audio_only: currentUniAudioOnly,
                selected_subtitles: selectedSubs,
                subtitles_info: c.subtitles || [],
                local_manifest_path: c.local_manifest_path || '',
                output_format: outputFormat
            })
        });

        const data = await resp.json();
        if (!resp.ok || data.error) {
            showToast('Download start failed: ' + (data.error || 'Unknown error'), 'error');
            $('uniProgressSection').classList.add('hidden');
            $('uniResultsCard').classList.remove('hidden');
            return;
        }

        currentUniJobId = data.job_id;
        listenToUniversalProgress(currentUniJobId);

    } catch (err) {
        showToast('Error initiating download: ' + err.message, 'error');
        $('uniProgressSection').classList.add('hidden');
        $('uniResultsCard').classList.remove('hidden');
    }
}

function listenToUniversalProgress(jobId) {
    if (currentUniEventSource) {
        currentUniEventSource.close();
    }

    currentUniEventSource = new EventSource(`/api/progress/${jobId}`);

    currentUniEventSource.onmessage = e => {
        try {
            const data = JSON.parse(e.data);

            if (data.type === 'progress') {
                const pct = data.percent || 0.0;
                $('uniProgressBarFill').style.width = `${pct}%`;
                $('uniProgPercent').textContent = `${pct.toFixed(1)}%`;
                $('uniProgSpeed').textContent = data.speed_str || '---';
                $('uniProgSize').textContent = `${data.downloaded_str || '---'} / ${data.total_str || '---'}`;
                $('uniProgEta').textContent = data.eta_str ? `ETA: ${data.eta_str}` : 'ETA: ---';
                $('uniProgressStatus').textContent = (pct >= 99) ? 'Muxing video, audio & subtitles via FFmpeg...' : 'Downloading media stream...';
            } else if (data.type === 'done' || data.status === 'complete') {
                currentUniEventSource.close();
                $('uniProgressSection').classList.add('hidden');
                $('uniCompleteSection').classList.remove('hidden');
                $('uniCompleteTitle').textContent = 'Download Complete!';
                $('uniCompleteDetail').textContent = `${data.title || 'Media File'} • ${data.filesize_str || ''} • Saved to: ${data.filepath || ''}`;
                showToast('Stream downloaded & muxed successfully!', 'success');
                loadStats();
                loadHistory();
            } else if (data.type === 'error') {
                currentUniEventSource.close();
                $('uniProgressSection').classList.add('hidden');
                $('uniResultsCard').classList.remove('hidden');
                showToast('Download error: ' + (data.message || 'Stream download failed'), 'error');
            }
        } catch (err) {
            console.error('Error parsing SSE event:', err);
        }
    };

    currentUniEventSource.onerror = err => {
        console.error('SSE Error:', err);
        currentUniEventSource.close();
    };
}


