"""
Web push notifications for booked tee times.

This module owns everything push-specific: VAPID configuration, subscription
validation, the reminder schedule, notification copy, and sending. pywebpush is
imported only here. Subscriptions live in the encrypted user profile under
``push_subscriptions``; the durable queue is ``request_watches.notification_outbox``.

Nothing here raises on a send failure: a push problem must never affect a
booking, a deletion, or a login.
"""

import base64
import ipaddress
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import request_watches
import user_store

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc

MAX_SUBSCRIPTIONS = 5
MAX_ENDPOINT_LENGTH = 1024
MAX_P256DH_LENGTH = 200
MAX_AUTH_LENGTH = 64
ALLOWED_HOSTS = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "web.push.apple.com",
    "notify.windows.com",
)
BOOKING_TTL_SECONDS = 6 * 3600
MAX_PAYLOAD_BYTES = 3000
SEND_TIMEOUT_SECONDS = 10
NOT_SET_UP_MESSAGE = "Notifications are not set up on this server."

# Printable ASCII URL characters. Backslash and percent are deliberately absent:
# parsers disagree about them, which is how an endpoint could name one host to
# this validator and another to the HTTP client.
# "%" is allowed only after the host (Windows push tokens are percent-encoded);
# validate_endpoint rejects it anywhere in the scheme, host, or port part.
_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_TIME = re.compile(r"\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*", re.IGNORECASE)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ── Configuration ────────────────────────────────────────────────────────────

def _b64url_decode(value):
    text = str(value).strip().rstrip("=")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def derive_public_key(private_key):
    """Return the base64url uncompressed public point for a raw P-256 private key,
    or None when the private key is not a valid 32-byte value."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        raw = _b64url_decode(private_key)
        if len(raw) != 32:
            return None
        key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
        point = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return _b64url_encode(point)
    except Exception:
        return None


_PRIVATE_KEY = ""
_PUBLIC_KEY = ""
_SUBJECT = "https://villagefairways.com"
_CONFIGURED = False


def _load_config():
    """Read the VAPID settings from the environment. Both keys must be present and
    the public key must be the one derived from the private key; otherwise push
    stays off and one ERROR line explains why (never the key values)."""
    global _PRIVATE_KEY, _PUBLIC_KEY, _SUBJECT, _CONFIGURED
    private = (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()
    public = (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()
    _SUBJECT = (os.environ.get("VAPID_SUBJECT") or "").strip() or "https://villagefairways.com"
    _PRIVATE_KEY, _PUBLIC_KEY, _CONFIGURED = "", "", False
    if not private and not public:
        return
    if not private or not public:
        log.error("push.config_invalid reason=one_key_missing; notifications are off")
        return
    derived = derive_public_key(private)
    if derived is None or derived != public.rstrip("="):
        log.error("push.config_invalid reason=keys_do_not_match; notifications are off")
        return
    _PRIVATE_KEY, _PUBLIC_KEY, _CONFIGURED = private, derived, True


_load_config()


def is_configured():
    return _CONFIGURED


def public_key():
    return _PUBLIC_KEY if _CONFIGURED else None


# ── Subscription validation ──────────────────────────────────────────────────

def _host_allowed(host):
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS)


def _url_parser():
    """Return (parse_url, LocationParseError) from urllib3, imported on first use so
    a missing or broken install can never stop the app from starting; None if
    it cannot be imported (validation then fails closed)."""
    try:
        from urllib3.exceptions import LocationParseError
        from urllib3.util import parse_url
        return parse_url, LocationParseError
    except Exception:
        return None


def validate_endpoint(endpoint):
    """Return True only for an https push-service URL on an allowed host.

    Two independent parsers (urlsplit and urllib3, which requests uses to
    connect) must agree on scheme, host, and port, and the host must be a plain
    DNS name on the allowlist."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        return False
    if not _URL_CHARS.fullmatch(endpoint):
        return False
    parser = _url_parser()
    if parser is None:
        return False
    parse_url, location_error = parser
    try:
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").lower()
        port = parts.port
        other = parse_url(endpoint)
    except (ValueError, location_error):
        return False
    if "%" in parts.netloc or "%" in endpoint.split("//", 1)[0]:
        return False
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return False
    if not _HOSTNAME.fullmatch(host):
        return False
    if port not in (None, 443):
        return False
    if (other.scheme or "").lower() != "https" or (other.host or "").lower() != host or other.port not in (None, 443):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # IP literal
    except ValueError:
        pass
    return _host_allowed(host)


def validate_subscription(raw):
    """Return a clean {"endpoint", "keys": {"p256dh", "auth"}} dict, or None."""
    if not isinstance(raw, dict):
        return None
    endpoint = raw.get("endpoint")
    keys = raw.get("keys")
    if not validate_endpoint(endpoint) or not isinstance(keys, dict):
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    for value, limit in ((p256dh, MAX_P256DH_LENGTH), (auth, MAX_AUTH_LENGTH)):
        if not isinstance(value, str) or not value or len(value) > limit or not _BASE64URL.fullmatch(value):
            return None
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


# ── Stored subscriptions ─────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_subscription(username, subscription):
    """Upsert a validated subscription into one profile (same endpoint replaces,
    newest last, at most MAX_SUBSCRIPTIONS kept). Returns False when the profile
    does not exist."""
    entry = dict(subscription, created_at=_now_iso())

    def apply(profile):
        current = [s for s in (profile.get("push_subscriptions") or [])
                   if isinstance(s, dict) and s.get("endpoint") != entry["endpoint"]]
        current.append(entry)
        profile["push_subscriptions"] = current[-MAX_SUBSCRIPTIONS:]

    return user_store.update_user(username, apply) is not None


def remove_endpoint(username, endpoint):
    """Remove one endpoint from one profile. Returns True if it was present."""
    removed = []

    def apply(profile):
        current = profile.get("push_subscriptions") or []
        kept = [s for s in current if not (isinstance(s, dict) and s.get("endpoint") == endpoint)]
        if len(kept) != len(current):
            removed.append(True)
            profile["push_subscriptions"] = kept

    user_store.update_user(username, apply)
    return bool(removed)


def remove_endpoint_from_others(endpoint, keep_username):
    """One device belongs to whoever is signed in on it: drop the endpoint from
    every other profile."""
    for username, profile in user_store.all_users().items():
        if username == keep_username:
            continue
        if any(isinstance(s, dict) and s.get("endpoint") == endpoint
               for s in (profile.get("push_subscriptions") or [])):
            remove_endpoint(username, endpoint)


# ── Reminder schedule ────────────────────────────────────────────────────────

def tee_time_utc(play_date, display_time):
    """Return the tee time as an aware UTC datetime, or None when unparseable.
    Both values come from the stored booking, never from the site's own format."""
    if not isinstance(play_date, str) or not _DATE.fullmatch(play_date.strip()):
        return None
    match = _TIME.fullmatch(display_time) if isinstance(display_time, str) else None
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if not 1 <= hour <= 12 or minute > 59:
        return None
    try:
        day = datetime.strptime(play_date.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    hour = hour % 12 + (12 if meridiem == "PM" else 0)
    tee = datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET)
    return tee.astimezone(_UTC)


def reminder_time_utc(play_date, display_time, now_utc):
    """Return when to send the reminder (aware UTC), or None for no reminder.

    Twelve hours before the tee time in Eastern wall-clock time. A reminder that
    would land at or after 9:00 PM or before 7:00 AM Eastern moves to 8:00 PM
    Eastern on the evening before the tee date. A time already past is None."""
    tee_utc = tee_time_utc(play_date, display_time)
    if tee_utc is None:
        return None
    tee = tee_utc.astimezone(_ET)
    remind = tee - timedelta(hours=12)
    if remind.hour >= 21 or remind.hour < 7:
        evening = tee.date() - timedelta(days=1)
        remind = datetime(evening.year, evening.month, evening.day, 20, 0, tzinfo=_ET)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=_UTC)
    remind_utc = remind.astimezone(_UTC)
    if remind_utc <= now_utc:
        return None
    return remind_utc


# ── Copy ─────────────────────────────────────────────────────────────────────

def _fit(payload):
    """Keep a payload under MAX_PAYLOAD_BYTES by trimming the body."""
    while len(json.dumps(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES and payload["body"]:
        payload["body"] = payload["body"][: max(0, len(payload["body"]) - 100)]
    return payload


def _date_label(booking):
    return booking.get("date_label") or request_watches.friendly_date(booking.get("play_date")) or ""


def booking_payload(booking):
    reservation_no = booking.get("reservation_no", "")
    return _fit({
        "title": "Tee time booked",
        "body": f"{booking.get('course', '')}, {_date_label(booking)} at "
                f"{booking.get('display_time', '')}. Reservation {reservation_no}.",
        "tag": f"booking-{reservation_no}",
        "url": "/",
    })


def reminder_payload(booking, now_utc=None):
    """The reminder carries the booking's own date, not a relative word, so the
    text stays true however late it is delivered. `now_utc` is accepted for
    callers that pass it and is not used."""
    reservation_no = booking.get("reservation_no", "")
    return _fit({
        "title": "Tee time reminder",
        "body": f"{_date_label(booking)} at {booking.get('display_time', '')}: "
                f"{booking.get('course', '')}. Reservation {reservation_no}.",
        "tag": f"reminder-{reservation_no}",
        "url": "/",
    })


def device_test_payload():
    return {"title": "Village Fairways", "body": "Notifications are working on this device.",
            "tag": "test", "url": "/"}



# ── Sending ──────────────────────────────────────────────────────────────────

_SESSION_CLASS = None


def _new_session():
    """A requests session that never follows redirects. A push service has no
    reason to redirect, and following one would let a hostile endpoint steer this
    server's POST elsewhere; a 3xx is returned as is and treated as a failure.
    requests is imported on first use. Raises ImportError if it is missing, which
    send_to_subscription turns into "failed"."""
    global _SESSION_CLASS
    if _SESSION_CLASS is None:
        import requests

        class NoRedirectSession(requests.Session):
            def request(self, method, url, **kwargs):
                kwargs["allow_redirects"] = False
                return super().request(method, url, **kwargs)

        _SESSION_CLASS = NoRedirectSession
    return _SESSION_CLASS()


def send_to_subscription(subscription, payload, ttl):
    """Send one push. Returns "sent", "gone" (the push service says the
    subscription no longer exists), or "failed". Never raises."""
    try:
        if not _CONFIGURED or not validate_endpoint((subscription or {}).get("endpoint")):
            return "failed"
        from pywebpush import WebPushException, webpush

        session = _new_session()
        try:
            webpush(
                subscription_info={"endpoint": subscription["endpoint"], "keys": dict(subscription["keys"])},
                data=json.dumps(payload),
                vapid_private_key=_PRIVATE_KEY,
                vapid_claims={"sub": _SUBJECT},
                ttl=int(ttl),
                timeout=SEND_TIMEOUT_SECONDS,
                requests_session=session,
            )
            return "sent"
        except WebPushException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status in (404, 410):
                return "gone"
            log.warning("push.send_failed status=%s", status)
            return "failed"
        finally:
            session.close()
    except Exception as error:
        log.warning("push.send_failed error=%s", type(error).__name__)
        return "failed"


def send_to_users(usernames, payload, ttl):
    """Send to every stored device of each named user. Subscriptions the push
    service reports gone are removed. Returns device and outcome counts."""
    totals = {"devices": 0, "sent": 0, "failed": 0, "gone": 0}
    for username in dict.fromkeys(u for u in (usernames or []) if u):
        try:
            profile = user_store.get_user(username)
        except Exception:
            log.warning("push.profile_unreadable")
            continue
        if not isinstance(profile, dict):
            continue
        gone = []
        for stored in profile.get("push_subscriptions") or []:
            subscription = validate_subscription(stored)
            totals["devices"] += 1
            if subscription is None:
                totals["failed"] += 1
                continue
            outcome = send_to_subscription(subscription, payload, ttl)
            totals[outcome] += 1
            if outcome == "gone":
                gone.append(subscription["endpoint"])
        for endpoint in gone:
            try:
                remove_endpoint(username, endpoint)
            except Exception:
                log.warning("push.remove_gone_failed")
    return totals
