import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import mapping_service

_SAMPLE_BOUNDS = {"min_lon": -82.02, "min_lat": 28.88, "max_lon": -81.96, "max_lat": 28.94}


def _payload():
    course = mapping_service.get_course("silver-lake-executive")
    last_vertex = [course["longitude"], course["latitude"]]
    return {
        "stops": {
            "features": [
                {"attributes": {"Sequence": 1, "Status": 0}, "geometry": {"x": -82.01, "y": 28.90}},
                {"attributes": {"Sequence": 2, "Status": 0},
                 "geometry": {"x": course["longitude"], "y": course["latitude"]}},
            ]
        },
        "routes": {
            "features": [{
                "attributes": {"Total_Length": 2.75},
                "geometry": {"paths": [[[-82.01, 28.90, 0], last_vertex + [1]]]}
            }]
        },
        "directions": [{
            "summary": {"totalLength": 2.75, "totalTime": 9.5},
            "features": [
                {"attributes": {"text": "Start", "length": 0, "time": 0}},
                {"attributes": {"text": "Turn right", "length": 1.2, "time": 4.25}},
            ],
        }],
    }


class MappingServiceTests(unittest.TestCase):
    def setUp(self):
        # Several tests below reuse the same origin/course as the success test;
        # clear the module-level route cache so a cached success from an
        # earlier test can never shadow the mocked payload this test installs.
        mapping_service._ROUTE_CACHE.clear()
        mapping_service._MAP_CACHE.clear()

    def test_catalog_has_stable_ids_and_published_locations(self):
        courses = mapping_service.get_courses()
        self.assertEqual(len(courses), 64)
        self.assertEqual(len({course["id"] for course in courses}), 64)
        self.assertGreaterEqual(
            {course["course_type"] for course in courses}, {"Championship", "Executive", "Pitch & Putt"}
        )
        self.assertTrue(all(course["course_url"].startswith("https://www.thevillages.com/") for course in courses))
        self.assertTrue(
            all(isinstance(course["latitude"], float) and isinstance(course["longitude"], float) for course in courses)
        )

    def test_missing_catalog_raises_catalog_unavailable_and_recovers(self):
        real_path = mapping_service.CATALOG_PATH

        def restore():
            mapping_service.CATALOG_PATH = real_path
            mapping_service._reset_catalog_cache()

        self.addCleanup(restore)
        with tempfile.TemporaryDirectory() as tmp:
            mapping_service.CATALOG_PATH = Path(tmp) / "missing.json"
            mapping_service._reset_catalog_cache()
            with self.assertRaises(mapping_service.CatalogUnavailable):
                mapping_service.get_courses()
        restore()
        courses = mapping_service.get_courses()
        self.assertGreaterEqual(len(courses), 62)

    def test_route_success_normalizes_geojson_and_directions(self):
        calls = []

        def fake_read(request):
            calls.append(request.full_url)
            return _payload()

        course = mapping_service.get_course("silver-lake-executive")
        with patch.object(mapping_service, "_read_upstream", fake_read):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")

        self.assertTrue(result["success"])
        self.assertEqual(result["course_id"], "silver-lake-executive")
        self.assertEqual(result["geometry"], {
            "type": "LineString",
            "coordinates": [[-82.01, 28.90], [course["longitude"], course["latitude"]]],
        })
        self.assertEqual(result["miles"], 2.75)
        self.assertEqual(result["minutes"], 9.5)
        self.assertEqual(result["directions"][1], {"text": "Turn right", "miles": 1.2, "minutes": 4.2})
        self.assertEqual(len(calls), 1)
        self.assertIn("stops=-82.01%2C28.9%3B", calls[0])

    def test_route_cache_avoids_repeating_upstream(self):
        calls = []
        with patch.object(mapping_service, "_read_upstream", lambda request: calls.append(request) or _payload()):
            origin = {"latitude": 28.901, "longitude": -82.011}
            first = mapping_service.route_to_course(origin, "silver-lake-executive")
            second = mapping_service.route_to_course(origin, "silver-lake-executive")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_route_rejects_unknown_or_out_of_region_origin(self):
        called = False

        def fail(request):
            nonlocal called
            called = True
            raise AssertionError("upstream should not be called")

        with patch.object(mapping_service, "_read_upstream", fail):
            self.assertFalse(
                mapping_service.route_to_course({"latitude": 40, "longitude": -82}, "silver-lake-executive")["success"]
            )
            self.assertFalse(mapping_service.route_to_course({"latitude": 28.9, "longitude": -82}, "missing")["success"])
        self.assertFalse(called)

    def test_route_missing_stops_is_rejected(self):
        payload = _payload()
        del payload["stops"]
        with patch.object(mapping_service, "_read_upstream", lambda request: payload):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result["success"])
        self.assertIn("usable stops", result["error"])

    def test_route_malformed_stop_rows_do_not_raise(self):
        payload = _payload()
        payload["stops"]["features"] = ["x", {"attributes": {"Sequence": "a"}}]
        with patch.object(mapping_service, "_read_upstream", lambda request: payload):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result["success"])
        self.assertIn("malformed stops", result["error"])

    def test_route_unlocated_stop_is_rejected(self):
        payload = _payload()
        payload["stops"]["features"][1]["attributes"]["Status"] = 1
        with patch.object(mapping_service, "_read_upstream", lambda request: payload):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result["success"])
        self.assertIn("could not locate", result["error"])

    def test_route_far_snap_is_rejected(self):
        payload = _payload()
        payload["routes"]["features"][0]["geometry"]["paths"][0][0][1] = 28.90 + 0.0145
        with patch.object(mapping_service, "_read_upstream", lambda request: payload):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result["success"])
        self.assertIn("0.25", result["error"])

    def test_route_malformed_geometry_is_rejected(self):
        payload = _payload()
        payload["routes"]["features"][0]["geometry"]["paths"] = "not-a-list"
        with patch.object(mapping_service, "_read_upstream", lambda request: payload):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result["success"])
        self.assertTrue("disconnected" in result["error"] or "invalid" in result["error"])

        payload2 = _payload()
        payload2["routes"]["features"][0] = "not-a-feature"
        with patch.object(mapping_service, "_read_upstream", lambda request: payload2):
            result2 = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result2["success"])
        self.assertEqual(result2["error"], "routing service returned an invalid route feature")

    def test_route_non_finite_coordinates_are_rejected(self):
        payload = _payload()
        payload["routes"]["features"][0]["geometry"]["paths"][0][0] = [float("nan"), 28.9]
        with patch.object(mapping_service, "_read_upstream", lambda request: payload):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result["success"])
        self.assertIn("non-finite", result["error"])

    def test_route_upstream_error_payload_is_reported(self):
        with patch.object(mapping_service, "_read_upstream", lambda request: {"error": {"message": "boom"}}):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "boom")

    def test_route_upstream_exception_is_reported(self):
        def raise_error(request):
            raise URLError("down")

        with patch.object(mapping_service, "_read_upstream", raise_error):
            result = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(result["success"])
        self.assertIn("unavailable", result["error"])

    def test_route_non_mapping_payload_is_reported(self):
        for bad_payload in (None, ["x"]):
            with self.subTest(payload=bad_payload):
                with patch.object(mapping_service, "_read_upstream", lambda request: bad_payload):
                    result = mapping_service.route_to_course(
                        {"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive"
                    )
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "routing service returned an unexpected response")

    def test_route_failure_is_not_cached(self):
        calls = []

        def fail_then_succeed(request):
            calls.append(request)
            if len(calls) == 1:
                return {"error": {"message": "boom"}}
            return _payload()

        with patch.object(mapping_service, "_read_upstream", fail_then_succeed):
            first = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
            second = mapping_service.route_to_course({"latitude": 28.90, "longitude": -82.01}, "silver-lake-executive")
        self.assertFalse(first["success"])
        self.assertTrue(second["success"])
        self.assertEqual(len(calls), 2)

    def test_catalog_has_bands_with_expected_counts(self):
        courses = mapping_service.get_courses()
        valid_labels = {"North of 466", "Between the 6s", "South of 466A", "South of the Turnpike"}
        self.assertTrue(all(course.get("band") in valid_labels for course in courses))
        counts = Counter(course["band"] for course in courses)
        self.assertEqual(counts["North of 466"], 14)
        self.assertEqual(counts["Between the 6s"], 24)
        self.assertEqual(counts["South of 466A"], 11)
        self.assertEqual(counts["South of the Turnpike"], 15)

    def test_fit_map_bounds_contains_input_and_matches_aspect(self):
        for width, height in ((800, 600), (1000, 700)):
            with self.subTest(width=width, height=height):
                result = mapping_service.fit_map_bounds(_SAMPLE_BOUNDS, width, height)
                self.assertLessEqual(result["min_lon"], _SAMPLE_BOUNDS["min_lon"])
                self.assertGreaterEqual(result["max_lon"], _SAMPLE_BOUNDS["max_lon"])
                self.assertLessEqual(result["min_lat"], _SAMPLE_BOUNDS["min_lat"])
                self.assertGreaterEqual(result["max_lat"], _SAMPLE_BOUNDS["max_lat"])
                aspect = mapping_service.map_aspect(result)
                self.assertLess(abs(aspect - width / height), 1e-9)

    def test_map_image_rejects_invalid_requests_without_upstream(self):
        def fail(_request):
            raise AssertionError("upstream should not be called")

        fitted = mapping_service.fit_map_bounds(_SAMPLE_BOUNDS, 800, 600)
        outside_villages = {"min_lon": -90.0, "min_lat": 28.88, "max_lon": -89.0, "max_lat": 28.94}
        inverted_latitude = {"min_lon": -82.02, "min_lat": 28.94, "max_lon": -81.96, "max_lat": 28.88}

        with patch.object(mapping_service, "_read_upstream_bytes", fail):
            _, _, status = mapping_service.map_image(fitted, 100, 600)
            self.assertEqual(status, 400)

            _, _, status = mapping_service.map_image(outside_villages, 800, 600)
            self.assertEqual(status, 400)

            _, _, status = mapping_service.map_image(inverted_latitude, 800, 600)
            self.assertEqual(status, 400)

            # Unfitted: its aspect does not match an 800x600 image.
            _, _, status = mapping_service.map_image(_SAMPLE_BOUNDS, 800, 600)
            self.assertEqual(status, 400)

    def test_map_image_returns_png_and_caches(self):
        calls = []
        payload = mapping_service._PNG_SIGNATURE + b"x"
        fitted = mapping_service.fit_map_bounds(_SAMPLE_BOUNDS, 800, 600)

        def fake_read(request):
            calls.append(request)
            return payload

        with patch.object(mapping_service, "_read_upstream_bytes", fake_read):
            first_bytes, first_error, first_status = mapping_service.map_image(fitted, 800, 600)
            second_bytes, second_error, second_status = mapping_service.map_image(fitted, 800, 600)

        self.assertEqual(len(calls), 1)
        self.assertEqual(first_bytes, payload)
        self.assertEqual(second_bytes, payload)
        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)

    def test_map_image_rejects_non_png_and_does_not_cache(self):
        calls = []
        fitted = mapping_service.fit_map_bounds(_SAMPLE_BOUNDS, 800, 600)

        with patch.object(
            mapping_service, "_read_upstream_bytes",
            lambda request: calls.append(request) or b"<html>",
        ):
            png_bytes, error, status = mapping_service.map_image(fitted, 800, 600)
        self.assertIsNone(png_bytes)
        self.assertEqual(status, 502)
        self.assertIsNotNone(error)

        payload = mapping_service._PNG_SIGNATURE + b"y"
        with patch.object(
            mapping_service, "_read_upstream_bytes",
            lambda request: calls.append(request) or payload,
        ):
            png_bytes2, error2, status2 = mapping_service.map_image(fitted, 800, 600)
        self.assertEqual(png_bytes2, payload)
        self.assertIsNone(error2)
        self.assertEqual(status2, 200)
        self.assertEqual(len(calls), 2)

    def test_map_image_reports_upstream_failure(self):
        fitted = mapping_service.fit_map_bounds(_SAMPLE_BOUNDS, 800, 600)

        def raise_error(_request):
            raise URLError("down")

        with patch.object(mapping_service, "_read_upstream_bytes", raise_error):
            png_bytes, error, status = mapping_service.map_image(fitted, 800, 600)
        self.assertIsNone(png_bytes)
        self.assertEqual(status, 502)
        self.assertIn("unavailable", error)

    def test_whole_catalog_map_fits_every_shipped_aspect(self):
        # Against the pre-fix _VILLAGES_BOUNDS check (longitude -82.20 to
        # -81.80), fitting the whole-catalog box to any of these shipped
        # aspects pushes longitude well past that window (down to roughly
        # -82.34 at the widest), so _validate_map_request would reject every
        # one of them and the Courses tab basemap would never render. This
        # test only passes once map bounds are checked against the wider
        # _MAP_REGION_BOUNDS instead.
        raw = mapping_service.catalog_map_bounds()
        for width, height in ((700, 483), (900, 429), (1200, 667), (1600, 1103), (1600, 762)):
            with self.subTest(width=width, height=height):
                fitted = mapping_service.fit_map_bounds(raw, width, height)
                self.assertLessEqual(fitted["min_lon"], raw["min_lon"])
                self.assertGreaterEqual(fitted["max_lon"], raw["max_lon"])
                self.assertLessEqual(fitted["min_lat"], raw["min_lat"])
                self.assertGreaterEqual(fitted["max_lat"], raw["max_lat"])
                clean_bounds, error = mapping_service._validate_map_request(fitted, width, height)
                self.assertIsNone(error)
                self.assertIsNotNone(clean_bounds)

    def test_fit_map_bounds_latitude_branch(self):
        bounds = {"min_lon": -82.10, "max_lon": -81.90, "min_lat": 28.90, "max_lat": 28.92}
        fitted = mapping_service.fit_map_bounds(bounds, 800, 600)
        self.assertEqual(fitted["min_lon"], bounds["min_lon"])
        self.assertEqual(fitted["max_lon"], bounds["max_lon"])
        self.assertLessEqual(fitted["min_lat"], bounds["min_lat"])
        self.assertGreaterEqual(fitted["max_lat"], bounds["max_lat"])
        self.assertLess(abs(mapping_service.map_aspect(fitted) - 800 / 600), 1e-9)

        # This bounds' raw aspect (~8.75) is above even a 1600x200 target
        # (8.0), so that size also takes the latitude-widening branch:
        # longitude is untouched and only latitude grows. (Verified
        # numerically; see the deviation ledger for the exact figures and why
        # this differs from a literal reading of the fix spec's wording.)
        wide = mapping_service.fit_map_bounds(bounds, 1600, 200)
        self.assertEqual(wide["min_lon"], bounds["min_lon"])
        self.assertEqual(wide["max_lon"], bounds["max_lon"])
        self.assertLess(wide["min_lat"], bounds["min_lat"])
        self.assertGreater(wide["max_lat"], bounds["max_lat"])

    def test_map_aspect_rejects_zero_span(self):
        zero_lat = {"min_lon": -82.0, "max_lon": -81.9, "min_lat": 28.9, "max_lat": 28.9}
        zero_lon = {"min_lon": -82.0, "max_lon": -82.0, "min_lat": 28.8, "max_lat": 28.9}
        for bad_bounds in (zero_lat, zero_lon):
            with self.subTest(bounds=bad_bounds):
                with self.assertRaises(ValueError):
                    mapping_service.map_aspect(bad_bounds)
                with self.assertRaises(ValueError):
                    mapping_service.fit_map_bounds(bad_bounds, 800, 600)


    def test_route_maps_from_any_origin_corner_fit_the_region(self):
        # Reviewer finding: a route from an origin at the edge of the origin
        # guard to a far course, padded 15 percent and fitted to the 1.8 route
        # aspect, must still be accepted by the map-region guard.
        guard = mapping_service._VILLAGES_BOUNDS
        corners = [(guard["min_lat"], guard["min_lon"]), (guard["min_lat"], guard["max_lon"]),
                   (guard["max_lat"], guard["min_lon"]), (guard["max_lat"], guard["max_lon"])]
        checked = 0
        for lat, lon in corners:
            for course in mapping_service.get_courses():
                lons = [lon, course["longitude"]]
                lats = [lat, course["latitude"]]
                lon_pad = max((max(lons) - min(lons)) * 0.15, 0.004)
                lat_pad = max((max(lats) - min(lats)) * 0.15, 0.004)
                raw = {"min_lon": min(lons) - lon_pad, "max_lon": max(lons) + lon_pad,
                       "min_lat": min(lats) - lat_pad, "max_lat": max(lats) + lat_pad}
                fitted = mapping_service.fit_map_bounds(raw, 1200, 667)
                clean, error = mapping_service._validate_map_request(fitted, 1200, 667)
                self.assertIsNone(error, f"{course['name']} from {lat},{lon}: {error}")
                self.assertIsNotNone(clean)
                checked += 1
        self.assertEqual(checked, 4 * 64)

if __name__ == "__main__":
    unittest.main()
