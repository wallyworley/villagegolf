"""
Villages Golf Service
Uses Playwright (headless Chromium) to automate the thevillages.net
tee-time booking system. Mirrors the exact flow used in the live session.
"""

import os
import re
import time as _time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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
class GolfService:
    # Keep one browser/page alive for the active user until an actual failure
    # or user switch requires a reset.
    _SESSION_TTL = None

    def __init__(self):
        self._pw          = None
        self._browser     = None
        self._page        = None
        self._session_ts  = 0
        self._current_user = None  # tvn_username of cached session
        self._search_context = None
        self._golf_home_url = None  # stored after login to glf100

    def _close_session(self):
        """Close any cached browser session."""
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._page = None
        self._session_ts = 0
        self._current_user = None
        self._search_context = None

    def _get_or_create_session(self, tvn_username):
        """Return a (browser, page) reusing the cached session if still valid
        and belongs to the same user."""
        if (self._page and self._browser
                and self._current_user == tvn_username):
            return self._browser, self._page
        # Stale, missing, or different user — start fresh
        self._close_session()
        self._pw = sync_playwright().start()
        self._browser, self._page = self._launch(self._pw)
        self._session_ts = _time.time()
        self._current_user = tvn_username
        return self._browser, self._page

    def _launch(self, playwright):
        browser = playwright.chromium.launch(
            headless=_HEADLESS,
            slow_mo=600 if not _HEADLESS else 0,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        return browser, ctx.new_page()

    def _login(self, page, tvn_username, tvn_password, golf_password):
        # Step 1: TVN login
        page.goto("https://www.thevillages.net/", wait_until="networkidle", timeout=20000)
        page.locator('input[type="text"]').first.fill(tvn_username)
        page.locator('input[type="password"]').first.fill(tvn_password)
        page.locator('input[type="image"]').click()
        page.wait_for_load_state("networkidle", timeout=15000)

        if "login" in page.url.lower():
            raise RuntimeError("TVN login failed — check username / password")

        # Step 2: Golf system login
        page.locator("a", has_text="GOLF").first.click()
        page.wait_for_url("**/glf000**", timeout=12000)
        page.locator("input:visible").first.fill(golf_password)
        page.locator('input[value="Continue"]').click()
        page.wait_for_url("**/glf100**", timeout=12000)
        self._golf_home_url = page.url
        self._session_ts = _time.time()

    def _navigate_to_golf_home(self, page):
        """Navigate back to glf100 from any golf page."""
        url = page.url or ""
        if "glf100" in url.lower():
            return
        if self._golf_home_url:
            page.goto(self._golf_home_url, wait_until="networkidle", timeout=15000)
            return
        # Fallback: derive glf100 URL from current URL
        import re as _re
        m = _re.search(r'(https?://[^/]+/)', url)
        if m:
            page.goto(m.group(1) + "glf100", wait_until="networkidle", timeout=15000)
            self._golf_home_url = page.url
        else:
            raise RuntimeError("Cannot navigate to golf home — session may be invalid")

    def _ensure_logged_in(self, page, tvn_username, tvn_password, golf_password):
        """Avoid re-authentication if an active golf page is already open.
        Always ensures we end up on glf100."""
        url = (page.url or "").lower()
        if "thevillages.net" in url and "/glf" in url and "glf000" not in url:
            self._navigate_to_golf_home(page)
            self._session_ts = _time.time()
            return
        self._login(page, tvn_username, tvn_password, golf_password)

    def _nav_to_glf109c(self, page, num_golfers, has_guests, course_type):
        """Navigate from glf100 through glf109b to glf109c."""
        # glf109a
        page.locator("a", has_text="Reservations-View Open Tee Times").click()
        page.wait_for_url("**/glf109a**", timeout=10000)

        # glf109b
        page.locator("a", has_text="Create New Reservation").click()
        page.wait_for_url("**/glf109b**", timeout=10000)

        page.locator('table input[type="text"]').first.fill(str(num_golfers))
        page.locator(f'input[value="{"Y" if has_guests else "N"}"]').check()
        page.locator("select").select_option(_COURSE_TYPE_CODES.get(course_type, "02"))
        page.locator("a", has_text="Continue to Enter Golfers").click()
        page.wait_for_url("**/glf109c**", timeout=10000)

    def _setup_reservation(self, page, num_golfers, has_guests, course_type,
                           golfer_ids, date_str):
        self._nav_to_glf109c(page, num_golfers, has_guests, course_type)

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
        self._session_ts = _time.time()

    def _extract_times(self, page, time_filter=None):
        rows = page.query_selector_all("table tr")
        times = []
        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) < 4:
                continue
            course = cells[1].inner_text().strip().split("\n")[0]
            time   = cells[2].inner_text().strip()
            avail  = cells[3].inner_text().strip()
            link   = cells[0].query_selector("a")
            if not (course and time and link):
                continue
            if time_filter and not _matches_filter(time, time_filter):
                continue
            times.append({
                "course":       course,
                "time":         time,
                "display_time": _display_time(time),
                "available":    avail,
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
                options = golfer_select.locator("option").all()
                for opt in options:
                    value = (opt.get_attribute("value") or "").strip()
                    raw_text = opt.inner_text().strip()
                    # Skip placeholder options (empty value or non-numeric)
                    if not value or not value.isdigit():
                        continue
                    clean_name = _clean_golfer_name(raw_text, value)
                    entry = {"id": value, "name": clean_name, "initials": _initials(clean_name)}
                    buddies.append(entry)
                # First real option is the primary golfer (the logged-in user)
                if buddies:
                    primary = buddies[0]

            # Navigate back to golf home so session is reusable for tee-time searches
            if self._golf_home_url:
                page.goto(self._golf_home_url, wait_until="networkidle", timeout=15000)

            return {"success": True, "primary": primary, "buddies": buddies}

        except PWTimeout as e:
            self._close_session()
            return {"success": False, "error": f"Page timed out: {e}"}
        except RuntimeError as e:
            self._close_session()
            return {"success": False, "error": str(e)}
        except Exception as e:
            self._close_session()
            return {"success": False, "error": f"Unexpected error: {e}"}

    def get_available_times(self, tvn_username, tvn_password, golf_password,
                            date_str, date_label, course_type,
                            golfer_ids, num_golfers, has_guests, time_filter=None):
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._setup_reservation(page, num_golfers, has_guests, course_type, golfer_ids, date_str)
            times = self._extract_times(page, time_filter)
            self._search_context = {
                "user": tvn_username,
                "date_str": str(date_str or "").strip(),
                "course_type": str(course_type or "").strip(),
                "golfer_ids": [str(g).strip() for g in (golfer_ids or []) if str(g).strip()],
                "num_golfers": int(num_golfers),
                "has_guests": bool(has_guests),
            }
            self._session_ts = _time.time()  # refresh TTL
            return {"success": True, "times": times, "date_label": date_label}
        except PWTimeout as e:
            self._close_session()
            return {"success": False, "error": f"Page timed out: {e}"}
        except RuntimeError as e:
            self._close_session()
            return {"success": False, "error": str(e)}
        except Exception as e:
            self._close_session()
            return {"success": False, "error": f"Unexpected error: {e}"}

    def book_tee_time(self, tvn_username, tvn_password, golf_password, course_name, time_str):
        try:
            page = self._page
            ctx = self._search_context or {}

            # Booking must reuse the exact session built by get_available_times().
            # Do not silently rebuild with defaults; require user to refetch if stale.
            if page is None:
                return {"success": False, "error": "Session expired. Please fetch tee times again."}
            if self._current_user != tvn_username:
                return {"success": False, "error": "Selected golfer changed. Please fetch tee times again."}
            if ctx.get("user") != tvn_username:
                return {"success": False, "error": "Search context changed. Please fetch tee times again."}
            if "glf109e" not in (page.url or "").lower():
                return {"success": False, "error": "Tee-time page is no longer active. Please fetch tee times again."}

            # Click the course/time row on glf109e
            rows = page.query_selector_all("table tr")
            clicked = False
            for row in rows:
                cells = row.query_selector_all("td")
                if len(cells) < 3:
                    continue
                course = cells[1].inner_text().strip().split("\n")[0]
                time   = cells[2].inner_text().strip()
                if course_name in course and time == time_str:
                    link = cells[0].query_selector("a")
                    if link:
                        link.click()
                        page.wait_for_url("**/glf109y**", timeout=10000)
                        clicked = True
                        break

            if not clicked:
                self._close_session()
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
                self._close_session()
                return {"success": False, "error": "Could not find that tee time — it may have just been taken."}

            # Replicate the redirect() function's submit logic
            page.evaluate(f"""
                const form = document.golfers;
                const rowNum = {alloc_row_num};
                form.pick.value = form.pickin.value.substr((rowNum - 1) * 7, 7) + '{ctx.get("num_golfers", 1)}';
                form.rows.value = '1';
                form.crshl.value = 'x';
                form.cfmt.value = 'x';
                form.availw.value = 'x';
                form.submit();
            """)
            page.wait_for_url("**/glf109g**", timeout=30000)

            body = page.inner_text("body")
            res_match = re.search(r"Reservation No\.\s*(\d+)", body)
            res_no = res_match.group(1) if res_match else "\u2014"

            return {
                "success":        True,
                "reservation_no": res_no,
                "course":         course_name,
                "time":           time_str,
                "display_time":   _display_time(time_str),
                "message":        f"Booked! Reservation #{res_no} \u2014 {course_name} at {_display_time(time_str)}",
            }

        except PWTimeout as e:
            self._close_session()
            return {"success": False, "error": f"Page timed out: {e}"}
        except RuntimeError as e:
            self._close_session()
            return {"success": False, "error": str(e)}
        except Exception as e:
            self._close_session()
            return {"success": False, "error": f"Booking failed: {e}"}
