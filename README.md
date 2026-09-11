# Village Fairways

A mobile-friendly website for The Villages golf: find and book tee times,
submit advance requests, discover courses, and preview golf cart directions.
It runs in Safari, Chrome, and desktop browsers. No native iOS application is
required; an optional home-screen shortcut opens the same website.

## Local development

Use Python 3.10 or newer. Create a virtual environment, install
`requirements.txt`, then run `python -m playwright install chromium`.
Copy `.env.example` to `.env` and set stable session and encryption keys.
For local HTTP only, set `SESSION_COOKIE_SECURE=0` and `TRUST_PROXY=0`.
Run `python app.py`, then open `http://localhost:8080`.

Profiles are encrypted in SQLite. Keep both the database and its encryption
key in your backups. Do not rotate either key as part of a routine deployment.

## Website features

- Tee-time search with group, guest, course-type, region, and time-window
  preferences; a confirmation sheet before booking.
- Advance requests with ordered course preferences, saved groups, and clear
  handling of unavailable golfers or unconfirmed submissions.
- Course discovery with maps, favorites, sourced locations and course details.
- Golf cart route previews with mileage, estimated travel time, and written
  directions; origin can be current location or a saved home pin.
- Reservation calendar export and a configurable arrival buffer.

Course and route data are separate from the stateful golf booking browser.
Opening a map cannot navigate away from the upstream tee-time selection page.
Routing is a preview based on the provider's network; it does not promise
continuous spoken navigation, current closures, or background GPS tracking.
See [mapping provenance](docs/MAPPING_SOURCES.md) for sources and freshness.

## Testing

```sh
python -m unittest discover -s tests -v
```

Tests use temporary databases, synthetic profiles, and mocked Villages and
email services. They do not submit live reservations. `test_fetch.py` and
`test_menu.py` are manual, opt-in diagnostics requiring `--live`; they are
not the automated suite.

For an isolated local preview, set `DISABLE_REQUEST_WATCHER=1`, use a temporary
database and fixture worker, and never load production credentials.

## Deployment

The existing deployment uses one VPS, Caddy, systemd, and Gunicorn. See
[DEPLOY.md](DEPLOY.md). Keep one Gunicorn worker so browser state and background
notifications have a single owner. HTTP requests use four threads and enqueue
automation on a dedicated Playwright thread. Recommended HTTP timeout: 300s.

The Docker image uses the matching Playwright version and excludes secrets,
databases, and local environments. Mount persistent storage at `/data` and
provide the keys through environment variables; never bake secrets into an image.
