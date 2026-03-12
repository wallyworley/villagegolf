"""
Villages Golf Booking App — Flask Backend
Serves the frontend and provides API endpoints for
fetching tee times and booking via The Villages system.
"""

import json
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
APP_PIN = os.environ.get("APP_PIN", "1234")

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    logging.warning("SECRET_KEY not set — sessions will not survive restarts")
    SECRET_KEY = os.urandom(24).hex()
app.secret_key = SECRET_KEY

app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
)

# ── PIN rate limiting ─────────────────────────────────────────────────────────
_pin_attempts = {}  # ip -> {"count": int, "locked_until": float}
_pin_lock = threading.Lock()
_MAX_PIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 minutes


def _check_pin_rate(ip):
    """Return error string if IP is locked out, else None."""
    with _pin_lock:
        rec = _pin_attempts.get(ip)
        if not rec:
            return None
        if rec["locked_until"] and time.time() < rec["locked_until"]:
            remaining = int(rec["locked_until"] - time.time())
            return f"Too many attempts. Try again in {remaining}s."
        if rec["locked_until"] and time.time() >= rec["locked_until"]:
            del _pin_attempts[ip]
        return None


def _record_pin_failure(ip):
    with _pin_lock:
        rec = _pin_attempts.setdefault(ip, {"count": 0, "locked_until": None})
        rec["count"] += 1
        if rec["count"] >= _MAX_PIN_ATTEMPTS:
            rec["locked_until"] = time.time() + _LOCKOUT_SECONDS


def _clear_pin_attempts(ip):
    with _pin_lock:
        _pin_attempts.pop(ip, None)


# ── User store ────────────────────────────────────────────────────────────────
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
_users = {}
_users_lock = threading.Lock()


def _load_users():
    global _users
    with _users_lock:
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r") as f:
                    _users = json.load(f)
            except Exception:
                _users = {}
        return dict(_users)


def _save_users():
    with _users_lock:
        with open(USERS_FILE, "w") as f:
            json.dump(_users, f, indent=2)


def _get_user(username):
    with _users_lock:
        return _users.get(username)


def _set_user(username, data):
    with _users_lock:
        _users[username] = data
    _save_users()


def _delete_user(username):
    with _users_lock:
        _users.pop(username, None)
    _save_users()


_load_users()


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
            return {"success": False, "error": f"Worker error: {done['error']}"}
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

    parsed = {
        "date_str": date_str,
        "course_type": course_type,
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

    # Collect display names for all golfers in the booking
    golfer_names = []
    for gid in golfer_ids:
        with _users_lock:
            for u in _users.values():
                for b in u.get("buddies", []):
                    if b["id"] == str(gid):
                        golfer_names.append(b["name"])
                        break

    with _users_lock:
        users_snapshot = dict(_users)

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


@app.route("/api/verify-pin", methods=["POST"])
def verify_pin():
    """Check the app PIN and set a session cookie."""
    ip = request.remote_addr or "unknown"
    lockout = _check_pin_rate(ip)
    if lockout:
        return jsonify({"ok": False, "error": lockout}), 429

    data = request.get_json() or {}
    if str(data.get("pin", "")).strip() == str(APP_PIN).strip():
        session["auth"] = True
        _clear_pin_attempts(ip)
        return jsonify({"ok": True})

    _record_pin_failure(ip)
    return jsonify({"ok": False, "error": "Incorrect PIN"}), 401


@app.route("/api/users", methods=["GET"])
def list_users():
    """Return list of cached user profiles (no passwords)."""
    err = require_auth()
    if err:
        return err
    users_safe = []
    with _users_lock:
        for username, u in _users.items():
            users_safe.append({
                "username": username,
                "display_name": u.get("display_name", username),
                "primary": u.get("primary"),
                "initials": u.get("primary", {}).get("initials", "?"),
            })
    return jsonify({"users": users_safe})


@app.route("/api/select-user", methods=["POST"])
def select_user():
    """Select a cached user profile for this session."""
    err = require_auth()
    if err:
        return err
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    user = _get_user(username)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    session["username"] = username
    return jsonify({
        "ok": True,
        "display_name": user.get("display_name", username),
        "primary": user.get("primary"),
        "buddies": user.get("buddies", []),
        "email": _user_email(user),
    })


@app.route("/api/clear-user", methods=["POST"])
def clear_user():
    """Clear selected user from the current authenticated session."""
    err = require_auth()
    if err:
        return err
    session.pop("username", None)
    return jsonify({"ok": True})


@app.route("/api/register", methods=["POST"])
def register_user():
    """Register a new user: login to Villages, fetch buddy list, cache profile."""
    err = require_auth()
    if err:
        return err
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
    session["username"] = tvn_username

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

    # Send confirmation emails on success
    if result.get("success"):
        app.logger.info(
            "email.trigger request_id=%s golfer_ids=%s",
            request_id, data_golfer_ids,
        )
        try:
            _send_booking_emails(
                data_date_label, data_golfer_ids, result,
                course_name, request_id, booking_username=username,
            )
        except Exception as e:
            app.logger.warning("email.notification_error request_id=%s error=%s", request_id, e)

    return jsonify(result)


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
