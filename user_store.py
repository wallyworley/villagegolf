"""
SQLite-backed user profile store.

Replaces the Firestore backend so the app has no external cloud dependency and
runs self-contained on a single host (e.g. a VPS). Concurrency is handled by
SQLite's WAL mode plus a busy timeout; the volume here is tiny (a handful of
golfers), so a fresh connection per call keeps the threading model trivial.

Profile shape (one row per TVN username, stored as a JSON blob in `data`):
    tvn_password, golf_password, display_name,
    primary {id, name, initials}, buddies [{id, name, initials}], email
"""

import json
import os
import sqlite3
import threading
from contextlib import closing

_DB_PATH = os.environ.get("USER_DB_PATH", "users.db")
_lock = threading.Lock()
_initialized = False


def _connect():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ensure_schema():
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        with closing(_connect()) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "  username TEXT PRIMARY KEY,"
                "  data TEXT NOT NULL"
                ")"
            )
        _initialized = True


def get_user(username):
    """Return the profile dict for `username`, or None if not found."""
    if not username:
        return None
    _ensure_schema()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT data FROM users WHERE username = ?", (username,)
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def set_user(username, data):
    """Insert or replace the profile for `username`."""
    if not username:
        return
    _ensure_schema()
    blob = json.dumps(data or {})
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO users (username, data) VALUES (?, ?) "
            "ON CONFLICT(username) DO UPDATE SET data = excluded.data",
            (username, blob),
        )


def delete_user(username):
    """Delete the profile for `username` (no-op if absent)."""
    if not username:
        return
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))


def all_users():
    """Return {username: data} for every registered profile."""
    _ensure_schema()
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT username, data FROM users").fetchall()
    out = {}
    for username, blob in rows:
        try:
            out[username] = json.loads(blob) or {}
        except (ValueError, TypeError):
            out[username] = {}
    return out
