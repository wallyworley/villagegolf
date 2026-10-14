"""
Villages Golf Booking App — Flask Backend
Serves the frontend and provides API endpoints for
fetching tee times and booking via The Villages system.
"""

import hashlib
import hmac
import json
import logging
import os
import queue
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, session, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from course_metadata import BAND_KEYS
load_dotenv()  # load .env (on the VPS, systemd also injects these via EnvironmentFile)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__, template_folder="templates")
app.logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Config ────────────────────────────────────────────────────────────────────
# SECRET_KEY signs session cookies. In production it MUST be set and stable —
# a random per-boot value logs every device out on each restart. Fail hard if
# it is missing, unless ALLOW_EPHEMERAL_SECRET_KEY=1 is set (local dev only).
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if os.environ.get("ALLOW_EPHEMERAL_SECRET_KEY") == "1":
        logging.warning(
            "SECRET_KEY not set — using an ephemeral key (dev only). "
            "Sessions will not survive a restart."
        )
        SECRET_KEY = os.urandom(24).hex()
    else:
        raise RuntimeError(
            "SECRET_KEY is not set. Set a stable SECRET_KEY in the environment "
            "(see DEPLOY/.env), or set ALLOW_EPHEMERAL_SECRET_KEY=1 for local dev."
        )
app.secret_key = SECRET_KEY

# Session cookie is a ~31-day bearer token, so it must not travel over plaintext
# HTTP. Secure is on by default; local http dev sets SESSION_COOKIE_SECURE=0.
_cookie_secure = os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=_cookie_secure,
    MAX_CONTENT_LENGTH=65536,
)

# Behind Caddy (a single trusted reverse proxy on localhost) the real client IP
# arrives in X-Forwarded-For and the original scheme in X-Forwarded-Proto.
# Trust one hop so login rate limiting keys on the actual client, not 127.0.0.1,
# and Flask knows requests are https. Disable with TRUST_PROXY=0 when running
# gunicorn/Flask directly without a proxy in front.
if os.environ.get("TRUST_PROXY", "1") != "0":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# ── Login rate limiting ───────────────────────────────────────────────────────
_login_attempts = {}  # ip:user -> {"count": int, "locked_until": float}
_login_lock = threading.Lock()
_MAX_LOGIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 minutes

_WORKER_PUBLIC_ERRORS = {
    "register": "Could not connect to the golf system. Please try again.",
    "refresh_buddies": "Could not refresh your golfers right now. Please try again.",
    "tee_times": "Could not fetch tee times right now. Please try again.",
    "my_tee_times": "Could not load your tee times right now. Please try again.",
    "delete_reservation": "Could not delete that reservation right now. Please try again.",
    "book": "Booking failed. Please try again.",
    "request_courses": "Could not load course list. Please try again.",
    "submit_request": "Could not submit request. Please try again.",
    "my_requests": "Could not load your requests. Please try again.",
    "delete_request": "Could not cancel that request. Please try again.",
}


def _check_login_rate(key):
    """Return error string if IP is locked out, else None."""
    with _login_lock:
        rec = _login_attempts.get(key)
        if not rec:
            return None
        if rec["locked_until"] and time.time() < rec["locked_until"]:
            remaining = int(rec["locked_until"] - time.time())
            return f"Too many attempts. Try again in {remaining}s."
        if rec["locked_until"] and time.time() >= rec["locked_until"]:
            del _login_attempts[key]
        return None


def _record_login_failure(key):
    with _login_lock:
        rec = _login_attempts.setdefault(key, {"count": 0, "locked_until": None})
        rec["count"] += 1
        if rec["count"] >= _MAX_LOGIN_ATTEMPTS:
            rec["locked_until"] = time.time() + _LOCKOUT_SECONDS


def _clear_login_attempts(key):
    with _login_lock:
        _login_attempts.pop(key, None)


# ── Register rate limiting ────────────────────────────────────────────────────
# /api/register is unauthenticated and each call drives a real headless-Chromium
# login against thevillages.net with caller-supplied credentials. Without a cap
# it is a brute-force oracle for residents' TVN passwords and a cheap way to
# monopolize the single browser worker during the 7 AM rush. Limit per-IP
# volume; credential-guessing also trips the shared ip:username login lockout.
_register_attempts = {}  # ip -> {"count": int, "window_start": float}
_register_lock = threading.Lock()
_MAX_REGISTER_PER_HOUR = 10


def _check_register_rate(ip):
    """Return an error string if this IP has exceeded the hourly cap, else None."""
    now = time.time()
    with _register_lock:
        rec = _register_attempts.get(ip)
        if not rec or now - rec["window_start"] >= 3600:
            _register_attempts[ip] = {"count": 1, "window_start": now}
            return None
        if rec["count"] >= _MAX_REGISTER_PER_HOUR:
            return "Too many registration attempts. Please try again later."
        rec["count"] += 1
        return None


def _worker_public_error(action):
    return _WORKER_PUBLIC_ERRORS.get(
        action,
        "Could not complete that request right now. Please try again.",
    )


# ── User store (SQLite) ─────────────────────────────────────────────────────
# Self-contained local store — no external cloud dependency. See user_store.py.
import user_store
import request_watches
import push_notifications
import preferences as website_preferences

# Fail fast at boot if credentials-at-rest encryption isn't configured, so we
# never silently write cleartext Villages passwords/PINs to disk in production.
user_store.verify_encryption_config()


def _get_user(username):
    if not username:
        return None
    return user_store.get_user(username)


def _set_user(username, data):
    user_store.set_user(username, data)


def _delete_user(username):
    user_store.delete_user(username)


def _all_users():
    """Return {username: data} for every registered profile."""
    return user_store.all_users()


# ── Email notifications ──────────────────────────────────────────────────────
from email_notifications import send_booking_confirmation, send_email, send_request_result


# ── Operator alerting ─────────────────────────────────────────────────────────
# Email the operator when the browser worker errors or wedges, so selector drift
# or a hung session is caught before the 7 AM rush instead of by golfers. Set
# OPERATOR_ALERT_EMAIL to enable. Rate-limited so one bad morning can't flood.
_OPERATOR_ALERT_EMAIL = (os.environ.get("OPERATOR_ALERT_EMAIL") or "").strip()
_alert_state = {"last_sent": 0.0}
_alert_lock = threading.Lock()
_ALERT_COOLDOWN_SECONDS = 900  # at most one alert per 15 min


def _operator_alert(subject, body):
    """Email the operator about a worker failure (fire-and-forget, rate-limited)."""
    if not _OPERATOR_ALERT_EMAIL:
        return
    now = time.time()
    with _alert_lock:
        if now - _alert_state["last_sent"] < _ALERT_COOLDOWN_SECONDS:
            return
        _alert_state["last_sent"] = now

    def _task():
        try:
            send_email(_OPERATOR_ALERT_EMAIL, subject, body)
        except Exception as e:  # never let alerting break the request path
            app.logger.warning("operator_alert.error error=%s", e)

    threading.Thread(target=_task, name="operator-alert", daemon=True).start()


# ── Golf Worker ───────────────────────────────────────────────────────────────
class GolfWorker:
    """Single-threaded worker so Playwright objects stay on one thread."""

    # A single Playwright call (e.g. a hung page.evaluate) has no timeout and can
    # wedge this one worker thread forever, taking the whole app down. If a job
    # runs past this deadline it is definitively stuck (the caller already gave
    # up at the call timeout), so we self-terminate and let systemd/gunicorn
    # restart us. Set comfortably above _CALL_TIMEOUT_SECONDS so slow-but-
    # progressing jobs are never killed.
    _WATCHDOG_SECONDS = int(os.environ.get("WORKER_WATCHDOG_SECONDS", "280"))

    # How long call() waits for a job before giving up on the caller's behalf.
    # The job itself is not stopped when this elapses; it keeps running on the
    # worker thread until it finishes, or the watchdog above kills the process.
    _CALL_TIMEOUT_SECONDS = 240

    # Mutations must not run twice for the same account while one is still in
    # flight (e.g. a double-tapped Book It! button). Refuse the second call
    # outright instead of queueing it or making the caller wait.
    _MUTATION_METHODS = frozenset({"book_tee_time", "submit_request", "delete_reservation", "delete_request"})

    def __init__(self):
        self._job_q = queue.Queue(maxsize=12)
        self._ready = threading.Event()
        self._current = {"started": 0.0, "method": None}
        self._current_lock = threading.Lock()
        self._inflight = {}  # exclusive_key -> True, while a mutation for it runs
        self._inflight_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="golf-worker",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=10)
        if not self._ready.is_set():
            raise RuntimeError("Golf worker failed to start")
        threading.Thread(
            target=self._watchdog_loop,
            name="golf-watchdog",
            daemon=True,
        ).start()

    def _watchdog_loop(self):
        """Kill the process if a single job wedges the worker thread."""
        while True:
            time.sleep(10)
            with self._current_lock:
                started = self._current["started"]
                method = self._current["method"]
            if started and (time.monotonic() - started) > self._WATCHDOG_SECONDS:
                elapsed = int(time.monotonic() - started)
                app.logger.error(
                    "worker.watchdog wedged method=%s elapsed_s=%s — restarting process",
                    method, elapsed,
                )
                _operator_alert(
                    "Golf worker wedged — restarting",
                    f"A '{method}' job ran {elapsed}s (watchdog {self._WATCHDOG_SECONDS}s) "
                    "and appears stuck. The process is self-terminating so it restarts.",
                )
                time.sleep(2)  # give the alert thread a moment to fire
                os._exit(1)

    def _run(self):
        from golf_service import GolfService

        service = GolfService()
        self._ready.set()

        while True:
            job = self._job_q.get()
            if job is None:
                break

            method_name, kwargs, done = job
            # Skip jobs whose caller already gave up (timed out) while this one
            # sat in the queue. Executing a booking after the user was told it
            # failed would produce a silent/duplicate reservation.
            if done.get("cancelled"):
                app.logger.warning("worker.skip_cancelled method=%s", method_name)
                self._release(done.get("key"))
                done["event"].set()
                continue
            with self._current_lock:
                self._current = {"started": time.monotonic(), "method": method_name}
            try:
                method = getattr(service, method_name)
                done["result"] = method(**kwargs)
            except BaseException as exc:
                done["error"] = exc
            finally:
                with self._current_lock:
                    self._current = {"started": 0.0, "method": None}
                self._release(done.get("key"))
                done["event"].set()
            # A BaseException that is not an Exception (SystemExit,
            # KeyboardInterrupt, asyncio.CancelledError, ...) is a genuine
            # interpreter-level exit signal, not an application error. The
            # release and event-set above have already run, so the caller in
            # call() is unblocked with the error recorded; only now do we let
            # the exception continue so this thread actually terminates.
            if done["error"] is not None and not isinstance(done["error"], Exception):
                app.logger.error("worker.fatal_exception method=%s exc=%r", method_name, done["error"])
                raise done["error"]

    def _release(self, key):
        """Release an exclusivity key, tolerating a call with no key at all."""
        if key is None:
            return
        with self._inflight_lock:
            self._inflight.pop(key, None)

    def call(self, method_name, request_id="-", action=None, exclusive_key=None, **kwargs):
        if not self._thread.is_alive():
            raise RuntimeError("Golf worker thread is not running")

        action = action or method_name

        is_mutation = method_name in self._MUTATION_METHODS
        key = exclusive_key if (is_mutation and exclusive_key is not None) else None
        if key is not None:
            with self._inflight_lock:
                if key in self._inflight:
                    app.logger.info(
                        "worker.busy request_id=%s action=%s key=%s",
                        request_id, action, key,
                    )
                    return {"success": False, "busy": True, "error":
                             "A booking or cancellation for this account is still being "
                             "processed. Wait for it to finish, then check My Tee Times or "
                             "My Requests before trying again."}
                self._inflight[key] = True

        started = time.monotonic()
        app.logger.info(
            "worker.start request_id=%s action=%s method=%s",
            request_id,
            action,
            method_name,
        )

        done = {"event": threading.Event(), "result": None, "error": None,
                "cancelled": False, "key": key}
        try:
            self._job_q.put_nowait((method_name, kwargs, done))
        except queue.Full:
            self._release(key)
            return {"success": False, "error": "The golf system is busy. Please try again shortly."}

        if not done["event"].wait(timeout=self._CALL_TIMEOUT_SECONDS):
            # Mark the job cancelled so the worker skips it if it is still queued
            # behind a slow job. (A job already mid-execution is not stopped here;
            # that is the golf_service watchdog's job.) Do not release the
            # exclusivity key here: the job may still be running, and only the
            # worker thread (skip_cancelled path or _run's finally) releases it.
            done["cancelled"] = True
            elapsed_ms = int((time.monotonic() - started) * 1000)
            app.logger.error(
                "worker.timeout request_id=%s action=%s duration_ms=%s",
                request_id,
                action,
                elapsed_ms,
            )
            _operator_alert(
                "Golf worker timeout",
                f"A '{action}' job exceeded {self._CALL_TIMEOUT_SECONDS}s (request_id={request_id}). "
                "The browser worker may be wedged or the queue saturated.",
            )
            mutation = method_name in self._MUTATION_METHODS
            return {"success": False, "unknown": mutation,
                    "error": "We could not confirm the outcome. Check your tee times or requests before trying again."
                    if mutation else "The golf system took too long to respond. Please try again."}
        if done["error"] is not None:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            app.logger.exception(
                "worker.error request_id=%s action=%s duration_ms=%s",
                request_id,
                action,
                elapsed_ms,
                exc_info=done["error"],
            )
            _operator_alert(
                f"Golf worker error: {action}",
                f"A '{action}' job raised {type(done['error']).__name__}: "
                f"{done['error']} (request_id={request_id}). Possible selector "
                "drift on thevillages.net.",
            )
            mutation = method_name in self._MUTATION_METHODS
            return {"success": False, "unknown": mutation, "error":
                    "We could not confirm the outcome. Check your tee times or requests before trying again."
                    if mutation else _worker_public_error(action)}
        if not isinstance(done["result"], dict):
            # Defense in depth: every normal exit from _run's job loop sets
            # either done["result"] (success) or done["error"] (any raised
            # exception) before the event fires. This should not be reachable,
            # but a caller blocked on done["event"] must never call .get() on
            # something that is not a dict.
            elapsed_ms = int((time.monotonic() - started) * 1000)
            app.logger.error(
                "worker.no_result request_id=%s action=%s duration_ms=%s",
                request_id,
                action,
                elapsed_ms,
            )
            mutation = method_name in self._MUTATION_METHODS
            return {"success": False, "unknown": mutation, "error":
                    "We could not confirm the outcome. Check your tee times or requests before trying again."
                    if mutation else _worker_public_error(action)}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        global _worker_last_success_ts
        # Closing a browser session says nothing about whether the Villages site works.
        if done["result"].get("success") and method_name != "release_session":
            _worker_last_success_ts = time.time()
        app.logger.info(
            "worker.done request_id=%s action=%s duration_ms=%s success=%s",
            request_id,
            action,
            elapsed_ms,
            bool(done["result"].get("success")),
        )
        return done["result"]


_worker = None
_worker_lock = threading.Lock()
_worker_last_success_ts = 0.0  # wall-clock of the last successful worker job


def get_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker._thread.is_alive():
            _worker = GolfWorker()
    return _worker


# ── Request-result watcher ────────────────────────────────────────────────────
# The Villages assigns requests by a points lottery ~1 AM ET, 3 days before
# play, and the golfer must check back to see if their request became a
# reservation. This background loop runs those checks at the right time and
# emails the outcome. Watches live in SQLite, so a restart loses nothing.
_WATCH_INTERVAL_SECONDS = int(os.environ.get("REQUEST_WATCH_INTERVAL", "900"))  # 15 min
_WATCH_BATCH_LIMIT = int(os.environ.get("REQUEST_WATCH_BATCH", "8"))
_WATCH_JITTER_SECONDS = (0.5, 3.0)


def _watch_pause():
    """Short random pause between due-watch checks (see _run_due_request_checks)."""
    time.sleep(random.uniform(*_WATCH_JITTER_SECONDS))


def _send_pending_request_notifications():
    """Deliver persisted outcomes; retry failed sends across process restarts."""
    for watch in request_watches.due_notifications():
        user = _get_user(watch["username"])
        if not user:
            request_watches.cancel_watch(watch["id"])
            continue
        email = _user_email(user)  # honor email changes after the request was made
        if not email:
            continue
        try:
            sent = send_request_result(
                email, watch["result"], watch.get("play_date_label") or watch["play_date"],
                json.loads(watch.get("courses") or "[]"),
                json.loads(watch.get("result_detail") or "null"),
            )
            request_watches.finish_notification(watch["id"], bool(sent))
            app.logger.info(
                "request_watch.notification user=%s sent=%s", watch["username"], bool(sent),
            )
        except Exception:
            request_watches.finish_notification(watch["id"], False)
            app.logger.exception("request_watch.notify_error")


_PUSH_KINDS = frozenset({"push_booking", "push_reminder"})
_REMINDER_STALE_BEFORE_TEE = timedelta(hours=1)
_REMINDER_DEFER_SECONDS = 600
_REMINDER_VERIFICATIONS_PER_PASS = 3
_PUSH_LOOP_INTERVAL_SECONDS = int(os.environ.get("PUSH_LOOP_INTERVAL", "60"))
_EASTERN = ZoneInfo("America/New_York")
_REMINDER_ACTIVITY_WINDOW_SECONDS = 600
_REMINDER_LATE_LIMIT_SECONDS = 3600

# Worker-backed routes a golfer is actively using. The reminder check drives the
# booker's shared browser session, which would break a search in progress.
_WORKER_ACTIVITY_PATHS = frozenset({
    "/api/refresh-buddies", "/api/tee-times", "/api/my-tee-times", "/api/delete-reservation",
    "/api/book", "/api/request-courses", "/api/submit-request", "/api/my-requests",
    "/api/delete-request",
})
_worker_activity = {}  # username -> time.time() of the last worker-backed request
_worker_activity_lock = threading.Lock()


def _note_worker_activity(username):
    if not username:
        return
    with _worker_activity_lock:
        _worker_activity[username] = time.time()


def _recent_worker_activity(username, window=_REMINDER_ACTIVITY_WINDOW_SECONDS):
    with _worker_activity_lock:
        last = _worker_activity.get(username)
    return last is not None and time.time() - last < window


def _push_log(row, totals):
    app.logger.info(
        "push.sent kind=%s id=%s devices=%d sent=%d failed=%d gone=%d",
        row["kind"], row["id"], totals["devices"], totals["sent"], totals["failed"], totals["gone"],
    )


def _finish_push_row(row, totals):
    """Sent when any device got it or there were no devices (nothing to retry,
    but logged so a silent drop is visible); otherwise retry."""
    if totals["devices"] == 0:
        app.logger.info("push.no_devices kind=%s id=%s", row["kind"], row["id"])
    delivered = totals["devices"] == 0 or totals["sent"] > 0
    request_watches.finish_outbox(row["id"], delivered)


def _deliver_push_row(row, pause_before_worker=False):
    """Send one claimed push_booking or push_reminder outbox row. Never raises:
    an unexpected error leaves the row to retry. Returns True when the row used
    the browser worker (a reminder verification)."""
    try:
        if not push_notifications.is_configured():
            request_watches.finish_outbox(row["id"], False)
            app.logger.warning("push.not_configured id=%s", row["id"])
            return False
        payload = json.loads(row["payload"])
        recipients = payload.get("recipients") or []
        if row["kind"] == "push_booking":
            totals = push_notifications.send_to_users(
                recipients, push_notifications.booking_payload(payload),
                push_notifications.BOOKING_TTL_SECONDS,
            )
            _push_log(row, totals)
            _finish_push_row(row, totals)
            return False
        return _deliver_reminder_row(row, payload, recipients, pause_before_worker)
    except Exception:
        request_watches.finish_outbox(row["id"], False)
        app.logger.exception("push.outbox_error id=%s", row["id"])
        return False


def _release_booker_session(booker):
    """Close the booker's pooled browser session after a reminder check so the
    check never keeps (or evicts someone else for) a session slot."""
    try:
        get_worker().call(
            "release_session", request_id="reminder", action="release_session", tvn_username=booker,
        )
    except Exception:
        app.logger.exception("push.release_session_failed")


def _next_open_hour(now):
    """The next 7:00 AM Eastern strictly after `now` (aware UTC), in UTC."""
    et = now.astimezone(_EASTERN)
    day = et.date() if et.hour < 7 else et.date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, 7, 0, tzinfo=_EASTERN).astimezone(timezone.utc)


def _previous_open_hour(now):
    """The most recent 7:00 AM Eastern at or before `now` (aware UTC), in UTC."""
    et = now.astimezone(_EASTERN)
    day = et.date() if et.hour >= 7 else et.date() - timedelta(days=1)
    return datetime(day.year, day.month, day.day, 7, 0, tzinfo=_EASTERN).astimezone(timezone.utc)


def _hold_for_quiet_hours(row, tee_at, now):
    """Never deliver between 9 PM and 7 AM Eastern, however late the sender runs.
    Inside quiet hours, wait for 7 AM when the tee time leaves room, else drop the
    row. Returns True when the row was deferred or cancelled."""
    eastern_hour = now.astimezone(_EASTERN).hour
    if not (eastern_hour >= 21 or eastern_hour < 7):
        return False
    opens = _next_open_hour(now)
    if tee_at - _REMINDER_STALE_BEFORE_TEE > opens:
        request_watches.defer_outbox(row["id"], max(1, int((opens - now).total_seconds()) + 1))
        app.logger.info("push.reminder_quiet_deferred id=%s", row["id"])
    else:
        request_watches.cancel_outbox(row["id"])
        app.logger.info("push.reminder_quiet_cancel id=%s", row["id"])
    return True


def _deliver_reminder_row(row, payload, recipients, pause_before_worker=False):
    now = datetime.now(timezone.utc)
    try:
        tee_at = datetime.fromisoformat(str(payload.get("tee_at_utc")))
        if tee_at.tzinfo is None:
            tee_at = tee_at.replace(tzinfo=timezone.utc)
    except ValueError:
        tee_at = None
    if tee_at is None or now > tee_at - _REMINDER_STALE_BEFORE_TEE:
        request_watches.cancel_outbox(row["id"])
        app.logger.info("push.reminder_stale id=%s", row["id"])
        return False
    booker = payload.get("booker") or row["username"]
    user = _get_user(booker)
    if not user:
        request_watches.cancel_outbox(row["id"])
        return False
    if _hold_for_quiet_hours(row, tee_at, now):
        return False
    scheduled = push_notifications.reminder_time_utc(
        payload.get("play_date"), payload.get("display_time"), datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    # A reminder held overnight counts as late from 7 AM, not from its original schedule.
    due_from = max(scheduled, _previous_open_hour(now)) if scheduled else None
    late_seconds = (now - due_from).total_seconds() if due_from else _REMINDER_LATE_LIMIT_SECONDS
    if _recent_worker_activity(booker) and late_seconds < _REMINDER_LATE_LIMIT_SECONDS:
        request_watches.defer_outbox(row["id"], _REMINDER_DEFER_SECONDS)
        app.logger.info("push.reminder_deferred id=%s", row["id"])
        return False
    if pause_before_worker:
        _watch_pause()
    try:
        result = get_worker().call(
            "view_my_tee_times",
            request_id="reminder",
            action="my_tee_times",
            tvn_username=booker,
            tvn_password=user["tvn_password"],
            golf_password=user["golf_password"],
        )
    finally:
        # Do not close a session the booker started while the check ran.
        if not _recent_worker_activity(booker):
            _release_booker_session(booker)
    reservations = result.get("reservations") if isinstance(result, dict) else None
    if not isinstance(result, dict) or not result.get("success") or not isinstance(reservations, list):
        request_watches.finish_outbox(row["id"], False)
        app.logger.info("push.reminder_verify_failed id=%s", row["id"])
        return True
    wanted = str(payload.get("reservation_no") or "").strip()
    reservation = next((
        r for r in reservations
        if isinstance(r, dict) and wanted and str(r.get("reservation_no") or "").strip() == wanted
    ), None)
    if reservation is None:
        request_watches.cancel_outbox(row["id"])
        app.logger.info("push.reminder_cancelled_missing id=%s", row["id"])
        return True
    recipients = _recipients_still_booked(recipients, booker, reservation.get("golfer_ids"))
    # Verification can be slow: check the quiet hours again against the clock now.
    now = datetime.now(timezone.utc)
    if _hold_for_quiet_hours(row, tee_at, now):
        return True
    ttl = max(60, int((tee_at - now).total_seconds()))
    totals = push_notifications.send_to_users(
        recipients, push_notifications.reminder_payload(payload, now), ttl,
    )
    _push_log(row, totals)
    _finish_push_row(row, totals)
    return True


def _recipients_still_booked(recipients, booker, golfer_ids):
    """When the verified reservation lists its golfers, keep only the booker and
    recipients whose primary golfer ID is still on it."""
    if not isinstance(golfer_ids, list) or not golfer_ids:
        return recipients
    on_it = {str(g) for g in golfer_ids}
    kept = []
    for username in recipients:
        primary = ((_get_user(username) or {}).get("primary") or {}).get("id")
        if username == booker or (primary is not None and str(primary) in on_it):
            kept.append(username)
    return kept


def _deliver_outbox_row(row):
    """Deliver one claimed non-push outbox row (booking confirmation emails).
    An unknown kind fails immediately."""
    if row["kind"] != "booking_confirmation":
        request_watches.finish_outbox(row["id"], False, exhaust=True)
        app.logger.warning("email.outbox_unknown_kind id=%s kind=%s", row["id"], row["kind"])
        return
    try:
        sent = send_booking_confirmation(row["email"], **json.loads(row["payload"]))
        request_watches.finish_outbox(row["id"], bool(sent))
        app.logger.info(
            "email.outbox_sent id=%s user=%s sent=%s", row["id"], row["username"], bool(sent),
        )
    except Exception:
        request_watches.finish_outbox(row["id"], False)
        app.logger.exception("email.outbox_error id=%s", row["id"])


def _send_pending_outbox():
    """Deliver persisted outbox notifications (e.g. booking confirmations);
    retry failed sends across process restarts, mirroring
    _send_pending_request_notifications. Each row is claimed with
    request_watches.claim_outbox before it is acted on, so the immediate
    post-booking thread and the loop's own periodic pass can never both send
    the same row; a row this call loses the claim race for is skipped and
    left to whichever sender claimed it. Push rows have their own sender
    (_send_push_outbox), so each row has one owner."""
    for row in request_watches.due_outbox(exclude_kinds=_PUSH_KINDS):
        if not request_watches.claim_outbox(row["id"]):
            continue
        _deliver_outbox_row(row)


def _send_outbox_rows(ids):
    """Deliver exactly these outbox rows (the ones a booking just queued), right
    away. Never touches a push_reminder row or any other due row."""
    for row in request_watches.outbox_rows(ids):
        if row["kind"] == "push_reminder" or row["status"] != "pending":
            continue
        if not request_watches.claim_outbox(row["id"]):
            continue
        if row["kind"] == "push_booking":
            _deliver_push_row(row)
        else:
            _deliver_outbox_row(row)


def _send_push_outbox():
    """One pass of the push sender: deliver due push_booking and push_reminder
    rows. At most _REMINDER_VERIFICATIONS_PER_PASS reminders are verified per
    pass, with a short random pause between worker calls; the rest wait for the
    next pass."""
    if not push_notifications.is_configured():
        return
    verified = 0
    for row in request_watches.due_outbox(limit=50, kinds=_PUSH_KINDS):
        if row["kind"] == "push_reminder" and verified >= _REMINDER_VERIFICATIONS_PER_PASS:
            continue
        if not request_watches.claim_outbox(row["id"]):
            continue
        if _deliver_push_row(row, pause_before_worker=verified > 0):
            verified += 1


def _run_due_request_checks():
    """Check due watches (assignment time passed), up to a per-pass cap.

    Each check drives the shared Playwright worker, the same one live golfer
    requests wait behind, so a pass is capped at _WATCH_BATCH_LIMIT watches
    with a short random pause between worker calls (not after the last), to
    avoid a burst of due watches starving a live request. Any watches left
    over past the cap are picked up on the loop's next pass; this is
    safe because a watch is not otherwise time-sensitive beyond the 2-hour
    grace window after the 1:30 AM ET assignment time
    (request_watches._ASSIGNMENT_GRACE_SECONDS), comfortably longer than one
    loop interval.
    """
    due = request_watches.due_watches(limit=_WATCH_BATCH_LIMIT)
    if not due:
        return
    app.logger.info("request_watch.due count=%d limit=%d", len(due), _WATCH_BATCH_LIMIT)
    for index, w in enumerate(due):
        user = _get_user(w["username"])
        if not user:
            request_watches.cancel_watch(w["id"])  # account gone
        else:
            result = get_worker().call(
                "view_my_tee_times",
                request_id="watch",
                action="my_tee_times",
                tvn_username=w["username"],
                tvn_password=user["tvn_password"],
                golf_password=user["golf_password"],
            )
            if not result.get("success"):
                if request_watches.bump_attempt(w["id"]):  # retries exhausted
                    request_watches.mark_result(w["id"], "unknown")
            else:
                reservations = result.get("reservations", []) or []
                matched = request_watches.matching_reservation(w, reservations)
                if matched:
                    request_watches.mark_result(w["id"], "matched", matched)
                elif request_watches.bump_attempt(
                    w["id"], allow_exhaust=request_watches.assignment_grace_passed(w)
                ):
                    # Absence or incomplete details do not prove a lottery loss.
                    request_watches.mark_result(w["id"], "unknown")
        if index < len(due) - 1:
            _watch_pause()


def _request_watch_loop():
    time.sleep(30)  # let startup settle before the first pass
    while True:
        # Each pass is isolated so one raising pass cannot skip the others
        # for a whole loop interval.
        for step in (_run_due_request_checks, _send_pending_request_notifications, _send_pending_outbox):
            try:
                step()
            except Exception:
                app.logger.exception("request_watch.loop_error step=%s", step.__name__)
        time.sleep(_WATCH_INTERVAL_SECONDS)


def _push_outbox_loop():
    time.sleep(20)  # let startup settle before the first pass
    while True:
        try:
            _send_push_outbox()
        except Exception:
            app.logger.exception("push.loop_error")
        time.sleep(_PUSH_LOOP_INTERVAL_SECONDS)


def _start_request_watcher():
    if os.environ.get("DISABLE_REQUEST_WATCHER") == "1":
        return
    threading.Thread(target=_push_outbox_loop, name="push-sender", daemon=True).start()
    app.logger.info("push.loop_started interval_s=%d", _PUSH_LOOP_INTERVAL_SECONDS)
    threading.Thread(target=_request_watch_loop, name="request-watcher", daemon=True).start()
    app.logger.info("request_watch.started interval_s=%d", _WATCH_INTERVAL_SECONDS)


# ── Helpers ───────────────────────────────────────────────────────────────────
def require_auth():
    if not session.get("auth"):
        return jsonify({"error": "Not authenticated"}), 401
    return None


def _request_id():
    incoming = (request.headers.get("X-Request-ID") or "").strip()
    return incoming if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", incoming) else uuid.uuid4().hex[:12]


def _get_session_user():
    """Return the user profile dict for the currently logged-in user, or None."""
    username = session.get("username")
    if not username:
        return None
    return _get_user(username)


def _login_key(ip, username=""):
    return f"{ip}:{(username or '').strip().lower()}"


def _set_authenticated_user(username):
    session["auth"] = True
    session["username"] = username
    session.permanent = True


def _const_eq(stored, supplied):
    """Constant-time string comparison to avoid login timing side-channels."""
    if stored is None or supplied is None:
        return False
    return hmac.compare_digest(str(stored).encode(), str(supplied).encode())


def _parse_booking_inputs(data, require_course_time=False):
    """Validate and normalize booking/search payload from frontend."""
    date_str = (data.get("date") or "").strip()
    date_str = request_watches.normalize_play_date(date_str)
    if not date_str:
        return None, "A valid play date is required."

    course_type = (data.get("course_type") or "").strip()
    if course_type not in {"Executive", "Championship"}:
        return None, "Course type must be Executive or Championship."

    raw_ids = data.get("golfer_ids")
    if not isinstance(raw_ids, list) or len(raw_ids) == 0:
        return None, "Select at least one golfer."
    golfer_ids = [str(g).strip() for g in raw_ids if str(g).strip()]
    if len(golfer_ids) == 0:
        return None, "Select at least one golfer."

    try:
        num_golfers = int(data.get("num_golfers"))
    except (TypeError, ValueError):
        return None, "Number of golfers is required."
    if not 1 <= num_golfers <= 4:
        return None, "Select between 1 and 4 golfers."
    if num_golfers != len(golfer_ids):
        return None, "Number of golfers must match selected golfers."
    if len(set(golfer_ids)) != len(golfer_ids) or any(not re.fullmatch(r"\d{1,20}", g) for g in golfer_ids):
        return None, "Select distinct, valid golfer IDs."
    if not isinstance(data.get("has_guests", False), bool):
        return None, "Guest selection must be yes or no."
    start, end = data.get("time_from", ""), data.get("time_to", "")
    if any(t and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in (start, end)):
        return None, "Use HH:MM for the time window."
    if start and end and start > end:
        return None, "Earliest time must precede latest time."

    region_filter = (data.get("region_filter") or "all").strip().lower()
    if region_filter not in ({"all"} | BAND_KEYS):
        allowed = ", ".join(["all", *sorted(BAND_KEYS)])
        return None, f"Region must be one of: {allowed}."

    parsed = {
        "date_str": date_str,
        "course_type": course_type,
        "region_filter": region_filter,
        "golfer_ids": golfer_ids,
        "num_golfers": num_golfers,
        "has_guests": bool(data.get("has_guests", False)),
        "time_from": start, "time_to": end,
    }

    if require_course_time:
        course_name = (data.get("course") or "").strip()
        time_str = (data.get("time") or "").strip()
        if not course_name or not time_str:
            return None, "Course and time are required."
        parsed["course_name"] = course_name
        parsed["time_str"] = time_str

    return parsed, None


def _today_et():
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


_BODYLESS_API_POSTS = frozenset({"/api/logout", "/api/refresh-buddies"})


@app.before_request
def validate_api_json():
    """Reject malformed payloads before they reach slow browser operations."""
    if request.method != "POST" or not request.path.startswith("/api/"):
        return None
    if request.path in _BODYLESS_API_POSTS and not request.data:
        return None
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "ok": False, "error": "Send a JSON object."}), 400
    strings = {"username", "password", "golf_pin", "date", "date_label", "course_type", "course",
               "time", "time_filter", "time_from", "time_to", "region_filter", "search_id", "play_date",
               "preference", "time_to_play", "earliest_time", "latest_time", "reservation_no",
               "request_id", "course_id", "email", "id", "name"}
    for key in strings.intersection(data):
        if data[key] is None:
            # An empty optional field arrives as null from the page; the
            # route-level parser decides whether the field was required.
            continue
        if not isinstance(data[key], str) or len(data[key]) > 512:
            return jsonify({"success": False, "ok": False, "error": f"Invalid {key.replace('_', ' ')}."}), 400
    for key in ("has_guests", "any_course"):
        if key in data and not isinstance(data[key], bool):
            return jsonify({"success": False, "error": f"Invalid {key.replace('_', ' ')}."}), 400


@app.before_request
def track_worker_activity():
    """Remember which golfer last used a worker-backed route (see _deliver_reminder_row)."""
    if request.path in _WORKER_ACTIVITY_PATHS and session.get("auth"):
        _note_worker_activity(session.get("username"))
    return None


# App shell pages and the service worker must always revalidate so a deploy
# reaches every device on its next visit.
_NO_CACHE_PAGES = frozenset({"/", "/install", "/help", "/offline", "/sw.js", "/manifest.webmanifest"})


@app.after_request
def api_cache_policy(response):
    if request.path.startswith("/api/") and response.mimetype != "image/png":
        response.headers["Cache-Control"] = "no-store"
    if request.path in _NO_CACHE_PAGES:
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ── Email notification helper ────────────────────────────────────────────────
def _user_email(user_data):
    """Return the preferred notification email for a user record."""
    return (
        user_data.get("email")
        or user_data.get("imessage_address")
        or ""
    ).strip()


def _booking_recipients(golfer_ids, booking_result, course_name, booking_username):
    """Return one entry per distinct recipient email for a booking confirmation.

    Recipients are: (1) the user who made the booking, and (2) any other
    registered user whose primary golfer ID is in the booking. Golfer display
    names are resolved once, here, and frozen into each entry's payload -- a
    later buddy-list rename does not change what was booked. Each entry is
    ``{"username", "email", "payload"}``, where payload is ready to pass as
    ``**payload`` to ``send_booking_confirmation``.
    """
    res_no = booking_result.get("reservation_no", "N/A")
    display_time = booking_result.get("display_time", "")
    date_label = booking_result.get("date_label") or request_watches.friendly_date(
        booking_result.get("play_date")
    )

    users_snapshot = _all_users()

    # Collect display names for all golfers in the booking
    golfer_names = []
    for gid in golfer_ids:
        for u in users_snapshot.values():
            matched = next(
                (b.get("name") for b in u.get("buddies", []) if b.get("id") == str(gid)),
                None,
            )
            if matched:
                golfer_names.append(matched)
                break

    golfer_id_strs = [str(g) for g in golfer_ids]
    payload = {
        "reservation_no": res_no, "course": course_name, "display_time": display_time,
        "date_label": date_label, "golfer_names": golfer_names,
    }

    recipients = []
    seen_emails = set()

    def _add(username, email):
        if not email or email in seen_emails:
            return
        seen_emails.add(email)
        recipients.append({"username": username, "email": email, "payload": dict(payload)})

    # Always notify the user who made the booking
    if booking_username and booking_username in users_snapshot:
        _add(booking_username, _user_email(users_snapshot[booking_username]))

    # Also notify any other registered user whose primary ID is in the booking
    for uname, udata in users_snapshot.items():
        primary_id = (udata.get("primary") or {}).get("id")
        if primary_id and str(primary_id) in golfer_id_strs:
            _add(uname, _user_email(udata))

    return recipients


def _push_recipients(golfer_ids, booking_username):
    """Usernames to notify for a booking: the booker plus every registered user
    whose primary golfer ID is in the booking. Unlike email, no address is needed."""
    users_snapshot = _all_users()
    golfer_id_strs = {str(g) for g in golfer_ids or []}
    recipients = []
    if booking_username and booking_username in users_snapshot:
        recipients.append(booking_username)
    for uname, udata in users_snapshot.items():
        primary_id = (udata.get("primary") or {}).get("id")
        if primary_id and str(primary_id) in golfer_id_strs and uname not in recipients:
            recipients.append(uname)
    return recipients


def _enqueue_push_for_booking(result, username, request_id):
    """Queue the booking notification (due now) and, when its time has not
    passed, the reminder (due at the reminder time, keyed by reservation number)."""
    reservation_no = str(result.get("reservation_no") or "").strip()
    play_date = str(result.get("play_date") or "")
    display_time = str(result.get("display_time") or "")
    tee_at = push_notifications.tee_time_utc(play_date, display_time)
    payload = {
        "reservation_no": reservation_no,
        "course": str(result.get("course") or ""),
        "display_time": display_time,
        "date_label": str(result.get("date_label") or request_watches.friendly_date(play_date) or ""),
        "play_date": play_date,
        "booker": username,
        "recipients": _push_recipients(result.get("golfer_ids", []), username),
        "tee_at_utc": tee_at.isoformat() if tee_at else "",
    }
    ids = [request_watches.enqueue_notification(
        "push_booking", username, "", payload, ref=reservation_no or None,
    )]
    remind_at = push_notifications.reminder_time_utc(play_date, display_time, datetime.now(timezone.utc))
    if remind_at is not None and reservation_no:
        request_watches.enqueue_notification(
            "push_reminder", username, "", payload, notify_after=remind_at, ref=reservation_no,
        )
    app.logger.info(
        "push.enqueued request_id=%s recipients=%d reminder=%s",
        request_id, len(payload["recipients"]), remind_at is not None,
    )
    return ids  # the reminder is not delivered by the post-booking thread


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/api/courses", methods=["GET"])
def course_catalog():
    request_id = _request_id()
    err = require_auth()
    if err:
        return err
    try:
        from mapping_service import CatalogUnavailable, get_courses
        courses = get_courses()
    except CatalogUnavailable as exc:
        app.logger.error("catalog.unavailable request_id=%s error=%s", request_id, exc)
        return jsonify({"success": False, "error": "Course catalog is unavailable right now."}), 503
    return jsonify({"success": True, "courses": courses})


@app.route("/api/preferences", methods=["GET", "POST"])
def preferences_route():
    err = require_auth()
    if err:
        return err
    user = _get_session_user()
    if not user:
        return jsonify({"ok": False, "error": "Please sign in again."}), 401
    if request.method == "GET":
        return jsonify({"ok": True, "preferences": website_preferences.get_preferences(user)})
    request_id = _request_id()
    try:
        from mapping_service import CatalogUnavailable, get_courses
        course_ids = {c["id"] for c in get_courses()}
    except CatalogUnavailable as exc:
        app.logger.error("catalog.unavailable request_id=%s error=%s", request_id, exc)
        return jsonify({"ok": False, "error": "Course catalog is unavailable right now."}), 503
    try:
        changes = website_preferences.validate_preferences(request.get_json(), user, course_ids)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    def update(profile):
        latest = website_preferences.get_preferences(profile)
        latest.update(changes)
        profile["preferences"] = latest
    saved = user_store.update_user(session["username"], update)
    if not saved:
        return jsonify({"ok": False, "error": "Please sign in again."}), 401
    return jsonify({"ok": True, "preferences": website_preferences.get_preferences(saved)})


_route_attempts = {}
_route_lock = threading.Lock()


def _mapping_rate_limited(username):
    """Return True when this user has made 80+ mapping calls in the last 60s.

    Shared by every mapping endpoint (routes and map imagery) so one process-
    local per-user budget covers both, matching the docstring in AGENTS.md
    that describes an 80 requests/user/minute mapping limit.
    """
    now = time.monotonic()
    with _route_lock:
        for key in list(_route_attempts):
            if now - _route_attempts[key][0] > 60:
                del _route_attempts[key]
        started, count = _route_attempts.get(username, (now, 0))
        if count >= 80:
            return True
        _route_attempts[username] = (started, count + 1)
    return False


@app.route("/api/course-route", methods=["POST"])
def course_route():
    request_id = _request_id()
    err = require_auth()
    if err:
        return err
    if not _get_session_user():
        return jsonify({"success": False, "error": "Please sign in again."}), 401
    username = session["username"]
    if _mapping_rate_limited(username):
        return jsonify({"success": False, "error": "Please wait a minute before calculating more routes."}), 429
    data = request.get_json()
    try:
        origin = website_preferences.validate_origin(data.get("origin"))
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    try:
        from mapping_service import CatalogUnavailable, route_to_course
        result = route_to_course(origin, data.get("course_id", ""))
    except CatalogUnavailable as exc:
        app.logger.error("catalog.unavailable request_id=%s error=%s", request_id, exc)
        return jsonify({"success": False, "error": "Course catalog is unavailable right now."}), 503
    except Exception:
        app.logger.exception("route.failed request_id=%s", request_id)
        return jsonify({"success": False, "error": "Route preview is unavailable right now."}), 502
    if result.get("success"):
        return jsonify(result), 200
    if result.get("error") == "unknown course":
        return jsonify(result), 404
    return jsonify(result), 502


@app.route("/api/map-image", methods=["GET"])
def map_image_route():
    """Serve a proxied basemap image tile for the given bounds and pixel size.

    Maps use a separate service and never touch the Playwright golf worker.
    """
    request_id = _request_id()
    err = require_auth()
    if err:
        return err
    if not _get_session_user():
        return jsonify({"success": False, "error": "Please sign in again."}), 401
    username = session["username"]

    try:
        bounds = {
            "min_lon": float(request.args["min_lon"]),
            "min_lat": float(request.args["min_lat"]),
            "max_lon": float(request.args["max_lon"]),
            "max_lat": float(request.args["max_lat"]),
        }
        width = int(request.args["w"])
        height = int(request.args["h"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid map request."}), 400
    if _mapping_rate_limited(username):
        return jsonify({"success": False, "error": "Please wait a minute before requesting more map imagery."}), 429

    try:
        from mapping_service import CatalogUnavailable, map_image
        png_bytes, error, status = map_image(bounds, width, height)
    except CatalogUnavailable as exc:
        app.logger.error("catalog.unavailable request_id=%s error=%s", request_id, exc)
        return jsonify({"success": False, "error": "Course catalog is unavailable right now."}), 503
    except Exception:
        app.logger.exception("map.failed request_id=%s", request_id)
        return jsonify({"success": False, "error": "Map imagery is unavailable right now."}), 502
    if error is not None:
        return jsonify({"success": False, "error": error}), status

    response = Response(png_bytes, mimetype="image/png")
    response.headers["Cache-Control"] = "private, max-age=21600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


# Served instead of templates/sw.js when DISABLE_SERVICE_WORKER=1. It registers no
# fetch listener, so it becomes inert (no fetch handling, caches deleted), and it
# asks the browser to unregister it.
_KILL_SWITCH_WORKER = (
    "self.addEventListener('install', () => { self.skipWaiting(); });\n"
    "self.addEventListener('activate', (event) => {\n"
    "  event.waitUntil(\n"
    "    caches.keys()\n"
    "      .then((names) => Promise.all(names.map((name) => caches.delete(name))))\n"
    "      .then(() => self.registration.unregister())\n"
    "  );\n"
    "});\n"
)


def _compute_asset_version():
    """First 12 hex characters of sha256 over the page and every local script and stylesheet it loads."""
    root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(root, "templates", "index.html"), "rb") as handle:
        page = handle.read()
    digest = hashlib.sha256(page)
    text = page.decode("utf-8")
    references = []
    for match in re.finditer(r"<script\b[^>]*\bsrc=\"(/static/[^\"]+)\"|<link\b[^>]*\brel=\"stylesheet\"[^>]*\bhref=\"(/static/[^\"]+)\"", text):
        references.append((match.start(), match.group(1) or match.group(2)))
    for _position, reference in sorted(references):
        relative = reference.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        with open(os.path.join(root, *relative.split("/")), "rb") as handle:  # raises if the file is missing
            digest.update(handle.read())
    return digest.hexdigest()[:12]


_ASSET_VERSION = _compute_asset_version()


@app.route("/api/version", methods=["GET"])
def asset_version():
    """Let a long-lived installed app notice that a deploy changed the page. Reveals nothing secret."""
    return jsonify({"version": _ASSET_VERSION})


@app.route("/manifest.webmanifest")
def web_app_manifest():
    return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")


def _build_service_worker_body():
    root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(root, "templates", "offline.html"), "rb") as handle:
        version = hashlib.sha256(handle.read()).hexdigest()[:12]
    with open(os.path.join(root, "templates", "sw.js"), encoding="utf-8") as handle:
        return handle.read().replace("__OFFLINE_VERSION__", version)


# Built once at import: the offline page cannot change without a deploy, and every deploy restarts the app.
_SERVICE_WORKER_BODY = _build_service_worker_body()


@app.route("/sw.js")
def service_worker():
    if os.environ.get("DISABLE_SERVICE_WORKER") == "1":
        return Response(_KILL_SWITCH_WORKER, mimetype="application/javascript")
    return Response(_SERVICE_WORKER_BODY, mimetype="application/javascript")


@app.route("/offline")
def offline_page():
    return send_from_directory("templates", "offline.html")


@app.route("/install")
def install_guide():
    return send_from_directory("templates", "install.html")


@app.route("/help")
def user_guide():
    return send_from_directory("templates", "help.html")


@app.route("/healthz")
def healthz():
    """Liveness/health probe for an uptime monitor. No auth; leaks no secrets.

    Reports whether the browser worker thread is alive, the current job-queue
    depth, and how long since the last successful worker job — so selector
    drift or a wedged session is visible before golfers hit it at 7 AM.
    """
    w = _worker
    if w is None:
        # Lazily-created worker hasn't been needed yet — idle, not unhealthy.
        status, code, alive, depth = "idle", 200, None, None
    elif w._thread.is_alive():
        status, code, alive, depth = "ok", 200, True, w._job_q.qsize()
    else:
        status, code, alive, depth = "degraded", 503, False, w._job_q.qsize()
    since = (time.time() - _worker_last_success_ts) if _worker_last_success_ts else None
    return jsonify({
        "status": status,
        "worker_alive": alive,
        "queue_depth": depth,
        "seconds_since_last_success": round(since, 1) if since is not None else None,
    }), code


@app.route("/api/session", methods=["GET"])
def get_session():
    """Return current session state so the frontend can skip screens."""
    if not session.get("auth"):
        return jsonify({"auth": False})
    username = session.get("username")
    user = _get_user(username) if username else None
    if user:
        return jsonify({
            "auth": True,
            "username": username,
            "play_date": _today_et(),
            "display_name": user.get("display_name", username),
            "primary": user.get("primary"),
            "buddies": user.get("buddies", []),
            "email": _user_email(user),
        })
    session.clear()
    return jsonify({"auth": False})


@app.route("/api/login-user", methods=["POST"])
def login_user():
    """Authenticate by TVN username + password + golf PIN, then set a session cookie."""
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    golf_pin = (data.get("golf_pin") or "").strip()

    ip = request.remote_addr or "unknown"
    rate_key = _login_key(ip, username)
    lockout = _check_login_rate(rate_key)
    if lockout:
        return jsonify({"ok": False, "error": lockout}), 429

    user = _get_user(username) if username else None
    credentials_ok = (
        user is not None
        and password
        and golf_pin
        and _const_eq(user.get("tvn_password"), password)
        and _const_eq(user.get("golf_password"), golf_pin)
    )
    if credentials_ok:
        _set_authenticated_user(username)
        _clear_login_attempts(rate_key)
        return jsonify({
            "ok": True,
            "username": username,
            "play_date": _today_et(),
            "display_name": user.get("display_name", username),
            "primary": user.get("primary"),
            "buddies": user.get("buddies", []),
            "email": _user_email(user),
        })

    _record_login_failure(rate_key)
    return jsonify({"ok": False, "error": "Incorrect username, password, or PIN"}), 401


@app.route("/api/register", methods=["POST"])
def register_user():
    """Register a new user: login to Villages, fetch buddy list, cache profile."""
    request_id = _request_id()
    data = request.get_json() or {}
    tvn_username = (data.get("username") or "").strip()
    tvn_password = (data.get("password") or "").strip()
    golf_password = (data.get("golf_pin") or "").strip()

    if not tvn_username or not tvn_password or not golf_password:
        return jsonify({"ok": False, "error": "All fields are required."}), 400

    ip = request.remote_addr or "unknown"
    reg_limit = _check_register_rate(ip)
    if reg_limit:
        app.logger.info("api.denied request_id=%s action=register reason=rate_limited", request_id)
        return jsonify({"ok": False, "error": reg_limit}), 429
    rate_key = _login_key(ip, tvn_username)
    lockout = _check_login_rate(rate_key)
    if lockout:
        return jsonify({"ok": False, "error": lockout}), 429

    app.logger.info("api.start request_id=%s action=register user=%s", request_id, tvn_username)

    result = get_worker().call(
        "fetch_buddy_list",
        request_id=request_id,
        action="register",
        tvn_username=tvn_username,
        tvn_password=tvn_password,
        golf_password=golf_password,
    )

    if not result.get("success"):
        _record_login_failure(rate_key)
        app.logger.info("api.done request_id=%s action=register success=False", request_id)
        return jsonify({"ok": False, "error": result.get("error", "Registration failed.")}), 400

    _clear_login_attempts(rate_key)

    primary = result.get("primary")
    buddies = result.get("buddies", [])

    # Preserve manually-added buddies from a previous registration
    existing = _get_user(tvn_username)
    if existing:
        scraped_ids = {b["id"] for b in buddies}
        for old_buddy in existing.get("buddies", []):
            if old_buddy["id"] not in scraped_ids:
                buddies.append(old_buddy)

    # Use the TVN username as display name (not the first golfer in the dropdown)
    display_name = existing.get("display_name", tvn_username) if existing else tvn_username

    email = (data.get("email") or "").strip()

    user_data = {
        "tvn_password": tvn_password,
        "golf_password": golf_password,
        "display_name": display_name,
        "primary": primary,
        "buddies": buddies,
        "email": email or (_user_email(existing) if existing else ""),
        "preferences": website_preferences.get_preferences(existing or {}),
    }
    if existing and existing.get("push_subscriptions"):
        user_data["push_subscriptions"] = existing["push_subscriptions"]
    _set_user(tvn_username, user_data)
    _set_authenticated_user(tvn_username)

    app.logger.info(
        "api.done request_id=%s action=register success=True user=%s buddies=%d",
        request_id, tvn_username, len(buddies),
    )
    return jsonify({
        "ok": True,
        "username": tvn_username,
        "play_date": _today_et(),
        "display_name": display_name,
        "primary": primary,
        "buddies": buddies,
        "email": user_data.get("email", ""),
    })


@app.route("/api/refresh-buddies", methods=["POST"])
def refresh_buddies():
    """Re-scrape the buddy list from the Villages system."""
    err = require_auth()
    if err:
        return err
    request_id = _request_id()
    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"ok": False, "error": "No user selected."}), 400

    result = get_worker().call(
        "fetch_buddy_list",
        request_id=request_id,
        action="refresh_buddies",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
    )

    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error", "Failed to refresh.")}), 400

    def merge_buddies(profile):
        fresh_buddies = list(result.get("buddies", []))
        scraped_ids = {b["id"] for b in fresh_buddies}
        for old_buddy in profile.get("buddies", []):
            if old_buddy["id"] not in scraped_ids:
                fresh_buddies.append(old_buddy)
        profile["primary"] = result.get("primary")
        profile["buddies"] = fresh_buddies
    user = user_store.update_user(username, merge_buddies)
    if not user:
        return jsonify({"ok": False, "error": "Please sign in again."}), 401

    return jsonify({"ok": True, "primary": user["primary"], "buddies": user["buddies"]})


@app.route("/api/add-buddy", methods=["POST"])
def add_buddy():
    """Manually add a golfer by ID to the current user's buddy cache."""
    err = require_auth()
    if err:
        return err
    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"ok": False, "error": "No user selected."}), 400

    data = request.get_json() or {}
    golfer_id = (data.get("id") or "").strip()
    golfer_name = (data.get("name") or "").strip() or f"Golfer #{golfer_id}"

    if not golfer_id or not golfer_id.isdigit():
        return jsonify({"ok": False, "error": "A valid numeric golfer ID is required."}), 400

    # Check for duplicate
    existing_ids = {b["id"] for b in user.get("buddies", [])}
    if golfer_id in existing_ids:
        return jsonify({"ok": False, "error": "That golfer is already in your list."}), 400

    from golf_service import _initials
    buddy = {"id": golfer_id, "name": golfer_name, "initials": _initials(golfer_name)}
    def append_buddy(profile):
        buddies = profile.setdefault("buddies", [])
        if not any(b["id"] == golfer_id for b in buddies):
            buddies.append(buddy)
    user = user_store.update_user(username, append_buddy)
    if not user:
        return jsonify({"ok": False, "error": "Please sign in again."}), 401

    return jsonify({"ok": True, "buddy": buddy, "buddies": user["buddies"]})


@app.route("/api/update-email", methods=["POST"])
def update_email():
    """Update the notification email for the current user."""
    err = require_auth()
    if err:
        return err
    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"ok": False, "error": "No user selected."}), 400

    data = request.get_json() or {}
    email = (data.get("email") or "").strip()
    if email and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
        return jsonify({"ok": False, "error": "Enter a valid email address."}), 400
    if not user_store.update_user(username, lambda u: u.update(email=email)):
        return jsonify({"ok": False, "error": "Please sign in again."}), 401

    return jsonify({"ok": True, "email": email})


@app.route("/api/remove-user", methods=["POST"])
def remove_user():
    """Remove the currently logged-in user's OWN cached profile.

    The target is taken from the session, never the request body, so a logged-in
    golfer cannot delete anyone else's account (IDOR). The session is fully
    cleared afterward to avoid a half-authenticated state.
    """
    err = require_auth()
    if err:
        return err
    username = session.get("username")
    if not username:
        return jsonify({"ok": False, "error": "No user selected."}), 400
    _delete_user(username)
    try:
        request_watches.cancel_watches_for_user(username)
        request_watches.cancel_outbox_for_user(username)
    except Exception:
        pass
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/tee-times", methods=["POST"])
def get_tee_times():
    """Fetch available tee times from The Villages system."""
    request_id = _request_id()
    started = time.monotonic()
    err = require_auth()
    if err:
        app.logger.info("api.denied request_id=%s action=tee_times reason=not_authenticated", request_id)
        return err

    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"success": False, "error": "No user selected."}), 400

    data = request.get_json() or {}
    parsed, parse_err = _parse_booking_inputs(data, require_course_time=False)
    if parse_err:
        return jsonify({"success": False, "error": parse_err}), 400

    app.logger.info(
        "api.start request_id=%s action=tee_times user=%s date=%s course_type=%s num_golfers=%s",
        request_id,
        username,
        parsed["date_str"],
        parsed["course_type"],
        parsed["num_golfers"],
    )
    result = get_worker().call(
        "get_available_times",
        request_id=request_id,
        action="tee_times",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
        date_str     = parsed["date_str"],
        date_label   = data.get("date_label", ""),
        course_type  = parsed["course_type"],
        region_filter = parsed["region_filter"],
        golfer_ids   = parsed["golfer_ids"],
        num_golfers  = parsed["num_golfers"],
        has_guests   = parsed["has_guests"],
        time_filter  = data.get("time_filter"),
    )
    if result.get("success") and (parsed["time_from"] or parsed["time_to"]):
        from golf_service import _parse_time, _to_real_hour
        def in_window(item):
            hour, minute = _parse_time(item.get("time", ""))
            if hour < 0 or minute < 0:
                return False
            value = f"{_to_real_hour(hour):02d}:{minute:02d}"
            return ((not parsed["time_from"] or value >= parsed["time_from"])
                    and (not parsed["time_to"] or value <= parsed["time_to"]))
        result["times"] = [item for item in result.get("times", []) if in_window(item)]
    elapsed_ms = int((time.monotonic() - started) * 1000)
    app.logger.info(
        "api.done request_id=%s action=tee_times duration_ms=%s success=%s count=%s",
        request_id,
        elapsed_ms,
        bool(result.get("success")),
        len(result.get("times", [])) if isinstance(result.get("times"), list) else 0,
    )
    return jsonify(result)


@app.route("/api/my-tee-times", methods=["GET"])
def my_tee_times():
    """Fetch the current user's outstanding tee times/reservations."""
    request_id = _request_id()
    started = time.monotonic()
    err = require_auth()
    if err:
        app.logger.info("api.denied request_id=%s action=my_tee_times reason=not_authenticated", request_id)
        return err

    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"success": False, "error": "No user selected."}), 400

    app.logger.info("api.start request_id=%s action=my_tee_times user=%s", request_id, username)
    result = get_worker().call(
        "view_my_tee_times",
        request_id=request_id,
        action="my_tee_times",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    app.logger.info(
        "api.done request_id=%s action=my_tee_times duration_ms=%s success=%s count=%s",
        request_id,
        elapsed_ms,
        bool(result.get("success")),
        len(result.get("reservations", [])) if isinstance(result.get("reservations"), list) else 0,
    )
    return jsonify(result)


@app.route("/api/delete-reservation", methods=["POST"])
def delete_reservation():
    """Delete all players on the specified reservation."""
    request_id = _request_id()
    started = time.monotonic()
    err = require_auth()
    if err:
        app.logger.info("api.denied request_id=%s action=delete_reservation reason=not_authenticated", request_id)
        return err

    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"success": False, "error": "No user selected."}), 400

    data = request.get_json() or {}
    reservation_no = (data.get("reservation_no") or "").strip()
    if not reservation_no:
        return jsonify({"success": False, "error": "Reservation number is required."}), 400

    app.logger.info(
        "api.start request_id=%s action=delete_reservation user=%s reservation_no=%s",
        request_id,
        username,
        reservation_no,
    )
    result = get_worker().call(
        "delete_reservation",
        request_id=request_id,
        action="delete_reservation",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
        reservation_no=reservation_no,
        exclusive_key=username,
    )
    if result.get("busy"):
        return jsonify(result), 409
    elapsed_ms = int((time.monotonic() - started) * 1000)
    app.logger.info(
        "api.done request_id=%s action=delete_reservation duration_ms=%s success=%s",
        request_id,
        elapsed_ms,
        bool(result.get("success")),
    )
    if result.get("success"):
        try:
            request_watches.cancel_outbox_ref("push_reminder", reservation_no)
            request_watches.cancel_outbox_ref("push_booking", reservation_no)
        except Exception:
            app.logger.exception("push.cancel_reminder_failed request_id=%s", request_id)
    return jsonify(result)


@app.route("/api/book", methods=["POST"])
def book_tee_time():
    """Book a specific tee time."""
    request_id = _request_id()
    started = time.monotonic()
    err = require_auth()
    if err:
        app.logger.info("api.denied request_id=%s action=book reason=not_authenticated", request_id)
        return err

    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"success": False, "error": "No user selected."}), 400

    data = request.get_json() or {}
    parsed, parse_err = _parse_booking_inputs(data, require_course_time=True)
    if parse_err:
        return jsonify({"success": False, "error": parse_err}), 400
    search_id = data.get("search_id", "").strip()
    if not search_id:
        return jsonify({"success": False, "error": "Refresh the page and fetch tee times again before booking."}), 400
    course_name, time_str = parsed["course_name"], parsed["time_str"]

    app.logger.info(
        "api.start request_id=%s action=book user=%s course=%s time=%s",
        request_id,
        username,
        course_name,
        time_str,
    )
    result = get_worker().call(
        "book_tee_time",
        request_id=request_id,
        action="book",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
        course_name=course_name,
        time_str=time_str,
        expected_date=parsed["date_str"],
        expected_golfer_ids=parsed["golfer_ids"],
        expected_num_golfers=parsed["num_golfers"],
        expected_course_type=parsed["course_type"],
        expected_has_guests=parsed["has_guests"],
        search_id=search_id,
        exclusive_key=username,
    )
    if result.get("busy"):
        return jsonify(result), 409
    elapsed_ms = int((time.monotonic() - started) * 1000)
    app.logger.info(
        "api.done request_id=%s action=book duration_ms=%s success=%s",
        request_id,
        elapsed_ms,
        bool(result.get("success")),
    )

    # Enqueue confirmation emails on success into the durable outbox
    # (synchronously, before returning) so a booking that already committed
    # can never lose its confirmation to a crashed or timed-out send: a
    # background pass, or the loop's own next pass, retries it later. The
    # booking is already committed at this point, so nothing here may turn
    # a successful booking into a failed response: any problem building or
    # enqueuing the confirmation is logged and swallowed.
    if result.get("success"):
        outbox_ids = []
        try:
            recipients = _booking_recipients(
                result.get("golfer_ids", []), result, result.get("course", course_name), username,
            )
            for recipient in recipients:
                outbox_ids.append(request_watches.enqueue_notification(
                    "booking_confirmation", recipient["username"], recipient["email"], recipient["payload"],
                ))
            app.logger.info("email.enqueued request_id=%s count=%d", request_id, len(recipients))
        except Exception:
            app.logger.exception("email.enqueue_failed request_id=%s", request_id)

        # Phone notifications, in their own guard so nothing here can change the
        # booking response or the email path. Only a confirmed success (never
        # "unknown") reaches this point.
        if push_notifications.is_configured():
            try:
                outbox_ids.extend(_enqueue_push_for_booking(result, username, request_id))
            except Exception:
                app.logger.exception("push.enqueue_failed request_id=%s", request_id)

        # One thread delivers exactly the rows this booking queued, right away.
        # It never touches reminders or other due rows (the loops own those).
        if outbox_ids and os.environ.get("DISABLE_REQUEST_WATCHER") != "1":
            try:
                threading.Thread(
                    target=_send_outbox_rows,
                    args=(list(outbox_ids),),
                    name=f"outbox-{request_id}",
                    daemon=True,
                ).start()
            except Exception:
                app.logger.exception("outbox.thread_failed request_id=%s", request_id)

    return jsonify(result)


@app.route("/api/request-courses", methods=["POST"])
def request_courses():
    """Fetch the list of available courses for a given date + course type from
    the Requests page. Used to populate the course-preference picker."""
    request_id = _request_id()
    err = require_auth()
    if err:
        return err

    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"success": False, "error": "No user selected."}), 400

    data = request.get_json() or {}
    play_date = (data.get("play_date") or "").strip()
    course_type = (data.get("course_type") or "Championship").strip()
    any_course = bool(data.get("any_course") or False)
    if not request_watches.normalize_play_date(play_date):
        return jsonify({"success": False, "error": "A valid play date is required."}), 400
    if course_type not in ("Championship", "Executive"):
        return jsonify({"success": False, "error": "Invalid course type."}), 400

    result = get_worker().call(
        "fetch_request_courses",
        request_id=request_id,
        action="request_courses",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
        play_date=play_date,
        course_type=course_type,
        any_course=any_course,
    )
    return jsonify(result)


@app.route("/api/submit-request", methods=["POST"])
def submit_request_route():
    """Submit a tee-time request."""
    request_id = _request_id()
    err = require_auth()
    if err:
        return err

    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"success": False, "error": "No user selected."}), 400

    data = request.get_json() or {}
    play_date = (data.get("play_date") or "").strip()
    course_type = (data.get("course_type") or "").strip()
    preference = (data.get("preference") or "").strip()
    course_choices = data.get("course_choices") or []
    golfer_ids = data.get("golfer_ids") or []
    try:
        max_golfers = int(data.get("max_golfers") or 0)
    except (TypeError, ValueError):
        max_golfers = 0

    if not request_watches.normalize_play_date(play_date):
        return jsonify({"success": False, "error": "A valid play date is required."}), 400
    if max_golfers < 1 or max_golfers > 4:
        return jsonify({"success": False, "error": "Max golfers must be between 1 and 4."}), 400
    if course_type not in ("Championship", "Executive"):
        return jsonify({"success": False, "error": "Invalid course type."}), 400
    if preference not in ("Course", "Time"):
        return jsonify({"success": False, "error": "Preference must be Course or Time."}), 400
    if not isinstance(course_choices, list) or not course_choices:
        return jsonify({"success": False, "error": "At least one course must be selected."}), 400
    if not isinstance(golfer_ids, list) or not golfer_ids:
        return jsonify({"success": False, "error": "At least one golfer is required."}), 400
    golfer_ids = [str(g).strip() for g in golfer_ids]
    if (len(golfer_ids) > max_golfers or len(set(golfer_ids)) != len(golfer_ids)
            or any(not re.fullmatch(r"\d{1,20}", g) for g in golfer_ids)):
        return jsonify({"success": False, "error": "Choose distinct golfers within the selected maximum."}), 400
    if len(course_choices) > 100 or any(not isinstance(c, str) or not c.strip() or len(c) > 200 for c in course_choices):
        return jsonify({"success": False, "error": "Choose valid courses from the course list."}), 400
    course_choices = list(dict.fromkeys(c.strip() for c in course_choices))
    time_to_play = data.get("time_to_play", "").strip()
    earliest_time = data.get("earliest_time", "").strip()
    latest_time = data.get("latest_time", "").strip()
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in (time_to_play, earliest_time, latest_time)):
        return jsonify({"success": False, "error": "Choose a preferred time, earliest time, and latest time."}), 400
    if not earliest_time <= time_to_play <= latest_time:
        return jsonify({"success": False, "error": "Preferred time must fall between earliest and latest time."}), 400

    result = get_worker().call(
        "submit_request",
        request_id=request_id,
        action="submit_request",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
        play_date=play_date,
        max_golfers=max_golfers,
        has_guests=bool(data.get("has_guests") or False),
        course_type=course_type,
        any_course=bool(data.get("any_course") or False),
        time_to_play=time_to_play,
        earliest_time=earliest_time,
        latest_time=latest_time,
        preference=preference,
        course_choices=[str(c).strip() for c in course_choices if str(c).strip()],
        golfer_ids=[str(g).strip() for g in golfer_ids if str(g).strip()],
        exclusive_key=username,
    )
    if result.get("busy"):
        return jsonify(result), 409

    # Auto-enroll the request for result notification: after the ~1 AM lottery
    # (3 days before play) we'll check whether it was assigned and email the user.
    if result.get("success"):
        try:
            request_watches.add_watch(
                username=username,
                request_no=result.get("request_no"),
                play_date=play_date,
                play_date_label=request_watches.friendly_date(play_date),
                courses=course_choices,
                email=_user_email(user),
                preferences={"earliest_time": earliest_time, "latest_time": latest_time,
                             "golfer_ids": golfer_ids, "any_course": data.get("any_course", False)},
            )
        except Exception as e:
            app.logger.warning("request_watch.enroll_error request_id=%s error=%s", request_id, e)

    return jsonify(result)


@app.route("/api/my-requests", methods=["GET"])
def my_requests_route():
    """List the user's pending tee-time requests."""
    request_id = _request_id()
    err = require_auth()
    if err:
        return err

    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"success": False, "error": "No user selected."}), 400

    result = get_worker().call(
        "view_my_requests",
        request_id=request_id,
        action="my_requests",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
    )

    # Enroll any pending request for result notification (covers requests the
    # golfer submitted by phone or the Villages website, not just via the app).
    if result.get("success"):
        email = _user_email(user)
        for req in result.get("requests", []) or []:
            try:
                request_watches.add_watch(
                    username=username,
                    request_no=req.get("request_id"),
                    play_date=req.get("date"),
                    play_date_label=request_watches.friendly_date(req.get("date")),
                    courses=[],
                    email=email,
                )
            except Exception:
                pass

    return jsonify(result)


@app.route("/api/delete-request", methods=["POST"])
def delete_request_route():
    """Cancel a pending tee-time request."""
    request_id = _request_id()
    err = require_auth()
    if err:
        return err

    user = _get_session_user()
    username = session.get("username")
    if not user or not username:
        return jsonify({"success": False, "error": "No user selected."}), 400

    data = request.get_json() or {}
    rid = (data.get("request_id") or "").strip()
    if not rid:
        return jsonify({"success": False, "error": "Request ID is required."}), 400

    result = get_worker().call(
        "delete_request",
        request_id=request_id,
        action="delete_request",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
        target_request_id=rid,
        exclusive_key=username,
    )
    if result.get("busy"):
        return jsonify(result), 409
    if result.get("success"):
        request_watches.cancel_request_watch(username, rid)
    return jsonify(result)


_PUSH_TEST_LIMIT = 5
_PUSH_TEST_WINDOW_SECONDS = 600
_push_test_times = {}  # username -> [time.time() of each test request]
_push_test_lock = threading.Lock()


def _push_unavailable():
    if push_notifications.is_configured():
        return None
    return jsonify({"success": False, "error": push_notifications.NOT_SET_UP_MESSAGE}), 503


_PUSH_SUBSCRIBE_LIMIT = 30
_push_subscribe_times = {}  # username -> [time.time() of each subscribe request]


def _rate_limited(store, username, limit, window=_PUSH_TEST_WINDOW_SECONDS):
    now = time.time()
    with _push_test_lock:
        recent = [t for t in store.get(username, []) if now - t < window]
        if len(recent) >= limit:
            store[username] = recent
            return True
        recent.append(now)
        store[username] = recent
        return False


def _push_test_rate_limited(username):
    return _rate_limited(_push_test_times, username, _PUSH_TEST_LIMIT)


@app.route("/api/push/config", methods=["GET"])
def push_config():
    body = {
        "enabled": push_notifications.is_configured(),
        "public_key": push_notifications.public_key(),
    }
    # A signed-in device that names its endpoint learns whether the server holds it.
    endpoint = request.headers.get("X-Push-Endpoint") or request.args.get("endpoint")
    if body["enabled"] and endpoint and session.get("auth") and session.get("username"):
        try:
            profile = _get_user(session.get("username")) or {}
            body["device_registered"] = any(
                isinstance(s, dict) and s.get("endpoint") == endpoint
                for s in (profile.get("push_subscriptions") or [])
            )
        except Exception:
            app.logger.exception("push.config_lookup_failed")
            body["device_registered"] = False
    return jsonify(body)


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    err = require_auth()
    if err:
        return err
    unavailable = _push_unavailable()
    if unavailable:
        return unavailable
    username = session.get("username")
    if not username or not _get_user(username):
        return jsonify({"success": False, "error": "No user selected."}), 400
    data = request.get_json(silent=True) or {}
    subscription = push_notifications.validate_subscription(data.get("subscription"))
    if subscription is None:
        return jsonify({"success": False, "error": "That notification subscription is not valid."}), 400
    # Only valid requests count: they are the ones that write profiles.
    if _rate_limited(_push_subscribe_times, username, _PUSH_SUBSCRIBE_LIMIT):
        return jsonify({"success": False, "error": "Too many requests. Try again in a few minutes."}), 429
    try:
        # Drop the device from every other account first, so a failure part-way
        # leaves it with nobody rather than with the wrong person.
        # Every subscribe removes the endpoint from other accounts, so an endpoint
        # this user already holds is held by no one else: skip the all-profiles sweep.
        already_mine = any(
            isinstance(s, dict) and s.get("endpoint") == subscription["endpoint"]
            for s in ((_get_user(username) or {}).get("push_subscriptions") or [])
        )
        if not already_mine:
            push_notifications.remove_endpoint_from_others(subscription["endpoint"], username)
        stored = push_notifications.add_subscription(username, subscription)
    except Exception:
        app.logger.exception("push.subscribe_failed")
        return jsonify({"success": False, "error": "Could not turn on notifications. Please try again."}), 500
    if not stored:
        return jsonify({"success": False, "error": "No user selected."}), 400
    return jsonify({"success": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    err = require_auth()
    if err:
        return err
    unavailable = _push_unavailable()
    if unavailable:
        return unavailable
    username = session.get("username")
    endpoint = (request.get_json(silent=True) or {}).get("endpoint")
    if isinstance(endpoint, str) and username:
        try:
            push_notifications.remove_endpoint(username, endpoint)
        except Exception:
            app.logger.exception("push.unsubscribe_failed")
            return jsonify({"success": False, "error": "Could not turn off notifications. Please try again."}), 500
    return jsonify({"success": True})


@app.route("/api/push/test", methods=["POST"])
def push_test():
    err = require_auth()
    if err:
        return err
    unavailable = _push_unavailable()
    if unavailable:
        return unavailable
    username = session.get("username")
    if not username or not _get_user(username):
        return jsonify({"success": False, "error": "No user selected."}), 400
    if _push_test_rate_limited(username):
        return jsonify({
            "success": False, "sent": 0, "failed": 0,
            "error": "Too many test notifications. Try again in a few minutes.",
        }), 429
    totals = push_notifications.send_to_users(
        [username], push_notifications.device_test_payload(), push_notifications.BOOKING_TTL_SECONDS,
    )
    if totals["devices"] == 0:
        return jsonify({
            "success": False, "sent": 0, "failed": 0,
            "error": "No devices are set up for notifications on this account.",
        })
    return jsonify({"success": totals["sent"] > 0, "sent": totals["sent"], "failed": totals["failed"]})


@app.route("/api/logout", methods=["POST"])
def logout():
    # A device that signs out stops receiving this account's notifications.
    # Nothing here may stop the sign-out itself.
    try:
        endpoint = (request.get_json(silent=True) or {}).get("push_endpoint")
        username = session.get("username")
        if isinstance(endpoint, str) and endpoint and username:
            push_notifications.remove_endpoint(username, endpoint)
    except Exception:
        app.logger.exception("push.logout_cleanup_failed")
    session.clear()
    return jsonify({"ok": True})


# Start the request-result watcher at import so it runs under gunicorn too
# (not just direct `python app.py`). Idempotent enough: one daemon per process,
# and the worker runs --workers 1 so there is exactly one watcher.
_start_request_watcher()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
