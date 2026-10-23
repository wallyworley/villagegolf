"""API regressions with temporary encrypted profiles and a fake golf worker."""
import importlib
import json
import os
import tempfile
import threading
import time
import types
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet


class ApiTests(unittest.TestCase):
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

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api.user_store._DB_PATH = self.tmp.name + "/users.db"
        self.api.user_store._initialized = False
        self.api.user_store._fernet = Fernet(Fernet.generate_key())
        self.api.request_watches._DB_PATH = self.api.user_store._DB_PATH
        self.api.request_watches._initialized = False
        self.api._route_attempts.clear()
        self.api.user_store.set_user("golfer", {
            "tvn_password": "fixture", "golf_password": "1234", "email": "fixture@example.invalid",
            "buddies": [{"id": "111", "name": "Test Golfer"}, {"id": "222", "name": "Test Buddy"}],
        })
        self.client = self.api.app.test_client()
        with self.client.session_transaction() as session:
            session["auth"] = True
            session["username"] = "golfer"
        self.worker = Mock()
        self.worker.call.return_value = {"success": False, "unknown": True, "error": "Fixture unconfirmed"}
        self.worker_patch = patch.object(self.api, "get_worker", return_value=self.worker)
        self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.catalog = types.SimpleNamespace(
            get_courses=lambda: [{"id": "silver-lake", "name": "Silver Lake"}],
            route_to_course=Mock(return_value={"success": True, "miles": 2, "minutes": 10}),
            map_image=Mock(return_value=(b"\x89PNG\r\n\x1a\nx", None, 200)),
            CatalogUnavailable=type("CatalogUnavailable", (RuntimeError,), {}),
        )
        self.maps_patch = patch.dict("sys.modules", {"mapping_service": self.catalog})
        self.maps_patch.start()
        self.addCleanup(self.maps_patch.stop)

    def booking(self, **changes):
        value = {"date": "20260912", "course_type": "Executive", "golfer_ids": ["111"],
                 "num_golfers": 1, "has_guests": False, "course": "Silver Lake", "time": "08:00", "search_id": "test-search"}
        value.update(changes)
        return value

    def test_capitalized_username_reaches_the_existing_profile(self):
        # Phones capitalize the first letter; the Villages login ignores case.
        self.assertEqual(self.api.user_store.find_username("Golfer"), "golfer")
        self.assertIsNone(self.api.user_store.find_username("nobody"))
        client = self.api.app.test_client()
        response = client.post("/api/login-user", json={"username": "Golfer", "password": "fixture", "golf_pin": "1234"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["username"], "golfer")
        with client.session_transaction() as session:
            self.assertEqual(session["username"], "golfer")
        self.worker.call.return_value = {"success": True, "primary": {"id": "111", "name": "Test Golfer"},
                                         "buddies": [{"id": "111", "name": "Test Golfer"}]}
        response = client.post("/api/register", json={"username": "GOLFER", "password": "fixture", "golf_pin": "1234"})
        self.assertEqual(response.get_json()["username"], "golfer")
        self.assertEqual(self.worker.call.call_args.kwargs["tvn_username"], "golfer")
        self.assertEqual(self.api.user_store.find_username("GOLFER"), "golfer")
        self.assertEqual(sorted(self.api.user_store.all_users()), ["golfer"])
        self.assertEqual(self.api.user_store.get_user("golfer")["email"], "fixture@example.invalid")

    def test_book_requires_search_identity(self):
        response = self.client.post("/api/book", json=self.booking(search_id=""))
        self.assertEqual(response.status_code, 400)
        self.worker.call.assert_not_called()

    def test_booking_passes_complete_context_and_preserves_unknown(self):
        response = self.client.post("/api/book", json=self.booking(has_guests=True))
        self.assertTrue(response.json["unknown"])
        kwargs = self.worker.call.call_args.kwargs
        self.assertTrue(kwargs["expected_has_guests"])
        self.assertEqual(kwargs["expected_course_type"], "Executive")
        self.assertEqual(kwargs["search_id"], "test-search")

    def test_invalid_parties_do_not_reach_worker(self):
        for ids in [["111", "111"], ["111"] * 5, ["bad"]]:
            with self.subTest(ids=ids):
                response = self.client.post("/api/tee-times", json=self.booking(golfer_ids=ids, num_golfers=len(ids)))
                self.assertEqual(response.status_code, 400)
        self.worker.call.assert_not_called()

    def test_null_optional_time_window_is_accepted(self):
        # The page sends null for an empty time window; the hook must not
        # reject the search, and the route treats null as no filter.
        self.worker.call.return_value = {"success": True, "times": [], "search_id": "s", "date_label": "Sat 9/12"}
        payload = self.booking(time_from=None, time_to=None, time_filter="all")
        payload.pop("course"); payload.pop("time"); payload.pop("search_id")
        response = self.client.post("/api/tee-times", json=payload)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(self.worker.call.called)
        bad = self.client.post("/api/tee-times", json=dict(payload, time_from=123))
        self.assertEqual(bad.status_code, 400)

    def test_json_array_and_nonboolean_guest_rejected(self):
        self.assertEqual(self.client.post("/api/book", json=[]).status_code, 400)
        self.assertEqual(self.client.post("/api/book", json=self.booking(has_guests="false")).status_code, 400)

    def test_exact_time_window_filters_real_afternoon_hour(self):
        self.worker.call.return_value = {"success": True, "search_id": "test", "times": [
            {"time": "12:30"}, {"time": "01:15"}, {"time": "02:00"},
        ]}
        response = self.client.post("/api/tee-times", json=self.booking(time_from="13:00", time_to="13:30"))
        self.assertEqual(response.json["times"], [{"time": "01:15"}])

    def test_preferences_are_scoped_and_persist_without_losing_email(self):
        response = self.client.post("/api/preferences", json={"favorites": ["silver-lake"], "arrival_buffer_minutes": 25})
        self.assertEqual(response.status_code, 200)
        self.client.post("/api/update-email", json={"email": "new@example.invalid"})
        profile = self.api.user_store.get_user("golfer")
        self.assertEqual(profile["preferences"]["favorites"], ["silver-lake"])
        self.assertEqual(profile["email"], "new@example.invalid")
        self.api.user_store.set_user("another", {"tvn_password": "fixture", "golf_password": "1234"})
        with self.client.session_transaction() as session:
            session["username"] = "another"
        self.assertEqual(self.client.get("/api/preferences").json["preferences"]["favorites"], [])

    def test_invalid_home_is_not_saved_or_routed(self):
        for origin in [{"latitude": "nan", "longitude": -82}, {"latitude": 40, "longitude": -74}]:
            self.assertEqual(self.client.post("/api/preferences", json={"home": origin}).status_code, 400)
            self.assertEqual(self.client.post("/api/course-route", json={"course_id": "silver-lake", "origin": origin}).status_code, 400)
        self.catalog.route_to_course.assert_not_called()

    def test_mapping_never_uses_golf_worker(self):
        self.client.get("/api/courses")
        result = self.client.post("/api/course-route", json={"course_id": "silver-lake", "origin": {"latitude": 28.9, "longitude": -82.0}})
        self.assertTrue(result.json["success"])
        self.worker.call.assert_not_called()

    def test_cancel_request_cancels_notification_only_on_confirmed_success(self):
        with patch.object(self.api.request_watches, "cancel_request_watch") as cancel:
            self.client.post("/api/delete-request", json={"request_id": "123456"})
            cancel.assert_not_called()
            self.worker.call.return_value = {"success": True}
            self.client.post("/api/delete-request", json={"request_id": "123456"})
            cancel.assert_called_once_with("golfer", "123456")

    def test_request_party_and_time_validation(self):
        value = {"play_date": "20260912", "max_golfers": 1, "course_type": "Executive", "preference": "Course",
                 "course_choices": ["Silver Lake"], "golfer_ids": ["111", "222"],
                 "time_to_play": "08:00", "earliest_time": "07:00", "latest_time": "10:00"}
        self.assertEqual(self.client.post("/api/submit-request", json=value).status_code, 400)
        value.update(golfer_ids=["111"], earliest_time="09:00")
        self.assertEqual(self.client.post("/api/submit-request", json=value).status_code, 400)
        self.worker.call.assert_not_called()

    def test_failed_notification_remains_retryable(self):
        w = self.api.request_watches
        wid = w.add_watch("golfer", "123456", "20260912", "Saturday", ["Silver Lake"], "fixture@example.invalid")
        w.mark_result(wid, "unknown")
        with patch.object(self.api, "send_request_result", return_value=False) as send:
            self.api._send_pending_request_notifications()
            send.assert_called_once()
        record = w.all_watches()[0]
        self.assertEqual(record["notification_status"], "pending")
        self.assertEqual(record["notification_attempts"], 1)

    def test_due_watch_checks_are_capped_and_paused(self):
        w = self.api.request_watches
        ids = [w.add_watch(f"user{i}", None, "20260912", "Saturday", [], "") for i in range(3)]
        past = "2000-01-01 00:00:00"
        with closing(w._connect()) as conn, conn:
            for wid in ids:
                conn.execute("UPDATE request_watches SET check_after=? WHERE id=?", (past, wid))
        self.worker.call.return_value = {"success": True, "verified": True, "reservations": []}
        fixture_user = self.api.user_store.get_user("golfer")
        with patch.object(self.api, "_WATCH_BATCH_LIMIT", 2), \
             patch.object(self.api, "_watch_pause") as pause, \
             patch.object(self.api, "_get_user", return_value=fixture_user):
            self.api._run_due_request_checks()
        self.assertEqual(self.worker.call.call_count, 2)
        pause.assert_called_once()

    def test_refresh_buddies_without_body_reaches_worker(self):
        self.worker.call.return_value = {
            "success": True,
            "buddies": [{"id": "333", "name": "New Buddy"}],
            "primary": {"id": "111", "name": "Test Golfer"},
        }
        response = self.client.post("/api/refresh-buddies")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertEqual(self.worker.call.call_args.args[0], "fetch_buddy_list")

    def test_busy_mutation_returns_409(self):
        self.worker.call.return_value = {"success": False, "busy": True, "error": "busy"}
        response = self.client.post("/api/book", json=self.booking())
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json["busy"])
        response = self.client.post("/api/delete-reservation", json={"reservation_no": "123456"})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json["busy"])

    def test_mutations_pass_exclusive_key(self):
        self.client.post("/api/book", json=self.booking())
        self.assertEqual(self.worker.call.call_args.kwargs["exclusive_key"], "golfer")

    def test_booking_success_enqueues_durable_confirmation(self):
        self.worker.call.return_value = {
            "success": True, "golfer_ids": ["111"], "reservation_no": "654321",
            "course": "Silver Lake", "display_time": "8:00 AM", "date_label": "Saturday, 9/12/2026",
        }
        with patch.object(self.api, "threading") as fake_threading:
            response = self.client.post("/api/book", json=self.booking())
            fake_threading.Thread.assert_not_called()
        self.assertTrue(response.json["success"])
        with closing(self.api.request_watches._connect()) as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM notification_outbox").fetchall()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["username"], "golfer")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        payload = json.loads(row["payload"])
        self.assertEqual(payload["golfer_names"], ["Test Golfer"])

    def test_booking_success_survives_email_enqueue_failure(self):
        self.worker.call.return_value = {
            "success": True, "golfer_ids": ["111"], "reservation_no": "654321",
            "course": "Silver Lake", "display_time": "8:00 AM", "date_label": "Saturday, 9/12/2026",
        }
        with patch.object(self.api, "_booking_recipients", side_effect=RuntimeError("boom")):
            response = self.client.post("/api/book", json=self.booking())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        self.assertEqual(response.json["reservation_no"], "654321")

        # A malformed buddy record (missing "id") must not raise either, and
        # the booker's own confirmation must still be enqueued.
        self.api.user_store.set_user("golfer", {
            "tvn_password": "fixture", "golf_password": "1234", "email": "fixture@example.invalid",
            "buddies": [{"name": "No Id Buddy"}],
        })
        response = self.client.post("/api/book", json=self.booking())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["success"])
        with closing(self.api.request_watches._connect()) as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM notification_outbox").fetchall()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "golfer")

    def test_outbox_retries_failed_booking_email(self):
        w = self.api.request_watches
        outbox_id = w.enqueue_notification(
            "booking_confirmation", "golfer", "fixture@example.invalid",
            {"reservation_no": "1", "course": "Silver Lake", "display_time": "8:00 AM",
             "date_label": "Saturday", "golfer_names": ["Test Golfer"]},
        )
        with patch.object(self.api, "send_booking_confirmation", return_value=False) as send:
            self.api._send_pending_outbox()
            send.assert_called_once()
        with closing(w._connect()) as conn:
            row = dict(conn.execute("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)).fetchone())
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)

        with closing(w._connect()) as conn, conn:
            conn.execute(
                "UPDATE notification_outbox SET notify_after=? WHERE id=?", ("2000-01-01 00:00:00", outbox_id)
            )

        with patch.object(self.api, "send_booking_confirmation", return_value=True) as send:
            self.api._send_pending_outbox()
            send.assert_called_once()
        with closing(w._connect()) as conn:
            row = dict(conn.execute("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)).fetchone())
        self.assertEqual(row["status"], "sent")

    def test_concurrent_outbox_senders_send_once(self):
        w = self.api.request_watches
        outbox_id = w.enqueue_notification(
            "booking_confirmation", "golfer", "fixture@example.invalid",
            {"reservation_no": "1", "course": "Silver Lake", "display_time": "8:00 AM",
             "date_label": "Saturday", "golfer_names": ["Test Golfer"]},
        )
        calls = []

        def slow_send(*args, **kwargs):
            calls.append((args, kwargs))
            time.sleep(0.2)
            return True

        with patch.object(self.api, "send_booking_confirmation", side_effect=slow_send):
            threads = [threading.Thread(target=self.api._send_pending_outbox) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(calls), 1)
        with closing(w._connect()) as conn:
            row = dict(conn.execute("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)).fetchone())
        self.assertEqual(row["status"], "sent")

    def test_unknown_outbox_kind_fails_immediately(self):
        w = self.api.request_watches
        outbox_id = w.enqueue_notification("mystery_kind", "golfer", "fixture@example.invalid", {"x": 1})
        self.api._send_pending_outbox()
        with closing(w._connect()) as conn:
            row = dict(conn.execute("SELECT * FROM notification_outbox WHERE id=?", (outbox_id,)).fetchone())
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 1)

    def test_course_route_reports_service_failure_as_json(self):
        origin = {"latitude": 28.9, "longitude": -82.0}
        payload = {"course_id": "silver-lake", "origin": origin}

        self.catalog.route_to_course = Mock(side_effect=RuntimeError("x"))
        response = self.client.post("/api/course-route", json=payload)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json["success"])

        self.catalog.route_to_course = Mock(return_value={"success": False, "error": "unknown course"})
        response = self.client.post("/api/course-route", json=payload)
        self.assertEqual(response.status_code, 404)

        self.catalog.route_to_course = Mock(
            return_value={"success": False, "error": "routing service unavailable: URLError"}
        )
        response = self.client.post("/api/course-route", json=payload)
        self.assertEqual(response.status_code, 502)

    def test_catalog_unavailable_returns_503_not_500(self):
        self.catalog.get_courses = Mock(side_effect=self.catalog.CatalogUnavailable("boom"))
        response = self.client.get("/api/courses")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["success"])

        response = self.client.post("/api/preferences", json={"favorites": ["silver-lake"]})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ok"])

        self.catalog.get_courses = lambda: [{"id": "silver-lake", "name": "Silver Lake"}]
        self.catalog.route_to_course = Mock(side_effect=self.catalog.CatalogUnavailable("boom"))
        response = self.client.post(
            "/api/course-route",
            json={"course_id": "silver-lake", "origin": {"latitude": 28.9, "longitude": -82.0}},
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["success"])

    def test_tee_times_accepts_band_keys_and_rejects_legacy_regions(self):
        self.worker.call.return_value = {"success": True, "search_id": "test", "times": []}
        response = self.client.post("/api/tee-times", json=self.booking(region_filter="between-the-6s"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.worker.call.call_args.kwargs["region_filter"], "between-the-6s")

        self.worker.call.reset_mock()
        response = self.client.post("/api/tee-times", json=self.booking(region_filter="north"))
        self.assertEqual(response.status_code, 400)
        self.worker.call.assert_not_called()

    def test_map_image_validates_and_serves_png(self):
        params = {"min_lon": "-82.02", "min_lat": "28.88", "max_lon": "-81.96", "max_lat": "28.94", "h": "600"}
        response = self.client.get("/api/map-image", query_string=params)
        self.assertEqual(response.status_code, 400)

        params["w"] = "800"
        response = self.client.get("/api/map-image", query_string=params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertIn("max-age", response.headers.get("Cache-Control", ""))

        self.catalog.map_image = Mock(return_value=(None, "map service unavailable: URLError", 502))
        response = self.client.get("/api/map-image", query_string=params)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json["success"])

    def test_map_image_requires_auth(self):
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.get("/api/map-image", query_string={
            "min_lon": "-82.02", "min_lat": "28.88", "max_lon": "-81.96", "max_lat": "28.94",
            "w": "800", "h": "600",
        })
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
