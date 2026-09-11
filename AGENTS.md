# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What This Is

A Flask + Playwright app that automates tee-time booking on The Villages golf system (thevillages.net). It drives a headless Chromium browser through a multi-step login and reservation flow. Each person registers once with their own Villages credentials and then signs in directly on each device they use.

## Running Locally

```bash
.venv/bin/python app.py
```

App runs on http://localhost:8080. Each golfer signs in with their TVN username + TVN password + golf PIN, and then stays signed in on that device via the Flask session cookie.

Profiles and preferences are stored in encrypted SQLite. Set a stable `SECRET_KEY` and `USER_DB_ENCRYPTION_KEY`; no Firestore or Google Cloud credentials are needed. For local HTTP set `SESSION_COOKIE_SECURE=0` and `TRUST_PROXY=0`.

To watch the browser during debugging, set `HEADLESS=false` in `.env` (also sets `slow_mo=600ms`).

## Project Structure

Core modules:

- **`app.py`** — Flask backend, authenticated APIs, serialized golf worker, persistent request-result notifications, and per-user mapping limits. User profiles live in encrypted SQLite via `user_store.py`.
- **`golf_service.py`** — All Playwright automation. `GolfService` class handles login + booking. Credentials are passed per-call (not stored at init) to support multiple users. Includes `fetch_buddy_list()` to scrape available golfers from glf109c. Time parsing helpers deal with the site's non-standard 12-hour format (hours 1–6 = PM, 7–12 = AM).
- **`templates/index.html`**, **`static/`** — Mobile-first website for booking, requests, and course discovery. Keep maps independent of the Playwright booking context.
- **`mapping_service.py`**, **`course_metadata.py`** — Sourced course catalog and bounded, read-only golf cart route provider. See `docs/MAPPING_SOURCES.md`.
- **`preferences.py`**, **`user_store.py`** — Validated account preferences, favorites, home pin, and presets inside the encrypted profile. Use transactional `update_user` for partial edits.
- **`request_watches.py`**, **`email_notifications.py`** — Persistent request checks and retried result notifications. Never infer a lottery assignment from date alone.
- **`tests/`** — Deterministic tests with synthetic data and mocked upstream calls. Run `python -m unittest discover -s tests -v`. Never run live diagnostics as automated tests.

## User Flow

1. New user → register: enters TVN username, password, golf PIN, and optional email → system logs in, scrapes buddy list from glf109c, stores the profile in encrypted SQLite
2. Returning user → login: enters TVN username + TVN password + golf PIN. All three must match the stored values.
3. Successful login/register sets a persistent Flask session cookie so the device stays signed in (~31 days)
4. Buddy list can be refreshed or extended by adding a golfer ID manually
5. There is no global "user picker" — each device only sees the account it has authenticated as

## The Villages Login Flow

The automation navigates these pages in order:

1. `thevillages.net` — TVN username + password login
2. Click **GOLF** link → `glf000` — golf PIN screen ("Enter Your Pin Number"), filled with user's golf PIN
3. `glf100` — golf home, click "Reservations-View Open Tee Times"
4. `glf109a` → `glf109b` → `glf109c` — reservation setup (num golfers, course type, date, golfer ID)
5. `glf109e` — tee time list (scraped for available times)
6. `glf109y` — golfer allocation (hidden input encodes slot as `HHMM01`)
7. `glf109g` — confirmation page (scrapes reservation number)

## Key Implementation Details

- All selected golfers must be placed in the upstream form; never silently omit one. Group size is 1–4.
- Every booking requires the `search_id` returned by its successful search plus matching date, golfers, course type, and guest setting. Changes invalidate results.
- Once a mutation may have been submitted, errors mean `unknown` until reconciled; never invite a blind retry or claim an unverified deletion.
- **`_SITE_SELECT_PLAY_TIME = "98"`** is the option value for "View by Play Time" on glf109c.
- **`_COURSE_TYPE_CODES`**: `{"Championship": "01", "Executive": "02"}`.
- The hidden booking input on glf109y is `HHMM01` (e.g., time `02:05` → `020501`).
- Time filter labels in `golf_service.py` (`_FILTER_*` constants) must stay in sync with the onclick strings in `index.html`. Includes `_FILTER_ALL = "all"` for "Any Time".
- Gunicorn uses one process and four HTTP threads. All Playwright objects stay on one dedicated worker thread. Keep the HTTP timeout above the 240-second worker wait (300 seconds recommended).
- `GolfService` keeps one shared headless browser with a small LRU pool of per-user browser contexts; a context is dropped and rebuilt when its login goes stale.
- Encrypted profile shape, keyed by TVN username: `tvn_password`, `golf_password`, `display_name`, `primary` (id/name/initials), `buddies`, `email`, `preferences`.
- Login rate limiting is an in-memory soft guardrail on the single VPS process; it resets after restart.

## API Routes

- `GET /api/session` — Return current session state (auth, username, profile)
- `POST /api/login-user` — TVN username + password + golf PIN login (rate limited: 5 attempts, 5min lockout per IP/user)
- `POST /api/register` — Register new user (login + scrape buddy list, write encrypted SQLite)
- `POST /api/refresh-buddies` — Re-scrape buddy list from Villages
- `POST /api/add-buddy` — Manually add a golfer by ID
- `POST /api/update-email` — Update notification email
- `POST /api/remove-user` — Delete the cached profile
- `POST /api/tee-times` — Fetch available tee times
- `GET /api/my-tee-times` — List the current user's reservations
- `POST /api/delete-reservation` — Delete a reservation
- `POST /api/book` — Book a tee time
- `POST /api/logout` — Clear session
- `GET /api/courses` — Sourced course catalog
- `GET/POST /api/preferences` — Account favorites, home, presets, and travel preferences
- `POST /api/course-route` — Golf cart route preview from an explicit origin to a catalog course
- Requests APIs: `/api/request-courses`, `/api/submit-request`, `/api/my-requests`, `/api/delete-request`

## Environment Variables

Defined in `.env` (gitignored). See `.env.example` for the full list. Key vars:
- `HEADLESS` — set to `false` to watch the browser locally
- `SECRET_KEY` — Flask session secret. **Set this once and never rotate it** — changing the value invalidates every device's login cookie. Auto-generated only as a last resort for local dev.
- `LOG_LEVEL` — logging level (default `INFO`)
- `USER_DB_PATH` — SQLite database path (default `users.db`; mount `/data` for Docker)
- `USER_DB_ENCRYPTION_KEY` — Stable Fernet key; preserve this across deploys and backups
- `DISABLE_REQUEST_WATCHER=1` — Required for isolated tests/previews that must not contact Villages or send notifications
