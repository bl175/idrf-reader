import contextlib
import os
import secrets
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readers(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('primary', 'second')),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  folder TEXT UNIQUE NOT NULL,
  patient TEXT,
  timepoint TEXT,
  double_read INTEGER NOT NULL DEFAULT 0,
  series_hint TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  message TEXT,
  meta TEXT,
  sig TEXT,
  imported_at TEXT,
  source TEXT NOT NULL DEFAULT 'local'
);
CREATE TABLE IF NOT EXISTS reads(
  reader_id INTEGER NOT NULL REFERENCES readers(id),
  case_id INTEGER NOT NULL REFERENCES cases(id),
  answers TEXT NOT NULL DEFAULT '{}',
  confidence INTEGER,
  notes TEXT NOT NULL DEFAULT '',
  seconds REAL NOT NULL DEFAULT 0,
  saved INTEGER NOT NULL DEFAULT 0,
  saved_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (reader_id, case_id)
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  reader_id INTEGER,
  is_admin INTEGER NOT NULL DEFAULT 0,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  who TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);
"""

_path = None
# WAL needs shared memory, which network file shares (e.g. Azure App Service /home) do not support well.
JOURNAL_MODE = os.environ.get("SQLITE_JOURNAL_MODE", "WAL").upper()


def init(path):
    global _path
    _path = str(path)
    with connect() as c:
        c.executescript(SCHEMA)
        if "source" not in {r["name"] for r in c.execute("PRAGMA table_info(cases)")}:
            c.execute("ALTER TABLE cases ADD COLUMN source TEXT NOT NULL DEFAULT 'local'")
        if not c.execute("SELECT 1 FROM meta WHERE key='order_salt'").fetchone():
            c.execute("INSERT INTO meta(key, value) VALUES('order_salt', ?)", (secrets.token_hex(16),))


@contextlib.contextmanager
def connect():
    c = sqlite3.connect(_path, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def meta_get(c, key, default=None):
    r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default
