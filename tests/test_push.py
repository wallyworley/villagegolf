"""Offline regressions for phone push notifications. No real push is ever sent:
pywebpush.webpush is mocked, the golf worker is a Mock, and keys are throwaway."""
import collections
import importlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_vapid_keys  # noqa: E402
from test_pwa import run_node, visible_text  # noqa: E402

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
NOTIFY_JS = (ROOT / "static" / "js" / "notify.js").read_text(encoding="utf-8")
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
HELP = (ROOT / "templates" / "help.html").read_text(encoding="utf-8")

PRIVATE, PUBLIC = generate_vapid_keys.generate()
OTHER_PRIVATE, OTHER_PUBLIC = generate_vapid_keys.generate()

FCM = "https://fcm.googleapis.com/fcm/send/"
KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
        "auth": "tBHItJI5svbpez7KI4CCXg"}
BANNED = re.compile(r"\b(simply|just|easily|currently|soon|click|tap|etc|now|new|above|below)\b|\be\.g\.|\bi\.e\.", re.I)


def subscription(name, host="fcm.googleapis.com"):
    return {"endpoint": f"https://{host}/send/{name}", "keys": dict(KEYS)}


def utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class PushTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(os.environ, {
            "SECRET_KEY": "test-only-session-key", "USER_DB_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "DISABLE_REQUEST_WATCHER": "1", "SESSION_COOKIE_SECURE": "0", "TRUST_PROXY": "0",
        })
        cls.environment.start()
        with patch("dotenv.load_dotenv"):
            cls.api = importlib.import_module("app")
        cls.api.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        cls.push = cls.api.push_notifications

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        api = self.api
        api.user_store._DB_PATH = self.tmp.name + "/users.db"
        api.user_store._initialized = False
        api.user_store._fernet = Fernet(Fernet.generate_key())
        api.request_watches._DB_PATH = api.user_store._DB_PATH
        api.request_watches._initialized = False
        api._worker_activity.clear()
        api._push_test_times.clear()
        api._push_subscribe_times.clear()
        patcher = patch.object(api, "_watch_pause")
        patcher.start()
        self.addCleanup(patcher.stop)
        api._route_attempts.clear()
        self.profiles()
        self.worker = Mock()
        self.worker.call.return_value = {"success": False, "unknown": True, "error": "Fixture unconfirmed"}
        patcher = patch.object(api, "get_worker", return_value=self.worker)
        patcher.start()
        self.addCleanup(patcher.stop)

    # Helpers -----------------------------------------------------------------
    def profiles(self):
        store = self.api.user_store
        base = {"tvn_password": "fixture", "golf_password": "1234", "buddies": []}
        store.set_user("golfer", dict(base, email="golfer@example.invalid", primary={"id": "111", "name": "Test Golfer"}))
        store.set_user("member", dict(base, primary={"id": "222", "name": "Test Member"}))  # no email
        store.set_user("other", dict(base, email="other@example.invalid", primary={"id": "333", "name": "Not In Group"}))

    def configure(self, on=True):
        patcher = patch.multiple(self.push, _CONFIGURED=on, _PRIVATE_KEY=PRIVATE if on else "",
                                 _PUBLIC_KEY=PUBLIC if on else "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def client_for(self, username=None):
        client = self.api.app.test_client()
        if username:
            with client.session_transaction() as session:
                session["auth"] = True
                session["username"] = username
        return client

    def stored(self, username):
        return [s["endpoint"] for s in (self.api.user_store.get_user(username).get("push_subscriptions") or [])]

    def subscribe(self, client, sub):
        return client.post("/api/push/subscribe", json={"subscription": sub})

    def outbox(self, kind=None):
        with closing(self.api.request_watches._connect()) as conn:
            sql = "SELECT * FROM notification_outbox" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
            return [dict(r) for r in conn.execute(sql, (kind,) if kind else ()).fetchall()]

    def outbox_row(self, outbox_id):
        with closing(self.api.request_watches._connect()) as conn:
            return dict(conn.execute("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)).fetchone())

    def add_devices(self, username, *names):
        for name in names:
            sub = subscription(name)
            self.assertTrue(self.push.add_subscription(username, sub))

    # 1 -----------------------------------------------------------------------
    def test_reminder_time_table(self):
        cases = [
            ("2026-10-17", "7:18 AM", utc(2026, 10, 14, 12), utc(2026, 10, 16, 23, 18)),
            ("2026-10-17", "8:59 AM", utc(2026, 10, 14, 12), utc(2026, 10, 17, 0, 59)),
            ("2026-10-17", "9:00 AM", utc(2026, 10, 14, 12), utc(2026, 10, 17, 0, 0)),
            ("2026-10-17", "1:00 PM", utc(2026, 10, 14, 12), utc(2026, 10, 17, 0, 0)),
            ("2026-10-17", "7:00 PM", utc(2026, 10, 14, 12), utc(2026, 10, 17, 11, 0)),
            ("2026-11-01", "8:00 AM", utc(2026, 10, 25, 12), utc(2026, 11, 1, 0, 0)),
            ("2026-10-17", "6:30 PM", utc(2026, 10, 14, 12), utc(2026, 10, 17, 0, 0)),
            ("2026-10-17", "7:18 AM", utc(2026, 10, 16, 23, 30), None),
            ("2026-10-17", "25:00 PM", utc(2026, 10, 14, 12), None),
        ]
        for play_date, display_time, now, expected in cases:
            with self.subTest(play_date=play_date, display_time=display_time):
                self.assertEqual(self.push.reminder_time_utc(play_date, display_time, now), expected)
        for bad_date, bad_time in (("2026-1-5", "7:18 AM"), ("20261017", "7:18 AM"), ("2026-10-17", "0:30 AM"),
                                   ("2026-10-17", "7:60 AM"), ("2026-10-17", "07:18"), ("2026-02-30", "7:18 AM"),
                                   (None, "7:18 AM"), ("2026-10-17", None)):
            self.assertIsNone(self.push.reminder_time_utc(bad_date, bad_time, utc(2026, 10, 14, 12)), (bad_date, bad_time))

    # 2 -----------------------------------------------------------------------
    def test_subscription_validation_rejects_unsafe_endpoints(self):
        self.configure()
        client = self.client_for("golfer")
        bad_endpoints = [
            "http://fcm.googleapis.com/send/x",
            "https://127.0.0.1/send/x",
            "https://[::1]/send/x",
            "https://example.com/send/x",
            "https://fcm.googleapis.com.evil.example/send/x",
            "https://evilfcm.googleapis.com/send/x",
            "https://user@fcm.googleapis.com/send/x",
            "https://fcm.googleapis.com:8443/send/x",
            "https://fcm.googleapis.com/" + "a" * 2000,
            "ftp://fcm.googleapis.com/x",
            "",
            # Parser disagreement: urlsplit and urllib3 read these as different hosts.
            "https://127.0.0.1\\.fcm.googleapis.com/x",
            "https://127.0.0.1\\@fcm.googleapis.com/x",
            "https://evil.example\\@fcm.googleapis.com/x",
            "https://fcm.googleapis.com\\@127.0.0.1/x",
            "https://127.0.0.1%5c.fcm.googleapis.com/x",
            "https://fcm.googleapis.com%2f@127.0.0.1/x",
            "https://fcm%2egoogleapis.com/x",
            "https%3a//fcm.googleapis.com/x",
            "https://fcm.googleapis.com@127.0.0.1/x",
            "https://fcm.googleapis.com:443@127.0.0.1/x",
            "https://127.0.0.1#.fcm.googleapis.com/x",
            "https://127.0.0.1?.fcm.googleapis.com/x",
            "https://m\u00fcnchen.fcm.googleapis.com/x",
            "https://fcm.googleapis.com\u2024evil.example/x",
            "https://fcm.googleapis.com./x",
            "https://-bad.fcm.googleapis.com/x",
            "https://fcm..googleapis.com/x",
            "https://fcm.googleapis.com/x y",
            "https://fcm.googleapis.com/x\n",
            "https://2130706433/x",
            "https://localhost/x",
        ]
        for endpoint in bad_endpoints:
            with self.subTest(endpoint=endpoint[:60]):
                self.assertEqual(self.subscribe(client, {"endpoint": endpoint, "keys": dict(KEYS)}).status_code, 400)
        for keys in ({"p256dh": "bad key!", "auth": KEYS["auth"]}, {"p256dh": KEYS["p256dh"], "auth": "a" * 65},
                     {"p256dh": "a" * 201, "auth": KEYS["auth"]}, {"p256dh": KEYS["p256dh"]}, {"p256dh": "", "auth": "x"},
                     {"p256dh": 5, "auth": "x"}, "nope"):
            with self.subTest(keys=str(keys)[:40]):
                self.assertEqual(self.subscribe(client, {"endpoint": FCM + "x", "keys": keys}).status_code, 400)
        self.assertEqual(client.post("/api/push/subscribe", json={"subscription": "text"}).status_code, 400)
        self.assertEqual(client.post("/api/push/subscribe", json={}).status_code, 400)
        self.assertEqual(self.stored("golfer"), [])
        # Property: whatever the validator accepts, both parsers name the same host.
        from urllib.parse import urlsplit
        from urllib3.util import parse_url
        for endpoint in bad_endpoints + [FCM + "ok", "https://xn--mnchen-3ya.fcm.googleapis.com/x"]:
            if self.push.validate_endpoint(endpoint):
                self.assertEqual(urlsplit(endpoint).hostname, parse_url(endpoint).host, endpoint)
        self.assertEqual(self.subscribe(client, subscription("one")).status_code, 200)
        self.assertEqual(self.subscribe(client, subscription("idn", "xn--mnchen-3ya.fcm.googleapis.com")).status_code, 200)
        self.assertEqual(self.subscribe(client, subscription("two", "web.push.apple.com")).status_code, 200)
        self.assertEqual(self.subscribe(client, subscription("three", "updates.push.services.mozilla.com")).status_code, 200)
        # "%" after the host is allowed (Windows push tokens are percent-encoded); the host stays fixed.
        windows = "https://wns2-par02p.notify.windows.com/w/?token=BQYAAAB%2bAbc%3d"
        self.assertTrue(self.push.validate_endpoint(windows))
        self.assertEqual(urlsplit(windows).hostname, parse_url(windows).host)
        padded = {"endpoint": "https://wns2.notify.windows.com/x", "keys": {"p256dh": "AAAA==", "auth": "BBBB="}}
        self.assertEqual(self.subscribe(client, padded).status_code, 200)
        self.assertEqual(len(self.stored("golfer")), 5)

    def test_push_send_does_not_follow_redirects(self):
        import base64

        import requests
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.configure()
        point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        sub = {"endpoint": FCM + "redirect",
               "keys": {"p256dh": base64.urlsafe_b64encode(point).decode().rstrip("="),
                        "auth": base64.urlsafe_b64encode(b"0123456789abcdef").decode().rstrip("=")}}
        seen = []

        class Redirecting(requests.adapters.HTTPAdapter):
            def send(self, request, **kwargs):
                seen.append(request.url)
                response = requests.Response()
                response.status_code = 302
                response.reason = "Found"
                response.headers["Location"] = "http://127.0.0.1:9/internal"
                response.url = request.url
                response._content = b""
                response.request = request
                return response

        make_session = self.push._new_session

        def session():
            built = make_session()
            built.mount("https://", Redirecting())
            built.mount("http://", Redirecting())
            return built

        with patch.object(self.push, "_new_session", side_effect=session):
            self.assertEqual(self.push.send_to_subscription(sub, {"title": "t"}, 60), "failed")
        self.assertEqual(seen, [sub["endpoint"]], "a 3xx must not be followed")
        # A plain session would have followed it; this proves the adapter alone would have seen two requests.
        seen.clear()
        plain = requests.Session()
        plain.mount("https://", Redirecting())
        plain.mount("http://", Redirecting())
        try:
            plain.post(sub["endpoint"], data=b"x", timeout=1)
        except Exception:
            pass
        self.assertGreaterEqual(len(seen), 2)
        self.assertIsInstance(self.push._new_session(), requests.Session)
        # Stored subscriptions are re-validated at send time: a bad old row never sends.
        with patch("pywebpush.webpush") as webpush:
            bad = {"endpoint": "https://127.0.0.1\\.fcm.googleapis.com/x", "keys": dict(KEYS)}
            self.assertEqual(self.push.send_to_subscription(bad, {"title": "t"}, 60), "failed")
            webpush.assert_not_called()
        self.api.user_store.update_user("golfer", lambda p: p.update(push_subscriptions=[
            {"endpoint": "https://127.0.0.1\\.fcm.googleapis.com/x", "keys": dict(KEYS), "created_at": "x"}]))
        with patch("pywebpush.webpush") as webpush:
            totals = self.push.send_to_users(["golfer"], {"title": "t"}, 60)
            webpush.assert_not_called()
        self.assertEqual((totals["devices"], totals["sent"], totals["failed"]), (1, 0, 1))

    # 3 -----------------------------------------------------------------------
    def test_subscribe_moves_device_between_accounts(self):
        self.configure()
        first, second = self.client_for("golfer"), self.client_for("member")
        shared = subscription("shared")
        self.assertEqual(self.subscribe(first, shared).status_code, 200)
        self.assertEqual(self.stored("golfer"), [shared["endpoint"]])
        self.assertEqual(self.subscribe(second, shared).status_code, 200)
        self.assertEqual(self.stored("golfer"), [])
        self.assertEqual(self.stored("member"), [shared["endpoint"]])
        self.assertEqual(self.subscribe(second, shared).status_code, 200)  # same endpoint replaces
        self.assertEqual(self.stored("member"), [shared["endpoint"]])
        entry = self.api.user_store.get_user("member")["push_subscriptions"][0]
        self.assertEqual(set(entry), {"endpoint", "keys", "created_at"})
        for index in range(6):
            self.assertEqual(self.subscribe(first, subscription(f"device{index}")).status_code, 200)
        endpoints = self.stored("golfer")
        self.assertEqual(len(endpoints), 5)
        self.assertNotIn(subscription("device0")["endpoint"], endpoints)
        self.assertEqual(endpoints[-1], subscription("device5")["endpoint"])
        self.assertEqual(self.client_for("golfer").post(
            "/api/push/unsubscribe", json={"endpoint": subscription("device5")["endpoint"]}).json, {"success": True})
        self.assertNotIn(subscription("device5")["endpoint"], self.stored("golfer"))
        absent = self.client_for("golfer").post("/api/push/unsubscribe", json={"endpoint": "https://nowhere.example/x"})
        self.assertEqual(absent.json, {"success": True})

    # 4 -----------------------------------------------------------------------
    def test_logout_removes_the_device_endpoint(self):
        self.configure()
        client = self.client_for("golfer")
        keep, drop = subscription("keep"), subscription("drop")
        self.subscribe(client, keep)
        self.subscribe(client, drop)
        self.assertEqual(client.post("/api/logout", json={"push_endpoint": drop["endpoint"]}).status_code, 200)
        self.assertEqual(self.stored("golfer"), [keep["endpoint"]])
        with client.session_transaction() as session:
            self.assertFalse(session.get("auth"))
        for body in ({"push_endpoint": 5}, {"push_endpoint": None}, {}):
            again = self.client_for("golfer")
            self.assertEqual(again.post("/api/logout", json=body).status_code, 200)
        self.assertEqual(self.client_for("golfer").post("/api/logout").status_code, 200)
        self.assertEqual(self.stored("golfer"), [keep["endpoint"]])
        with patch.object(self.push, "remove_endpoint", side_effect=RuntimeError("boom")):
            client = self.client_for("golfer")
            response = client.post("/api/logout", json={"push_endpoint": keep["endpoint"]})
            self.assertEqual(response.status_code, 200)
            with client.session_transaction() as session:
                self.assertFalse(session.get("auth"))

    # 5 -----------------------------------------------------------------------
    def test_push_routes_require_auth_and_configuration(self):
        self.configure()
        anonymous = self.client_for()
        for path in ("/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test"):
            self.assertEqual(anonymous.post(path, json={}).status_code, 401, path)
        config = anonymous.get("/api/push/config")
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.json, {"enabled": True, "public_key": PUBLIC})
        self.configure(False)
        signed_in = self.client_for("golfer")
        for path in ("/api/push/subscribe", "/api/push/unsubscribe", "/api/push/test"):
            response = signed_in.post(path, json={"subscription": subscription("x"), "endpoint": FCM + "x"})
            self.assertEqual(response.status_code, 503, path)
            self.assertEqual(response.json, {"success": False, "error": "Notifications are not set up on this server."})
        self.assertEqual(anonymous.get("/api/push/config").json, {"enabled": False, "public_key": None})
        self.assertEqual(self.stored("golfer"), [])

    # 6 -----------------------------------------------------------------------
    def test_mismatched_vapid_keys_disable_push(self):
        push = self.push

        def reload_with(env):
            with patch.dict(os.environ, env):
                for name in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY"):
                    if name not in env:
                        os.environ.pop(name, None)
                importlib.reload(push)

        try:
            with self.assertLogs("push_notifications", level="ERROR") as logs:
                reload_with({"VAPID_PRIVATE_KEY": PRIVATE, "VAPID_PUBLIC_KEY": OTHER_PUBLIC})
            self.assertFalse(push.is_configured())
            self.assertIsNone(push.public_key())
            self.assertEqual(len(logs.records), 1)
            for secret in (PRIVATE, OTHER_PUBLIC, PUBLIC):
                self.assertNotIn(secret, "\n".join(logs.output))
            self.assertEqual(push.send_to_subscription(subscription("x"), {"title": "t"}, 60), "failed")
            with self.assertLogs("push_notifications", level="ERROR"):
                reload_with({"VAPID_PRIVATE_KEY": PRIVATE})  # public key missing
            self.assertFalse(push.is_configured())
            with self.assertLogs("push_notifications", level="ERROR"):
                reload_with({"VAPID_PRIVATE_KEY": "not-a-key", "VAPID_PUBLIC_KEY": PUBLIC})
            self.assertFalse(push.is_configured())
            reload_with({})
            self.assertFalse(push.is_configured())
            reload_with({"VAPID_PRIVATE_KEY": PRIVATE, "VAPID_PUBLIC_KEY": PUBLIC + "=", "VAPID_SUBJECT": "mailto:a@example.invalid"})
            self.assertTrue(push.is_configured())
            self.assertEqual(push.public_key(), PUBLIC)
            self.assertEqual(push._SUBJECT, "mailto:a@example.invalid")
        finally:
            reload_with({})
        self.assertFalse(push.is_configured())

    # 7 -----------------------------------------------------------------------
    def booking_result(self, play_date, display_time="7:18 AM", reservation_no="654321"):
        return {"success": True, "golfer_ids": ["111", "222"], "reservation_no": reservation_no,
                "course": "Silver Lake", "display_time": display_time, "play_date": play_date,
                "date_label": "Saturday, 10/17/2026"}

    def book(self, client=None):
        body = {"date": "20261017", "course_type": "Executive", "golfer_ids": ["111", "222"], "num_golfers": 2,
                "has_guests": False, "course": "Silver Lake", "time": "07:18", "search_id": "test-search"}
        return (client or self.client_for("golfer")).post("/api/book", json=body)

    def test_booking_enqueues_push_booking_and_reminder(self):
        self.configure()
        play_date = (datetime.now(ET).date() + timedelta(days=5)).isoformat()
        self.worker.call.return_value = self.booking_result(play_date)
        response = self.book()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        booking_rows, reminder_rows = self.outbox("push_booking"), self.outbox("push_reminder")
        self.assertEqual((len(booking_rows), len(reminder_rows)), (1, 1))
        booking, reminder = booking_rows[0], reminder_rows[0]
        for row in (booking, reminder):
            self.assertEqual((row["username"], row["email"], row["status"], row["attempts"]), ("golfer", "", "pending", 0))
            payload = json.loads(row["payload"])
            self.assertEqual(payload["recipients"], ["golfer", "member"])  # the member has no email
            self.assertEqual(payload["booker"], "golfer")
            self.assertEqual((payload["reservation_no"], payload["course"], payload["display_time"], payload["play_date"]),
                             ("654321", "Silver Lake", "7:18 AM", play_date))
            self.assertEqual(payload["date_label"], "Saturday, 10/17/2026")
        day = datetime.fromisoformat(play_date)
        expected_tee = datetime(day.year, day.month, day.day, 7, 18, tzinfo=ET).astimezone(UTC)
        self.assertEqual(json.loads(reminder["payload"])["tee_at_utc"], expected_tee.isoformat())
        before = day - timedelta(days=1)
        expected_remind = datetime(before.year, before.month, before.day, 19, 18, tzinfo=ET).astimezone(UTC)
        self.assertEqual(reminder["notify_after"], expected_remind.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(reminder["ref"], "654321")
        self.assertLessEqual(booking["notify_after"], datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"))
        emails = self.outbox("booking_confirmation")
        self.assertEqual([e["username"] for e in emails], ["golfer"])
        self.assertEqual(emails[0]["email"], "golfer@example.invalid")
        self.assertIn("golfer_names", json.loads(emails[0]["payload"]))

        # A reminder time already past enqueues only the booking notification.
        with closing(self.api.request_watches._connect()) as conn, conn:
            conn.execute("DELETE FROM notification_outbox")
        today = datetime.now(ET).date().isoformat()
        self.worker.call.return_value = self.booking_result(today, "12:05 AM", "777777")
        self.assertTrue(self.book().json["success"])
        self.assertEqual((len(self.outbox("push_booking")), len(self.outbox("push_reminder"))), (1, 0))
        self.assertEqual(len(self.outbox("booking_confirmation")), 1)

        # An unconfirmed booking enqueues nothing at all.
        with closing(self.api.request_watches._connect()) as conn, conn:
            conn.execute("DELETE FROM notification_outbox")
        for unconfirmed in ({"success": False, "unknown": True, "error": "unsure"},
                            {"success": False, "error": "failed"}, {"success": False, "busy": True}):
            self.worker.call.return_value = unconfirmed
            self.book()
        self.assertEqual(self.outbox(), [])

        # A push problem never changes the booking response.
        self.worker.call.return_value = self.booking_result(play_date, reservation_no="888888")
        with patch.object(self.api, "_enqueue_push_for_booking", side_effect=RuntimeError("boom")):
            response = self.book()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        self.assertEqual(response.json["reservation_no"], "888888")
        self.assertEqual(len(self.outbox("booking_confirmation")), 1)

    def test_booking_without_push_configured_is_unchanged(self):
        self.configure(False)
        self.worker.call.return_value = self.booking_result("2030-01-05")
        self.assertTrue(self.book().json["success"])
        self.assertEqual([r["kind"] for r in self.outbox()], ["booking_confirmation"])

    # 8 -----------------------------------------------------------------------
    def test_delete_cancels_the_pending_reminder(self):
        self.configure()
        play_date = (datetime.now(ET).date() + timedelta(days=5)).isoformat()
        self.worker.call.return_value = self.booking_result(play_date)
        self.assertTrue(self.book().json["success"])  # the real enqueue path
        self.worker.call.return_value = self.booking_result(play_date, reservation_no="111111")
        self.assertTrue(self.book().json["success"])
        mine = [r["id"] for r in self.outbox() if r["ref"] == "654321" and r["kind"].startswith("push_")]
        other = [r["id"] for r in self.outbox() if r["ref"] == "111111" and r["kind"].startswith("push_")]
        self.assertEqual(len(mine), 2)
        self.assertEqual(len(other), 2)
        email = [r["id"] for r in self.outbox("booking_confirmation")]
        client = self.client_for("golfer")
        self.worker.call.return_value = {"success": False, "error": "Could not delete"}
        client.post("/api/delete-reservation", json={"reservation_no": "654321"})
        self.assertEqual({self.outbox_row(i)["status"] for i in mine}, {"pending"})
        self.worker.call.return_value = {"success": False, "busy": True}
        self.assertEqual(client.post("/api/delete-reservation", json={"reservation_no": "654321"}).status_code, 409)
        self.assertEqual({self.outbox_row(i)["status"] for i in mine}, {"pending"})
        self.worker.call.return_value = {"success": True, "reservation_no": "654321", "reservations": [], "verified": True}
        response = client.post("/api/delete-reservation", json={"reservation_no": "654321"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        self.assertEqual({self.outbox_row(i)["status"] for i in mine}, {"cancelled"})
        self.assertEqual({self.outbox_row(i)["status"] for i in other}, {"pending"})
        self.assertEqual({self.outbox_row(i)["status"] for i in email}, {"pending"})
        # A failure while cancelling never changes a verified delete.
        watches = self.api.request_watches
        again = watches.enqueue_notification("push_reminder", "golfer", "", {}, notify_after=utc(2030, 1, 1), ref="654321")
        with patch.object(watches, "cancel_outbox_ref", side_effect=RuntimeError("boom")):
            response = client.post("/api/delete-reservation", json={"reservation_no": "654321"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        self.assertEqual(self.outbox_row(again)["status"], "pending")

    # 9 -----------------------------------------------------------------------
    def reminder_row(self, **changes):
        payload = {"reservation_no": "654321", "course": "Silver Lake", "display_time": "7:18 AM",
                   "date_label": "Saturday, 10/17/2026", "play_date": "2026-10-17", "booker": "golfer",
                   "recipients": ["golfer", "member"],
                   "tee_at_utc": self.push.tee_time_utc("2026-10-17", "7:18 AM").isoformat()}
        payload.update(changes)
        return self.api.request_watches.enqueue_notification(
            "push_reminder", payload["booker"], "", payload, notify_after=utc(2000, 1, 1), ref=payload["reservation_no"])

    def fake_now(self, moment):
        real = datetime
        holder = [moment]  # tests can move the clock by assigning holder[0]

        class Clock(real):
            @classmethod
            def now(cls, tz=None):
                return holder[0] if tz else holder[0].replace(tzinfo=None)

        patcher = patch.object(self.api, "datetime", Clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return holder

    def view_calls(self):
        return [c for c in self.worker.call.call_args_list if c.args[0] == "view_my_tee_times"]

    def release_calls(self):
        return [c for c in self.worker.call.call_args_list if c.args[0] == "release_session"]

    def test_reminder_send_path(self):
        self.configure()
        self.add_devices("golfer", "phone", "tablet")
        self.add_devices("member", "member-phone")
        watches = self.api.request_watches
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        send = sender.start()
        self.addCleanup(sender.stop)
        late_enough = utc(2026, 10, 16, 23, 20)  # two minutes after the 23:18Z reminder time

        # 1. Stale: within an hour of the tee time, cancelled with no worker call.
        stale = self.reminder_row()
        self.fake_now(utc(2026, 10, 17, 10, 30))
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(stale)["status"], "cancelled")
        self.worker.call.assert_not_called()
        send.assert_not_called()

        # 1b. Daytime stale (no quiet hours involved): a 1:00 PM tee processed at 12:30 PM Eastern.
        daytime = self.reminder_row(display_time="1:00 PM", reservation_no="121212", recipients=["golfer"],
                                    tee_at_utc=self.push.tee_time_utc("2026-10-17", "1:00 PM").isoformat())
        self.fake_now(utc(2026, 10, 17, 16, 30))
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(daytime)["status"], "cancelled")
        self.worker.call.assert_not_called()
        send.assert_not_called()

        # 2. The booker's account is gone: cancelled with no worker call.
        self.fake_now(late_enough)
        orphan = self.reminder_row(booker="ghost", reservation_no="222222")
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(orphan)["status"], "cancelled")
        self.worker.call.assert_not_called()

        # 3. Recent booker activity defers without a worker call or a counted attempt.
        busy = self.reminder_row(reservation_no="333333")
        self.api._note_worker_activity("golfer")
        self.api._send_push_outbox()
        row = self.outbox_row(busy)
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))
        self.assertGreater(row["notify_after"], (datetime.now(UTC) + timedelta(seconds=500)).strftime("%Y-%m-%d %H:%M:%S"))
        self.worker.call.assert_not_called()
        send.assert_not_called()
        self.api._worker_activity.clear()

        # 4. A failed verification retries later, counts one attempt, and still releases the session.
        retry = self.reminder_row(reservation_no="444444")
        self.worker.call.return_value = {"success": False, "error": "site down"}
        self.api._send_push_outbox()
        row = self.outbox_row(retry)
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.assertEqual(len(self.view_calls()), 1)
        self.assertEqual(len(self.release_calls()), 1)
        send.assert_not_called()

        # 5. A reservation that is no longer listed is cancelled and never sent.
        missing = self.reminder_row(reservation_no="555555")
        self.worker.call.reset_mock()
        self.worker.call.return_value = {"success": True, "reservations": [{"reservation_no": "999999"}]}
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(missing)["status"], "cancelled")
        send.assert_not_called()
        self.assertEqual(len(self.release_calls()), 1)

        # 6. A listed reservation sends to every recipient device, then releases the booker's session.
        present = self.reminder_row()
        self.worker.call.reset_mock()
        self.worker.call.return_value = {"success": True, "reservations": [{"reservation_no": "654321"}]}
        self.api._send_push_outbox()
        self.assertEqual(len(self.view_calls()), 1)
        call = self.view_calls()[0]
        self.assertEqual((call.kwargs["tvn_username"], call.kwargs["request_id"]), ("golfer", "reminder"))
        names = [c.args[0] for c in self.worker.call.call_args_list]
        self.assertEqual(names, ["view_my_tee_times", "release_session"])
        self.assertEqual(self.release_calls()[0].kwargs["tvn_username"], "golfer")
        self.assertEqual(send.call_count, 3)
        payloads = {json.dumps(c.args[1], sort_keys=True) for c in send.call_args_list}
        self.assertEqual(len(payloads), 1)
        message = send.call_args.args[1]
        self.assertEqual(message["title"], "Tee time reminder")
        self.assertEqual(message["body"], "Saturday, 10/17/2026 at 7:18 AM: Silver Lake. Reservation 654321.")
        self.assertEqual((message["tag"], message["url"]), ("reminder-654321", "/"))
        self.assertEqual(send.call_args.args[2], int((utc(2026, 10, 17, 11, 18) - late_enough).total_seconds()))
        self.assertEqual(self.outbox_row(present)["status"], "sent")
        self.assertEqual({c.args[0]["endpoint"] for c in send.call_args_list},
                         {subscription(n)["endpoint"] for n in ("phone", "tablet", "member-phone")})

        # 7. Delivery to no device at all still finishes (and is logged); devices that all fail retry.
        send.reset_mock()
        quiet = self.reminder_row(reservation_no="654321", recipients=["other"])
        with self.assertLogs("app", level="INFO") as logs:
            self.api.app.logger.info("anchor")
            self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(quiet)["status"], "sent")
        self.assertTrue(any("push.no_devices kind=push_reminder" in line for line in logs.output))
        send.return_value = "failed"
        failing = self.reminder_row(recipients=["golfer"])
        self.api._send_push_outbox()
        row = self.outbox_row(failing)
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))

        # The booking notice carries the booking's own date and the six-hour lifetime.
        send.reset_mock()
        send.return_value = "sent"
        notice = watches.enqueue_notification("push_booking", "golfer", "", json.loads(
            self.outbox_row(present)["payload"]))
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(notice)["status"], "sent")
        self.assertEqual(send.call_args.args[1]["body"], "Silver Lake, Saturday, 10/17/2026 at 7:18 AM. Reservation 654321.")
        self.assertEqual(send.call_args.args[1]["title"], "Tee time booked")
        self.assertEqual(send.call_args.args[2], 6 * 3600)

    def test_reminder_goes_only_to_golfers_still_on_the_reservation(self):
        self.configure()
        self.add_devices("golfer", "phone")
        self.add_devices("member", "member-phone")
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        send = sender.start()
        self.addCleanup(sender.stop)
        self.fake_now(utc(2026, 10, 16, 23, 20))
        # The member (primary 222) was removed from the reservation: only the booker is told.
        removed = self.reminder_row()
        self.worker.call.return_value = {"success": True, "reservations": [
            {"reservation_no": "654321", "golfer_ids": ["111", "999"]}]}
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(removed)["status"], "sent")
        self.assertEqual({c.args[0]["endpoint"] for c in send.call_args_list}, {subscription("phone")["endpoint"]})
        # Still on it: both are told. An empty or missing list changes nothing.
        for golfer_ids, expected in ((["111", "222"], 2), ([], 2), (None, 2)):
            send.reset_mock()
            row = {"reservation_no": "654321"}
            if golfer_ids is not None:
                row["golfer_ids"] = golfer_ids
            self.reminder_row()
            self.worker.call.return_value = {"success": True, "reservations": [row]}
            self.api._send_push_outbox()
            self.assertEqual(send.call_count, expected, golfer_ids)

    def test_reminder_never_delivers_during_quiet_hours(self):
        self.configure()
        self.add_devices("golfer", "phone")
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        send = sender.start()
        self.addCleanup(sender.stop)
        # Due at 20:59 but processed at 21:02 Eastern (01:02Z): a 1:00 PM tee waits for 7:00 AM.
        afternoon = self.reminder_row(display_time="1:00 PM", play_date="2026-10-17",
                                      tee_at_utc=self.push.tee_time_utc("2026-10-17", "1:00 PM").isoformat(),
                                      recipients=["golfer"])
        self.fake_now(utc(2026, 10, 17, 1, 2))
        self.api._send_push_outbox()
        row = self.outbox_row(afternoon)
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))
        wait = (datetime.strptime(row["notify_after"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                - datetime.now(UTC)).total_seconds()
        expected = (utc(2026, 10, 17, 11, 0) - utc(2026, 10, 17, 1, 2)).total_seconds()  # 7:00 AM EDT = 11:00Z
        self.assertAlmostEqual(wait, expected, delta=30)
        self.worker.call.assert_not_called()
        send.assert_not_called()
        # An 8:00 AM tee: 7:00 AM is not later than one hour before the tee time, so it is dropped.
        morning = self.reminder_row(display_time="8:00 AM", play_date="2026-10-17", reservation_no="888888",
                                    tee_at_utc=self.push.tee_time_utc("2026-10-17", "8:00 AM").isoformat(),
                                    recipients=["golfer"])
        with self.assertLogs("app", level="INFO") as logs:
            self.api.app.logger.info("anchor")
            self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(morning)["status"], "cancelled")
        self.assertTrue(any("push.reminder_quiet_cancel" in line for line in logs.output))
        self.worker.call.assert_not_called()
        send.assert_not_called()
        # Before 7 AM the same rule applies (6:30 AM Eastern, 10:30Z).
        early = self.reminder_row(display_time="1:00 PM", play_date="2026-10-17", reservation_no="777777",
                                  tee_at_utc=self.push.tee_time_utc("2026-10-17", "1:00 PM").isoformat(),
                                  recipients=["golfer"])
        self.fake_now(utc(2026, 10, 17, 10, 30))
        self.api._send_push_outbox()
        row = self.outbox_row(early)
        self.assertEqual(row["status"], "pending")
        wait = (datetime.strptime(row["notify_after"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                - datetime.now(UTC)).total_seconds()
        self.assertAlmostEqual(wait, 30 * 60, delta=30)
        # Exactly 20:59 is outside quiet hours and delivers normally.
        send.reset_mock()
        normal = self.reminder_row(reservation_no="666666", recipients=["golfer"])
        self.fake_now(utc(2026, 10, 17, 0, 59))
        self.worker.call.return_value = {"success": True, "reservations": [{"reservation_no": "666666"}]}
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(normal)["status"], "sent")

    def test_reminder_late_by_an_hour_skips_the_activity_deferral(self):
        self.configure()
        self.add_devices("golfer", "phone")
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        send = sender.start()
        self.addCleanup(sender.stop)
        self.fake_now(utc(2026, 10, 17, 0, 30))  # 72 minutes past the 23:18Z reminder time, 8:30 PM Eastern
        row_id = self.reminder_row(recipients=["golfer"])
        self.api._note_worker_activity("golfer")
        self.worker.call.return_value = {"success": True, "reservations": [{"reservation_no": "654321"}]}
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(row_id)["status"], "sent")
        self.assertEqual(send.call_count, 1)

    def test_push_pass_verifies_at_most_three_reminders(self):
        self.configure()
        self.add_devices("golfer", "phone")
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        sender.start()
        self.addCleanup(sender.stop)
        self.fake_now(utc(2026, 10, 16, 23, 20))
        ids = [self.reminder_row(reservation_no=f"50000{n}", recipients=["golfer"]) for n in range(5)]
        self.worker.call.side_effect = lambda name, **kw: (
            {"success": True, "reservations": [{"reservation_no": f"50000{n}"} for n in range(5)]}
            if name == "view_my_tee_times" else {"success": True})
        self.api._send_push_outbox()
        self.assertEqual(len(self.view_calls()), 3)
        self.assertEqual([self.outbox_row(i)["status"] for i in ids], ["sent"] * 3 + ["pending"] * 2)
        self.assertEqual(self.api._watch_pause.call_count, 2)  # between worker calls, not before the first
        self.api._send_push_outbox()
        self.assertEqual([self.outbox_row(i)["status"] for i in ids], ["sent"] * 5)

    def test_booking_thread_sends_only_its_own_rows(self):
        self.configure()
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        send = sender.start()
        self.addCleanup(sender.stop)
        self.add_devices("golfer", "phone")
        # An overdue reminder, an overdue unrelated email, and an overdue booking notice from earlier.
        reminder = self.reminder_row()
        watches = self.api.request_watches
        old_email = watches.enqueue_notification("booking_confirmation", "golfer", "golfer@example.invalid", {
            "reservation_no": "1", "course": "A", "display_time": "8:00 AM", "date_label": "d", "golfer_names": []})
        play_date = (datetime.now(ET).date() + timedelta(days=5)).isoformat()
        self.worker.call.return_value = self.booking_result(play_date, reservation_no="424242")
        started = []

        class ImmediateThread:
            def __init__(self, target=None, args=(), **kwargs):
                started.append((target, args))
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with patch.dict(os.environ, {"DISABLE_REQUEST_WATCHER": "0"}), \
                patch.object(self.api.threading, "Thread", ImmediateThread), \
                patch.object(self.api, "send_booking_confirmation", return_value=True) as email:
            self.assertTrue(self.book().json["success"])
        self.assertEqual(len(started), 1, "one delivery thread")
        self.assertEqual(started[0][0], self.api._send_outbox_rows)
        email.assert_called_once()
        self.assertEqual(email.call_args.args[0], "golfer@example.invalid")
        self.assertEqual(email.call_args.kwargs["reservation_no"], "424242")
        self.assertEqual([r["status"] for r in self.outbox("booking_confirmation")], ["pending", "sent"])
        self.assertEqual(self.outbox_row(old_email)["status"], "pending")
        new_booking = [r for r in self.outbox("push_booking") if r["ref"] == "424242"][0]
        self.assertEqual(new_booking["status"], "sent")
        self.assertEqual(send.call_args.args[1]["title"], "Tee time booked")
        self.assertEqual(self.outbox_row(reminder)["status"], "pending")
        self.assertEqual(self.outbox_row(reminder)["attempts"], 0)
        self.assertEqual(len(self.view_calls()), 0)
        future_reminder = [r for r in self.outbox("push_reminder") if r["ref"] == "424242"][0]
        self.assertEqual(future_reminder["status"], "pending")
        # The 900-second email loop never touches push rows, and the push loop never touches email.
        self.api._send_outbox_rows([reminder])
        self.assertEqual(self.outbox_row(reminder)["status"], "pending")
        due_push = self.api.request_watches.due_outbox(kinds=self.api._PUSH_KINDS)
        self.assertEqual([r["id"] for r in due_push], [reminder])
        with patch.object(self.api, "send_booking_confirmation", return_value=True):
            self.api._send_pending_outbox()
        self.assertEqual(self.outbox_row(reminder)["status"], "pending")
        self.assertEqual(self.outbox_row(old_email)["status"], "sent")

    def test_reminder_held_overnight_still_respects_the_activity_guard_at_seven(self):
        self.configure()
        self.add_devices("golfer", "phone")
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        send = sender.start()
        self.addCleanup(sender.stop)
        # Processed at 07:00:30 Eastern (11:00:30Z) for a 1:00 PM tee; the booker was active 20 seconds ago.
        self.fake_now(datetime(2026, 10, 17, 11, 0, 30, tzinfo=UTC))
        row_id = self.reminder_row(display_time="1:00 PM", recipients=["golfer"],
                                   tee_at_utc=self.push.tee_time_utc("2026-10-17", "1:00 PM").isoformat())
        self.api._worker_activity["golfer"] = time.time() - 20
        self.api._send_push_outbox()
        row = self.outbox_row(row_id)
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))
        self.worker.call.assert_not_called()
        send.assert_not_called()
        # With no recent activity the same reminder goes out.
        self.api._worker_activity.clear()
        with closing(self.api.request_watches._connect()) as conn, conn:
            conn.execute("UPDATE notification_outbox SET notify_after='2000-01-01 00:00:00' WHERE id=?", (row_id,))
        self.worker.call.return_value = {"success": True, "reservations": [{"reservation_no": "654321"}]}
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(row_id)["status"], "sent")

    def test_release_skipped_when_booker_became_active_during_verification(self):
        self.configure()
        self.add_devices("golfer", "phone")
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        sender.start()
        self.addCleanup(sender.stop)
        self.fake_now(utc(2026, 10, 16, 23, 20))
        calm = self.reminder_row(recipients=["golfer"])
        busy = self.reminder_row(recipients=["golfer"], reservation_no="654322")

        def verify(name, **kwargs):
            if name == "view_my_tee_times" and kwargs["tvn_username"] == "golfer" and state["active"]:
                self.api._note_worker_activity("golfer")  # the golfer opens the app mid-check
            return {"success": True, "reservations": [{"reservation_no": "654321"}, {"reservation_no": "654322"}]}

        state = {"active": False}
        self.worker.call.side_effect = verify
        # First pass handles the calm row only: make the busy one not yet due.
        with closing(self.api.request_watches._connect()) as conn, conn:
            conn.execute("UPDATE notification_outbox SET notify_after='2999-01-01 00:00:00' WHERE id=?", (busy,))
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(calm)["status"], "sent")
        self.assertEqual(len(self.release_calls()), 1)
        self.worker.call.reset_mock()
        state["active"] = True
        with closing(self.api.request_watches._connect()) as conn, conn:
            conn.execute("UPDATE notification_outbox SET notify_after='2000-01-01 00:00:00' WHERE id=?", (busy,))
        self.api._send_push_outbox()
        self.assertEqual(self.outbox_row(busy)["status"], "sent")
        self.assertEqual(len(self.view_calls()), 1)
        self.assertEqual(self.release_calls(), [], "no release for a session the booker just started")

    def test_quiet_hours_are_checked_again_before_sending(self):
        self.configure()
        self.add_devices("golfer", "phone")
        sender = patch.object(self.push, "send_to_subscription", return_value="sent")
        send = sender.start()
        self.addCleanup(sender.stop)
        clock = self.fake_now(datetime(2026, 10, 17, 0, 59, 30, tzinfo=UTC))  # 20:59:30 Eastern
        afternoon = self.reminder_row(display_time="1:00 PM", recipients=["golfer"],
                                      tee_at_utc=self.push.tee_time_utc("2026-10-17", "1:00 PM").isoformat())
        morning = self.reminder_row(reservation_no="654322", recipients=["golfer"])

        def slow_verify(name, **kwargs):
            if name == "view_my_tee_times":
                clock[0] = datetime(2026, 10, 17, 1, 0, 30, tzinfo=UTC)  # the check ran past 21:00
            return {"success": True, "reservations": [{"reservation_no": "654321"}, {"reservation_no": "654322"}]}

        self.worker.call.side_effect = slow_verify
        self.api._send_push_outbox()
        send.assert_not_called()
        self.assertEqual(self.outbox_row(afternoon)["status"], "pending")  # waits for 7 AM
        self.assertEqual(self.outbox_row(afternoon)["attempts"], 0)
        self.assertEqual(self.outbox_row(morning)["status"], "cancelled")  # 7:00 AM leaves no room for a 7:18 AM tee

    def test_registering_again_keeps_stored_devices(self):
        self.configure()
        self.add_devices("golfer", "phone", "tablet")
        before = self.api.user_store.get_user("golfer")["push_subscriptions"]
        self.worker.call.return_value = {"success": True, "primary": {"id": "111", "name": "Test Golfer"},
                                         "buddies": [{"id": "111", "name": "Test Golfer"}]}
        response = self.client_for().post("/api/register", json={"username": "golfer", "password": "fixture", "golf_pin": "1234"})
        self.assertTrue(response.json["ok"], response.json)
        after = self.api.user_store.get_user("golfer")
        self.assertEqual(after["push_subscriptions"], before)
        self.assertEqual(len(after["push_subscriptions"]), 2)
        # A first registration has no devices and gets no empty key.
        self.api.user_store.delete_user("fresh")
        response = self.client_for().post("/api/register", json={"username": "fresh", "password": "p", "golf_pin": "1"})
        self.assertTrue(response.json["ok"], response.json)
        self.assertNotIn("push_subscriptions", self.api.user_store.get_user("fresh"))

    def test_release_session_does_not_reset_the_health_timestamp(self):
        import golf_service

        class FakeService:
            def release_session(self, tvn_username):
                return {"success": True}

            def view_my_tee_times(self, **kwargs):
                return {"success": True, "reservations": []}

        patcher = patch.object(golf_service, "GolfService", FakeService)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker = self.api.GolfWorker()
        self.addCleanup(lambda: (worker._job_q.put(None), worker._thread.join(timeout=5)))
        restore = patch.object(self.api, "_worker_last_success_ts", 0.0)
        restore.start()
        self.addCleanup(restore.stop)
        self.assertEqual(worker.call("release_session", request_id="t", action="release_session", tvn_username="u"), {"success": True})
        self.assertEqual(self.api._worker_last_success_ts, 0.0)
        worker.call("view_my_tee_times", request_id="t", action="my_tee_times", tvn_username="u", tvn_password="p", golf_password="g")
        self.assertGreater(self.api._worker_last_success_ts, 0.0)

    def test_app_starts_and_push_is_inert_when_requests_and_urllib3_cannot_import(self):
        import subprocess

        script = r"""
import sys
for name in ("requests", "urllib3", "pywebpush"):
    sys.modules[name] = None  # any import of these raises ImportError
import os
os.environ.update({"SECRET_KEY": "x", "USER_DB_ENCRYPTION_KEY": %r, "DISABLE_REQUEST_WATCHER": "1",
                   "VAPID_PRIVATE_KEY": %r, "VAPID_PUBLIC_KEY": %r})
from unittest.mock import patch
with patch("dotenv.load_dotenv"):
    import app
import push_notifications as push
assert push.is_configured()
sub = {"endpoint": "https://fcm.googleapis.com/send/x", "keys": {"p256dh": "AAAA", "auth": "BBBB"}}
assert push.validate_endpoint(sub["endpoint"]) is False
assert push.validate_subscription(sub) is None
assert push.send_to_subscription(sub, {"title": "t"}, 60) == "failed"
assert push.send_to_users(["nobody"], {"title": "t"}, 60)["devices"] == 0
print("inert")
""" % (Fernet.generate_key().decode(), PRIVATE, PUBLIC)
        result = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); exec(%r)" % (str(ROOT), script)],
                                capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip().splitlines()[-1], "inert")
        self.assertIn("requests==2.32.5", (ROOT / "requirements.txt").read_text())

    def test_worker_routes_record_booker_activity(self):
        client = self.client_for("golfer")
        self.worker.call.return_value = {"success": True, "reservations": []}
        self.assertFalse(self.api._recent_worker_activity("golfer"))
        client.get("/api/my-tee-times")
        self.assertTrue(self.api._recent_worker_activity("golfer"))
        self.api._worker_activity.clear()
        client.get("/api/session")
        self.assertFalse(self.api._recent_worker_activity("golfer"))
        self.client_for().get("/api/my-tee-times")
        self.assertEqual(self.api._worker_activity, {})

    # 10 ----------------------------------------------------------------------
    def test_gone_subscriptions_are_removed(self):
        from pywebpush import WebPushException

        self.configure()
        self.add_devices("golfer", "expired", "flaky", "healthy")

        def fake_webpush(**kwargs):
            endpoint = kwargs["subscription_info"]["endpoint"]
            if endpoint.endswith("expired"):
                raise WebPushException("gone", response=Mock(status_code=410))
            if endpoint.endswith("flaky"):
                raise WebPushException("broken", response=Mock(status_code=500))
            return Mock(status_code=201)

        with patch("pywebpush.webpush", side_effect=fake_webpush) as webpush:
            totals = self.push.send_to_users(["golfer", "golfer", "ghost"], {"title": "t", "body": "b", "tag": "x", "url": "/"}, 120)
        self.assertEqual(totals, {"devices": 3, "sent": 1, "failed": 1, "gone": 1})
        self.assertEqual(self.stored("golfer"), [subscription("flaky")["endpoint"], subscription("healthy")["endpoint"]])
        call = webpush.call_args_list[0].kwargs
        self.assertEqual((call["ttl"], call["timeout"], call["vapid_claims"]), (120, 10, {"sub": self.push._SUBJECT}))
        self.assertEqual(json.loads(call["data"])["tag"], "x")
        self.assertEqual(call["vapid_private_key"], PRIVATE)
        with patch("pywebpush.webpush", side_effect=WebPushException("nf", response=Mock(status_code=404))):
            self.assertEqual(self.push.send_to_subscription(subscription("x"), {}, 60), "gone")
        with patch("pywebpush.webpush", side_effect=WebPushException("no response")):
            self.assertEqual(self.push.send_to_subscription(subscription("x"), {}, 60), "failed")
        with patch("pywebpush.webpush", side_effect=OSError("network")):
            self.assertEqual(self.push.send_to_subscription(subscription("x"), {}, 60), "failed")
        with patch("pywebpush.webpush", return_value=Mock()):
            self.assertEqual(self.push.send_to_subscription(subscription("x"), {}, 60), "sent")

    def test_payloads_stay_small_and_use_the_exact_copy(self):
        long = {"reservation_no": "1", "course": "C" * 6000, "display_time": "7:18 AM", "date_label": "d", "play_date": "2026-10-17"}
        for payload in (self.push.booking_payload(long), self.push.reminder_payload(long, utc(2026, 10, 14, 12))):
            self.assertLess(len(json.dumps(payload).encode()), 3072)
        test = self.push.device_test_payload()
        self.assertEqual((test["title"], test["body"], test["tag"]), ("Village Fairways", "Notifications are working on this device.", "test"))
        sample = {"reservation_no": "1", "course": "A", "display_time": "7:18 AM", "play_date": "2026-10-17"}
        for now in (utc(2026, 10, 14, 12), utc(2026, 10, 17, 12), utc(2026, 10, 30, 12)):
            self.assertEqual(self.push.reminder_payload(sample, now)["body"], "Saturday, 10/17/2026 at 7:18 AM: A. Reservation 1.")
        self.assertEqual(self.push.reminder_payload(dict(sample, date_label="Sat, Oct 17"))["body"],
                         "Sat, Oct 17 at 7:18 AM: A. Reservation 1.")
        self.assertFalse(hasattr(self.push, "when_label"))

    # 11 ----------------------------------------------------------------------
    def test_booking_confirmation_email_path_unchanged(self):
        self.configure()
        watches = self.api.request_watches
        payload = {"reservation_no": "1", "course": "Silver Lake", "display_time": "8:00 AM",
                   "date_label": "Saturday", "golfer_names": ["Test Golfer"]}
        row_id = watches.enqueue_notification("booking_confirmation", "golfer", "golfer@example.invalid", payload)
        with patch.object(self.api, "send_booking_confirmation", return_value=True) as email, \
                patch.object(self.push, "send_to_users") as push_send:
            self.api._send_pending_outbox()
        email.assert_called_once_with("golfer@example.invalid", **payload)
        push_send.assert_not_called()
        self.assertEqual(self.outbox_row(row_id)["status"], "sent")
        unknown = watches.enqueue_notification("mystery_kind", "golfer", "golfer@example.invalid", {"x": 1})
        self.api._send_pending_outbox()
        row = self.outbox_row(unknown)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 1))
        self.assertIsNone(self.outbox_row(row_id)["ref"])

    def test_outbox_migration_adds_ref_to_an_existing_table(self):
        import sqlite3

        watches = self.api.request_watches
        path = self.tmp.name + "/legacy.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE notification_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                         "username TEXT NOT NULL, email TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL "
                         "DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, notify_after TEXT NOT NULL, created_at TEXT)")
            conn.execute("INSERT INTO notification_outbox (kind, username, email, payload, notify_after) "
                         "VALUES ('booking_confirmation','u','e@example.invalid','{}','2000-01-01 00:00:00')")
        watches._DB_PATH = path
        watches._initialized = False
        new_id = watches.enqueue_notification("push_reminder", "u", "", {}, notify_after=utc(2030, 1, 1), ref="9")
        self.assertEqual(self.outbox_row(new_id)["notify_after"], "2030-01-01 00:00:00")
        self.assertEqual(watches.cancel_outbox_ref("push_reminder", "9"), 1)
        self.assertEqual(watches.cancel_outbox_ref("push_reminder", "9"), 0)
        self.assertEqual(len(watches.due_outbox()), 1)  # the legacy row survived

    # 12 ----------------------------------------------------------------------
    def test_test_notification_rate_limit(self):
        self.configure()
        self.add_devices("golfer", "phone")
        client = self.client_for("golfer")
        with patch.object(self.push, "send_to_subscription", return_value="sent") as send:
            for _ in range(5):
                response = client.post("/api/push/test", json={})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json, {"success": True, "sent": 1, "failed": 0})
            limited = client.post("/api/push/test", json={})
            self.assertEqual(limited.status_code, 429)
            self.assertFalse(limited.json["success"])
            self.assertTrue(limited.json["error"])
            self.assertEqual(send.call_count, 5)
            self.assertEqual(send.call_args.args[1]["title"], "Village Fairways")
            self.assertEqual(self.client_for("member").post("/api/push/test", json={}).status_code, 200)
        self.api._push_test_times.clear()
        nobody = self.client_for("other").post("/api/push/test", json={})
        self.assertEqual(nobody.status_code, 200)
        self.assertFalse(nobody.json["success"])
        self.assertTrue(nobody.json["error"])
        self.assertEqual((nobody.json["sent"], nobody.json["failed"]), (0, 0))
        with patch.object(self.push, "send_to_subscription", return_value="failed"):
            failed = client.post("/api/push/test", json={}).json
        self.assertEqual((failed["success"], failed["sent"], failed["failed"]), (False, 0, 1))

    # 13 ----------------------------------------------------------------------
    def test_service_worker_shows_every_push_and_opens_the_app(self):
        response = self.client_for().get("/sw.js")
        self.addCleanup(response.close)
        script = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const BODY = %s;
const listeners = {};
const shown = [];
const state = {clients: [], opened: []};
const self_ = {
  location: {origin: 'https://villagefairways.com'},
  addEventListener(type, handler) { listeners[type] = handler; },
  skipWaiting() { return Promise.resolve(); },
  clients: {claim() { return Promise.resolve(); },
            matchAll() { return Promise.resolve(state.clients); },
            openWindow(url) { state.opened.push(url); return Promise.resolve({}); }},
  registration: {showNotification(title, options) { shown.push({title, options}); return Promise.resolve(); }},
};
const context = {self: self_, URL, Promise, console,
  Request: class { constructor(url, options) { this.url = url; } },
  caches: {open() { return Promise.resolve({add() { return Promise.resolve(); }}); }, keys() { return Promise.resolve([]); },
           delete() { return Promise.resolve(true); }, match(url) { return Promise.resolve({tag: 'offline', url}); }},
  fetch: () => Promise.resolve({tag: 'network'})};
vm.createContext(context);
vm.runInContext(BODY, context);

async function push(data) {
  let waited = null;
  listeners.push({data, waitUntil(promise) { waited = promise; }});
  assert.ok(waited, 'push must call waitUntil');
  await waited;
}
async function click(notification) {
  let waited = null; let closed = 0;
  listeners.notificationclick({notification: Object.assign({close() { closed += 1; }}, notification), waitUntil(promise) { waited = promise; }});
  assert.ok(waited, 'click must call waitUntil');
  await waited;
  return closed;
}

(async () => {
  await push({json: () => ({title: 'Tee time booked', body: 'Silver Lake, Saturday at 7:18 AM. Reservation 1.', tag: 'booking-1', url: '/'})});
  assert.deepEqual(JSON.parse(JSON.stringify(shown[0])), {title: 'Tee time booked', options: {
    body: 'Silver Lake, Saturday at 7:18 AM. Reservation 1.', icon: '/static/icons/icon-192.png', data: {url: '/'}, tag: 'booking-1'}});
  await push({json: () => { throw new SyntaxError('bad'); }});
  await push(null);
  await push({json: () => 'a string'});
  await push({json: () => ({title: 5, body: {}, tag: 7, url: 9})});
  assert.equal(shown.length, 5, 'every push shows a notification');
  for (const item of shown.slice(1)) {
    assert.equal(item.title, 'Village Fairways');
    assert.equal(item.options.body, '');
    assert.equal(item.options.icon, '/static/icons/icon-192.png');
  }

  let focused = 0;
  state.clients = [{url: 'https://gis.thevillages.net/', focus() { focused += 100; return Promise.resolve(); }},
                   {url: 'https://villagefairways.com/help', focus() { focused += 1; return Promise.resolve(); }}];
  assert.equal(await click({data: {url: '/'}}), 1);
  assert.equal(focused, 1, 'focuses the open window of this origin');
  assert.deepEqual(state.opened, []);
  state.clients = [{url: 'https://gis.thevillages.net/', focus() { focused += 100; return Promise.resolve(); }}];
  await click({data: {url: '/'}});
  assert.deepEqual(state.opened, ['/'], 'a foreign window is not reused');
  state.clients = [];
  await click({data: null});
  await click({data: {url: 'https://evil.example/phish'}});
  assert.deepEqual(state.opened, ['/', '/', '/']);

  // Existing behavior: a page load still goes to the network and falls back to the offline page.
  let responded = null;
  listeners.fetch({request: {method: 'GET', mode: 'navigate', url: 'https://villagefairways.com/'}, respondWith(p) { responded = p; }});
  assert.deepEqual(await responded, {tag: 'network'});
  context.fetch = () => Promise.reject(new TypeError('offline'));
  listeners.fetch({request: {method: 'GET', mode: 'navigate', url: 'https://villagefairways.com/'}, respondWith(p) { responded = p; }});
  assert.deepEqual(await responded, {tag: 'offline', url: '/offline'});
  responded = null;
  listeners.fetch({request: {method: 'GET', mode: 'cors', url: 'https://villagefairways.com/api/session'}, respondWith(p) { responded = p; }});
  assert.equal(responded, null);
  console.log('ok');
})().catch((error) => { console.error(error); process.exit(1); });
""" % json.dumps(response.get_data(as_text=True))
        self.assertEqual(run_node(script).strip(), "ok")
        with patch.dict(os.environ, {"DISABLE_SERVICE_WORKER": "1"}):
            kill = self.client_for().get("/sw.js")
            self.addCleanup(kill.close)
        self.assertNotIn("showNotification", kill.get_data(as_text=True))

    # 14 ----------------------------------------------------------------------
    def test_notify_state_table(self):
        script = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const window = {navigator: {}, addEventListener() {}, fetch: () => Promise.reject(new Error('no'))};
const document = {getElementById: () => null, addEventListener() {}};
vm.runInContext(%s, vm.createContext({window, document}));
const state = window.VFNotify.notifyState;
const base = {configured: true, supported: true, platform: 'android', permission: 'default', subscribed: false};
const table = [
  [{configured: false}, 'hidden'],
  [{}, 'hidden'],
  [undefined, 'hidden'],
  [Object.assign({}, base, {configured: false, platform: 'ios'}), 'hidden'],
  [Object.assign({}, base, {supported: false}), 'unsupported'],
  [Object.assign({}, base, {supported: false, platform: 'desktop'}), 'unsupported'],
  [Object.assign({}, base, {platform: 'ios'}), 'install-first'],
  [Object.assign({}, base, {platform: 'ios', supported: false}), 'install-first'],
  [Object.assign({}, base, {platform: 'ios', permission: 'denied'}), 'install-first'],
  [Object.assign({}, base, {platform: 'standalone', supported: true}), 'off'],
  [Object.assign({}, base, {permission: 'denied'}), 'blocked'],
  [Object.assign({}, base, {permission: 'denied', subscribed: true}), 'blocked'],
  [Object.assign({}, base, {permission: 'granted', subscribed: true}), 'on'],
  [Object.assign({}, base, {permission: 'granted', subscribed: false}), 'off'],
  [Object.assign({}, base, {permission: 'default', subscribed: true}), 'off'],
  [base, 'off'],
];
for (const [input, expected] of table) assert.equal(state(input), expected, JSON.stringify(input));
console.log('ok');
""" % json.dumps(NOTIFY_JS)
        self.assertEqual(run_node(script).strip(), "ok")

    # 15 ----------------------------------------------------------------------
    NODE_ENV = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const SOURCE = %s;

function makeEnv(options) {
  const registry = new Map();
  function node(tag) {
    return {tagName: tag, className: '', children: [], attrs: {}, listeners: {}, _t: '', type: '',
      setAttribute(n, v) { this.attrs[n] = String(v); }, removeAttribute(n) { delete this.attrs[n]; },
      getAttribute(n) { return this.attrs[n]; },
      appendChild(c) { this.children.push(c); return c; }, addEventListener(t, h) { this.listeners[t] = h; },
      get textContent() { return this._t + this.children.map((c) => c.textContent).join(''); },
      set textContent(v) { this._t = String(v); this.children = []; },
      get hidden() { return 'hidden' in this.attrs; }};
  }
  const section = node('section'); section.attrs.hidden = '';
  const status = node('p'); const actions = node('div');
  registry.set('notify-section', section); registry.set('notify-status', status); registry.set('notify-actions', actions);
  const docHandlers = {};
  const document = {visibilityState: 'visible',
    getElementById: (id) => registry.get(id) || null, createElement: node,
    createTextNode: (t) => { const n = node('#text'); n._t = t; return n; },
    addEventListener(type, handler) { docHandlers[type] = handler; }};
  const window = Object.assign({navigator: {userAgent: 'test'}, setTimeout, atob: (s) => Buffer.from(s, 'base64').toString('binary')},
    options.window || {});
  window.document = document;
  const context = {window, document, console, setTimeout, Uint8Array, Promise};
  vm.createContext(context);
  return {window, document, section, status, actions, docHandlers, context};
}
function buttons(env) { return env.actions.children.filter((c) => c.tagName === 'button'); }
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));
"""

    def test_notify_script_survives_missing_apis(self):
        script = (self.NODE_ENV % json.dumps(NOTIFY_JS)) + r"""
(async () => {
  const throwing = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }};
  const enabled = () => Promise.resolve({ok: true, json: () => Promise.resolve({enabled: true, public_key: 'QUJD'})});
  // No Notification, no PushManager, no service worker, storage that throws: renders unsupported.
  {
    const env = makeEnv({window: {localStorage: throwing, fetch: enabled}});
    vm.runInContext(SOURCE, env.context);
    await settle();
    assert.equal(env.section.hidden, false);
    assert.equal(env.status.textContent, "This browser can't show notifications.");
    assert.equal(buttons(env).length, 0);
    const started = Date.now();
    assert.equal(await env.window.VFNotify.currentEndpoint(), null);
    assert.ok(Date.now() - started < 2500);
    assert.equal(await env.window.VFNotify.syncSubscription(), false);
    env.docHandlers.visibilitychange();
  }
  // Server says notifications are off, or the config call fails: the section stays hidden.
  for (const fetchImpl of [() => Promise.resolve({ok: true, json: () => Promise.resolve({enabled: false, public_key: null})}),
                           () => Promise.reject(new Error('offline')), () => Promise.resolve({ok: false}), undefined]) {
    const env = makeEnv({window: {fetch: fetchImpl}});
    vm.runInContext(SOURCE, env.context);
    await settle();
    assert.equal(env.section.hidden, true);
  }
  // A service worker that never becomes ready, and a registration lookup that never settles: still bounded.
  {
    const never = new Promise(() => {});
    const env = makeEnv({window: {fetch: enabled, Notification: {permission: 'granted'}, PushManager: function () {},
      navigator: {userAgent: 'x', serviceWorker: {ready: never, getRegistration: () => never}}}});
    vm.runInContext(SOURCE, env.context);
    const started = Date.now();
    assert.equal(await env.window.VFNotify.currentEndpoint(), null);
    const waited = Date.now() - started;
    assert.ok(waited >= 1500 && waited < 3000, 'bounded near 2 seconds: ' + waited);
  }
  // No document nodes at all, no window.navigator: loading never throws.
  {
    const window = {};
    const document = {getElementById: () => null};
    vm.runInContext(SOURCE, vm.createContext({window, document}));
    assert.equal(await window.VFNotify.currentEndpoint(), null);
  }
  // A throwing getter on navigator does not escape.
  {
    const window = {fetch: enabled};
    Object.defineProperty(window, 'navigator', {get() { throw new Error('nope'); }});
    const document = {getElementById: () => null};
    vm.runInContext(SOURCE, vm.createContext({window, document}));
    assert.equal(await window.VFNotify.currentEndpoint(), null);
  }
  console.log('ok');
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
        self.assertEqual(run_node(script).strip(), "ok")

    def test_notify_turn_on_flow_and_sync(self):
        script = (self.NODE_ENV % json.dumps(NOTIFY_JS)) + r"""
function pushWorld(initialPermission, subscribed, respond) {
  const calls = [];
  const N = {permission: initialPermission, async requestPermission() { N.permission = 'granted'; return 'granted'; }};
  const manager = {subscription: null, subscribeOptions: null,
    async getSubscription() { return manager.subscription; },
    async subscribe(options) {
      manager.subscribeOptions = options;
      manager.subscription = {endpoint: 'https://fcm.googleapis.com/send/abc',
        toJSON() { return {endpoint: this.endpoint, keys: {p256dh: 'AAAA', auth: 'BBBB'}}; },
        async unsubscribe() { manager.subscription = null; return true; }};
      return manager.subscription;
    }};
  if (subscribed) manager.subscribe({});
  const registration = {pushManager: manager};
  const server = {endpoints: new Set()};
  const fetch = async (path, init) => {
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({path, body, method: init && init.method});
    if (path === '/api/push/config') {
      const payload = {enabled: true, public_key: 'QUJD'};
      const named = init && init.headers && init.headers['X-Push-Endpoint'];
      if (named) payload.device_registered = server.endpoints.has(named);
      return {ok: true, json: async () => payload};
    }
    const reply = respond(path);
    if (reply.ok && path === '/api/push/subscribe') server.endpoints.add(body.subscription.endpoint);
    if (reply.ok && path === '/api/push/unsubscribe') server.endpoints.delete(body.endpoint);
    return reply;
  };
  const storage = {data: {}, getItem(k) { return k in this.data ? this.data[k] : null; }, setItem(k, v) { this.data[k] = String(v); }};
  return {calls, N, manager, registration, fetch, storage, server,
    window: {fetch, Notification: N, PushManager: function () {}, localStorage: storage,
      navigator: {userAgent: 'Android', serviceWorker: {ready: Promise.resolve(registration), getRegistration: async () => registration}}}};
}
const ok = (json) => ({ok: true, status: 200, json: async () => json});
const posts = (world, path) => world.calls.filter((c) => c.path === path);

(async () => {
  // Off -> turn on -> test -> turn off.
  {
    const world = pushWorld('default', false, () => ok({success: true}));
    const env = makeEnv({window: world.window});
    vm.runInContext(SOURCE, env.context);
    await settle();
    assert.equal(env.status.textContent, 'Get a notification when a tee time is booked, and a reminder about 12 hours before it.');
    assert.deepEqual(buttons(env).map((b) => b.textContent), ['Turn on notifications']);
    assert.equal(posts(world, '/api/push/subscribe').length, 0, 'no sync without permission');
    await buttons(env)[0].listeners.click();
    assert.equal(world.manager.subscribeOptions.userVisibleOnly, true);
    assert.deepEqual(Array.from(world.manager.subscribeOptions.applicationServerKey), [65, 66, 67]);
    assert.equal(posts(world, '/api/push/subscribe').length, 1);
    assert.equal(posts(world, '/api/push/subscribe')[0].body.subscription.endpoint, 'https://fcm.googleapis.com/send/abc');
    assert.equal(env.status.textContent, 'Notifications are on for this device.');
    assert.deepEqual(buttons(env).map((b) => b.textContent), ['Send a test notification', 'Turn off notifications']);
    await buttons(env)[0].listeners.click();
    assert.equal(posts(world, '/api/push/test').length, 1);
    assert.match(env.actions.textContent, /Test sent\./);
    assert.equal(await env.window.VFNotify.currentEndpoint(), 'https://fcm.googleapis.com/send/abc');
    await buttons(env)[1].listeners.click();
    assert.equal(world.manager.subscription, null);
    assert.deepEqual(posts(world, '/api/push/unsubscribe')[0].body, {endpoint: 'https://fcm.googleapis.com/send/abc'});
    assert.equal(env.status.textContent, 'Get a notification when a tee time is booked, and a reminder about 12 hours before it.');
  }
  // A server that rejects the subscription leaves the browser unsubscribed and shows the error.
  {
    const world = pushWorld('default', false, () => ({ok: false, status: 400, json: async () => ({error: 'That notification subscription is not valid.'})}));
    const env = makeEnv({window: world.window});
    vm.runInContext(SOURCE, env.context);
    await settle();
    await buttons(env)[0].listeners.click();
    assert.equal(world.manager.subscription, null);
    assert.match(env.actions.textContent, /not valid/);
    assert.equal(buttons(env)[0].textContent, 'Turn on notifications');
  }
  // Blocked and iOS-in-a-tab states.
  {
    const world = pushWorld('denied', false, () => ok({}));
    const env = makeEnv({window: world.window});
    vm.runInContext(SOURCE, env.context);
    await settle();
    assert.equal(env.status.textContent, "Notifications are blocked for Village Fairways. Turn them on in your phone's Settings.");
    assert.equal(buttons(env).length, 0);
  }
  {
    const world = pushWorld('default', false, () => ok({}));
    world.window.VFInstall = {detectInstallPlatform: () => 'ios',
      appendWithLabels(parent, text) { parent.appendChild({tagName: '#text', _t: text, children: [], get textContent() { return this._t; }}); }};
    const env = makeEnv({window: world.window});
    vm.runInContext(SOURCE, env.context);
    await settle();
    assert.equal(env.status.textContent, 'Add Village Fairways to your Home Screen, then open it from the Home Screen icon to turn on notifications.');
    const link = env.actions.children.find((c) => c.tagName === 'a');
    assert.equal(link.textContent, 'Home Screen install guide');
    assert.equal(link.getAttribute('href'), '/install');
  }
  // Sync: posts at load, then at most once per ten minutes; a 401 does not start the clock.
  {
    const world = pushWorld('granted', true, () => ok({success: true}));
    const env = makeEnv({window: world.window});
    vm.runInContext(SOURCE, env.context);
    await settle();
    assert.equal(posts(world, '/api/push/subscribe').length, 1);
    assert.equal(env.status.textContent, 'Notifications are on for this device.', 'on once the server confirms the device');
    assert.equal(await env.window.VFNotify.syncSubscription(), false);
    env.docHandlers.visibilitychange();
    env.window.openAccountDialog && env.window.openAccountDialog();
    await settle();
    assert.equal(posts(world, '/api/push/subscribe').length, 1, 'throttled');
    world.storage.data.vf_push_synced_at = String(Date.now() - 700000);
    env.document.visibilityState = 'visible';
    env.docHandlers.visibilitychange();
    await settle();
    assert.equal(posts(world, '/api/push/subscribe').length, 1, 'the in-memory clock also limits this page view');
  }
  {
    const world = pushWorld('granted', true, () => ({ok: false, status: 401, json: async () => ({error: 'Not authenticated'})}));
    const env = makeEnv({window: world.window});
    vm.runInContext(SOURCE, env.context);
    await settle();
    await env.window.VFNotify.syncSubscription();
    assert.equal(posts(world, '/api/push/subscribe').length, 3, 'load sync, the render\'s one forced retry, then this call: a 401 shows nothing');
    assert.doesNotMatch(env.actions.textContent, /Not authenticated/);
    assert.equal(env.status.textContent, 'Get a notification when a tee time is booked, and a reminder about 12 hours before it.',
      'a browser subscription the server does not hold is shown as off');
    assert.deepEqual(buttons(env).map((b) => b.textContent), ['Turn on notifications']);
  }
  // Opening the Account dialog re-renders and syncs.
  {
    const world = pushWorld('granted', true, () => ok({success: true}));
    let opened = 0;
    world.window.openAccountDialog = function () { opened += 1; return 'opened'; };
    const env = makeEnv({window: world.window});
    vm.runInContext(SOURCE, env.context);
    await settle();
    assert.equal(env.window.openAccountDialog(), 'opened');
    assert.equal(opened, 1);
  }
  console.log('ok');
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
        self.assertEqual(run_node(script).strip(), "ok")

    def test_sign_in_resyncs_device(self):
        script = (self.NODE_ENV % json.dumps(NOTIFY_JS)) + r"""
const ok = (json) => ({ok: true, status: 200, json: async () => json});
(async () => {
  const calls = [];
  const server = new Set();
  const N = {permission: 'granted'};
  const subscription = {endpoint: 'https://fcm.googleapis.com/send/abc',
    toJSON() { return {endpoint: this.endpoint, keys: {p256dh: 'AAAA', auth: 'BBBB'}}; }, async unsubscribe() { return true; }};
  const registration = {pushManager: {getSubscription: async () => subscription}};
  const storage = {data: {}, getItem(k) { return k in this.data ? this.data[k] : null; }, setItem(k, v) { this.data[k] = String(v); },
                   removeItem(k) { delete this.data[k]; }};
  let signedIn = 'first';
  const fetch = async (path, init) => {
    calls.push({path, user: signedIn, body: init && init.body ? JSON.parse(init.body) : null});
    if (path === '/api/push/config') {
      const named = init && init.headers && init.headers['X-Push-Endpoint'];
      const payload = {enabled: true, public_key: 'QUJD'};
      if (named) payload.device_registered = server.has(signedIn + ':' + named);
      return ok(payload);
    }
    if (path === '/api/push/subscribe') { server.add(signedIn + ':' + calls[calls.length - 1].body.subscription.endpoint); return ok({success: true}); }
    return ok({});
  };
  const env = makeEnv({window: {fetch, Notification: N, PushManager: function () {}, localStorage: storage,
    navigator: {userAgent: 'Android', serviceWorker: {ready: Promise.resolve(registration), getRegistration: async () => registration}}}});
  vm.runInContext(SOURCE, env.context);
  await settle();
  const subs = () => calls.filter((c) => c.path === '/api/push/subscribe');
  assert.equal(subs().length, 1, 'the first account syncs at load');
  assert.ok(storage.data.vf_push_synced_at, 'a sync stamp is stored');
  // Sign out: the stamp is cleared. A different golfer signs in a moment later: a forced sync runs at once.
  env.window.VFNotify.clearSyncStamp();
  assert.equal(storage.data.vf_push_synced_at, undefined);
  signedIn = 'second';
  assert.equal(await env.window.VFNotify.syncSubscription({force: true}), true);
  assert.equal(subs().length, 2);
  assert.equal(subs()[1].user, 'second');
  // Even with a fresh stamp, force goes through; an ordinary call is throttled.
  assert.ok(storage.data.vf_push_synced_at);
  signedIn = 'third';
  assert.equal(await env.window.VFNotify.syncSubscription({force: true}), true);
  assert.equal(subs().length, 3);
  assert.equal(subs()[2].user, 'third');
  assert.equal(await env.window.VFNotify.syncSubscription(), false);
  assert.equal(subs().length, 3);
  // The Account section shows on only for the account that holds the device.
  await env.window.VFNotify.render();
  assert.equal(env.status.textContent, 'Notifications are on for this device.');
  signedIn = 'fourth';
  server.clear();
  await env.window.VFNotify.render();
  await settle();
  assert.equal(env.status.textContent, 'Notifications are on for this device.', 'a render resyncs a device the server lost');
  assert.equal(subs()[subs().length - 1].user, 'fourth');
  console.log('ok');
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
        self.assertEqual(run_node(script).strip(), "ok")
        for name in ("loginUser", "registerUser"):
            start = INDEX.index(f"async function {name}(")
            body = INDEX[start:INDEX.index("\n}\n", start)]
            self.assertIn("enterApp(d);\n      syncPushAfterSignIn();", body, name)
        helper = INDEX[INDEX.index("function syncPushAfterSignIn"):INDEX.index("async function loginUser")]
        self.assertIn("syncSubscription?.({force:true})", helper)
        self.assertIn("catch(e){}", helper)
        switch = INDEX[INDEX.index("async function switchUser"):INDEX.index("function resetMyTeeTimes")]
        self.assertIn("clearSyncStamp", switch)

    def test_push_config_reports_whether_this_device_is_registered(self):
        self.configure()
        mine = subscription("mine")
        client = self.client_for("golfer")
        self.assertEqual(self.subscribe(client, mine).status_code, 200)
        other = self.client_for("member")
        self.subscribe(other, subscription("theirs"))
        self.assertTrue(client.get("/api/push/config", headers={"X-Push-Endpoint": mine["endpoint"]}).json["device_registered"])
        self.assertFalse(client.get("/api/push/config", headers={"X-Push-Endpoint": subscription("theirs")["endpoint"]}).json["device_registered"])
        self.assertFalse(client.get("/api/push/config", headers={"X-Push-Endpoint": FCM + "unknown"}).json["device_registered"])
        self.assertTrue(client.get("/api/push/config", query_string={"endpoint": mine["endpoint"]}).json["device_registered"])
        self.assertNotIn("device_registered", client.get("/api/push/config").json)
        self.assertNotIn("device_registered", self.client_for().get(
            "/api/push/config", headers={"X-Push-Endpoint": mine["endpoint"]}).json)
        self.assertEqual(self.client_for().get("/api/push/config").json, {"enabled": True, "public_key": PUBLIC})

    def test_subscribe_is_rate_limited_and_skips_the_sweep_for_known_endpoints(self):
        self.configure()
        client = self.client_for("golfer")
        mine = subscription("mine")
        self.assertEqual(self.subscribe(client, mine).status_code, 200)
        with patch.object(self.push, "remove_endpoint_from_others") as sweep:
            for _ in range(5):
                self.assertEqual(self.subscribe(client, mine).status_code, 200)
            sweep.assert_not_called()
            self.assertEqual(self.subscribe(client, subscription("fresh")).status_code, 200)
            sweep.assert_called_once()
        self.assertEqual(len(self.stored("golfer")), 2)
        for _ in range(30 - 7):
            self.assertEqual(self.subscribe(client, mine).status_code, 200)
        limited = self.subscribe(client, mine)
        self.assertEqual(limited.status_code, 429)
        self.assertFalse(limited.json["success"])
        self.assertEqual(self.subscribe(self.client_for("member"), mine).status_code, 200)  # per user
        self.assertEqual(self.stored("golfer"), [subscription("fresh")["endpoint"]])

    def test_release_session_closes_only_that_users_session(self):
        import golf_service

        service = golf_service.GolfService.__new__(golf_service.GolfService)
        service._sessions = collections.OrderedDict()
        service._active, service._active_user = None, None
        closed = []
        for name in ("alice", "bob"):
            sess = golf_service._Session()
            sess.context = Mock(close=lambda name=name: closed.append(name))
            service._sessions[name] = sess
        self.assertEqual(service.release_session("alice"), {"success": True})
        self.assertEqual(closed, ["alice"])
        self.assertEqual(list(service._sessions), ["bob"])
        self.assertEqual(service.release_session("alice"), {"success": True})  # absent is fine
        self.assertEqual(service.release_session("nobody"), {"success": True})
        self.assertEqual(closed, ["alice"])
        self.assertEqual(list(service._sessions), ["bob"])

    # 16 ----------------------------------------------------------------------
    def test_switch_user_sends_push_endpoint(self):
        start = INDEX.index("async function switchUser(")
        depth, end = 0, None
        for index in range(INDEX.index("{", start), len(INDEX)):
            depth += {"{": 1, "}": -1}.get(INDEX[index], 0)
            if depth == 0:
                end = index + 1
                break
        function = INDEX[start:end]
        script = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const SOURCE = %s;
async function run(window, fetchImpl) {
  const log = {fetches: [], reset: 0, myTimes: 0, login: 0};
  const context = {window, fetch: (path, init) => { log.fetches.push({path, init}); return fetchImpl(path, init); },
    resetApp() { log.reset += 1; }, resetMyTeeTimes() { log.myTimes += 1; }, showLogin() { log.login += 1; }};
  vm.createContext(context);
  vm.runInContext(SOURCE + '; this.switchUser = switchUser;', context);
  await context.switchUser();
  return log;
}
(async () => {
  let log = await run({VFNotify: {currentEndpoint: async () => 'https://fcm.googleapis.com/send/abc'}}, async () => ({}));
  assert.equal(log.fetches.length, 1);
  assert.equal(log.fetches[0].path, '/api/logout');
  assert.equal(log.fetches[0].init.method, 'POST');
  assert.equal(log.fetches[0].init.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(log.fetches[0].init.body), {push_endpoint: 'https://fcm.googleapis.com/send/abc'});
  assert.deepEqual([log.reset, log.myTimes, log.login], [1, 1, 1]);
  // VFNotify missing, throwing, rejecting, or returning nothing: the user is still logged out.
  const variants = [{}, {VFNotify: {}}, {VFNotify: {currentEndpoint() { throw new Error('boom'); }}},
                    {VFNotify: {currentEndpoint: () => Promise.reject(new Error('boom'))}}, {VFNotify: {currentEndpoint: async () => null}}];
  for (const window of variants) {
    log = await run(window, async () => ({}));
    assert.equal(log.fetches[0].path, '/api/logout');
    assert.equal(JSON.parse(log.fetches[0].init.body).push_endpoint, null);
    assert.deepEqual([log.reset, log.myTimes, log.login], [1, 1, 1]);
  }
  // A failing logout request still resets the page.
  log = await run({}, async () => { throw new Error('offline'); });
  assert.deepEqual([log.reset, log.myTimes, log.login], [1, 1, 1]);
  console.log('ok');
})().catch((error) => { console.error(error); process.exit(1); });
""" % json.dumps(function)
        self.assertEqual(run_node(script).strip(), "ok")
        self.assertIn('<script src="/static/js/notify.js"></script>', INDEX)
        self.assertLess(INDEX.index("/static/js/install.js"), INDEX.index("/static/js/notify.js"))
        section = INDEX[INDEX.index('id="notify-section"'):]
        self.assertLess(section.index('id="account-notify-heading"'), section.index('id="account-help-heading"'))
        for fragment in ('class="account-section" id="notify-section"', 'aria-labelledby="account-notify-heading" hidden>',
                         '<h3 id="account-notify-heading">Phone notifications</h3>', '<p id="notify-status"></p>',
                         '<div class="notify-actions" id="notify-actions"></div>'):
            self.assertIn(fragment, INDEX)

    # 17 ----------------------------------------------------------------------
    def test_notification_copy_follows_house_style(self):
        def check(label, text):
            text = text.replace("Not now", "")
            self.assertTrue(text.strip(), label)
            self.assertNotIn(chr(0x2014), text, label)
            match = BANNED.search(text)
            self.assertIsNone(match, f"{label}: {match.group(0) if match else ''}")
            self.assertNotIn("click here", text.lower(), label)
            self.assertEqual(text.count("!"), 0, label)

        script = (self.NODE_ENV % json.dumps(NOTIFY_JS)) + r"""
const window = {navigator: {}, addEventListener() {}, fetch: () => Promise.reject(new Error('no'))};
vm.runInContext(SOURCE, vm.createContext({window, document: {getElementById: () => null, addEventListener() {}}}));
console.log(JSON.stringify(window.VFNotify.copy));
"""
        copy = json.loads(run_node(script))
        for group in copy.values():
            for key, value in group.items():
                check(f"notify.js {key}", value)
        self.assertEqual(copy["labels"], {"turnOn": "Turn on notifications", "turnOff": "Turn off notifications",
                                          "test": "Send a test notification", "guide": "Home Screen install guide"})
        self.assertEqual(copy["text"]["blocked"], "Notifications are blocked for Village Fairways. Turn them on in your phone's Settings.")
        self.assertEqual(copy["text"]["unsupported"], "This browser can't show notifications.")
        sample = {"reservation_no": "1", "course": "Silver Lake", "display_time": "7:18 AM", "date_label": "Saturday, 10/17/2026",
                  "play_date": "2026-10-17"}
        for now in (utc(2026, 10, 14, 12), utc(2026, 10, 16, 23, 18), utc(2026, 10, 17, 12)):
            check("reminder payload", " ".join(self.push.reminder_payload(sample, now).values()))
        check("booking payload", " ".join(self.push.booking_payload(sample).values()))
        check("test payload", " ".join(self.push.device_test_payload().values()))
        check("not set up", self.push.NOT_SET_UP_MESSAGE)
        server_text = (ROOT / "app.py").read_text(encoding="utf-8")
        for message in re.findall(r'"error": "([^"]+)"', server_text[server_text.index("def push_config"):server_text.index("def logout")]):
            check("push route message", message)
        section = HELP[HELP.index('<h2 id="notifications">'):HELP.index('<h2 id="trouble">')]
        check("help notifications section", visible_text(section))
        self.assertIn('<li><a href="#notifications">Phone notifications</a></li>', HELP)
        self.assertLess(HELP.index('href="#account"'), HELP.index('href="#notifications"'))
        self.assertLess(HELP.index('href="#notifications"'), HELP.index('href="#trouble"'))
        self.assertIn("<ol>", section)
        for label in ("Account", "Turn on notifications", "Allow", "Send a test notification", "Turn off notifications"):
            self.assertIn(f"<strong>{label}</strong>", section)
        install = (ROOT / "templates" / "install.html").read_text(encoding="utf-8")
        self.assertIn("Phone notifications when you book and before your tee time, after you turn them on in <strong>Account</strong>.", install)
        self.assertEqual(NOTIFY_JS.count(chr(0x2014)), 0)


if __name__ == "__main__":
    unittest.main()
