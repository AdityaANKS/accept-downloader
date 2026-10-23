"""
Database Manager for Personal Media Downloader
With automatic schema migration support.
"""

import os
import sqlite3
import json
import threading
from datetime import datetime
from typing import Optional, Dict, List, Any
from contextlib import contextmanager
from dataclasses import dataclass, asdict

# Imports
try:
    from config import Config as _Cfg
    _DEFAULT_DB_PATH = _Cfg.DATABASE_PATH
except ImportError:
    _DEFAULT_DB_PATH = os.path.join(
        os.path.expanduser("~"), "Downloads", "downloader", "downloader.db"
    )

from utils import format_size, format_duration, get_logger, ensure_directory

logger = get_logger()


@dataclass
class DownloadRecord:
    """Download record"""
    id: Optional[int] = None
    url: str = ""
    platform: str = ""
    title: str = ""
    uploader: str = ""
    duration: float = 0
    quality: str = ""
    format: str = ""
    filesize: int = 0
    filepath: str = ""
    thumbnail_path: str = ""
    description: str = ""
    view_count: int = 0
    like_count: int = 0
    video_id: str = ""
    media_type: str = "video"
    status: str = "complete"
    error_message: str = ""
    download_date: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict):
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid}
        return cls(**filtered)
    
    @classmethod
    def from_row(cls, row):
        if row is None:
            return None
        return cls.from_dict(dict(row))


@dataclass
class QueueItem:
    """Queue item"""
    id: Optional[int] = None
    url: str = ""
    priority: int = 0
    status: str = "pending"
    added_date: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)


class DownloadStatus:
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DatabaseManager:
    """SQLite database manager with migration support"""
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or _DEFAULT_DB_PATH
        self._local = threading.local()
        self._lock = threading.Lock()
        
        # Ensure directory
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            ensure_directory(db_dir)
        
        # Initialize
        self._init_database()
        self._run_migrations()
    
    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA foreign_keys = ON")
        return self._local.conn
    
    @contextmanager
    def _cursor(self):
        cursor = self._conn.cursor()
        try:
            yield cursor
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            cursor.close()
    
    def _init_database(self):
        """Create tables if not exist"""
        with self._lock:
            with self._cursor() as c:
                # Downloads table
                c.execute('''
                    CREATE TABLE IF NOT EXISTS downloads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT UNIQUE NOT NULL,
                        platform TEXT,
                        title TEXT,
                        uploader TEXT,
                        duration REAL DEFAULT 0,
                        quality TEXT,
                        format TEXT,
                        filesize INTEGER DEFAULT 0,
                        filepath TEXT,
                        thumbnail_path TEXT,
                        description TEXT,
                        view_count INTEGER DEFAULT 0,
                        like_count INTEGER DEFAULT 0,
                        video_id TEXT,
                        media_type TEXT DEFAULT 'video',
                        status TEXT DEFAULT 'pending',
                        error_message TEXT,
                        download_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Queue table
                c.execute('''
                    CREATE TABLE IF NOT EXISTS queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        priority INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'pending',
                        added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Active tasks table (for chunked download pause/resume/edit)
                c.execute('''
                    CREATE TABLE IF NOT EXISTS active_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        filepath TEXT NOT NULL,
                        filename TEXT DEFAULT '',
                        total_size INTEGER DEFAULT 0,
                        downloaded INTEGER DEFAULT 0,
                        chunks_json TEXT DEFAULT '[]',
                        status TEXT DEFAULT 'pending',
                        headers_json TEXT DEFAULT '{}',
                        thread_count INTEGER DEFAULT 8,
                        etag TEXT DEFAULT '',
                        error TEXT DEFAULT '',
                        created_at TEXT DEFAULT '',
                        updated_at TEXT DEFAULT ''
                    )
                ''')
                
                # Indexes
                c.execute('CREATE INDEX IF NOT EXISTS idx_downloads_url ON downloads(url)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status)')
                c.execute('CREATE INDEX IF NOT EXISTS idx_active_tasks_status ON active_tasks(status)')
    
    def _run_migrations(self):
        """Run database migrations for schema updates if needed"""
        with self._lock:
            with self._cursor() as c:
                c.execute("PRAGMA table_info(downloads)")
                download_cols = {row['name'] for row in c.fetchall()}
                
                new_cols = [
                    ("media_type", "TEXT DEFAULT 'video'"),
                    ("error_message", "TEXT"),
                    ("video_id", "TEXT"),
                    ("thumbnail_path", "TEXT"),
                    ("description", "TEXT"),
                    ("view_count", "INTEGER DEFAULT 0"),
                    ("like_count", "INTEGER DEFAULT 0"),
                ]
                
                for col_name, col_def in new_cols:
                    if col_name not in download_cols:
                        try:
                            c.execute(f"ALTER TABLE downloads ADD COLUMN {col_name} {col_def}")
                        except Exception:
                            pass
    
    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
    
    # ==================== DOWNLOADS ====================
    
    def add_download(self, record: DownloadRecord = None, **kwargs) -> int:
        """Add download record"""
        if record:
            data = record.to_dict()
            data.pop('id', None)
        else:
            data = kwargs
        
        url = data.get('url')
        if not url:
            raise ValueError("URL required")
        
        data.setdefault('status', 'complete')
        data.setdefault('media_type', 'video')
        data.setdefault('download_date', datetime.now().isoformat())
        
        # Truncate description
        if data.get('description') and len(str(data['description'])) > 1000:
            data['description'] = str(data['description'])[:1000]
        
        with self._lock:
            with self._cursor() as c:
                # Check exists
                c.execute("SELECT id FROM downloads WHERE url = ?", (url,))
                existing = c.fetchone()
                
                if existing:
                    # Update
                    cols = [k for k in data.keys() if k not in ('url', 'id')]
                    if cols:
                        set_clause = ", ".join([f"{k} = ?" for k in cols])
                        values = [data[k] for k in cols] + [url]
                        c.execute(f"UPDATE downloads SET {set_clause}, download_date = CURRENT_TIMESTAMP WHERE url = ?", values)
                    return existing['id']
                else:
                    # Insert
                    cols = [k for k in data.keys() if k != 'id']
                    placeholders = ", ".join(["?" for _ in cols])
                    c.execute(f"INSERT INTO downloads ({', '.join(cols)}) VALUES ({placeholders})", [data[k] for k in cols])
                    return c.lastrowid
    
    def get_download(self, download_id: int = None, url: str = None) -> Optional[DownloadRecord]:
        """Get download by ID or URL"""
        with self._cursor() as c:
            if download_id:
                c.execute("SELECT * FROM downloads WHERE id = ?", (download_id,))
            elif url:
                c.execute("SELECT * FROM downloads WHERE url = ?", (url,))
            else:
                return None
            return DownloadRecord.from_row(c.fetchone())
    
    def get_downloads(self, limit=100, offset=0, status=None, platform=None, order_desc=True) -> List[DownloadRecord]:
        """Get downloads with filtering"""
        query = "SELECT * FROM downloads WHERE 1=1"
        params = []
        
        if status:
            query += " AND status = ?"
            params.append(status)
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        
        query += f" ORDER BY download_date {'DESC' if order_desc else 'ASC'} LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        with self._cursor() as c:
            c.execute(query, params)
            return [DownloadRecord.from_row(row) for row in c.fetchall()]
    
    def is_downloaded(self, url: str) -> bool:
        """Check if URL downloaded"""
        with self._cursor() as c:
            c.execute("SELECT status FROM downloads WHERE url = ?", (url,))
            row = c.fetchone()
            return row is not None and row['status'] == 'complete'
    
    def update_download_status(self, url: str = None, download_id: int = None, status: str = None, 
                                filepath: str = None, filesize: int = None, error: str = None) -> bool:
        """Update download status"""
        if not url and not download_id:
            return False
        
        updates = []
        params = []
        
        if status:
            updates.append("status = ?")
            params.append(status)
        if filepath:
            updates.append("filepath = ?")
            params.append(filepath)
        if filesize is not None:
            updates.append("filesize = ?")
            params.append(filesize)
        if error is not None:
            updates.append("error_message = ?")
            params.append(error)
        
        if not updates:
            return False
        
        updates.append("download_date = CURRENT_TIMESTAMP")
        
        where = "id = ?" if download_id else "url = ?"
        params.append(download_id if download_id else url)
        
        with self._lock:
            with self._cursor() as c:
                c.execute(f"UPDATE downloads SET {', '.join(updates)} WHERE {where}", params)
                return c.rowcount > 0
    
    def delete_download(self, download_id: int = None, url: str = None) -> bool:
        """Delete download"""
        with self._lock:
            with self._cursor() as c:
                if download_id:
                    c.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
                elif url:
                    c.execute("DELETE FROM downloads WHERE url = ?", (url,))
                else:
                    return False
                return c.rowcount > 0
    
    def search_downloads(self, query: str, limit=50) -> List[DownloadRecord]:
        """Search downloads"""
        term = f"%{query}%"
        with self._cursor() as c:
            c.execute("""
                SELECT * FROM downloads 
                WHERE title LIKE ? OR uploader LIKE ? OR description LIKE ?
                ORDER BY download_date DESC LIMIT ?
            """, (term, term, term, limit))
            return [DownloadRecord.from_row(row) for row in c.fetchall()]
    
    def get_failed_downloads(self) -> List[DownloadRecord]:
        """Get failed downloads"""
        return self.get_downloads(status='failed', limit=1000)
    
    def clear_failed_downloads(self) -> int:
        """Delete failed downloads"""
        with self._lock:
            with self._cursor() as c:
                c.execute("DELETE FROM downloads WHERE status = 'failed'")
                return c.rowcount
    
    # ==================== STATISTICS ====================
    
    def get_statistics(self) -> Dict:
        """Get statistics"""
        with self._cursor() as c:
            # Consolidated aggregate statistics in single query
            c.execute("""
                SELECT 
                    COUNT(CASE WHEN status = 'complete' THEN 1 END) as total_downloads,
                    COALESCE(SUM(CASE WHEN status = 'complete' THEN filesize END), 0) as total_size,
                    COALESCE(SUM(CASE WHEN status = 'complete' THEN duration END), 0) as total_duration,
                    COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed_downloads
                FROM downloads
            """)
            row = c.fetchone()
            total_dl = row['total_downloads']
            total_size = row['total_size']
            total_dur = row['total_duration']
            failed_dl = row['failed_downloads']
            
            c.execute("""
                SELECT platform, COUNT(*) as n FROM downloads 
                WHERE status = 'complete' AND platform IS NOT NULL AND platform != ''
                GROUP BY platform ORDER BY n DESC
            """)
            by_platform = {r['platform']: r['n'] for r in c.fetchall()}
            
            return {
                'total_downloads': total_dl,
                'total_size_bytes': total_size,
                'total_size_human': format_size(total_size),
                'total_duration': format_duration(total_dur),
                'by_platform': by_platform,
                'failed_downloads': failed_dl,
            }
    
    # ==================== QUEUE ====================
    
    def add_to_queue(self, url: str, priority: int = 0) -> int:
        """Add to queue"""
        with self._lock:
            with self._cursor() as c:
                c.execute("INSERT INTO queue (url, priority, status) VALUES (?, ?, 'pending')", (url, priority))
                return c.lastrowid
    
    def get_queue(self, status: str = "pending") -> List[QueueItem]:
        """Get queue items"""
        with self._cursor() as c:
            c.execute("SELECT * FROM queue WHERE status = ? ORDER BY priority DESC, added_date ASC", (status,))
            return [QueueItem(
                id=r['id'],
                url=r['url'],
                priority=r['priority'],
                status=r['status'],
                added_date=r['added_date']
            ) for r in c.fetchall()]
    
    def get_next_queue_item(self) -> Optional[QueueItem]:
        """Get next pending item"""
        items = self.get_queue("pending")
        return items[0] if items else None
    
    def update_queue_status(self, queue_id: int, status: str) -> bool:
        """Update queue status"""
        with self._lock:
            with self._cursor() as c:
                c.execute("UPDATE queue SET status = ? WHERE id = ?", (status, queue_id))
                return c.rowcount > 0
    
    def remove_from_queue(self, queue_id: int) -> bool:
        """Remove from queue"""
        with self._lock:
            with self._cursor() as c:
                c.execute("DELETE FROM queue WHERE id = ?", (queue_id,))
                return c.rowcount > 0
    
    def clear_queue(self, status: str = None) -> int:
        """Clear queue"""
        with self._lock:
            with self._cursor() as c:
                if status:
                    c.execute("DELETE FROM queue WHERE status = ?", (status,))
                else:
                    c.execute("DELETE FROM queue")
                return c.rowcount
    
    def get_queue_count(self, status: str = "pending") -> int:
        """Count queue items"""
        with self._cursor() as c:
            c.execute("SELECT COUNT(*) as n FROM queue WHERE status = ?", (status,))
            return c.fetchone()['n']
    
    # ==================== ACTIVE TASKS (Chunked Download Management) ====================
    
    def add_task(self, url: str, filepath: str, filename: str = '',
                 total_size: int = 0, chunks_json: str = '[]',
                 headers_json: str = '{}', thread_count: int = 8,
                 etag: str = '', status: str = 'pending') -> int:
        """Add an active download task for pause/resume tracking."""
        now = datetime.now().isoformat()
        with self._lock:
            with self._cursor() as c:
                c.execute("""
                    INSERT INTO active_tasks
                        (url, filepath, filename, total_size, downloaded,
                         chunks_json, status, headers_json, thread_count,
                         etag, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, '', ?, ?)
                """, (url, filepath, filename, total_size,
                      chunks_json, status, headers_json, thread_count,
                      etag, now, now))
                return c.lastrowid
    
    def update_task(self, task_id: int, **kwargs) -> bool:
        """Update any subset of columns on an active task."""
        if not kwargs:
            return False
        allowed = {
            'url', 'filepath', 'filename', 'total_size', 'downloaded',
            'chunks_json', 'status', 'headers_json', 'thread_count',
            'etag', 'error',
        }
        cols = {k: v for k, v in kwargs.items() if k in allowed}
        if not cols:
            return False
        cols['updated_at'] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        values = list(cols.values()) + [task_id]
        with self._lock:
            with self._cursor() as c:
                c.execute(f"UPDATE active_tasks SET {set_clause} WHERE id = ?", values)
                return c.rowcount > 0
    
    def get_task(self, task_id: int) -> Optional[Dict]:
        """Get a single active task by ID."""
        with self._cursor() as c:
            c.execute("SELECT * FROM active_tasks WHERE id = ?", (task_id,))
            row = c.fetchone()
            return dict(row) if row else None
    
    def get_all_tasks(self, status: str = None) -> List[Dict]:
        """Get all active tasks, optionally filtered by status."""
        with self._cursor() as c:
            if status:
                c.execute("SELECT * FROM active_tasks WHERE status = ? ORDER BY updated_at DESC", (status,))
            else:
                c.execute("SELECT * FROM active_tasks ORDER BY updated_at DESC")
            return [dict(r) for r in c.fetchall()]
    
    def get_paused_tasks(self) -> List[Dict]:
        """Shorthand to get all paused tasks."""
        return self.get_all_tasks(status='paused')
    
    def delete_task(self, task_id: int) -> bool:
        """Delete an active task."""
        with self._lock:
            with self._cursor() as c:
                c.execute("DELETE FROM active_tasks WHERE id = ?", (task_id,))
                return c.rowcount > 0


# Global instance
_db: Optional[DatabaseManager] = None

def get_database(db_path: str = None) -> DatabaseManager:
    global _db
    if _db is None:
        _db = DatabaseManager(db_path)
    return _db

def close_database():
    global _db
    if _db:
        _db.close()
        _db = None


if __name__ == "__main__":
    print("Database Module Test")
    print("=" * 50)
    
    db = DatabaseManager()
    
    # Test add
    print("\nAdding test download...")
    rec = DownloadRecord(
        url="https://test.com/video123",
        platform="YouTube",
        title="Test Video",
        uploader="Test User",
        duration=180,
        quality="1080p",
        filesize=50*1024*1024,
        status="complete"
    )
    did = db.add_download(rec)
    print(f"  ID: {did}")
    
    # Test queue
    print("\nAdding to queue...")
    qid = db.add_to_queue("https://test.com/queue_test")
    print(f"  Queue ID: {qid}")
    
    # Stats
    print("\nStatistics:")
    stats = db.get_statistics()
    for k, v in stats.items():
        print(f"  {k}: {v}")
    
    db.close()
    print("\nDone!")