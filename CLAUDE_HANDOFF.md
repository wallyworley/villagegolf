# Village Fairways / villages-golf-app — Claude handoff

Prepared September 9, 2026. Walter asked to stop expensive work and hand this to
Claude because his Codex allowance is nearly exhausted. Smaller agents also hit
their usage limit. **Implementation is in progress, not release-ready.** Continue
from the working tree; do not redo the review or discard the changes.

## User intent and authorization

Walter requested a code review and an opinionated review of the existing options,
course-location help, and golf cart directions using The Villages' multi-modal
paths. He then authorized implementing the recommendations and using smaller
agents. He clarified that this should be a **website**, not a native iOS app.
Please combine the work below with Claude's own suggestions, prioritize correctness,
and finish integration before deployment. No real booking, cancellation, or email
was performed for testing.

## Workspace and status

- Repository: `/Users/walterworley/Documents/villages-golf-app`
- Branch: `feat/course-maps-and-booking-reliability`
- Baseline commit: `815f6d7`
- All changes remain uncommitted; production has not been updated.
- `.sf/` and `AGENTS.md` were already untracked when work began. Leave `.sf/`
  alone. `AGENTS.md` was intentionally updated to reflect SQLite and the new APIs.
- Do not overwrite unrelated edits, read secrets unnecessarily, or rotate the
  production session/encryption keys. The existing `.env` and real user database
  were not read or modified.
- Consult the repository instructions and Walter's Obsidian vault first. The
  vault hot cache and Skills Reference were checked; no golf-specific knowledge
  was found. This is ordinary Flask, not a Salesforce UI Bundle project.

## Recommended product direction

Keep the mobile website. Booking, course discovery, favorites, calendar export,
and cart route previews fit Safari/Chrome well and avoid separate iOS/Android
maintenance. An optional Add to Home Screen shortcut is enough initially.
Browser location needs HTTPS and an explicit user action/permission. Continuous
background GPS and spoken turn-by-turn navigation would be a separate project;
do not promise that in this release.

The most useful new feature is a real map and a searchable course directory with
the village/area, course type, address, favorites, and route access wherever a
course is shown. Course addresses are not yet verified cart entrances or starter
desks. Broad North/Central/South groupings are convenient but should not replace
actual village names or geographic context.

Existing options should remain: executive/championship, group and guests, time
filters, immediate tee times, advance requests, and reservation management.
Make “Any course” behavior clear, prevent silently dropping golfers, invalidate
results when selections change, and never invite a blind retry after a mutation
may already have succeeded.

## Verified mapping research — public sources

1. Official course collection (no authentication required):
   <https://www.thevillages.com/wp-json/wp/v2/golf?per_page=100>
   ACF metadata supplies course coordinates, address, phone, holes, par, level,
   and village. The checked-in catalog has **62 courses** with official page links.
   Refresh utility: `scripts/update_course_locations.py`.
2. Official cart routing metadata:
   <https://arc7.thevillages.com/arcgis/rest/services/GOLFCROUTEZ/NAServer/Route>
   The service describes the TV Mobile App Golf Car Routes network, revision
   **2025-12-15**. Public unauthenticated route solve was successfully tested:
   <https://arc7.thevillages.com/arcgis/rest/services/GOLFCROUTEZ/NAServer/Route/solve>
   It returned geometry, mileage, estimated driving time, and steps explicitly
   referring to a Multi-modal Trail. This is substantially better evidence than
   substituting Google driving or cycling directions for golf cart routing.
3. Solve parameters: `f=json`, `stops=lon,lat;lon,lat`, `outSR=4326`,
   `returnDirections=true`, `returnRoutes=true`, `returnStops=true`,
   `directionsLengthUnits=esriNAUMiles`,
   `directionsTimeAttributeName=Drivetime`, `ignoreInvalidLocations=false`.
   Public sample coordinates `-82.007446,28.938158;-81.999893,28.915922`
   returned approximately 2.697 miles, 8.88 minutes, and 152 geometry vertices.
4. Official basemap:
   <https://arc7.thevillages.com/arcgis/rest/services/PUBLICMAP26/MapServer>
   Metadata indicated an update on 2026-06-23. Map export can provide real map
   imagery. Some shared-use layers are polygons with partial coverage; they are
   not a complete routable path graph.
5. Official public pages: <https://www.thevillages.com/map/> and
   <https://www.thevillages.com/golf/>.
6. The separate VillagesGPS provider: <https://villagesgps.com/faq/>.
   No documented public integration API or supported destination deep link was
   found. Do not conflate that product with The Villages' own service.
7. `https://api.v2.thevillages.com/locations` returned 401. Do not use an
   authorization constant exposed in public frontend JavaScript to bypass it;
   the public WordPress source already solves course metadata needs.

Public accessibility is not a verified third-party usage agreement or SLA. Keep
attribution, network date, graceful failures, and route-preview wording. Confirm
provider usage terms before depending on this service at scale. Do not claim live
closures, guaranteed entrance accuracy, or verified accessibility of every path.
Additional source notes are in `docs/MAPPING_SOURCES.md`.

## Changes already implemented

### Booking and automation — `golf_service.py`

The original review found partial golfer-selection risk, brittle reservation
scraping, stale booking context, and ambiguous post-submit results. The agent
implemented the following, with nine fake-page regression tests:

- Require all requested golfers, unique numeric IDs, and complete party selection.
- Structurally parse reservations without inventing golfer IDs from phone numbers
  or reservation numbers; infer upcoming dates using Eastern time and weekday.
- Recognize valid list pages; an unexpected page is not an empty reservation list.
- Each successful search issues a UUID `search_id` and retains authoritative date,
  party, guests, course type, offered tee times, and preexisting reservation IDs.
- Booking requires the same complete context and verifies the offered course/time.
- After a possible commit, return `{success:false, unknown:true, error:...}` when
  success cannot be confirmed. Reconciliation requires a new exact reservation.
- Delete success requires a verified list proving the target is absent; reject a
  confirmation page for another reservation. Apply uncertainty handling to advance
  request submission/cancellation too.

Important service contract:

```text
get_available_times(...) -> success, times, search_id, date_label
book_tee_time(..., course_name, time_str,
  expected_date=None, expected_golfer_ids=None, expected_num_golfers=None,
  expected_course_type=None, expected_has_guests=None, search_id=None)
```

Successful booking returns authoritative `play_date` (ISO), `date` (YYYYMMDD),
`date_label`, `golfer_ids`, `num_golfers`, `has_guests`, `course_type`, `search_id`.
`view_my_tee_times` and `view_my_requests` return `verified:true` only on recognized
pages. These changes have not been verified against a live authenticated site.

### Backend, preferences, and notifications

- `app.py`: bounded worker queue, mutation timeout/exception uncertainty, validated
  JSON and dates/parties/times, complete booking context, exact time-window filters,
  no-store API responses, account resets, and authoritative confirmation-email data.
- Login/session/register include `username` and today's Eastern `play_date`.
- `user_store.py`: atomic profile updates so email/buddy/preference writes cannot
  overwrite each other or recreate an account removed during the operation.
- `preferences.py`: validated account-scoped favorites, optional saved origin,
  arrival buffer, maximum cart minutes, and saved group/search presets.
- `request_watches.py`: additive schema migration, idempotent enrollment, strict
  course/date/time matching, ambiguity rejection, grace period, and persisted
  notification retries. A matching reservation is described as a match, not proof
  of a lottery assignment. No reservation found does not automatically mean failure.
- `email_notifications.py`: escaped dynamic HTML and truthful result wording.
- Exact successful request cancellation cancels its notification watch.

New APIs (authenticated):

```text
GET  /api/courses             -> {success:true, courses:[...]}
GET  /api/preferences         -> {ok:true, preferences:{...}}
POST /api/preferences         -> same; accepts partial preference update
POST /api/course-route        <- {course_id, origin:{latitude,longitude}}
                              -> {success, geometry, miles, minutes, directions,
                                  course_id, source_url, network_updated,
                                  calculated_at, attribution}
```

Maps use a separate service and never navigate the stateful golf browser. The
route API is process-rate-limited (80 requests/user/minute). Saved home coordinates
are in the encrypted profile; transient origins use a small, short-lived memory
cache. Catalog IDs are official slugs, e.g. `silver-lake-executive`, not `silver-lake`.

### Website — `templates/index.html`, `static/css/experience.css`, `static/js/experience.js`

Code exists for a Courses tab, filtering/favorites, course detail modal, routes,
saved origin, arrival buffer, maximum cart time preference, presets, precise time
windows, hourly weather near tee time with refresh timestamp, calendar export,
booking-context invalidation, async response guards, and request-state reset.
**These are implemented drafts requiring integration/visual QA**, not a claim
that every feature is complete. The new script overrides several old inline
functions; inspect that arrangement and simplify if appropriate.

### Dependencies and operations

- Pins updated to Flask 3.1.3, Playwright 1.62.0, Gunicorn 26.2.0,
  python-dotenv 1.2.3, certifi 2026.7.22, cryptography 50.0.1.
- Matching Playwright Docker base `v1.62.0-noble`, non-root runtime, persistent
  `/data`, Docker secret/database exclusions, 300-second Gunicorn timeout.
- `.env.example`, README, and AGENTS documentation updated for encrypted SQLite.
- `test_fetch.py`/`test_menu.py` are now explicit `--live` diagnostics, excluded
  from accidental automated execution. They were not run against real accounts.
- `DEPLOY.md` still needs its release procedure updated for these changes.

## Highest-priority unfinished work — do this before release

1. **Replace the invented course-map background.** `renderCourseMap()` currently
   draws fictional SVG roads/water and fixed North/Central/South labels even when
   filtering changes bounds. It must use a real basemap with correctly aligned
   projection/bounds, or a plainly labeled geographic pin plot without invented
   features. The route SVG also needs a real basemap for orientation. The website
   agent was interrupted before implementing this fix.
2. **Finish partially applied routing hardening.** `mapping_service.py` now has
   stricter `_route_geometry`, `_route_summary`, `_route_directions`, and a new
   `_route_stops` validator, but `route_to_course` does not call `_route_stops` or
   catch `_RoutePayloadError` around parsing. Malformed payloads can cause a 500.
   `_route_stops` sorts rows before validating their shape. Finish defensive
   validation and readable error responses. Check provider stop Status, actual
   snap evidence/endpoints, and excessive snapping; proposed threshold was 0.25mi.
   Verify actual ArcGIS semantics rather than assuming stop geometry is snapped.
3. Convert `tests/test_mapping_service.py` and `tests/test_frontend_experience.py`
   from free pytest functions to unittest TestCases (or explicitly adopt pytest).
   **They are currently skipped by the documented unittest discovery command.**
   Add route malformed/NaN/disconnected/unlocated/far-snap/error/cache tests.
4. Frontend `safeNumber(null)` currently becomes zero; fix null/empty handling.
   Course-name resolution should use catalog aliases for Glenview Champions,
   Lopez Legacy, Palmer Legends, etc., rather than broad ambiguous substring guesses.
5. Finish desktop/mobile browser QA: map alignment and real imagery, selection,
   course-modal focus/scroll, favorites and account switching, saved pin/privacy,
   routing errors, stale search invalidation, unknown booking/delete verification,
   guest/group changes, advance-request flows, calendar date/time, and weather.
   Check that maximum cart time is actually used meaningfully, not merely saved.
   Root's latest browser check reached only the isolated preview login page.
6. Review the large automation diff against realistic sanitized HTML fixtures.
   Nine mocked tests are useful but cannot establish all live upstream selectors.
   Do not perform real booking/cancellation as a smoke test.
7. Update deployment instructions, make a controlled release/rollback plan, then
   deploy only after checks pass and access is restored. No partial deployment.

Further review questions: Are broad region bands useful to Walter or would named
villages/nearby town squares help more? Should preferred courses and groups apply
to both search and requests? Are cart entrances independently verified? Does the
mapping provider permit the expected third-party traffic? Is ordinary booking
confirmation email worth a durable outbox too? It still uses the older background
delivery path; persisted retries currently cover request-result notifications.
The rate limit is process-local, appropriate to the present single-worker VPS but
not a distributed defense. Full CSRF hardening and a broad frontend refactor were
not added in this pass; evaluate separately without losing the working behavior.

## Validation and safe preview

An isolated environment already exists at `/tmp/villages-golf-upgrade-venv` with
the updated requirements and pip-audit installed. The original repository `.venv`
was not upgraded. Latest executed regression suite: **26 passed** (11 API,
9 golf service, 6 request watches). This was before the final partial mapping
hardening edits; mapping/frontend pytest-style functions were not included.

```sh
PYTHONDONTWRITEBYTECODE=1 /tmp/villages-golf-upgrade-venv/bin/python -m unittest discover -s tests -v
```

Dependency audit reported **no known vulnerabilities** for the updated pins.
Result file: `/tmp/villages-golf-dependency-audit.json`. No need to repeat unless
dependencies change. New Playwright Chromium runtime still needs installation
and runtime verification on the deployment host.

Safe UI fixture:

```sh
PYTHONDONTWRITEBYTECODE=1 /tmp/villages-golf-upgrade-venv/bin/python tests/preview_server.py
```

Open <http://127.0.0.1:8091>. Synthetic login: **preview / preview / 1234**.
This fixture suppresses dotenv loading, uses a temporary encrypted database,
disables watchers, mocks mail/booking, and supplies deliberately synthetic route
geometry. Real course metadata is loaded. It cannot verify actual route accuracy.
It is a development fixture, not an alternate production configuration. The
fixture's booking response has a hardcoded display time and does not append a
reservation; improve those if testing subsequent reservation-list behavior.

## Deployment status

Existing public site: <https://villagefairways.com>.
Documented VPS: `root@40.160.233.235`, checkout `/opt/villages-golf-app`, Caddy,
systemd `villages-golf.service`, one Gunicorn worker/four threads. A read-only SSH
connection attempt timed out; nothing was changed remotely. Check whether the
address or required network has changed. Current DEPLOY.md says timeout 250;
new code/Docker guidance recommends 300. Keep one worker for the stateful browser
and watcher owner. Back up the SQLite database and stable encryption key securely;
do not print secrets, commit `.env`, or regenerate keys during deployment.

## Suggested first instruction to Claude

“Read CLAUDE_HANDOFF.md, inspect the existing uncommitted branch, and combine
these findings with your suggestions. Finish the listed release blockers first,
especially real map imagery, incomplete route validation, and missing test
discovery. Keep this a mobile website. Preserve the existing working booking
flow, verify with synthetic fixtures, and report what is ready before deployment.”
