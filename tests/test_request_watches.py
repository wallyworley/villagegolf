import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
import request_watches as watches


class RequestWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        watches._DB_PATH = self.tmp.name + "/test.db"
        watches._initialized = False

    def watch(self, **changes):
        value = {"play_date": "20260912", "request_no": "123456", "courses": '["Silver Lake"]',
                 "preferences": json.dumps({"earliest_time": "07:00", "latest_time": "10:00", "golfer_ids": ["111"]})}
        value.update(changes)
        return value

    def reservation(self, **changes):
        value = {"reservation_no": "555555", "play_date": "2026-09-12", "course": "Silver Lake", "display_time": "8:00 AM"}
        value.update(changes)
        return value

    def test_date_normalization_checks_calendar_and_year(self):
        self.assertIsNone(watches.normalize_play_date("20260230"))
        self.assertEqual(watches.normalize_play_date("9/12/2026"), "20260912")
        self.assertTrue(watches.reservation_matches("Saturday: 09/12", "20260912"))
        self.assertFalse(watches.reservation_matches("9/12/2025", "20260912"))

    def test_same_day_is_insufficient_evidence(self):
        self.assertIsNone(watches.matching_reservation(self.watch(), [self.reservation(course="Saddlebrook")]))
        self.assertIsNone(watches.matching_reservation(self.watch(courses="[]"), [self.reservation()]))
        self.assertIsNone(watches.matching_reservation(self.watch(), [self.reservation(display_time="3:00 PM")]))
        self.assertIsNone(watches.matching_reservation(self.watch(), [self.reservation(golfer_ids=["222"])]))

    def test_single_matching_reservation_and_ambiguity(self):
        r = self.reservation()
        self.assertEqual(watches.matching_reservation(self.watch(), [r]), r)
        self.assertIsNone(watches.matching_reservation(self.watch(), [r, self.reservation(reservation_no="666666")]))

    def test_empty_result_gets_grace_period(self):
        self.assertFalse(watches.assignment_grace_passed(self.watch(), datetime(2026, 9, 9, 5, 40, tzinfo=timezone.utc)))
        self.assertTrue(watches.assignment_grace_passed(self.watch(), datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)))

    def test_cancel_prevents_result_and_queued_notification(self):
        wid = watches.add_watch("user", "123456", "20260912", "Saturday", ["Silver Lake"], "fixture@example.invalid")
        watches.cancel_request_watch("other", "123456")
        self.assertEqual(watches.all_watches()[0]["status"], "pending")
        watches.cancel_request_watch("user", "123456")
        self.assertFalse(watches.mark_result(wid, "matched", self.reservation()))
        self.assertEqual(watches.due_notifications(), [])

    def test_enrollment_idempotence_and_retry_survives_reopen(self):
        wid = watches.add_watch("user", "123456", "20260912", "Saturday", ["Silver Lake"], "")
        self.assertIsNone(watches.add_watch("user", "123456", "20260912", "Saturday", [], ""))
        watches.mark_result(wid, "unknown")
        watches.finish_notification(wid, False)
        watches._initialized = False
        record = watches.all_watches()[0]
        self.assertEqual(record["notification_attempts"], 1)
        self.assertEqual(record["notification_status"], "pending")

    def test_cancelled_watch_allows_reenrollment(self):
        wid1 = watches.add_watch("user", "123456", "20260912", "Saturday", ["Silver Lake"], "fixture@example.invalid")
        watches.cancel_request_watch("user", "123456")
        wid2 = watches.add_watch("user", "123456", "20260912", "Saturday", ["Silver Lake"], "fixture@example.invalid")
        self.assertIsNotNone(wid2)
        self.assertNotEqual(wid1, wid2)
        past = "2000-01-01 00:00:00"
        with closing(watches._connect()) as conn, conn:
            conn.execute("UPDATE request_watches SET check_after=? WHERE id=?", (past, wid2))
        due_ids = {w["id"] for w in watches.due_watches()}
        self.assertEqual(due_ids, {wid2})

    def test_play_date_only_watch_is_idempotent_while_pending_and_reenrolls_after_cancel(self):
        wid1 = watches.add_watch("user", None, "20260912", "Saturday", ["Silver Lake"], "fixture@example.invalid")
        self.assertIsNotNone(wid1)
        self.assertIsNone(
            watches.add_watch("user", None, "20260912", "Saturday", ["Silver Lake"], "fixture@example.invalid")
        )
        watches.cancel_watch(wid1)
        wid2 = watches.add_watch("user", None, "20260912", "Saturday", ["Silver Lake"], "fixture@example.invalid")
        self.assertIsNotNone(wid2)
        self.assertNotEqual(wid1, wid2)

    def test_due_watches_limit(self):
        ids = [watches.add_watch(f"user{i}", None, "20260912", "Saturday", [], "") for i in range(3)]
        with closing(watches._connect()) as conn, conn:
            for index, wid in enumerate(ids):
                conn.execute(
                    "UPDATE request_watches SET check_after=? WHERE id=?",
                    (f"2000-01-01 00:00:0{index}", wid),
                )
        self.assertEqual(len(watches.due_watches()), 3)
        limited = watches.due_watches(limit=2)
        self.assertEqual([w["id"] for w in limited], ids[:2])

    def test_outbox_enqueue_due_finish_and_cancel(self):
        oid = watches.enqueue_notification(
            "booking_confirmation", "user", "fixture@example.invalid", {"reservation_no": "1"},
        )
        due = watches.due_outbox()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["id"], oid)
        self.assertEqual(json.loads(due[0]["payload"]), {"reservation_no": "1"})

        past = "2000-01-01 00:00:00"
        for attempt in range(1, watches._MAX_ATTEMPTS):
            watches.finish_outbox(oid, False)
            with closing(watches._connect()) as conn:
                row = conn.execute(
                    "SELECT status, attempts FROM notification_outbox WHERE id=?", (oid,)
                ).fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["attempts"], attempt)
            with closing(watches._connect()) as conn, conn:
                conn.execute("UPDATE notification_outbox SET notify_after=? WHERE id=?", (past, oid))

        watches.finish_outbox(oid, False)
        with closing(watches._connect()) as conn:
            row = conn.execute("SELECT status, attempts FROM notification_outbox WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], watches._MAX_ATTEMPTS)
        self.assertEqual(watches.due_outbox(), [])

        oid2 = watches.enqueue_notification("booking_confirmation", "user2", "b@example.invalid", {"x": 1})
        watches.cancel_outbox_for_user("user2")
        self.assertEqual(watches.due_outbox(), [])
        with closing(watches._connect()) as conn:
            row2 = conn.execute("SELECT status FROM notification_outbox WHERE id=?", (oid2,)).fetchone()
        self.assertEqual(row2["status"], "cancelled")

    def test_outbox_claim_is_exclusive_and_stale_claims_recover(self):
        oid = watches.enqueue_notification(
            "booking_confirmation", "user", "fixture@example.invalid", {"reservation_no": "1"},
        )
        self.assertTrue(watches.claim_outbox(oid))
        self.assertFalse(watches.claim_outbox(oid))

        eleven_minutes_ago = (datetime.now(timezone.utc) - timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S")
        with closing(watches._connect()) as conn, conn:
            conn.execute("UPDATE notification_outbox SET notify_after=? WHERE id=?", (eleven_minutes_ago, oid))
        due_ids = {row["id"] for row in watches.due_outbox()}
        self.assertIn(oid, due_ids)
        # A stale claim can be taken over; a fresh claim cannot.
        self.assertTrue(watches.claim_outbox(oid))
        self.assertFalse(watches.claim_outbox(oid))

        watches.cancel_outbox_for_user("user")
        with closing(watches._connect()) as conn:
            row = conn.execute("SELECT status FROM notification_outbox WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row["status"], "cancelled")

        watches.finish_outbox(oid, True)
        with closing(watches._connect()) as conn:
            row = conn.execute("SELECT status FROM notification_outbox WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
