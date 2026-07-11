"""
SQLite-backed user profile store.

Replaces the Firestore backend so the app has no external cloud dependency and
runs self-contained on a single host (e.g. a VPS). Concurrency is handled by
SQLite's WAL mode plus a busy timeout; the volume here is tiny (a handful of
golfers), so a fresh connection per call keeps the threading model trivial.

Profile shape (one row per TVN username, stored as a JSON blob in `data`):
    tvn_password, golf_password, display_name,
    primary {id, name, initials}, buddies [{id, name, initials}], email

Encryption at rest
------------------
The `data` blob holds Villages credentials (the resident's master
thevillages.net password + golf PIN), so it is encrypted with Fernet
(AES-128-CBC + HMAC) before it touches disk. This protects a leaked DB file or
backup. Encryption is MANDATORY: if USER_DB_ENCRYPTION_KEY is unset the store
refuses to operate, unless ALLOW_PLAINTEXT_USER_DB=1 is set for local dev.

Reads are backward-compatible: a row that isn't a valid token is treated as
legacy plaintext JSON, so an existing unencrypted DB keeps working and is
upgraded to ciphertext on its next write. To upgrade every existing row in one
pass, run:

    USER_DB_ENCRYPTION_KEY=<key> python user_store.py reencrypt

Generate a key with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import json
import logging
import os
import sqlite3
import threading
from contextlib import closing

log = logging.getLogger(__name__)

_DB_PATH = os.environ.get("USER_DB_PATH", "users.db")
_lock = threading.Lock()
_initialized = False

# ── Optional encryption ──────────────────────────────────────────────────────
_fernet = None
_warned_plaintext = False


_ENCRYPTION_REQUIRED_MSG = (
    "USER_DB_ENCRYPTION_KEY is not set. User profiles hold Villages credentials "
    "and must be encrypted at rest. Generate a key with "
    '`python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"` and set USER_DB_ENCRYPTION_KEY, or '
    "set ALLOW_PLAINTEXT_USER_DB=1 for local dev only."
)


def _plaintext_allowed():
    return os.environ.get("ALLOW_PLAINTEXT_USER_DB") == "1"


def _get_fernet():
    """Return a Fernet instance if a key is configured, else None.

    Raises if no key is set and plaintext storage was not explicitly allowed
    (ALLOW_PLAINTEXT_USER_DB=1), so production cannot silently write cleartext
    credentials to disk.
    """
    global _fernet, _warned_plaintext
    if _fernet is not None:
        return _fernet
    key = (os.environ.get("USER_DB_ENCRYPTION_KEY") or "").strip()
    if not key:
        if not _plaintext_allowed():
            raise RuntimeError(_ENCRYPTION_REQUIRED_MSG)
        if not _warned_plaintext:
            log.warning(
                "USER_DB_ENCRYPTION_KEY not set and ALLOW_PLAINTEXT_USER_DB=1 — "
                "user profiles are stored as PLAINTEXT. Local dev only; never in "
                "production."
            )
            _warned_plaintext = True
        return None
    from cryptography.fernet import Fernet
    _fernet = Fernet(key.encode())
    return _fernet


def verify_encryption_config():
    """Fail fast at startup if credentials-at-rest encryption isn't configured.

    Call once at app boot so a missing key surfaces immediately rather than on
    the first user's login/registration.
    """
    _get_fernet()  # raises if no key and ALLOW_PLAINTEXT_USER_DB != "1"


def _encode(data):
    """Serialize a profile dict to the on-disk string (encrypted if keyed)."""
    blob = json.dumps(data or {})
    f = _get_fernet()
    if f is None:
        return blob
    return f.encrypt(blob.encode()).decode()


def _decode(stored):
    """Parse an on-disk string back to a dict, tolerating legacy plaintext."""
    if stored is None:
        return None
    f = _get_fernet()
    if f is not None:
        try:
            from cryptography.fernet import InvalidToken
            try:
                return json.loads(f.decrypt(stored.encode()).decode())
            except InvalidToken:
                pass  # legacy plaintext row written before encryption — fall through
        except Exception:
            pass
    # Plaintext (no key configured, or legacy row)
    try:
        return json.loads(stored)
    except (ValueError, TypeError):
        return None


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
    return _decode(row[0])


def set_user(username, data):
    """Insert or replace the profile for `username`."""
    if not username:
        return
    _ensure_schema()
    blob = _encode(data)
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
        out[username] = _decode(blob) or {}
    return out


def reencrypt_all():
    """Rewrite every row through the current codec.

    With USER_DB_ENCRYPTION_KEY set, legacy plaintext rows are read via the
    decode fallback and rewritten as ciphertext. Idempotent — already-encrypted
    rows round-trip unchanged. Returns the number of rows rewritten.
    """
    _ensure_schema()
    rewritten = 0
    with closing(_connect()) as conn, conn:
        rows = conn.execute("SELECT username, data FROM users").fetchall()
        for username, stored in rows:
            data = _decode(stored)
            if data is None:
                log.warning("reencrypt: skipping unreadable row for %r", username)
                continue
            conn.execute(
                "UPDATE users SET data = ? WHERE username = ?",
                (_encode(data), username),
            )
            rewritten += 1
    return rewritten


if __name__ == "__main__":
    import sys

    logging.basicConfig(level="INFO")
    if len(sys.argv) >= 2 and sys.argv[1] == "reencrypt":
        if not (os.environ.get("USER_DB_ENCRYPTION_KEY") or "").strip():
            sys.exit(
                "Refusing to re-encrypt: USER_DB_ENCRYPTION_KEY is not set. "
                "Set the key first, then re-run."
            )
        n = reencrypt_all()
        print(f"Re-encrypted {n} row(s) in {_DB_PATH}.")
    else:
        sys.exit("usage: python user_store.py reencrypt")
