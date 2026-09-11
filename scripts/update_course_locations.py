#!/usr/bin/env python3
"""Refresh the checked-in catalog from The Villages' public golf feed.

Usage: ``python scripts/update_course_locations.py [--dry-run] [--add-new]``

The feed is public course metadata only. This script never reads account
data. For every catalog row already on file, it matches the row's name to a
live golf page and refreshes course_type, area, address, coordinates, and
similar fields from that page, while keeping the row's existing id, region,
band, and aliases untouched (a warning goes to stderr when the matched
page's slug no longer agrees with the kept id). Golf pages that have no
matching catalog row are reported to stderr as unmatched and are not
written; pass --add-new to append them instead (each arrives with no band,
which must be assigned by hand before shipping).

The Palms and The Lakes are already carried as aliases on the
hacienda-hills-championship row (they are nines of Hacienda Hills), and the
feed separately lists two unopened courses under those same titles. Until
those courses open, a page titled "The Palms" or "The Lakes" is treated as
the existing Hacienda Hills alias and never added. When they do open, an
operator has to resolve that name clash by hand (move or drop the alias,
then add the new course) rather than expect --add-new to do it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "course_locations.json"
WP_API_URL = "https://www.thevillages.com/wp-json/wp/v2/golf"
SOURCE_DESCRIPTION = "The Villages official golf page (WordPress REST ACF latitude/longitude)"


def region_for_latitude(latitude: float) -> str:
    """Assign the catalog's broad display band from the published latitude."""

    return "North" if latitude >= 28.91 else ("Central" if latitude >= 28.82 else "South")


def fetch_pages() -> list[dict]:
    query = urlencode({"per_page": 100})
    request = Request(WP_API_URL + "?" + query, headers={"User-Agent": "VillagesGolfCatalog/1.0"})
    with urlopen(request, timeout=20) as response:
        pages = json.loads(response.read(12_000_000).decode("utf-8"))
    if not isinstance(pages, list):
        raise ValueError("WordPress response was not a list")
    return pages


def choose_page(name: str, pages: list[dict], existing_names: set[str]) -> dict:
    """Match a catalog row's name to one live golf page.

    Prefers an exact, case-insensitive title match. Falls back to the
    "title starts with name + ' '" prefix rule only when no page title
    exactly matches `name`, and only when the prefix-matched page's title
    is not itself the exact name of a different existing catalog row (so,
    for example, "Glenview" can never silently absorb the page that really
    belongs to "Glenview Champions"). Among the surviving candidates,
    prefers a slug ending in "-championship" or "-executive", or containing
    "pitch-putt".
    """
    lowered_name = name.casefold()
    exact_matches = []
    prefix_matches = []
    for page in pages:
        title = str((page.get("title") or {}).get("rendered") or "")
        acf = page.get("acf")
        if not (isinstance(acf, dict) and acf.get("latitude")):
            continue
        lowered_title = title.casefold()
        if lowered_title == lowered_name:
            exact_matches.append(page)
        elif lowered_title.startswith(lowered_name + " ") and lowered_title not in existing_names:
            prefix_matches.append(page)
    candidates = exact_matches or prefix_matches
    for page in candidates:
        slug = str(page.get("slug") or "")
        if slug.endswith("-championship") or slug.endswith("-executive") or "pitch-putt" in slug:
            return page
    if candidates:
        return candidates[0]
    raise KeyError(f"no public golf page found for {name}")


def refresh(existing: list[dict], pages: list[dict]) -> list[dict]:
    existing_names = {str(row.get("name") or "").strip().casefold() for row in existing}
    result = []
    for old in existing:
        name = old["name"]
        page = choose_page(name, pages, existing_names)
        acf = page["acf"]
        slug = str(page["slug"])
        old_id = old.get("id")
        if slug != old_id:
            print(
                f"warning: {name}: slug is now {slug}, keeping id {old_id}",
                file=sys.stderr,
            )
        course_type = "Pitch & Putt" if "pitch-putt" in slug else ("Championship" if "championship" in slug else "Executive")
        village = str(acf.get("village") or "").strip()
        area = village.removeprefix("Village of ").strip() or old.get("area")
        updated = dict(old)
        # `id`, `region`, `band`, and `aliases` are curated, owner-assigned
        # fields with no reliable source in the feed, so none of them are
        # part of the update() call below: they survive untouched from the
        # checked-in record.
        updated["id"] = old_id
        updated["region"] = old.get("region")
        updated["band"] = old.get("band")
        updated["aliases"] = list(old.get("aliases") or [])
        latitude = float(acf["latitude"])
        longitude = float(acf["longitude"])
        updated.update({
            "course_type": course_type,
            "area": area,
            "address": str(acf.get("address") or old.get("address") or "").strip(),
            "latitude": latitude,
            "longitude": longitude,
            "phone": acf.get("phone") or None,
            "level": acf.get("level") if acf.get("level") not in ("", None) else None,
            "holes": acf.get("holes"),
            "par": acf.get("par"),
            "course_url": page.get("link"),
            "location_accuracy": "address",
            "location_source": SOURCE_DESCRIPTION,
            "updated_at": page.get("modified") or page.get("date"),
        })
        result.append(updated)
    return result


def _name_matches_existing(title: str, existing: list[dict]) -> bool:
    lowered_title = title.casefold()
    for row in existing:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        lowered_name = name.casefold()
        if lowered_title == lowered_name or lowered_title.startswith(lowered_name + " "):
            return True
    return False


def _alias_owner(title: str, existing: list[dict]) -> str | None:
    lowered_title = title.casefold()
    for row in existing:
        for alias in row.get("aliases") or []:
            if lowered_title == str(alias).strip().casefold():
                return row.get("name")
    return None


def _id_owner(slug: str, existing: list[dict]) -> str | None:
    if not slug:
        return None
    for row in existing:
        if slug == str(row.get("id") or ""):
            return row.get("name")
    return None


def page_is_known(page: dict, existing: list[dict]) -> str | None:
    """Return why `page` is already covered by the catalog, or None if it
    looks like a genuinely new course page.

    "name" when the title equals an existing row's name or starts with
    "<name> " (the same signal choose_page matches a row to its page with);
    "alias" when the title equals one of an existing row's aliases, case-
    insensitively; "id" when the page's slug equals an existing row's id.
    Name is checked first: it is the strongest and most common signal, and
    a name match means refresh() already owns updating that row from this
    same page.
    """
    title = str((page.get("title") or {}).get("rendered") or "").strip()
    slug = str(page.get("slug") or "")
    if _name_matches_existing(title, existing):
        return "name"
    if _alias_owner(title, existing) is not None:
        return "alias"
    if _id_owner(slug, existing) is not None:
        return "id"
    return None


def discover_new(existing: list[dict], pages: list[dict]) -> list[dict]:
    """Return candidate catalog rows for golf pages not already in `existing`.

    A page counts as a golf-page candidate when its slug ends with
    '-championship' or '-executive', contains 'pitch-putt', or the page
    carries ACF latitude and longitude. page_is_known() then filters out
    anything already covered: a name match is skipped silently (refresh()
    owns updating that row); an alias or id match is reported to stderr and
    skipped, since it usually means an unopened nine of an existing course
    rather than a genuinely new one (see the module docstring about
    Hacienda Hills). A candidate with no finite coordinates is reported to
    stderr and not returned, since the catalog requires a usable location.
    """
    candidates = []
    for page in pages:
        slug = str(page.get("slug") or "")
        acf = page.get("acf") if isinstance(page.get("acf"), dict) else {}
        has_coords = bool(acf.get("latitude")) and bool(acf.get("longitude"))
        is_golf_page = (
            slug.endswith("-championship") or slug.endswith("-executive")
            or "pitch-putt" in slug or has_coords
        )
        if not is_golf_page:
            continue
        title = str((page.get("title") or {}).get("rendered") or "").strip()
        if not title:
            continue
        reason = page_is_known(page, existing)
        if reason == "name":
            continue
        if reason == "alias":
            print(f"skipped: {title} matches alias of {_alias_owner(title, existing)}", file=sys.stderr)
            continue
        if reason == "id":
            print(
                f"skipped: {title} slug {slug} already used by {_id_owner(slug, existing)}",
                file=sys.stderr,
            )
            continue
        try:
            latitude = float(acf.get("latitude"))
            longitude = float(acf.get("longitude"))
        except (TypeError, ValueError):
            latitude = longitude = float("nan")
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            print(f"skipped: {title} has no coordinates", file=sys.stderr)
            continue
        course_type = (
            "Pitch & Putt" if "pitch-putt" in slug
            else ("Championship" if slug.endswith("-championship") else "Executive")
        )
        village = str(acf.get("village") or "").strip()
        area = village.removeprefix("Village of ").strip()
        candidates.append({
            "id": slug,
            "name": title,
            "course_type": course_type,
            "region": region_for_latitude(latitude),
            "band": None,
            "area": area,
            "address": str(acf.get("address") or "").strip(),
            "zip": None,
            "latitude": latitude,
            "longitude": longitude,
            "phone": acf.get("phone") or None,
            "level": acf.get("level") if acf.get("level") not in ("", None) else None,
            "holes": acf.get("holes"),
            "par": acf.get("par"),
            "course_url": page.get("link"),
            "scorecard_url": None,
            "location_accuracy": "address",
            "location_source": SOURCE_DESCRIPTION,
            "updated_at": page.get("modified") or page.get("date"),
            "aliases": [],
        })
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="validate and print the refreshed count without writing")
    parser.add_argument(
        "--add-new", action="store_true",
        help="append discovered golf pages that have no matching catalog row",
    )
    args = parser.parse_args()
    existing = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    pages = fetch_pages()
    refreshed = refresh(existing, pages)
    candidates = discover_new(existing, pages)
    if args.add_new:
        existing_ids = {str(row.get("id") or "") for row in existing}
        for candidate in candidates:
            if candidate["id"] in existing_ids:
                print(
                    f"refused: {candidate['name']} ({candidate['id']}) already exists in the catalog",
                    file=sys.stderr,
                )
                continue
            print(f"added: {candidate['name']}; assign a band by hand before shipping", file=sys.stderr)
            refreshed.append(candidate)
    else:
        for candidate in candidates:
            print(f"unmatched course page: {candidate['name']} ({candidate['id']})", file=sys.stderr)
    if args.dry_run:
        print(f"validated {len(refreshed)} course records")
        return 0
    CATALOG_PATH.write_text(json.dumps(refreshed, indent=2) + "\n", encoding="utf-8")
    print(f"updated {CATALOG_PATH} ({len(refreshed)} course records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
