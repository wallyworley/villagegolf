# Upgrade review: Codex handoff merged with API research

Prepared September 9, 2026. This document merges three inputs into one list so you can decide what to upgrade before and after the next release:

1. The Codex review and partial implementation described in `CLAUDE_HANDOFF.md`.
2. A verification pass over the working tree: all 12 factual claims in the handoff held, and the unit suite passes 26 of 26.
3. A fresh diff review that found eight risks the handoff does not list, plus a five-agent survey of public API endpoints around The Villages.

Tags on each item show where it came from: `[Codex]` from the handoff, `[Review]` from the diff review, `[Research]` from the endpoint survey.

## State of the branch

| Item | Value |
|---|---|
| Branch | `feat/course-maps-and-booking-reliability` at `815f6d7` (same commit as `master`) |
| Uncommitted | 12 modified tracked files (+1245 / -422) and 10 new paths |
| Tests | 98 pass via `unittest discover -s tests` in `/tmp/villages-golf-upgrade-venv` (26 at the start of the review) |
| Not run | 8 pytest-style tests in `tests/test_mapping_service.py` and `tests/test_frontend_experience.py` |
| Deployed | Nothing. `DEPLOY.md` is stale. |

The `fix/30-day-hardening` branch is already merged into this commit, so the older docs under `docs/` are tracked history, not stray work.

## Tier 1: fix before any deploy

| # | Item | Where | Effort | Source |
|---|---|---|---|---|
| 1 | **Refresh golfer list is broken.** The new `validate_api_json` hook returns 400 for any `POST /api/*` without a JSON object body, and the refresh button posts with no body. Only `/api/logout` is exempt. | `app.py:567-576`, `static/js/experience.js:727` | S | Review, confirmed |
| 2 | **Catalog merge silently changes region and course type for 13 of 62 courses.** The checked-in catalog overwrites the curated dict. Havana and Palmer Legends move North to Central, Southern Oaks moves Central to South, Live Oak loses "Executive". The live tee-time filter uses `region`, so users lose courses from filters they know. | `course_metadata.py:459-475`, `data/course_locations.json` | S-M | Review, confirmed |
| 3 | **Route validation is half applied.** `route_to_course` never calls `_route_stops` and does not catch `_RoutePayloadError`; the view has no try/except. A malformed ArcGIS payload returns a 500. `_route_stops` also sorts rows before checking their shape. | `mapping_service.py:143-169, 234-288`, `app.py:705-728` | M | Codex #2, Review |
| 4 | **The course map background is invented.** `renderCourseMap` draws fictional roads and water and fixed North/Central/South labels that ignore the current bounds. The route view has no basemap. | `static/js/experience.js:249-254, 348-355` | M-L | Codex #1 |
| 5 | **Eight tests never run.** The mapping and frontend tests are free functions, invisible to unittest discovery. Convert to `TestCase` or adopt pytest, then add malformed, NaN, disconnected, far-snap, error, and cache cases for routing. | `tests/test_mapping_service.py`, `tests/test_frontend_experience.py` | S | Codex #3 |
| 6 | **`safeNumber(null)` returns 0** and course-name matching is substring first-match. Glenview Champions and Palmer Legends have empty alias lists. | `static/js/experience.js:34-37`, `course_metadata.py:443-456` | S | Codex #4 |
| 7 | **Deployment docs are stale.** `DEPLOY.md` says timeout 250; the Dockerfile says 300. No Playwright 1.62 install step, no `/data` volume, no rollback plan. | `DEPLOY.md:9`, `Dockerfile` | S | Codex #7 |
| 8 | **Browser QA and fixture review are unstarted.** The nine mocked golf-service tests cannot prove live selectors. Manual QA reached only the preview login page. | `golf_service.py`, `tests/preview_server.py` | M-L | Codex #5, #6 |


**Status on September 9, 2026 (late evening): items 1 through 7 are implemented and verified; item 8 is partly done.** The work ran as two spec-driven batches with adversarial review (six reviewer passes, 17 findings, all fixed or dispositioned). The suite grew from 26 to 73 tests, all passing in the isolated venv. A preview-fixture smoke test covered login, the course catalog with bands and aliases, the map image proxy, band filters, the bodiless refresh, routing from the region corner, and the booking guard. Still open in item 8: browser QA on real phones and desktop, and a review of the golf-service scraping diff against realistic HTML fixtures. Nothing has been committed or deployed.

## Tier 2: correctness risks for a fast follow-up

These can ship after Tier 1, but each one can produce a wrong result for a real user.

- **Duplicate booking after an unknown result** `[Review]`. A client timeout at 240 seconds does not stop the job on the worker, and nothing on the server prevents the same booking twice. A user who re-searches and re-books after `{unknown: true}` can hold two reservations. Add a per-user guard that blocks a new booking while a prior booking job is still running, or a short-lived key on username, date, time, and course. `app.py:315-319`. Effort M.
- **Booking reconciliation takes the first match** `[Review]`. `_find_booked_reservation` accepts the first new reservation on date, time, and course; `_reconcile_new_request` requires exactly one. Align booking with the stricter rule. `golf_service.py` around lines 683-706. Effort S.
- **A failed pre-search snapshot poisons the whole search** `[Review]`. If the glf109a snapshot throws, every booking under that `search_id` reports unknown, with no log line. Log it and retry the snapshot once. `golf_service.py` around lines 908-919. Effort S.
- **Cancelled watches block re-enrollment** `[Review]`. The fallback idempotency check matches on username and date without status. `request_watches.add_watch`. Effort S.
- **Catalog import failure breaks preferences** `[Review]`. `mapping_service` loads the catalog at import, and the preferences POST imports it to validate `course_id`. A missing file 500s three routes. Load lazily and return a readable error. Effort S.
- **Grace-period retries stack on the single worker** `[Review]`. Every unmatched watch rechecks every 15 minutes for 2 hours after the 1:30 AM lottery, sequentially on the same worker that serves live bookings. Add jitter and a cap. `request_watches.py:9-10`, `app.py:415-444`. Effort S.
- **Catalog refresh can silently rename course ids** `[Review]`. `scripts/update_course_locations.py` says it keeps stable ids, but `refresh()` overwrites `id` (and `region`) from the WordPress slug on every run. Favorites, presets, and saved routes key on `id`, so a slug change upstream would orphan them. Make `refresh()` keep the existing id when the course name matches, and warn on any id change. Effort S.
- **Two site courses are missing from the catalog** `[Research]`. The golf system's own course list (glf120b.html) names 65 courses; the catalog has 62. `Beautyberry` and `Honeysuckle` have no catalog entry, so they cannot be mapped, routed, or favorited. Run `scripts/update_course_locations.py` once thevillages.com publishes their pages, or add them by hand with coordinates. Effort S.
- **Booking confirmation email is fire-and-forget** `[Codex open question]`. Request results use the persisted outbox with retries; ordinary bookings still use a daemon thread that only logs on failure. Move bookings onto the outbox. `app.py:1223-1246`. Effort S-M.


**Status on September 10, 2026: Tier 2 is implemented and verified.** Every item below is done on the branch, still uncommitted: booking reconciliation requires exactly one match; the pre-search snapshot retries once and fails loudly; cancelled or finished watches no longer block re-enrollment; the catalog loads lazily and every catalog route returns a readable 503 when the file is missing; due watch checks are capped per pass with jitter; booking confirmations go through a durable outbox with atomic claims, stale-claim recovery, and retries; the refresh script keeps ids, matches by one rule in both directions, reports alias and id collisions, and can discover new courses behind `--add-new`; and Beautyberry and Honeysuckle (Long Prairie, south of the Turnpike) are in the catalog, which now has 64 courses. The duplicate-booking item was closed by the Tier 1 in-flight guard. Three review passes raised 14 findings, all fixed or dispositioned; the suite is at 98 tests. Operator note: the feed lists four unopened courses without coordinates (The Palms, Timberland, The Lakes, Meadow View); "The Palms" and "The Lakes" are also aliases of Hacienda Hills nines, so when those courses open the aliases must be resolved by hand before running `--add-new`.

**Owner test feedback on September 10, and what changed:** the tee-time search failed with "Invalid time from" because the page sent null for an empty time window and the JSON guard rejected it (fixed on both sides, two tests); the preview fixture now uses the real map and route services by default (`PREVIEW_OFFLINE_MAPS=1` for stubs) and accepts `PREVIEW_PORT`; the course map is interactive (zoom buttons, drag to pan, wheel and pinch zoom, double tap, pins that become dots when zoomed out and disappear off screen, number badges on the cards that match the pins); a browser-driven review found and fixed a native image drag hijacking the pan on a mouse and a missing pointer capture. The suite is at 104 tests.

**Evening of September 10: the map moved to tiles.** Phone testing showed the image-per-view map lagging on every pinch and drag. The Courses map and the route preview now run on Leaflet 1.9.4 (vendored under `static/vendor/leaflet`) with a custom CRS that reproduces The Villages tile cache exactly (`/tile/{z}/{y}/{x}`, WKID 4326, origin -400/400, level 0 at 0.0012182840349851034 degrees per pixel). Pins become numbers when 14 or fewer are in view. The `/api/map-image` proxy stays on the server but the page no longer uses it. 100 tests.

## Tier 3: upgrades the API research makes possible

The survey found no public tee-time API. The reservation system is in-house CGI on `gis.thevillages.net` (Apache 2.4.53 on IBM i), and the login is Classic ASP. Playwright against the CGI flow stays the only booking path. The findings below are the useful adjacent sources.

| Source | Gives you | Plug-in point | Effort | Recommendation |
|---|---|---|---|---|
| `gis.thevillages.net/glf120c.html` and `glf120b.html` (no auth, 60-second cache, verified live) | Every course's phone number and day-by-day closures for the current week: tournaments, ladies' day, full closures | New closures module with its own 60-second cache. Show "closed today" in the course directory and detail modal, and as an advisory on zero-result searches. Keep the live request-picker scrape as ground truth. | S-M | Yes, after release |
| `thevillages.com/wp-json/wp/v2/golf` and `arc7.thevillages.com` ArcGIS route solver (already integrated) | Official catalog and golf-cart routing on the multi-modal network, revision 2025-12-15 | Already in `course_metadata.py` and `mapping_service.py`. The catalog endpoint returns 100 items and about 10 MB, so keep the checked-in 62-course extract and the refresh script. Add the same "usage terms not confirmed" caveat for the catalog that `docs/MAPPING_SOURCES.md` already applies to routing. | S | Doc fix before release |
| `districtgov.org/golf/maintenance/` (static HTML) | Long-term renovations and reopening plans | Overlaps the weekly schedule above. Scrape only if the closures module earns its keep. | M | Later |
| `api.weather.gov/gridpoints/TBW/85,142/forecast/hourly` (no auth, verified) | Official hourly forecast resolved to The Villages | Open-Meteo already works in the app. Use NWS only as a fallback. | S | Optional |
| OpenGolfAPI (`api.opengolfapi.org`, ODbL) | Course GPS, par, phone | Redundant with the official catalog and adds share-alike overhead. | none | Skip |
| Classic ASP login (`login/easyloginNow.asp`, plain username and password, no CSRF token) | A possible requests-based login step | The glf pages still need the server-side session the login creates, so the saving is small. | M | Park |
| CourtReserve (pickleball) and ActiveNet (rec centers) | Documented vendor APIs | Keys are issued per organization and are not public. | none | Not reachable |

One correction to the earlier research summary: ArcGIS REST does exist on a Villages host. The survey checked only `gis.thevillages.net` and `districtgov.org`; Codex found `arc7.thevillages.com`. A separate `api.v2.thevillages.com` also exists and returns 401; do not try to reuse a frontend authorization constant against it.

## Decisions made on September 9, 2026

1. **Region semantics: Option A.** Four bands split by the real roads, stored as a `band` field per course and seeded from the catalog coordinates with village names as the tiebreaker: North of 466 (14 courses), Between the 6s (24), South of 466A (11), South of the Turnpike (13). The seven courses that leave the old "North" label are Hawkes Bay, Saddlebrook, Chula Vista, Mira Mesa, Belmont, Churchill Greens, and Pimlico. The old `region` field stays in the data but is no longer shown or filtered on.
2. **Map approach: real imagery from The Villages map service.** Server-side proxy with a six-hour cache, Mercator-fitted bounds shared by client and server so pins align, and a labeled pin-plot fallback when the image fails. One 800 by 600 export measured 106 KB in 0.6 seconds. No attribution work.
3. **Test runner: unittest.** Standard library, no new dependency, already the documented command. The eight pytest-style tests were converted.
4. **Booking dedup: refuse.** A second mutation for the same account is refused with HTTP 409 while one is in flight, and the guard is released only when the worker finishes the job, not when the HTTP call times out.
5. **Release scope.** Tier 1 in two batches (batch 1: hook fix, in-flight guard, route validation, alias resolution, test conversion, deploy doc; batch 2: bands and imagery), then Tier 2 as a fast follow-up.

## What the change set does well

Preserve these while fixing the items above.

- `user_store.update_user` uses `BEGIN IMMEDIATE` read-modify-write and never recreates a deleted profile mid-edit.
- `mapping_service.py` has a bounded LRU cache, a 10-second upstream timeout, NaN and infinity rejection, bounding-box checks, a cache lock, and a 2 MB response cap.
- `email_notifications.py` escapes every interpolated value.
- Mutation paths in `golf_service.py` track `submission_attempted` and reconcile against a freshly scraped page, returning `{unknown: true}` instead of guessing.
- `app.py` adds `MAX_CONTENT_LENGTH`, no-store headers, a constant-time compare fix, tighter booking input parsing, and a bounded worker queue.

## Verification notes

Commands used, none of which touched `.env`, `users.db`, or the live site:

```sh
PYTHONDONTWRITEBYTECODE=1 /tmp/villages-golf-upgrade-venv/bin/python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 /tmp/villages-golf-upgrade-venv/bin/python tests/preview_server.py
```

The preview fixture accepts the synthetic account with the field name `golf_pin`, not `pin` as the handoff says. Real bookings, cancellations, and emails were not exercised.
