"""Unit tests for scripts/update_course_locations.py.

Loaded as a module via importlib from the scripts directory (it is not a
package member of the app), and fetch_pages() is never called for real -
every test patches it or passes fixture pages directly to refresh()/
discover_new().
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "update_course_locations.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("update_course_locations", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ucl = _load_script()


class UpdateCourseLocationsTests(unittest.TestCase):
    def setUp(self):
        self.existing_row = {
            "id": "silver-lake-executive", "name": "Silver Lake", "course_type": "Executive",
            "region": "Central", "band": "Between the 6s", "area": "Fenney",
            "address": "123 Old Address", "zip": "32163", "latitude": 28.85, "longitude": -82.0,
            "phone": "352-555-0000", "level": None, "holes": 9, "par": 27,
            "course_url": "https://www.thevillages.com/golf/silver-lake-executive/",
            "scorecard_url": None, "location_accuracy": "address",
            "location_source": ucl.SOURCE_DESCRIPTION, "updated_at": "2025-01-01T00:00:00",
            "aliases": ["Silver Lk"],
        }

    def _page(self, title, slug, **acf_overrides):
        acf = {
            "latitude": "28.86", "longitude": "-82.01", "village": "Village of Fenney",
            "address": "456 New Address", "phone": "352-555-1111", "level": None,
            "holes": 9, "par": 27,
        }
        acf.update(acf_overrides)
        return {
            "title": {"rendered": title}, "slug": slug, "acf": acf,
            "link": f"https://www.thevillages.com/golf/{slug}/",
            "modified": "2026-01-01T00:00:00",
        }

    def test_refresh_keeps_id_region_band_and_aliases_when_slug_changes(self):
        page = self._page("Silver Lake", "silver-lake-executive-renamed")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            refreshed = ucl.refresh([self.existing_row], [page])
        self.assertEqual(len(refreshed), 1)
        row = refreshed[0]
        self.assertEqual(row["id"], "silver-lake-executive")
        self.assertEqual(row["region"], "Central")
        self.assertEqual(row["band"], "Between the 6s")
        self.assertEqual(row["aliases"], ["Silver Lk"])
        # Values the feed does own are still refreshed even though id/region/
        # band/aliases are kept.
        self.assertEqual(row["address"], "456 New Address")
        self.assertIn(
            "warning: Silver Lake: slug is now silver-lake-executive-renamed, "
            "keeping id silver-lake-executive",
            buf.getvalue(),
        )

    def test_refresh_is_silent_when_slug_is_unchanged(self):
        page = self._page("Silver Lake", "silver-lake-executive")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ucl.refresh([self.existing_row], [page])
        self.assertEqual(buf.getvalue(), "")

    def test_discover_new_reports_and_returns_only_located_pages(self):
        located = self._page("Beautyberry", "beautyberry")
        unlocated = self._page("The Palms", "the-palms-championship", latitude="", longitude="")
        already_known = self._page("Silver Lake", "silver-lake-executive")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            candidates = ucl.discover_new([self.existing_row], [located, unlocated, already_known])
        self.assertEqual([c["name"] for c in candidates], ["Beautyberry"])
        candidate = candidates[0]
        self.assertEqual(candidate["id"], "beautyberry")
        self.assertEqual(candidate["band"], None)
        self.assertEqual(candidate["aliases"], [])
        self.assertEqual(candidate["region"], ucl.region_for_latitude(28.86))
        self.assertIsInstance(candidate["latitude"], float)
        self.assertIsInstance(candidate["longitude"], float)
        self.assertIn("skipped: The Palms has no coordinates", buf.getvalue())
        self.assertNotIn("Beautyberry has no coordinates", buf.getvalue())

    def test_main_without_add_new_does_not_write_new_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "course_locations.json"
            catalog_path.write_text(json.dumps([self.existing_row]), encoding="utf-8")
            located = self._page("Beautyberry", "beautyberry")
            already_known = self._page("Silver Lake", "silver-lake-executive")
            with patch.object(ucl, "CATALOG_PATH", catalog_path), \
                 patch.object(ucl, "fetch_pages", return_value=[located, already_known]) as fetch, \
                 patch.object(sys, "argv", ["update_course_locations.py"]):
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    exit_code = ucl.main()
            fetch.assert_called_once()
            self.assertEqual(exit_code, 0)
            written = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0]["id"], "silver-lake-executive")
            self.assertIn("unmatched course page: Beautyberry (beautyberry)", buf.getvalue())

    def test_main_with_add_new_appends_discovered_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "course_locations.json"
            catalog_path.write_text(json.dumps([self.existing_row]), encoding="utf-8")
            located = self._page("Beautyberry", "beautyberry")
            already_known = self._page("Silver Lake", "silver-lake-executive")
            with patch.object(ucl, "CATALOG_PATH", catalog_path), \
                 patch.object(ucl, "fetch_pages", return_value=[located, already_known]), \
                 patch.object(sys, "argv", ["update_course_locations.py", "--add-new"]):
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    exit_code = ucl.main()
            self.assertEqual(exit_code, 0)
            written = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(len(written), 2)
            self.assertEqual({row["id"] for row in written}, {"silver-lake-executive", "beautyberry"})
            self.assertIn("added: Beautyberry; assign a band by hand before shipping", buf.getvalue())

    def test_choose_page_prefers_exact_title_over_prefix(self):
        glenview = self._page("Glenview", "glenview")
        glenview_champions = self._page("Glenview Champions", "glenview-championship")
        existing_names = {"glenview", "glenview champions"}

        for pages in ([glenview, glenview_champions], [glenview_champions, glenview]):
            with self.subTest(order=[p["slug"] for p in pages]):
                resolved = ucl.choose_page("Glenview Champions", pages, existing_names)
                self.assertEqual(resolved["slug"], "glenview-championship")

                resolved_short = ucl.choose_page("Glenview", pages, existing_names)
                self.assertEqual(resolved_short["slug"], "glenview")

    def test_discover_new_skips_alias_and_id_collisions_with_stderr(self):
        existing = [dict(
            self.existing_row, id="hacienda-hills-championship", name="Hacienda Hills",
            aliases=["The Palms", "The Lakes"],
        )]
        alias_page = self._page("The Palms", "the-palms-championship")
        id_page = self._page("Hacienda Renamed", "hacienda-hills-championship")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            candidates = ucl.discover_new(existing, [alias_page, id_page])
        self.assertEqual(candidates, [])
        output = buf.getvalue()
        self.assertIn("skipped: The Palms matches alias of Hacienda Hills", output)
        self.assertIn(
            "skipped: Hacienda Renamed slug hacienda-hills-championship already used by Hacienda Hills",
            output,
        )

    def test_add_new_refuses_duplicate_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "course_locations.json"
            catalog_path.write_text(json.dumps([self.existing_row]), encoding="utf-8")
            same_page = self._page("Silver Lake", "silver-lake-executive")
            duplicate_candidate = dict(self.existing_row, name="Silver Lake Duplicate")
            with patch.object(ucl, "CATALOG_PATH", catalog_path), \
                 patch.object(ucl, "fetch_pages", return_value=[same_page]), \
                 patch.object(ucl, "discover_new", return_value=[duplicate_candidate]), \
                 patch.object(sys, "argv", ["update_course_locations.py", "--add-new"]):
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    exit_code = ucl.main()
            self.assertEqual(exit_code, 0)
            written = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0]["id"], "silver-lake-executive")
            self.assertIn(
                "refused: Silver Lake Duplicate (silver-lake-executive) already exists in the catalog",
                buf.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
