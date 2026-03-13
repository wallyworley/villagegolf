"""
Transactional email helpers backed by the MailerSend Email API.
"""

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_MAILERSEND_API_URL = "https://api.mailersend.com/v1/email"


def _mask_email(email):
    """Mask an email address for logs."""
    if not email or "@" not in email:
        return "?"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        local_mask = "*" * len(local)
    else:
        local_mask = local[:1] + "*" * (len(local) - 2) + local[-1:]
    return f"{local_mask}@{domain}"


def _mailersend_config():
    token = (os.environ.get("MAILERSEND_API_TOKEN") or "").strip()
    from_email = (os.environ.get("MAIL_FROM_EMAIL") or "").strip()
    from_name = (os.environ.get("MAIL_FROM_NAME") or "Villages Golf").strip()
    enabled = bool(token and from_email)
    return {
        "enabled": enabled,
        "token": token,
        "from_email": from_email,
        "from_name": from_name,
    }


def send_email(recipient_email, subject, text_body, html_body=None):
    """Send one transactional email through MailerSend."""
    cfg = _mailersend_config()
    if not recipient_email or not subject or not text_body:
        log.warning("email.skip missing recipient, subject, or text body")
        return False
    if not cfg["enabled"]:
        log.warning("email.skip mailersend not configured recipient=%s", _mask_email(recipient_email))
        return False

    payload = {
        "from": {
            "email": cfg["from_email"],
            "name": cfg["from_name"],
        },
        "to": [
            {
                "email": recipient_email,
            }
        ],
        "subject": subject,
        "text": text_body,
        "html": html_body or f"<pre>{text_body}</pre>",
    }

    req = urllib.request.Request(
        _MAILERSEND_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg['token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "VillagesGolfApp/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            message_id = resp.headers.get("x-message-id", "")
            log.info(
                "email.sent recipient=%s status=%s message_id=%s",
                _mask_email(recipient_email),
                getattr(resp, "status", "?"),
                message_id,
            )
            return True
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<unreadable>"
        log.warning(
            "email.fail recipient=%s status=%s body=%s",
            _mask_email(recipient_email),
            exc.code,
            body,
        )
        return False
    except Exception as exc:
        log.warning("email.error recipient=%s error=%s", _mask_email(recipient_email), exc)
        return False


def send_booking_confirmation(recipient_email, reservation_no, course, display_time,
                              date_label, golfer_names):
    """Send a booking confirmation email."""
    golfers_str = ", ".join(golfer_names) if golfer_names else "N/A"
    subject = f"Tee Time Confirmed: {course} at {display_time}"
    text = (
        f"Tee Time Confirmed!\n\n"
        f"Reservation #{reservation_no}\n"
        f"Date: {date_label}\n"
        f"Time: {display_time}\n"
        f"Course: {course}\n"
        f"Golfers: {golfers_str}\n"
    )
    html = (
        "<p><strong>Tee Time Confirmed!</strong></p>"
        f"<p>Reservation #{reservation_no}<br>"
        f"Date: {date_label}<br>"
        f"Time: {display_time}<br>"
        f"Course: {course}<br>"
        f"Golfers: {golfers_str}</p>"
    )
    return send_email(recipient_email, subject, text, html)
