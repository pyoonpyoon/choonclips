"""Tiny SQLite layer. One file, no ORM."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source TEXT,
  path TEXT,
  duration REAL,
  width INTEGER,
  height INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',   -- queued|downloading|transcribing|analyzing|rendering|ready|error
  progress REAL DEFAULT 0,
  message TEXT,
  error TEXT,
  options TEXT,                             -- json
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  rank INTEGER,
  start REAL NOT NULL,
  end REAL NOT NULL,
  score REAL,
  title TEXT,
  hook TEXT,
  reason TEXT,
  transcript TEXT,
  meta TEXT,                                -- json: per-platform captions, hashtags, signals
  path TEXT,
  thumb TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending|rendering|ready|error
  error TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
  platform TEXT PRIMARY KEY,
  account_id TEXT,
  account_name TEXT,
  access_token TEXT,
  refresh_token TEXT,
  expires_at REAL,
  extra TEXT,
  connected_at REAL
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,
  clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  caption TEXT,
  status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled|posting|posted|error|canceled
  scheduled_at REAL NOT NULL,
  posted_at REAL,
  remote_id TEXT,
  url TEXT,
  error TEXT,
  attempts INTEGER DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  verifier TEXT,
  created_at REAL NOT NULL
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            settings.ensure_dirs()
            _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
        return _conn


def reset_for_tests() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    with _lock:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("meta", "options", "extra"):
        if k in d and isinstance(d[k], str) and d[k]:
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with _lock:
        return [row(r) for r in connect().execute(sql, params).fetchall()]  # type: ignore[misc]


def one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    with _lock:
        return row(connect().execute(sql, params).fetchone())


def execute(sql: str, params: tuple | list = ()) -> None:
    with _lock:
        connect().execute(sql, params)


def update(table: str, id_: str, key: str = "id", **fields: Any) -> None:
    if not fields:
        return
    cols = []
    vals = []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    vals.append(id_)
    execute(f"UPDATE {table} SET {', '.join(cols)} WHERE {key}=?", vals)


def insert(table: str, **fields: Any) -> None:
    cols = ",".join(fields)
    qs = ",".join("?" for _ in fields)
    vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
    execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", vals)
