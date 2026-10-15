import json
import sqlite3
import threading

from .config import DB_PATH, ensure_dirs

COLUMNS = [
    ("id", "TEXT PRIMARY KEY"),
    ("url", "TEXT NOT NULL"),
    ("kind", "TEXT NOT NULL DEFAULT 'file'"),
    ("filename", "TEXT"),
    ("directory", "TEXT"),
    ("path", "TEXT"),
    ("total", "INTEGER"),
    ("downloaded", "INTEGER DEFAULT 0"),
    ("status", "TEXT NOT NULL DEFAULT 'queued'"),
    ("error", "TEXT"),
    ("category", "TEXT"),
    ("created", "REAL"),
    ("finished", "REAL"),
    ("headers", "TEXT"),
    ("options", "TEXT"),
    ("segments", "TEXT"),
    ("etag", "TEXT"),
    ("last_modified", "TEXT"),
    ("resumable", "INTEGER DEFAULT 0"),
    ("queue_pos", "INTEGER DEFAULT 0"),
    ("page_url", "TEXT"),
    ("sha256", "TEXT"),
    ("vt", "TEXT"),
    ("elapsed", "REAL"),
    ("post", "TEXT"),
]
JSON_FIELDS = {"headers", "options", "segments", "vt", "post"}


class Store:
    def __init__(self, path=None):
        ensure_dirs()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path or DB_PATH), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        cols = ", ".join(f"{n} {t}" for n, t in COLUMNS)
        self._db.execute(f"CREATE TABLE IF NOT EXISTS downloads ({cols})")
        existing = {r[1] for r in self._db.execute("PRAGMA table_info(downloads)")}
        for name, typ in COLUMNS:
            if name not in existing:
                self._db.execute(f"ALTER TABLE downloads ADD COLUMN {name} {typ.replace('PRIMARY KEY', '')}")

    @staticmethod
    def _encode(fields):
        out = {}
        for k, v in fields.items():
            if k in JSON_FIELDS and v is not None and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                v = int(v)
            out[k] = v
        return out

    @staticmethod
    def _decode(row):
        d = dict(row)
        for k in JSON_FIELDS:
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    d[k] = None
        d["resumable"] = bool(d.get("resumable"))
        return d

    def insert(self, rec):
        rec = self._encode({k: v for k, v in rec.items() if k in dict(COLUMNS)})
        keys = ", ".join(rec)
        marks = ", ".join("?" for _ in rec)
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO downloads ({keys}) VALUES ({marks})", list(rec.values()))

    def update(self, did, **fields):
        fields = self._encode({k: v for k, v in fields.items() if k in dict(COLUMNS) and k != "id"})
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE downloads SET {sets} WHERE id=?", [*fields.values(), did])

    def delete(self, did):
        with self._lock:
            self._db.execute("DELETE FROM downloads WHERE id=?", (did,))

    def all(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM downloads ORDER BY queue_pos, created").fetchall()
        return [self._decode(r) for r in rows]

    def close(self):
        with self._lock:
            self._db.close()
