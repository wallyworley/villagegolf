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

        def call(self, method, **kwargs):
            if method == "get_available_times":
                self.context = dict(kwargs)
                return {"success": True, "search_id": "preview-search", "date_label": kwargs["date_label"], "times": [
                    {"course": "Silver Lake", "course_name": "Silver Lake", "time": "08:15", "display_time": "8:15 AM", "available": "4"},
                    {"course": "Saddlebrook", "course_name": "Saddlebrook", "time": "09:30", "display_time": "9:30 AM", "available": "4"},
                    {"course": "Bonita Pass", "course_name": "Bonita Pass", "time": "01:15", "display_time": "1:15 PM", "available": "4"},
                ]}
            if method == "view_my_tee_times":
                return {"success": True, "verified": True, "reservations": list(reservations)}
            if method == "delete_reservation":
                reservations[:] = [r for r in reservations if r["reservation_no"] != kwargs["reservation_no"]]
                return {"success": True, "verified": True, "reservations": list(reservations)}
            if method == "book_tee_time":
                date = datetime.strptime(self.context["date_str"], "%Y%m%d").date()
                return {"success": True, "reservation_no": "654321", "course": kwargs["course_name"], "time": kwargs["time_str"],
                        "display_time": "8:15 AM", "play_date": date.isoformat(), "date": self.context["date_str"],
                        "date_label": self.context["date_label"], "golfer_ids": self.context["golfer_ids"],
                        "course_type": self.context["course_type"], "has_guests": self.context["has_guests"],
                        "num_golfers": self.context["num_golfers"]}
            if method == "fetch_request_courses":
                return {"success": True, "courses": ["Silver Lake", "Saddlebrook", "Bonita Pass"], "closed_courses": ["Bogart"]}
            if method == "submit_request":
                requests.append({"request_id": "777777", "name": "Preview request", "date": kwargs["play_date"], "action": "View / Cancel"})
                return {"success": True, "request_no": "777777", "message": "Preview request submitted"}
            if method == "view_my_requests":
                return {"success": True, "verified": True, "requests": list(requests)}
            if method == "delete_request":
                requests.clear()
                return {"success": True}
            if method == "fetch_buddy_list":
                profile = app.user_store.get_user("preview")
                return {"success": True, "primary": profile["primary"], "buddies": profile["buddies"]}
            return {"success": False, "error": "Operation unavailable in preview."}

    def fixture_route(origin, course_id):
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
    # use the synthetic stand-ins instead (no network at all).
    live_maps = os.environ.get("PREVIEW_OFFLINE_MAPS") != "1"
    if live_maps:
        print("Live maps: routes and basemap imagery come from the real map services; bookings and mail stay fake. Set PREVIEW_OFFLINE_MAPS=1 for synthetic stand-ins.", flush=True)
    else:
        print("PREVIEW_OFFLINE_MAPS=1: the basemap is a flat synthetic image and routes are straight lines.", flush=True)
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
