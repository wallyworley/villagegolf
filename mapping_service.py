"""Course catalog and privacy-conscious driving route previews.

The route service is deliberately small: destinations come only from the
checked-in catalog and origins are used for one upstream request and are never
logged or persisted.
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CATALOG_PATH = Path(__file__).resolve().parent / "data" / "course_locations.json"
ROUTE_SOLVE_URL = (
    "https://arc7.thevillages.com/arcgis/rest/services/"
    "GOLFCROUTEZ/NAServer/Route/solve"
)
NETWORK_UPDATED = "2025-12-15"
ATTRIBUTION = (
    "Directions provided by The Villages ArcGIS GOLFCROUTEZ network; "
    "network revision 2025-12-15. ETA is the provider's estimate, not live traffic."
)
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 128
_MAX_RESPONSE_BYTES = 2_000_000
_REQUEST_TIMEOUT_SECONDS = 10.0
_MAX_SNAP_MILES = 0.25
_VILLAGES_BOUNDS = {"min_lat": 28.65, "max_lat": 29.10, "min_lon": -82.20, "max_lon": -81.80}
# Origins must be inside The Villages (_VILLAGES_BOUNDS), but a landscape
# image of the whole community needs surrounding land east and west, so map
# bounds are checked against this wider window instead, which still keeps the
# proxy from fetching arbitrary world imagery. The map service's own full
# extent is longitude -82.326 to -81.625 and latitude 28.445 to 29.185;
# imagery outside it renders blank, which is acceptable at the margins.
# Worst case measured on September 9, 2026: a route from any corner of the origin
# guard to any catalog course, padded 15 percent and fitted to the 1.8 route-map
# aspect, reaches longitude -82.58 to -81.42 and latitude 28.60 to 29.15.
_MAP_REGION_BOUNDS = {"min_lat": 28.50, "max_lat": 29.30, "min_lon": -82.70, "max_lon": -81.30}

MAP_EXPORT_URL = "https://arc7.thevillages.com/arcgis/rest/services/PUBLICMAP26/MapServer/export"
_MAP_CACHE_TTL_SECONDS = 6 * 3600.0
_MAP_CACHE_MAX_ENTRIES = 32
_MAP_MAX_RESPONSE_BYTES = 4_000_000
_MAP_MIN_PIXELS = 200
_MAP_MAX_PIXELS = 1600
_MAP_MIN_SPAN_DEGREES = 0.002
_MAP_ASPECT_TOLERANCE = 0.01
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _RoutePayloadError(ValueError):
    """Raised when the upstream schema cannot safely be used."""


class CatalogUnavailable(RuntimeError):
    """Raised when the course catalog file cannot be loaded (missing,
    unreadable, or malformed). Callers should treat this as a temporary,
    retryable condition and report it distinctly from a routing failure."""


def _load_catalog() -> list[dict[str, Any]]:
    with CATALOG_PATH.open(encoding="utf-8") as stream:
        rows = json.load(stream)
    if not isinstance(rows, list):
        raise ValueError("course catalog must be a list")
    return rows


_catalog_lock = threading.Lock()
_CATALOG_CACHE: tuple[list[dict[str, Any]], dict[str, dict[str, Any]]] | None = None
_ROUTE_CACHE: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()
_MAP_CACHE: OrderedDict[tuple[Any, ...], tuple[float, bytes]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _catalog() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return the cached ``(courses, by_id)`` tuple, loading on first use.

    Loading happens once, under a lock, so concurrent requests never race to
    parse the file twice. A load failure caches nothing, so the very next
    call retries instead of the process staying wedged until restart.
    """

    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    with _catalog_lock:
        if _CATALOG_CACHE is not None:
            return _CATALOG_CACHE
        try:
            courses = _load_catalog()
            by_id = {str(course["id"]): course for course in courses}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CatalogUnavailable(
                f"course catalog could not be loaded: {exc.__class__.__name__}"
            ) from exc
        _CATALOG_CACHE = (courses, by_id)
        return _CATALOG_CACHE


def _reset_catalog_cache() -> None:
    """Test hook: forget the cached catalog so the next call reloads it."""

    global _CATALOG_CACHE
    with _catalog_lock:
        _CATALOG_CACHE = None


def get_courses() -> list[dict[str, Any]]:
    """Return the complete catalog as detached dictionaries."""

    courses, _by_id = _catalog()
    return copy.deepcopy(courses)


def get_course(course_id: str) -> dict[str, Any] | None:
    """Return one catalog course, or ``None`` for an unknown id."""

    if not isinstance(course_id, str):
        return None
    _courses, by_id = _catalog()
    course = by_id.get(course_id.strip())
    return copy.deepcopy(course) if course else None


def distance_miles(origin: Mapping[str, Any], destination: Mapping[str, Any]) -> float:
    """Return the great-circle distance between two latitude/longitude points."""

    lat1, lon1 = float(origin["latitude"]), float(origin["longitude"])
    lat2, lon2 = float(destination["latitude"]), float(destination["longitude"])
    radius = 3958.7613
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.asin(math.sqrt(min(1.0, a)))


def _valid_coordinate(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_origin(origin: Any) -> tuple[float, float] | None:
    if not isinstance(origin, Mapping):
        return None
    latitude, longitude = origin.get("latitude"), origin.get("longitude")
    if not (_valid_coordinate(latitude) and _valid_coordinate(longitude)):
        return None
    bounds = _VILLAGES_BOUNDS
    if not (
        bounds["min_lat"] <= latitude <= bounds["max_lat"]
        and bounds["min_lon"] <= longitude <= bounds["max_lon"]
    ):
        return None
    return float(latitude), float(longitude)


def _read_upstream(request: Request) -> dict[str, Any]:
    with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise ValueError("routing response exceeded the size limit")
    result = json.loads(payload.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("routing response was not an object")
    return result


def _route_geometry(payload: Mapping[str, Any]) -> list[list[float]]:
    if not isinstance(payload, Mapping):
        raise _RoutePayloadError("routing service returned an invalid route object")
    routes = payload.get("routes")
    features = routes.get("features") if isinstance(routes, Mapping) else None
    if not isinstance(features, list) or len(features) != 1 or not isinstance(features[0], Mapping):
        raise _RoutePayloadError("routing service returned an invalid route feature")
    geometry = features[0].get("geometry")
    paths = geometry.get("paths") if isinstance(geometry, Mapping) else None
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], list):
        raise _RoutePayloadError("routing service returned disconnected route geometry")
    result = []
    for point in paths[0]:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise _RoutePayloadError("routing service returned malformed route coordinates")
        longitude, latitude = point[0], point[1]
        if not (_valid_coordinate(longitude) and _valid_coordinate(latitude)):
            raise _RoutePayloadError("routing service returned non-finite route coordinates")
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise _RoutePayloadError("routing service returned out-of-range route coordinates")
        result.append([float(longitude), float(latitude)])
    if len(result) < 2:
        raise _RoutePayloadError("routing service returned too few route coordinates")
    return result


def _route_stops(payload: Mapping[str, Any], requested: tuple[tuple[float, float], tuple[float, float]]) -> None:
    """Require two located, on-network stops.

    ``requested`` keeps its two-point shape only so every caller and test
    shares one signature; the actual snap distance is measured against the
    route polyline endpoints in ``_route_snap_check``, since the stop
    geometry ArcGIS returns simply echoes the requested coordinates back.
    """

    del requested  # kept for signature compatibility only; see docstring
    stops = payload.get("stops") if isinstance(payload, Mapping) else None
    features = stops.get("features") if isinstance(stops, Mapping) else None
    if not isinstance(features, list) or len(features) != 2:
        raise _RoutePayloadError("routing service returned no usable stops")

    sequenced = []
    for row in features:
        if not isinstance(row, Mapping):
            raise _RoutePayloadError("routing service returned malformed stops")
        attrs = row.get("attributes")
        if not isinstance(attrs, Mapping):
            raise _RoutePayloadError("routing service returned malformed stops")
        sequence = attrs.get("Sequence")
        valid_sequence = isinstance(sequence, int) and not isinstance(sequence, bool)
        valid_sequence = valid_sequence or (isinstance(sequence, str) and sequence.isdigit())
        if not valid_sequence:
            raise _RoutePayloadError("routing service returned malformed stops")
        sequenced.append((int(sequence), row, attrs))

    for _, row, attrs in sorted(sequenced, key=lambda item: item[0]):
        status = attrs.get("Status")
        if status not in (0, "0"):
            raise _RoutePayloadError("routing service could not locate a route stop")
        geometry = row.get("geometry")
        if not isinstance(geometry, Mapping):
            raise _RoutePayloadError("routing service returned malformed stop coordinates")
        longitude, latitude = geometry.get("x"), geometry.get("y")
        if not (_valid_coordinate(longitude) and _valid_coordinate(latitude)):
            raise _RoutePayloadError("routing service returned non-finite stop coordinates")
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise _RoutePayloadError("routing service returned out-of-range stop coordinates")


def _route_snap_check(
    requested: tuple[tuple[float, float], tuple[float, float]],
    geometry: list[list[float]],
) -> None:
    """Reject a route whose on-network snap lands too far from a requested point.

    ``requested`` is ``((lat, lon), (lat, lon))`` for the origin and the
    course. ``geometry`` is the ``[lon, lat]`` polyline from
    ``_route_geometry``; its first vertex is the on-network point for the
    origin and its last vertex is the on-network point for the course.
    """

    origin_lat, origin_lon = requested[0]
    course_lat, course_lon = requested[1]
    origin_snap = {"latitude": geometry[0][1], "longitude": geometry[0][0]}
    course_snap = {"latitude": geometry[-1][1], "longitude": geometry[-1][0]}
    if distance_miles({"latitude": origin_lat, "longitude": origin_lon}, origin_snap) > _MAX_SNAP_MILES:
        raise _RoutePayloadError(
            f"routing service could not reach the network within {_MAX_SNAP_MILES} miles of the origin"
        )
    if distance_miles({"latitude": course_lat, "longitude": course_lon}, course_snap) > _MAX_SNAP_MILES:
        raise _RoutePayloadError(
            f"routing service could not reach the network within {_MAX_SNAP_MILES} miles of the course"
        )


def _route_summary(payload: Mapping[str, Any]) -> tuple[float | None, float | None]:
    features = ((payload.get("routes") or {}).get("features") or [])
    if features and not isinstance(features[0], Mapping):
        raise _RoutePayloadError("routing service returned an invalid route summary")
    attrs = features[0].get("attributes") if features else {}
    attrs = attrs or {}
    directions = payload.get("directions") or []
    summary = directions[0].get("summary") if directions and isinstance(directions[0], Mapping) else {}
    summary = summary or {}
    miles = attrs.get("Total_Length", summary.get("totalLength"))
    minutes = summary.get("totalTime", summary.get("totalDriveTime"))
    if not (_valid_coordinate(miles) and _valid_coordinate(minutes) and miles >= 0 and minutes >= 0):
        raise _RoutePayloadError("routing service returned an invalid route summary")
    return float(miles), float(minutes)


def _route_directions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    directions = payload.get("directions") or []
    if not isinstance(directions, list) or len(directions) != 1 or not isinstance(directions[0], Mapping):
        raise _RoutePayloadError("routing service returned malformed directions")
    rows = directions[0].get("features")
    if not isinstance(rows, list) or not rows:
        raise _RoutePayloadError("routing service returned no directions")
    result = []
    for row in rows or []:
        attrs = row.get("attributes") if isinstance(row, Mapping) else None
        if not isinstance(attrs, Mapping):
            raise _RoutePayloadError("routing service returned malformed directions")
        text = attrs.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _RoutePayloadError("routing service returned malformed direction text")
        length, minutes = attrs.get("length", 0), attrs.get("time", 0)
        if not (_valid_coordinate(length) and _valid_coordinate(minutes) and length >= 0 and minutes >= 0):
            raise _RoutePayloadError("routing service returned invalid direction metrics")
        result.append({
            "text": text.strip(),
            "miles": round(float(length), 2),
            "minutes": round(float(minutes), 1),
        })
    return result


def _cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _ROUTE_CACHE.get(key)
        if not item:
            return None
        created, result = item
        if now - created >= _CACHE_TTL_SECONDS:
            _ROUTE_CACHE.pop(key, None)
            return None
        _ROUTE_CACHE.move_to_end(key)
        return copy.deepcopy(result)


def _cache_put(key: tuple[Any, ...], result: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _ROUTE_CACHE[key] = (time.monotonic(), copy.deepcopy(result))
        _ROUTE_CACHE.move_to_end(key)
        while len(_ROUTE_CACHE) > _CACHE_MAX_ENTRIES:
            _ROUTE_CACHE.popitem(last=False)


def route_to_course(origin: Mapping[str, Any], course_id: str) -> dict[str, Any]:
    """Calculate a route from an origin to a catalog course.

    The returned geometry uses GeoJSON order (``[longitude, latitude]``).
    """

    coordinates = _validate_origin(origin)
    if coordinates is None:
        return {"success": False, "error": "origin coordinates must be finite and within The Villages region"}
    course = get_course(course_id)
    if course is None:
        return {"success": False, "error": "unknown course"}
    destination = (course.get("latitude"), course.get("longitude"))
    if not (_valid_coordinate(destination[0]) and _valid_coordinate(destination[1])):
        return {"success": False, "error": "course has no usable location"}
    destination = (float(destination[0]), float(destination[1]))
    key = (round(coordinates[0], 6), round(coordinates[1], 6), course["id"])
    cached = _cache_get(key)
    if cached is not None:
        return cached
    query = {
        "f": "json",
        "stops": f"{coordinates[1]},{coordinates[0]};{destination[1]},{destination[0]}",
        "outSR": "4326",
        "returnDirections": "true",
        "returnRoutes": "true",
        "returnStops": "true",
        "directionsLengthUnits": "esriNAUMiles",
        "directionsTimeAttributeName": "Drivetime",
        "ignoreInvalidLocations": "false",
    }
    try:
        payload = _read_upstream(Request(ROUTE_SOLVE_URL + "?" + urlencode(query), headers={"Accept": "application/json"}))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {"success": False, "error": f"routing service unavailable: {exc.__class__.__name__}"}
    # _read_upstream already rejects non-dict JSON, so this is defense in
    # depth against a caller (e.g. a test) that patches _read_upstream itself.
    if not isinstance(payload, Mapping):
        return {"success": False, "error": "routing service returned an unexpected response"}
    if payload.get("error"):
        details = payload["error"].get("message") if isinstance(payload["error"], Mapping) else None
        return {"success": False, "error": details or "routing service returned an error"}
    try:
        _route_stops(payload, (coordinates, destination))
        geometry = _route_geometry(payload)
        _route_snap_check((coordinates, destination), geometry)
        miles, minutes = _route_summary(payload)
        directions = _route_directions(payload)
    except _RoutePayloadError as exc:
        return {"success": False, "error": str(exc)}
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return {"success": False, "error": "routing service returned an unexpected response"}
    result = {
        "success": True,
        "geometry": {"type": "LineString", "coordinates": geometry},
        "miles": round(miles, 2) if miles is not None else None,
        "minutes": round(minutes, 1) if minutes is not None else None,
        "directions": directions,
        "source_url": ROUTE_SOLVE_URL,
        "network_updated": NETWORK_UPDATED,
        "attribution": ATTRIBUTION,
        "calculated_at": datetime.now(timezone.utc).isoformat(),
        "course_id": course["id"],
    }
    _cache_put(key, result)
    return copy.deepcopy(result)


# ── Basemap imagery ──────────────────────────────────────────────────────────
# A server-side proxy for The Villages' public ArcGIS map export service, so
# the client never talks to a third-party host directly and every request is
# bounded, cached, and validated the same way the routing calls above are.


def mercator_y(latitude: float) -> float:
    """Web Mercator y coordinate (unscaled) for a latitude in degrees."""

    return math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2))


def map_aspect(bounds: Mapping[str, float]) -> float:
    """Width-to-height ratio a Mercator-projected image of ``bounds`` would have."""

    lon_span = bounds["max_lon"] - bounds["min_lon"]
    lat_span = bounds["max_lat"] - bounds["min_lat"]
    if lon_span <= 0 or lat_span <= 0:
        raise ValueError("map bounds must have positive spans")
    return (math.radians(lon_span)) / (
        mercator_y(bounds["max_lat"]) - mercator_y(bounds["min_lat"])
    )


def fit_map_bounds(bounds: Mapping[str, float], width: int, height: int) -> dict[str, float]:
    """Expand ``bounds`` symmetrically about its center until its aspect matches ``width / height``.

    The result always contains the input bounds; only one axis (longitude or
    latitude, whichever the target aspect needs) is ever expanded, so this
    never shrinks what the caller asked to see. Latitude expansion happens in
    Mercator space and is converted back to degrees with the inverse
    projection, so the result stays consistent with ``map_aspect``.
    """

    target = width / height
    min_lon, max_lon = bounds["min_lon"], bounds["max_lon"]
    min_lat, max_lat = bounds["min_lat"], bounds["max_lat"]
    current = map_aspect(bounds)
    if current < target:
        # Not enough longitude span for this target aspect: widen longitude,
        # holding the latitude (Mercator) span fixed.
        merc_span = mercator_y(max_lat) - mercator_y(min_lat)
        needed_lon_span = target * merc_span
        center_lon = (min_lon + max_lon) / 2
        half_span_degrees = math.degrees(needed_lon_span) / 2
        min_lon = center_lon - half_span_degrees
        max_lon = center_lon + half_span_degrees
    elif current > target:
        # Too much longitude span for this target aspect: widen the latitude
        # span instead, in Mercator space, then convert back to degrees.
        lon_span_rad = math.radians(max_lon - min_lon)
        needed_merc_span = lon_span_rad / target
        y_min, y_max = mercator_y(min_lat), mercator_y(max_lat)
        center_y = (y_min + y_max) / 2
        half_merc = needed_merc_span / 2
        min_lat = math.degrees(2 * math.atan(math.exp(center_y - half_merc)) - math.pi / 2)
        max_lat = math.degrees(2 * math.atan(math.exp(center_y + half_merc)) - math.pi / 2)
    return {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}


def catalog_map_bounds() -> dict[str, float]:
    """Bounding box of every catalog course with finite coordinates.

    Padded per axis by ``max(span * 0.12, 0.012)``, the same rule the client's
    ``mapBoundsFor`` uses, so a server-side test can prove the two agree on
    what a fitted request to ``map_image()`` will accept.
    """

    courses, _by_id = _catalog()
    points = [
        (course.get("longitude"), course.get("latitude"))
        for course in courses
        if _valid_coordinate(course.get("longitude")) and _valid_coordinate(course.get("latitude"))
    ]
    lons = [float(p[0]) for p in points]
    lats = [float(p[1]) for p in points]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    lon_pad = max((max_lon - min_lon) * 0.12, 0.012)
    lat_pad = max((max_lat - min_lat) * 0.12, 0.012)
    return {
        "min_lon": min_lon - lon_pad, "max_lon": max_lon + lon_pad,
        "min_lat": min_lat - lat_pad, "max_lat": max_lat + lat_pad,
    }


def _validate_map_request(
    bounds: Any, width: Any, height: Any
) -> tuple[dict[str, float] | None, str | None]:
    """Validate a map-image request. Returns ``(clean_bounds, None)`` or ``(None, message)``."""

    if not isinstance(width, int) or isinstance(width, bool) or not (_MAP_MIN_PIXELS <= width <= _MAP_MAX_PIXELS):
        return None, f"width must be an integer between {_MAP_MIN_PIXELS} and {_MAP_MAX_PIXELS}"
    if not isinstance(height, int) or isinstance(height, bool) or not (_MAP_MIN_PIXELS <= height <= _MAP_MAX_PIXELS):
        return None, f"height must be an integer between {_MAP_MIN_PIXELS} and {_MAP_MAX_PIXELS}"

    if not isinstance(bounds, Mapping):
        return None, "map bounds are required"
    try:
        min_lon, min_lat = bounds["min_lon"], bounds["min_lat"]
        max_lon, max_lat = bounds["max_lon"], bounds["max_lat"]
    except KeyError:
        return None, "map bounds are required"

    if not all(_valid_coordinate(value) for value in (min_lon, min_lat, max_lon, max_lat)):
        return None, "map bounds must be finite numbers"
    if not (min_lon < max_lon and min_lat < max_lat):
        return None, "map bounds are inverted"
    if (max_lon - min_lon) < _MAP_MIN_SPAN_DEGREES or (max_lat - min_lat) < _MAP_MIN_SPAN_DEGREES:
        return None, "map bounds span is too small"

    region = _MAP_REGION_BOUNDS
    if not (
        region["min_lat"] <= min_lat and max_lat <= region["max_lat"]
        and region["min_lon"] <= min_lon and max_lon <= region["max_lon"]
    ):
        return None, "map bounds must be within the map region"

    clean_bounds = {
        "min_lon": float(min_lon), "min_lat": float(min_lat),
        "max_lon": float(max_lon), "max_lat": float(max_lat),
    }
    target = width / height
    if abs(map_aspect(clean_bounds) - target) > _MAP_ASPECT_TOLERANCE * target:
        return None, "map bounds do not match the image size"
    return clean_bounds, None


def _read_upstream_bytes(request: Request) -> bytes:
    with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        payload = response.read(_MAP_MAX_RESPONSE_BYTES + 1)
    if len(payload) > _MAP_MAX_RESPONSE_BYTES:
        raise ValueError("map response exceeded the size limit")
    return payload


def _map_cache_get(key: tuple[Any, ...]) -> bytes | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _MAP_CACHE.get(key)
        if not item:
            return None
        created, payload = item
        if now - created >= _MAP_CACHE_TTL_SECONDS:
            _MAP_CACHE.pop(key, None)
            return None
        _MAP_CACHE.move_to_end(key)
        return payload


def _map_cache_put(key: tuple[Any, ...], payload: bytes) -> None:
    with _CACHE_LOCK:
        _MAP_CACHE[key] = (time.monotonic(), payload)
        _MAP_CACHE.move_to_end(key)
        while len(_MAP_CACHE) > _MAP_CACHE_MAX_ENTRIES:
            _MAP_CACHE.popitem(last=False)


def map_image(bounds: Any, width: Any, height: Any) -> tuple[bytes | None, str | None, int]:
    """Return a proxied PNG basemap export for ``bounds``.

    Returns ``(png_bytes, None, 200)`` on success, ``(None, message, 400)`` for
    a validation failure, or ``(None, message, 502)`` for an upstream failure.
    """

    clean_bounds, error = _validate_map_request(bounds, width, height)
    if error is not None:
        return None, error, 400

    key = (
        round(clean_bounds["min_lon"], 5), round(clean_bounds["min_lat"], 5),
        round(clean_bounds["max_lon"], 5), round(clean_bounds["max_lat"], 5),
        width, height,
    )
    cached = _map_cache_get(key)
    if cached is not None:
        return cached, None, 200

    query = {
        "bbox": f"{clean_bounds['min_lon']},{clean_bounds['min_lat']},"
                f"{clean_bounds['max_lon']},{clean_bounds['max_lat']}",
        "bboxSR": "4326",
        "imageSR": "3857",
        "size": f"{width},{height}",
        "format": "png",
        "transparent": "false",
        "dpi": "96",
        "f": "image",
    }
    try:
        payload = _read_upstream_bytes(
            Request(MAP_EXPORT_URL + "?" + urlencode(query), headers={"Accept": "image/png"})
        )
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"map service unavailable: {exc.__class__.__name__}", 502

    if not payload.startswith(_PNG_SIGNATURE):
        return None, "map service returned an unexpected response", 502

    _map_cache_put(key, payload)
    return payload, None, 200


__all__ = [
    "ATTRIBUTION", "CatalogUnavailable", "MAP_EXPORT_URL", "catalog_map_bounds", "distance_miles",
    "fit_map_bounds", "get_course", "get_courses", "map_aspect", "map_image", "mercator_y",
    "route_to_course",
]
