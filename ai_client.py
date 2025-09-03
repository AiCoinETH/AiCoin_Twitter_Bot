# dedup.py
import os, sqlite3, hashlib, time
from typing import Optional, Tuple

DEFAULT_DB = os.getenv("DEDUP_DB_PATH", "dedup.db")

class Dedup:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY,
            created_at INTEGER NOT NULL,
            text_hash TEXT,
            img_hash  TEXT,
            vid_hash  TEXT,
            platform  TEXT,
            text_len  INTEGER,
            src_url   TEXT,
            note      TEXT
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_text ON posts(text_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_img  ON posts(img_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_posts_vid  ON posts(vid_hash)")
        con.commit()
        con.close()

    @staticmethod
    def _hash_text(text: Optional[str]) -> Optional[str]:
        if not text: return None
        normalized = " ".join(text.lower().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_bytes(data: Optional[bytes]) -> Optional[str]:
        if not data: return None
        return hashlib.sha256(data).hexdigest()

    def check(self, *, text: Optional[str], img_bytes: Optional[bytes] = None,
              vid_bytes: Optional[bytes] = None, within_days: int = 15) -> Tuple[bool, Optional[str]]:
        cutoff = int(time.time()) - within_days * 86400
        th = self._hash_text(text)
        ih = self._hash_bytes(img_bytes)
        vh = self._hash_bytes(vid_bytes)

        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("""
            SELECT created_at, platform, src_url
            FROM posts
            WHERE created_at >= ?
              AND (
                    (text_hash IS NOT NULL AND text_hash = ?)
                 OR (? IS NOT NULL AND img_hash = ?)
                 OR (? IS NOT NULL AND vid_hash = ?)
              )
            ORDER BY created_at DESC
            LIMIT 1
        """, (cutoff, th, ih, ih, vh, vh))
        row = cur.fetchone()
        con.close()
        if row:
            ts, platform, src_url = row
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))
            return True, f"duplicate within {within_days}d (was {when} UTC, platform={platform or '-'})"
        return False, None

    def remember(self, *, text: Optional[str], img_bytes: Optional[bytes] = None,
                 vid_bytes: Optional[bytes] = None, platform: Optional[str] = None,
                 src_url: Optional[str] = None, note: Optional[str] = None):
        th = self._hash_text(text)
        ih = self._hash_bytes(img_bytes)
        vh = self._hash_bytes(vid_bytes)
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("""
            INSERT INTO posts(created_at, text_hash, img_hash, vid_hash, platform, text_len, src_url, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (int(time.time()), th, ih, vh, platform, len(text or ""), src_url, note))
        con.commit()
        con.close()

    def purge(self, older_than_days: int = 30) -> int:
        cutoff = int(time.time()) - older_than_days * 86400
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("DELETE FROM posts WHERE created_at < ?", (cutoff,))
        n = cur.rowcount
        con.commit()
        con.close()
        return n