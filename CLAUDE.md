# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Flask + Playwright app that automates tee-time booking on The Villages golf system (thevillages.net). It drives a headless Chromium browser through a multi-step login and reservation flow. Supports multiple users — each person registers with their own Villages credentials.

## Running Locally

```bash
.venv/bin/python app.py
```

App runs on http://localhost:8080. PIN to unlock the UI is set via `APP_PIN` in `.env` (currently `1730`).

To watch the browser during debugging, set `HEADLESS=false` in `.env` (also sets `slow_mo=600ms`).

## Project Structure

Three files do all the work:

- **`app.py`** — Flask backend. Loads `.env` via `python-dotenv`, exposes API routes for PIN auth, user management, tee times, and booking. User profiles are cached in `users.json` (gitignored). PIN rate limiting protects against brute-force.
- **`golf_service.py`** — All Playwright automation. `GolfService` class handles login + booking. Credentials are passed per-call (not stored at init) to support multiple users. Includes `fetch_buddy_list()` to scrape available golfers from glf109c. Time parsing helpers deal with the site's non-standard 12-hour format (hours 1–6 = PM, 7–12 = AM).
- **`templates/index.html`** — Single-file SPA. All JS inline. Flow: PIN → user selection → booking. Golfer list is dynamic (fetched from Villages system and cached).

## Multi-User Flow

1. User enters the shared app PIN
2. Selects an existing profile or registers as a new golfer
3. Registration: enters TVN username, password, and golf PIN → system logs in, scrapes buddy list from glf109c, caches profile to `users.json`
4. Subsequent visits: just select your name from the list
5. Buddy list can be refreshed or extended by adding a golfer ID manually

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
- User profiles stored in `users.json` include: TVN password, golf PIN, display name, primary golfer (id/name/initials), and buddy list.

## API Routes

- `POST /api/verify-pin` — App PIN check (rate limited: 5 attempts, 5min lockout)
- `GET /api/users` — List cached user profiles (no passwords)
- `POST /api/select-user` — Set active user for session
- `POST /api/register` — Register new user (login + scrape buddy list)
- `POST /api/refresh-buddies` — Re-scrape buddy list from Villages
- `POST /api/add-buddy` — Manually add a golfer by ID
- `POST /api/remove-user` — Delete a cached profile
- `POST /api/tee-times` — Fetch available tee times
- `POST /api/book` — Book a tee time
- `POST /api/logout` — Clear session

## Environment Variables

Defined in `.env` (gitignored). See `.env.example` for the full list. Key vars:
- `APP_PIN` — PIN to unlock the web UI
- `HEADLESS` — set to `false` to watch the browser locally
- `SECRET_KEY` — Flask session secret (auto-generated if not set)
- `LOG_LEVEL` — logging level (default `INFO`)
