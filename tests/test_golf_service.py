import re
import shutil
import subprocess
import types
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


class SlowConfirmationPage(WrongDeleteConfirmationPage):
    """The confirmation page appears only after the first click's navigation,
    so a reader that does not wait for it sees the reservations list."""

    def __init__(self):
        super().__init__()
        self.confirmation_shown = False
        self.waited_states = []

    def locator(self, selector, **kwargs):
        if selector == "text=Delete Reservation No. 123456":
            page = self

            class ConfirmationText(DeleteLocator):
                def wait_for(self, **kwargs):
                    page.waited_states.append(kwargs.get("state", "visible"))
                    page.confirmation_shown = kwargs.get("state") != "hidden"

            return ConfirmationText()
        return super().locator(selector, **kwargs)

    def inner_text(self, _selector):
        if self.confirmation_shown:
            return "Delete Reservation No. 123456\nDELETE ALL PLAYERS ON THIS RESERVATION"
        return "Reservation No. 123456\nDELETE ALL PLAYERS ON THIS RESERVATION"

    def once(self, *_args):
        pass


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


# glf109c as the Villages site returns it when it sends the golfer form back
# (captured October 25, 2026, with synthetic names and IDs).
GOLFER_FORM_BODY = (
    "The Villages Tee Time System\nPat Example\nYour ID is: 100001\n\nClick here for Help\n\n"
    "Play Date of Reservation:\nSelect a Play Date\n10/26/2026 - Monday\n10/27/2026 - Tuesday\n\n"
    "Course\nView Open Times by Course\nView Open Times by Play Time\nAmberwood\nYankee Clipper\n"
    "A Villages.net Account Owner(s) Must be one of the Players.\n\n"
    "Enter 3 Golfers, arranging them in the groups you wish:\n"
    "Golfer No.\tGuest?\tGolfer ID.\tName\tAdditional Information\n"
    "Golfer 1\tNo Yes\n--Select From List--\nLee Sample 100002\nPat Example 100001\n"
)
GOLFER_FORM_OPTIONS = [
    "Select a Play Date", "10/26/2026 - Monday", "10/27/2026 - Tuesday",
    "View Open Times by Course", "View Open Times by Play Time", "Amberwood", "Yankee Clipper",
    "--Select From List--", "Lee Sample 100002", "Pat Example 100001",
]
GOLFER_FORM_REPLY = {"golferForm": True, "options": GOLFER_FORM_OPTIONS, "rowMessages": []}
NO_FORM_REPLY = {"golferForm": False, "options": [], "rowMessages": []}
RECORDED_ROWS = object()  # the rows script answers with what the test typed


class GolferFormLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    def count(self):
        return 1 if self.selector in self.page.present else 0

    def _record(self, action, value=None):
        self.page.actions.append((self.selector, action, value))

    def fill(self, value):
        self._record("fill", value)
        match = re.fullmatch(r'input\[name="glfers(\d+)"\]', self.selector)
        if match:
            self.page.fields[int(match.group(1))] = value

    def check(self):
        self._record("check")
        match = re.fullmatch(r'input\[name="guest(\d+)"\]\[value="([NY])"\]', self.selector)
        if match:
            self.page.flags[int(match.group(1))] = match.group(2)

    def select_option(self, value):
        self._record("select_option", value)
        match = re.fullmatch(r'select\[name="buddy(\d+)"\]', self.selector)
        if match:
            self.page.fields[int(match.group(1))] = value

    def click(self, **_kwargs):
        self._record("click")
        if self.selector == 'input[value="Submit"]':
            self.page.submits += 1


class GolferFormPage:
    """A glf109c double that records every locator action. Which selectors
    exist is driven by `present`; the golfer rows and the page's guest flags
    (gst, "N" until a radio says otherwise) are tracked as the site's script
    would track them."""

    def __init__(self, rows=3, buddies=("100001", "100002"), body=GOLFER_FORM_BODY,
                 form_state=NO_FORM_REPLY, rows_state=RECORDED_ROWS):
        self.url = "https://example.test/glf109c"
        self.body = body
        self.form_state = form_state
        self.rows_state = rows_state
        self.present = {f'input[name="glfers{n}"]' for n in range(1, rows + 1)}
        self.present |= {
            f'select[name="buddy{n}"] option[value="{gid}"]'
            for n in range(1, rows + 1) for gid in buddies
        }
        self.actions = []
        self.waited = []
        self.fields = {}
        self.flags = {}
        self.submits = 0

    def locator(self, selector, **_kwargs):
        return GolferFormLocator(self, selector)

    def get_by_role(self, role, name=None, exact=False):
        return GolferFormLocator(self, f"role={role}:{name}")

    def wait_for_url(self, pattern, timeout=None):
        self.waited.append(pattern)

    def inner_text(self, _selector):
        return self.body

    def evaluate(self, script, *args):
        if script == golf_service._GOLFER_ROWS_STATE_JS:
            if self.rows_state is not RECORDED_ROWS:
                return self.rows_state
            count = args[0]
            return {"ids": [self.fields.get(n, "") for n in range(1, count + 1)],
                    "flags": [self.flags.get(n, "N") for n in range(1, count + 1)]}
        if script == golf_service._GOLFER_FORM_STATE_JS:
            return self.form_state
        return []  # the tee time table scrape finds no rows

    def guest_radios(self):
        return [selector for selector, action, _ in self.actions
                if action == "check" and selector.startswith('input[name="guest')]


class GolferSearchService(GolfService):
    """get_available_times against a GolferFormPage, with the login steps stubbed."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self._active = _Session()
        self._active.page = page
        self._active_user = "walter"

    def _get_or_create_session(self, _username):
        return None, self.page

    def _ensure_logged_in(self, *_args):
        pass

    def _nav_to_glf109a(self, _page):
        pass

    def _capture_before_state(self, _page, _username):
        return set()


def search_with_guests(page, ids, guest_count=0, has_guests=None, num_golfers=None):
    service = GolferSearchService(page)
    result = service.get_available_times(
        "walter", "pw", "pin", "20261026", "Monday, Oct 26", "Championship", "all",
        ids, len(ids) if num_golfers is None else num_golfers,
        guest_count > 0 if has_guests is None else has_guests,
        guest_count=guest_count,
    )
    return service, result


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

    def _buddy_list(self, header):
        options = [{"value": "", "text": "Select a golfer"},
                   {"value": "100002", "text": "Alice Able"},
                   {"value": "100001", "text": "Walter Example"}]
        select = types.SimpleNamespace(evaluate=lambda _script: options)
        page = types.SimpleNamespace(
            locator=lambda _selector: types.SimpleNamespace(all=lambda: [object(), object(), select]),
            inner_text=lambda _selector: VILLAGES_HEADER.replace("Your ID is: 100001", header),
        )

        class BuddyService(GolfService):
            def _get_or_create_session(self, _username):
                return None, page

            def _login(self, *_args):
                pass

            def _nav_to_glf109c(self, *_args, **_kwargs):
                pass

        return BuddyService().fetch_buddy_list("walter", "pw", "pin")

    def test_primary_golfer_comes_from_the_page_header_not_the_first_option(self):
        self.assertIn("Your ID is: 100001", VILLAGES_HEADER)
        self.assertEqual(golf_service._own_golfer_id(VILLAGES_HEADER), "100001")
        self.assertIsNone(golf_service._own_golfer_id("No header here"))
        result = self._buddy_list("Your ID is: 100001")
        self.assertEqual(result["primary"]["id"], "100001")
        self.assertEqual([b["id"] for b in result["buddies"]], ["100002", "100001"])
        missing = self._buddy_list("Your ID is: 100009")
        self.assertEqual(missing["primary"]["id"], "100009")
        self.assertEqual(missing["buddies"][0]["id"], "100009")
        with self.assertLogs("golf_service", "WARNING"):
            legacy = self._buddy_list("")
        self.assertEqual(legacy["primary"]["id"], "100002")

    def test_allocation_code_uses_the_nine_starting_hole(self):
        # Course cells as glf109e shows them (captured October 23, 2026).
        self.assertEqual(golf_service._allocation_code("Mallory Hill,\nVirginia/hole(10)", "10:08"), "100810")
        self.assertEqual(golf_service._allocation_code("Hacienda Hills,\nPalms/hole(19)", "07:36"), "073619")
        self.assertEqual(golf_service._allocation_code("Tierra Del Sol,\nFront Nine/hole(1)", "01:00"), "010001")
        self.assertEqual(golf_service._allocation_code(
            "Chula Vista (52)\nhole(1)\nLevel: 1 - Yardage: 1298-1507 yds", "07:47"), "074701")
        self.assertEqual(golf_service._allocation_code("Southern Oaks", "02:05"), "020501")
        self.assertEqual(golf_service._nine_name("Mallory Hill,\nVirginia/hole(10)"), "Virginia")
        self.assertEqual(golf_service._nine_name("Mallory Hill, Virginia/hole(10)"), "Virginia")
        self.assertEqual(golf_service._nine_name("Shallow Creek,\nhole(1)"), "")
        self.assertEqual(golf_service._nine_name("Chula Vista (52)\nhole(1)\nLevel: 1"), "")

    def _second_nine_page(self, allocation_rows):
        page = DeclinedReplyPage(VILLAGES_HEADER + "No reservation number here.")
        page.evaluated = []
        page.evaluate = lambda _script, args=None: page.evaluated.append(args)
        tee_row = FakeElement(attrs={"cells": [
            FakeElement(child=page.link),
            FakeElement("Mallory Hill,\nVirginia/hole(10)"),
            FakeElement("10:08"),
        ]})
        page.query_selector_all = lambda _selector, calls=iter([[tee_row], allocation_rows]): next(calls)
        context = {
            "user": "walter", "date_str": "20261025", "play_date": "2026-10-25",
            "date_label": "Sunday, Oct 25", "course_type": "Championship",
            "golfer_ids": ["100001", "100002", "100003", "100004"], "num_golfers": 4, "has_guests": False,
            "search_id": "search-1", "offered_times": [("Mallory Hill, - Virginia", "10:08")],
            "preexisting_reservation_ids": set(),
        }
        return page, context

    def _book_second_nine(self, page, context):
        return DeclinedReplyService(page, context).book_tee_time(
            "walter", "pw", "pin", "Mallory Hill, - Virginia", "10:08",
            expected_date="20261025", expected_golfer_ids=["100001", "100002", "100003", "100004"],
            expected_num_golfers=4, expected_course_type="Championship",
            expected_has_guests=False, search_id="search-1",
        )

    def test_second_nine_booking_allocates_its_own_row(self):
        first_nine = FakeElement("10:08 Lakes/hole 1 4", attrs={
            "hidden": FakeElement(attrs={"value": "100801 "}),
            "text_input": FakeElement(attrs={"name": "allo3"}),
        })
        virginia = FakeElement("10:08 Virginia/hole 1 4 8 Min - 18 holes straight", attrs={
            "hidden": FakeElement(attrs={"value": "100810 "}),
            "text_input": FakeElement(attrs={"name": "allo20"}),
        })
        page, context = self._second_nine_page([first_nine, virginia])
        with self.assertLogs("golf_service", "WARNING"):
            self._book_second_nine(page, context)
        self.assertEqual(page.evaluated, [{"rowNum": 20, "numGolfers": 4}])
        self.assertEqual(virginia.attrs["text_input"].filled, "4")
        self.assertIsNone(first_nine.attrs["text_input"].filled)

    def test_booking_refuses_when_its_nine_has_no_allocation_row(self):
        first_nine = FakeElement("10:08 Lakes/hole 1 4", attrs={
            "hidden": FakeElement(attrs={"value": "100801 "}),
            "text_input": FakeElement(attrs={"name": "allo3"}),
        })
        page, context = self._second_nine_page([first_nine])
        with self.assertLogs("golf_service", "WARNING") as logs:
            result = self._book_second_nine(page, context)
        self.assertFalse(result["success"])
        self.assertNotIn("unknown", result)
        self.assertEqual(page.evaluated, [])
        self.assertIsNone(first_nine.attrs["text_input"].filled)
        self.assertTrue(any("code=100810" in line and "100801" in line for line in logs.output))

    def test_booking_refuses_a_matching_code_that_names_another_nine(self):
        mislabeled = FakeElement("10:08 Lakes/hole 1 4", attrs={
            "hidden": FakeElement(attrs={"value": "100810 "}),
            "text_input": FakeElement(attrs={"name": "allo3"}),
        })
        page, context = self._second_nine_page([mislabeled])
        with self.assertLogs("golf_service", "WARNING"):
            result = self._book_second_nine(page, context)
        self.assertFalse(result["success"])
        self.assertEqual(page.evaluated, [])
        self.assertIsNone(mislabeled.attrs["text_input"].filled)

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

    def test_booking_success_reports_guest_count(self):
        context = {
            "date_str": "20260910", "play_date": "2026-09-10", "golfer_ids": ["100001", "100002"],
            "num_golfers": 2, "has_guests": True, "guest_count": 1, "search_id": "search-1",
        }
        service = GolfService()
        self.assertEqual(service._booking_success("812345", "Southern Oaks", "02:05", context)["guest_count"], 1)
        del context["guest_count"]
        self.assertEqual(service._booking_success("812345", "Southern Oaks", "02:05", context)["guest_count"], 0)

    def test_golfer_page_message_reads_the_line_above_the_golfer_table(self):
        message = golf_service._golfer_page_message(GOLFER_FORM_BODY, GOLFER_FORM_OPTIONS, [])
        self.assertEqual(message, "A Villages.net Account Owner(s) Must be one of the Players.")

    def test_golfer_page_message_is_empty_on_a_normal_golfer_page(self):
        body = GOLFER_FORM_BODY.replace("A Villages.net Account Owner(s) Must be one of the Players.\n", "")
        self.assertEqual(golf_service._golfer_page_message(body, GOLFER_FORM_OPTIONS, []), "")
        self.assertEqual(golf_service._golfer_page_message("", [], []), "")
        self.assertEqual(golf_service._golfer_page_message("No golfer table here\nJust text", [], []), "")

    def test_golfer_page_message_adds_row_notes(self):
        message = golf_service._golfer_page_message(
            GOLFER_FORM_BODY, GOLFER_FORM_OPTIONS, [" ", "Golfer 100002 already has a tee time"],
        )
        self.assertTrue(message.endswith("Golfer 100002 already has a tee time"))
        self.assertTrue(message.startswith("A Villages.net Account Owner(s)"))
        # A note the page already printed above the table is not repeated.
        again = golf_service._golfer_page_message(
            GOLFER_FORM_BODY, GOLFER_FORM_OPTIONS,
            ["A Villages.net  Account Owner(s) Must be one of the Players."],
        )
        self.assertEqual(again, "A Villages.net Account Owner(s) Must be one of the Players.")
        # Without the table heading, only the row notes remain.
        self.assertEqual(
            golf_service._golfer_page_message("Some page", [], ["Golfer 100002 already has a tee time"]),
            "Golfer 100002 already has a tee time",
        )

    def test_golfer_page_message_takes_at_most_three_lines_and_stops_at_an_option(self):
        body = "one\ntwo\nthree\nfour\nEnter 2 Golfers, arranging them:\n"
        self.assertEqual(golf_service._golfer_page_message(body, [], []), "two three four")
        body = "one\ntwo\nthree\nEnter 2 Golfers, arranging them:\n"
        self.assertEqual(golf_service._golfer_page_message(body, ["two\u00a0"], []), "three")
        self.assertEqual(len(golf_service._golfer_page_message("word " * 200 + "\nEnter 1 Golfers", [], [])), 300)

    def test_search_rejected_by_the_site_reports_its_message(self):
        page = GolferFormPage(form_state=GOLFER_FORM_REPLY)
        with self.assertLogs("golf_service", "WARNING") as logs:
            service, result = search_with_guests(page, ["100001", "100002", "100003"])
        self.assertFalse(result["success"])
        self.assertNotIn("times", result)
        self.assertIn("Account Owner(s) Must be one of the Players", result["error"])
        self.assertTrue(result["error"].startswith("The Villages site said: \u201c"))
        self.assertEqual(page.submits, 1)
        self.assertTrue(any("sent the golfer page back" in line for line in logs.output))
        self.assertIsNone(service._active, "A rejected search must not leave a bookable session.")

    def test_search_rejected_without_a_message_says_so(self):
        body = GOLFER_FORM_BODY.replace("A Villages.net Account Owner(s) Must be one of the Players.\n", "")
        page = GolferFormPage(body=body, form_state=GOLFER_FORM_REPLY)
        with self.assertLogs("golf_service", "WARNING"):
            _, result = search_with_guests(page, ["100001", "100002", "100003"])
        self.assertFalse(result["success"])
        self.assertEqual(
            result["error"],
            "The Villages site didn't accept these golfers. Check your group and search again.",
        )

    def test_search_accepted_by_the_site_returns_the_tee_time_list(self):
        page = GolferFormPage(rows=1, buddies=("100001",))
        service, result = search_with_guests(page, ["100001"])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["times"], [])
        self.assertEqual(page.waited[-1], "**/glf109e**")
        self.assertEqual(service._active.search_context["guest_count"], 0)

    def test_guest_rows_use_the_signed_in_golfers_id(self):
        page = GolferFormPage(rows=3)
        service, result = search_with_guests(page, ["100001", "100002"], guest_count=1)
        self.assertTrue(result["success"], result)
        self.assertIn(('table input[type="text"]', "fill", "3"), page.actions,
                      "glf109b must ask for the whole party, guests included.")
        self.assertIn(('input[value="Y"]', "check", None), page.actions)
        self.assertEqual(page.guest_radios(), [
            'input[name="guest1"][value="N"]',
            'input[name="guest2"][value="N"]',
            'input[name="guest3"][value="Y"]',
        ])
        self.assertEqual(page.fields, {1: "100001", 2: "100002", 3: "100001"})
        self.assertEqual(page.flags, {1: "N", 2: "N", 3: "Y"})
        self.assertIn(('select[name="playdate"]', "select_option", "20261026"), page.actions)
        self.assertIn(('select[name="reqcs"]', "select_option", "98"), page.actions)
        self.assertEqual(page.submits, 1)
        context = service._active.search_context
        self.assertEqual((context["num_golfers"], context["guest_count"], context["has_guests"]), (2, 1, True))
        self.assertEqual(context["golfer_ids"], ["100001", "100002"])

    def test_guests_require_the_signed_in_golfer_in_the_group(self):
        page = GolferFormPage(rows=2, buddies=("100001", "100002"))
        _, result = search_with_guests(page, ["100002"], guest_count=1)
        self.assertFalse(result["success"])
        self.assertIn("select yourself", result["error"])
        self.assertEqual(page.submits, 0)
        self.assertEqual(page.guest_radios(), [])

    def test_guests_refused_without_a_golfer_id_on_the_page(self):
        page = GolferFormPage(rows=2, body=GOLFER_FORM_BODY.replace("Your ID is: 100001\n", ""))
        _, result = search_with_guests(page, ["100001"], guest_count=1)
        self.assertFalse(result["success"])
        self.assertIn("couldn't read your golfer ID", result["error"])
        self.assertEqual(page.submits, 0)
        self.assertEqual(page.guest_radios(), [])

    def test_search_refuses_when_the_golfer_page_has_too_few_rows(self):
        page = GolferFormPage(rows=2)
        _, result = search_with_guests(page, ["100001", "100002"], guest_count=1)
        self.assertFalse(result["success"])
        self.assertIn("didn't match your group", result["error"])
        self.assertEqual(page.submits, 0)
        self.assertEqual(page.fields, {})

    def test_search_refuses_when_the_golfer_page_has_extra_rows(self):
        page = GolferFormPage(rows=4)
        _, result = search_with_guests(page, ["100001", "100002"], guest_count=1)
        self.assertFalse(result["success"])
        self.assertIn("didn't match your group", result["error"])
        self.assertEqual(page.submits, 0)

    def test_search_refuses_when_a_row_did_not_take(self):
        cases = {
            "wrong id": {"ids": ["100001", "999999", "100001"], "flags": ["N", "N", "Y"]},
            "blank id": {"ids": ["100001", "100002", ""], "flags": ["N", "N", "Y"]},
            "guest flag lost": {"ids": ["100001", "100002", "100001"], "flags": ["N", "N", "N"]},
            "no answer": None,
        }
        for label, state in cases.items():
            with self.subTest(label):
                page = GolferFormPage(rows=3, rows_state=state)
                with self.assertLogs("golf_service", "WARNING") as logs:
                    _, result = search_with_guests(page, ["100001", "100002"], guest_count=1)
                self.assertFalse(result["success"])
                self.assertIn("couldn't fill in every golfer", result["error"])
                self.assertEqual(page.submits, 0)
                self.assertTrue(any("did not take" in line for line in logs.output))
        page = GolferFormPage(rows=2, rows_state={"ids": ["100001", "100002"], "flags": ["N", "Y"]})
        with self.assertLogs("golf_service", "WARNING"):
            _, result = search_with_guests(page, ["100001", "100002"])
        self.assertFalse(result["success"], "A guest flag with no guests requested must stop the search.")
        self.assertEqual(page.submits, 0)

    def test_search_without_guests_never_touches_guest_radios(self):
        page = GolferFormPage(rows=2, buddies=("100001",))
        _, result = search_with_guests(page, ["100001", "100002"])
        self.assertTrue(result["success"], result)
        self.assertEqual(page.guest_radios(), [])
        self.assertIn(('input[value="N"]', "check", None), page.actions)
        self.assertNotIn(('input[value="Y"]', "check", None), page.actions)
        self.assertIn(('table input[type="text"]', "fill", "2"), page.actions)
        # Row one comes from the buddy list; row two is typed because the list lacks it.
        self.assertIn(('select[name="buddy1"]', "select_option", "100001"), page.actions)
        self.assertIn(('input[name="glfers2"]', "fill", "100002"), page.actions)
        self.assertEqual(page.fields, {1: "100001", 2: "100002"})
        self.assertEqual(page.submits, 1)

    def test_search_refuses_guest_counts_that_do_not_fit_the_group(self):
        cases = {
            "four golfers and a guest": (["100001", "100002", "100003", "100004"], 1, True, None),
            "three golfers and two guests": (["100001", "100002", "100003"], 2, True, None),
            "more than three guests": (["100001"], 4, True, None),
            "negative guests": (["100001"], -1, False, None),
            "a boolean count": (["100001"], True, True, None),
            "a text count": (["100001"], "1", True, None),
            "guests without a flag": (["100001"], 1, False, None),
            "a flag without guests": (["100001"], 0, True, None),
        }
        for label, (ids, guest_count, has_guests, num_golfers) in cases.items():
            with self.subTest(label):
                page = GolferFormPage(rows=4)
                service = GolferSearchService(page)
                result = service.get_available_times(
                    "walter", "pw", "pin", "20261026", "Monday", "Championship", "all",
                    ids, len(ids), has_guests, guest_count=guest_count,
                )
                self.assertFalse(result["success"])
                self.assertIn("Your guests changed", result["error"])
                self.assertEqual(page.actions, [], "Nothing may be typed or clicked for a group that cannot fit.")
        page = GolferFormPage(rows=2)
        result = GolferSearchService(page).get_available_times(
            "walter", "pw", "pin", "20261026", "Monday", "Championship", "all",
            ["100001"], 2, False,
        )
        self.assertFalse(result["success"])
        self.assertIn("Your golfers changed", result["error"])
        self.assertEqual(page.actions, [])

    def test_search_refuses_an_id_outside_the_villages_range(self):
        page = GolferFormPage(rows=2)
        _, result = search_with_guests(page, ["100001", "99999"])
        self.assertFalse(result["success"])
        self.assertIn("99999", result["error"])
        self.assertIn("isn't a valid Villages golfer ID", result["error"])
        self.assertEqual(page.actions, [], "Nothing may be typed or clicked, including the glf109b count.")
        self.assertEqual(page.submits, 0)
        for bad in ("100000", "999991", "1000010", "00100001", "abc123", "\u0661\u0660\u0660\u0660\u0660\u0661", ""):
            with self.subTest(bad):
                page = GolferFormPage(rows=2)
                _, result = search_with_guests(page, ["100001", bad or " 12 "])
                self.assertFalse(result["success"])
                self.assertIn("valid Villages golfer ID", result["error"])
                self.assertEqual(page.actions, [])
        # The range check comes before the guest checks, and the range ends are valid.
        for edge in ("100001", "999990"):
            page = GolferFormPage(rows=1, buddies=(edge,))
            _, result = search_with_guests(page, [edge])
            self.assertTrue(result["success"], (edge, result))

    def test_search_rejection_includes_row_notes(self):
        reply = dict(GOLFER_FORM_REPLY, rowMessages=["Golfer 100002 already has a tee time"])
        page = GolferFormPage(form_state=reply)
        with self.assertLogs("golf_service", "WARNING"):
            _, result = search_with_guests(page, ["100001", "100002", "100003"])
        self.assertFalse(result["success"])
        self.assertIn("Golfer 100002 already has a tee time", result["error"])
        self.assertIn("Account Owner(s) Must be one of the Players", result["error"])

    def test_guest_booking_allocates_the_whole_party(self):
        page = DeclinedReplyPage(VILLAGES_HEADER + "No reservation number here.")
        page.evaluated = []
        page.evaluate = lambda _script, args=None: page.evaluated.append(args)
        tee_row = FakeElement(attrs={"cells": [
            FakeElement(child=page.link),
            FakeElement("Southern Oaks"),
            FakeElement("02:05"),
        ]})
        page.query_selector_all = lambda _selector, calls=iter([[tee_row], [page.allocation]]): next(calls)
        context = {
            "user": "walter", "date_str": "20260910", "play_date": "2026-09-10",
            "date_label": "Thursday, Sep 10", "course_type": "Executive",
            "golfer_ids": ["100001", "100002"], "num_golfers": 2, "has_guests": True, "guest_count": 1,
            "search_id": "search-1", "offered_times": [("Southern Oaks", "02:05")],
            "preexisting_reservation_ids": set(),
        }
        with self.assertLogs("golf_service", "WARNING"):
            DeclinedReplyService(page, context).book_tee_time(
                "walter", "pw", "pin", "Southern Oaks", "02:05",
                expected_date="20260910", expected_golfer_ids=["100001", "100002"],
                expected_num_golfers=2, expected_course_type="Executive",
                expected_has_guests=True, search_id="search-1", expected_guest_count=1,
            )
        self.assertEqual(page.allocation.attrs["text_input"].filled, "3")
        self.assertEqual(page.evaluated, [{"rowNum": 2, "numGolfers": 3}])

    def test_booking_refuses_a_changed_guest_count(self):
        page = SubmitRaisesPage()
        context = {
            "user": "walter", "date_str": "20260910", "play_date": "2026-09-10",
            "date_label": "Thursday, Sep 10", "course_type": "Executive",
            "golfer_ids": ["100001"], "num_golfers": 1, "has_guests": False, "guest_count": 0,
            "search_id": "search-1", "offered_times": [("Southern Oaks", "02:05")],
            "preexisting_reservation_ids": set(),
        }
        for label, expected in (("one more guest", 1), ("text", "many"), ("unreadable", object())):
            with self.subTest(label):
                result = BookingBoundaryService(page, context).book_tee_time(
                    "walter", "pw", "pin", "Southern Oaks", "02:05",
                    expected_date="20260910", expected_golfer_ids=["100001"],
                    expected_num_golfers=1, expected_course_type="Executive",
                    search_id="search-1", expected_guest_count=expected,
                )
                self.assertFalse(result["success"])
                self.assertNotIn("unknown", result)
                self.assertIn("Your guests changed since you searched", result["error"])
                self.assertEqual(page.row_calls, 0, "No tee time row may be clicked.")
                self.assertFalse(page.link.clicked)

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

    def test_reservation_delete_waits_for_the_confirmation_page(self):
        page = SlowConfirmationPage()
        service = DeleteService(page)
        service._reconcile_reservation_absent = lambda *_args: (True, [])
        result = service.delete_reservation("walter", "pw", "pin", "123456")
        self.assertTrue(result["success"], result)
        self.assertTrue(page.final_delete_requested)
        self.assertEqual(page.waited_states, ["visible", "hidden"],
                         "Wait for the confirmation page, then for it to go away after the final click.")

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
