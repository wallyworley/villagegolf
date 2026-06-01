"""
Capture the Requests flow on villagefairways.com, step by step, into PNGs.

We can't use real Villages credentials, so we mock the app's own API responses
(/api/request-courses, /api/submit-request, /api/my-requests) at the network
layer. That exercises the real frontend JS exactly as a user would see it,
without touching the live Villages site.
"""
import pathlib
from playwright.sync_api import sync_playwright

OUT = pathlib.Path("/Users/walterworley/Documents/villages-golf-app/docs/screenshots")
OUT.mkdir(parents=True, exist_ok=True)

MOCK_COURSES = [
    "Evans Prairie", "Cane Garden", "Belle Glade", "Bonifay",
    "Southern Oaks", "Mallory Hill", "Palmer Legends", "Glenview Champions",
]

def route_handler(route):
    url = route.request.url
    if "/api/request-courses" in url:
        route.fulfill(status=200, content_type="application/json",
                      body='{"success": true, "courses": %s}' % str(MOCK_COURSES).replace("'", '"'))
    elif "/api/submit-request" in url:
        route.fulfill(status=200, content_type="application/json",
                      body='{"success": true, "request_no": "80421", "message": "Request #80421 submitted"}')
    elif "/api/my-requests" in url:
        route.fulfill(status=200, content_type="application/json",
                      body='{"success": true, "requests": [{"request_id":"80421","action":"View / Cancel","name":"Championship \\u2013 2 golfers","date":"Mon: 6/8"}]}')
    else:
        route.continue_()

def shot(pg, name):
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(OUT / name), full_page=True)
    print("  captured", name)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    ctx.route("**/api/**", route_handler)
    pg = ctx.new_page()
    pg.goto("https://villagefairways.com/", wait_until="networkidle", timeout=30000)
    pg.wait_for_timeout(700)

    # Mock an authenticated session with a golfer list
    pg.evaluate("""
      BUDDIES=[{id:'483204',name:'Walter Worley',initials:'WW'},
               {id:'640405',name:'Marcia Ann Worley',initials:'MW'},
               {id:'111222',name:'Jim Caldwell',initials:'JC'},
               {id:'333444',name:'Bob Henderson',initials:'BH'}];
      updateHeader('Walter Worley',BUDDIES[0]); buildGolfers(); buildDates();
      showScreen('app'); showAppView('request');
    """)
    pg.wait_for_timeout(400)

    # Step 1 — empty Requests form
    shot(pg, "requests-01-empty.png")

    # Step 2 — fill the "When" + "Course & Time" cards
    pg.evaluate("""
      // play date ~ a week out
      const d=new Date(); d.setDate(d.getDate()+7);
      document.getElementById('rq-date').value = d.toISOString().slice(0,10);
      setRequestMax(2);
      setRequestCourse('Championship');
      setRequestPref('Course');
      document.getElementById('rq-time').value='12:00';
      document.getElementById('rq-earliest').value='9:00';
      document.getElementById('rq-latest').value='2:00';
    """)
    shot(pg, "requests-02-filled.png")

    # Step 3 — load courses (mocked) -> picker appears
    pg.evaluate("loadRequestCourses()")
    pg.wait_for_timeout(700)
    shot(pg, "requests-03-courses-loaded.png")

    # Step 4 — add two courses into the selected list, in order
    pg.evaluate("""
      const avail=document.getElementById('rq-available');
      [...avail.options].forEach(o=>o.selected=(o.value==='Evans Prairie'));
      rqAddCourse();
      [...avail.options].forEach(o=>o.selected=(o.value==='Cane Garden'));
      rqAddCourse();
    """)
    shot(pg, "requests-04-courses-selected.png")

    # Step 5 — pick golfers
    pg.evaluate("toggleRequestGolfer('483204'); toggleRequestGolfer('640405');")
    shot(pg, "requests-05-golfers.png")

    # Step 6 — submit (mocked success) -> status + My Open Requests populates
    pg.evaluate("submitRequest()")
    pg.wait_for_timeout(900)
    shot(pg, "requests-06-submitted.png")

    # Step 7 — My Open Requests list close-up (scroll to bottom card)
    pg.evaluate("document.getElementById('rq-list').scrollIntoView({block:'center'})")
    shot(pg, "requests-07-open-requests.png")

    b.close()
print("DONE")
