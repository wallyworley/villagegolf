"""Isolated UI preview. No real credentials, reservations, or emails are used.

Run from the repository: python tests/preview_server.py
Sign in as preview / preview / 1234 at http://127.0.0.1:8091.
The course catalog is real; tee times and route responses are explicit fixtures.
"""
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet


def main():
    temporary = tempfile.TemporaryDirectory(prefix="villages-preview-")
    os.environ.update({
        "SECRET_KEY": "local-preview-only", "USER_DB_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "USER_DB_PATH": str(Path(temporary.name) / "preview.db"), "DISABLE_REQUEST_WATCHER": "1",
        "SESSION_COOKIE_SECURE": "0", "TRUST_PROXY": "0", "OPERATOR_ALERT_EMAIL": "",
    })
    with patch("dotenv.load_dotenv"):
        import app
    import mapping_service
    from golf_service import _display_time, _matches_filter, _to_real_hour
    app.send_booking_confirmation = lambda *args, **kwargs: True
    app.send_request_result = lambda *args, **kwargs: True
    app.send_email = lambda *args, **kwargs: True
    app.user_store.set_user("preview", {
        "tvn_password": "preview", "golf_password": "1234", "display_name": "Preview Golfer", "email": "",
        "primary": {"id": "111", "name": "Preview Golfer", "initials": "PG"},
        "buddies": [{"id": "111", "name": "Preview Golfer", "initials": "PG"},
                    {"id": "222", "name": "Preview Buddy", "initials": "PB"}],
    })
    tomorrow = datetime.now(ZoneInfo("America/New_York")).date() + timedelta(days=1)
    reservations = [{"reservation_no": "123456", "course": "Silver Lake", "play_date": tomorrow.isoformat(),
                     "date": tomorrow.strftime("%A: %-m/%-d"), "display_time": "8:15 AM", "time": "8:15 AM",
                     "actions": ["DELETE ALL PLAYERS ON THIS RESERVATION"], "golfer_ids": ["111", "222"]}]
    requests = []

    class FixtureWorker:
        def __init__(self):
            self.context = {}
            self.search_id = None
            self.times = []

        def call(self, method, **kwargs):
            if method == "get_available_times":
                self.context = dict(kwargs)
                self.context["guest_count"] = kwargs.get("guest_count", 0)
                self.search_id = "preview-" + uuid4().hex
                candidates = [
                    {"course": "Silver Lake", "course_name": "Silver Lake", "time": "08:15", "display_time": "8:15 AM", "available": "4"},
                    {"course": "Saddlebrook", "course_name": "Saddlebrook", "time": "09:30", "display_time": "9:30 AM", "available": "4"},
                    {"course": "Bonita Pass", "course_name": "Bonita Pass", "time": "01:15", "display_time": "1:15 PM", "available": "4"},
                ]
                if kwargs["course_type"] == "Championship":
                    for row, name in zip(candidates, ("Hacienda Hills", "Glenview Champions", "Belle Glade")):
                        row.update(course=name, course_name=name)
                def allowed(row):
                    hour, minute = map(int, row["time"].split(":"))
                    clock = f"{_to_real_hour(hour):02d}:{minute:02d}"
                    return (_matches_filter(row["time"], kwargs.get("time_filter"))
                            and (not kwargs.get("time_from") or clock >= kwargs["time_from"])
                            and (not kwargs.get("time_to") or clock <= kwargs["time_to"]))
                self.times = [row for row in candidates if allowed(row)]
                return {"success": True, "search_id": self.search_id, "date_label": kwargs["date_label"], "times": list(self.times)}
            if method == "view_my_tee_times":
                return {"success": True, "verified": True, "reservations": list(reservations)}
            if method == "delete_reservation":
                reservations[:] = [r for r in reservations if r["reservation_no"] != kwargs["reservation_no"]]
                return {"success": True, "verified": True, "reservations": list(reservations)}
            if method == "book_tee_time":
                matches = kwargs.get("search_id") == self.search_id and self.search_id is not None
                for expected, actual in (("expected_date", "date_str"), ("expected_golfer_ids", "golfer_ids"),
                                         ("expected_num_golfers", "num_golfers"), ("expected_course_type", "course_type"),
                                         ("expected_has_guests", "has_guests"),
                                         ("expected_guest_count", "guest_count")):
                    matches = matches and kwargs.get(expected) == self.context.get(actual)
                matches = matches and any(row["course_name"] == kwargs["course_name"] and row["time"] == kwargs["time_str"] for row in self.times)
                if not matches:
                    return {"success": False, "error": "Search again before booking this preview time."}
                self.search_id = None
                if os.environ.get("PREVIEW_BOOKING_UNKNOWN") == "1":
                    return {"success": False, "unknown": True, "error": "Preview: booking could not be verified."}
                date = datetime.strptime(self.context["date_str"], "%Y%m%d").date()
                result = {"success": True, "reservation_no": str(654321 + len(reservations)), "course": kwargs["course_name"], "time": kwargs["time_str"],
                        "display_time": _display_time(kwargs["time_str"]), "play_date": date.isoformat(), "date": self.context["date_str"],
                        "date_label": self.context["date_label"], "golfer_ids": self.context["golfer_ids"],
                        "course_type": self.context["course_type"], "has_guests": self.context["has_guests"],
                        "guest_count": self.context["guest_count"], "num_golfers": self.context["num_golfers"]}
                reservations.append({**result, "date": date.strftime("%A: %-m/%-d"),
                                     "actions": ["DELETE ALL PLAYERS ON THIS RESERVATION"]})
                return result
            if method == "fetch_request_courses":
                return {"success": True, "courses": ["Silver Lake", "Saddlebrook", "Bonita Pass"], "closed_courses": ["Bogart"]}
            if method == "submit_request":
                request_no = str(777777 + len(requests))
                requests.append({"request_id": request_no, "name": "Preview request", "date": kwargs["play_date"],
                                 "play_date": kwargs["play_date"], "course": ", ".join(kwargs["course_choices"]),
                                 "time": kwargs["time_to_play"], "action": "View / Cancel"})
                return {"success": True, "request_no": request_no, "message": "Preview request submitted"}
            if method == "view_my_requests":
                return {"success": True, "verified": True, "requests": list(requests)}
            if method == "delete_request":
                requests[:] = [r for r in requests if r["request_id"] != kwargs["target_request_id"]]
                return {"success": True}
            if method == "fetch_buddy_list":
                profile = app.user_store.get_user("preview")
                return {"success": True, "primary": profile["primary"], "buddies": profile["buddies"]}
            return {"success": False, "error": "Operation unavailable in preview."}

    def fixture_route(origin, course_id):
        if os.environ.get("PREVIEW_ROUTE_ERROR") == "1":
            return {"success": False, "error": "Preview: route service is temporarily unavailable."}
        course = mapping_service.get_course(course_id)
        if not course:
            return {"success": False, "error": "Course not found."}
        return {"success": True, "course_id": course_id, "miles": 3.1, "minutes": 14,
                "geometry": {"type": "LineString", "coordinates": [
                    [origin["longitude"], origin["latitude"]], [course["longitude"], course["latitude"]],
                ]}, "directions": [{"text": "Preview fixture — follow the multi-modal trail", "miles": 3.1, "minutes": 14}],
                "source_url": mapping_service.ROUTE_SOLVE_URL, "network_updated": "2025-12-15",
                "calculated_at": datetime.now(ZoneInfo("UTC")).isoformat(), "attribution": "Synthetic preview route"}

    worker = FixtureWorker()
    app.get_worker = lambda: worker
    # Maps and routes come from the real, public, read-only map services by
    # default so a tester sees the real thing; set PREVIEW_OFFLINE_MAPS=1 to
    # use synthetic backend stand-ins. Browser tiles and weather can still
    # use public network services; this setting only replaces backend mapping.
    live_maps = os.environ.get("PREVIEW_OFFLINE_MAPS") != "1"
    if live_maps:
        print("Live maps: routes and basemap imagery come from the real map services; bookings and mail stay fake. Set PREVIEW_OFFLINE_MAPS=1 for synthetic stand-ins.", flush=True)
    else:
        print("PREVIEW_OFFLINE_MAPS=1: synthetic straight-line routes and legacy map images; browser map tiles/weather may still use the network.", flush=True)
    if not live_maps:
        mapping_service.route_to_course = fixture_route
    def fixture_map_image(bounds, width, height):
        """Synthetic basemap: validate like production, then return a flat PNG of the requested size."""
        import struct
        import zlib
        clean, error = mapping_service._validate_map_request(bounds, width, height)
        if error is not None:
            return None, error, 400
        row = b"\x00" + bytes([222, 234, 214]) * width
        raw = row * height
        def chunk(kind, data):
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
        return png, None, 200

    if not live_maps:
        mapping_service.map_image = fixture_map_image
    print(f"Preview on http://{os.environ.get('PREVIEW_HOST', '127.0.0.1')}:{os.environ.get('PREVIEW_PORT', '8091')}", flush=True)
    print("SYNTHETIC PREVIEW ONLY — no real bookings or mail. Login: preview / preview / 1234", flush=True)
    try:
        port = int(os.environ.get("PREVIEW_PORT", "8091"))
        app.app.run(host=os.environ.get("PREVIEW_HOST", "127.0.0.1"), port=port, debug=False, use_reloader=False)
    finally:
        temporary.cleanup()


if __name__ == "__main__":
    main()
