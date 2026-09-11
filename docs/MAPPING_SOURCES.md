# Mapping sources

The course catalog in `data/course_locations.json` is refreshed from The
Villages’ public WordPress REST golf collection:

<https://www.thevillages.com/wp-json/wp/v2/golf?per_page=100>

Each course record uses the page’s published ACF `address`, `latitude`,
`longitude`, `phone`, `holes`, `par`, `level`, and `village` values. The linked
course page is retained in `course_url`; the catalog intentionally leaves
`scorecard_url` nullable until a stable public PDF URL is available. The
coordinates identify the official course page’s mapped address and are marked
`location_accuracy: "address"`; they are not asserted to be a starter desk or
cart entrance.

`region` is a display grouping derived consistently from the published
latitude: North is approximately 28.91°N and above, Central is approximately
28.82°N through 28.91°N, and South is below approximately 28.82°N. These broad
bands describe map browsing areas rather than municipal boundaries; `area`
retains the course page’s named village or Eastport label for local context.

Driving previews use The Villages’ public ArcGIS Network Analyst service:

<https://arc7.thevillages.com/arcgis/rest/services/GOLFCROUTEZ/NAServer/Route/solve>

The request uses `stops=longitude,latitude;longitude,latitude`, `outSR=4326`,
`returnDirections=true`, `returnRoutes=true`, `returnStops=true`,
`directionsLengthUnits=esriNAUMiles`,
`directionsTimeAttributeName=Drivetime`, and
`ignoreInvalidLocations=false`. The network service metadata currently reports
revision `12152025`, represented by the API as `network_updated: "2025-12-15"`.
The returned ETA is the provider’s estimate and is not live traffic.

The map client also links to The Villages’ public `PUBLICMAP26` basemap for
future shared-use-path work:

<https://arc7.thevillages.com/arcgis/rest/services/PUBLICMAP26/MapServer>

Some basemap layers are polygons or have partial coverage, so they are not used
as a substitute for course-page coordinates. `mapping_service.py` validates
origins against a broad Villages-area bounding box, accepts destinations only by
catalog id, bounds its in-memory route cache, and does not log or persist
origins.

## Basemap imagery

Course Finder and route previews draw real map imagery from The Villages'
public ArcGIS export endpoint:

<https://arc7.thevillages.com/arcgis/rest/services/PUBLICMAP26/MapServer/export>

`GET /api/map-image` proxies this endpoint server-side, so the browser never
contacts it directly. The proxied request uses `bbox=min_lon,min_lat,max_lon,max_lat`,
`bboxSR=4326`, `imageSR=3857`, `size=width,height`, `format=png`,
`transparent=false`, `dpi=96`, and `f=image`. The client and server compute the
same Mercator-fitted bounds for a given container size before making the
request, so the requested bbox always matches the requested image aspect and
the pins drawn over the image line up with it.

Successful exports are cached in memory for six hours (`_MAP_CACHE_TTL_SECONDS`)
in a bounded LRU (`_MAP_CACHE_MAX_ENTRIES`), keyed on the rounded bounds and
pixel size. Requested width and height are capped at 1600 pixels
(`_MAP_MAX_PIXELS`) in either dimension. When the export request fails, times
out, or returns something other than a PNG, `map_image()` returns an error
instead of caching it, and the frontend falls back to a labeled pin plot: the
basemap image is hidden and a "Map imagery unavailable. Pins show relative
positions." note is shown, so the map is still usable with just the pins.

## Tiles

The Courses tab and the course modal's route preview render a Leaflet tile
layer directly against The Villages' public PUBLICMAP26 tile cache:

<https://arc7.thevillages.com/arcgis/rest/services/PUBLICMAP26/MapServer/tile/{z}/{y}/{x}>

The browser fetches these tiles directly: they are plain cached PNG images,
so no CORS handling is involved. The service uses WKID 4326 (plate carree,
degrees) with a 256 pixel tile size, an origin of x=-400, y=400, and a
resolution of 0.0012182840349851034 degrees per pixel at zoom 0 that halves
at each of the 11 zoom levels (0 through 10). `static/js/experience.js`
defines a Leaflet CRS (`VillagesCRS`) that reproduces this scheme exactly, so
Leaflet's own tile coordinates match the service's row and column at every
integer zoom.

This tile layer replaces the `/api/map-image` proxy for the page: the
frontend no longer requests that endpoint. `GET /api/map-image` and its
Python tests stay in place, unused by the page.
