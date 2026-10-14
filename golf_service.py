"""
Villages Golf Service
Uses Playwright (headless Chromium) to automate the thevillages.net
tee-time booking system. Mirrors the exact flow used in the live session.
"""

import os
import re
import time as _time
import logging
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from course_metadata import band_key, lookup_course_metadata
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

# Set HEADLESS=false in .env to watch the browser during local testing
_HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

# The Villages golf site displays times in a non-standard 12-hour format
# with no AM/PM label.  The mapping is:
#   07:xx – 12:xx  →  7 AM – 12 PM  (morning)
#   01:xx – 06:xx  →  1 PM –  6 PM  (afternoon)
# So hours 1-6 need +12 to convert to real 24-hour time.
_SITE_SELECT_PLAY_TIME = "98"   # option value for "View by Play Time" dropdown
_COURSE_TYPE_CODES = {"Championship": "01", "Executive": "02"}

# Time-filter labels sent by the frontend (must stay in sync with index.html)
_FILTER_ALL     = "all"
_FILTER_EARLY   = "before 9am"
_FILTER_MORNING = "9am \u2013 noon"
_FILTER_MIDDAY  = "noon \u2013 2pm"
_FILTER_AFTER   = "after 2pm"


# ── Time helpers ──────────────────────────────────────────────────────────────
def _parse_time(t):
    """Parse HH:MM (site 12-ish hour) into (hour, minute) integers."""
    try:
        parts = t.strip().split(":")
        return int(parts[0]), int(parts[1])
    except Exception:
        return -1, -1

def _to_real_hour(h):
    """Convert site hour (1-12, no AM/PM) to real 24-hour hour."""
    # 07-12 = AM (no adjustment), 01-06 = PM (+12)
    return h + 12 if 1 <= h <= 6 else h

def _matches_filter(time_str, time_filter):
    """Return True if time_str passes the requested time_filter."""
    if not time_filter or time_filter == _FILTER_ALL:
        return True
    h, _ = _parse_time(time_str)
    if h < 0:
        return False
    real_h = _to_real_hour(h)
    f = time_filter.strip()
    if f == _FILTER_EARLY:
        return real_h < 9
    if f == _FILTER_MORNING:
        return 9 <= real_h < 12
    if f == _FILTER_MIDDAY:
        return 12 <= real_h < 14
    if f == _FILTER_AFTER:
        return real_h >= 14
    return True  # unknown filter → show everything

def _display_time(t):
    """Convert site time (e.g. '02:05') to 12-hour display ('2:05 PM')."""
    h, m = _parse_time(t)
    if h < 0:
        return t
    real_h = _to_real_hour(h)
    if real_h < 12:
        return f"{real_h}:{m:02d} AM"
    if real_h == 12:
        return f"12:{m:02d} PM"
    return f"{real_h - 12}:{m:02d} PM"


def _normalize_course_label(raw_text):
    """Build a compact course label from the tee-time table cell."""
    lines = [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]
    if not lines:
        return ""

    text = " - ".join(line for line in lines if not line.isdigit())
    if not text:
        text = " - ".join(lines)

    # Drop extra stats; the UI only needs the course and starting hole.
    text = re.split(r"\s*-\s*(?:Level|Yardage)\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0]

    # Remove numeric course ids like "(48)" and normalize hole formatting.
    text = re.sub(r"\s*\(\d+\)", "", text)
    text = re.sub(
        r"[/\-]\s*hole\s*\(?\s*(\d+)\s*\)?",
        lambda m: f", hole {m.group(1)}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bhole\s*\(?\s*(\d+)\s*\)?",
        lambda m: f"hole {m.group(1)}",
        text,
        flags=re.IGNORECASE,
    )

    # Championship labels sometimes end with incomplete fragments like
    # "/hole" or "- hole" with no number. Keep the nine name and drop the
    # dangling suffix.
    text = re.sub(r"[/\-]\s*hole\b\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r",\s*$", "", text)

    # Clean up separator spacing introduced by the source HTML.
    text = re.sub(r"\s*-\s*", " - ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    return text.strip(" -")


def _clean_golfer_name(raw_name, golfer_id=None):
    """Strip trailing golfer ID from a Villages display name.
    e.g. 'Marcia Ann Worley 640405' → 'Marcia Ann Worley'"""
    name = raw_name.strip()
    if golfer_id and name.endswith(golfer_id):
        name = name[:-len(golfer_id)].strip()
    # Also strip any trailing all-digit token (fallback)
    parts = name.rsplit(None, 1)
    if len(parts) == 2 and parts[1].isdigit():
        name = parts[0]
    return name


def _normalize_space(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _normalize_play_date(value, today=None):
    """Return a Villages play date as YYYY-MM-DD, or None when unparseable.

    Open reservations omit the year (for example ``Thursday: 9/10``). Those
    reservations are future-facing, so infer the closest date on or after the
    current Eastern date. The weekday, when present, must agree with the
    inferred date.
    """
    raw = _normalize_space(value)
    if not raw:
        return None
    current = today or datetime.now(ZoneInfo("America/New_York")).date()
    raw_without_weekday = re.sub(
        r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*:\s*",
        "", raw, flags=re.IGNORECASE,
    )

    for pattern, order in (
        (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", "ymd"),
        (r"^(\d{4})(\d{2})(\d{2})$", "ymd"),
        (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", "mdy"),
    ):
        match = re.fullmatch(pattern, raw_without_weekday)
        if not match:
            continue
        parts = [int(part) for part in match.groups()]
        year, month, day = parts if order == "ymd" else (parts[2], parts[0], parts[1])
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    match = re.search(
        r"^(?:(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*:\s*)?"
        r"(\d{1,2})/(\d{1,2})$",
        raw,
        re.IGNORECASE,
    )
    if not match:
        return None
    weekday, month_text, day_text = match.groups()
    candidates = []
    for year in (current.year - 1, current.year, current.year + 1, current.year + 2):
        try:
            candidate = date(year, int(month_text), int(day_text))
        except ValueError:
            continue
        if weekday and candidate.strftime("%A").lower() != weekday.lower():
            continue
        if (candidate - current).days >= -1:
            candidates.append(candidate)
    return min(candidates).isoformat() if candidates else None


def _course_key(value):
    """Normalize a course label for exact equality across the two CGI pages."""
    label = _normalize_course_label(value)
    label = re.sub(r"\b(?:starting\s+)?hole\s*\d+\b", "", label, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def _reservation_identity(reservation):
    return str((reservation or {}).get("reservation_no") or "").strip()


def _unknown_result(message):
    return {"success": False, "unknown": True, "error": message}


# Marks the "delete all players" link in the row for one reservation number.
# Use regex literals only: a RegExp built from a string literal here loses its
# backslashes twice (Python, then JavaScript) and silently never matches.
_MARK_RESERVATION_DELETE_LINK_JS = """reservationNo => {
    const rows = Array.from(document.querySelectorAll('table tr'));
    for (const row of rows) {
        const cells = row.querySelectorAll('td');
        if (cells.length < 3) continue;
        const match = (cells[1].innerText || '').match(/Reservation\\s*No\\.?\\s*(\\d+)/i);
        if (!match || match[1] !== reservationNo) continue;
        const link = Array.from(row.querySelectorAll('a')).find(a =>
            /^delete\\s+all\\s+players\\s+on\\s+this\\s+reservation$/i.test(
                (a.innerText || '').replace(/\\s+/g, ' ').trim()
            )
        );
        if (link) {
            link.setAttribute('data-delete-reservation', reservationNo);
            return true;
        }
    }
    return false;
}"""


# Header and menu lines that every Villages golf page shares.
_SITE_CHROME_LINES = frozenset({
    "common internet termslegal disclaimer", "common internet terms", "legal disclaimer",
    "home", "email", "golf", "events", "news", "user info", "help", "logout",
    "the villages tee time system", "click here for help",
})


def _site_reply_text(body, limit=300):
    """Return the message part of a Villages golf page without its header.

    A booking reply without a reservation number usually explains why the
    site declined it, so the golfer sees that reason instead of only a
    generic warning.
    """
    lines = [_normalize_space(line) for line in str(body or "").splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        if line.lower() == "click here for help":
            lines = lines[index + 1:]
            break
    kept = [
        line for line in lines
        if line.lower() not in _SITE_CHROME_LINES
        and not re.match(r"your id is\b", line, re.IGNORECASE)
        and not re.fullmatch(
            r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+\w+\s+\d{1,2},\s*\d{4}",
            line, re.IGNORECASE,
        )
    ]
    text = " ".join(kept)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _timeout_error():
    return "The Villages golf site took too long to respond. Please try again."


# A rejected TVN login does not redirect anywhere with "login" in the URL. The
# site bounces back to the homepage with a ?reason=denied_* query string (e.g.
# denied_bad_password) and an error banner. Detect both, otherwise the next step
# (click GOLF, wait for glf000) just spins until its timeout and the golfer is
# told the site was slow when their username or password was actually wrong.
_TVN_DENIED_ERROR = (
    "The Villages website did not accept that username or password. "
    "Your TVN username is not your email address (it is the username you use on "
    "thevillages.net), and the password is case-sensitive."
)
_GOLF_PIN_DENIED_ERROR = (
    "The Villages golf system did not accept that golf PIN. "
    "It is the PIN you use on the golf reservation screens, not your TVN password."
)


def _page_text(page):
    """Best-effort visible text of the page, lowercased. '' if unavailable."""
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def _tvn_login_rejected(page):
    """True if thevillages.net refused the TVN username / password."""
    url = (page.url or "").lower()
    if "reason=denied" in url or "login" in url:
        return True
    return "error in processing either your" in _page_text(page)


def _initials(name):
    """Derive initials from a name like 'Worley, Walter Douglas' or 'Walter Worley'."""
    name = name.strip()
    # Strip trailing numeric ID if present (e.g. "Walter Douglas Worley 483204")
    parts = name.rsplit(None, 1)
    if len(parts) == 2 and parts[1].isdigit():
        name = parts[0]
    if "," in name:
        parts = [p.strip() for p in name.split(",")]
        # "Last, First Middle" → first letter of First + Last
        if len(parts) >= 2 and parts[1]:
            return (parts[1][0] + parts[0][0]).upper()
        return parts[0][0].upper()
    words = name.split()
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    return name[0].upper() if name else "?"


# ── Main service class ────────────────────────────────────────────────────────
class _Session:
    """One logged-in browser context for a single golfer."""
    __slots__ = ("context", "page", "ts", "golf_home_url", "search_context")

    def __init__(self):
        self.context = None
        self.page = None
        self.ts = 0
        self.golf_home_url = None   # stored after login to glf100
        self.search_context = None


class GolfService:
    # Playwright's sync API allows only one live sync_playwright() instance per
    # OS thread, so GolfService owns exactly one shared playwright instance and
    # one shared chromium Browser (both lazy, built on first use). Per-user
    # isolation comes from BrowserContext, not the Browser: each _Session holds
    # its own context (cookies, login, viewport, user agent), and a small pool
    # of these per-user contexts is kept warm so concurrent golfers don't evict
    # each other and force a full re-login on every request. All work runs on
    # the single worker thread, so this is safe without locking.
    # Least-recently-used context is evicted when the pool is full.
    _MAX_SESSIONS = int(os.environ.get("MAX_BROWSER_SESSIONS", "4"))
    # A session used within this window is treated as "mid-shopping" (a golfer
    # who searched and is about to book) and protected from LRU eviction.
    _SEARCH_PROTECT_SECONDS = int(os.environ.get("SESSION_PROTECT_SECONDS", "300"))

    def __init__(self):
        from collections import OrderedDict
        self._sessions = OrderedDict()   # tvn_username -> _Session
        self._active = None              # the _Session for the in-flight request
        self._active_user = None
        self._pw = None                  # shared sync_playwright() instance
        self._browser = None             # shared chromium Browser

    def _safe_url(self):
        """URL of the in-flight session's page, for logging. '?' if unavailable."""
        try:
            return self._active.page.url if self._active and self._active.page else "?"
        except Exception:
            return "?"

    def _close(self, sess):
        """Tear down one session's browser context. The shared browser and
        playwright instance outlive individual sessions and are never closed
        here."""
        if not sess:
            return
        try:
            sess.context and sess.context.close()
        except Exception:
            pass

    def _drop_session(self, tvn_username):
        """Close and forget a user's session."""
        sess = self._sessions.pop(tvn_username, None)
        self._close(sess)
        if self._active_user == tvn_username:
            self._active = None
            self._active_user = None

    def _close_active(self):
        """Drop the session for the request currently being served."""
        if self._active_user:
            self._drop_session(self._active_user)

    def _session_usable(self, sess):
        """Return True if a session's context/page are still valid AND the
        shared browser they belong to is still alive. A dead shared browser
        takes every pooled context down with it, so this must fail closed
        rather than let a caller (e.g. book_tee_time) try to use a page whose
        underlying browser process is gone."""
        if not sess or not sess.context or not sess.page:
            return False
        try:
            if not self._browser or not self._browser.is_connected():
                return False
            if sess.page.is_closed():
                return False
        except Exception:
            return False
        return True

    def _get_or_create_session(self, tvn_username):
        """Return (browser, page) for this user, reusing a warm session when
        possible. Marks it active for the duration of the request."""
        sess = self._sessions.get(tvn_username)
        if sess and self._session_usable(sess):
            self._sessions.move_to_end(tvn_username)   # mark most-recently-used
            self._active, self._active_user = sess, tvn_username
            return self._browser, sess.page

        # Stale entry — discard before rebuilding.
        if sess:
            self._drop_session(tvn_username)

        # Do this before the eviction loop: if the shared browser died, every
        # pooled context died with it, so the pool gets invalidated first and
        # the eviction loop below runs against a clean (possibly empty) pool.
        self._ensure_browser()

        # Evict to make room, but never evict a golfer who just searched and is
        # about to book (the 7 AM money path). Prefer the oldest idle session;
        # only if every session is mid-shopping do we evict the absolute oldest.
        now = _time.time()
        while len(self._sessions) >= self._MAX_SESSIONS:
            victim_key = None
            for key, s in self._sessions.items():  # oldest-first (LRU order)
                if now - (s.ts or 0) > self._SEARCH_PROTECT_SECONDS:
                    victim_key = key
                    break
            if victim_key is None:
                victim_key = next(iter(self._sessions))  # all busy — evict oldest
            old_sess = self._sessions.pop(victim_key)
            self._close(old_sess)

        sess = _Session()
        sess.context, sess.page = self._new_context()
        sess.ts = _time.time()
        self._sessions[tvn_username] = sess
        self._active, self._active_user = sess, tvn_username
        return self._browser, sess.page

    def _ensure_browser(self):
        """Make sure self._browser is a live chromium Browser, launching (or
        relaunching) the shared playwright + browser as needed. Playwright's
        sync API tolerates only one sync_playwright() instance per thread, so
        this instance and browser are shared across every pooled session."""
        if self._browser is not None:
            try:
                alive = self._browser.is_connected()
            except Exception:
                alive = False
            if alive:
                return
            # The shared browser died, which takes every pooled context down
            # with it. There is nothing left to reuse, so drop the whole pool
            # rather than leave sessions around whose context/page are dead.
            logger.warning(
                "shared browser died; relaunching and invalidating %d pooled sessions",
                len(self._sessions),
            )
            for sess in list(self._sessions.values()):
                self._close(sess)
            self._sessions.clear()
            self._active = None
            self._active_user = None
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._pw is None:
            self._pw = sync_playwright().start()

        logger.info("launching shared chromium browser (headless=%s)", _HEADLESS)
        try:
            self._browser = self._pw.chromium.launch(
                headless=_HEADLESS,
                slow_mo=600 if not _HEADLESS else 0,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
        except Exception:
            # A wedged playwright instance should not permanently break the
            # service: restart it once and retry the launch once.
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self._pw = sync_playwright().start()
            logger.info("relaunching shared chromium browser after a failed launch")
            self._browser = self._pw.chromium.launch(
                headless=_HEADLESS,
                slow_mo=600 if not _HEADLESS else 0,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )

    def _new_context(self):
        """Create a fresh, isolated BrowserContext + Page on the shared
        browser. Cookies, storage, viewport, and user agent are all
        context-scoped, so a fresh context is all per-user isolation needs."""
        ctx = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        return ctx, ctx.new_page()

    def _login(self, page, tvn_username, tvn_password, golf_password):
        # Step 1: TVN login
        page.goto("https://www.thevillages.net/", wait_until="networkidle", timeout=20000)
        page.locator('input[type="text"]').first.fill(tvn_username)
        page.locator('input[type="password"]').first.fill(tvn_password)
        page.locator('input[type="image"]').click()
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            page.wait_for_load_state("domcontentloaded", timeout=10000)

        if _tvn_login_rejected(page):
            raise RuntimeError(_TVN_DENIED_ERROR)

        # Step 2: Golf system login
        page.locator("a", has_text="GOLF").first.click()
        try:
            page.wait_for_url("**/glf000**", timeout=12000)
        except PWTimeout:
            # Never reaching the PIN screen almost always means the TVN login
            # was refused after all, rather than the site being slow.
            if _tvn_login_rejected(page):
                raise RuntimeError(_TVN_DENIED_ERROR)
            raise

        page.locator("input:visible").first.fill(golf_password)
        page.locator('input[value="Continue"]').click()
        try:
            page.wait_for_url("**/glf100**", timeout=12000)
        except PWTimeout:
            # A bad PIN re-renders glf000 instead of advancing to the menu.
            if "glf000" in (page.url or "").lower():
                raise RuntimeError(_GOLF_PIN_DENIED_ERROR)
            raise

        self._active.golf_home_url = page.url
        self._active.ts = _time.time()

    def _ensure_logged_in(self, page, tvn_username, tvn_password, golf_password):
        url = (page.url or "").lower()
        
        # If we are already exactly on the main menu, we can assume session is okay.
        # If it timed out server-side, a subsequent click will fail, but usually it's fine.
        if "glf100" in url or "glf105" in url:
            self._active.ts = _time.time()
            return
            
        # Try to click the native "Go Back to Menu" form button if we are on a subpage.
        # This properly preserves the CGI backward state without needing a PIN re-login.
        try:
            btn = page.locator("input[name='Menu']")
            if btn.count() > 0:
                btn.first.click(timeout=5000)
                page.wait_for_load_state("networkidle")
                new_url = page.url.lower()
                if "glf100" in new_url or "glf105" in new_url:
                    self._active.ts = _time.time()
                    return
        except Exception:
            pass

        # If we are anywhere else (error page, stuck), attempting to navigate via GET
        # breaks the CGI state. Safest and fastest robust way to get back
        # to the main menu is to re-run the PIN login sequence from the TVN homepage.
        self._login(page, tvn_username, tvn_password, golf_password)

    def _nav_to_glf109c(self, page, num_golfers, has_guests, course_type,
                        from_reservations_page=False):
        """Navigate from glf100 (or an existing glf109a) to glf109c."""
        if not from_reservations_page:
            page.locator("a", has_text="Reservations-View Open Tee Times").click()
            page.wait_for_url("**/glf109a**", timeout=10000)
        # glf109b
        page.get_by_role("link", name="Create New Reservation", exact=True).click()
        page.wait_for_url("**/glf109b**", timeout=10000)

        page.locator('table input[type="text"]').first.fill(str(num_golfers))
        page.locator(f'input[value="{"Y" if has_guests else "N"}"]').check()
        page.locator("select").select_option(_COURSE_TYPE_CODES.get(course_type, "02"))
        page.locator("a", has_text="Continue to Enter Golfers").click()
        page.wait_for_url("**/glf109c**", timeout=10000)

    def _nav_to_glf109a(self, page):
        """Navigate from glf100 to the reservations menu page (glf109a)."""
        page.locator("a", has_text="Reservations-View Open Tee Times").click()
        page.wait_for_url("**/glf109a**", timeout=10000)

    def _extract_reservations(self, page):
        """Scrape a recognized glf109a page, preserving field line breaks.

        Raises when the page is not a valid reservations list. An empty list
        therefore means a verified empty list rather than a scraper failure.
        """
        payload = page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll("table tr"));
            const parsedRows = rows.map(row => {
                const cells = Array.from(row.querySelectorAll("td")).map(cell =>
                    (cell.innerText || "").split(/\\r?\\n/)
                        .map(line => line.replace(/[ \\t]+/g, " ").trim())
                        .filter(Boolean).join("\\n")
                );
                const links = Array.from(row.querySelectorAll("a")).map(link =>
                    (link.innerText || "").replace(/\\s+/g, " ").trim()
                ).filter(Boolean);
                const golferIds = Array.from(row.querySelectorAll(
                    'input[name*="golf" i], input[name*="player" i], select[name*="buddy" i]'
                )).map(el => (el.value || '').trim()).filter(v => /^\\d+$/.test(v));
                return {cells, links, golferIds};
            }).filter(row => row.cells.some(Boolean));
            const body = (document.body && document.body.innerText || '');
            const valid = /glf109a/i.test(location.pathname + location.search) &&
                /(open reservations|create new reservation|no (?:open |outstanding )?reservations)/i.test(body);
            return {valid, rows: parsedRows};
        }""")

        if not isinstance(payload, dict) or not payload.get("valid"):
            raise RuntimeError("The reservations page layout was not recognized.")
        raw_rows = payload.get("rows") or []

        reservations = []
        for row in raw_rows:
            cells = row.get("cells") or []
            if len(cells) < 3:
                continue

            action_text = _normalize_space(cells[0])
            reservation_text = _normalize_space(cells[1])
            description_text = _normalize_space(cells[2])
            combined = " ".join(x for x in [action_text, reservation_text, description_text] if x).lower()

            if not combined:
                continue
            if "action" in combined and "open reservations" in combined:
                continue
            if "create new reservation" in action_text.lower():
                continue

            reservation_no = None
            res_match = re.search(r"reservation\s*no\.?\s*(\d+)", reservation_text, re.IGNORECASE)
            if res_match:
                reservation_no = res_match.group(1)
            if not reservation_no:
                continue

            description_lines = [line.strip() for line in str(cells[2] or "").splitlines() if line.strip()]
            description = _normalize_space(" ".join(description_lines)) or description_text
            description_source = "\n".join(description_lines)

            date = None
            time = None
            display_time = None
            course = None
            phone = None

            date_match = re.search(
                r"\b([A-Za-z]+)\s*:\s*(\d{1,2}/\d{1,2}(?:/\d{4})?)\s+at\s+"
                r"(\d{1,2}:\d{2}\s*[AP]M)\b",
                description_source,
                re.IGNORECASE,
            )
            if date_match:
                date = f"{date_match.group(1).title()}: {date_match.group(2)}"
                display_time = _normalize_space(date_match.group(3).upper())

            course_match = re.search(r"^\s*Course\s*:\s*(.+?)\s*$", description_source,
                                     re.IGNORECASE | re.MULTILINE)
            if not course_match:
                course_match = re.search(
                    r"\bCourse\s*:\s*(.+?)(?=\s+(?:Phone|Golfer(?:s)?|Player(?:s)?)\s*:|$)",
                    description,
                    re.IGNORECASE,
                )
            if course_match:
                course = _normalize_space(course_match.group(1))
            phone_match = re.search(r"^\s*Phone\s*:\s*(.+?)\s*$", description_source,
                                    re.IGNORECASE | re.MULTILINE)
            if not phone_match:
                phone_match = re.search(
                    r"\bPhone\s*:\s*(.+?)(?=\s+(?:Course|Golfer(?:s)?|Player(?:s)?)\s*:|$)",
                    description,
                    re.IGNORECASE,
                )
            if phone_match:
                phone = _normalize_space(phone_match.group(1))

            if display_time:
                time = display_time
            if not course:
                course = description

            actions = row.get("links") or []

            observed_golfer_ids = []
            for value in row.get("golferIds") or []:
                gid = str(value).strip()
                if gid.isdigit() and gid not in observed_golfer_ids:
                    observed_golfer_ids.append(gid)
            for match in re.finditer(
                r"\b(?:Golfer(?:s)?|Player(?:s)?|Member(?:s)?)\s*:\s*([^\n]+)",
                str(cells[2] or ""),
                re.IGNORECASE,
            ):
                for gid in re.findall(r"(?<!\d)(\d{4,8})(?!\d)", match.group(1)):
                    if gid not in observed_golfer_ids:
                        observed_golfer_ids.append(gid)

            item = {
                "reservation_no": reservation_no,
                "date": date,
                "play_date": _normalize_play_date(date),
                "time": time,
                "display_time": display_time or "",
                "course": course,
                "phone": phone,
                "actions": actions,
                "summary": description,
            }
            if observed_golfer_ids:
                item["golfer_ids"] = observed_golfer_ids
            reservations.append(item)

        return reservations

    def _find_booked_reservation(self, page, tvn_username, tvn_password,
                                 golf_password, time_str, expected_date,
                                 expected_course, preexisting_ids):
        """After an ambiguous booking submit, check glf109a for a matching
        reservation. Returns the reservation dict only when exactly one new
        reservation matches time, date, and course; otherwise None.

        Used to distinguish "the confirmation page was just slow / worded
        differently" (booking DID commit) from "the booking never happened",
        so we never report a plain failure that invites a duplicate booking.
        Two or more matches are just as unconfirmed as zero: picking one at
        random would be as likely to report the wrong reservation as the
        right one, so an ambiguous result is logged and treated as unknown.
        Best-effort: any navigation error yields None (treated as unconfirmed).
        """
        try:
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_glf109a(page)
            reservations = self._extract_reservations(page)
        except Exception:
            logger.exception("book reconciliation failed for user=%s", tvn_username)
            return None

        if preexisting_ids is None:
            return None
        want_time = _normalize_space(_display_time(time_str)).upper()
        want_date = _normalize_play_date(expected_date)
        want_course = _course_key(expected_course)
        if not want_time or not want_date or not want_course:
            return None
        matches = []
        for r in reservations:
            rid = _reservation_identity(r)
            if not rid or rid in preexisting_ids:
                continue
            if _normalize_space(r.get("display_time")).upper() != want_time:
                continue
            if r.get("play_date") != want_date:
                continue
            if _course_key(r.get("course")) != want_course:
                continue
            matches.append(r)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "book reconciliation ambiguous user=%s matches=%d",
                tvn_username, len(matches),
            )
        return None

    def _reconcile_reservation_absent(self, page, tvn_username, tvn_password,
                                      golf_password, reservation_no):
        """Return (absent, reservations) from a verified glf109a, or (None, None)."""
        try:
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_glf109a(page)
            reservations = self._extract_reservations(page)
        except Exception:
            logger.exception(
                "reservation delete reconciliation failed user=%s reservation=%s",
                tvn_username, reservation_no,
            )
            return None, None
        target = str(reservation_no).strip()
        exists = any(_reservation_identity(item) == target for item in reservations)
        return not exists, reservations

    def _booking_success(self, reservation_no, course_name, time_str, ctx):
        """Build booking output exclusively from the stored search context."""
        return {
            "success": True,
            "reservation_no": str(reservation_no),
            "course": course_name,
            "time": time_str,
            "display_time": _display_time(time_str),
            "play_date": ctx.get("play_date"),
            "date": ctx.get("date_str"),
            "date_label": ctx.get("date_label"),
            "golfer_ids": list(ctx.get("golfer_ids") or []),
            "num_golfers": ctx.get("num_golfers"),
            "has_guests": ctx.get("has_guests"),
            "course_type": ctx.get("course_type"),
            "search_id": ctx.get("search_id"),
            "message": (
                f"Booked! Reservation #{reservation_no}: {course_name} "
                f"at {_display_time(time_str)}"
            ),
        }

    def _reconcile_booking_outcome(self, page, tvn_username, tvn_password,
                                   golf_password, ctx, course_name, time_str,
                                   site_reply=""):
        found = self._find_booked_reservation(
            page, tvn_username, tvn_password, golf_password,
            time_str, ctx.get("date_str"), course_name,
            ctx.get("preexisting_reservation_ids"),
        )
        if found and found.get("reservation_no"):
            return self._booking_success(
                found["reservation_no"], course_name, time_str, ctx
            )
        self._close_active()
        if site_reply:
            return _unknown_result(
                "We could not confirm your booking, so check My Tee Times before "
                f"trying again. The Villages site said: “{site_reply}”"
            )
        return _unknown_result(
            "We could not confirm your booking. Open My Tee Times to check "
            "whether it went through before trying again."
        )

    def _setup_reservation(self, page, num_golfers, has_guests, course_type,
                           golfer_ids, date_str, from_reservations_page=False):
        self._nav_to_glf109c(
            page, num_golfers, has_guests, course_type,
            from_reservations_page=from_reservations_page,
        )

        # glf109c — first select = date, second = sort order, remaining = golfer slots
        selects = page.locator("select").all()
        selects[0].select_option(date_str)
        selects[1].select_option(_SITE_SELECT_PLAY_TIME)

        golfer_selects = selects[2:]
        selected_ids = [str(g).strip() for g in (golfer_ids or []) if str(g).strip()]

        # Fill golfer slots in order so slot 1 is never skipped when num_golfers > 1.
        for idx, golfer_select in enumerate(golfer_selects):
            if idx >= len(selected_ids):
                break
            golfer_select.select_option(selected_ids[idx])

        page.locator('input[value="Submit"]').first.click()
        page.wait_for_url("**/glf109e**", timeout=15000)
        self._active.ts = _time.time()

    def _extract_times(self, page, time_filter=None, region_filter=None):
        raw_times = page.evaluate("""() => {
            const rows = document.querySelectorAll("table tr");
            const results = [];
            for (let i = 0; i < rows.length; i++) {
                const cells = rows[i].querySelectorAll("td");
                if (cells.length < 4) continue;
                const linkElem = cells[0].querySelector("a");
                if (!linkElem) continue;
                
                const courseText = cells[1].innerText || "";
                const timeStr = (cells[2].innerText || "").trim();
                const avail = (cells[3].innerText || "").trim();
                
                if (courseText && timeStr) {
                    results.push({ course: courseText, time: timeStr, available: avail });
                }
            }
            return results;
        }""")

        times = []
        for t in raw_times:
            time_val = t["time"]
            if time_filter and not _matches_filter(time_val, time_filter):
                continue
            course_label = _normalize_course_label(t["course"])
            canonical_name, metadata = lookup_course_metadata(course_label)
            if region_filter and region_filter != "all":
                if band_key(metadata.get("band")) != region_filter:
                    continue
            times.append({
                "course":       course_label,
                "course_name":  canonical_name or course_label,
                "region":       metadata.get("region"),
                "band":         metadata.get("band"),
                "course_type":  metadata.get("course_type"),
                "address":      metadata.get("address"),
                "zip":          metadata.get("zip"),
                "time":         time_val,
                "display_time": _display_time(time_val),
                "available":    t["available"],
            })
        return times

    # ── Public methods ────────────────────────────────────────────────────────

    def fetch_buddy_list(self, tvn_username, tvn_password, golf_password):
        """Login and scrape the buddy list from glf109c golfer selects.
        Returns {success, primary: {id,name,initials}, buddies: [{id,name,initials}]}
        Keeps the Playwright session alive so subsequent operations reuse it."""
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._login(page, tvn_username, tvn_password, golf_password)
            self._nav_to_glf109c(page, num_golfers=1, has_guests=False, course_type="Executive")

            selects = page.locator("select").all()
            golfer_select = selects[2] if len(selects) > 2 else None

            buddies = []
            primary = None
            if golfer_select:
                raw_options = golfer_select.evaluate("""select => {
                    return Array.from(select.options).map(opt => ({
                        value: (opt.value || "").trim(),
                        text: (opt.innerText || "").trim()
                    }));
                }""")
                for opt in raw_options:
                    value = opt["value"]
                    raw_text = opt["text"]
                    # Skip placeholder options (empty value or non-numeric)
                    if not value or not value.isdigit():
                        continue
                    clean_name = _clean_golfer_name(raw_text, value)
                    entry = {"id": value, "name": clean_name, "initials": _initials(clean_name)}
                    buddies.append(entry)
                # First real option is the primary golfer (the logged-in user)
                if buddies:
                    primary = buddies[0]

            return {"success": True, "primary": primary, "buddies": buddies}

        except PWTimeout:
            # Record where we stalled; a bare timeout with no URL leaves the
            # reported "site was slow" unfalsifiable after the fact.
            logger.warning(
                "fetch_buddy_list timed out for user=%s at url=%s",
                tvn_username, self._safe_url(),
            )
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            logger.info("fetch_buddy_list rejected for user=%s: %s", tvn_username, e)
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("fetch_buddy_list failed for user=%s", tvn_username)
            self._close_active()
            return {"success": False, "error": "Could not connect to the golf system. Please try again."}

    def _capture_before_state(self, page, tvn_username):
        """Capture the set of reservation identities that exist before a
        search, so booking reconciliation can later tell a new reservation
        from a preexisting one. Retries once (re-navigating to glf109a
        before the second attempt) before giving up; a failed first attempt
        is logged at WARNING and retried, a failed second attempt is logged
        at ERROR (via logger.exception, unchanged severity) and returns
        None, which forces every later ambiguous booking in that search to
        report unknown rather than guess.
        """
        for attempt in (1, 2):
            try:
                before = self._extract_reservations(page)
            except Exception as exc:
                if attempt == 1:
                    logger.warning(
                        "before-state capture failed user=%s attempt=1 error=%s",
                        tvn_username, exc.__class__.__name__,
                    )
                    try:
                        self._nav_to_glf109a(page)
                    except Exception:
                        logger.exception(
                            "could not return to reservations page for before-state retry user=%s",
                            tvn_username,
                        )
                        return None
                    continue
                logger.exception(
                    "could not capture reservation before-state user=%s", tvn_username
                )
                return None
            return {_reservation_identity(r) for r in before}

    def get_available_times(self, tvn_username, tvn_password, golf_password,
                            date_str, date_label, course_type, region_filter,
                            golfer_ids, num_golfers, has_guests, time_filter=None):
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            # Capture a verified before-state while glf109a is already in the
            # normal search path. Reconciliation may only accept a reservation
            # identity that did not exist before this search.
            self._nav_to_glf109a(page)
            preexisting_ids = self._capture_before_state(page, tvn_username)
            self._setup_reservation(
                page, num_golfers, has_guests, course_type, golfer_ids, date_str,
                from_reservations_page=True,
            )
            times = self._extract_times(page, time_filter, region_filter)
            search_id = str(uuid.uuid4())
            self._active.search_context = {
                "user": tvn_username,
                "date_str": str(date_str or "").strip(),
                "play_date": _normalize_play_date(date_str),
                "date_label": str(date_label or "").strip(),
                "course_type": str(course_type or "").strip(),
                "region_filter": str(region_filter or "all").strip().lower(),
                "golfer_ids": [str(g).strip() for g in (golfer_ids or []) if str(g).strip()],
                "num_golfers": int(num_golfers),
                "has_guests": bool(has_guests),
                "search_id": search_id,
                "offered_times": [(t.get("course"), t.get("time")) for t in times],
                "preexisting_reservation_ids": preexisting_ids,
            }
            self._active.ts = _time.time()  # refresh TTL
            return {
                "success": True,
                "times": times,
                "date_label": date_label,
                "search_id": search_id,
            }
        except PWTimeout:
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("get_available_times failed for user=%s", tvn_username)
            self._close_active()
            return {"success": False, "error": "Could not fetch tee times right now. Please try again."}

    def view_my_tee_times(self, tvn_username, tvn_password, golf_password):
        """Return the current user's outstanding reservations from glf109a."""
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_glf109a(page)
            reservations = self._extract_reservations(page)
            self._active.ts = _time.time()
            return {"success": True, "verified": True, "reservations": reservations}
        except PWTimeout:
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("view_my_tee_times failed for user=%s", tvn_username)
            self._close_active()
            return {"success": False, "error": "Could not load your tee times right now. Please try again."}

    def delete_reservation(self, tvn_username, tvn_password, golf_password, reservation_no):
        """Delete one exact reservation and verify its absence on glf109a."""
        submission_attempted = False
        page = None
        target = str(reservation_no or "").strip()
        try:
            if not target.isdigit():
                return {"success": False, "error": "A valid reservation number is required."}
            _, page = self._get_or_create_session(tvn_username)
            current_url = (page.url or "").lower()
            if "glf109a" not in current_url:
                return {
                    "success": False,
                    "error": "Reservations page is no longer active. Tap Refresh My Tee Times, then try deleting again.",
                }

            # Parsing the page first proves this is a recognized reservations
            # list and that the exact reservation identity exists.
            current = self._extract_reservations(page)
            if not any(_reservation_identity(item) == target for item in current):
                return {"success": False, "error": "Reservation not found."}

            found = page.evaluate(_MARK_RESERVATION_DELETE_LINK_JS, target)
            if not found:
                return {"success": False, "error": "Delete action is not available for this reservation."}

            page.locator(f'a[data-delete-reservation="{target}"]').first.click(timeout=8000)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                page.wait_for_load_state("domcontentloaded", timeout=5000)

            # Step 2: the site shows a dedicated confirmation page. Stay on the
            # same live session/page and wait specifically for that page before
            # doing anything else. Do not re-login/re-navigate until after the
            # final delete click has been attempted. The load-state waits above
            # can return before the click's navigation starts, so wait for the
            # confirmation text itself or the list page is read by mistake.
            try:
                page.locator(f"text=Delete Reservation No. {target}").first.wait_for(timeout=10000)
            except PWTimeout:
                pass  # the identity check below reports what the page showed
            raw_body = page.inner_text("body")
            body_text = _normalize_space(raw_body)
            confirm_match = re.search(
                r"Delete\s+Reservation\s+No\.?\s*(\d+)", body_text, re.IGNORECASE
            )
            if not confirm_match or confirm_match.group(1) != target:
                logger.warning(
                    "delete: confirmation page did not identify reservation=%s user=%s page=%r",
                    target, tvn_username, _site_reply_text(raw_body),
                )
                return {
                    "success": False,
                    "error": "The delete confirmation did not identify the requested reservation.",
                    "debug_page": body_text[:500],
                }

            confirm_link = page.locator("a", has_text="DELETE ALL PLAYERS ON THIS RESERVATION")
            if confirm_link.count() == 0:
                return {"success": False, "error": "Delete confirmation page appeared, but the final delete link was not found."}

            def _accept_dialog(dialog):
                try:
                    dialog.accept()
                except Exception:
                    pass

            page.once("dialog", _accept_dialog)

            confirm_target = confirm_link.last
            confirm_target.scroll_into_view_if_needed(timeout=5000)
            confirm_target.wait_for(state="visible", timeout=5000)
            submission_attempted = True
            confirm_target.click(timeout=5000, force=True)

            # Same race as the first click: wait until the confirmation page is
            # gone so reconciliation never navigates away mid-delete.
            try:
                page.locator(f"text=Delete Reservation No. {target}").first.wait_for(
                    state="hidden", timeout=15000
                )
            except PWTimeout:
                pass  # reconciliation below decides deleted, present, or unknown
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                page.wait_for_load_state("domcontentloaded", timeout=5000)

            absent, remaining = self._reconcile_reservation_absent(
                page, tvn_username, tvn_password, golf_password, target
            )
            if absent is False:
                return {"success": False, "error": "Reservation still appears on the site after delete attempt."}
            if absent is None:
                self._close_active()
                return _unknown_result(
                    "We could not confirm whether the reservation was deleted. "
                    "Open My Tee Times and verify before trying again."
                )

            self._active.ts = _time.time()
            return {"success": True, "reservation_no": target, "reservations": remaining, "verified": True}
        except PWTimeout:
            if submission_attempted and page is not None:
                absent, remaining = self._reconcile_reservation_absent(
                    page, tvn_username, tvn_password, golf_password, target
                )
                if absent is True:
                    return {"success": True, "reservation_no": target, "reservations": remaining, "verified": True}
                if absent is False:
                    return {"success": False, "error": "Reservation still appears on the site after delete attempt."}
                self._close_active()
                return _unknown_result(
                    "We could not confirm whether the reservation was deleted. "
                    "Open My Tee Times and verify before trying again."
                )
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            if submission_attempted and page is not None:
                absent, remaining = self._reconcile_reservation_absent(
                    page, tvn_username, tvn_password, golf_password, target
                )
                if absent is True:
                    return {"success": True, "reservation_no": target,
                            "reservations": remaining, "verified": True}
                if absent is False:
                    return {"success": False,
                            "error": "Reservation still appears on the site after delete attempt."}
                self._close_active()
                return _unknown_result(
                    "We could not confirm whether the reservation was deleted. "
                    "Open My Tee Times and verify before trying again."
                )
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception(
                "delete_reservation failed for user=%s reservation_no=%s",
                tvn_username,
                reservation_no,
            )
            if submission_attempted and page is not None:
                absent, remaining = self._reconcile_reservation_absent(
                    page, tvn_username, tvn_password, golf_password, target
                )
                if absent is True:
                    return {"success": True, "reservation_no": target, "reservations": remaining, "verified": True}
                if absent is False:
                    return {"success": False, "error": "Reservation still appears on the site after delete attempt."}
                self._close_active()
                return _unknown_result(
                    "We could not confirm whether the reservation was deleted. "
                    "Open My Tee Times and verify before trying again."
                )
            self._close_active()
            return {"success": False, "error": "Could not delete that reservation right now. Please try again."}

    def book_tee_time(self, tvn_username, tvn_password, golf_password, course_name, time_str,
                      expected_date=None, expected_golfer_ids=None,
                      expected_num_golfers=None, expected_course_type=None,
                      expected_has_guests=None, search_id=None):
        submission_attempted = False
        page = None
        ctx = {}
        try:
            # Booking must reuse the exact session built by get_available_times().
            # Look it up by user so another golfer's request can't clobber it.
            sess = self._sessions.get(tvn_username)
            if not sess or not self._session_usable(sess):
                return {"success": False, "error": "Session expired. Please fetch tee times again."}
            # Keep this session warm — it just searched and is about to book, so
            # it must not be the LRU victim if another golfer needs a session.
            self._sessions.move_to_end(tvn_username)
            self._active, self._active_user = sess, tvn_username
            page = sess.page
            ctx = sess.search_context or {}

            if ctx.get("user") != tvn_username:
                return {"success": False, "error": "Search context changed. Please fetch tee times again."}
            if "glf109e" not in (page.url or "").lower():
                return {"success": False, "error": "Tee-time page is no longer active. Please fetch tee times again."}
            if search_id is not None and str(search_id) != str(ctx.get("search_id") or ""):
                return {"success": False,
                        "error": "Search context changed. Please fetch tee times again."}

            # Defense-in-depth: the pooled session's glf109e page reflects the
            # LAST search. If the frontend is now asking to book against a
            # different date / golfers / party size than that search, refuse —
            # otherwise we would book the stale row while the confirmation sheet
            # and email describe the golfer's newer selection.
            if expected_date is not None and str(expected_date).strip() != str(ctx.get("date_str", "")).strip():
                return {"success": False,
                        "error": "Your date changed since you searched. Please fetch tee times again."}
            if expected_num_golfers is not None:
                try:
                    if int(expected_num_golfers) != int(ctx.get("num_golfers", 0)):
                        return {"success": False,
                                "error": "Your golfers changed since you searched. Please fetch tee times again."}
                except (TypeError, ValueError):
                    return {"success": False,
                            "error": "Your golfers changed since you searched. Please fetch tee times again."}
            if expected_golfer_ids is not None:
                want = sorted(str(g).strip() for g in expected_golfer_ids if str(g).strip())
                have = sorted(str(g).strip() for g in (ctx.get("golfer_ids") or []))
                if want != have:
                    return {"success": False,
                            "error": "Your golfers changed since you searched. Please fetch tee times again."}
            if (expected_course_type is not None
                    and str(expected_course_type).strip() != str(ctx.get("course_type") or "")):
                return {"success": False,
                        "error": "Your course type changed since you searched. Please fetch tee times again."}
            if (expected_has_guests is not None
                    and bool(expected_has_guests) != bool(ctx.get("has_guests"))):
                return {"success": False,
                        "error": "Your guest selection changed since you searched. Please fetch tee times again."}

            offered = {
                (str(course or ""), str(slot_time or ""))
                for course, slot_time in (ctx.get("offered_times") or [])
            }
            if (course_name, time_str) not in offered:
                return {"success": False,
                        "error": "That tee time was not part of this search. Please fetch tee times again."}

            # Click the course/time row on glf109e
            rows = page.query_selector_all("table tr")
            clicked = False
            for row in rows:
                cells = row.query_selector_all("td")
                if len(cells) < 3:
                    continue
                course = _normalize_course_label(cells[1].inner_text())
                time   = cells[2].inner_text().strip()
                if course_name == course and time == time_str:
                    link = cells[0].query_selector("a")
                    if link:
                        link.click()
                        page.wait_for_url("**/glf109y**", timeout=10000)
                        clicked = True
                        break

            if not clicked:
                self._close_active()
                return {"success": False, "error": "Could not find that tee time — it may have just been taken."}

            # Allocate golfers on glf109y and find the row number
            site_time = f"{time_str.replace(':', '')}01"
            alloc_row_num = None
            table_rows = page.query_selector_all("table tr")
            for tr in table_rows:
                hidden = tr.query_selector('input[type="hidden"]')
                hidden_val = (hidden.get_attribute("value") or "").strip() if hidden else ""
                text_input = tr.query_selector('input[type="Text"], input[type="text"]')
                if hidden_val == site_time and text_input:
                    text_input.fill(str(ctx.get("num_golfers", 1)))
                    # Extract row number from input name (e.g. "allo46" → 46)
                    allo_name = text_input.get_attribute("name") or ""
                    alloc_row_num = int(allo_name.replace("allo", ""))
                    break

            if alloc_row_num is None:
                self._close_active()
                return {"success": False, "error": "Could not find that tee time — it may have just been taken."}

            # Set the boundary before invoking submit: evaluate itself can fail
            # after the browser has already sent the form.
            submission_attempted = True
            page.evaluate(
                """({rowNum, numGolfers}) => {
                    const form = document.golfers;
                    form.pick.value = form.pickin.value.substr((rowNum - 1) * 7, 7) + String(numGolfers);
                    form.rows.value = '1';
                    form.crshl.value = 'x';
                    form.cfmt.value = 'x';
                    form.availw.value = 'x';
                    form.submit();
                }""",
                {"rowNum": alloc_row_num, "numGolfers": ctx.get("num_golfers", 1)},
            )
            # The form is now submitted. Everything below is confirmation, NOT
            # commit \u2014 so a timeout or a differently-worded confirmation page
            # does NOT mean the booking failed. Never report a plain failure
            # here without first checking whether it actually went through.
            res_no = None
            site_reply = ""
            try:
                page.wait_for_url("**/glf109g**", timeout=30000)
                body = page.inner_text("body")
                res_match = re.search(r"Reservation No\.\s*(\d+)", body)
                if res_match:
                    res_no = res_match.group(1)
                else:
                    site_reply = _site_reply_text(body)
            except PWTimeout:
                logger.warning(
                    "book: glf109g confirmation timed out user=%s time=%s \u2014 reconciling",
                    tvn_username, time_str,
                )
                try:
                    site_reply = _site_reply_text(page.inner_text("body"))
                except Exception:
                    site_reply = ""

            # If we didn't capture a real reservation number (slow confirmation,
            # reworded page, or a silent commit), reconcile against the live
            # reservations list before deciding success/failure.
            if not res_no:
                logger.warning(
                    "book: no reservation number in the Villages reply user=%s "
                    "course=%s time=%s reply=%r",
                    tvn_username, course_name, time_str, site_reply,
                )
                return self._reconcile_booking_outcome(
                    page, tvn_username, tvn_password, golf_password,
                    ctx, course_name, time_str, site_reply=site_reply,
                )

            return self._booking_success(res_no, course_name, time_str, ctx)

        except PWTimeout:
            if submission_attempted and page is not None:
                return self._reconcile_booking_outcome(
                    page, tvn_username, tvn_password, golf_password,
                    ctx, course_name, time_str,
                )
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            if submission_attempted and page is not None:
                return self._reconcile_booking_outcome(
                    page, tvn_username, tvn_password, golf_password,
                    ctx, course_name, time_str,
                )
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception(
                "book_tee_time failed for user=%s course=%s time=%s",
                tvn_username,
                course_name,
                time_str,
            )
            if submission_attempted and page is not None:
                return self._reconcile_booking_outcome(
                    page, tvn_username, tvn_password, golf_password,
                    ctx, course_name, time_str,
                )
            self._close_active()
            return {"success": False, "error": "Booking failed. Please try again."}

    # ── Requests (Requests and Templates feature) ─────────────────────────────
    # Note: Single Group only (max 4 golfers). Templates feature deferred.

    def _nav_to_requests_landing(self, page, tvn_username=None, tvn_password=None,
                                 golf_password=None):
        """Navigate to the Requests landing page from the golf main menu.

        Self-healing: a prior request action (e.g. fetch_request_courses) can
        leave the session stranded on an intermediate page (glf105b, etc.)
        where the "Requests and Templates" link is absent. Rather than hang on
        a 10s click timeout, detect the missing link and recover to the main
        menu first (re-running the PIN login) when credentials are available.
        """
        logger.info("requests.nav_landing url=%s", page.url)
        link = page.locator("a", has_text="Requests and Templates")
        if link.count() == 0:
            logger.info("requests.nav_landing.recover url=%s", page.url)
            if tvn_username and tvn_password and golf_password:
                # Force a clean return to the main menu (glf100).
                self._login(page, tvn_username, tvn_password, golf_password)
                link = page.locator("a", has_text="Requests and Templates")
            if link.count() == 0:
                raise RuntimeError(
                    "Could not reach the Requests page. Please try again."
                )
        with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            link.first.click(timeout=10000)
        logger.info("requests.nav_landing.done url=%s", page.url)

    def _open_create_new_request(self, page):
        """From the Requests landing page, click 'Create New Request'."""
        logger.info("requests.open_form url=%s", page.url)
        with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            page.locator("a", has_text="Create New Request").first.click(timeout=10000)
        logger.info("requests.open_form.done url=%s", page.url)

    def _format_site_time(self, t):
        """Convert "12:00" / "9:00" to the 4-digit format the site accepts (1200, 0900).
        Returns empty string for empty input."""
        if not t:
            return ""
        digits = "".join(ch for ch in str(t) if ch.isdigit())
        if not digits:
            return ""
        if len(digits) == 3:
            digits = "0" + digits
        return digits[:4].zfill(4)

    def _request_date_value(self, play_date):
        """Convert UI date strings to the request form's YYYYMMDD option value."""
        raw = str(play_date or "").strip()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
        if m:
            month, day, year = m.groups()
            return f"{year}{int(month):02d}{int(day):02d}"
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
        if m:
            year, month, day = m.groups()
            return f"{year}{int(month):02d}{int(day):02d}"
        return raw

    def _scrape_available_courses(self, page):
        """On the course selection page, return the course labels from the
        'Available Courses' listbox, split into open vs closed.

        The page greys out (inline color #999999) courses that are closed or
        have nothing available for the chosen date; its own move_item() JS
        refuses to move those and pops an alert. Mirror that exact check here
        so we never offer a course the site will reject."""
        # The course-selection page has two <select> elements (multiple).
        # The first is "Available Courses", the second is "Selected Courses".
        result = page.evaluate("""() => {
            const selects = Array.from(document.querySelectorAll('select'));
            const multi = selects.filter(s => s.multiple);
            const src = multi.length > 0 ? multi[0] : selects[0];
            if (!src) return {open: [], closed: []};
            const open = [], closed = [];
            for (const o of src.options) {
                const label = (o.innerText || o.value || '').trim();
                if (!label) continue;
                const c = (o.style.color || '').replace(/\\s+/g, '').toLowerCase();
                const grey = c === 'rgb(153,153,153)' || c === '#999999';
                (grey ? closed : open).push(label);
            }
            return {open, closed};
        }""") or {"open": [], "closed": []}
        return result

    def _fill_request_form(self, page, play_date, max_golfers, has_guests,
                           course_type, any_course, time_to_play,
                           earliest_time, latest_time, preference):
        """Fill the first request form (Play Date, Max Golfers, etc.)."""
        filled = page.evaluate(
            """(values) => {
                const form = document.forms.reqdta;
                if (!form) return false;
                const eventOpts = { bubbles: true };
                const fire = el => {
                    el.dispatchEvent(new Event('input', eventOpts));
                    el.dispatchEvent(new Event('change', eventOpts));
                };
                const setValue = (name, value) => {
                    const field = form.elements[name];
                    if (!field) return false;
                    field.value = String(value);
                    fire(field);
                    return true;
                };
                const setRadio = (name, value) => {
                    const radios = Array.from(form.querySelectorAll(`input[name="${name}"]`));
                    if (!radios.length) return false;
                    const radio = radios.find(r => r.value === value);
                    if (!radio) return false;
                    radio.checked = true;
                    fire(radio);
                    return true;
                };

                const ok = [];
                ok.push(setValue('playdate', values.playDate));
                ok.push(setValue('noofglf', values.maxGolfers));
                ok.push(setRadio('anygsts', values.hasGuests ? 'Y' : 'N'));
                ok.push(setValue('crstype', values.courseTypeCode));
                ok.push(setRadio('anycrs', values.anyCourse ? 'Y' : 'N'));
                ok.push(setValue('exactt', values.timeToPlay));
                ok.push(setValue('strtt', values.earliestTime));
                ok.push(setValue('latet', values.latestTime));
                ok.push(setRadio('pref', values.preference === 'Course' ? 'C' : 'T'));
                return ok.every(Boolean);
            }""",
            {
                "playDate": self._request_date_value(play_date),
                "maxGolfers": max_golfers,
                "hasGuests": has_guests,
                "courseTypeCode": _COURSE_TYPE_CODES.get(course_type, "01"),
                "anyCourse": any_course,
                "timeToPlay": self._format_site_time(time_to_play) or "1100",
                "earliestTime": self._format_site_time(earliest_time) or "0700",
                "latestTime": self._format_site_time(latest_time) or "0600",
                "preference": preference if preference in ("Course", "Time") else "Course",
            },
        )
        if not filled:
            raise RuntimeError("Request form layout was not recognized.")

        with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            page.evaluate("document.forms.reqdta.submit()")

    def _select_request_courses(self, page, course_choices):
        """On glf105b, move the chosen courses from the 'Available Courses' list
        (lwin) into the 'Selected Courses' list (rwin), in preference order.

        The page is a dual-listbox: you select options in lwin and click the
        " >> " button (onclick=move_item(lwin,rwin,'lr')) to append them to rwin.
        Finalize (redirect(form, rwin, 2)) reads rwin, so the courses must
        actually land there. We move one course at a time so rwin's order
        matches the user's stated preference. course_choices are labels exactly
        as they appear in lwin (that's what fetch_request_courses scraped)."""
        if not course_choices:
            return
        form = page.locator('form[name="f1"]')
        if (page.locator('select[name="lwin"]').count() == 0
                or page.locator('select[name="rwin"]').count() == 0):
            raise RuntimeError("Course selection page layout unexpected.")
        move_btn = page.locator('input[onclick^="move_item"]').first
        if move_btn.count() == 0:
            raise RuntimeError("Course move control not found on the request page.")

        # The page's move_item() refuses greyed-out (closed / nothing
        # available) courses and pops an alert. Capture it so the golfer gets
        # the site's real reason instead of a generic failure; without a
        # listener Playwright silently dismisses the alert.
        alerts = []

        def _on_dialog(dialog):
            alerts.append(dialog.message)
            dialog.accept()

        page.on("dialog", _on_dialog)
        try:
            for label in course_choices:
                # Select exactly this one option in lwin (by exact visible
                # text), deselecting the rest, then click the page's own
                # >> mover.
                found = page.evaluate(
                    """(target) => {
                        const lwin = document.forms.f1.lwin;
                        let hit = false;
                        for (const o of lwin.options) {
                            const m = (o.innerText || o.value || '').trim() === target;
                            o.selected = m;
                            if (m) hit = true;
                        }
                        return hit;
                    }""",
                    label,
                )
                if not found:
                    logger.error("requests.course_not_in_list label=%r", label)
                    raise RuntimeError(
                        f"'{label}' was not in the available course list. "
                        "Please reload courses and try again."
                    )
                before = page.evaluate("() => document.forms.f1.rwin.options.length")
                move_btn.click()
                page.wait_for_timeout(150)  # let move_item + any alert settle
                after = page.evaluate("() => document.forms.f1.rwin.options.length")
                if after <= before:
                    reason = alerts[-1].strip() if alerts else (
                        f"The Villages site would not add '{label}'."
                    )
                    logger.error("requests.move_refused label=%r alert=%r",
                                 label, alerts[-1] if alerts else None)
                    raise RuntimeError(
                        f"{reason} Remove it from your course choices "
                        "(or reload the course list) and try again."
                    )
        finally:
            page.remove_listener("dialog", _on_dialog)

        # Verify rwin now holds every chosen course before finalizing.
        selected = page.evaluate(
            "() => Array.from(document.forms.f1.rwin.options)"
            ".map(o => (o.innerText || '').trim())"
        )
        if len(selected) < len(course_choices):
            logger.error("requests.rwin_short rwin=%s wanted=%s", selected, course_choices)
            raise RuntimeError("Could not add all chosen courses. Please try again.")

    def _fill_request_golfers(self, page, golfer_ids):
        """On glf105b, select every requested golfer and verify the result.

        Each buddyN <select> fires onchange=sel2inp(...) which writes the
        golfer's ID into the hidden 'glfers' field the backend reads, so the
        golfer must be chosen THROUGH the dropdown (select_option fires change),
        not by writing 'glfers' directly. Submission is deliberately performed
        by the caller after this method verifies all slots."""
        ids = [str(g).strip() for g in (golfer_ids or []) if str(g).strip()]
        if not ids:
            raise RuntimeError("No golfer IDs provided for request.")
        if len(ids) > 4:
            raise RuntimeError("A request can include at most four golfers.")
        if any(not gid.isdigit() for gid in ids):
            raise RuntimeError("Golfer IDs must contain only numbers.")
        if len(set(ids)) != len(ids):
            raise RuntimeError("Each golfer can be selected only once.")

        selected = []
        for idx, gid in enumerate(ids):
            sel = page.locator(f'select[name="buddy{idx + 1}"]')
            if sel.count() == 0:
                raise RuntimeError(f"Golfer slot {idx + 1} was not available on the request page.")
            try:
                sel.first.select_option(value=gid)  # onchange -> sel2inp -> glfers
                actual = sel.first.input_value()
                if str(actual).strip() != gid:
                    raise RuntimeError("selection did not stick")
                selected.append(gid)
            except Exception:
                logger.warning(
                    "requests.golfer_not_in_dropdown id=%s slot=%s", gid, idx + 1
                )
                raise RuntimeError(
                    f"Golfer {gid} was not available in request slot {idx + 1}."
                )
        if selected != ids:
            raise RuntimeError("Could not select every requested golfer.")

    def _submit_request_selection(self, page):
        """Cross the request commit boundary after all selections validate."""
        with page.expect_navigation(wait_until="domcontentloaded", timeout=25000):
            page.locator('input[name="but2"]').first.click()

    def _extract_requests(self, page):
        """Parse a verified requests landing page; empty means verified empty."""
        payload = page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('table tr')).map(row => {
                const cells = Array.from(row.querySelectorAll('td')).map(c =>
                    (c.innerText || '').replace(/\\s+/g, ' ').trim()
                );
                const links = Array.from(row.querySelectorAll('a')).map(a => ({
                    text: (a.innerText || '').replace(/\\s+/g, ' ').trim(),
                    href: a.getAttribute('href') || '',
                }));
                return {cells, links};
            }).filter(row => row.cells.some(Boolean));
            const body = (document.body && document.body.innerText || '');
            const valid = /glf105a/i.test(location.pathname + location.search) &&
                /(requests and templates|create new request|no (?:open |pending )?requests)/i.test(body);
            return {valid, rows};
        }""")
        if not isinstance(payload, dict) or not payload.get("valid"):
            raise RuntimeError("The requests page layout was not recognized.")

        requests_out = []
        for row in payload.get("rows") or []:
            cells = row.get("cells") or []
            links = row.get("links") or []
            if len(cells) < 3:
                continue
            action, name, request_date = (str(cells[i] or "").strip() for i in range(3))
            if not action or "create new" in action.lower() or "action" in action.lower():
                continue
            rid = None
            joined = " ".join([action, name] + [link.get("href", "") for link in links])
            match = re.search(r"(?:Request\s*(?:No\.?)?\s*)?(?<!\d)(\d{5,})(?!\d)", joined, re.IGNORECASE)
            if match:
                rid = match.group(1)
            if rid:
                requests_out.append({
                    "request_id": rid,
                    "action": action,
                    "name": name,
                    "date": request_date,
                })
        return requests_out

    def _extract_request_confirmation(self, page):
        """On the confirmation page, scrape the Request No."""
        body = page.inner_text("body")
        m = re.search(r"Request\s*No\.?\s*[:\-]?\s*(\d+)", body, re.IGNORECASE)
        return m.group(1) if m else None

    def _reconcile_new_request(self, page, tvn_username, tvn_password,
                               golf_password, preexisting_ids):
        if preexisting_ids is None:
            return None
        try:
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(
                page, tvn_username, tvn_password, golf_password
            )
            requests_out = self._extract_requests(page)
        except Exception:
            logger.exception("request submit reconciliation failed user=%s", tvn_username)
            return None
        new_ids = [
            item["request_id"] for item in requests_out
            if item.get("request_id") not in preexisting_ids
        ]
        return new_ids[0] if len(new_ids) == 1 else None

    def _reconcile_request_absent(self, page, tvn_username, tvn_password,
                                  golf_password, request_id):
        try:
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(
                page, tvn_username, tvn_password, golf_password
            )
            requests_out = self._extract_requests(page)
        except Exception:
            logger.exception(
                "request delete reconciliation failed user=%s request=%s",
                tvn_username, request_id,
            )
            return None, None
        exists = any(item.get("request_id") == request_id for item in requests_out)
        return not exists, requests_out

    def fetch_request_courses(self, tvn_username, tvn_password, golf_password,
                              play_date, course_type="Championship",
                              any_course=False):
        """Open the request form just far enough to scrape the available
        courses for a given date + course type, then navigate back to the menu.
        Returns {success, courses: [labels]}."""
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(page, tvn_username, tvn_password, golf_password)
            self._open_create_new_request(page)
            self._fill_request_form(
                page,
                play_date=play_date,
                max_golfers=1,
                has_guests=False,
                course_type=course_type,
                any_course=any_course,
                time_to_play="",
                earliest_time="",
                latest_time="",
                preference="Course",
            )
            scraped = self._scrape_available_courses(page)
            courses = scraped["open"]
            closed_courses = scraped["closed"]
            # Return to a known-good main menu so the next request action starts
            # clean. The "Back to the Menu" link is unreliable and can strand the
            # session on an intermediate page (glf105b), so verify we actually
            # landed on the menu and re-login if not.
            try:
                page.locator("a", has_text="Back to the Menu").first.click(timeout=3000)
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            if "glf100" not in (page.url or "").lower():
                self._login(page, tvn_username, tvn_password, golf_password)
            self._active.ts = _time.time()
            return {"success": True, "courses": courses,
                    "closed_courses": closed_courses}
        except PWTimeout:
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("fetch_request_courses failed for user=%s", tvn_username)
            self._close_active()
            return {"success": False, "error": "Could not load course list. Please try again."}

    def submit_request(self, tvn_username, tvn_password, golf_password,
                       play_date, max_golfers, has_guests, course_type,
                       any_course, time_to_play, earliest_time, latest_time,
                       preference, course_choices, golfer_ids):
        """Submit a tee-time request. Returns {success, request_no}."""
        submission_attempted = False
        page = None
        preexisting_ids = None
        try:
            ids = [str(g).strip() for g in (golfer_ids or []) if str(g).strip()]
            try:
                party_limit = int(max_golfers)
            except (TypeError, ValueError):
                party_limit = 0
            if party_limit < 1 or party_limit > 4:
                return {"success": False, "error": "Max golfers must be between 1 and 4."}
            if len(ids) > party_limit:
                return {"success": False,
                        "error": "Selected golfers cannot exceed max golfers."}
            if not ids or len(ids) > 4 or len(set(ids)) != len(ids) or any(not gid.isdigit() for gid in ids):
                return {"success": False,
                        "error": "Golfer IDs must be unique numeric values, with at most four selected."}

            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(page, tvn_username, tvn_password, golf_password)
            before = self._extract_requests(page)
            preexisting_ids = {item["request_id"] for item in before}
            self._open_create_new_request(page)
            self._fill_request_form(
                page,
                play_date=play_date,
                max_golfers=max_golfers,
                has_guests=has_guests,
                course_type=course_type,
                any_course=any_course,
                time_to_play=time_to_play,
                earliest_time=earliest_time,
                latest_time=latest_time,
                preference=preference,
            )
            self._select_request_courses(page, course_choices)
            self._fill_request_golfers(page, ids)
            submission_attempted = True
            self._submit_request_selection(page)
            request_no = self._extract_request_confirmation(page)
            self._active.ts = _time.time()
            if not request_no:
                request_no = self._reconcile_new_request(
                    page, tvn_username, tvn_password, golf_password,
                    preexisting_ids,
                )
            if not request_no:
                self._close_active()
                return _unknown_result(
                    "We could not confirm whether the request was submitted. "
                    "Open My Requests and verify before trying again."
                )
            return {
                "success": True,
                "request_no": request_no,
                "message": f"Request #{request_no} submitted",
            }
        except PWTimeout:
            if submission_attempted and page is not None:
                request_no = self._reconcile_new_request(
                    page, tvn_username, tvn_password, golf_password,
                    preexisting_ids,
                )
                if request_no:
                    return {"success": True, "request_no": request_no,
                            "message": f"Request #{request_no} submitted"}
                self._close_active()
                return _unknown_result(
                    "We could not confirm whether the request was submitted. "
                    "Open My Requests and verify before trying again."
                )
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            if submission_attempted and page is not None:
                request_no = self._reconcile_new_request(
                    page, tvn_username, tvn_password, golf_password,
                    preexisting_ids,
                )
                if request_no:
                    return {"success": True, "request_no": request_no,
                            "message": f"Request #{request_no} submitted"}
                self._close_active()
                return _unknown_result(
                    "We could not confirm whether the request was submitted. "
                    "Open My Requests and verify before trying again."
                )
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("submit_request failed for user=%s", tvn_username)
            if submission_attempted and page is not None:
                request_no = self._reconcile_new_request(
                    page, tvn_username, tvn_password, golf_password,
                    preexisting_ids,
                )
                if request_no:
                    return {"success": True, "request_no": request_no,
                            "message": f"Request #{request_no} submitted"}
                self._close_active()
                return _unknown_result(
                    "We could not confirm whether the request was submitted. "
                    "Open My Requests and verify before trying again."
                )
            self._close_active()
            return {"success": False, "error": "Could not submit request. Please try again."}

    def view_my_requests(self, tvn_username, tvn_password, golf_password):
        """Scrape pending requests from the Requests landing page."""
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(page, tvn_username, tvn_password, golf_password)
            requests_out = self._extract_requests(page)

            self._active.ts = _time.time()
            return {"success": True, "verified": True, "requests": requests_out}
        except PWTimeout:
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("view_my_requests failed for user=%s", tvn_username)
            self._close_active()
            return {"success": False, "error": "Could not load your requests. Please try again."}

    def delete_request(self, tvn_username, tvn_password, golf_password, target_request_id):
        """Cancel a pending request by ID.

        The Villages delete is TWO steps: on the landing page (glf105a) the
        row's 'Delete this Request' link opens a confirmation page (glf105e),
        where the 'Delete This Request' anchor finalizes it (glf105f). We match
        the row by exact request number (not loose substring) and pick the
        delete link specifically (never 'View'/'Change'), then confirm we are on
        the right request before finalizing."""
        submission_attempted = False
        page = None
        target = str(target_request_id or "").strip()

        def reconciled_result():
            absent, requests_out = self._reconcile_request_absent(
                page, tvn_username, tvn_password, golf_password, target
            )
            if absent is True:
                return {"success": True, "verified": True, "request_id": target,
                        "requests": requests_out, "message": f"Request {target} canceled"}
            if absent is False:
                return {"success": False,
                        "error": "Request still appears on the site after delete attempt."}
            self._close_active()
            return _unknown_result(
                "We could not confirm whether the request was canceled. "
                "Open My Requests and verify before trying again."
            )

        try:
            if not target.isdigit():
                return {"success": False, "error": "A valid request number is required."}
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(page, tvn_username, tvn_password, golf_password)
            current = self._extract_requests(page)
            if not any(item.get("request_id") == target for item in current):
                return {"success": False, "error": "Request not found."}

            found = page.evaluate("""(rid) => {
                const rows = Array.from(document.querySelectorAll('table tr'));
                const tok = new RegExp('(^|\\\\D)' + rid + '(\\\\D|$)');
                for (const row of rows) {
                    const cells = Array.from(row.querySelectorAll('td')).map(c => (c.innerText || '').trim());
                    const links = Array.from(row.querySelectorAll('a'));
                    const identityText = cells.join(' | ') + ' | ' + links.map(a => a.href || '').join(' | ');
                    if (!tok.test(identityText)) continue;
                    const del = links.find(a => /^delete\\s+this\\s+request$/i.test(
                        (a.innerText || '').replace(/\\s+/g, ' ').trim()
                    ));
                    if (del) { del.setAttribute('data-del-target', rid); return true; }
                }
                return false;
            }""", target)
            if not found:
                return {"success": False, "error": "Delete action is not available for this request."}

            with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                page.locator(f'a[data-del-target="{target}"]').first.click(timeout=8000)

            confirm_text = _normalize_space(page.inner_text("body"))
            confirm_match = re.search(
                r"Request\s*(?:No\.?)?\s*[:\-]?\s*(\d+)", confirm_text, re.IGNORECASE
            )
            if not confirm_match or confirm_match.group(1) != target:
                return {"success": False,
                        "error": "The delete confirmation did not identify the requested request."}

            confirm_link = page.locator("a", has_text="Delete This Request")
            if confirm_link.count() == 0:
                return {"success": False, "error": "Final request delete control was not found."}
            submission_attempted = True
            with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                confirm_link.first.click(timeout=8000)
            return reconciled_result()
        except PWTimeout:
            if submission_attempted and page is not None:
                return reconciled_result()
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            if submission_attempted and page is not None:
                return reconciled_result()
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("delete_request failed for user=%s", tvn_username)
            if submission_attempted and page is not None:
                return reconciled_result()
            self._close_active()
            return {"success": False, "error": "Could not cancel that request. Please try again."}
