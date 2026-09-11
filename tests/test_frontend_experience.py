"""Offline regressions for the single-page website integration contract."""

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
EXPERIENCE_JS = (ROOT / "static" / "js" / "experience.js").read_text(encoding="utf-8")
EXPERIENCE_CSS = (ROOT / "static" / "css" / "experience.css").read_text(encoding="utf-8")


class _IdsAndAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.scripts = []
        self.stylesheets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            assert attrs["id"] not in self.ids, f"duplicate id {attrs['id']}"
            self.ids.add(attrs["id"])
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs.get("href"))


def _extract_function(source, name):
    """Return the source text of a top-level ``function name(...){...}`` block."""
    start = source.index(f"function {name}(")
    depth = 0
    started = False
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
            started = True
        elif char == "}":
            depth -= 1
            if started and depth == 0:
                return source[start:index + 1]
    raise ValueError(f"could not extract function {name}")


def _extract_const_statement(source, name):
    """Return the literal source text of a top-level ``const name = ...;`` statement."""
    match = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*[^;]+;", source)
    if not match:
        raise ValueError(f"could not extract const {name}")
    return match.group(0)


class FrontendExperienceTests(unittest.TestCase):
    def test_course_experience_assets_and_controls_are_wired(self):
        parser = _IdsAndAssets()
        parser.feed(INDEX)
        required = {
            "tab-book", "tab-mytimes", "tab-request", "tab-courses",
            "view-courses", "course-map", "course-list", "course-overlay",
            "search-time-from", "search-time-to", "arrival-buffer", "preset-select",
        }
        self.assertLessEqual(required, parser.ids)
        self.assertIn("/static/js/experience.js", parser.scripts)
        self.assertIn("/static/css/experience.css", parser.stylesheets)
        self.assertTrue((ROOT / "static" / "js" / "experience.js").is_file())
        self.assertTrue((ROOT / "static" / "css" / "experience.css").is_file())

    def test_frontend_uses_separate_catalog_route_and_preference_apis(self):
        for endpoint in ("/api/courses", "/api/preferences", "/api/course-route"):
            self.assertIn(endpoint, EXPERIENCE_JS)
        route_section = EXPERIENCE_JS.split("async function requestCourseRoute", 1)[1].split(
            "function formatDistance", 1
        )[0]
        self.assertIn("/api/course-route", route_section)
        self.assertNotIn("/api/tee-times", route_section)
        self.assertNotIn("/api/book", route_section)

    def test_booking_guard_carries_search_context_and_unknown_results_verify(self):
        self.assertIn("search_id:EXPERIENCE.searchId", EXPERIENCE_JS)
        self.assertIn("JSON.stringify(teeSearchPayload())!==EXPERIENCE.searchSnapshot", EXPERIENCE_JS)
        self.assertIn("if(data.unknown) bookingUnconfirmed", EXPERIENCE_JS)
        self.assertIn("The delete result is unknown. Refreshing reservations to verify", EXPERIENCE_JS)
        self.assertIn("The cancellation result is unknown. Refreshing requests to verify", EXPERIENCE_JS)

    def test_javascript_parses_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        subprocess.run(
            [node, "--check", str(ROOT / "static" / "js" / "experience.js")],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_safe_number_treats_missing_values_as_null(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        source = _extract_function(EXPERIENCE_JS, "safeNumber")
        script = source + """
var results = [
  safeNumber(null),
  safeNumber(undefined),
  safeNumber(''),
  safeNumber(' '),
  safeNumber(true),
  safeNumber('3.5'),
];
var expected = [null, null, null, null, null, 3.5];
for (var i = 0; i < expected.length; i++) {
  if (results[i] !== expected[i]) {
    throw new Error('case ' + i + ': expected ' + expected[i] + ' got ' + results[i]);
  }
}
"""
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_search_payload_never_sends_null_time_window(self):
        source = _extract_function(EXPERIENCE_JS, "teeSearchPayload")
        self.assertIn("time_from:from||''", source)
        self.assertNotIn("||null", source)

    def test_current_location_explains_insecure_context_and_fallback(self):
        source = _extract_function(EXPERIENCE_JS, "useCurrentLocation")
        self.assertIn("isSecureContext", source)
        self.assertIn("secure (https) address", source)
        self.assertIn("Press and hold the map to save a starting location", source)
        self.assertIn("error.code===1", source)

    def test_find_course_consults_aliases(self):
        self.assertIn("aliases", _extract_function(EXPERIENCE_JS, "findCourse"))

    def test_region_bands_are_wired(self):
        for tile_id in ("r-all", "r-north-of-466", "r-between-the-6s", "r-south-of-466a", "r-south-of-turnpike"):
            self.assertIn(f'id="{tile_id}"', INDEX)
        self.assertIn("Between the 6s", INDEX)
        self.assertIn("c.band", EXPERIENCE_JS)
        self.assertNotIn("c.region", EXPERIENCE_JS)
        self.assertNotIn("course.region", EXPERIENCE_JS)

    def test_leaflet_is_vendored_and_loaded(self):
        vendor_dir = ROOT / "static" / "vendor" / "leaflet"
        for relative_path in ("leaflet.js", "leaflet.css", "images/marker-icon.png"):
            self.assertTrue((vendor_dir / relative_path).is_file(), f"missing {relative_path}")
        self.assertIn('href="/static/vendor/leaflet/leaflet.css"', INDEX)
        leaflet_script_pos = INDEX.index('/static/vendor/leaflet/leaflet.js')
        experience_script_pos = INDEX.index('/static/js/experience.js')
        self.assertLess(leaflet_script_pos, experience_script_pos)
        self.assertNotIn("/api/map-image", EXPERIENCE_JS)
        self.assertNotIn("map-basemap", EXPERIENCE_JS)

    def test_tile_math_matches_verified_tiles(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        const_names = (
            "VILLAGES_TILE_RES0", "VILLAGES_TILE_ORIGIN_LON",
            "VILLAGES_TILE_ORIGIN_LAT", "VILLAGES_TILE_SIZE",
        )
        source = "\n".join(_extract_const_statement(EXPERIENCE_JS, name) for name in const_names)
        source += "\n" + _extract_function(EXPERIENCE_JS, "tileResolution")
        source += "\n" + _extract_function(EXPERIENCE_JS, "tileForLatLng")
        script = source + """
var zoom5 = tileForLatLng(28.9099, -81.9932, 5);
if (zoom5.x !== 32628 || zoom5.y !== 38075) {
  throw new Error('zoom 5 tile mismatch: ' + JSON.stringify(zoom5));
}
var zoom8 = tileForLatLng(28.9099, -81.9932, 8);
if (zoom8.x !== 261028 || zoom8.y !== 304600) {
  throw new Error('zoom 8 tile mismatch: ' + JSON.stringify(zoom8));
}
var resolutionAtLevel3 = tileResolution(3);
var expectedResolution = VILLAGES_TILE_RES0 / 8;
if (Math.abs(resolutionAtLevel3 - expectedResolution) >= 1e-15) {
  throw new Error('resolution at level 3 mismatch: ' + resolutionAtLevel3 + ' vs ' + expectedResolution);
}
"""
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_course_map_uses_tile_layer_and_markers(self):
        self.assertIn("/tile/{z}/{y}/{x}", EXPERIENCE_JS)
        self.assertIn("L.tileLayer", EXPERIENCE_JS)
        self.assertIn("L.divIcon", EXPERIENCE_JS)
        self.assertIn("map-pin-dot", EXPERIENCE_JS)
        self.assertIn("pin-no", EXPERIENCE_JS)
        self.assertIn("contextmenu", EXPERIENCE_JS)
        self.assertIn("invalidateSize", EXPERIENCE_JS)
        self.assertIn("attributionControl:false", EXPERIENCE_JS)
        route_source = _extract_function(EXPERIENCE_JS, "renderRouteMap")
        self.assertIn("L.polyline", route_source)
        self.assertIn(".remove()", route_source)


if __name__ == "__main__":
    unittest.main()
