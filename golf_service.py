"""
Villages Golf Service
Uses Playwright (headless Chromium) to automate the thevillages.net
tee-time booking system. Mirrors the exact flow used in the live session.
"""

import os
import re
import time as _time
import logging

from course_metadata import lookup_course_metadata
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


def _timeout_error():
    return "The Villages golf site took too long to respond. Please try again."


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
    """One logged-in browser session for a single golfer."""
    __slots__ = ("pw", "browser", "context", "page", "ts",
                 "golf_home_url", "search_context")

    def __init__(self):
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.ts = 0
        self.golf_home_url = None   # stored after login to glf100
        self.search_context = None


class GolfService:
    # Keep a small pool of warm sessions, one per golfer, so concurrent users
    # don't evict each other and force a full re-login on every request. All
    # sessions live on the single worker thread (Playwright sync API), so a
    # dict of browsers on one thread is safe. Least-recently-used is evicted
    # when the pool is full.
    _MAX_SESSIONS = int(os.environ.get("MAX_BROWSER_SESSIONS", "4"))

    def __init__(self):
        from collections import OrderedDict
        self._sessions = OrderedDict()   # tvn_username -> _Session
        self._active = None              # the _Session for the in-flight request
        self._active_user = None

    def _close(self, sess):
        """Tear down one session's Playwright objects."""
        if not sess:
            return
        for closer in (
            lambda: sess.context and sess.context.close(),
            lambda: sess.browser and sess.browser.close(),
            lambda: sess.pw and sess.pw.stop(),
        ):
            try:
                closer()
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
        """Return True if a session's Playwright objects still look valid."""
        if not sess or not sess.browser or not sess.page:
            return False
        try:
            if hasattr(sess.browser, "is_connected") and not sess.browser.is_connected():
                return False
            if hasattr(sess.page, "is_closed") and sess.page.is_closed():
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
            return sess.browser, sess.page

        # Stale entry — discard before rebuilding.
        if sess:
            self._drop_session(tvn_username)

        # Evict least-recently-used sessions until there is room.
        while len(self._sessions) >= self._MAX_SESSIONS:
            _, old_sess = self._sessions.popitem(last=False)
            self._close(old_sess)

        sess = _Session()
        sess.pw = sync_playwright().start()
        sess.browser, sess.context, sess.page = self._launch(sess.pw)
        sess.ts = _time.time()
        self._sessions[tvn_username] = sess
        self._active, self._active_user = sess, tvn_username
        return sess.browser, sess.page

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
        return browser, ctx, ctx.new_page()

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

        if "login" in page.url.lower():
            raise RuntimeError("TVN login failed — check username / password")

        # Step 2: Golf system login
        page.locator("a", has_text="GOLF").first.click()
        page.wait_for_url("**/glf000**", timeout=12000)
        page.locator("input:visible").first.fill(golf_password)
        page.locator('input[value="Continue"]').click()
        page.wait_for_url("**/glf100**", timeout=12000)
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

    def _nav_to_glf109c(self, page, num_golfers, has_guests, course_type):
        """Navigate from glf100 through glf109b to glf109c."""
        # glf109a
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
        """Scrape outstanding reservations from glf109a."""
        raw_rows = page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll("table tr"));
            return rows.map(row => {
                const cells = Array.from(row.querySelectorAll("td")).map(cell =>
                    (cell.innerText || "").replace(/\\s+/g, " ").trim()
                );
                const links = Array.from(row.querySelectorAll("a")).map(link =>
                    (link.innerText || "").replace(/\\s+/g, " ").trim()
                ).filter(Boolean);
                return {cells, links};
            }).filter(row => row.cells.some(Boolean));
        }""")

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

            date = None
            time = None
            display_time = None
            course = None
            phone = None

            for line in description_lines:
                date_match = re.search(r"([A-Za-z]+):\s*(\d{1,2}/\d{1,2})\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", line, re.IGNORECASE)
                if date_match:
                    date = f"{date_match.group(1)}: {date_match.group(2)}"
                    display_time = _normalize_space(date_match.group(3).upper())
                    continue
                course_match = re.search(r"Course:\s*(.+)", line, re.IGNORECASE)
                if course_match:
                    course = _normalize_space(course_match.group(1))
                    continue
                phone_match = re.search(r"Phone:\s*(.+)", line, re.IGNORECASE)
                if phone_match:
                    phone = _normalize_space(phone_match.group(1))

            if display_time:
                time = display_time
            if not course:
                course = description

            actions = row.get("links") or []

            reservations.append({
                "reservation_no": reservation_no,
                "date": date,
                "time": time,
                "display_time": display_time or "",
                "course": course,
                "phone": phone,
                "actions": actions,
                "summary": description,
            })

        return reservations

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
                region = (metadata.get("region") or "").strip().lower()
                if region != region_filter:
                    continue
            times.append({
                "course":       course_label,
                "course_name":  canonical_name or course_label,
                "region":       metadata.get("region"),
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
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("fetch_buddy_list failed for user=%s", tvn_username)
            self._close_active()
            return {"success": False, "error": "Could not connect to the golf system. Please try again."}

    def get_available_times(self, tvn_username, tvn_password, golf_password,
                            date_str, date_label, course_type, region_filter,
                            golfer_ids, num_golfers, has_guests, time_filter=None):
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._setup_reservation(page, num_golfers, has_guests, course_type, golfer_ids, date_str)
            times = self._extract_times(page, time_filter, region_filter)
            self._active.search_context = {
                "user": tvn_username,
                "date_str": str(date_str or "").strip(),
                "course_type": str(course_type or "").strip(),
                "region_filter": str(region_filter or "all").strip().lower(),
                "golfer_ids": [str(g).strip() for g in (golfer_ids or []) if str(g).strip()],
                "num_golfers": int(num_golfers),
                "has_guests": bool(has_guests),
            }
            self._active.ts = _time.time()  # refresh TTL
            return {"success": True, "times": times, "date_label": date_label}
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
            return {"success": True, "reservations": reservations}
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
        """Click 'DELETE ALL PLAYERS ON THIS RESERVATION' for a reservation."""
        try:
            _, page = self._get_or_create_session(tvn_username)
            current_url = (page.url or "").lower()
            if "glf109a" not in current_url:
                return {
                    "success": False,
                    "error": "Reservations page is no longer active. Please tap View My Tee Times again before deleting.",
                }

            target_index = page.evaluate("""reservationNo => {
                const rows = Array.from(document.querySelectorAll('table tr'));
                for (let i = 0; i < rows.length; i++) {
                    const cells = rows[i].querySelectorAll('td');
                    if (cells.length < 3) continue;
                    const reservationText = (cells[1].innerText || '').replace(/\\s+/g, ' ').trim();
                    if (reservationText.includes(String(reservationNo))) return i;
                }
                return -1;
            }""", str(reservation_no))

            if target_index < 0:
                return {"success": False, "error": "Reservation not found."}

            clicked = page.evaluate("""rowIndex => {
                const rows = Array.from(document.querySelectorAll('table tr'));
                const row = rows[rowIndex];
                if (!row) return false;
                const links = Array.from(row.querySelectorAll('a'));
                const target = links.find(link =>
                    /delete\\s+all\\s+players\\s+on\\s+this\\s+reservation/i.test((link.innerText || '').trim())
                );
                if (!target) return false;
                target.click();
                return true;
            }""", int(target_index))

            if not clicked:
                return {"success": False, "error": "Delete action is not available for this reservation."}

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                page.wait_for_load_state("domcontentloaded", timeout=5000)

            # Step 2: the site shows a dedicated confirmation page. Stay on the
            # same live session/page and wait specifically for that page before
            # doing anything else. Do not re-login/re-navigate until after the
            # final delete click has been attempted.
            confirm_ready = False
            try:
                page.locator(f"text=Delete Reservation No. {reservation_no}").first.wait_for(timeout=8000)
                confirm_ready = True
            except Exception:
                pass
            if not confirm_ready:
                try:
                    page.locator("text=DO NOT DELETE THIS RESERVATION").first.wait_for(timeout=3000)
                    confirm_ready = True
                except Exception:
                    pass

            if not confirm_ready:
                body_text = _normalize_space(page.inner_text("body"))
                return {
                    "success": False,
                    "error": "Delete confirmation page did not appear after the first delete click.",
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

            clicked_confirm = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a'));
                const target = links.find(link =>
                    (link.innerText || '').replace(/\\s+/g, ' ').trim() === 'DELETE ALL PLAYERS ON THIS RESERVATION'
                );
                if (!target) return false;
                target.scrollIntoView({block: 'center'});
                target.click();
                return true;
            }""")
            if not clicked_confirm:
                confirm_target.click(timeout=5000, force=True)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                page.wait_for_load_state("domcontentloaded", timeout=5000)

            current_url = (page.url or "").lower()
            if "glf109a" not in current_url:
                menu_button = page.locator("input[name='Menu'], input[value='Go to Menu'], input[value='Menu']")
                if menu_button.count() > 0:
                    menu_button.first.click(timeout=5000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    current_url = (page.url or "").lower()
                if "glf109a" not in current_url:
                    return {
                        "success": True,
                        "reservation_no": str(reservation_no),
                        "reservations": None,
                        "needs_refresh": True,
                        "message": "Reservation delete completed. Refreshing reservations is recommended.",
                        "debug_page": _normalize_space(page.inner_text("body"))[:500],
                    }

            remaining = self._extract_reservations(page)
            still_exists = any(str(item.get("reservation_no") or "") == str(reservation_no) for item in remaining)
            if still_exists:
                return {"success": False, "error": "Reservation still appears on the site after delete attempt."}

            self._active.ts = _time.time()
            return {"success": True, "reservation_no": str(reservation_no), "reservations": remaining}
        except PWTimeout:
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception(
                "delete_reservation failed for user=%s reservation_no=%s",
                tvn_username,
                reservation_no,
            )
            self._close_active()
            return {"success": False, "error": "Could not delete that reservation right now. Please try again."}

    def book_tee_time(self, tvn_username, tvn_password, golf_password, course_name, time_str):
        try:
            # Booking must reuse the exact session built by get_available_times().
            # Look it up by user so another golfer's request can't clobber it.
            sess = self._sessions.get(tvn_username)
            if not sess or not self._session_usable(sess):
                return {"success": False, "error": "Session expired. Please fetch tee times again."}
            self._active, self._active_user = sess, tvn_username
            page = sess.page
            ctx = sess.search_context or {}

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

        except PWTimeout:
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception(
                "book_tee_time failed for user=%s course=%s time=%s",
                tvn_username,
                course_name,
                time_str,
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
        link.first.click(timeout=10000)
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        logger.info("requests.nav_landing.done url=%s", page.url)

    def _open_create_new_request(self, page):
        """From the Requests landing page, click 'Create New Request'."""
        logger.info("requests.open_form url=%s", page.url)
        page.locator("a", has_text="Create New Request").first.click(timeout=10000)
        page.wait_for_load_state("domcontentloaded", timeout=10000)
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
        """On the course selection page, return the list of available course
        labels from the 'Available Courses' listbox in the order shown."""
        # The course-selection page has two <select> elements (multiple).
        # The first is "Available Courses", the second is "Selected Courses".
        return page.evaluate("""() => {
            const selects = Array.from(document.querySelectorAll('select'));
            const multi = selects.filter(s => s.multiple);
            const src = multi.length > 0 ? multi[0] : selects[0];
            if (!src) return [];
            return Array.from(src.options).map(o => (o.innerText || o.value || '').trim()).filter(Boolean);
        }""") or []

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
        """On the course selection page, select the user's chosen courses in
        preference order using the >> button. course_choices is a list of
        course labels exactly as they appear in the Available Courses listbox."""
        if not course_choices:
            return
        selects = page.locator("select").all()
        if len(selects) < 2:
            raise RuntimeError("Course selection page layout unexpected.")
        available = selects[0]

        for label in course_choices:
            try:
                available.select_option(label=label)
            except Exception:
                # Try by exact text (the option innerText may include whitespace)
                available.evaluate(
                    "(sel, target) => {"
                    "  for (const o of sel.options) {"
                    "    if ((o.innerText || '').trim() === target) {"
                    "      o.selected = true; sel.dispatchEvent(new Event('change')); return;"
                    "    }"
                    "  }"
                    "}",
                    label,
                )
            # Click the "move into Selected" control. The Villages page label
            # for this varies (>>, >, Add, →, Select), and it may be an
            # input button OR an anchor/link, so match broadly. On failure,
            # surface the actual controls present to aid debugging.
            moved = page.evaluate(
                """() => {
                    const wanted = ['>>', '>', 'add', 'select', '\\u2192', '\\u00bb'];
                    const cands = Array.from(document.querySelectorAll(
                        'input[type="button"], input[type="submit"], button, a'
                    ));
                    const txt = el => ((el.value || el.innerText || el.textContent || '')
                        .trim().toLowerCase());
                    const btn = cands.find(el => {
                        const t = txt(el);
                        return wanted.some(w => t.includes(w.toLowerCase()));
                    });
                    if (!btn) {
                        return {ok: false, found: cands.map(el =>
                            (el.value || el.innerText || el.textContent || '').trim()
                        ).filter(Boolean).slice(0, 20)};
                    }
                    btn.click();
                    return {ok: true};
                }"""
            )
            if not moved.get("ok"):
                logger.error(
                    "requests.move_button_not_found controls=%s",
                    moved.get("found"),
                )
                raise RuntimeError(
                    "Could not add the course to your selection. Please try again."
                )

    def _fill_request_golfers(self, page, golfer_ids):
        """On the golfer entry page, fill Group 1 Golfer N rows with IDs."""
        ids = [str(g).strip() for g in (golfer_ids or []) if str(g).strip()]
        if not ids:
            raise RuntimeError("No golfer IDs provided for request.")

        # Fill Golfer ID inputs in row order. The page has a Golfer ID text
        # input and a Name <select> per row; filling the ID typically auto-
        # populates the name on blur, but we also try to set the name dropdown.
        for idx, gid in enumerate(ids[:4]):  # Group 1 = up to 4 slots
            field_name = f"glfers{idx + 1}"
            field = page.locator(f'input[name="{field_name}"]').first
            field.fill(gid)
            field.blur()

        # Try to find a name dropdown per row and select by value (golfer id).
        for idx, gid in enumerate(ids[:4]):
            select_name = f"buddy{idx + 1}"
            try:
                page.locator(f'select[name="{select_name}"]').first.select_option(value=gid)
            except Exception:
                pass  # ID-only fill may be enough

        with page.expect_navigation(wait_until="domcontentloaded", timeout=25000):
            page.locator('input[name="but2"]').first.click()

    def _extract_request_confirmation(self, page):
        """On the confirmation page, scrape the Request No."""
        body = page.inner_text("body")
        m = re.search(r"Request\s*No\.?\s*[:\-]?\s*(\d+)", body, re.IGNORECASE)
        return m.group(1) if m else None

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
            courses = self._scrape_available_courses(page)
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
            return {"success": True, "courses": courses}
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
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(page, tvn_username, tvn_password, golf_password)
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
            self._fill_request_golfers(page, golfer_ids)
            request_no = self._extract_request_confirmation(page)
            self._active.ts = _time.time()
            if not request_no:
                return {"success": False, "error": "Request submitted but no confirmation number was returned."}
            return {
                "success": True,
                "request_no": request_no,
                "message": f"Request #{request_no} submitted",
            }
        except PWTimeout:
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("submit_request failed for user=%s", tvn_username)
            self._close_active()
            return {"success": False, "error": "Could not submit request. Please try again."}

    def view_my_requests(self, tvn_username, tvn_password, golf_password):
        """Scrape pending requests from the Requests landing page."""
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(page, tvn_username, tvn_password, golf_password)

            raw_rows = page.evaluate("""() => {
                const rows = Array.from(document.querySelectorAll('table tr'));
                return rows.map(row => {
                    const cells = Array.from(row.querySelectorAll('td')).map(c =>
                        (c.innerText || '').replace(/\\s+/g, ' ').trim()
                    );
                    const links = Array.from(row.querySelectorAll('a')).map(a => ({
                        text: (a.innerText || '').trim(),
                        href: a.getAttribute('href') || '',
                    }));
                    return { cells, links };
                }).filter(r => r.cells.some(Boolean));
            }""")

            requests_out = []
            for row in raw_rows:
                cells = row.get("cells") or []
                links = row.get("links") or []
                if len(cells) < 3:
                    continue
                action = (cells[0] or "").strip()
                name = (cells[1] or "").strip()
                date = (cells[2] or "").strip()
                lower = action.lower()
                if not action or "create new" in lower or "action" in lower:
                    continue
                # An existing request row will have a request number we can use as ID.
                rid = None
                for link in links:
                    href = link.get("href", "")
                    m = re.search(r"(\d{5,})", href)
                    if m:
                        rid = m.group(1)
                        break
                if not rid:
                    m = re.search(r"(\d{5,})", " ".join([action, name]))
                    if m:
                        rid = m.group(1)
                if not rid:
                    continue
                requests_out.append({
                    "request_id": rid,
                    "action": action,
                    "name": name,
                    "date": date,
                })

            self._active.ts = _time.time()
            return {"success": True, "requests": requests_out}
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
        """Cancel a pending request by ID from the Requests landing page."""
        try:
            _, page = self._get_or_create_session(tvn_username)
            self._ensure_logged_in(page, tvn_username, tvn_password, golf_password)
            self._nav_to_requests_landing(page, tvn_username, tvn_password, golf_password)

            # Find the row containing this request id and click its delete/cancel link.
            target = str(target_request_id).strip()
            handled = page.evaluate("""(rid) => {
                const rows = Array.from(document.querySelectorAll('table tr'));
                for (const row of rows) {
                    const text = (row.innerText || '');
                    if (text.includes(rid)) {
                        const link = row.querySelector('a');
                        if (link) { link.click(); return true; }
                    }
                }
                return false;
            }""", target)

            if not handled:
                return {"success": False, "error": "Request not found."}

            page.wait_for_load_state("networkidle", timeout=15000)
            # If a confirmation prompt appears, accept it.
            try:
                page.on("dialog", lambda d: d.accept())
                page.locator('input[value="Yes"], input[value="Delete"], input[value="Cancel Request"]').first.click(timeout=3000)
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            self._active.ts = _time.time()
            return {"success": True, "message": f"Request {target} canceled"}
        except PWTimeout:
            self._close_active()
            return {"success": False, "error": _timeout_error()}
        except RuntimeError as e:
            self._close_active()
            return {"success": False, "error": str(e)}
        except Exception:
            logger.exception("delete_request failed for user=%s", tvn_username)
            self._close_active()
            return {"success": False, "error": "Could not cancel that request. Please try again."}
