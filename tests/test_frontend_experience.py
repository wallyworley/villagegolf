"""Offline regressions for the single-page website integration contract."""

import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
EXPERIENCE_JS = (ROOT / "static" / "js" / "experience.js").read_text(encoding="utf-8")
EXPERIENCE_CSS = (ROOT / "static" / "css" / "experience.css").read_text(encoding="utf-8")


class _IdsAndAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.scripts = []
        self.stylesheets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            assert attrs["id"] not in self.ids, f"duplicate id {attrs['id']}"
            self.ids.add(attrs["id"])
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets.append(attrs.get("href"))


def _extract_function(source, name):
    """Return the source text of a top-level ``function name(...){...}`` block."""
    start = source.index(f"function {name}(")
    depth = 0
    started = False
    for index in range(start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
            started = True
        elif char == "}":
            depth -= 1
            if started and depth == 0:
                return source[start:index + 1]
    raise ValueError(f"could not extract function {name}")


def _extract_const_statement(source, name):
    """Return the literal source text of a top-level ``const name = ...;`` statement."""
    match = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*[^;]+;", source)
    if not match:
        raise ValueError(f"could not extract const {name}")
    return match.group(0)


class FrontendExperienceTests(unittest.TestCase):
    def run_behavior(self, assertions):
        """Execute the shipped enhancement code with a small, offline DOM double."""
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = new Map();
function element() {
  const classes = new Set();
  return {value:'', checked:false, disabled:false, innerHTML:'', textContent:'', style:{}, dataset:{},
    classList:{add:(...xs)=>xs.forEach(x=>classes.add(x)),remove:(...xs)=>xs.forEach(x=>classes.delete(x)),contains:x=>classes.has(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x)},
    setAttribute(){}, removeAttribute(){}, focus(){}, remove(){}, appendChild(x){return x;},
    querySelector(){return element();},querySelectorAll(){return [];},addEventListener(){},insertAdjacentHTML(){},insertAdjacentElement(){},
    options:[], isConnected:true};
}
const context = {
  assert, console, setTimeout, clearTimeout, setInterval, clearInterval, AbortController,
  document:{getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},
    querySelector(){return element();},querySelectorAll(){return [];},createElement:element,
    addEventListener(){},contains(){return true;},body:element(),activeElement:element()},
  window:{addEventListener(){},isSecureContext:true}, navigator:{},
  S:{date:'20260912',dateLabel:'Saturday',course:'Executive',regionFilter:'all',golfers:['111'],guestCount:0,timeFilter:'all'},
  SR:{}, BUDDIES:[{id:'111',name:'Preview Golfer'}], BOOKING_IN_FLIGHT:false, MY_TEE_TIMES:[],
  _wxCache:null, WX_LABELS:{}, esc:value=>String(value??''), baseCalls:{},
  closeConfirm(){}, showError(message){context.lastError=message;}, checkFetch(){},
  rqFormatDateForApi(){return '20260918';},rqSaveRecents(){},
};
for(const match of SOURCE.matchAll(/^(\w+)=(?:async )?function/gm))context[match[1]]=()=>{};
for(const match of SOURCE.matchAll(/^const base\w+=(\w+);/gm))context[match[1]]=(...args)=>{context.baseCalls[match[1]]=args;};
vm.createContext(context);
vm.runInContext(SOURCE,context);
vm.runInContext('(async()=>{'+ASSERTIONS+'})()',context).catch(error=>{console.error(error);process.exitCode=1;});
"""
        script = "const SOURCE=" + json.dumps(EXPERIENCE_JS) + ";\nconst ASSERTIONS=" + json.dumps(assertions) + ";\n" + harness
        result = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_golfer_selection_cannot_submit_reviewed_booking(self):
        self.run_behavior("""
let submissions=0;
jsonRequest=async()=>{submissions++;return {success:true};};
EXPERIENCE.searchId='fixture-search';
EXPERIENCE.searchSnapshot=JSON.stringify(teeSearchPayload());
openConfirm({course:'Silver Lake',time:'08:15',display_time:'8:15 AM'});
assert.equal(EXPERIENCE.pendingSnapshot.search_id,'fixture-search');
S.golfers.push('222');
await confirmBook();
assert.equal(submissions,0,'A changed group must require a fresh search, not book the reviewed time.');
assert.match(lastError,/choices changed/i);
""")

    def test_guest_count_is_capped_by_the_group_size(self):
        self.run_behavior("""
const count=document.getElementById('guestCount'), minus=document.getElementById('guestMinus'), plus=document.getElementById('guestPlus');
S.golfers=['111','222','333'];
changeGuests(1);
assert.equal(S.guestCount,1);
assert.equal(count.textContent,'1');
assert.equal(minus.disabled,false);
assert.equal(plus.disabled,true,'Three golfers leave room for only one guest.');
changeGuests(1);
assert.equal(S.guestCount,1,'A fourth player is the most a group can hold.');
S.golfers.push('444');
syncGuestLimit();
assert.equal(S.guestCount,0,'Adding a fourth golfer must drop the guest.');
assert.equal(count.textContent,'0');
assert.equal(plus.disabled,true);
assert.equal(minus.disabled,true);
changeGuests(1);
assert.equal(S.guestCount,0,'A group of four cannot add a guest.');
changeGuests(-1);
assert.equal(S.guestCount,0,'The count never goes below zero.');
S.golfers=['111'];
syncGuestLimit();
assert.equal(plus.disabled,false);
for(let i=0;i<5;i++) changeGuests(1);
assert.equal(S.guestCount,3,'No group can bring more than three guests.');
""")

    def test_guest_change_requires_a_fresh_search(self):
        self.run_behavior("""
let submissions=0;
jsonRequest=async()=>{submissions++;return {success:true};};
const detail=document.getElementById('cs-detail');
EXPERIENCE.searchId='fixture-search';
EXPERIENCE.searchSnapshot=JSON.stringify(teeSearchPayload());
openConfirm({course:'Silver Lake',time:'08:15',display_time:'8:15 AM'});
assert.match(detail.innerHTML,/Guests<\\/span><span>None</);
changeGuests(1);
await confirmBook();
assert.equal(submissions,0,'A changed guest count must require a fresh search, not book the reviewed time.');
assert.match(lastError,/choices changed/i);

changeGuests(1);
EXPERIENCE.searchId='second-search';
EXPERIENCE.searchSnapshot=JSON.stringify(teeSearchPayload());
openConfirm({course:'Silver Lake',time:'08:15',display_time:'8:15 AM'});
assert.match(detail.innerHTML,/Guests<\\/span><span>2 guests</);
globalThis.lastError='';
S.guestCount=1;
await confirmBook();
assert.equal(submissions,0,'Even a count changed without clearing the results must not book.');
assert.match(lastError,/choices changed/i);

S.guestCount=1;
EXPERIENCE.searchSnapshot=JSON.stringify(teeSearchPayload());
openConfirm({course:'Silver Lake',time:'08:15',display_time:'8:15 AM'});
assert.match(detail.innerHTML,/Guests<\\/span><span>1 guest</);
""")

    def test_search_payload_sends_guest_count_only(self):
        self.run_behavior("""
S.guestCount=2;
const payload=teeSearchPayload();
assert.equal(payload.guest_count,2);
assert.equal('has_guests' in payload,false,'The server derives has_guests from the count.');
EXPERIENCE.searchId='fixture-search';
EXPERIENCE.searchSnapshot=JSON.stringify(payload);
openConfirm({course:'Silver Lake',time:'08:15',display_time:'8:15 AM'});
assert.equal(EXPERIENCE.pendingSnapshot.guest_count,2,'The booking request must carry the reviewed guest count.');
assert.equal('has_guests' in EXPERIENCE.pendingSnapshot,false);
""")

    def test_old_guest_switch_is_gone(self):
        parser = _IdsAndAssets()
        parser.feed(INDEX)
        self.assertNotIn("guestTog", INDEX)
        self.assertNotIn("guestTog", EXPERIENCE_JS)
        self.assertLessEqual({"guestMinus", "guestCount", "guestPlus"}, parser.ids)
        for source in (INDEX, EXPERIENCE_JS):
            self.assertNotIn("S.guests", source)
            self.assertNotIn("guestChanged", source)
        self.assertIn('onclick="changeGuests(-1)"', INDEX)
        self.assertIn('onclick="changeGuests(1)"', INDEX)
        self.assertIn("rq-guests", INDEX, "The Advance Requests guest switch is out of scope.")
        for rule in (".guest-stepper", ".step-btn", ".step-val"):
            self.assertIn(rule, EXPERIENCE_CSS)

    def test_guest_stepper_respects_the_section_lock(self):
        self.run_behavior("""
const section=document.getElementById('sec-course');
const minus=document.getElementById('guestMinus'), plus=document.getElementById('guestPlus');
section.classList.add('dim');
S.golfers=['111','222','333','444'];
renderGuestStepper();
assert.equal(plus.disabled,true);
assert.equal(plus.dataset.progressiveDisabled,undefined,'A button that must stay off after unlock is not tagged for re-enabling.');
assert.equal(minus.disabled,true);
assert.equal(minus.dataset.progressiveDisabled,undefined);
S.golfers=['111'];
renderGuestStepper();
assert.equal(plus.disabled,true,'A locked section keeps every control disabled.');
assert.equal(plus.dataset.progressiveDisabled,'true','A button that should be enabled is tagged so unlock re-enables it.');
assert.equal(minus.disabled,true);
assert.equal(minus.dataset.progressiveDisabled,undefined);
section.classList.remove('dim');
renderGuestStepper();
assert.equal(plus.disabled,false);
assert.equal(plus.dataset.progressiveDisabled,undefined);
assert.equal(minus.disabled,true);
S.golfers=['111','222','333','444'];
renderGuestStepper();
assert.equal(plus.disabled,true,'An unlocked stepper follows the group limit.');
assert.equal(plus.dataset.progressiveDisabled,undefined);
S.golfers=['111'];
changeGuests(1);
assert.equal(minus.disabled,false);
assert.equal(minus.dataset.progressiveDisabled,undefined);
""")

    def test_guest_stepper_is_described_for_screen_readers(self):
        parser = _IdsAndAssets()
        parser.feed(INDEX)
        self.assertLessEqual({"guestHelp", "guestCountSpoken"}, parser.ids)
        self.assertIn('aria-describedby="guestHelp"', INDEX)
        count_tag = re.search(r'<span[^>]*id="guestCount"[^>]*>', INDEX).group(0)
        self.assertNotIn("aria-live", count_tag, "The visible count must not announce on its own.")
        spoken_tag = re.search(r'<span[^>]*id="guestCountSpoken"[^>]*>', INDEX).group(0)
        self.assertIn('aria-live="polite"', spoken_tag)
        self.assertIn("visually-hidden", spoken_tag)
        self.assertIn(".visually-hidden", EXPERIENCE_CSS)
        self.run_behavior("""
const spoken=document.getElementById('guestCountSpoken');
let value='', writes=0;
Object.defineProperty(spoken,'textContent',{get(){return value;},set(next){writes++;value=next;},configurable:true});
renderGuestStepper();
assert.equal(value,'No guests');
renderGuestStepper();
assert.equal(writes,1,'An unchanged count must not be announced again.');
changeGuests(1);
assert.equal(value,'1 guest');
changeGuests(1);
assert.equal(value,'2 guests');
renderGuestStepper();
assert.equal(writes,3);
changeGuests(-1);changeGuests(-1);
assert.equal(value,'No guests');
""")

    def test_golfer_and_reset_paths_keep_the_guest_count_in_step(self):
        build = _extract_function(INDEX, "buildGolfers")
        card, after_loop = build.split("list.appendChild(c);", 1)
        self.assertIn("syncGuestLimit", card, "Toggling a golfer card must re-check the guest limit.")
        self.assertLess(card.index("clearResults()"), card.index("syncGuestLimit"))
        self.assertLess(card.index("syncGuestLimit"), card.index("checkFetch()"))
        self.assertIn("if(typeof syncGuestLimit==='function') syncGuestLimit();", after_loop,
                      "Rebuilding the golfer list must re-render the stepper after the loop.")
        reset = _extract_function(INDEX, "resetApp")
        self.assertIn("S.guestCount=0", reset)
        self.assertIn("renderGuestStepper", reset)
        success = _extract_function(INDEX, "showSuccess")
        self.assertIn("d.guest_count", success)
        self.assertIn("<span>Guests</span>", success)
        self.assertIn("syncGuestLimit", _extract_function(EXPERIENCE_JS, "applySelectedPreset"))

    def test_guest_changes_clear_visible_results(self):
        self.run_behavior("""
const cleared=()=>'clearResults' in baseCalls;
const reset=()=>{delete baseCalls.clearResults;};
S.golfers=['111'];
reset(); changeGuests(1);
assert.equal(S.guestCount,1);
assert.ok(cleared(),'Adding a guest must hide the old results.');
reset(); changeGuests(-1);
assert.equal(S.guestCount,0);
assert.ok(cleared(),'Removing a guest must hide the old results.');
reset(); changeGuests(-1);
assert.equal(cleared(),false,'No change, nothing to clear.');
S.golfers=['111','222','333'];
changeGuests(1);
reset(); changeGuests(1);
assert.equal(S.guestCount,1);
assert.equal(cleared(),false,'A push past the limit changes nothing, so the results stay.');
reset(); syncGuestLimit();
assert.equal(cleared(),false,'A count within the limit leaves the results alone.');
S.golfers.push('444');
reset(); syncGuestLimit();
assert.equal(S.guestCount,0);
assert.ok(cleared(),'Dropping a guest to fit the group must hide the old results.');
""")

    def test_map_origin_is_used_once_unless_saving_is_selected(self):
        self.run_behavior("""
let saves=0, routes=[];
updatePreferences=async()=>{saves++;return true;};
requestCourseRoute=async(id,point)=>routes.push({id,point});
EXPERIENCE.username='preview';
EXPERIENCE.originPicker={mode:'route',course:{id:'silver-lake'},point:null};
selectOriginPickerPoint({lat:28.93,lng:-81.98});
assert.equal(saves,0,'Selecting a pin must not save a location.');
assert.equal(routes.length,0,'Selecting a pin must not calculate before confirmation.');
await confirmOriginPicker();
assert.equal(saves,0,'One-time origin must not overwrite the saved starting point.');
assert.equal(routes.length,1);
assert.equal(routes[0].point.latitude,28.93);
EXPERIENCE.originPicker={mode:'route',course:{id:'silver-lake'},point:{latitude:28.93,longitude:-81.98}};
document.getElementById('origin-picker-save').checked=true;
await confirmOriginPicker();
assert.equal(saves,1,'Explicit save choice must persist the selected point.');
""")

    def test_location_callback_cannot_reopen_a_closed_course(self):
        self.run_behavior("""
let success, routes=0, saves=0;
navigator.geolocation={getCurrentPosition(callback){success=callback;}};
requestCourseRoute=async()=>{routes++;};
updatePreferences=async()=>{saves++;return true;};
EXPERIENCE.username='preview';
EXPERIENCE.courses=[{id:'silver-lake',name:'Silver Lake'}];
EXPERIENCE.activeCourse=EXPERIENCE.courses[0];
document.getElementById('course-overlay').classList.add('show');
useCurrentLocation(false,'silver-lake');
closeCourseModal();
await success({coords:{latitude:28.93,longitude:-81.98}});
assert.equal(routes,0,'A late GPS result must not open directions after closing the course.');
assert.equal(saves,0);
""")

    def test_older_route_response_cannot_replace_newer_origin(self):
        self.run_behavior("""
let resolvers=[];
jsonRequest=()=>new Promise(resolve=>resolvers.push(resolve));
EXPERIENCE.username='preview';
EXPERIENCE.courses=[{id:'silver-lake',name:'Silver Lake'}];
EXPERIENCE.activeCourse=EXPERIENCE.courses[0];
document.getElementById('course-overlay').classList.add('show');
const first=requestCourseRoute('silver-lake',{latitude:28.93,longitude:-81.98},'First origin');
const second=requestCourseRoute('silver-lake',{latitude:28.94,longitude:-81.99},'New origin');
resolvers[1]({success:true,miles:7.2,minutes:30});await second;
const current=document.getElementById('route-panel').innerHTML;
resolvers[0]({success:true,miles:1.2,minutes:5});await first;
assert.equal(document.getElementById('route-panel').innerHTML,current,'Late first route must not overwrite the second route.');
""")

    def test_next_upcoming_reservation_precedes_past_or_unknown_dates(self):
        self.run_behavior("""
const dateIn=days=>{const date=new Date();date.setDate(date.getDate()+days);return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;};
const items=[
  {reservation_no:'later',play_date:dateIn(4),display_time:'8:15 AM'},
  {reservation_no:'past',play_date:dateIn(-2),display_time:'8:15 AM'},
  {reservation_no:'unknown'},
  {reservation_no:'next',play_date:dateIn(1),display_time:'9:30 AM'}
];
const sorted=sortReservations(items);
assert.equal(sorted[0].reservation_no,'next');
assert.equal(sorted[1].reservation_no,'later');
assert.equal(items[0].reservation_no,'later','Sorting must not mutate the caller list.');
renderMyTeeTimes(items);
assert.equal(baseCalls.renderMyTeeTimes[0][0].reservation_no,'next','The base renderer and calendar actions must use the sorted order.');
""")

    def test_advance_request_requires_confirmation_of_unchanged_choices(self):
        self.run_behavior("""
let submissions=[];
jsonRequest=async(url,options)=>{if(url==='/api/submit-request')submissions.push(JSON.parse(options.body));return {success:true,requests:[]};};
Object.assign(SR,{maxGolfers:2,course:'Executive',selectedCourses:['Silver Lake'],golfers:['111'],preference:'Course'});
document.getElementById('rq-date').value='2026-09-18';
document.getElementById('rq-time').value='09:00';
document.getElementById('rq-earliest').value='08:00';
document.getElementById('rq-latest').value='10:00';
submitRequest();
assert.equal(submissions.length,0,'Review must never send the request.');
assert.ok(EXPERIENCE.requestReviewSnapshot);
document.getElementById('rq-time').value='09:30';
await confirmSubmitRequest();
assert.equal(submissions.length,0,'Changed choices need a fresh review.');
assert.match(document.getElementById('rq-err').textContent,/choices changed/);
submitRequest();
await confirmSubmitRequest();
assert.equal(submissions.length,1);
assert.equal(submissions[0].time_to_play,'09:30');
""")

    def test_course_experience_assets_and_controls_are_wired(self):
        parser = _IdsAndAssets()
        parser.feed(INDEX)
        required = {
            "tab-book", "tab-mytimes", "tab-request", "tab-courses",
            "view-courses", "course-map", "course-list", "course-overlay",
            "search-time-from", "search-time-to", "arrival-buffer", "preset-select",
        }
        self.assertLessEqual(required, parser.ids)
        self.assertIn("/static/js/experience.js", parser.scripts)
        self.assertIn("/static/css/experience.css", parser.stylesheets)
        self.assertTrue((ROOT / "static" / "js" / "experience.js").is_file())
        self.assertTrue((ROOT / "static" / "css" / "experience.css").is_file())

    def test_frontend_uses_separate_catalog_route_and_preference_apis(self):
        for endpoint in ("/api/courses", "/api/preferences", "/api/course-route"):
            self.assertIn(endpoint, EXPERIENCE_JS)
        route_section = EXPERIENCE_JS.split("async function requestCourseRoute", 1)[1].split(
            "function formatDistance", 1
        )[0]
        self.assertIn("/api/course-route", route_section)
        self.assertNotIn("/api/tee-times", route_section)
        self.assertNotIn("/api/book", route_section)

    def test_booking_guard_carries_search_context_and_unknown_results_verify(self):
        self.assertIn("search_id:EXPERIENCE.searchId", EXPERIENCE_JS)
        self.assertIn("JSON.stringify(teeSearchPayload())!==EXPERIENCE.searchSnapshot", EXPERIENCE_JS)
        self.assertIn("if(data.unknown) bookingUnconfirmed", EXPERIENCE_JS)
        self.assertIn("The delete result is unknown. Refreshing reservations to verify", EXPERIENCE_JS)
        self.assertIn("The cancellation result is unknown. Refreshing requests to verify", EXPERIENCE_JS)

    def test_javascript_parses_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        subprocess.run(
            [node, "--check", str(ROOT / "static" / "js" / "experience.js")],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_safe_number_treats_missing_values_as_null(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        source = _extract_function(EXPERIENCE_JS, "safeNumber")
        script = source + """
var results = [
  safeNumber(null),
  safeNumber(undefined),
  safeNumber(''),
  safeNumber(' '),
  safeNumber(true),
  safeNumber('3.5'),
];
var expected = [null, null, null, null, null, 3.5];
for (var i = 0; i < expected.length; i++) {
  if (results[i] !== expected[i]) {
    throw new Error('case ' + i + ': expected ' + expected[i] + ' got ' + results[i]);
  }
}
"""
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_search_payload_never_sends_null_time_window(self):
        source = _extract_function(EXPERIENCE_JS, "teeSearchPayload")
        self.assertIn("time_from:from||''", source)
        self.assertNotIn("||null", source)

    def test_current_location_explains_insecure_context_and_fallback(self):
        source = _extract_function(EXPERIENCE_JS, "useCurrentLocation")
        self.assertIn("isSecureContext", source)
        self.assertIn("secure (https) address", source)
        self.assertIn("Choose a starting point on the map instead", source)
        self.assertIn("error.code===1", source)

    def test_find_course_consults_aliases(self):
        self.assertIn("aliases", _extract_function(EXPERIENCE_JS, "findCourse"))

    def test_region_bands_are_wired(self):
        for tile_id in ("r-all", "r-north-of-466", "r-between-the-6s", "r-south-of-466a", "r-south-of-turnpike"):
            self.assertIn(f'id="{tile_id}"', INDEX)
        self.assertIn("Between the 6s", INDEX)
        self.assertIn("c.band", EXPERIENCE_JS)
        self.assertNotIn("c.region", EXPERIENCE_JS)
        self.assertNotIn("course.region", EXPERIENCE_JS)

    def test_leaflet_is_vendored_and_loaded(self):
        vendor_dir = ROOT / "static" / "vendor" / "leaflet"
        for relative_path in ("leaflet.js", "leaflet.css", "images/marker-icon.png"):
            self.assertTrue((vendor_dir / relative_path).is_file(), f"missing {relative_path}")
        self.assertIn('href="/static/vendor/leaflet/leaflet.css"', INDEX)
        leaflet_script_pos = INDEX.index('/static/vendor/leaflet/leaflet.js')
        experience_script_pos = INDEX.index('/static/js/experience.js')
        self.assertLess(leaflet_script_pos, experience_script_pos)
        self.assertNotIn("/api/map-image", EXPERIENCE_JS)
        self.assertNotIn("map-basemap", EXPERIENCE_JS)

    def test_tile_math_matches_verified_tiles(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        const_names = (
            "VILLAGES_TILE_RES0", "VILLAGES_TILE_ORIGIN_LON",
            "VILLAGES_TILE_ORIGIN_LAT", "VILLAGES_TILE_SIZE",
        )
        source = "\n".join(_extract_const_statement(EXPERIENCE_JS, name) for name in const_names)
        source += "\n" + _extract_function(EXPERIENCE_JS, "tileResolution")
        source += "\n" + _extract_function(EXPERIENCE_JS, "tileForLatLng")
        script = source + """
var zoom5 = tileForLatLng(28.9099, -81.9932, 5);
if (zoom5.x !== 32628 || zoom5.y !== 38075) {
  throw new Error('zoom 5 tile mismatch: ' + JSON.stringify(zoom5));
}
var zoom8 = tileForLatLng(28.9099, -81.9932, 8);
if (zoom8.x !== 261028 || zoom8.y !== 304600) {
  throw new Error('zoom 8 tile mismatch: ' + JSON.stringify(zoom8));
}
var resolutionAtLevel3 = tileResolution(3);
var expectedResolution = VILLAGES_TILE_RES0 / 8;
if (Math.abs(resolutionAtLevel3 - expectedResolution) >= 1e-15) {
  throw new Error('resolution at level 3 mismatch: ' + resolutionAtLevel3 + ' vs ' + expectedResolution);
}
"""
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_course_map_uses_tile_layer_and_markers(self):
        self.assertIn("/tile/{z}/{y}/{x}", EXPERIENCE_JS)
        self.assertIn("L.tileLayer", EXPERIENCE_JS)
        self.assertIn("L.divIcon", EXPERIENCE_JS)
        self.assertIn("pin-no", EXPERIENCE_JS)
        self.assertIn("contextmenu", EXPERIENCE_JS)
        self.assertIn("invalidateSize", EXPERIENCE_JS)
        self.assertIn("attributionControl:false", EXPERIENCE_JS)
        route_source = _extract_function(EXPERIENCE_JS, "renderRouteMap")
        self.assertIn("L.polyline", route_source)
        self.assertIn(".remove()", route_source)

    def test_course_markers_cluster_without_redrawing_on_pan(self):
        vendor_dir = ROOT / "static" / "vendor" / "leaflet.markercluster"
        for relative_path in ("leaflet.markercluster.js", "MarkerCluster.css", "LICENSE.txt"):
            self.assertTrue((vendor_dir / relative_path).is_file(), f"missing {relative_path}")
        leaflet = INDEX.index("/static/vendor/leaflet/leaflet.js")
        cluster = INDEX.index("/static/vendor/leaflet.markercluster/leaflet.markercluster.js")
        self.assertLess(leaflet, cluster)
        self.assertLess(cluster, INDEX.index("/static/js/experience.js"))
        self.assertIn("L.markerClusterGroup(", EXPERIENCE_JS)
        # Redrawing on every pan closed an open cluster on phones; markers now
        # rebuild only when the shown courses or favorites change.
        self.assertNotIn("map.on('moveend'", EXPERIENCE_JS)
        layers = _extract_function(EXPERIENCE_JS, "renderCourseMapLayers")
        self.assertIn("markerSignature!==EXPERIENCE.courseMarkerSignature", layers)

    def test_request_list_refresh_cannot_strand_a_submission(self):
        self.run_behavior("""
let finishSubmit;
jsonRequest=(url,options)=>url==='/api/submit-request'
  ? new Promise(resolve=>{finishSubmit=resolve;})
  : Promise.resolve({success:true,requests:[]});
Object.assign(SR,{maxGolfers:2,course:'Executive',selectedCourses:['Silver Lake'],golfers:['111'],preference:'Course'});
document.getElementById('rq-date').value='2026-09-18';
document.getElementById('rq-time').value='09:00';
document.getElementById('rq-earliest').value='08:00';
document.getElementById('rq-latest').value='10:00';
submitRequest();
const pending=confirmSubmitRequest();
await loadMyRequests();
clearRequestCourses();
finishSubmit({success:true,message:'Request #777777 submitted'});
await pending;
assert.equal(EXPERIENCE.requestSubmissionInFlight,false);
assert.equal(document.getElementById('rq-status').textContent,'Request #777777 submitted','A list refresh or form edit must not hide the submission result.');
""")

    def test_my_tee_times_reload_after_booking_and_wait_after_failure(self):
        self.run_behavior("""
let loads=0;
globalThis.setMyTimesStatus=()=>{};
jsonRequest=async()=>{loads++;return {success:false,error:'Villages login failed.'};};
EXPERIENCE.username='preview';
showAppView('mytimes');
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(loads,1);
showAppView('book'); showAppView('mytimes');
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(loads,1,'A failed load must wait for the refresh button instead of retrying on every visit.');
EXPERIENCE.myTimesLoaded=true;
showSuccess({course:'Silver Lake',display_time:'8:15 AM'});
assert.equal(EXPERIENCE.myTimesLoaded,false,'A new booking must make My Tee Times reload.');
EXPERIENCE.myTimesLoaded=true;
bookingUnconfirmed('Unknown');
assert.equal(EXPERIENCE.myTimesLoaded,false,'An unknown booking must make My Tee Times reload to verify it.');
""")

    def test_unconfirmed_booking_message_survives_the_reload(self):
        self.run_behavior("""
const notice=document.getElementById('myTimesNotice');
bookingUnconfirmed('The Villages site said: golfer already booked.');
assert.equal(notice.textContent,'The Villages site said: golfer already booked.');
assert.equal(notice.hidden,false);
assert.equal(baseCalls.bookingUnconfirmed[0],'Checking your reservations…','The status line is replaced by the reload, so the reason lives in the notice.');
showSuccess({course:'Silver Lake',display_time:'8:15 AM'});
assert.equal(notice.hidden,true,'A confirmed booking clears the old warning.');
""")

    def test_request_review_uses_plain_twelve_hour_times(self):
        self.run_behavior("""
assert.equal(clockLabel('13:05'),'1:05 PM');
assert.equal(clockLabel('00:15'),'12:15 AM');
assert.equal(clockLabel('08:00'),'8:00 AM');
assert.equal(clockLabel('12:30'),'12:30 PM');
const markup=requestReviewMarkup({play_date:'20260918',golfer_ids:['111'],course_type:'Executive',course_choices:['Silver Lake'],
  time_to_play:'08:00',earliest_time:'07:00',latest_time:'13:00',preference:'Course',has_guests:false,any_course:false,max_golfers:2});
assert.match(markup,/7:00 AM to 1:00 PM/);
assert.doesNotMatch(markup,/13:00/);
assert.match(markup,/Keeping my course ranking/);
""")

    def test_dark_mode_text_and_phone_bars_stay_readable(self):
        dark_blocks = re.findall(r"@media \(prefers-color-scheme:dark\) \{(.*?)\n\}", EXPERIENCE_CSS, re.S)
        self.assertTrue(any("--ink-accent" in block and "--ink-strong" in block for block in dark_blocks))
        self.assertIn("--ink-accent: var(--g2)", INDEX)
        for rule in (".app-tab.active", ".request-group h3", ".request-picker-label", ".text-action"):
            line = next(line for line in EXPERIENCE_CSS.splitlines() if line.startswith(rule + " "))
            self.assertRegex(line, r"color:var\(--ink-(accent|strong)\)", rule)
        self.assertIn("[hidden] { display:none !important; }", EXPERIENCE_CSS)
        phone = EXPERIENCE_CSS.split("@media (max-width:759px) {", 1)[1].split("\n}", 1)[0]
        self.assertIn(".rq-done-bar { bottom:calc(88px", phone)


if __name__ == "__main__":
    unittest.main()
