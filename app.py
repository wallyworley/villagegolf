"""
Villages Golf Booking App — Flask Backend
Serves the frontend and provides API endpoints for
fetching tee times and booking via The Villages system.
"""

import hmac
import logging
import os
import queue
import threading
import time
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request, session, send_from_directory
load_dotenv()  # loads .env when running locally; no-op in Cloud Run

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

app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
)

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


def _worker_public_error(action):
    return _WORKER_PUBLIC_ERRORS.get(
        action,
        "Could not complete that request right now. Please try again.",
    )


# ── User store (SQLite) ─────────────────────────────────────────────────────
# Self-contained local store — no external cloud dependency. See user_store.py.
import user_store


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
from email_notifications import send_booking_confirmation


# ── Golf Worker ───────────────────────────────────────────────────────────────
class GolfWorker:
    """Single-threaded worker so Playwright objects stay on one thread."""

    def __init__(self):
        self._job_q = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="golf-worker",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=10)
        if not self._ready.is_set():
            raise RuntimeError("Golf worker failed to start")

    def _run(self):
        from golf_service import GolfService

        service = GolfService()
        self._ready.set()

        while True:
            job = self._job_q.get()
            if job is None:
                break

            method_name, kwargs, done = job
            try:
                method = getattr(service, method_name)
                done["result"] = method(**kwargs)
            except Exception as exc:
                done["error"] = exc
            finally:
                done["event"].set()

    def call(self, method_name, request_id="-", action=None, **kwargs):
        if not self._thread.is_alive():
            raise RuntimeError("Golf worker thread is not running")

        action = action or method_name
        started = time.monotonic()
        app.logger.info(
            "worker.start request_id=%s action=%s method=%s",
            request_id,
            action,
            method_name,
        )

        done = {"event": threading.Event(), "result": None, "error": None}
        self._job_q.put((method_name, kwargs, done))

        if not done["event"].wait(timeout=240):
            elapsed_ms = int((time.monotonic() - started) * 1000)
            app.logger.error(
                "worker.timeout request_id=%s action=%s duration_ms=%s",
                request_id,
                action,
                elapsed_ms,
            )
            return {"success": False, "error": "Internal timeout waiting for booking worker."}
        if done["error"] is not None:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            app.logger.exception(
                "worker.error request_id=%s action=%s duration_ms=%s",
                request_id,
                action,
                elapsed_ms,
                exc_info=done["error"],
            )
            return {"success": False, "error": _worker_public_error(action)}
        elapsed_ms = int((time.monotonic() - started) * 1000)
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


def get_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker._thread.is_alive():
            _worker = GolfWorker()
    return _worker


# ── Helpers ───────────────────────────────────────────────────────────────────
def require_auth():
    if not session.get("auth"):
        return jsonify({"error": "Not authenticated"}), 401
    return None


def _request_id():
    incoming = (request.headers.get("X-Request-ID") or "").strip()
    return incoming or uuid.uuid4().hex[:12]


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
    return hmac.compare_digest(str(stored), str(supplied))


def _parse_booking_inputs(data, require_course_time=False):
    """Validate and normalize booking/search payload from frontend."""
    date_str = (data.get("date") or "").strip()
    if not date_str:
        return None, "Date is required."

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
    if num_golfers <= 0:
        return None, "Number of golfers must be at least 1."
    if num_golfers != len(golfer_ids):
        return None, "Number of golfers must match selected golfers."

    region_filter = (data.get("region_filter") or "all").strip().lower()
    if region_filter not in {"all", "north", "central", "south"}:
        return None, "Region must be north, central, south, or all."

    parsed = {
        "date_str": date_str,
        "course_type": course_type,
        "region_filter": region_filter,
        "golfer_ids": golfer_ids,
        "num_golfers": num_golfers,
        "has_guests": bool(data.get("has_guests", False)),
    }

    if require_course_time:
        course_name = (data.get("course") or "").strip()
        time_str = (data.get("time") or "").strip()
        if not course_name or not time_str:
            return None, "Course and time are required."
        parsed["course_name"] = course_name
        parsed["time_str"] = time_str

    return parsed, None


# ── Email notification helper ────────────────────────────────────────────────
def _user_email(user_data):
    """Return the preferred notification email for a user record."""
    return (
        user_data.get("email")
        or user_data.get("imessage_address")
        or ""
    ).strip()


def _send_booking_emails(date_label, golfer_ids, booking_result,
                         course_name, request_id, booking_username=None):
    """Send confirmation emails.

    Sends to: (1) the user who made the booking, and (2) any other registered
    user whose primary golfer ID is in the booking."""
    res_no = booking_result.get("reservation_no", "—")
    display_time = booking_result.get("display_time", "")

    users_snapshot = _all_users()

    # Collect display names for all golfers in the booking
    golfer_names = []
    for gid in golfer_ids:
        for u in users_snapshot.values():
            matched = next(
                (b["name"] for b in u.get("buddies", []) if b["id"] == str(gid)),
                None,
            )
            if matched:
                golfer_names.append(matched)
                break

    golfer_id_strs = [str(g) for g in golfer_ids]
    emails_sent = set()

    def _notify(email):
        """Send a booking confirmation to one email address."""
        if email in emails_sent:
            return
        emails_sent.add(email)
        send_booking_confirmation(
            email, res_no, course_name, display_time,
            date_label, golfer_names,
        )

    # Always notify the user who made the booking
    if booking_username and booking_username in users_snapshot:
        email = _user_email(users_snapshot[booking_username])
        if email:
            _notify(email)
            app.logger.info("email.sent_booking_user request_id=%s user=%s", request_id, booking_username)

    # Also notify any other registered user whose primary ID is in the booking
    for uname, udata in users_snapshot.items():
        email = _user_email(udata)
        if not email:
            continue
        primary_id = (udata.get("primary") or {}).get("id")
        if primary_id and str(primary_id) in golfer_id_strs:
            _notify(email)
            app.logger.info("email.sent_golfer request_id=%s user=%s", request_id, uname)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


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
            "display_name": user.get("display_name", username),
            "primary": user.get("primary"),
            "buddies": user.get("buddies", []),
            "email": _user_email(user),
        })
    return jsonify({"auth": True, "username": None})


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
        app.logger.info("api.done request_id=%s action=register success=False", request_id)
        return jsonify({"ok": False, "error": result.get("error", "Registration failed.")}), 400

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
    }
    _set_user(tvn_username, user_data)
    _set_authenticated_user(tvn_username)

    app.logger.info(
        "api.done request_id=%s action=register success=True user=%s buddies=%d",
        request_id, tvn_username, len(buddies),
    )
    return jsonify({
        "ok": True,
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

    # Merge: keep manually-added buddies that aren't in the fresh scrape
    fresh_buddies = result.get("buddies", [])
    scraped_ids = {b["id"] for b in fresh_buddies}
    for old_buddy in user.get("buddies", []):
        if old_buddy["id"] not in scraped_ids:
            fresh_buddies.append(old_buddy)

    user["primary"] = result.get("primary")
    user["buddies"] = fresh_buddies
    _set_user(username, user)

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
    user.setdefault("buddies", []).append(buddy)
    _set_user(username, user)

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
    user["email"] = email
    _set_user(username, user)

    return jsonify({"ok": True, "email": email})


@app.route("/api/remove-user", methods=["POST"])
def remove_user():
    """Remove a cached user profile."""
    err = require_auth()
    if err:
        return err
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Username required."}), 400
    _delete_user(username)
    if session.get("username") == username:
        session.pop("username", None)
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
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    app.logger.info(
        "api.done request_id=%s action=delete_reservation duration_ms=%s success=%s",
        request_id,
        elapsed_ms,
        bool(result.get("success")),
    )
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
    course_name = (data.get("course") or "").strip()
    time_str = (data.get("time") or "").strip()
    if not course_name or not time_str:
        return jsonify({"success": False, "error": "Course and time are required."}), 400

    app.logger.info(
        "api.start request_id=%s action=book user=%s course=%s time=%s",
        request_id,
        username,
        course_name,
        time_str,
    )
    data_date_label = (data.get("date_label") or "").strip()
    data_golfer_ids = data.get("golfer_ids") or []

    result = get_worker().call(
        "book_tee_time",
        request_id=request_id,
        action="book",
        tvn_username=username,
        tvn_password=user["tvn_password"],
        golf_password=user["golf_password"],
        course_name=course_name,
        time_str=time_str,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    app.logger.info(
        "api.done request_id=%s action=book duration_ms=%s success=%s",
        request_id,
        elapsed_ms,
        bool(result.get("success")),
    )

    # Send confirmation emails on success — fire-and-forget on a background
    # thread so a slow or failing mail call never blocks (or crashes) the
    # request worker. The booking is already committed at this point.
    if result.get("success"):
        app.logger.info(
            "email.trigger request_id=%s golfer_ids=%s",
            request_id, data_golfer_ids,
        )

        def _email_task():
            try:
                _send_booking_emails(
                    data_date_label, data_golfer_ids, result,
                    course_name, request_id, booking_username=username,
                )
            except Exception as e:
                app.logger.warning("email.notification_error request_id=%s error=%s", request_id, e)

        threading.Thread(
            target=_email_task,
            name=f"email-{request_id}",
            daemon=True,
        ).start()

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
    if not play_date:
        return jsonify({"success": False, "error": "Play date is required."}), 400
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

    if not play_date:
        return jsonify({"success": False, "error": "Play date is required."}), 400
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
        time_to_play=(data.get("time_to_play") or "").strip(),
        earliest_time=(data.get("earliest_time") or "").strip(),
        latest_time=(data.get("latest_time") or "").strip(),
        preference=preference,
        course_choices=[str(c).strip() for c in course_choices if str(c).strip()],
        golfer_ids=[str(g).strip() for g in golfer_ids if str(g).strip()],
    )
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
    )
    return jsonify(result)


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
