"""
Course metadata for The Villages golf courses.

lookup_course_metadata matches a scraped label against every canonical name
and alias as a whole word or phrase, and keeps the longest matching
candidate text (ties favor the canonical name over an alias, then the first
entry in dict order).
"""

import re

COURSE_METADATA = {
    "Belle Glade": {
        "course_type": "Championship",
        "address": "434 Moyer Loop",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Bonifay": {
        "course_type": "Championship",
        "address": "1033 Pinellas Place",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Cane Garden": {
        "course_type": "Championship",
        "address": "1728 Bailey Trail",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Evans Prairie": {
        "course_type": "Championship",
        "address": "1824 Evans Prairie Trail",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Glenview Champions": {
        "course_type": "Championship",
        "address": "3190 Glenview Rd",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Hacienda Hills": {
        "course_type": "Championship",
        "address": "1195 Morse Blvd",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Havana": {
        "course_type": "Championship",
        "address": "2488 O'Dell Circle",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Lopez Legacy": {
        "course_type": "Championship",
        "address": "17145 Buena Vista Blvd",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Mallory Hill": {
        "course_type": "Championship",
        "address": "1675 O'Dell Circle",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Orange Blossom Hills": {
        "course_type": "Championship",
        "address": "1548 Water Tower Circle",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Palmer Legends": {
        "course_type": "Championship",
        "address": "1645 Palmer Way",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Shallow Creek": {
        "course_type": "Championship",
        "address": "8007 Landstone Blvd",
        "zip": "33585",
        "region": "South",
        "aliases": [],
    },
    "Southern Oaks": {
        "course_type": "Championship",
        "address": "766 Marilee Place, Suite 110",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Tierra Del Sol": {
        "course_type": "Championship",
        "address": "806 San Marino Dr",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Woodlands": {
        "course_type": "Championship",
        "address": "1480 Craig Ct",
        "zip": "33585",
        "region": "South",
        "aliases": [],
    },
    "Amberwood": {
        "course_type": "Executive",
        "address": "7415 SE 172nd Legacy Ln",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Bacall": {
        "course_type": "Executive",
        "address": "2042 Canal St",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Belmont": {
        "course_type": "Executive",
        "address": "534 Belvedere Blvd",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Bogart": {
        "course_type": "Executive",
        "address": "2042 Canal St",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Bonita Pass": {
        "course_type": "Executive",
        "address": "2313 Buttonwood Run",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Briarwood": {
        "course_type": "Executive",
        "address": "8501 SE 172nd Legacy Ln",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Bellaire": {
        "course_type": "Executive",
        "address": "7028 Beebe Ct",
        "zip": "33585",
        "region": "South",
        "aliases": [],
    },
    "Chula Vista": {
        "course_type": "Executive",
        "address": "1019 Rio Grand Ave",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Churchill Greens": {
        "course_type": "Executive",
        "address": "534 Belvedere Blvd",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "De La Vista": {
        "course_type": "Executive",
        "address": "803 San Marino Blvd",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "El Diablo": {
        "course_type": "Executive",
        "address": "2381 Enrique Dr",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "El Santiago": {
        "course_type": "Executive",
        "address": "2381 Enrique Dr",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Escambia": {
        "course_type": "Executive",
        "address": "3020 Moyer Loop",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Gray Fox": {
        "course_type": "Executive",
        "address": "5674 Hummingbird Lane",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Hawkes Bay": {
        "course_type": "Executive",
        "address": "740 Buena Vista Blvd",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Heron": {
        "course_type": "Executive",
        "address": "1261 Sunset Point Blvd",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Hill Top": {
        "course_type": "Executive",
        "address": "1432 Water Tower Circle",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Laurel Oak": {
        "course_type": "Executive",
        "address": "6345 McNeill Dr",
        "zip": "33585",
        "region": "South",
        "aliases": [],
    },
    "Live Oak": {
        "course_type": "Executive / Pitch & Putt",
        "address": "6345 McNeill Dr",
        "zip": "33585",
        "region": "South",
        "aliases": [],
    },
    "Loblolly": {
        "course_type": "Executive",
        "address": "2197 Fenney Way",
        "zip": "32163",
        "region": "South",
        "aliases": [],
    },
    "Longleaf": {
        "course_type": "Executive",
        "address": "2217 Fenney Way",
        "zip": "32163",
        "region": "South",
        "aliases": [],
    },
    "Lowlands": {
        "course_type": "Executive",
        "address": "5197 Marsh Bend Trail",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Mangrove": {
        "course_type": "Executive",
        "address": "3198 Hendry Drive",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Marsh View": {
        "course_type": "Executive / Pitch & Putt",
        "address": "5223 Marsh Bend Trail",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Mickylee": {
        "course_type": "Executive / Pitch & Putt",
        "address": "6772 Meggison Road",
        "zip": "32163",
        "region": "South",
        "aliases": [],
    },
    "Mira Mesa": {
        "course_type": "Executive",
        "address": "998 Rio Grande Ave",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Oakleigh": {
        "course_type": "Executive",
        "address": "7415 SE 172nd Legacy Ln",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Okeechobee": {
        "course_type": "Executive",
        "address": "3020 Moyer Loop",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Palmetto": {
        "course_type": "Executive",
        "address": "3106 Hendry Drive",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Pelican": {
        "course_type": "Executive",
        "address": "1261 Sunset Point Blvd",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Pimlico": {
        "course_type": "Executive",
        "address": "534 Belvedere Blvd",
        "zip": "32162",
        "region": "North",
        "aliases": [],
    },
    "Redfish Run": {
        "course_type": "Executive",
        "address": "2376 Nobelton Lane",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Red Fox": {
        "course_type": "Executive",
        "address": "5674 Hummingbird Lane",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Richmond": {
        "course_type": "Executive / Pitch & Putt",
        "address": "4468 Burgess Drive",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Roosevelt": {
        "course_type": "Executive",
        "address": "2735 Canal St",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Saddlebrook": {
        "course_type": "Executive",
        "address": "2980 Saddlebrook Ln",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Sandhill": {
        "course_type": "Executive",
        "address": "2580 Buena Vista Blvd",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Sarasota": {
        "course_type": "Executive",
        "address": "2991 Morse Blvd",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Silver Lake": {
        "course_type": "Executive",
        "address": "679 Rainbow Blvd",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Southern Star": {
        "course_type": "Executive",
        "address": "2514 St. Charles Pl",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Sweetgum": {
        "course_type": "Executive",
        "address": "3198 Hendry Drive",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Tarpon Boil": {
        "course_type": "Executive",
        "address": "2376 Nobelton Lane",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Truman": {
        "course_type": "Executive",
        "address": "2735 Canal St",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Turtle Mound": {
        "course_type": "Executive",
        "address": "2605 Turtle Mound Path",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
    "Volusia": {
        "course_type": "Executive",
        "address": "128 Moyer Loop",
        "zip": "32163",
        "region": "Central",
        "aliases": [],
    },
    "Walnut Grove": {
        "course_type": "Executive",
        "address": "8501 SE 172nd Legacy Ln",
        "zip": "32159",
        "region": "North",
        "aliases": [],
    },
    "Yankee Clipper": {
        "course_type": "Executive",
        "address": "2514 St. Charles Pl",
        "zip": "32162",
        "region": "Central",
        "aliases": [],
    },
}


BANDS = (
    ("north-of-466", "North of 466"),
    ("between-the-6s", "Between the 6s"),
    ("south-of-466a", "South of 466A"),
    ("south-of-turnpike", "South of the Turnpike"),
)
BAND_KEYS = frozenset(key for key, _ in BANDS)
_BAND_KEY_BY_LABEL = {label.lower(): key for key, label in BANDS}


def band_key(label):
    """Return the filter key for a band label, or None when the label is unknown."""
    return _BAND_KEY_BY_LABEL.get(str(label or "").strip().lower())


_LOOKUP_CANDIDATES = None


def _lookup_candidates():
    """Compile the whole-word patterns for every canonical name and alias once.

    ``COURSE_METADATA`` is only mutated by the catalog merge at import time,
    which runs before the first lookup, so building the list lazily on first
    use sees the merged catalog.
    """
    global _LOOKUP_CANDIDATES
    if _LOOKUP_CANDIDATES is None:
        candidates = []
        for canonical_name, meta in COURSE_METADATA.items():
            texts = [(canonical_name, True)] + [(alias, False) for alias in meta.get("aliases", [])]
            for candidate_text, is_canonical in texts:
                pattern = re.compile(r"\b" + re.escape(candidate_text.lower()) + r"\b")
                candidates.append((pattern, (len(candidate_text), is_canonical), canonical_name, meta))
        _LOOKUP_CANDIDATES = candidates
    return _LOOKUP_CANDIDATES


def lookup_course_metadata(course_label):
    """Match a scraped course label to structured metadata.

    Matches every canonical name and alias as a whole word or phrase in the
    label and keeps the longest matching candidate: ties favor the canonical
    name over an alias, then the first entry in dict order.
    """
    label = (course_label or "").strip()
    if not label:
        return None, {}

    lower_label = label.lower()
    best = None  # (sort_key, canonical_name, meta)
    for pattern, sort_key, canonical_name, meta in _lookup_candidates():
        if not pattern.search(lower_label):
            continue
        if best is None or sort_key > best[0]:
            best = (sort_key, canonical_name, meta)
    if best is None:
        return None, {}
    return best[1], best[2]


# The scraper historically consumed ``COURSE_METADATA`` keyed by display name.
# Keep that API while sourcing the richer map catalog from one checked-in file.
# A source-free fallback remains above so importing the scraper still works if
# a packaged deployment accidentally omits the optional catalog file. The
# curated fallback carries no `band` entries: when the catalog file below is
# missing, lookup_course_metadata returns no band value for any course, so
# every band filter excludes those courses instead of guessing (fail closed).
try:
    import json
    from pathlib import Path

    _catalog_path = Path(__file__).resolve().parent / "data" / "course_locations.json"
    _catalog = json.loads(_catalog_path.read_text(encoding="utf-8"))
    for _course in _catalog:
        _meta = dict(_course)
        _meta["aliases"] = list(_course.get("aliases") or [])
        COURSE_METADATA[_course["name"]] = _meta
except (OSError, ValueError, TypeError, KeyError):
    # The literal metadata above is the compatibility fallback.
    pass
