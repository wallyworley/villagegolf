'use strict';

// Enhancements are intentionally isolated from Playwright. These requests only use
// the app's catalog, preference, routing, and weather endpoints.
const EXPERIENCE = {
  username: null,
  courses: [],
  preferences: {favorites:[],home:null,arrival_buffer_minutes:20,presets:[],max_cart_minutes:null},
  activeCourse: null,
  restoreFocus: null,
  teeGeneration: 0,
  courseGeneration: 0,
  preferenceGeneration: 0,
  requestGeneration: 0,
  myTimesGeneration: 0,
  myTimesLoaded: false,
  myTimesLoading: false,
  myTimesAutoFailed: false,
  requestSubmitGeneration: 0,
  requestListGeneration: 0,
  routeGeneration: 0,
  gpsGeneration: 0,
  locateGeneration: 0,
  searchId: null,
  searchSnapshot: null,
  pendingSnapshot: null,
  weatherHourly: [],
  weatherUpdatedAt: null,
  courseMap: null,
  courseMarkers: null,
  courseHomeMarker: null,
  courseMapFitted: false,
  courseMapFitPending: false,
  courseFilterSignature: null,
  courseMarkerSignature: null,
  myLocationLayer: null,
  routeMap: null,
  routeOrigin: null,
  routeOriginLabel: null,
  originPicker: null,
  originPickerMap: null,
  originPickerMarker: null,
  requestReviewSnapshot: null,
  requestSubmissionInFlight: false,
};

function jsonRequest(url, options={}){
  return fetch(url, options).then(async response=>{
    let body={};
    try{ body=await response.json(); }catch(e){ body={error:'The server returned an unreadable response.'}; }
    if(!response.ok && !body.error) body.error=`Request failed (${response.status}).`;
    return body;
  });
}

function safeNumber(value){
  if(value===null || value===undefined) return null;
  if(typeof value==='boolean') return null;
  if(typeof value==='string' && value.trim()==='') return null;
  const n=Number(value);
  return Number.isFinite(n) ? n : null;
}

// Town squares and landmarks residents use to describe each band. Searchable in the
// Course Finder so "Spanish Springs" finds the courses north of 466.
const BAND_LANDMARKS={
  'North of 466':['Spanish Springs','Spanish Springs Town Square','Lady Lake'],
  'Between the 6s':['Lake Sumter Landing','Lake Sumter','Sumter Landing'],
  'South of 466A':['Brownwood','Brownwood Paddock Square','Paddock Square'],
  'South of the Turnpike':['Sawgrass Grove','Eastport','Middleton'],
};

// Tile math for The Villages' PUBLICMAP26 ArcGIS tile cache: WKID 4326
// (plate carree, degrees), tile size 256, origin x=-400, y=400, resolution
// halving exactly at each integer zoom level. This is a pure function so it
// can be tested with node (no DOM) and reused by the Leaflet CRS below,
// which reproduces the same scheme so Leaflet's own tile coordinates equal
// these ArcGIS column/row values at every integer zoom.
const VILLAGES_TILE_RES0 = 0.0012182840349851034;
const VILLAGES_TILE_ORIGIN_LON = -400;
const VILLAGES_TILE_ORIGIN_LAT = 400;
const VILLAGES_TILE_SIZE = 256;

function tileResolution(zoom){ return VILLAGES_TILE_RES0 / Math.pow(2, zoom); }

function tileForLatLng(lat, lon, zoom){
  const tileSpan = tileResolution(zoom) * VILLAGES_TILE_SIZE;
  return {
    x: Math.floor((lon - VILLAGES_TILE_ORIGIN_LON) / tileSpan),
    y: Math.floor((VILLAGES_TILE_ORIGIN_LAT - lat) / tileSpan),
  };
}

const VILLAGES_TILE_URL = 'https://arc7.thevillages.com/arcgis/rest/services/PUBLICMAP26/MapServer/tile/{z}/{y}/{x}';

// Guarded so a missing Leaflet vendor bundle (L undefined) never throws at
// script-evaluation time; renderCourseMap falls back to the missing-map note.
const VillagesCRS = (typeof L !== 'undefined') ? L.extend({}, L.CRS.Earth, {
  code: 'ESRI:PUBLICMAP26',
  projection: L.Projection.LonLat,
  transformation: new L.Transformation(1, 400, -1, 400),
  scale: function(zoom){ return Math.pow(2, zoom) / VILLAGES_TILE_RES0; },
  zoom: function(scale){ return Math.log(scale * VILLAGES_TILE_RES0) / Math.LN2; },
  infinite: false,
  wrapLng: undefined,
}) : null;

const _VILLAGES_TILE_BOUNDS = [[28.445, -82.326], [29.185, -81.625]];
const _COURSE_MAP_MAX_BOUNDS = [[28.40, -82.60], [29.25, -81.40]];
const _VILLAGES_TILE_LAYER_OPTIONS = {
  tileSize: VILLAGES_TILE_SIZE, minZoom: 0, maxZoom: 10, noWrap: true,
  updateWhenIdle: false, keepBuffer: 4, bounds: _VILLAGES_TILE_BOUNDS,
};

function normalizeCourseName(value){
  return String(value||'').toLowerCase()
    .replace(/&/g,' and ')
    .replace(/\b(the|golf|course|country|club|executive|championship)\b/g,' ')
    .replace(/[^a-z0-9]+/g,' ')
    .trim().replace(/\s+/g,' ');
}

function findCourse(value){
  if(!value) return null;
  const raw=String(value);
  const norm=normalizeCourseName(raw);
  let matches=EXPERIENCE.courses.filter(c=>String(c.id)===raw || normalizeCourseName(c.name)===norm ||
    (c.aliases||[]).some(a=>normalizeCourseName(a)===norm));
  if(matches.length===1) return matches[0];
  matches=EXPERIENCE.courses.map(c=>{
    const candidates=[c.name,...(c.aliases||[])].map(normalizeCourseName);
    const matchLens=candidates.filter(cn=>norm.length>=4 && (cn.includes(norm)||norm.includes(cn))).map(cn=>cn.length);
    return {c,best:matchLens.length?Math.max(...matchLens):-1};
  }).filter(x=>x.best>=0).sort((a,b)=>b.best-a.best).map(x=>x.c);
  return matches[0]||null;
}

function resetExperienceAccount(username){
  closeOriginPicker(false);
  showBookingNotice('');
  EXPERIENCE.username=username||null;
  EXPERIENCE.courses=[];
  EXPERIENCE.preferences={favorites:[],home:null,arrival_buffer_minutes:20,presets:[],max_cart_minutes:null};
  EXPERIENCE.activeCourse=null;
  EXPERIENCE.searchId=null;
  EXPERIENCE.searchSnapshot=null;
  EXPERIENCE.pendingSnapshot=null;
  EXPERIENCE.teeGeneration++;
  EXPERIENCE.courseGeneration++;
  EXPERIENCE.preferenceGeneration++;
  EXPERIENCE.requestGeneration++;
  EXPERIENCE.myTimesGeneration++;
  EXPERIENCE.myTimesLoaded=false;
  EXPERIENCE.myTimesLoading=false;
  EXPERIENCE.myTimesAutoFailed=false;
  EXPERIENCE.requestSubmitGeneration++;
  EXPERIENCE.requestListGeneration++;
  EXPERIENCE.routeGeneration++;
  EXPERIENCE.gpsGeneration++;
  EXPERIENCE.locateGeneration++;
  EXPERIENCE.weatherHourly=[];
  EXPERIENCE.weatherUpdatedAt=null;
  EXPERIENCE.courseMapFitted=false;
  EXPERIENCE.courseMapFitPending=false;
  EXPERIENCE.courseFilterSignature=null;
  EXPERIENCE.courseMarkerSignature=null;
  if(EXPERIENCE.myLocationLayer){ EXPERIENCE.myLocationLayer.remove(); EXPERIENCE.myLocationLayer=null; }
  EXPERIENCE.routeOrigin=null;
  EXPERIENCE.routeOriginLabel=null;
  EXPERIENCE.requestReviewSnapshot=null;
  EXPERIENCE.requestSubmissionInFlight=false;
  resetRequestState();
  renderPreferenceUI();
}

const baseEnterApp=enterApp;
enterApp=function enhancedEnterApp(data){
  resetExperienceAccount(data.username);
  window.__playDate=data.play_date||null;
  baseEnterApp(data);
  if(data.play_date) setRequestDateMinimum(data.play_date);
  void Promise.allSettled([loadCourses(),loadPreferences()]);
};

const baseShowAppView=showAppView;
showAppView=function enhancedShowAppView(view){
  baseShowAppView(view);
  const courseView=document.getElementById('view-courses');
  const courseTab=document.getElementById('tab-courses');
  if(courseView) courseView.classList.toggle('active',view==='courses');
  if(courseTab) courseTab.classList.toggle('active',view==='courses');
  if(view==='courses'){
    if(!EXPERIENCE.courses.length) void loadCourses();
    renderCourseExplorer();
    if(EXPERIENCE.courseMap) EXPERIENCE.courseMap.invalidateSize();
  }
  // Load once on first visit. After a failure, wait for the explicit refresh
  // button: each load is a real Villages sign-in on the shared worker.
  if(view==='mytimes' && EXPERIENCE.username && !EXPERIENCE.myTimesLoaded && !EXPERIENCE.myTimesLoading && !EXPERIENCE.myTimesAutoFailed) void loadMyTeeTimes();
};

const baseSwitchUser=switchUser;
switchUser=async function enhancedSwitchUser(){
  resetExperienceAccount(null);
  closeCourseModal();
  await baseSwitchUser();
  const badge=document.getElementById('hdr-badge');
  if(badge){ badge.textContent=''; badge.style.display='none'; }
};

async function loadCourses(){
  const generation=++EXPERIENCE.courseGeneration;
  const status=document.getElementById('course-explorer-status');
  if(status) status.textContent='Loading course locations…';
  try{
    const data=await jsonRequest('/api/courses');
    if(generation!==EXPERIENCE.courseGeneration) return;
    if(data.success && Array.isArray(data.courses)){
      EXPERIENCE.courses=data.courses.filter(c=>c && c.id && c.name);
      renderCourseExplorer();
    }else if(status){ status.textContent=data.error||'Course locations are unavailable.'; }
  }catch(e){ if(generation===EXPERIENCE.courseGeneration && status) status.textContent='Course locations are unavailable.'; }
}

async function loadPreferences(){
  const generation=++EXPERIENCE.preferenceGeneration;
  try{
    const data=await jsonRequest('/api/preferences');
    if(generation!==EXPERIENCE.preferenceGeneration) return;
    if(data.ok && data.preferences){
      EXPERIENCE.preferences=Object.assign({},EXPERIENCE.preferences,data.preferences);
      EXPERIENCE.preferences.favorites=Array.isArray(EXPERIENCE.preferences.favorites)?EXPERIENCE.preferences.favorites:[];
      EXPERIENCE.preferences.presets=Array.isArray(EXPERIENCE.preferences.presets)?EXPERIENCE.preferences.presets:[];
      renderPreferenceUI(); renderCourseExplorer();
    }
  }catch(e){ setPreferenceStatus('Could not load saved preferences.','error'); }
}

async function updatePreferences(patch, message='Saved.'){
  const generation=++EXPERIENCE.preferenceGeneration;
  setPreferenceStatus('Saving…');
  const data=await jsonRequest('/api/preferences',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)
  }).catch(()=>({error:'Connection error.'}));
  if(generation!==EXPERIENCE.preferenceGeneration) return false;
  if(data.ok && data.preferences){
    EXPERIENCE.preferences=Object.assign({},EXPERIENCE.preferences,data.preferences);
    renderPreferenceUI(); renderCourseExplorer(); setPreferenceStatus(message,'success'); return true;
  }
  setPreferenceStatus(data.error||'Could not save preferences.','error'); return false;
}

function setPreferenceStatus(message,tone='muted'){
  const el=document.getElementById('preferences-status');
  if(!el) return;
  el.textContent=message||'';
  el.style.color=tone==='error'?'var(--ink-error)':tone==='success'?'var(--ink-accent)':'var(--muted)';
}

function renderPreferenceUI(){
  const p=EXPERIENCE.preferences;
  const home=document.getElementById('home-summary');
  if(home) home.textContent=p.home ? `${p.home.label||'Saved starting location'} · ${Number(p.home.latitude).toFixed(4)}, ${Number(p.home.longitude).toFixed(4)}` : 'No starting location saved.';
  const arrival=document.getElementById('arrival-buffer');
  const max=document.getElementById('max-cart-minutes');
  if(arrival) arrival.value=p.arrival_buffer_minutes ?? 20;
  if(max) max.value=p.max_cart_minutes ?? '';
  renderPresetSelect();
}

function savePreferenceFields(){
  const arrival=safeNumber(document.getElementById('arrival-buffer')?.value);
  const maxRaw=document.getElementById('max-cart-minutes')?.value;
  const max=maxRaw===''?null:safeNumber(maxRaw);
  void updatePreferences({arrival_buffer_minutes:arrival,max_cart_minutes:max});
}

function clearSavedHome(){ void updatePreferences({home:null},'Starting location cleared.'); }

function useCurrentLocation(saveAsHome=false, courseId=null){
  const routeCourse=courseId ? findCourse(courseId) : null;
  const status=courseId ? document.getElementById('route-panel') : document.getElementById('preferences-status');
  const fallbackHint=courseId ? 'Choose a starting point on the map instead.' : 'Choose a starting point on the map instead.';
  if(!navigator.geolocation){
    if(courseId) renderRouteOriginChoices(routeCourse,'Location is not supported by this browser. Choose on the map instead.');
    else if(status) status.textContent='Location is not supported by this browser. '+fallbackHint;
    return;
  }
  if(window.isSecureContext===false){
    if(courseId) renderRouteOriginChoices(routeCourse,'Current location needs a secure (https) address. Choose on the map instead.');
    else if(status) status.textContent='Current location needs a secure (https) address, which this preview does not have. '+fallbackHint;
    return;
  }
  const generation=++EXPERIENCE.gpsGeneration;
  const username=EXPERIENCE.username;
  if(courseId) renderRouteLoading(routeCourse, 'current location');
  else if(status) status.textContent='Waiting for location permission…';
  navigator.geolocation.getCurrentPosition(async position=>{
    if(generation!==EXPERIENCE.gpsGeneration || username!==EXPERIENCE.username) return;
    if(courseId && (!document.getElementById('course-overlay')?.classList.contains('show') || String(EXPERIENCE.activeCourse?.id)!==String(courseId))) return;
    const origin={latitude:position.coords.latitude,longitude:position.coords.longitude};
    if(saveAsHome) await updatePreferences({home:{...origin,label:'Current location'}},'Starting location saved.');
    if(generation!==EXPERIENCE.gpsGeneration || username!==EXPERIENCE.username) return;
    if(courseId) void requestCourseRoute(courseId,origin,'Current location');
  },error=>{
    if(generation!==EXPERIENCE.gpsGeneration || username!==EXPERIENCE.username) return;
    if(!status) return;
    const denied=error && error.code===1;
    if(courseId){
      renderRouteOriginChoices(routeCourse, denied
        ? 'Location permission was denied. Allow it in your browser settings, or choose on the map.'
        : 'Your location was unavailable. Try again or choose on the map.');
    }else{
      status.textContent=denied
        ? 'Location permission was denied. Allow it in your browser settings, or choose on the map.'
        : 'Your location was unavailable. Try again or choose on the map.';
    }
  },{enableHighAccuracy:false,timeout:12000,maximumAge:120000});
}

function ensureOriginPicker(){
  let overlay=document.getElementById('origin-picker-overlay');
  if(overlay) return overlay;
  overlay=document.createElement('div');
  overlay.id='origin-picker-overlay';
  overlay.className='overlay centered-overlay origin-picker-overlay';
  overlay.innerHTML=`<section class="course-modal origin-picker-modal" role="dialog" aria-modal="true" aria-labelledby="origin-picker-title" tabindex="-1">
    <button type="button" class="modal-close" onclick="closeOriginPicker()" aria-label="Cancel choosing a starting point">&#10005;</button>
    <div class="detail-kicker">Starting point</div><h2 id="origin-picker-title">Choose on map</h2>
    <p class="detail-address">Tap the map where your golf cart trip will start, then confirm the pin.</p>
    <div id="origin-picker-map" class="course-map origin-picker-map" role="application" aria-label="Choose a starting point on the map"></div>
    <button type="button" class="detail-link map-center-choice" onclick="selectOriginPickerMapCenter()">Use map center</button>
    <div id="origin-picker-status" class="contact-status" aria-live="polite">No point selected yet.</div>
    <label id="origin-picker-save-row" class="favorite-filter map-choice-save"><input id="origin-picker-save" type="checkbox"/> Save as my starting point</label>
    <div class="map-choice-actions"><button type="button" class="secondary-btn" onclick="closeOriginPicker()">Cancel</button><button id="origin-picker-confirm" type="button" class="fetch-btn map-choice-confirm" onclick="confirmOriginPicker()" disabled>Confirm starting point</button></div>
  </section>`;
  overlay.addEventListener('click',event=>{ if(event.target===overlay) closeOriginPicker(); });
  document.body.appendChild(overlay);
  return overlay;
}

function openOriginPicker(context){
  const overlay=ensureOriginPicker();
  closeOriginPicker(false);
  const generation=(EXPERIENCE.originPicker?.generation||0)+1;
  EXPERIENCE.originPicker={...context,generation,point:null,restoreFocus:context.restoreFocus||document.activeElement};
  const saveRow=document.getElementById('origin-picker-save-row');
  const save=document.getElementById('origin-picker-save');
  save.checked=context.mode==='account';
  saveRow.hidden=context.mode==='account';
  document.getElementById('origin-picker-confirm').textContent=context.mode==='account'?'Save starting point':'Confirm starting point';
  document.getElementById('origin-picker-confirm').disabled=true;
  document.getElementById('origin-picker-status').textContent='No point selected yet.';
  overlay.classList.add('show'); document.body.style.overflow='hidden';
  if(EXPERIENCE.originPickerMap){ EXPERIENCE.originPickerMap.remove(); EXPERIENCE.originPickerMap=null; }
  if(typeof L!=='undefined'){
    const map=L.map(document.getElementById('origin-picker-map'),{crs:VillagesCRS,minZoom:0,maxZoom:10,zoomSnap:0.5,attributionControl:false,zoomControl:true,maxBounds:_COURSE_MAP_MAX_BOUNDS,maxBoundsViscosity:0.8});
    L.tileLayer(VILLAGES_TILE_URL,_VILLAGES_TILE_LAYER_OPTIONS).addTo(map);
    const home=EXPERIENCE.preferences.home;
    const course=context.course;
    const points=[];
    if(safeNumber(home?.latitude)!==null&&safeNumber(home?.longitude)!==null) points.push([Number(home.latitude),Number(home.longitude)]);
    if(safeNumber(course?.latitude)!==null&&safeNumber(course?.longitude)!==null) points.push([Number(course.latitude),Number(course.longitude)]);
    if(points.length===1) map.setView(points[0],6); else if(points.length>1) map.fitBounds(points,{padding:[28,28]}); else map.setView([28.93,-81.98],3);
    map.on('click',event=>selectOriginPickerPoint(event.latlng));
    map.on('contextmenu',event=>selectOriginPickerPoint(event.latlng));
    EXPERIENCE.originPickerMap=map;
    if(context.initialPoint) selectOriginPickerPoint(context.initialPoint);
    setTimeout(()=>map.invalidateSize(),0);
  }else document.getElementById('origin-picker-status').textContent='The map is unavailable right now.';
  setTimeout(()=>overlay.querySelector('.origin-picker-modal')?.focus(),0);
}

function selectOriginPickerPoint(latlng){
  if(!EXPERIENCE.originPicker || !latlng) return;
  const point={latitude:Number(latlng.lat),longitude:Number(latlng.lng)};
  if(!Number.isFinite(point.latitude)||!Number.isFinite(point.longitude)) return;
  EXPERIENCE.originPicker.point=point;
  if(EXPERIENCE.originPickerMarker) EXPERIENCE.originPickerMarker.remove();
  if(EXPERIENCE.originPickerMap){
    EXPERIENCE.originPickerMarker=L.marker([point.latitude,point.longitude],{keyboard:false,title:'Selected starting point'}).addTo(EXPERIENCE.originPickerMap);
  }
  document.getElementById('origin-picker-status').textContent=`Selected pin · ${point.latitude.toFixed(4)}, ${point.longitude.toFixed(4)}`;
  document.getElementById('origin-picker-confirm').disabled=false;
}

function selectOriginPickerMapCenter(){
  if(EXPERIENCE.originPickerMap) selectOriginPickerPoint(EXPERIENCE.originPickerMap.getCenter());
}

async function confirmOriginPicker(){
  const picker=EXPERIENCE.originPicker;
  if(!picker?.point) return;
  const point={...picker.point};
  const shouldSave=picker.mode==='account'||!!document.getElementById('origin-picker-save')?.checked;
  const username=EXPERIENCE.username;
  if(shouldSave){
    const ok=await updatePreferences({home:{...point,label:'Map-selected starting point'}},'Starting location saved.');
    if(!ok || username!==EXPERIENCE.username || EXPERIENCE.originPicker!==picker) return;
  }
  closeOriginPicker();
  if(picker.mode==='route' && picker.course && username===EXPERIENCE.username){
    void requestCourseRoute(picker.course.id,point,'Chosen map point');
  }
}

function closeOriginPicker(restoreFocus=true){
  const picker=EXPERIENCE.originPicker;
  const overlay=document.getElementById('origin-picker-overlay');
  if(overlay) overlay.classList.remove('show');
  if(EXPERIENCE.originPickerMap){ EXPERIENCE.originPickerMap.remove(); EXPERIENCE.originPickerMap=null; }
  EXPERIENCE.originPickerMarker=null;
  EXPERIENCE.originPicker=null;
  const courseOpen=document.getElementById('course-overlay')?.classList.contains('show');
  document.body.style.overflow=courseOpen?'hidden':'';
  if(restoreFocus && picker?.restoreFocus && document.contains(picker.restoreFocus)) picker.restoreFocus.focus();
}

function startHomeSelection(initialPoint=null){
  const restoreFocus=document.getElementById('hdr-badge')||document.activeElement;
  if(typeof closeAccountDialog==='function') closeAccountDialog();
  openOriginPicker({mode:'account',restoreFocus,initialPoint});
}

function chooseRouteOriginOnMap(courseId,event){
  const course=findCourse(courseId); if(!course) return;
  openOriginPicker({mode:'route',course,restoreFocus:event?.currentTarget||document.activeElement});
}

function courseAreaDescription(course){
  const area=String(course.area||'').trim();
  const band=String(course.band||'').trim();
  const areaLabel=area ? (/\barea$/i.test(area)?area:`${area} area`) : '';
  if(areaLabel && band) return `${areaLabel} · ${band}`;
  return areaLabel || (band || 'Area information unavailable');
}

function filteredCourses(){
  const query=normalizeCourseName(document.getElementById('course-search')?.value||'');
  const type=document.getElementById('course-type-filter')?.value||'all';
  const region=document.getElementById('course-region-filter')?.value||'all';
  const favoriteOnly=!!document.getElementById('course-favorites-only')?.checked;
  const favorites=new Set(EXPERIENCE.preferences.favorites||[]);
  return EXPERIENCE.courses.filter(c=>{
    const haystack=normalizeCourseName([c.name,c.area,c.band,...(BAND_LANDMARKS[c.band]||[]),...(c.aliases||[]),c.address,c.zip].filter(Boolean).join(' '));
    return (!query||haystack.includes(query)) && (type==='all'||c.course_type===type) &&
      (region==='all'||c.band===region) && (!favoriteOnly||favorites.has(c.id));
  }).sort((a,b)=>a.name.localeCompare(b.name));
}

function courseFilterSignature(){
  return JSON.stringify({
    query:normalizeCourseName(document.getElementById('course-search')?.value||''),
    type:document.getElementById('course-type-filter')?.value||'all',
    region:document.getElementById('course-region-filter')?.value||'all',
    favorites:!!document.getElementById('course-favorites-only')?.checked,
  });
}

function renderCourseExplorer(){
  const listEl=document.getElementById('course-list');
  const status=document.getElementById('course-explorer-status');
  const map=document.getElementById('course-map');
  if(!listEl||!map) return;
  const courses=filteredCourses();
  const signature=courseFilterSignature();
  if(EXPERIENCE.courseFilterSignature!==null && EXPERIENCE.courseFilterSignature!==signature) EXPERIENCE.courseMapFitPending=true;
  EXPERIENCE.courseFilterSignature=signature;
  const favorites=new Set(EXPERIENCE.preferences.favorites||[]);
  if(status){ status.textContent=EXPERIENCE.courses.length ? `${courses.length} course${courses.length===1?'':'s'} shown` : 'Loading course locations…'; status.style.color=''; }
  const listCount=document.getElementById('course-list-count');
  if(listCount) listCount.textContent=EXPERIENCE.courses.length ? `(${courses.length})` : '';
  listEl.innerHTML=courses.length ? courses.map((c,i)=>{
    const fav=favorites.has(c.id);
    return `<article class="course-card"><div class="course-card-top">
      <button type="button" class="course-open" data-course-id="${esc(c.id)}" onclick="openCourseModal(this.dataset.courseId,event)">
        <div class="course-card-name"><span class="pin-no" aria-hidden="true">${i+1}</span>${esc(c.name)}</div>
        <div class="course-card-meta">${esc(c.course_type||'Course')} · ${esc(courseAreaDescription(c))}</div>
      </button>
      <button type="button" class="favorite-btn${fav?' on':''}" data-course-id="${esc(c.id)}" onclick="toggleFavorite(this.dataset.courseId)" aria-label="${fav?'Remove from':'Add to'} favorites" aria-pressed="${fav}">${fav?'★':'☆'}</button>
    </div>${c.address?`<div class="area-description">${esc(c.address)}${c.zip?`, ${esc(c.zip)}`:''}</div>`:''}</article>`;
  }).join('') : '<div class="mytimes-empty">No courses match these filters.</div>';
  renderCourseMap(courses);
}

function locatedCourses(){
  return EXPERIENCE.courses.filter(c=>safeNumber(c.latitude)!==null && safeNumber(c.longitude)!==null);
}

// All located catalog courses plus the saved home, as an L.latLngBounds for
// fitBounds. Used both for the map's initial view and the Reset control.
function courseMapFitBounds(courses=locatedCourses(),includeHome=true){
  const points=(courses||[]).filter(c=>safeNumber(c.latitude)!==null&&safeNumber(c.longitude)!==null).map(c=>[Number(c.latitude),Number(c.longitude)]);
  const home=EXPERIENCE.preferences.home;
  const hLat=safeNumber(home?.latitude),hLon=safeNumber(home?.longitude);
  if(includeHome && hLat!==null && hLon!==null) points.push([hLat,hLon]);
  return points.length ? L.latLngBounds(points) : null;
}

// Pin numbers match the numbered course cards in the list.
function courseDivIcon(index,favorite){
  return L.divIcon({
    className:`map-pin${favorite?' favorite':''}`,
    html:String(index+1),
    iconSize:[32,32],
    iconAnchor:[16,16],
  });
}

// Cluster bubbles are white with a green ring so a count never reads as a
// course number. Tapping one zooms in; at full zoom its courses fan out.
function courseClusterIcon(cluster){
  const count=cluster.getChildCount();
  const favorite=cluster.getAllChildMarkers().some(marker=>marker.options.favorite);
  return L.divIcon({
    className:`map-pin-cluster${favorite?' favorite':''}`,
    html:`<span aria-hidden="true">${count}</span><span class="visually-hidden">${count} courses. Zoom in to see them.</span>`,
    iconSize:[40,40],
    iconAnchor:[20,20],
  });
}

// A single custom control replacing the old zoom in/out/reset trio: Leaflet's
// own zoom control now handles zoom, and this handles reframing on all
// located courses plus the saved home.
const ResetMapControl=(typeof L!=='undefined') ? L.Control.extend({
  options:{position:'topright'},
  onAdd:function(map){
    const container=L.DomUtil.create('div','leaflet-bar map-reset-control');
    const button=L.DomUtil.create('button','map-zoom-btn map-zoom-reset',container);
    button.type='button';
    button.textContent='Reset';
    button.setAttribute('aria-label','Reset map view');
    L.DomEvent.disableClickPropagation(button);
    L.DomEvent.on(button,'click',()=>{
      const bounds=courseMapFitBounds();
      if(bounds) map.fitBounds(bounds,{padding:[24,24]});
    });
    return container;
  },
}) : null;

// On-demand "you are here" dot. Location is requested only when the button
// is pressed, and it is never saved.
const LocateControl=(typeof L!=='undefined') ? L.Control.extend({
  options:{position:'topleft'},
  onAdd:function(){
    const container=L.DomUtil.create('div','leaflet-bar map-locate-control');
    const button=L.DomUtil.create('button','map-zoom-btn map-locate-btn',container);
    button.type='button';
    button.textContent='My location';
    button.setAttribute('aria-label','Show my location on the map');
    L.DomEvent.disableClickPropagation(button);
    L.DomEvent.on(button,'click',showMyLocationOnMap);
    return container;
  },
}) : null;

function showMyLocationOnMap(){
  const status=document.getElementById('course-explorer-status');
  const say=(message,error=false)=>{ if(status){ status.textContent=message; status.style.color=error?'var(--ink-error)':''; } };
  if(!navigator.geolocation){ say('Location is not supported by this browser.',true); return; }
  if(window.isSecureContext===false){ say('Showing your location needs the secure (https) site.',true); return; }
  const generation=++EXPERIENCE.locateGeneration;
  const username=EXPERIENCE.username;
  say('Finding your location…');
  navigator.geolocation.getCurrentPosition(position=>{
    const map=EXPERIENCE.courseMap;
    if(generation!==EXPERIENCE.locateGeneration || username!==EXPERIENCE.username || !map) return;
    const latlng=[position.coords.latitude,position.coords.longitude];
    if(!L.latLngBounds(_COURSE_MAP_MAX_BOUNDS).contains(latlng)){ say('You appear to be outside The Villages map area.',true); return; }
    if(EXPERIENCE.myLocationLayer) EXPERIENCE.myLocationLayer.remove();
    const accuracy=Math.min(Math.max(Number(position.coords.accuracy)||0,0),1500);
    EXPERIENCE.myLocationLayer=L.layerGroup([
      L.circle(latlng,{radius:accuracy,interactive:false,color:'#1a73e8',weight:1,opacity:.5,fillOpacity:.12}),
      L.marker(latlng,{icon:L.divIcon({className:'my-location-dot',iconSize:[18,18],iconAnchor:[9,9]}),keyboard:false,interactive:false,title:'Your location'}),
    ]).addTo(map);
    map.setView(latlng,Math.max(map.getZoom(),6));
    say('Showing your current location. It is not saved.');
  },error=>{
    if(generation!==EXPERIENCE.locateGeneration || username!==EXPERIENCE.username) return;
    say(error&&error.code===1
      ? 'Location permission was denied. Allow it in your browser settings to see where you are.'
      : 'Your location was unavailable. Try again.',true);
  },{enableHighAccuracy:true,timeout:12000,maximumAge:60000});
}

// Phones show either the map or the list; wider screens show both side by side.
function setCourseLayout(mode,remember=true){
  const layout=mode==='list'?'list':'map';
  const view=document.getElementById('view-courses'); if(!view) return;
  view.classList.toggle('courses-show-list',layout==='list');
  view.classList.toggle('courses-show-map',layout==='map');
  document.getElementById('course-show-map')?.setAttribute('aria-pressed',String(layout==='map'));
  document.getElementById('course-show-list')?.setAttribute('aria-pressed',String(layout==='list'));
  if(remember){ try{ localStorage.setItem('vf_course_layout',layout); }catch(e){} }
  if(layout==='map') setTimeout(()=>{
    if(EXPERIENCE.courseMap) EXPERIENCE.courseMap.invalidateSize();
    renderCourseMap(filteredCourses());
  },0);
}

function initialCourseLayout(){
  try{ return localStorage.getItem('vf_course_layout')==='list'?'list':'map'; }catch(e){ return 'map'; }
}
setCourseLayout(initialCourseLayout(),false);

// Builds the one Leaflet map instance for the page, the first time the
// Courses view is shown. renderCourseMap gates the call so this never runs
// against a hidden, zero-size container.
function ensureCourseMap(){
  if(EXPERIENCE.courseMap || typeof L==='undefined') return EXPERIENCE.courseMap;
  const el=document.getElementById('course-map'); if(!el) return null;
  const map=L.map(el,{
    crs:VillagesCRS, minZoom:0, maxZoom:10, zoomSnap:0.5, zoomDelta:1,
    attributionControl:false, zoomControl:true,
    maxBounds:_COURSE_MAP_MAX_BOUNDS, maxBoundsViscosity:0.8, tap:true,
  });
  L.tileLayer(VILLAGES_TILE_URL,_VILLAGES_TILE_LAYER_OPTIONS).addTo(map);
  new ResetMapControl().addTo(map);
  new LocateControl().addTo(map);
  EXPERIENCE.courseMarkers=(typeof L.markerClusterGroup==='function')
    ? L.markerClusterGroup({maxClusterRadius:44,showCoverageOnHover:false,spiderfyOnMaxZoom:true,zoomToBoundsOnClick:true,iconCreateFunction:courseClusterIcon})
    : L.layerGroup();
  EXPERIENCE.courseMarkers.addTo(map);
  // A long press is only a shortcut into the explicit picker. It never saves.
  map.on('contextmenu',event=>startHomeSelection(event.latlng));
  EXPERIENCE.courseMap=map;
  return map;
}

function renderCourseMapHome(){
  const map=EXPERIENCE.courseMap; if(!map) return;
  if(EXPERIENCE.courseHomeMarker){ map.removeLayer(EXPERIENCE.courseHomeMarker); EXPERIENCE.courseHomeMarker=null; }
  const home=EXPERIENCE.preferences.home; if(!home) return;
  const lat=safeNumber(home.latitude),lon=safeNumber(home.longitude);
  if(lat===null||lon===null) return;
  const icon=L.divIcon({className:'map-home-pin',html:'H',iconSize:[27,27],iconAnchor:[13.5,13.5]});
  EXPERIENCE.courseHomeMarker=L.marker([lat,lon],{
    icon, keyboard:false, alt:'Saved starting location', title:'Saved starting location',
  }).addTo(map);
}

function renderCourseMap(courses){
  const el=document.getElementById('course-map'); if(!el) return;
  if(typeof L==='undefined'){
    if(!el.querySelector('.map-fallback-note')){
      el.insertAdjacentHTML('beforeend','<span class="map-fallback-note">Map imagery unavailable. Pins show relative positions.</span>');
    }
    return;
  }
  const viewActive=!!document.getElementById('view-courses')?.classList.contains('active');
  if(!EXPERIENCE.courseMap && (!viewActive || !el.clientWidth)) return; // create lazily, only once visible
  const map=ensureCourseMap(); if(!map) return;
  if(EXPERIENCE.courseMapRendering) return; // moveend from our own fitBounds must not re-enter
  EXPERIENCE.courseMapRendering=true;
  try{ renderCourseMapLayers(map,courses); } finally { EXPERIENCE.courseMapRendering=false; }
}

function renderCourseMapLayers(map,courses){
  const located=locatedCourses();
  const mapVisible=map.getSize().x>0; // a hidden map (phone list view) cannot be framed yet
  if(mapVisible && !EXPERIENCE.courseMapFitted && located.length){
    const bounds=courseMapFitBounds();
    if(bounds){ EXPERIENCE.courseMapFitted=true; map.fitBounds(bounds,{padding:[24,24]}); } // flag first: fitBounds fires moveend synchronously
  }
  if(mapVisible && EXPERIENCE.courseMapFitPending){
    EXPERIENCE.courseMapFitPending=false;
    const filteredBounds=courseMapFitBounds(courses,false);
    if(filteredBounds){
      if(filteredBounds.getNorthEast().equals(filteredBounds.getSouthWest())) map.setView(filteredBounds.getCenter(),7);
      else map.fitBounds(filteredBounds,{padding:[32,32],maxZoom:7});
    }
  }

  // Rebuild markers only when the shown courses or favorites change. Panning,
  // zooming, and opening a cluster must never redraw them.
  const favorites=new Set(EXPERIENCE.preferences.favorites||[]);
  const markerSignature=courses.map(course=>`${course.id}${favorites.has(course.id)?'*':''}`).join('|');
  if(markerSignature!==EXPERIENCE.courseMarkerSignature){
    EXPERIENCE.courseMarkerSignature=markerSignature;
    EXPERIENCE.courseMarkers.clearLayers();
    const markers=[];
    courses.forEach((course,index)=>{
      const lat=safeNumber(course.latitude),lon=safeNumber(course.longitude); if(lat===null||lon===null) return;
      const favorite=favorites.has(course.id);
      const marker=L.marker([lat,lon],{icon:courseDivIcon(index,favorite),keyboard:true,alt:course.name,title:course.name,favorite});
      marker.on('click',()=>openCourseModal(course.id));
      markers.push(marker);
    });
    if(typeof EXPERIENCE.courseMarkers.addLayers==='function') EXPERIENCE.courseMarkers.addLayers(markers);
    else markers.forEach(marker=>marker.addTo(EXPERIENCE.courseMarkers));
  }
  renderCourseMapHome();
}

let _mapResizeTimer=null;
function _handleMapViewportChange(){
  clearTimeout(_mapResizeTimer);
  _mapResizeTimer=setTimeout(()=>{
    if(EXPERIENCE.courseMap) EXPERIENCE.courseMap.invalidateSize();
    if(EXPERIENCE.routeMap) EXPERIENCE.routeMap.invalidateSize();
  },200);
}
window.addEventListener('resize',_handleMapViewportChange);
window.addEventListener('orientationchange',_handleMapViewportChange);

function toggleFavorite(courseId){
  const favorites=new Set(EXPERIENCE.preferences.favorites||[]);
  if(favorites.has(courseId)) favorites.delete(courseId); else favorites.add(courseId);
  EXPERIENCE.courseMapFitPending=!!document.getElementById('course-favorites-only')?.checked;
  void updatePreferences({favorites:[...favorites]},'Favorites updated.').then(ok=>{
    if(!ok){ const status=document.getElementById('course-explorer-status'); if(status){ status.textContent='Could not update favorites. Try again.'; status.style.color='var(--ink-error)'; } }
  });
}

function detailItem(label,value){ return value===null||value===undefined||value===''?'':`<div class="detail-item"><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`; }

function openCourseModal(courseId,event){
  const course=EXPERIENCE.courses.find(c=>String(c.id)===String(courseId)); if(!course) return;
  EXPERIENCE.routeGeneration++; EXPERIENCE.gpsGeneration++;
  EXPERIENCE.activeCourse=course; EXPERIENCE.restoreFocus=event?.currentTarget||document.activeElement;
  const content=document.getElementById('course-modal-content');
  const fullAddress=[course.address,course.zip].filter(Boolean).join(', ');
  const facts=[course.course_type,course.holes?`${course.holes} holes`:null,course.par?`Par ${course.par}`:null].filter(Boolean).join(' · ');
  content.innerHTML=`<div class="detail-kicker">Course details</div><h2 id="course-modal-title">${esc(course.name)}</h2>
    ${facts?`<div class="course-facts">${esc(facts)}</div>`:''}
    <div class="detail-address">${esc(courseAreaDescription(course))}${fullAddress?`<br>${esc(fullAddress)}`:''}</div>
    <div class="detail-actions">
      <button type="button" class="fetch-btn cart-directions-btn" onclick="startCourseDirections('${esc(course.id)}',event)">Cart directions</button>
      <div class="detail-secondary-actions">
        ${course.phone?`<a class="detail-link" href="tel:${esc(String(course.phone).replace(/[^+\d]/g,''))}">Call ${esc(course.phone)}</a>`:''}
        ${course.course_url?`<a class="detail-link" href="${esc(course.course_url)}" target="_blank" rel="noopener">Official course page</a>`:''}
        ${course.scorecard_url?`<a class="detail-link" href="${esc(course.scorecard_url)}" target="_blank" rel="noopener">Scorecard</a>`:''}
      </div>
    </div>`;
  if(EXPERIENCE.routeMap){ EXPERIENCE.routeMap.remove(); EXPERIENCE.routeMap=null; }
  document.getElementById('route-panel').innerHTML='';
  const overlay=document.getElementById('course-overlay'); overlay.classList.add('show');
  document.body.style.overflow='hidden';
  setTimeout(()=>overlay.querySelector('.course-modal')?.focus(),0);
}

function closeCourseModal(){
  const overlay=document.getElementById('course-overlay');
  if(!overlay?.classList.contains('show')) return;
  EXPERIENCE.routeGeneration++; EXPERIENCE.gpsGeneration++;
  if(EXPERIENCE.originPicker?.mode==='route') closeOriginPicker(false);
  overlay.classList.remove('show'); document.body.style.overflow='';
  if(EXPERIENCE.routeMap){ EXPERIENCE.routeMap.remove(); EXPERIENCE.routeMap=null; }
  const restore=EXPERIENCE.restoreFocus; EXPERIENCE.restoreFocus=null;
  if(restore && document.contains(restore)) restore.focus();
}

function routeFromSavedHome(courseId){
  startCourseDirections(courseId);
}

function routeOriginRow(course,originLabel,showChange=true){
  return `<div class="route-origin-row"><div><span class="route-origin-label">From:</span> <strong>${esc(originLabel||'Choose a starting point')}</strong></div>${showChange?`<button type="button" class="detail-link route-origin-change" onclick="changeRouteOrigin('${esc(course.id)}')">Change</button>`:''}</div>`;
}

function startCourseDirections(courseId,event){
  const course=findCourse(courseId); if(!course) return;
  EXPERIENCE.activeCourse=course;
  if(event?.currentTarget) EXPERIENCE.restoreFocus=EXPERIENCE.restoreFocus||event.currentTarget;
  const home=EXPERIENCE.preferences.home;
  if(home && safeNumber(home.latitude)!==null && safeNumber(home.longitude)!==null){
    const origin={latitude:Number(home.latitude),longitude:Number(home.longitude)};
    void requestCourseRoute(course.id,origin,home.label||'Saved starting point');
  }else renderRouteOriginChoices(course);
}

function changeRouteOrigin(courseId){
  EXPERIENCE.routeGeneration++; EXPERIENCE.gpsGeneration++;
  if(EXPERIENCE.routeMap){ EXPERIENCE.routeMap.remove(); EXPERIENCE.routeMap=null; }
  renderRouteOriginChoices(findCourse(courseId));
}

function renderRouteOriginChoices(course,message=''){
  if(!course) return;
  const panel=document.getElementById('route-panel'); if(!panel) return;
  const home=EXPERIENCE.preferences.home;
  panel.innerHTML=`<div class="route-chooser"><h3>Cart directions to ${esc(course.name)}</h3>
    ${routeOriginRow(course,home?'Choose a different starting point':'Choose a starting point',false)}
    ${message?`<div class="err-banner show">${esc(message)}</div>`:''}
    <label class="favorite-filter route-save-choice"><input id="route-save-origin" type="checkbox"/> Save this as my starting point</label>
    <div class="route-origin-actions">
      ${home?`<button type="button" class="secondary-btn" onclick="routeFromSavedHome('${esc(course.id)}')">Use saved starting point</button>`:''}
      <button type="button" class="secondary-btn" onclick="useCurrentLocation(!!document.getElementById('route-save-origin')?.checked,'${esc(course.id)}')">Use current location</button>
      <button type="button" class="secondary-btn" onclick="chooseRouteOriginOnMap('${esc(course.id)}',event)">Choose on map</button>
    </div></div>`;
}

function renderRouteLoading(course,originLabel){
  const panel=document.getElementById('route-panel'); if(!panel||!course) return;
  panel.innerHTML=`<div class="route-review"><h3>Cart directions to ${esc(course.name)}</h3>${routeOriginRow(course,originLabel)}<div class="loader show"><div class="spin"></div><div class="loader-txt">Calculating cart route…</div></div></div>`;
}

async function requestCourseRoute(courseId,origin,originLabel){
  const panel=document.getElementById('route-panel');
  const course=findCourse(courseId); if(!panel||!course) return;
  const normalizedOrigin={latitude:safeNumber(origin?.latitude),longitude:safeNumber(origin?.longitude)};
  if(normalizedOrigin.latitude===null||normalizedOrigin.longitude===null){ renderRouteOriginChoices(course,'Choose a valid starting point.'); return; }
  EXPERIENCE.routeOrigin=normalizedOrigin; EXPERIENCE.routeOriginLabel=originLabel||'Selected starting point';
  const generation=++EXPERIENCE.routeGeneration;
  const username=EXPERIENCE.username;
  renderRouteLoading(course,EXPERIENCE.routeOriginLabel);
  const data=await jsonRequest('/api/course-route',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({course_id:courseId,origin})}).catch(()=>({error:'Connection error.'}));
  if(generation!==EXPERIENCE.routeGeneration || username!==EXPERIENCE.username || !document.getElementById('course-overlay')?.classList.contains('show') || String(EXPERIENCE.activeCourse?.id)!==String(courseId)) return;
  if(!data.success){
    panel.innerHTML=`<div class="route-review"><h3>Cart directions to ${esc(course.name)}</h3>${routeOriginRow(course,EXPERIENCE.routeOriginLabel)}<div class="err-banner show">${esc(data.error||'Could not calculate this route.')}</div><div class="route-origin-actions"><button type="button" class="secondary-btn" onclick="requestCourseRoute('${esc(course.id)}',EXPERIENCE.routeOrigin,EXPERIENCE.routeOriginLabel)">Try again</button><button type="button" class="secondary-btn" onclick="changeRouteOrigin('${esc(course.id)}')">Change starting point</button></div></div>`;
    return;
  }
  const turns=Array.isArray(data.directions)?data.directions:[];
  const networkDate=formatNetworkDate(data.network_updated);
  const minutes=Number(data.minutes);
  panel.innerHTML=`<div class="route-summary"><h3>Cart directions to ${esc(course.name)}</h3>${routeOriginRow(course,EXPERIENCE.routeOriginLabel)}
    <div class="route-preview-label">Cart route preview · estimated travel time</div>
    <div class="route-headline">${formatDistance(data.miles)} · ${Number.isFinite(minutes)?`about ${Math.round(minutes)} min`:'Travel time unavailable'}</div>
    ${routeMapMarkup(data.geometry)}
    ${turns.length?`<details class="route-details"><summary>Show directions</summary><ol class="route-turns">${turns.map(t=>`<li>${esc(t.text||'Continue')}<span class="route-turn-distance">${formatDistance(t.miles)}${t.minutes!=null?` · about ${Math.round(Number(t.minutes))} min`:''}</span></li>`).join('')}</ol></details>`:''}
    <details class="route-details route-about"><summary>About this route</summary><div class="route-meta">Calculated ${esc(formatTimestamp(data.calculated_at))} using the cart-road network dated ${esc(networkDate)}. This is a preview with written directions; it does not provide live or spoken navigation.</div>${data.attribution?`<div class="route-meta">${esc(data.attribution)}</div>`:''}${data.source_url?`<a class="detail-link" href="${esc(data.source_url)}" target="_blank" rel="noopener">Route provider details</a>`:''}</details></div>`;
  renderRouteMap(data.geometry,origin,EXPERIENCE.activeCourse);
}

function formatDistance(miles){ const n=Number(miles); return Number.isFinite(n)?`${n<10?n.toFixed(1):Math.round(n)} mi`:'Distance unavailable'; }
function formatTimestamp(value){ if(!value) return 'just now'; const d=new Date(value); return Number.isNaN(d.valueOf())?String(value):d.toLocaleString([], {dateStyle:'medium',timeStyle:'short'}); }
function formatNetworkDate(value){ if(!value) return 'date unavailable'; const d=new Date(value); return Number.isNaN(d.valueOf())?String(value):d.toLocaleDateString([], {year:'numeric',month:'short',day:'numeric'}); }

function routeGeometryPoints(geometry){
  const coords=geometry?.type==='LineString'&&Array.isArray(geometry.coordinates)?geometry.coordinates:[];
  // GeoJSON coordinate order is [lon, lat]; Leaflet wants [lat, lon].
  return coords.map(p=>[Number(p[1]),Number(p[0])]).filter(p=>Number.isFinite(p[0])&&Number.isFinite(p[1]));
}

function routeMapMarkup(geometry){
  return routeGeometryPoints(geometry).length>=2 ? '<div class="route-map" id="route-map"></div>' : '';
}

// A second, short-lived Leaflet map for the course modal's route preview.
// Torn down (see closeCourseModal and the .remove() below) so its listeners
// never leak across route requests or modal closes.
function renderRouteMap(geometry,origin,course){
  if(EXPERIENCE.routeMap){ EXPERIENCE.routeMap.remove(); EXPERIENCE.routeMap=null; }
  if(typeof L==='undefined') return;
  const points=routeGeometryPoints(geometry);
  if(points.length<2) return;
  const el=document.getElementById('route-map'); if(!el) return;
  const map=L.map(el,{crs:VillagesCRS, attributionControl:false, zoomControl:true});
  L.tileLayer(VILLAGES_TILE_URL,_VILLAGES_TILE_LAYER_OPTIONS).addTo(map);
  const line=L.polyline(points,{color:'#22643b',weight:3}).addTo(map);
  const originLat=safeNumber(origin?.latitude),originLon=safeNumber(origin?.longitude);
  if(originLat!==null&&originLon!==null){
    L.circleMarker([originLat,originLon],{radius:6,color:'#2b6cb0',weight:2,fillColor:'#2b6cb0',fillOpacity:1}).addTo(map);
  }
  const courseLat=safeNumber(course?.latitude),courseLon=safeNumber(course?.longitude);
  if(courseLat!==null&&courseLon!==null){
    L.circleMarker([courseLat,courseLon],{radius:6,color:'#b3872f',weight:2,fillColor:'#b3872f',fillOpacity:1}).addTo(map);
  }
  map.fitBounds(line.getBounds(),{padding:[16,16]});
  EXPERIENCE.routeMap=map;
}

function openLocationForName(name,event){
  const course=findCourse(name);
  if(course) openCourseModal(course.id,event);
  else showError(`Location details are not available for ${name||'this course'}.`);
}

function locationButton(name,label='Location'){
  const c=findCourse(name); if(!c) return '';
  return `<button type="button" class="location-btn" data-course="${esc(c.id)}" onclick="openCourseModal(this.dataset.course,event)">${esc(label)}</button>`;
}

function directionsButton(name,label='Cart directions'){
  const course=findCourse(name); if(!course) return '';
  return `<button type="button" class="mytimes-action-btn cart-directions-btn" data-course="${esc(course.id)}" onclick="openReservationDirections(this.dataset.course,event)">${esc(label)}</button>`;
}

function openReservationDirections(courseId,event){
  const course=findCourse(courseId);
  if(!course){ setMyTimesStatus('Directions are unavailable because this course is not in the location catalog.','error'); return; }
  openCourseModal(course.id,event);
  startCourseDirections(course.id);
}

// Exact search window and guest changes always invalidate visible results.
function exactTimeChanged(){
  const from=document.getElementById('search-time-from').value;
  const to=document.getElementById('search-time-to').value;
  if(from&&to&&from>to) showError('Exact time “From” must be earlier than “To”.'); else showError('');
  clearResults(); checkFetch();
}

// Guests are listed under the signed-in golfer, so a group of four has room for none.
function guestLimit(){ return Math.max(0, Math.min(3, 4 - S.golfers.length)); }
function renderGuestStepper(){
  document.getElementById('guestCount').textContent=String(S.guestCount);
  // Screen readers hear the count only when it changes.
  const spoken=document.getElementById('guestCountSpoken');
  const words=S.guestCount<=0?'No guests':(S.guestCount===1?'1 guest':S.guestCount+' guests');
  if(spoken&&spoken.textContent!==words) spoken.textContent=words;
  // interface.js disables every control in a locked section and re-enables only the ones it
  // tagged, so a button that must stay off after unlock is left untagged.
  const section=document.getElementById('sec-course');
  const locked=!!section&&section.classList.contains('dim');
  [[document.getElementById('guestMinus'),S.guestCount<=0],[document.getElementById('guestPlus'),S.guestCount>=guestLimit()]].forEach(([button,off])=>{
    if(locked){
      button.disabled=true;
      if(off) delete button.dataset.progressiveDisabled; else button.dataset.progressiveDisabled='true';
    }else{
      button.disabled=off;
      delete button.dataset.progressiveDisabled;
    }
  });
}
function changeGuests(delta){
  const next=Math.max(0,Math.min(guestLimit(),S.guestCount+delta));
  if(next!==S.guestCount){ S.guestCount=next; clearResults(); checkFetch(); }
  renderGuestStepper();
}
// Call after the golfer list changes: guests plus golfers never exceed four players.
function syncGuestLimit(){
  if(S.guestCount>guestLimit()){ S.guestCount=guestLimit(); clearResults(); }
  renderGuestStepper();
}

const baseClearResults=clearResults;
clearResults=function enhancedClearResults(){
  EXPERIENCE.teeGeneration++;
  EXPERIENCE.searchId=null; EXPERIENCE.searchSnapshot=null; EXPERIENCE.pendingSnapshot=null;
  baseClearResults();
};

const baseResetApp=resetApp;
resetApp=function enhancedResetApp(){
  EXPERIENCE.routeGeneration++; EXPERIENCE.gpsGeneration++;
  closeOriginPicker(false); clearRequestReview(false,true);
  if(document.getElementById('course-overlay')?.classList.contains('show')) closeCourseModal();
  baseResetApp();
};

function teeSearchPayload(){
  const from=document.getElementById('search-time-from')?.value||'';
  const to=document.getElementById('search-time-to')?.value||'';
  return {date:S.date,date_label:S.dateLabel,course_type:S.course,region_filter:S.regionFilter,
    golfer_ids:[...S.golfers],num_golfers:S.golfers.length,guest_count:S.guestCount,time_filter:S.timeFilter,
    time_from:from||'',time_to:to||''};
}

fetchTimes=async function enhancedFetchTimes(){
  showError('');
  const payload=teeSearchPayload();
  if(payload.time_from&&payload.time_to&&payload.time_from>payload.time_to){ showError('Exact time “From” must be earlier than “To”.'); return; }
  const generation=++EXPERIENCE.teeGeneration;
  const snapshot=JSON.stringify(payload);
  EXPERIENCE.searchId=null; EXPERIENCE.searchSnapshot=null;
  const fb=document.getElementById('fetchBtn'); fb.disabled=true;
  document.getElementById('results-sec').style.display='none'; document.getElementById('success-sec').style.display='none';
  const loader=document.getElementById('loaderSec'); loader.style.display='block';
  const ltxt=document.getElementById('loaderTxt');
  const messages=['Logging in to The Villages…','Navigating to golf system…','Fetching available tee times…','Almost there…'];
  let index=0; const interval=setInterval(()=>{ if(index<messages.length-1) ltxt.textContent=messages[++index]; },3500);
  try{
    const data=await jsonRequest('/api/tee-times',{method:'POST',headers:{'Content-Type':'application/json'},body:snapshot});
    if(generation!==EXPERIENCE.teeGeneration || JSON.stringify(teeSearchPayload())!==snapshot) return;
    if(!data.success){ showError(data.error||'Failed to fetch tee times.'); return; }
    EXPERIENCE.searchId=data.search_id||null; EXPERIENCE.searchSnapshot=snapshot;
    renderTimes(data.times,data.date_label||S.dateLabel);
  }catch(e){ if(generation===EXPERIENCE.teeGeneration) showError('Network error — check your connection.'); }
  finally{ clearInterval(interval); if(generation===EXPERIENCE.teeGeneration){ loader.style.display='none'; checkFetch(); } }
};

renderTimeRows=function enhancedRenderTimeRows(){
  const list=document.getElementById('timeList'); list.innerHTML='';
  const times=S.courseFilter==='all'?S.allTimes:S.allTimes.filter(t=>t.course===S.courseFilter);
  if(!times.length){ list.innerHTML='<div class="no-times">No tee times found for that preference.<br>Try a different time or date.</div>'; return; }
  if(EXPERIENCE.weatherUpdatedAt && EXPERIENCE.weatherHourly.length){
    list.insertAdjacentHTML('beforeend',`<div class="weather-list-updated">Forecast updated ${esc(formatTimestamp(EXPERIENCE.weatherUpdatedAt))}</div>`);
  }
  // The whole card chooses the time; directions stay a separate, quieter link.
  times.forEach(t=>{
    const row=document.createElement('article'); row.className='time-row';
    row.innerHTML=`<button type="button" class="time-row-main"><span class="tr-top"><span class="tr-time">${esc(t.display_time)}</span><span class="tr-avail">${esc(t.available)} open</span></span>
      <span class="tr-course">${esc(t.course)}</span>${weatherForTeeTime(t)}<span class="tr-choose">Choose this time <span aria-hidden="true">&#8250;</span></span></button>
      <div class="time-row-actions">${locationButton(t.course,'Course & directions')}</div>`;
    row.querySelector('.time-row-main').onclick=()=>openConfirm(t); list.appendChild(row);
  });
};

function parseDisplayHour(display){
  const match=String(display||'').match(/(\d{1,2}):(\d{2})\s*([AP]M)/i); if(!match) return null;
  let hour=Number(match[1])%12; if(match[3].toUpperCase()==='PM') hour+=12;
  return {hour,minute:Number(match[2])};
}

function weatherForTeeTime(item){
  const hm=parseDisplayHour(item.display_time); if(!hm||!S.date||!EXPERIENCE.weatherHourly.length) return '';
  const iso=`${S.date.slice(0,4)}-${S.date.slice(4,6)}-${S.date.slice(6,8)}T${String(hm.hour).padStart(2,'0')}:${String(hm.minute).padStart(2,'0')}`;
  let best=null,diff=Infinity;
  EXPERIENCE.weatherHourly.forEach(w=>{ const d=Math.abs(new Date(w.time).valueOf()-new Date(iso).valueOf()); if(d<diff){diff=d;best=w;} });
  if(!best||diff>5400000) return '';
  return `<span class="weather-detail">${esc(WX_LABELS[best.code]||'Forecast')} · ${Math.round(best.temp)}°F · ${Math.round(best.rain||0)}% rain</span>`;
}

fetchWeather=async function enhancedFetchWeather(){
  if(_wxCache) return _wxCache;
  try{
    const url='https://api.open-meteo.com/v1/forecast?latitude=28.93&longitude=-81.98&daily=temperature_2m_max,temperature_2m_min,weather_code&hourly=temperature_2m,apparent_temperature,precipitation_probability,weather_code&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7';
    const response=await fetch(url),data=await response.json();
    _wxCache={};
    (data.daily?.time||[]).forEach((date,i)=>{ _wxCache[date]={hi:Math.round(data.daily.temperature_2m_max[i]),lo:Math.round(data.daily.temperature_2m_min[i]),code:data.daily.weather_code[i]}; });
    EXPERIENCE.weatherHourly=(data.hourly?.time||[]).map((time,i)=>({time,temp:data.hourly.temperature_2m[i],feels:data.hourly.apparent_temperature[i],rain:data.hourly.precipitation_probability[i],code:data.hourly.weather_code[i]}));
    EXPERIENCE.weatherUpdatedAt=new Date().toISOString();
    return _wxCache;
  }catch(e){ return null; }
};

openConfirm=function enhancedOpenConfirm(item){
  const current=teeSearchPayload();
  if(!EXPERIENCE.searchSnapshot || JSON.stringify(current)!==EXPERIENCE.searchSnapshot){ clearResults(); showError('Those results are out of date. Search again.'); return; }
  S.pendingCourse=item.course; S.pendingTime=item.time; S.pendingDisplay=item.display_time;
  EXPERIENCE.pendingSnapshot=Object.freeze({...current,golfer_ids:Object.freeze([...current.golfer_ids]),course:item.course,time:item.time,search_id:EXPERIENCE.searchId});
  const names=S.golfers.map(id=>esc(BUDDIES.find(g=>g.id===id)?.name.split(' ')[0]||id)).join(', ');
  document.getElementById('cs-detail').innerHTML=`<div class="cs-row"><span>Date</span><span>${esc(S.dateLabel)}</span></div><div class="cs-row"><span>Time</span><span>${esc(item.display_time)}</span></div><div class="cs-row"><span>Course</span><span>${esc(item.course)}</span></div><div class="cs-row"><span>Golfers</span><span>${names}</span></div><div class="cs-row"><span>Guests</span><span>${S.guestCount?(S.guestCount===1?'1 guest':S.guestCount+' guests'):'None'}</span></div><div class="location-actions">${locationButton(item.course,'Course details & route')}</div>`;
  const overlay=document.getElementById('overlay'); overlay.classList.add('show'); overlay.querySelector('.confirm-sheet').setAttribute('role','dialog'); overlay.querySelector('.confirm-sheet').setAttribute('aria-modal','true');
  document.getElementById('cs-book-btn').disabled=false; document.getElementById('cs-book-btn').textContent='Book It!';
};

confirmBook=async function enhancedConfirmBook(){
  if(BOOKING_IN_FLIGHT) return;
  const payload=EXPERIENCE.pendingSnapshot;
  if(!payload || JSON.stringify(teeSearchPayload())!==EXPERIENCE.searchSnapshot){ closeConfirm(); clearResults(); showError('Your choices changed. Search again before booking.'); return; }
  BOOKING_IN_FLIGHT=true;
  const btn=document.getElementById('cs-book-btn'),cancel=document.querySelector('#overlay .cs-cancel');
  btn.disabled=true; btn.textContent='Booking…'; if(cancel) cancel.disabled=true;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),120000);
  try{
    const data=await jsonRequest('/api/book',{method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify(payload)});
    closeConfirm();
    if(!data.success){
      if(data.unknown) bookingUnconfirmed(data.error||'We could not confirm your booking. Check My Tee Times before trying again.');
      else showError(data.error||'Booking failed.');
      return;
    }
    showSuccess(data);
  }catch(e){ closeConfirm(); bookingUnconfirmed('We could not confirm your booking. Check My Tee Times to see whether it went through before trying again.'); }
  finally{ clearTimeout(timeout); BOOKING_IN_FLIGHT=false; btn.disabled=false; btn.textContent='Book It!'; if(cancel) cancel.disabled=false; }
};

const baseBookingUnconfirmed=bookingUnconfirmed;
// The unconfirmed-booking message, including any reason the Villages site gave,
// stays above the list while My Tee Times reloads instead of being overwritten.
function showBookingNotice(message){
  const notice=document.getElementById('myTimesNotice');
  if(!notice) return;
  notice.textContent=message||'';
  notice.hidden=!message;
}
bookingUnconfirmed=function invalidatingBookingUnconfirmed(message){
  EXPERIENCE.myTimesLoaded=false; // My Tee Times must reload to verify the unknown booking
  showBookingNotice(message);
  baseBookingUnconfirmed('Checking your reservations…');
};

const baseShowSuccess=showSuccess;
showSuccess=function enhancedShowSuccess(data){
  showBookingNotice('');
  baseShowSuccess(data);
  const courseName=data.course||S.pendingCourse;
  const card=document.getElementById('conf-card');
  if(card) card.insertAdjacentHTML('beforeend',`<div class="location-actions">${directionsButton(courseName,'Cart directions')}<button type="button" class="location-btn calendar-btn" onclick="downloadBookingCalendar()">Add to calendar</button></div>${S.email?'':emailNudgeMarkup('Add an email in Account to get booking confirmations.')}`);
  EXPERIENCE.myTimesLoaded=false; // the next visit to My Tee Times shows the new booking
  EXPERIENCE.lastBooking=Object.assign({},EXPERIENCE.pendingSnapshot,data,{date:data.play_date||S.date,date_label:data.date_label||S.dateLabel,course:courseName,display_time:data.display_time||S.pendingDisplay});
};

const baseRenderMyTeeTimes=renderMyTeeTimes;
renderMyTeeTimes=function enhancedRenderMyTeeTimes(items){
  const sorted=sortReservations(items||[]);
  baseRenderMyTeeTimes(sorted);
  document.querySelectorAll('#myTimesList .mytimes-card').forEach((card,index)=>{
    const item=sorted[index]; if(!item) return;
    const actions=card.querySelector('.mytimes-btn-row')||card.appendChild(Object.assign(document.createElement('div'),{className:'mytimes-btn-row'}));
    const c=findCourse(item.course_id||item.course);
    if(c) actions.insertAdjacentHTML('afterbegin',directionsButton(c.id,'Cart directions'));
    // Keep the destructive delete last, away from the directions button.
    const calendar=`<button type="button" class="mytimes-action-btn calendar-btn" onclick="downloadReservationCalendar(${index})">Add to calendar</button>`;
    const remove=actions.querySelector('.mytimes-action-btn.delete');
    if(calendarDataFor(item)){ if(remove) remove.insertAdjacentHTML('beforebegin',calendar); else actions.insertAdjacentHTML('beforeend',calendar); }
  });
};

function reservationTimestamp(item){
  const cal=calendarDataFor(item); if(!cal) return Number.POSITIVE_INFINITY;
  const d=cal.date,hm=cal.hm;
  const value=new Date(`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}T${String(hm.hour).padStart(2,'0')}:${String(hm.minute).padStart(2,'0')}:00`).valueOf();
  return Number.isFinite(value)?value:Number.POSITIVE_INFINITY;
}

function sortReservations(items){
  const now=Date.now();
  return (items||[]).map((item,index)=>({item,index,time:reservationTimestamp(item)}))
    .sort((a,b)=>{
      const aGroup=!Number.isFinite(a.time)?2:a.time>=now?0:1;
      const bGroup=!Number.isFinite(b.time)?2:b.time>=now?0:1;
      if(aGroup!==bGroup) return aGroup-bGroup;
      if(aGroup===1) return b.time-a.time||a.index-b.index;
      return a.time-b.time||a.index-b.index;
    }).map(entry=>entry.item);
}

const baseLoadMyTeeTimes=loadMyTeeTimes;
loadMyTeeTimes=async function guardedLoadMyTeeTimes(){
  if(EXPERIENCE.myTimesLoading) return;
  const generation=++EXPERIENCE.myTimesGeneration;
  const btn=document.getElementById('myTimesBtn'),list=document.getElementById('myTimesList');
  EXPERIENCE.myTimesLoading=true;
  btn.disabled=true; setMyTimesStatus('Loading your reservations…'); list.innerHTML=''; showAppView('mytimes');
  try{
    const data=await jsonRequest('/api/my-tee-times'); if(generation!==EXPERIENCE.myTimesGeneration) return;
    if(data.success){ EXPERIENCE.myTimesLoaded=true; EXPERIENCE.myTimesAutoFailed=false; setMyTimesStatus(`Found ${(data.reservations||[]).length} reservation${(data.reservations||[]).length===1?'':'s'}.`); renderMyTeeTimes(data.reservations||[]); }
    else{ EXPERIENCE.myTimesAutoFailed=true; setMyTimesStatus(data.error||'Could not load your tee times.','error'); }
  }catch(e){ if(generation===EXPERIENCE.myTimesGeneration){ EXPERIENCE.myTimesAutoFailed=true; setMyTimesStatus('Connection error. Try again.','error'); } }
  finally{ if(generation===EXPERIENCE.myTimesGeneration){ EXPERIENCE.myTimesLoading=false; btn.disabled=false; } }
};

confirmDeleteReservation=async function verifiedDeleteReservation(){
  const reservationNo=DELETE_PENDING?.reservation_no; if(!reservationNo) return;
  const btn=document.getElementById('delete-confirm-btn'); btn.disabled=true; btn.textContent='Deleting…';
  try{
    const data=await jsonRequest('/api/delete-reservation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reservation_no:reservationNo})});
    closeDeleteConfirm();
    if(data.success){ setMyTimesStatus(data.message||'Reservation deleted.','success'); if(Array.isArray(data.reservations)) renderMyTeeTimes(data.reservations); else await loadMyTeeTimes(); }
    else if(data.unknown){ setMyTimesStatus('The delete result is unknown. Refreshing reservations to verify…','error'); await loadMyTeeTimes(); }
    else setMyTimesStatus(data.error||'Could not delete reservation.','error');
  }catch(e){ closeDeleteConfirm(); setMyTimesStatus('The delete result is unknown. Refreshing reservations to verify…','error'); await loadMyTeeTimes(); }
};

function calendarDataFor(item){
  const rawDate=item.play_date||item.date_iso||item.date;
  const rawTime=item.start_time||item.display_time||item.time;
  const date=parseReservationDate(rawDate),hm=parseCalendarTime(rawTime);
  return date&&hm?{date,hm}:null;
}

function parseReservationDate(value){
  const s=String(value||'');
  let m=s.match(/^(\d{4})-?(\d{2})-?(\d{2})$/); if(m) return `${m[1]}${m[2]}${m[3]}`;
  const d=new Date(s); if(Number.isNaN(d.valueOf())) return null;
  return `${d.getFullYear()}${String(d.getMonth()+1).padStart(2,'0')}${String(d.getDate()).padStart(2,'0')}`;
}

function parseCalendarTime(value){
  const s=String(value||''); let m=s.match(/(\d{1,2}):(\d{2})\s*([AP]M)/i);
  if(m){ let h=Number(m[1])%12;if(m[3].toUpperCase()==='PM')h+=12;return {hour:h,minute:Number(m[2])}; }
  m=s.match(/^(\d{1,2}):(\d{2})$/); return m?{hour:Number(m[1]),minute:Number(m[2])}:null;
}

function icsEscape(value){ return String(value||'').replace(/\\/g,'\\\\').replace(/\n/g,'\\n').replace(/,/g,'\\,').replace(/;/g,'\\;'); }
function toLocalIcs(date,hm){ return `${date}T${String(hm.hour).padStart(2,'0')}${String(hm.minute).padStart(2,'0')}00`; }
function addMinutesLocal(date,hm,minutes){ const d=new Date(`${date.slice(0,4)}-${date.slice(4,6)}-${date.slice(6,8)}T${String(hm.hour).padStart(2,'0')}:${String(hm.minute).padStart(2,'0')}:00`); d.setMinutes(d.getMinutes()+minutes); return `${d.getFullYear()}${String(d.getMonth()+1).padStart(2,'0')}${String(d.getDate()).padStart(2,'0')}T${String(d.getHours()).padStart(2,'0')}${String(d.getMinutes()).padStart(2,'0')}00`; }
function downloadIcs(item){
  const cal=calendarDataFor(item); if(!cal) return;
  const course=findCourse(item.course_id||item.course),address=course?.address||item.address||'';
  const uid=`${item.reservation_no||Date.now()}-${cal.date}@villagefairways`;
  const lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Village Fairways//Tee Time//EN','CALSCALE:GREGORIAN','BEGIN:VEVENT',`UID:${icsEscape(uid)}`,`DTSTAMP:${new Date().toISOString().replace(/[-:]/g,'').replace(/\.\d{3}/,'')}`,`DTSTART:${toLocalIcs(cal.date,cal.hm)}`,`DTEND:${addMinutesLocal(cal.date,cal.hm,270)}`,`SUMMARY:${icsEscape(`Tee time · ${item.course||course?.name||'Golf'}`)}`];
  if(address) lines.push(`LOCATION:${icsEscape(address)}`);
  if(item.reservation_no) lines.push(`DESCRIPTION:${icsEscape(`Reservation #${item.reservation_no}`)}`);
  lines.push('END:VEVENT','END:VCALENDAR','');
  const blob=new Blob([lines.join('\r\n')],{type:'text/calendar;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=url;a.download=`tee-time-${cal.date}.ics`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function downloadReservationCalendar(index){ const item=MY_TEE_TIMES[index]; if(item) downloadIcs(item); }
function downloadBookingCalendar(){ if(EXPERIENCE.lastBooking) downloadIcs(EXPERIENCE.lastBooking); }

// Requests: reset dependent choices when date, type, or any-course changes.
function resetRequestState(){
  clearRequestReview(false);
  Object.assign(SR,{date:null,maxGolfers:null,course:null,guests:false,anyCourse:false,timeToPlay:'',earliest:'',latest:'',preference:'Course',availableCourses:[],closedCourses:[],selectedCourses:[],golfers:[],courseSearch:''});
  const picker=document.getElementById('rq-course-picker'); if(picker) picker.style.display='none';
  const status=document.getElementById('rq-course-status'); if(status) status.textContent='';
  const list=document.getElementById('rq-list'); if(list) list.innerHTML='';
}

function clearRequestCourses(){
  clearRequestReview(false);
  EXPERIENCE.requestGeneration++;
  SR.availableCourses=[]; SR.closedCourses=[]; SR.selectedCourses=[]; SR.courseSearch='';
  const picker=document.getElementById('rq-course-picker'); if(picker) picker.style.display='none';
  const search=document.getElementById('rq-search'); if(search) search.value='';
  const status=document.getElementById('rq-course-status'); if(status) status.textContent='Date or course choice changed. Load courses again.';
  rqRenderSelected();
}

function requestInputsChanged(){ clearRequestReview(false); clearRequestCourses(); }
function requestAnyCourseChanged(checked){ SR.anyCourse=!!checked; const note=document.getElementById('rq-any-course-note'); if(note) note.style.display=checked?'block':'none'; clearRequestCourses(); }

const baseSetRequestMax=setRequestMax;
setRequestMax=function enhancedSetRequestMax(n){
  clearRequestReview(false);
  if(SR.golfers.length>n) SR.golfers=SR.golfers.slice(0,n);
  baseSetRequestMax(n);
  for(let i=1;i<=4;i++) document.getElementById(`rq-max-${i}`).setAttribute('aria-pressed',String(i===n));
  const sub=document.querySelector('#rq-golfers-sec .sec-sub'); if(sub) sub.textContent=`Up to ${n} player${n===1?'':'s'} in this request`;
};

const baseSetRequestCourse=setRequestCourse;
setRequestCourse=function enhancedSetRequestCourse(course){ clearRequestReview(false); const changed=SR.course!==course; baseSetRequestCourse(course); document.getElementById('rq-exec').setAttribute('aria-pressed',String(course==='Executive'));document.getElementById('rq-champ').setAttribute('aria-pressed',String(course==='Championship'));if(changed) clearRequestCourses(); };

const baseSetRequestPref=setRequestPref;
setRequestPref=function enhancedSetRequestPref(preference){clearRequestReview(false);baseSetRequestPref(preference);document.getElementById('rq-pref-course').setAttribute('aria-pressed',String(preference==='Course'));document.getElementById('rq-pref-time').setAttribute('aria-pressed',String(preference==='Time'));};

loadRequestCourses=async function guardedLoadRequestCourses(){
  const status=document.getElementById('rq-course-status'),picker=document.getElementById('rq-course-picker');
  const play_date=rqFormatDateForApi(); SR.anyCourse=!!document.getElementById('rq-anycourse').checked;
  if(!play_date){ status.textContent='Pick a date first.'; status.style.color='var(--ink-error)'; return; }
  if(!SR.course){ status.textContent='Pick a course type first.'; status.style.color='var(--ink-error)'; return; }
  const generation=++EXPERIENCE.requestGeneration,snapshot=JSON.stringify({play_date,course_type:SR.course,any_course:SR.anyCourse});
  status.textContent='Loading courses…'; status.style.color='var(--muted)';
  const data=await jsonRequest('/api/request-courses',{method:'POST',headers:{'Content-Type':'application/json'},body:snapshot}).catch(()=>({error:'Connection error.'}));
  const current=JSON.stringify({play_date:rqFormatDateForApi(),course_type:SR.course,any_course:!!document.getElementById('rq-anycourse').checked});
  if(generation!==EXPERIENCE.requestGeneration||snapshot!==current) return;
  if(data.success){
    SR.availableCourses=data.courses||[];SR.closedCourses=data.closed_courses||[];SR.selectedCourses=[];SR.courseSearch='';
    document.getElementById('rq-search').value='';rqReopen();rqRenderAvailable();rqRenderSelected();picker.style.display='block';
    status.textContent=`Loaded ${SR.availableCourses.length} open courses${SR.closedCourses.length?` (${SR.closedCourses.length} closed for this date)`:''}.`;status.style.color='var(--ink-accent)';
  }else{ status.textContent=data.error||'Could not load courses.';status.style.color='var(--ink-error)'; }
};

const baseRqRenderSelected=rqRenderSelected;
rqRenderSelected=function enhancedRqRenderSelected(){
  baseRqRenderSelected();
  document.querySelectorAll('#rq-selected .rq-sel-row').forEach((row,index)=>{
    const course=SR.selectedCourses[index]; if(findCourse(course)) row.insertAdjacentHTML('beforeend',locationButton(course,'Location'));
  });
};

const baseToggleRequestGolfer=toggleRequestGolfer;
toggleRequestGolfer=function reviewedToggleRequestGolfer(id){ clearRequestReview(false); baseToggleRequestGolfer(id); };
const baseRqAddCourse=rqAddCourse;
rqAddCourse=function reviewedRqAddCourse(name){ clearRequestReview(false); baseRqAddCourse(name); };
const baseRqRemoveAt=rqRemoveAt;
rqRemoveAt=function reviewedRqRemoveAt(index){ clearRequestReview(false); baseRqRemoveAt(index); };
const baseRqMove=rqMove;
rqMove=function reviewedRqMove(index,direction){ clearRequestReview(false); baseRqMove(index,direction); };
const baseRqUseUsual=rqUseUsual;
rqUseUsual=function reviewedRqUseUsual(){ clearRequestReview(false); baseRqUseUsual(); };

rqRecentsKey=function accountScopedRequestRecents(){ return `vf_usual_courses_${EXPERIENCE.username||'signed-out'}_${SR.course||''}`; };

loadMyRequests=async function guardedLoadMyRequests(){
  const generation=++EXPERIENCE.requestListGeneration;
  const status=document.getElementById('rq-list-status'),list=document.getElementById('rq-list');
  status.textContent='Loading…';status.style.color='var(--muted)';list.innerHTML='';
  const data=await jsonRequest('/api/my-requests').catch(()=>({error:'Connection error.'}));
  if(generation!==EXPERIENCE.requestListGeneration) return;
  if(!data.success){status.textContent=data.error||'Could not load requests.';status.style.color='var(--ink-error)';return;}
  const items=data.requests||[];
  if(!items.length){list.innerHTML='<div class="mytimes-empty">No open requests.</div>';status.textContent='';return;}
  list.innerHTML=items.map(item=>{
    const requestId=String(item.request_id||'');
    const names=Array.isArray(item.course_choices)?item.course_choices:(item.course?[item.course]:[]);
    const locations=names.map(name=>locationButton(name,'Location')).join('');
    return `<article class="mytimes-card"><div class="mytimes-top"><div class="mytimes-course">${esc(item.name||'Request')}</div><div class="mytimes-time">${esc(item.date||item.play_date||'')}</div></div>
      <div class="mytimes-meta">${esc(item.action||'')}</div><div class="mytimes-meta">Request #${esc(requestId)}</div>
      <div class="mytimes-btn-row">${locations}<button type="button" class="mytimes-action-btn delete" data-request-id="${esc(requestId)}" onclick="deleteRequest(this.dataset.requestId)">Cancel Request</button></div></article>`;
  }).join('');status.textContent='';
};

function requestPayloadFromForm(){
  return {
    play_date:rqFormatDateForApi(),
    max_golfers:SR.maxGolfers,
    has_guests:!!document.getElementById('rq-guests')?.checked,
    course_type:SR.course,
    any_course:!!document.getElementById('rq-anycourse')?.checked,
    time_to_play:document.getElementById('rq-time')?.value.trim()||'',
    earliest_time:document.getElementById('rq-earliest')?.value.trim()||'',
    latest_time:document.getElementById('rq-latest')?.value.trim()||'',
    preference:SR.preference,
    course_choices:[...SR.selectedCourses],
    golfer_ids:[...SR.golfers],
  };
}

function validateRequestPayload(payload){
  if(!payload.play_date) return 'Pick a play date.';
  if(!payload.max_golfers) return 'Pick max number of golfers.';
  if(!payload.course_type) return 'Pick a course type.';
  if(!payload.course_choices.length) return 'Add at least one course choice.';
  if(!payload.golfer_ids.length) return 'Pick at least one golfer.';
  if(!payload.time_to_play||!payload.earliest_time||!payload.latest_time) return 'Choose a preferred, earliest, and latest time.';
  if(payload.golfer_ids.length>(payload.max_golfers||4)) return 'Selected golfers exceed the group maximum.';
  if(payload.earliest_time&&payload.latest_time&&payload.earliest_time>payload.latest_time) return 'Earliest time must precede latest time.';
  if(payload.time_to_play&&payload.earliest_time&&payload.time_to_play<payload.earliest_time || payload.time_to_play&&payload.latest_time&&payload.time_to_play>payload.latest_time) return 'Preferred time must fall inside the acceptable window.';
  return '';
}

// "13:05" -> "1:05 PM" for the review summary; unknown input is shown as typed.
function clockLabel(value){
  const match=/^(\d{1,2}):(\d{2})/.exec(String(value||''));
  if(!match) return String(value||'');
  const hour=Number(match[1]);
  if(hour>23) return String(value);
  return `${hour%12||12}:${match[2]} ${hour<12?'AM':'PM'}`;
}

function requestReviewMarkup(payload){
  const golferNames=payload.golfer_ids.map(id=>BUDDIES.find(g=>String(g.id)===String(id))?.name||id).join(', ');
  const dateInput=document.getElementById('rq-date')?.value;
  const date=dateInput?new Date(`${dateInput}T12:00:00`).toLocaleDateString([], {weekday:'short',month:'short',day:'numeric',year:'numeric'}):payload.play_date;
  return `<div class="request-review-heading">Review your request</div><div class="cs-detail">
    <div class="cs-row"><span>Date</span><span>${esc(date)}</span></div>
    <div class="cs-row"><span>Golfers</span><span>${esc(golferNames)}</span></div>
    <div class="cs-row"><span>Course type</span><span>${esc(payload.course_type)}</span></div>
    <div class="cs-row"><span>Ranked courses</span><span>${payload.course_choices.map((name,index)=>`${index+1}. ${esc(name)}`).join('<br>')}</span></div>
    <div class="cs-row"><span>Preferred time</span><span>${esc(clockLabel(payload.time_to_play))}</span></div>
    <div class="cs-row"><span>Acceptable times</span><span>${esc(clockLabel(payload.earliest_time))} to ${esc(clockLabel(payload.latest_time))}</span></div>
    <div class="cs-row"><span>Most important</span><span>${payload.preference==='Time'?'Staying near my preferred time':'Keeping my course ranking'}</span></div>
    <div class="cs-row"><span>Guests</span><span>${payload.has_guests?'Guests included':'No guests'}</span></div>
    <div class="cs-row"><span>Other courses</span><span>${payload.any_course?'Any course of this type is OK':'Only my ranked courses'}</span></div>
  </div><p class="field-help">Submitting sends this request for assignment. It is not a confirmed tee time.</p>
  <div class="request-review-actions"><button type="button" class="secondary-btn" onclick="cancelRequestReview()">Go back</button><button type="button" class="fetch-btn" onclick="confirmSubmitRequest()">Submit request</button></div>`;
}

function clearRequestReview(restoreFocus=true,force=false){
  if(EXPERIENCE.requestSubmissionInFlight&&!force) return;
  const review=document.getElementById('rq-review');
  if(review) review.remove();
  EXPERIENCE.requestReviewSnapshot=null;
  const button=document.getElementById('rq-submit-btn');
  if(button){ button.hidden=false; button.disabled=false; if(restoreFocus) button.focus(); }
}

function cancelRequestReview(){ clearRequestReview(true); }

submitRequest=function reviewRequestBeforeSubmit(){
  const error=document.getElementById('rq-err'),status=document.getElementById('rq-status'),button=document.getElementById('rq-submit-btn');
  error.textContent='';error.classList.remove('show');status.textContent='';
  const payload=requestPayloadFromForm();
  const problem=validateRequestPayload(payload);
  if(problem){ error.textContent=problem; error.classList.add('show'); return; }
  clearRequestReview(false);
  const snapshot=JSON.stringify(payload);
  EXPERIENCE.requestReviewSnapshot=snapshot;
  const review=document.createElement('section'); review.id='rq-review'; review.className='request-review'; review.setAttribute('tabindex','-1'); review.innerHTML=requestReviewMarkup(payload);
  button.insertAdjacentElement('beforebegin',review); button.hidden=true;
  review.focus();
};

async function confirmSubmitRequest(){
  if(EXPERIENCE.requestSubmissionInFlight) return;
  const error=document.getElementById('rq-err'),status=document.getElementById('rq-status');
  const snapshot=EXPERIENCE.requestReviewSnapshot;
  if(!snapshot) return;
  const current=JSON.stringify(requestPayloadFromForm());
  if(current!==snapshot){ clearRequestReview(false); error.textContent='Your request choices changed. Review the updated request before submitting.'; error.classList.add('show'); return; }
  const generation=++EXPERIENCE.requestSubmitGeneration;
  EXPERIENCE.requestSubmissionInFlight=true;
  const reviewButtons=[...document.querySelectorAll('#rq-review button')]; reviewButtons.forEach(button=>button.disabled=true);
  status.textContent='Submitting request…';status.style.color='var(--muted)';
  const data=await jsonRequest('/api/submit-request',{method:'POST',headers:{'Content-Type':'application/json'},body:snapshot}).catch(()=>({unknown:true}));
  if(generation!==EXPERIENCE.requestSubmitGeneration){ EXPERIENCE.requestSubmissionInFlight=false; return; } // account changed
  EXPERIENCE.requestSubmissionInFlight=false;
  reviewButtons.forEach(button=>button.disabled=false);
  if(data.success){clearRequestReview(false);rqSaveRecents();status.textContent=data.message||`Request #${data.request_no||'submitted'}`;status.style.color='var(--ink-accent)';await loadMyRequests();}
  else if(data.unknown){clearRequestReview(false);status.textContent='The submission result is unknown. Refreshing open requests to verify before you try again…';status.style.color='var(--ink-error)';await loadMyRequests();}
  else{status.textContent=data.error||'Submission failed.';status.style.color='var(--ink-error)';}
}

deleteRequest=async function verifiedDeleteRequest(requestId){
  if(!confirm(`Cancel request #${requestId}?`)) return;
  const status=document.getElementById('rq-list-status');status.textContent='Cancelling…';status.style.color='var(--muted)';
  const data=await jsonRequest('/api/delete-request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:requestId})}).catch(()=>({unknown:true}));
  if(data.success){status.textContent='Cancelled.';status.style.color='var(--ink-accent)';await loadMyRequests();}
  else if(data.unknown){status.textContent='The cancellation result is unknown. Refreshing requests to verify…';status.style.color='var(--ink-error)';await loadMyRequests();}
  else{status.textContent=data.error||'Cancel failed.';status.style.color='var(--ink-error)';}
};

function setRequestDateMinimum(value){
  const input=document.getElementById('rq-date'); if(!input||!value) return;
  const s=String(value),m=s.match(/^(\d{4})-?(\d{2})-?(\d{2})$/); if(m) input.min=`${m[1]}-${m[2]}-${m[3]}`;
}

function renderPresetSelect(){
  const select=document.getElementById('preset-select'); if(!select) return;
  const current=select.value;
  select.innerHTML='<option value="">Choose a saved group</option>'+EXPERIENCE.preferences.presets.map((p,i)=>`<option value="${i}">${esc(p.name||`Group ${i+1}`)}</option>`).join('');
  if([...select.options].some(o=>o.value===current)) select.value=current;
}

function saveCurrentPreset(){
  const name=document.getElementById('preset-name').value.trim(),status=document.getElementById('preset-status');
  if(!name){status.textContent='Enter a name for this group.';status.style.color='var(--ink-error)';return;}
  if(!S.course||!S.golfers.length){status.textContent='Choose golfers here and a course type below, then save the group.';status.style.color='var(--ink-error)';return;}
  const preset={name,course_type:S.course,golfer_ids:[...S.golfers],course_ids:(EXPERIENCE.preferences.favorites||[]).filter(id=>findCourse(id)?.course_type===S.course),time_from:document.getElementById('search-time-from').value||null,time_to:document.getElementById('search-time-to').value||null};
  const presets=(EXPERIENCE.preferences.presets||[]).filter(p=>p.name.toLowerCase()!==name.toLowerCase());presets.push(preset);
  updatePreferences({presets},'Group saved.').then(ok=>{if(ok){document.getElementById('preset-name').value='';status.textContent='Group saved.';status.style.color='var(--ink-accent)';}else{status.textContent='Could not save this group. Try again.';status.style.color='var(--ink-error)';}});
}

function applySelectedPreset(){
  const index=document.getElementById('preset-select').value,p=EXPERIENCE.preferences.presets[Number(index)];if(index===''||!p)return;
  if(p.course_type) setCourse(p.course_type);
  S.golfers=(p.golfer_ids||[]).filter(id=>BUDDIES.some(b=>b.id===id));
  syncGuestLimit();
  document.querySelectorAll('#golferList .g-card').forEach(card=>{const selected=S.golfers.includes(card.dataset.id);card.classList.toggle('on',selected);card.setAttribute('aria-pressed',String(selected));});
  document.getElementById('search-time-from').value=p.time_from||'';document.getElementById('search-time-to').value=p.time_to||'';
  if(S.golfers.length) unlock('sec-course');clearResults();checkFetch();showAppView('book');
  document.getElementById('preset-status').textContent=`Applied ${p.name}. Check the course and time, then search.`;
}

function deleteSelectedPreset(){
  const select=document.getElementById('preset-select'),index=select.value;if(index==='')return;
  const presets=[...(EXPERIENCE.preferences.presets||[])];const removed=presets.splice(Number(index),1)[0];
  void updatePreferences({presets},`${removed?.name||'Group'} deleted.`).then(ok=>{if(!ok){const status=document.getElementById('preset-status');status.textContent='Could not delete this group. Try again.';status.style.color='var(--ink-error)';}});
}

async function refreshBuddyList(){
  const button=document.getElementById('refresh-buddies-btn'),status=document.getElementById('refresh-buddies-status');
  button.disabled=true;status.textContent='Refreshing golfer list…';status.style.color='var(--muted)';
  const data=await jsonRequest('/api/refresh-buddies',{method:'POST'}).catch(()=>({error:'Connection error.'}));
  button.disabled=false;
  if(data.ok){
    BUDDIES=data.buddies||[];buildGolfers();SR.golfers=[];buildRequestGolferList();
    status.textContent=`Updated ${BUDDIES.length} golfer${BUDDIES.length===1?'':'s'}.`;status.style.color='var(--ink-accent)';
  }else{status.textContent=data.error||'Could not refresh the golfer list.';status.style.color='var(--ink-error)';}
}

// Email settings live in Account, so point people there when none is saved.
function emailNudgeMarkup(message){
  return `<div class="email-nudge"><span>${esc(message)}</span><button type="button" class="text-action" onclick="openAccountEmail()">Add email</button></div>`;
}

function openAccountEmail(){
  if(typeof openAccountDialog==='function') openAccountDialog();
  setTimeout(()=>document.getElementById('emailAddr')?.focus(),60);
}

function renderEmailNudges(){
  const note=document.getElementById('rq-email-note'); if(!note) return;
  note.innerHTML=S.email
    ? `We'll email you after the nightly assignment (around 1 AM, 3 days before play) to tell you whether the request was assigned. A submitted request is not a confirmed tee time.`
    : `A submitted request is not a confirmed tee time.${emailNudgeMarkup('Add an email in Account to get the assignment result by email.')}`;
}

const baseUpdateEmailUI=updateEmailUI;
updateEmailUI=function nudgingUpdateEmailUI(){ baseUpdateEmailUI(); renderEmailNudges(); };
const baseSaveEmail=saveEmail;
saveEmail=async function nudgingSaveEmail(){ await baseSaveEmail(); renderEmailNudges(); };

document.addEventListener('change',event=>{
  const requestView=document.getElementById('view-request');
  if(EXPERIENCE.requestReviewSnapshot && requestView?.contains(event.target) && !document.getElementById('rq-review')?.contains(event.target)) clearRequestReview(false);
});

function trapDialogFocus(event,modal){
  const focusable=[...modal.querySelectorAll('button,a[href],input,select,[tabindex]:not([tabindex="-1"])')].filter(el=>!el.disabled&&!el.hidden);
  if(!focusable.length) return;
  const first=focusable[0],last=focusable.at(-1);
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
}

document.addEventListener('keydown',event=>{
  if(event.key==='Escape'){
    if(document.getElementById('origin-picker-overlay')?.classList.contains('show')) closeOriginPicker();
    else if(document.getElementById('course-overlay')?.classList.contains('show')) closeCourseModal();
    else if(document.getElementById('overlay')?.classList.contains('show')&&!BOOKING_IN_FLIGHT) closeConfirm();
    else if(document.getElementById('delete-overlay')?.classList.contains('show')) closeDeleteConfirm();
    else if(document.getElementById('rq-review')&&!EXPERIENCE.requestSubmissionInFlight) cancelRequestReview();
  }
  if(event.key==='Tab'){
    const origin=document.getElementById('origin-picker-overlay');
    if(origin?.classList.contains('show')) trapDialogFocus(event,origin.querySelector('.origin-picker-modal'));
    else if(document.getElementById('course-overlay')?.classList.contains('show')) trapDialogFocus(event,document.querySelector('#course-overlay .course-modal'));
  }
});
