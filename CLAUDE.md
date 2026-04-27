# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Flask + Playwright app that automates tee-time booking on The Villages golf system (thevillages.net). It drives a headless Chromium browser through a multi-step login and reservation flow. Each person registers once with their own Villages credentials and then signs in directly on each device they use.

## Running Locally

```bash
.venv/bin/python app.py
```

App runs on http://localhost:8080. Each golfer signs in with their TVN username + TVN password + golf PIN, and then stays signed in on that device via the Flask session cookie.

Local dev needs Application Default Credentials for Firestore — run `gcloud auth application-default login` once.

To watch the browser during debugging, set `HEADLESS=false` in `.env` (also sets `slow_mo=600ms`).

## Project Structure

Three files do all the work:

- **`app.py`** — Flask backend. Loads `.env` via `python-dotenv`, exposes API routes for login, registration, tee times, and booking. User profiles are stored in **Firestore** (collection: `users`, document ID = TVN username). Login rate limiting protects against brute-force.
- **`golf_service.py`** — All Playwright automation. `GolfService` class handles login + booking. Credentials are passed per-call (not stored at init) to support multiple users. Includes `fetch_buddy_list()` to scrape available golfers from glf109c. Time parsing helpers deal with the site's non-standard 12-hour format (hours 1–6 = PM, 7–12 = AM).
- **`templates/index.html`** — Single-file SPA. All JS inline. Flow: login form (TVN username + password + PIN) or first-time register → booking. Golfer list is dynamic (fetched from Villages system and cached in the user's Firestore document).

## User Flow

1. New user → register: enters TVN username, password, golf PIN, and optional email → system logs in, scrapes buddy list from glf109c, stores the profile in Firestore
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

- **Only `golfer_ids[0]`** is used to look up tee times; `num_golfers` controls how many spots are reserved.
- **`_SITE_SELECT_PLAY_TIME = "98"`** is the option value for "View by Play Time" on glf109c.
- **`_COURSE_TYPE_CODES`**: `{"Championship": "01", "Executive": "02"}`.
- The hidden booking input on glf109y is `HHMM01` (e.g., time `02:05` → `020501`).
- Time filter labels in `golf_service.py` (`_FILTER_*` constants) must stay in sync with the onclick strings in `index.html`. Includes `_FILTER_ALL = "all"` for "Any Time".
- Gunicorn is configured with `--workers 1` (Playwright sync API is not thread-safe across workers) and `--threads 4 --timeout 120`.
- `GolfService` tracks `_current_user` to invalidate the cached Playwright session when a different user makes a request.
- Firestore document shape (collection `users`, doc id = TVN username): `tvn_password`, `golf_password`, `display_name`, `primary` (id/name/initials), `buddies`, `email`.
- Login rate limiter (`_login_attempts`) is in-memory per-instance — soft guardrail only, not consistent across Cloud Run instances.

## API Routes

- `GET /api/session` — Return current session state (auth, username, profile)
- `POST /api/login-user` — TVN username + password + golf PIN login (rate limited: 5 attempts, 5min lockout per IP/user)
- `POST /api/register` — Register new user (login + scrape buddy list, write to Firestore)
- `POST /api/refresh-buddies` — Re-scrape buddy list from Villages
- `POST /api/add-buddy` — Manually add a golfer by ID
- `POST /api/update-email` — Update notification email
- `POST /api/remove-user` — Delete the cached profile
- `POST /api/tee-times` — Fetch available tee times
- `GET /api/my-tee-times` — List the current user's reservations
- `POST /api/delete-reservation` — Delete a reservation
- `POST /api/book` — Book a tee time
- `POST /api/logout` — Clear session

## Environment Variables

Defined in `.env` (gitignored). See `.env.example` for the full list. Key vars:
- `HEADLESS` — set to `false` to watch the browser locally
- `SECRET_KEY` — Flask session secret. **Set this once and never rotate it** — changing the value invalidates every device's login cookie. Auto-generated only as a last resort for local dev.
- `LOG_LEVEL` — logging level (default `INFO`)
- `FIRESTORE_USERS_COLLECTION` — Firestore collection name for user profiles (default `users`)
- `GOOGLE_APPLICATION_CREDENTIALS` — path to a service-account JSON key for Firestore (only needed locally if not using `gcloud auth application-default login`)
