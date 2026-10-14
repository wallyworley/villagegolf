import shutil
import subprocess
import unittest
from collections import OrderedDict
from datetime import date

import golf_service
from golf_service import GolfService, _Session


class PayloadPage:
    def __init__(self, payload):
        self.payload = payload

    def evaluate(self, _script, *_args):
        return self.payload


class StubReservationService(GolfService):
    def __init__(self, reservations):
        super().__init__()
        self.reservations = reservations

    def _ensure_logged_in(self, *_args):
        pass

    def _nav_to_glf109a(self, _page):
        pass

    def _extract_reservations(self, _page):
        return self.reservations


class FakeElement:
    def __init__(self, text="", attrs=None, child=None):
        self.text = text
        self.attrs = attrs or {}
        self.child = child
        self.filled = None
        self.clicked = False

    def inner_text(self):
        return self.text

    def query_selector(self, selector):
        if selector == "a":
            return self.child
        if 'input[type="hidden"]' in selector:
            return self.attrs.get("hidden")
        if 'input[type="Text"]' in selector:
            return self.attrs.get("text_input")
        return None

    def query_selector_all(self, selector):
        if selector == "td":
            return self.attrs.get("cells", [])
        return []

    def get_attribute(self, name):
        return self.attrs.get(name)

    def fill(self, value):
        self.filled = value

    def click(self, **_kwargs):
        self.clicked = True


class SubmitRaisesPage:
    def __init__(self):
        self.url = "https://example.test/glf109e"
        self.link = FakeElement()
        self.allocation = FakeElement(attrs={
            "hidden": FakeElement(attrs={"value": "020501"}),
            "text_input": FakeElement(attrs={"name": "allo2"}),
        })
        self.row_calls = 0

    def query_selector_all(self, _selector):
        self.row_calls += 1
        if self.row_calls == 1:
            return [FakeElement(child=self.link), FakeElement()]
        return [self.allocation]

    def wait_for_url(self, _pattern, timeout=None):
        self.url = "https://example.test/glf109y"

    def evaluate(self, _script, *_args):
        raise RuntimeError("connection dropped after form.submit")


class BookingBoundaryService(GolfService):
    def __init__(self, page, context):
        super().__init__()
        session = _Session()
        session.page = page
        session.context = object()
        session.search_context = context
        self._sessions = OrderedDict([("walter", session)])
        self.reconciled = False

    def _session_usable(self, _session):
        return True

    def _reconcile_booking_outcome(self, *_args, **_kwargs):
        self.reconciled = True
        return {"success": False, "unknown": True, "error": "verify"}


class DeclinedReplyPage(SubmitRaisesPage):
    """The Villages answers the submit with a page that has no reservation number."""

    def __init__(self, body):
        super().__init__()
        self.body = body

    def wait_for_url(self, pattern, timeout=None):
        self.url = "https://example.test/" + ("glf109g" if "glf109g" in pattern else "glf109y")

    def evaluate(self, _script, *_args):
        return None

    def inner_text(self, _selector):
        return self.body


class DeclinedReplyService(BookingBoundaryService):
    def _reconcile_booking_outcome(self, *args, **kwargs):
        return GolfService._reconcile_booking_outcome(self, *args, **kwargs)

    def _find_booked_reservation(self, *_args):
        return None

    def _close_active(self):
        pass


VILLAGES_HEADER = (
    "Common Internet TermsLegal Disclaimer\nWednesday October 14, 2026\nHOME\nEMAIL\n"
    "GOLF\nEVENTS\nNEWS\nUSER INFO\nHELP\nLOGOUT\nThe Villages Tee Time System\n"
    "Reservations\nPat Golfer\nYour ID is: 100001\n\nClick here for Help\n\n"
)


class SelectionLocator:
    def __init__(self, value=None, exists=True):
        self.value = value
        self.exists = exists
        self.first = self

    def count(self):
        return 1 if self.exists else 0

    def select_option(self, value):
        self.value = value

    def input_value(self):
        return self.value


class SelectionPage:
    def __init__(self, slots=4):
        self.slots = [SelectionLocator() for _ in range(slots)]

    def locator(self, selector, **_kwargs):
        number = int(selector.split("buddy", 1)[1].split('"', 1)[0])
        if number <= len(self.slots):
            return self.slots[number - 1]
        return SelectionLocator(exists=False)


class DeleteLocator:
    def __init__(self):
        self.first = self
        self.last = self
        self.clicked = False

    def click(self, **_kwargs):
        self.clicked = True

    def count(self):
        return 1

    def wait_for(self, **_kwargs):
        pass

    def scroll_into_view_if_needed(self, **_kwargs):
        pass


class WrongDeleteConfirmationPage:
    def __init__(self):
        self.url = "https://example.test/glf109a"
        self.row_delete = DeleteLocator()
        self.final_delete_requested = False

    def evaluate(self, _script, *_args):
        return True

    def locator(self, selector, **kwargs):
        if "data-delete-reservation" in selector:
            return self.row_delete
        if kwargs.get("has_text") == "DELETE ALL PLAYERS ON THIS RESERVATION":
            self.final_delete_requested = True
        return DeleteLocator()

    def wait_for_load_state(self, *_args, **_kwargs):
        pass

    def inner_text(self, _selector):
        return "Delete Reservation No. 999999"


class DeleteService(GolfService):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self._active = _Session()
        self._active.page = page

    def _get_or_create_session(self, _username):
        return None, self.page

    def _extract_reservations(self, _page):
        return [{"reservation_no": "123456"}]


class GolfServiceTests(unittest.TestCase):
    def test_year_inference_uses_eastern_upcoming_date_and_weekday(self):
        self.assertEqual(
            golf_service._normalize_play_date("Thursday: 9/10", date(2026, 9, 9)),
            "2026-09-10",
        )
        self.assertEqual(
            golf_service._normalize_play_date("Thursday: 9/10", date(2026, 9, 11)),
            "2026-09-10",
        )
        self.assertEqual(
            golf_service._normalize_play_date("Thursday: 9/10/2026", date(2026, 1, 1)),
            "2026-09-10",
        )

    def test_extract_reservation_preserves_structured_fields_and_observed_golfers(self):
        page = PayloadPage({
            "valid": True,
            "rows": [{
                "cells": [
                    "View\nDELETE ALL PLAYERS ON THIS RESERVATION",
                    "Reservation No. 812345",
                    "Thursday: 9/10/2026 at 2:05 PM\n"
                    "Course: Southern Oaks, hole 1\n"
                    "Phone: (352) 555-0123\n"
                    "Golfers: Walter Worley 483204; Marcia Worley 640405",
                ],
                "links": ["View", "DELETE ALL PLAYERS ON THIS RESERVATION"],
                "golferIds": [],
            }],
        })
        reservation = GolfService()._extract_reservations(page)[0]
        self.assertEqual(reservation["reservation_no"], "812345")
        self.assertEqual(reservation["play_date"], "2026-09-10")
        self.assertEqual(reservation["display_time"], "2:05 PM")
        self.assertEqual(reservation["course"], "Southern Oaks, hole 1")
        self.assertEqual(reservation["phone"], "(352) 555-0123")
        self.assertEqual(reservation["golfer_ids"], ["483204", "640405"])

    def test_extract_does_not_invent_golfer_ids_from_phone_or_reservation(self):
        page = PayloadPage({
            "valid": True,
            "rows": [{
                "cells": ["View", "Reservation No. 812345",
                          "Thursday: 9/10/2026 at 2:05 PM\nCourse: Riley Grove\nPhone: 3525550123"],
                "links": ["View"],
                "golferIds": [],
            }],
        })
        reservation = GolfService()._extract_reservations(page)[0]
        self.assertNotIn("golfer_ids", reservation)

    def test_invalid_reservations_page_is_not_treated_as_empty(self):
        with self.assertRaisesRegex(RuntimeError, "layout was not recognized"):
            GolfService()._extract_reservations(PayloadPage({"valid": False, "rows": []}))

    def test_reconciliation_requires_exact_new_date_course_and_time(self):
        reservations = [
            {"reservation_no": "old", "play_date": "2026-09-10",
             "display_time": "2:05 PM", "course": "Southern Oaks, hole 1"},
            {"reservation_no": "wrong-course", "play_date": "2026-09-10",
             "display_time": "2:05 PM", "course": "Riley Grove"},
            {"reservation_no": "new", "play_date": "2026-09-10",
             "display_time": "2:05 PM", "course": "Southern Oaks"},
        ]
        service = StubReservationService(reservations)
        found = service._find_booked_reservation(
            object(), "walter", "pw", "pin", "02:05", "20260910",
            "Southern Oaks", {"old"},
        )
        self.assertEqual(found["reservation_no"], "new")
        self.assertIsNone(service._find_booked_reservation(
            object(), "walter", "pw", "pin", "02:05", "20260910",
            "Southern Oaks", None,
        ))

    def test_reconciliation_rejects_two_matching_new_reservations(self):
        reservations = [
            {"reservation_no": "old", "play_date": "2026-09-10",
             "display_time": "2:05 PM", "course": "Southern Oaks, hole 1"},
            {"reservation_no": "new-1", "play_date": "2026-09-10",
             "display_time": "2:05 PM", "course": "Southern Oaks"},
            {"reservation_no": "new-2", "play_date": "2026-09-10",
             "display_time": "2:05 PM", "course": "Southern Oaks"},
        ]
        service = StubReservationService(reservations)
        found = service._find_booked_reservation(
            object(), "walter", "pw", "pin", "02:05", "20260910",
            "Southern Oaks", {"old"},
        )
        self.assertIsNone(found)

    def test_booking_submit_exception_is_reconciled_as_unknown(self):
        page = SubmitRaisesPage()
        # First table row has cells representing action, course and time.
        tee_row = FakeElement(attrs={"cells": [
            FakeElement(child=page.link),
            FakeElement("Southern Oaks"),
            FakeElement("02:05"),
        ]})
        page.query_selector_all = lambda _selector, calls=iter([
            [tee_row],
            [page.allocation],
        ]): next(calls)
        context = {
            "user": "walter", "date_str": "20260910", "play_date": "2026-09-10",
            "date_label": "Thursday, Sep 10", "course_type": "Executive",
            "golfer_ids": ["483204"], "num_golfers": 1, "has_guests": False,
            "search_id": "search-1", "offered_times": [("Southern Oaks", "02:05")],
            "preexisting_reservation_ids": set(),
        }
        service = BookingBoundaryService(page, context)
        result = service.book_tee_time(
            "walter", "pw", "pin", "Southern Oaks", "02:05",
            expected_date="20260910", expected_golfer_ids=["483204"],
            expected_num_golfers=1, expected_course_type="Executive",
            expected_has_guests=False, search_id="search-1",
        )
        self.assertTrue(service.reconciled)
        self.assertTrue(result["unknown"])

    def test_site_reply_drops_the_shared_page_header(self):
        reply = golf_service._site_reply_text(
            VILLAGES_HEADER + "Golfer 100002 already has a tee time on this date.\nReturn"
        )
        self.assertEqual(reply, "Golfer 100002 already has a tee time on this date. Return")
        self.assertLessEqual(len(golf_service._site_reply_text("word " * 200)), 301)

    def test_declined_booking_shows_what_the_villages_site_said(self):
        page = DeclinedReplyPage(
            VILLAGES_HEADER + "Golfer 100002 already has a tee time on this date."
        )
        tee_row = FakeElement(attrs={"cells": [
            FakeElement(child=page.link),
            FakeElement("Southern Oaks"),
            FakeElement("02:05"),
        ]})
        page.query_selector_all = lambda _selector, calls=iter([
            [tee_row],
            [page.allocation],
        ]): next(calls)
        context = {
            "user": "walter", "date_str": "20260910", "play_date": "2026-09-10",
            "date_label": "Thursday, Sep 10", "course_type": "Executive",
            "golfer_ids": ["100001"], "num_golfers": 1, "has_guests": False,
            "search_id": "search-1", "offered_times": [("Southern Oaks", "02:05")],
            "preexisting_reservation_ids": set(),
        }
        with self.assertLogs("golf_service", "WARNING") as logs:
            result = DeclinedReplyService(page, context).book_tee_time(
                "walter", "pw", "pin", "Southern Oaks", "02:05",
                expected_date="20260910", expected_golfer_ids=["100001"],
                expected_num_golfers=1, expected_course_type="Executive",
                expected_has_guests=False, search_id="search-1",
            )
        self.assertTrue(result["unknown"])
        self.assertIn("already has a tee time on this date", result["error"])
        self.assertIn("check My Tee Times", result["error"])
        self.assertNotIn("Your ID is", result["error"])
        self.assertNotIn("LOGOUT", result["error"])
        self.assertTrue(any("already has a tee time" in line for line in logs.output))

    def test_booking_success_uses_authoritative_search_context(self):
        context = {
            "date_str": "20260910", "play_date": "2026-09-10",
            "date_label": "Thursday, Sep 10", "course_type": "Executive",
            "golfer_ids": ["483204", "640405"], "num_golfers": 2,
            "has_guests": True, "search_id": "search-1",
        }
        result = GolfService()._booking_success("812345", "Southern Oaks", "02:05", context)
        self.assertEqual(result["play_date"], "2026-09-10")
        self.assertEqual(result["golfer_ids"], ["483204", "640405"])
        self.assertEqual(result["num_golfers"], 2)
        self.assertTrue(result["has_guests"])

    def test_request_golfers_must_be_unique_numeric_and_all_selected(self):
        service = GolfService()
        with self.assertRaisesRegex(RuntimeError, "only once"):
            service._fill_request_golfers(SelectionPage(), ["123456", "123456"])
        with self.assertRaisesRegex(RuntimeError, "only numbers"):
            service._fill_request_golfers(SelectionPage(), ["abc"])
        with self.assertRaisesRegex(RuntimeError, "slot 3"):
            service._fill_request_golfers(SelectionPage(slots=2), ["1", "2", "3"])
        page = SelectionPage()
        service._fill_request_golfers(page, ["1", "2", "3", "4"])
        self.assertEqual([slot.value for slot in page.slots], ["1", "2", "3", "4"])

    def test_delete_link_script_marks_only_the_exact_reservation_row(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        harness = r"""
const mark = eval(process.argv[1]);
function link(text) { return {innerText: text, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }}; }
function row(cellTexts, links) {
  return {querySelectorAll: sel => sel === 'td' ? cellTexts.map(t => ({innerText: t})) : links};
}
const longer = link('DELETE ALL PLAYERS ON THIS RESERVATION');
const exact = link('DELETE ALL\nPLAYERS ON THIS RESERVATION');
const rows = [
  row(['Action', 'Open Reservations', 'Description'], []),
  row(['View DELETE ALL PLAYERS ON THIS RESERVATION', 'Reservation No. 5661601', 'Friday: 10/17 at 7:18 AM'], [link('View'), longer]),
  row(['View DELETE ALL PLAYERS ON THIS RESERVATION', 'Reservation No. 566160', 'Friday: 10/17 at 7:18 AM'], [link('View'), exact]),
];
globalThis.document = {querySelectorAll: () => rows};
console.log(JSON.stringify({
  found: mark('566160'),
  exact: exact.attrs['data-delete-reservation'] || null,
  longer: longer.attrs['data-delete-reservation'] || null,
  missing: mark('123456'),
}));
"""
        result = subprocess.run(
            [node, "-e", harness, golf_service._MARK_RESERVATION_DELETE_LINK_JS],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            '{"found":true,"exact":"566160","longer":null,"missing":false}',
        )

    def test_reservation_delete_rejects_confirmation_for_another_id(self):
        page = WrongDeleteConfirmationPage()
        result = DeleteService(page).delete_reservation("walter", "pw", "pin", "123456")
        self.assertFalse(result["success"])
        self.assertIn("did not identify", result["error"])
        self.assertFalse(page.final_delete_requested)

    def test_band_filter_keeps_only_matching_band(self):
        page = PayloadPage([
            {"course": "Havana", "time": "0800", "available": "4"},
            {"course": "Lopez Legacy", "time": "0800", "available": "4"},
            {"course": "Unlisted Course", "time": "0800", "available": "4"},
        ])
        service = GolfService()

        matching = service._extract_times(page, region_filter="between-the-6s")
        self.assertEqual([row["course"] for row in matching], ["Havana"])
        self.assertEqual(matching[0]["band"], "Between the 6s")

        everything = service._extract_times(page, region_filter="all")
        self.assertEqual([row["course"] for row in everything], ["Havana", "Lopez Legacy", "Unlisted Course"])

    def test_before_state_retries_once_then_succeeds(self):
        service = GolfService.__new__(GolfService)
        calls = {"extract": 0, "nav": 0}

        def extract(_page):
            calls["extract"] += 1
            if calls["extract"] == 1:
                raise RuntimeError("boom")
            return [{"reservation_no": "111"}, {"reservation_no": "222"}]

        service._extract_reservations = extract
        service._nav_to_glf109a = lambda _page: calls.__setitem__("nav", calls["nav"] + 1)

        result = service._capture_before_state(object(), "walter")

        self.assertEqual(result, {"111", "222"})
        self.assertEqual(calls["nav"], 1)
        self.assertEqual(calls["extract"], 2)

    def test_before_state_gives_up_after_second_failure(self):
        service = GolfService.__new__(GolfService)
        calls = {"extract": 0}

        def extract(_page):
            calls["extract"] += 1
            raise RuntimeError("boom")

        service._extract_reservations = extract
        service._nav_to_glf109a = lambda _page: None

        result = service._capture_before_state(object(), "walter")

        self.assertIsNone(result)
        self.assertEqual(calls["extract"], 2)

    def test_before_state_gives_up_when_renavigation_fails(self):
        service = GolfService.__new__(GolfService)
        calls = {"extract": 0}

        def extract(_page):
            calls["extract"] += 1
            raise RuntimeError("boom")

        def nav(_page):
            raise RuntimeError("nav boom")

        service._extract_reservations = extract
        service._nav_to_glf109a = nav

        result = service._capture_before_state(object(), "walter")

        self.assertIsNone(result)
        self.assertEqual(calls["extract"], 1)


if __name__ == "__main__":
    unittest.main()
