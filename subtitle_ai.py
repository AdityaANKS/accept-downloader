"""
Subtitle AI — Local Subtitle Intelligence
==========================================
Rule-based subtitle cleaning and smart language recommendation.
No external API required — all processing is local.

Features:
  - Smart language recommendation based on OS locale
  - Auto-generated subtitle cleaning (15+ pattern rules)
  - Duplicate line removal and merge
  - Filler word / stutter removal
  - Timing overlap correction
  - HTML/formatting tag stripping
  - Sentence case normalization
  - Short line merging for readability
"""

import os
import re
import sys
import locale
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════
# LANGUAGE RECOMMENDER
# ════════════════════════════════════════════════════════════════════════

# ISO 639-1 code → English name
LANG_NAMES = {
    'en': 'English', 'es': 'Spanish', 'fr': 'French', 'de': 'German',
    'it': 'Italian', 'pt': 'Portuguese', 'ru': 'Russian', 'ja': 'Japanese',
    'ko': 'Korean', 'zh': 'Chinese', 'hi': 'Hindi', 'ar': 'Arabic',
    'nl': 'Dutch', 'pl': 'Polish', 'sv': 'Swedish', 'da': 'Danish',
    'no': 'Norwegian', 'fi': 'Finnish', 'tr': 'Turkish', 'th': 'Thai',
    'vi': 'Vietnamese', 'id': 'Indonesian', 'ms': 'Malay', 'tl': 'Filipino',
    'uk': 'Ukrainian', 'cs': 'Czech', 'el': 'Greek', 'he': 'Hebrew',
    'hu': 'Hungarian', 'ro': 'Romanian', 'bg': 'Bulgarian', 'hr': 'Croatian',
    'bn': 'Bengali', 'ta': 'Tamil', 'te': 'Telugu', 'mr': 'Marathi',
    'gu': 'Gujarati', 'kn': 'Kannada', 'ml': 'Malayalam', 'pa': 'Punjabi',
    'ur': 'Urdu', 'fa': 'Persian', 'ne': 'Nepali', 'si': 'Sinhala',
}

# Windows locale name → ISO code
_WIN_LOCALE_MAP = {
    'english': 'en', 'spanish': 'es', 'french': 'fr', 'german': 'de',
    'italian': 'it', 'portuguese': 'pt', 'russian': 'ru', 'japanese': 'ja',
    'korean': 'ko', 'chinese': 'zh', 'hindi': 'hi', 'arabic': 'ar',
    'dutch': 'nl', 'polish': 'pl', 'swedish': 'sv', 'danish': 'da',
    'norwegian': 'no', 'finnish': 'fi', 'turkish': 'tr', 'thai': 'th',
    'vietnamese': 'vi', 'indonesian': 'id', 'malay': 'ms', 'filipino': 'tl',
    'ukrainian': 'uk', 'czech': 'cs', 'greek': 'el', 'hebrew': 'he',
    'hungarian': 'hu', 'romanian': 'ro', 'bulgarian': 'bg', 'croatian': 'hr',
    'bengali': 'bn', 'tamil': 'ta', 'telugu': 'te', 'marathi': 'mr',
    'gujarati': 'gu', 'kannada': 'kn', 'malayalam': 'ml', 'punjabi': 'pa',
    'urdu': 'ur', 'persian': 'fa', 'nepali': 'ne', 'sinhala': 'si',
}


def detect_user_language() -> str:
    """
    Detect the user's preferred language from OS locale.
    
    Returns an ISO 639-1 code (e.g. 'en', 'hi', 'ja').
    Falls back to 'en' if detection fails.
    """
    try:
        # Try locale.getdefaultlocale first
        loc = locale.getdefaultlocale()
        if loc and loc[0]:
            code = loc[0].split('_')[0].lower()
            if code in LANG_NAMES:
                return code
        
        # Windows: try GetUserDefaultUILanguage
        if sys.platform == 'win32':
            try:
                import ctypes
                lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
                # Primary language ID is lower 10 bits
                primary = lang_id & 0x3FF
                # Common Windows primary language IDs
                win_lang_ids = {
                    0x09: 'en', 0x0A: 'es', 0x0C: 'fr', 0x07: 'de',
                    0x10: 'it', 0x16: 'pt', 0x19: 'ru', 0x11: 'ja',
                    0x12: 'ko', 0x04: 'zh', 0x39: 'hi', 0x01: 'ar',
                    0x13: 'nl', 0x15: 'pl', 0x1D: 'sv', 0x06: 'da',
                    0x14: 'no', 0x0B: 'fi', 0x1F: 'tr', 0x1E: 'th',
                    0x2A: 'vi', 0x21: 'id', 0x3E: 'ms', 0x22: 'uk',
                    0x05: 'cs', 0x08: 'el', 0x0D: 'he', 0x0E: 'hu',
                    0x18: 'ro', 0x02: 'bg', 0x1A: 'hr', 0x45: 'bn',
                    0x49: 'ta', 0x4A: 'te', 0x4E: 'mr', 0x47: 'gu',
                    0x4B: 'kn', 0x4C: 'ml', 0x46: 'pa', 0x20: 'ur',
                    0x29: 'fa', 0x61: 'ne',
                }
                if primary in win_lang_ids:
                    return win_lang_ids[primary]
            except Exception:
                pass
        
        # Try LANG environment variable (Linux/macOS)
        lang_env = os.environ.get('LANG', '')
        if lang_env:
            code = lang_env.split('.')[0].split('_')[0].lower()
            if code in LANG_NAMES:
                return code
        
        # Try LC_ALL, LC_MESSAGES
        for var in ('LC_ALL', 'LC_MESSAGES', 'LANGUAGE'):
            val = os.environ.get(var, '')
            if val:
                code = val.split('.')[0].split('_')[0].split(':')[0].lower()
                if code in LANG_NAMES:
                    return code
    except Exception:
        pass
    
    return 'en'


def recommend_languages(
    available_codes: List[str],
    user_lang: str = None,
    video_lang: str = None,
) -> List[str]:
    """
    Reorder available subtitle codes to put the most relevant ones first.
    
    Priority order:
      1. User's OS language (exact match)
      2. User's OS language (variant, e.g. zh-Hans for zh)
      3. English (if not already user's language)
      4. Video's original language
      5. Everything else alphabetically
    
    Returns: reordered list of language codes
    """
    if user_lang is None:
        user_lang = detect_user_language()
    
    priority = []
    remaining = []
    
    # Separate into priority and remaining
    for code in available_codes:
        base = code.split('-')[0].lower()
        if base == user_lang:
            priority.insert(0, code)  # User's language goes first
        elif base == 'en' and user_lang != 'en':
            priority.append(code)  # English as second priority
        elif video_lang and base == video_lang:
            priority.append(code)
        else:
            remaining.append(code)
    
    return priority + remaining


def get_recommendation_label(code: str, user_lang: str) -> str:
    """Get a recommendation label for a subtitle code."""
    base = code.split('-')[0].lower()
    if base == user_lang:
        return ' [*] recommended'
    elif base == 'en' and user_lang != 'en':
        return ' (universal)'
    return ''


# ════════════════════════════════════════════════════════════════════════
# SRT PARSER
# ════════════════════════════════════════════════════════════════════════

@dataclass
class SubtitleEntry:
    """A single subtitle entry (index, timing, text)."""
    index: int
    start: str        # "00:01:23,456"
    end: str          # "00:01:25,789"
    text: str          # Cleaned text content
    original: str = '' # Original text before cleaning
    
    @property
    def start_ms(self) -> int:
        return _time_to_ms(self.start)
    
    @property
    def end_ms(self) -> int:
        return _time_to_ms(self.end)
    
    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms
    
    def to_srt(self) -> str:
        return f"{self.index}\n{self.start} --> {self.end}\n{self.text}\n"


def _time_to_ms(t: str) -> int:
    """Convert SRT time '00:01:23,456' to milliseconds."""
    try:
        parts = t.replace(',', '.').split(':')
        h, m = int(parts[0]), int(parts[1])
        s_parts = parts[2].split('.')
        s = int(s_parts[0])
        ms = int(s_parts[1]) if len(s_parts) > 1 else 0
        return h * 3600000 + m * 60000 + s * 1000 + ms
    except (ValueError, IndexError):
        return 0


def _ms_to_time(ms: int) -> str:
    """Convert milliseconds to SRT time '00:01:23,456'."""
    if ms < 0:
        ms = 0
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(content: str) -> List[SubtitleEntry]:
    """Parse SRT content into SubtitleEntry list."""
    entries = []
    # Split by double newline (subtitle blocks)
    blocks = re.split(r'\n\s*\n', content.strip())
    
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        
        # Find the timing line (contains ' --> ')
        timing_idx = -1
        for i, line in enumerate(lines):
            if ' --> ' in line:
                timing_idx = i
                break
        
        if timing_idx < 0:
            continue
        
        # Parse timing
        timing = lines[timing_idx]
        match = re.match(
            r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})',
            timing,
        )
        if not match:
            continue
        
        start = match.group(1).replace('.', ',')
        end = match.group(2).replace('.', ',')
        
        # Index is the line before timing (if it's a number)
        idx_str = lines[timing_idx - 1].strip() if timing_idx > 0 else ''
        try:
            index = int(idx_str)
        except ValueError:
            index = len(entries) + 1
        
        # Text is everything after timing
        text = '\n'.join(lines[timing_idx + 1:]).strip()
        
        if text:
            entries.append(SubtitleEntry(
                index=index, start=start, end=end,
                text=text, original=text,
            ))
    
    return entries


def entries_to_srt(entries: List[SubtitleEntry]) -> str:
    """Convert SubtitleEntry list back to SRT string."""
    blocks = []
    for i, entry in enumerate(entries, 1):
        entry.index = i  # Re-number
        blocks.append(entry.to_srt())
    return '\n'.join(blocks)


# ════════════════════════════════════════════════════════════════════════
# SUBTITLE CLEANER
# ════════════════════════════════════════════════════════════════════════

class SubtitleCleaner:
    """
    Rule-based subtitle cleaning pipeline.
    
    Trained patterns from common auto-generated subtitle issues:
      - [Music], [Applause], [Laughter] tags
      - HTML/formatting tags (<i>, <b>, <font>, etc.)
      - Duplicate consecutive lines
      - Filler words and stutters
      - Broken sentence fragments across entries
      - Overlapping timing
      - Empty or whitespace-only entries
      - Speaker labels (>> SPEAKER:)
      - Excessive punctuation
      - Unicode artifacts
    """
    
    # ── Pattern Definitions (trained from common subtitle data) ──────
    
    # Sound effect / non-speech tags to remove entirely
    NOISE_TAGS = re.compile(
        r'\[(?:Music|Applause|Laughter|Cheering|Silence|Inaudible|'
        r'music|applause|laughter|cheering|silence|inaudible|'
        r'Music Playing|music playing|MUSIC|APPLAUSE|LAUGHTER|'
        r'♪.*?♪|🎵.*?🎵|🎶.*?🎶|'
        r'Background (?:Music|Noise)|background (?:music|noise))\]',
        re.DOTALL
    )
    
    # Music notes used as markers
    MUSIC_NOTES = re.compile(r'[♪♫🎵🎶]+\s*.*?\s*[♪♫🎵🎶]+', re.DOTALL)
    MUSIC_NOTES_SINGLE = re.compile(r'^[♪♫🎵🎶\s]+$')
    
    # HTML/formatting tags
    HTML_TAGS = re.compile(r'</?(?:i|b|u|font|span|div|br|em|strong)[^>]*/?>', re.IGNORECASE)
    
    # Speaker labels: ">> JOHN:", "SPEAKER 1:", "- Speaker:", etc.
    SPEAKER_LABEL = re.compile(r'^(?:>>?\s*)?(?:[A-Z][A-Z\s]{0,20}:)\s*')
    
    # Filler words / stutters (English-focused)
    FILLERS_EN = re.compile(
        r'\b(?:um+|uh+|er+|ah+|eh+|hm+|hmm+|mmm+|'
        r'you know|i mean|like,?\s+like|sort of|kind of|'
        r'basically|literally|actually,?\s+actually)\b',
        re.IGNORECASE,
    )
    
    # Stutters: "I- I- I want", "the- the thing"
    STUTTER = re.compile(r'\b(\w{1,4})-\s*(?:\1-?\s*)+\b', re.IGNORECASE)
    
    # Orphan commas left after filler removal: ", , text" -> "text"
    ORPHAN_COMMAS = re.compile(r'(?:,\s*){2,}')
    LEADING_COMMA = re.compile(r'^\s*,\s*')
    
    # Excessive punctuation: "!!!", "???", "..."  -> single
    EXCESS_PUNCT = re.compile(r'([!?.])\1{2,}')
    
    # Multiple spaces
    MULTI_SPACE = re.compile(r'  +')
    
    # Leading/trailing hyphens used as dialogue markers (keep one)
    DIALOGUE_DASH = re.compile(r'^-\s*-+\s*')
    
    # Unicode artifacts common in auto-subs
    UNICODE_ARTIFACTS = re.compile(r'[\u200b\u200c\u200d\ufeff\u00ad]')  # zero-width, BOM, soft hyphen
    
    # Emoji pattern — covers most Unicode emoji ranges
    EMOJI = re.compile(
        r'['
        r'\U0001F600-\U0001F64F'  # Emoticons
        r'\U0001F300-\U0001F5FF'  # Symbols & Pictographs
        r'\U0001F680-\U0001F6FF'  # Transport & Map
        r'\U0001F1E0-\U0001F1FF'  # Flags
        r'\U0001F900-\U0001F9FF'  # Supplemental Symbols
        r'\U0001FA00-\U0001FA6F'  # Chess, Extended-A
        r'\U0001FA70-\U0001FAFF'  # Symbols Extended-A
        r'\U00002702-\U000027B0'  # Dingbats
        r'\U000024C2-\U0000257F'  # Enclosed chars
        r'\U0000FE00-\U0000FE0F'  # Variation selectors
        r'\U00002600-\U000026FF'  # Misc symbols
        r'\U00002700-\U000027BF'  # Dingbats
        r'\U0000231A-\U0000231B'  # Watch/Hourglass
        r'\U000023E9-\U000023F3'  # Misc technical
        r'\U000023F8-\U000023FA'  # Misc technical
        r'\U000025AA-\U000025AB'  # Squares
        r'\U000025B6\U000025C0'   # Play/Reverse
        r'\U000025FB-\U000025FE'  # Squares
        r'\U00002934-\U00002935'  # Arrows
        r'\U00003030\U0000303D'   # Misc
        r'\U00003297\U00003299'   # Circled Ideograph
        r']+', flags=re.UNICODE
    )
    
    # ── Cleaning Pipeline ────────────────────────────────────────────
    
    def __init__(self, 
                 remove_music: bool = True,
                 remove_fillers: bool = True,
                 remove_emojis: bool = True,
                 fix_stutters: bool = True,
                 remove_html: bool = True,
                 fix_timing: bool = True,
                 merge_short: bool = True,
                 deduplicate: bool = True,
                 fix_capitalization: bool = True,
                 min_duration_ms: int = 200,
                 max_gap_ms: int = 500,
                 min_merge_chars: int = 15):
        """
        Configure the cleaning pipeline.
        
        Args:
            remove_music: Remove [Music], ♪, etc. tags
            remove_fillers: Remove filler words (um, uh, you know)
            fix_stutters: Fix stuttering (I- I- I → I)
            remove_html: Strip HTML formatting tags
            fix_timing: Fix overlapping timings
            merge_short: Merge short consecutive entries
            deduplicate: Remove duplicate consecutive lines
            fix_capitalization: Fix sentence capitalization
            min_duration_ms: Minimum subtitle display time
            max_gap_ms: Max gap between entries to merge
            min_merge_chars: Merge entries shorter than this
        """
        self.remove_music = remove_music
        self.remove_fillers = remove_fillers
        self.remove_emojis = remove_emojis
        self.fix_stutters = fix_stutters
        self.remove_html = remove_html
        self.fix_timing = fix_timing
        self.merge_short = merge_short
        self.deduplicate = deduplicate
        self.fix_capitalization = fix_capitalization
        self.min_duration_ms = min_duration_ms
        self.max_gap_ms = max_gap_ms
        self.min_merge_chars = min_merge_chars
        
        # Stats
        self.stats = {
            'total_entries': 0,
            'removed_noise': 0,
            'removed_duplicates': 0,
            'removed_emojis': 0,
            'merged_short': 0,
            'cleaned_fillers': 0,
            'fixed_stutters': 0,
            'fixed_timing': 0,
            'fixed_capitalization': 0,
            'removed_html': 0,
            'final_entries': 0,
        }
    
    def clean(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        """
        Run the full cleaning pipeline on subtitle entries.
        
        Pipeline order is important — each step may depend on the previous:
          1. Strip HTML tags
          2. Remove noise/music tags
          3. Remove unicode artifacts
          4. Clean text content (fillers, stutters, punctuation)
          5. Remove empty entries
          6. Deduplicate consecutive identical lines
          7. Fix timing overlaps
          8. Merge short consecutive entries
          9. Fix capitalization
          10. Re-index
        """
        self.stats['total_entries'] = len(entries)
        
        if not entries:
            return entries
        
        # 1. Strip HTML
        if self.remove_html:
            entries = self._strip_html(entries)
        
        # 2. Remove noise tags
        if self.remove_music:
            entries = self._remove_noise(entries)
        
        # 3. Unicode cleanup + emoji removal
        entries = self._clean_unicode(entries)
        if self.remove_emojis:
            entries = self._remove_emojis(entries)
        
        # 4. Clean text content
        entries = self._clean_text(entries)
        
        # 5. Remove empty
        entries = [e for e in entries if e.text.strip()]
        
        # 6. Deduplicate
        if self.deduplicate:
            entries = self._deduplicate(entries)
        
        # 7. Fix timing
        if self.fix_timing:
            entries = self._fix_timing_overlaps(entries)
        
        # 8. Merge short entries
        if self.merge_short:
            entries = self._merge_short_entries(entries)
        
        # 9. Fix capitalization
        if self.fix_capitalization:
            entries = self._fix_caps(entries)
        
        # 10. Re-index
        for i, entry in enumerate(entries, 1):
            entry.index = i
        
        self.stats['final_entries'] = len(entries)
        return entries
    
    def clean_file(self, filepath: str, output_path: str = None) -> Dict:
        """
        Clean an SRT file and save the result.
        
        Args:
            filepath: Path to input .srt file
            output_path: Path for cleaned output (default: adds '_clean' suffix)
        
        Returns: stats dict with cleaning metrics
        """
        if not os.path.exists(filepath):
            return {'error': f'File not found: {filepath}'}
        
        try:
            # Read with fallback encodings
            content = None
            for enc in ('utf-8', 'utf-8-sig', 'latin-1', 'cp1252'):
                try:
                    with open(filepath, 'r', encoding=enc) as f:
                        content = f.read()
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            
            if content is None:
                return {'error': 'Could not decode file'}
            
            entries = parse_srt(content)
            if not entries:
                return {'error': 'No subtitle entries found'}
            
            cleaned = self.clean(entries)
            result = entries_to_srt(cleaned)
            
            # Determine output path
            if output_path is None:
                base, ext = os.path.splitext(filepath)
                output_path = f"{base}_clean{ext}"
            
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(result)
            
            self.stats['output_path'] = output_path
            self.stats['input_path'] = filepath
            return self.stats.copy()
            
        except Exception as e:
            return {'error': str(e)}
    
    # ── Pipeline Steps ───────────────────────────────────────────────
    
    def _strip_html(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        count = 0
        for entry in entries:
            cleaned = self.HTML_TAGS.sub('', entry.text)
            if cleaned != entry.text:
                count += 1
                entry.text = cleaned
        self.stats['removed_html'] = count
        return entries
    
    def _remove_noise(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        result = []
        removed = 0
        for entry in entries:
            text = self.NOISE_TAGS.sub('', entry.text).strip()
            text = self.MUSIC_NOTES.sub('', text).strip()
            
            # Check if entry is entirely music/noise
            if self.MUSIC_NOTES_SINGLE.match(text) or not text:
                removed += 1
                continue
            
            entry.text = text
            result.append(entry)
        
        self.stats['removed_noise'] = removed
        return result
    
    def _clean_unicode(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        for entry in entries:
            entry.text = self.UNICODE_ARTIFACTS.sub('', entry.text)
        return entries
    
    def _remove_emojis(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        """Remove color emojis from subtitle text."""
        count = 0
        for entry in entries:
            cleaned = self.EMOJI.sub('', entry.text).strip()
            cleaned = self.MULTI_SPACE.sub(' ', cleaned)
            if cleaned != entry.text:
                count += 1
                entry.text = cleaned
        self.stats['removed_emojis'] = count
        return entries
    
    def _clean_text(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        filler_count = 0
        stutter_count = 0
        
        for entry in entries:
            text = entry.text
            
            # Remove speaker labels
            text = self.SPEAKER_LABEL.sub('', text)
            
            # Fix dialogue dashes
            text = self.DIALOGUE_DASH.sub('- ', text)
            
            # Remove fillers
            if self.remove_fillers:
                cleaned = self.FILLERS_EN.sub('', text)
                if cleaned != text:
                    filler_count += 1
                    text = cleaned
                    # Clean orphan commas left by filler removal
                    text = self.ORPHAN_COMMAS.sub(', ', text)
                    text = self.LEADING_COMMA.sub('', text)
            
            # Fix stutters: "I- I- I want" -> "I want"
            if self.fix_stutters:
                cleaned = self.STUTTER.sub(r'\1 ', text)
                if cleaned != text:
                    stutter_count += 1
                    text = cleaned
            
            # Fix excessive punctuation
            text = self.EXCESS_PUNCT.sub(r'\1', text)
            
            # Clean up whitespace
            text = self.MULTI_SPACE.sub(' ', text)
            text = text.strip()
            
            # Fix lines that are just punctuation
            if text and all(c in '.,!?;:-–—\'"()[] ' for c in text):
                text = ''
            
            entry.text = text
        
        self.stats['cleaned_fillers'] = filler_count
        self.stats['fixed_stutters'] = stutter_count
        return entries
    
    def _deduplicate(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        """Remove consecutive entries with identical text."""
        if not entries:
            return entries
        
        result = [entries[0]]
        removed = 0
        
        for entry in entries[1:]:
            # Normalize for comparison (lowercase, strip whitespace)
            prev_norm = result[-1].text.lower().strip()
            curr_norm = entry.text.lower().strip()
            
            if curr_norm == prev_norm:
                # Extend the previous entry's end time
                result[-1].end = entry.end
                removed += 1
            else:
                result.append(entry)
        
        self.stats['removed_duplicates'] = removed
        return result
    
    def _fix_timing_overlaps(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        """Fix overlapping subtitle timings."""
        fixed = 0
        
        for i in range(len(entries) - 1):
            curr = entries[i]
            nxt = entries[i + 1]
            
            curr_end = curr.end_ms
            nxt_start = nxt.start_ms
            
            if curr_end > nxt_start:
                # Overlap: set current end to next start minus a small gap
                new_end = max(curr.start_ms + self.min_duration_ms, nxt_start - 50)
                curr.end = _ms_to_time(new_end)
                fixed += 1
            
            # Ensure minimum duration
            if curr.duration_ms < self.min_duration_ms:
                curr.end = _ms_to_time(curr.start_ms + self.min_duration_ms)
                fixed += 1
        
        self.stats['fixed_timing'] = fixed
        return entries
    
    def _merge_short_entries(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        """Merge very short consecutive entries into longer, more readable ones."""
        if not entries:
            return entries
        
        result = [entries[0]]
        merged = 0
        
        for entry in entries[1:]:
            prev = result[-1]
            gap = entry.start_ms - prev.end_ms
            
            # Merge if: both are short, gap is small, combined isn't too long
            prev_short = len(prev.text) < self.min_merge_chars
            curr_short = len(entry.text) < self.min_merge_chars
            combined_ok = len(prev.text) + len(entry.text) < 120  # ~2 lines
            gap_ok = gap <= self.max_gap_ms
            
            if prev_short and curr_short and combined_ok and gap_ok:
                # Merge: extend previous entry
                prev.text = prev.text.rstrip() + ' ' + entry.text.lstrip()
                prev.end = entry.end
                merged += 1
            else:
                result.append(entry)
        
        self.stats['merged_short'] = merged
        return result
    
    def _fix_caps(self, entries: List[SubtitleEntry]) -> List[SubtitleEntry]:
        """Fix capitalization: sentence case for all-caps or all-lowercase subs."""
        fixed = 0
        
        for entry in entries:
            text = entry.text
            lines = text.split('\n')
            new_lines = []
            
            for line in lines:
                stripped = line.strip()
                if not stripped:
                    new_lines.append(line)
                    continue
                
                # Only fix all-uppercase lines (not mixed case)
                alpha_chars = [c for c in stripped if c.isalpha()]
                if alpha_chars and all(c.isupper() for c in alpha_chars) and len(alpha_chars) > 3:
                    # Convert to sentence case
                    fixed_line = self._sentence_case(stripped)
                    new_lines.append(fixed_line)
                    fixed += 1
                else:
                    new_lines.append(line)
            
            entry.text = '\n'.join(new_lines)
        
        self.stats['fixed_capitalization'] = fixed
        return entries
    
    @staticmethod
    def _sentence_case(text: str) -> str:
        """Convert text to sentence case, preserving known proper nouns."""
        # Split by sentence-ending punctuation
        sentences = re.split(r'([.!?]\s+)', text.lower())
        result = []
        for i, part in enumerate(sentences):
            if i % 2 == 0 and part:  # Sentence content (not delimiter)
                part = part[0].upper() + part[1:] if part else part
                # Re-capitalize "I" as a pronoun
                part = re.sub(r"\bi\b", "I", part)
            result.append(part)
        return ''.join(result)
    
    def get_stats_summary(self) -> str:
        """Get a human-readable summary of cleaning stats."""
        s = self.stats
        lines = [f"  Cleaned: {s['total_entries']} → {s['final_entries']} entries"]
        
        changes = []
        if s.get('removed_noise'):
            changes.append(f"{s['removed_noise']} noise tags removed")
        if s.get('removed_duplicates'):
            changes.append(f"{s['removed_duplicates']} duplicates removed")
        if s.get('merged_short'):
            changes.append(f"{s['merged_short']} short lines merged")
        if s.get('cleaned_fillers'):
            changes.append(f"{s['cleaned_fillers']} fillers removed")
        if s.get('fixed_stutters'):
            changes.append(f"{s['fixed_stutters']} stutters fixed")
        if s.get('fixed_timing'):
            changes.append(f"{s['fixed_timing']} timing issues fixed")
        if s.get('fixed_capitalization'):
            changes.append(f"{s['fixed_capitalization']} caps fixed")
        if s.get('removed_html'):
            changes.append(f"{s['removed_html']} HTML tags stripped")
        if s.get('removed_emojis'):
            changes.append(f"{s['removed_emojis']} emojis removed")
        
        if changes:
            lines.append(f"  Changes: {', '.join(changes)}")
        else:
            lines.append("  No changes needed — subtitles look clean!")
        
        return '\n'.join(lines)


# ════════════════════════════════════════════════════════════════════════
# SUBTITLE ANALYZER
# ════════════════════════════════════════════════════════════════════════

def analyze_subtitle_quality(entries: List[SubtitleEntry]) -> Dict:
    """
    Analyze subtitle quality and return a score + issues found.
    
    Score: 0-100 (100 = perfect, no issues)
    Useful for ML training data — lets the model learn what "good" subs look like.
    """
    if not entries:
        return {'score': 0, 'issues': ['No entries']}
    
    issues = []
    penalties = 0
    
    total = len(entries)
    
    # Check for noise tags
    noise_count = sum(1 for e in entries if SubtitleCleaner.NOISE_TAGS.search(e.text))
    if noise_count:
        ratio = noise_count / total
        penalties += min(20, int(ratio * 100))
        issues.append(f"{noise_count} noise tags ({ratio:.0%})")
    
    # Check for HTML
    html_count = sum(1 for e in entries if SubtitleCleaner.HTML_TAGS.search(e.text))
    if html_count:
        penalties += min(10, html_count)
        issues.append(f"{html_count} HTML tags")
    
    # Check for duplicates
    dup_count = 0
    for i in range(1, len(entries)):
        if entries[i].text.lower().strip() == entries[i-1].text.lower().strip():
            dup_count += 1
    if dup_count:
        penalties += min(15, dup_count)
        issues.append(f"{dup_count} duplicate lines")
    
    # Check for timing overlaps
    overlap_count = 0
    for i in range(len(entries) - 1):
        if entries[i].end_ms > entries[i+1].start_ms:
            overlap_count += 1
    if overlap_count:
        penalties += min(15, overlap_count)
        issues.append(f"{overlap_count} timing overlaps")
    
    # Check for very short entries
    short_count = sum(1 for e in entries if len(e.text.strip()) < 3)
    if short_count:
        ratio = short_count / total
        penalties += min(10, int(ratio * 50))
        issues.append(f"{short_count} very short entries")
    
    # Check for all-caps
    caps_count = sum(1 for e in entries 
                     if e.text.strip() and e.text == e.text.upper() 
                     and len(e.text.strip()) > 5)
    if caps_count > total * 0.5:
        penalties += 10
        issues.append(f"Mostly all-caps ({caps_count}/{total})")
    
    score = max(0, 100 - penalties)
    
    return {
        'score': score,
        'total_entries': total,
        'issues': issues,
        'needs_cleaning': score < 85,
    }
