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
_RETRY_SECONDS = 900
_CLAIM_STALE_SECONDS = 600  # an outbox row claimed longer ago than this is retried
_ASSIGNMENT_GRACE_SECONDS = 7200


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
                "  status TEXT NOT NULL DEFAULT 'pending',"  # pending|checked|cancelled
                "  result TEXT,"                    # assigned|not_assigned|unknown
                "  result_detail TEXT,"             # JSON
                "  email TEXT,"
                "  attempts INTEGER NOT NULL DEFAULT 0,"
                "  created_at TEXT,"
                "  checked_at TEXT"
                ")"
            )
            # Additive migration: existing watches and encrypted profiles survive.
            columns = {r[1] for r in conn.execute("PRAGMA table_info(request_watches)")}
            additions = {
                "preferences": "TEXT", "notification_status": "TEXT",
                "notification_attempts": "INTEGER NOT NULL DEFAULT 0", "notify_after": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE request_watches ADD COLUMN {name} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS request_watch_due ON request_watches(status, check_after)")
            # A second, generic outbox (additive; existing request_watches rows
            # are untouched) for notifications that have no "check" phase of
            # their own, such as a booking confirmation known synchronously at
            # booking time rather than discovered by a background check.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS notification_outbox ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  kind TEXT NOT NULL,"                       # booking_confirmation
                "  username TEXT NOT NULL,"
                "  email TEXT NOT NULL,"
                "  payload TEXT NOT NULL,"                    # JSON
                "  status TEXT NOT NULL DEFAULT 'pending',"    # pending|sent|failed|cancelled
                "  attempts INTEGER NOT NULL DEFAULT 0,"
                "  notify_after TEXT NOT NULL,"
                "  created_at TEXT"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS notification_outbox_due "
                "ON notification_outbox(status, notify_after)"
            )
            # Additive: a reference (for example a reservation number) lets a
            # scheduled row be cancelled without knowing its id.
            outbox_columns = {r[1] for r in conn.execute("PRAGMA table_info(notification_outbox)")}
            if "ref" not in outbox_columns:
                conn.execute("ALTER TABLE notification_outbox ADD COLUMN ref TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS notification_outbox_ref ON notification_outbox(kind, ref)"
            )
        _initialized = True


# ── time / date helpers ──────────────────────────────────────────────────────

def now_utc_str():
    return datetime.now(_UTC).strftime(_FMT)


def normalize_play_date(raw):
    """Return a YYYYMMDD string from common inputs (YYYYMMDD, YYYY-MM-DD,
    M/D/YYYY, or a label containing an M/D/YYYY), or None if unparseable."""
    s = str(raw or "").strip()
    value = s if re.fullmatch(r"\d{8}", s) else None
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        y, mo, d = m.groups()
        value = f"{y}{int(mo):02d}{int(d):02d}"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", s)
    if m:
        mo, d, y = m.groups()
        value = f"{y}{int(mo):02d}{int(d):02d}"
    try:
        return datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d") if value else None
    except ValueError:
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
    expected = normalize_play_date(play_date_yyyymmdd)
    actual = normalize_play_date(reservation_date_text)
    if actual:
        return actual == expected
    if not expected:
        return False
    d = datetime.strptime(expected, "%Y%m%d")
    return re.search(rf"(?<!\d)0?{d.month}/0?{d.day}(?![\d/])", str(reservation_date_text)) is not None


def matching_reservation(watch, reservations):
    """Return one reservation supported by request details, never date alone.

    Even a match is described as 'matching reservation found', not proof that
    this request won the lottery. Missing/ambiguous evidence stays unknown.
    """
    from course_metadata import lookup_course_metadata
    try:
        courses = json.loads(watch.get("courses") or "[]")
        prefs = json.loads(watch.get("preferences") or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(courses, list) or not isinstance(prefs, dict):
        return None
    wanted = {(lookup_course_metadata(c)[0] or str(c)).casefold() for c in courses}
    found = []
    for reservation in reservations:
        if not reservation_matches(reservation.get("play_date") or reservation.get("date"), watch["play_date"]):
            continue
        # A real linkage, when supplied by the upstream site, is strongest.
        linked = reservation.get("request_no") and str(reservation["request_no"]) == str(watch.get("request_no"))
        name = reservation.get("course_name") or reservation.get("course") or ""
        canonical = (lookup_course_metadata(name)[0] or name).casefold()
        if not linked and (not wanted or canonical not in wanted or prefs.get("any_course")):
            continue
        display = reservation.get("display_time") or reservation.get("time") or ""
        if not linked and (prefs.get("earliest_time") or prefs.get("latest_time")):
            match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*([AP]M)\s*", display, re.I)
            if not match:
                continue
            h, minute, ampm = match.groups()
            value = f"{int(h) % 12 + (12 if ampm.upper() == 'PM' else 0):02d}:{minute}"
            if prefs.get("earliest_time") and value < prefs["earliest_time"]:
                continue
            if prefs.get("latest_time") and value > prefs["latest_time"]:
                continue
        if reservation.get("golfer_ids") and prefs.get("golfer_ids"):
            if set(map(str, reservation["golfer_ids"])) != set(map(str, prefs["golfer_ids"])):
                continue
        found.append(reservation)
    return found[0] if len(found) == 1 else None


def assignment_grace_passed(watch, now=None):
    check = datetime.strptime(compute_check_after(watch["play_date"]), _FMT).replace(tzinfo=_UTC)
    return (now or datetime.now(_UTC)) >= check + timedelta(seconds=_ASSIGNMENT_GRACE_SECONDS)


# ── store operations ─────────────────────────────────────────────────────────

def add_watch(username, request_no, play_date, play_date_label, courses, email, preferences=None):
    """Enroll a request to watch. Idempotent per (username, request_no): or
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
        conn.execute("BEGIN IMMEDIATE")
        if rn:
            existing = conn.execute(
                "SELECT id FROM request_watches WHERE username=? AND request_no=? AND status='pending'",
                (username, rn),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT id FROM request_watches WHERE username=? AND play_date=? AND request_no IS NULL "
                "AND status='pending'",
                (username, pd),
            ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO request_watches "
            "(username, request_no, play_date, play_date_label, courses, check_after, "
            " status, email, created_at, preferences) "
            "VALUES (?,?,?,?,?,?,'pending',?,?,?)",
            (username, rn, pd, play_date_label or "", courses_json, check_after,
             (email or "").strip(), now_utc_str(), json.dumps(preferences or {})),
        )
        return cur.lastrowid


def due_watches(now_str=None, limit=None):
    """Return pending watches whose check time has passed (as dicts).

    With `limit`, at most that many are returned (oldest check_after first)
    so one pass never floods the shared golf worker; any remaining due
    watches are picked up on the caller's next pass.
    """
    _ensure_schema()
    now_str = now_str or now_utc_str()
    query = "SELECT * FROM request_watches WHERE status='pending' AND check_after <= ? ORDER BY check_after"
    params = [now_str]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def mark_result(watch_id, result, detail=None):
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE request_watches SET status='checked', result=?, result_detail=?, "
            "checked_at=?, notification_status='pending', notify_after=? WHERE id=? AND status='pending'",
            (result, json.dumps(detail) if detail is not None else None,
             now_utc_str(), now_utc_str(), watch_id),
        )
        return bool(cur.rowcount)


def bump_attempt(watch_id, allow_exhaust=True):
    """Increment the failed-attempt counter.
    Returns True if this attempt exhausted the retries (caller should notify)."""
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts FROM request_watches WHERE id=? AND status='pending'", (watch_id,)
        ).fetchone()
        if not row:
            return False
        attempts = (row["attempts"] if row else 0) + 1
        exhausted = allow_exhaust and attempts >= _MAX_ATTEMPTS
        later = (datetime.now(_UTC) + timedelta(seconds=_RETRY_SECONDS)).strftime(_FMT)
        conn.execute(
            "UPDATE request_watches SET attempts=?, check_after=? WHERE id=? AND status='pending'",
            (attempts, later, watch_id),
        )
    return exhausted


def cancel_watch(watch_id):
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE request_watches SET status='cancelled', notification_status='cancelled' WHERE id=?", (watch_id,)
        )


def cancel_watches_for_user(username):
    """Cancel all pending watches for a user (e.g. on account removal)."""
    if not username:
        return
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE request_watches SET status='cancelled', notification_status='cancelled' "
            "WHERE username=? AND (status='pending' OR notification_status='pending')",
            (username,),
        )


def cancel_request_watch(username, request_no):
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE request_watches SET status='cancelled', notification_status='cancelled' "
            "WHERE username=? AND request_no=?", (username, str(request_no)),
        )


def due_notifications():
    _ensure_schema()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM request_watches WHERE status='checked' AND notification_status='pending' "
            "AND notify_after<=? ORDER BY notify_after LIMIT 20", (now_utc_str(),),
        ).fetchall()
    return [dict(row) for row in rows]


def finish_notification(watch_id, sent):
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT notification_attempts FROM request_watches WHERE id=? "
            "AND notification_status='pending' AND status='checked'", (watch_id,),
        ).fetchone()
        if not row:
            return
        attempts = row[0] + 1
        state = "sent" if sent else ("failed" if attempts >= _MAX_ATTEMPTS else "pending")
        later = (datetime.now(_UTC) + timedelta(seconds=_RETRY_SECONDS)).strftime(_FMT)
        conn.execute(
            "UPDATE request_watches SET notification_status=?, notification_attempts=?, notify_after=? WHERE id=?",
            (state, attempts, later, watch_id),
        )


def all_watches():
    _ensure_schema()
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM request_watches").fetchall()]


# ── notification outbox (generic; not tied to a check-loop watch) ───────────

def enqueue_notification(kind, username, email, payload, notify_after=None, ref=None):
    """Queue a notification for durable, retryable delivery. Returns the row id.

    `notify_after` is an aware UTC datetime (a naive one is read as UTC) or None
    for "send now". `ref` is an optional lookup key for cancel_outbox_ref."""
    _ensure_schema()
    due = now_utc_str()
    if notify_after is not None:
        if notify_after.tzinfo is None:
            notify_after = notify_after.replace(tzinfo=_UTC)
        due = notify_after.astimezone(_UTC).strftime(_FMT)
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO notification_outbox "
            "(kind, username, email, payload, status, attempts, notify_after, created_at, ref) "
            "VALUES (?,?,?,?,'pending',0,?,?,?)",
            (kind, username, email, json.dumps(payload), due, now_utc_str(), ref),
        )
        return cur.lastrowid


def claim_outbox(outbox_id):
    """Atomically claim one pending row for sending, so two senders (the
    immediate post-booking thread and the loop's own pass) can never both
    act on the same row. Returns True if this call won the claim (status
    flips pending -> sending); False if it was already claimed, already
    settled, or does not exist. A 'sending' row whose claim is older than
    _CLAIM_STALE_SECONDS can be claimed again, so a sender that died mid-send
    does not leave the row stuck."""
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        stale_before = (datetime.now(_UTC) - timedelta(seconds=_CLAIM_STALE_SECONDS)).strftime(_FMT)
        cur = conn.execute(
            "UPDATE notification_outbox SET status='sending', notify_after=? "
            "WHERE id=? AND (status='pending' OR (status='sending' AND notify_after<=?))",
            (now_utc_str(), outbox_id, stale_before),
        )
        return cur.rowcount == 1


def due_outbox(limit=20, kinds=None, exclude_kinds=None):
    """Return outbox rows ready to send: pending rows whose notify_after has
    passed, plus 'sending' rows claimed more than _CLAIM_STALE_SECONDS ago
    (a sender that crashed or was killed mid-send leaves its claim behind,
    so those rows are retried instead of stuck forever). `kinds` limits the
    result to those kinds and `exclude_kinds` leaves those kinds out, so each
    kind has exactly one sender."""
    _ensure_schema()
    now_str = now_utc_str()
    stale_before = (datetime.now(_UTC) - timedelta(seconds=_CLAIM_STALE_SECONDS)).strftime(_FMT)
    sql = ("SELECT * FROM notification_outbox WHERE "
           "((status='pending' AND notify_after<=?) OR (status='sending' AND notify_after<=?))")
    args = [now_str, stale_before]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args.extend(sorted(kinds))
    if exclude_kinds:
        sql += " AND kind NOT IN (%s)" % ",".join("?" * len(exclude_kinds))
        args.extend(sorted(exclude_kinds))
    sql += " ORDER BY notify_after LIMIT ?"
    args.append(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(row) for row in rows]


def outbox_rows(ids):
    """Return the outbox rows with these ids (any status), oldest id first."""
    ids = [int(i) for i in ids or []]
    if not ids:
        return []
    _ensure_schema()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM notification_outbox WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(ids)), ids
        ).fetchall()
    return [dict(row) for row in rows]


def finish_outbox(outbox_id, sent, exhaust=False):
    """Record the outcome of one delivery attempt. Operates on rows still in
    ('pending', 'sending'); a row already 'cancelled', 'sent', or 'failed'
    is left alone, so a late finish from a stale-claim retry can never
    resurrect or overwrite a row another path already settled.
    sent -> 'sent'; failure -> attempts+1, then 'failed' when `exhaust` is
    True or attempts reach _MAX_ATTEMPTS, else 'pending' with notify_after
    pushed by _RETRY_SECONDS."""
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempts FROM notification_outbox WHERE id=? AND status IN ('pending', 'sending')",
            (outbox_id,),
        ).fetchone()
        if not row:
            return
        attempts = row[0] + 1
        state = "sent" if sent else ("failed" if exhaust or attempts >= _MAX_ATTEMPTS else "pending")
        later = (datetime.now(_UTC) + timedelta(seconds=_RETRY_SECONDS)).strftime(_FMT)
        conn.execute(
            "UPDATE notification_outbox SET status=?, attempts=?, notify_after=? WHERE id=?",
            (state, attempts, later, outbox_id),
        )


def cancel_outbox_for_user(username):
    """Cancel all still-active (pending or in-flight) outbox rows for a user
    (e.g. on account removal)."""
    if not username:
        return
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE notification_outbox SET status='cancelled' WHERE username=? AND status IN ('pending', 'sending')",
            (username,),
        )


def cancel_outbox_ref(kind, ref):
    """Cancel still-active (pending or in-flight) rows of one kind with one ref.
    Returns how many rows were cancelled."""
    if not kind or not ref:
        return 0
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE notification_outbox SET status='cancelled' "
            "WHERE kind=? AND ref=? AND status IN ('pending', 'sending')",
            (kind, str(ref)),
        )
        return cur.rowcount


def cancel_outbox(outbox_id):
    """Cancel one still-active outbox row. Settled rows are left alone."""
    _ensure_schema()
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE notification_outbox SET status='cancelled' "
            "WHERE id=? AND status IN ('pending', 'sending')",
            (outbox_id,),
        )


def defer_outbox(outbox_id, seconds):
    """Put a claimed row back to pending and push its due time out by `seconds`,
    without counting a delivery attempt."""
    _ensure_schema()
    later = (datetime.now(_UTC) + timedelta(seconds=seconds)).strftime(_FMT)
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE notification_outbox SET status='pending', notify_after=? "
            "WHERE id=? AND status IN ('pending', 'sending')",
            (later, outbox_id),
        )
