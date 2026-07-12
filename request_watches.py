"""
Request-result watches.

The Villages assigns tee-time requests by a points lottery at 12-1 AM, 3 days
before the play date, and the golfer has to check back afterward to see whether
their request became a reservation. This module records each request to watch
and when to check it; a background loop in app.py runs the due checks (scrape
reservations, see if one matches the request's play date) and emails the golfer
the outcome.

Stored in the same SQLite DB file as user_store, but a separate table. No
credentials are stored here (only request metadata + the notify email), so no
encryption is needed.
"""

import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_DB_PATH = os.environ.get("USER_DB_PATH", "users.db")
_lock = threading.Lock()
_initialized = False

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc
_FMT = "%Y-%m-%d %H:%M:%S"  # naive-UTC string; sorts lexically = chronologically

# When to check, in ET, on the assignment day (3 days before play). The lottery
# runs 12-1 AM ET, so 1:30 AM leaves margin for it to finish.
_CHECK_HOUR = 1
_CHECK_MINUTE = 30
_MAX_ATTEMPTS = 6  # give up (and notify) after this many failed check attempts


def _connect():
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
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
                "CREATE TABLE IF NOT EXISTS request_watches ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  username TEXT NOT NULL,"
                "  request_no TEXT,"
                "  play_date TEXT NOT NULL,"        # YYYYMMDD
                "  play_date_label TEXT,"
                "  courses TEXT,"                   # JSON list of labels
                "  check_after TEXT NOT NULL,"      # naive-UTC string
                "  status TEXT NOT NULL DEFAULT 'pending',"  # pending|checked|error|cancelled
                "  result TEXT,"                    # assigned|not_assigned|unknown
                "  result_detail TEXT,"             # JSON
                "  email TEXT,"
                "  attempts INTEGER NOT NULL DEFAULT 0,"
                "  created_at TEXT,"
                "  checked_at TEXT"
                ")"
            )
        _initialized = True


# ── time / date helpers ──────────────────────────────────────────────────────

def now_utc_str():
    return datetime.now(_UTC).strftime(_FMT)


def normalize_play_date(raw):
    """Return a YYYYMMDD string from common inputs (YYYYMMDD, YYYY-MM-DD,
    M/D/YYYY, or a label containing an M/D/YYYY), or None if unparseable."""
    s = str(raw or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return s
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{y}{int(mo):02d}{int(d):02d}"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, d, y = m.groups()
        return f"{y}{int(mo):02d}{int(d):02d}"
    return None


def friendly_date(raw):
    """Return a human label like 'Saturday, 7/18/2026' from any parseable date,
    or the original string if it can't be parsed."""
    pd = normalize_play_date(raw)
    if not pd:
        return str(raw or "")
    dt = datetime.strptime(pd, "%Y%m%d")
    return f"{dt:%A}, {dt.month}/{dt.day}/{dt.year}"


def compute_check_after(play_date_yyyymmdd):
    """UTC check time = 1:30 AM ET on (play_date - 3 days). Returns naive-UTC str."""
    d = datetime.strptime(play_date_yyyymmdd, "%Y%m%d").date()
    check_date = d - timedelta(days=3)
    local = datetime(
        check_date.year, check_date.month, check_date.day,
        _CHECK_HOUR, _CHECK_MINUTE, tzinfo=_ET,
    )
    return local.astimezone(_UTC).strftime(_FMT)


def reservation_matches(reservation_date_text, play_date_yyyymmdd):
    """True if a reservation's date text (e.g. 'Saturday: 7/18') is the play date."""
    if not reservation_date_text or not play_date_yyyymmdd:
        return False
    d = datetime.strptime(play_date_yyyymmdd, "%Y%m%d")
    md = f"{d.month}/{d.day}"                       # e.g. 7/18
    return re.search(rf"(^|\D){re.escape(md)}(\D|$)", reservation_date_text) is not None


# ── store operations ─────────────────────────────────────────────────────────

def add_watch(username, request_no, play_date, play_date_label, courses, email):
    """Enroll a request to watch. Idempotent per (username, request_no) — or
    (username, play_date) when no request number is known. Returns the row id,
    or None if a matching watch already exists or the date is unparseable."""
    if not username:
        return None
    pd = normalize_play_date(play_date)
    if not pd:
        log.warning("request_watch: unparseable play_date %r for %s", play_date, username)
        return None
    _ensure_schema()
    rn = (str(request_no).strip() or None) if request_no else None
    check_after = compute_check_after(pd)
    courses_json = json.dumps(courses or [])
    with closing(_connect()) as conn, conn:
        if rn:
            existing = conn.execute(
                "SELECT id FROM request_watches WHERE username=? AND request_no=?",
                (username, rn),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT id FROM request_watches WHERE username=? AND play_date=? AND request_no IS NULL",
                (username, pd),
            ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO request_watches "
            "(username, request_no, play_date, play_date_label, courses, check_after, "
            " status, email, created_at) "
            "VALUES (?,?,?,?,?,?,'pending',?,?)",
            (username, rn, pd, play_date_label or "", courses_json, check_after,
             (email or "").strip(), now_utc_str()),
        )
        return cur.lastrowid


def due_watches(now_str=None):
    """Return pending watches whose check time has passed (as dicts)."""
    _ensure_schema()
    now_str = now_str or now_utc_str()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM request_watches WHERE status='pending' AND check_after <= ? "
            "ORDER BY check_after",
            (now_str,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_result(watch_id, result, detail=None):
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE request_watches SET status='checked', result=?, result_detail=?, "
            "checked_at=? WHERE id=?",
            (result, json.dumps(detail) if detail is not None else None,
             now_utc_str(), watch_id),
        )


def bump_attempt(watch_id):
    """Increment the failed-attempt counter; flip to 'error' at the cap.
    Returns True if this attempt exhausted the retries (caller should notify)."""
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT attempts FROM request_watches WHERE id=?", (watch_id,)
        ).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        exhausted = attempts >= _MAX_ATTEMPTS
        conn.execute(
            "UPDATE request_watches SET attempts=?, status=? WHERE id=?",
            (attempts, "error" if exhausted else "pending", watch_id),
        )
    return exhausted


def cancel_watch(watch_id):
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE request_watches SET status='cancelled' WHERE id=?", (watch_id,)
        )


def cancel_watches_for_user(username):
    """Cancel all pending watches for a user (e.g. on account removal)."""
    if not username:
        return
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE request_watches SET status='cancelled' "
            "WHERE username=? AND status='pending'",
            (username,),
        )


def all_watches():
    _ensure_schema()
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM request_watches").fetchall()]
