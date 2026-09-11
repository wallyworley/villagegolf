"""Validated, per-account website preferences (stored in the encrypted profile)."""

import copy
import math
import re

DEFAULTS = {
    "favorites": [], "home": None, "arrival_buffer_minutes": 20,
    "presets": [], "max_cart_minutes": None,
}


def get_preferences(user):
    preferences = copy.deepcopy(DEFAULTS)
    saved = user.get("preferences", {})
    if isinstance(saved, dict):
        preferences.update({key: saved[key] for key in DEFAULTS if key in saved})
    return preferences


def validate_origin(origin):
    if not isinstance(origin, dict):
        raise ValueError("Choose a starting location.")
    try:
        if isinstance(origin.get("latitude"), bool) or isinstance(origin.get("longitude"), bool):
            raise ValueError
        lat, lon = float(origin["latitude"]), float(origin["longitude"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Choose a valid starting location.") from None
    if not (math.isfinite(lat) and math.isfinite(lon) and 28.4 <= lat <= 29.3 and -82.4 <= lon <= -81.6):
        raise ValueError("Choose a starting location in The Villages area.")
    return {"latitude": lat, "longitude": lon}


def _ids(value, allowed, limit, label):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"Invalid {label} list.")
    if any(not isinstance(x, str) or x not in allowed for x in value):
        raise ValueError(f"An item in {label} is no longer available. Refresh and try again.")
    return list(dict.fromkeys(value))


def _time(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Times must use HH:MM.")
    return value


def validate_preferences(data, user, course_ids):
    if not isinstance(data, dict) or any(k not in DEFAULTS for k in data):
        raise ValueError("Invalid preference fields.")
    result = {}
    if "favorites" in data:
        result["favorites"] = _ids(data["favorites"], course_ids, 150, "favorites")
    if "home" in data:
        home = data["home"]
        result["home"] = None if home is None else validate_origin(home)
        if home is not None:
            label = home.get("label", "Home")
            if not isinstance(label, str) or len(label) > 120:
                raise ValueError("Home label must be at most 120 characters.")
            result["home"]["label"] = label.strip() or "Home"
    for key, low, high in [("arrival_buffer_minutes", 0, 120), ("max_cart_minutes", 1, 180)]:
        if key not in data:
            continue
        value = data[key]
        if key == "max_cart_minutes" and value is None:
            result[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must be {low}–{high}.")
        result[key] = value
    if "presets" in data:
        if not isinstance(data["presets"], list) or len(data["presets"]) > 12:
            raise ValueError("Save up to 12 presets.")
        available_golfers = {str(b["id"]) for b in user.get("buddies", [])}
        result["presets"] = []
        names = set()
        for item in data["presets"]:
            if not isinstance(item, dict):
                raise ValueError("Invalid preset.")
            name = item.get("name", "")
            if not isinstance(name, str) or not name.strip() or len(name) > 60:
                raise ValueError("Give each preset a name of at most 60 characters.")
            name = name.strip()
            if name.casefold() in names:
                raise ValueError("Use a different name for each preset.")
            names.add(name.casefold())
            course_type = item.get("course_type")
            if course_type not in ("Executive", "Championship"):
                raise ValueError("Choose an Executive or Championship preset.")
            start, end = _time(item.get("time_from")), _time(item.get("time_to"))
            if start and end and start > end:
                raise ValueError("Earliest time must precede latest time.")
            result["presets"].append({
                "name": name, "course_type": course_type,
                "golfer_ids": _ids(item.get("golfer_ids", []), available_golfers, 4, "golfers"),
                "course_ids": _ids(item.get("course_ids", []), course_ids, len(course_ids), "courses"),
                "time_from": start, "time_to": end,
            })
    return result
