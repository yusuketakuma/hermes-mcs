"""Order a day's planned visits with two external services, sending addresses only and only when asked.

Addresses go to the GSI address search (国土地理院) to become coordinates; coordinates go to
openrouteservice to order the visits by road. Names, IDs, notes and phone numbers are never sent. The feature
is off unless a private settings file names the openrouteservice key and the starting address.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
import stat
import time
import urllib.parse
import urllib.request

from mcs_util import NoRedirect, no_proxy_opener
from homecare_records import require_actor
import homecare_visits as visits

GEOCODE_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
OPTIMIZE_URL = "https://api.openrouteservice.org/optimization"
SERVICE_SECONDS = 20 * 60
WINDOW_SECONDS = 15 * 60   # a booked visit is reached within 15 minutes either side of its time
SAME_PLACE_METRES = 300    # several search results count as one place only when they lie this close
_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_STOPS = 40
_TIMEOUT = 15


class RouteSettings:
    """``{"openrouteservice_key", "origin_address", "depart_time"?}`` from a file only its owner can read."""

    def __init__(self, path):
        path = Path(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise SystemExit("route settings must be a regular file readable only by its owner (chmod 600)")
        data = json.loads(path.read_text(encoding="utf-8"))
        key, origin = data.get("openrouteservice_key"), data.get("origin_address")
        if not isinstance(key, str) or not 10 <= len(key) <= 200 or not isinstance(origin, str) \
                or not 1 <= len(origin.strip()) <= 200:
            raise SystemExit("route settings need openrouteservice_key and origin_address")
        depart = data.get("depart_time", "09:00")
        if not isinstance(depart, str) or not _CLOCK.match(depart):
            raise SystemExit("route settings depart_time must be HH:MM")
        self.key, self.origin, self.depart = key, origin.strip(), _seconds(depart)


def _seconds(clock):
    hours, minutes = clock.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def _clock(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def _fetch(request):
    """HTTPS to the two fixed hosts only: no proxy, no redirects."""
    host = urllib.parse.urlsplit(request.full_url).hostname
    if host not in ("msearch.gsi.go.jp", "api.openrouteservice.org"):
        raise ValueError("route_failed")
    with no_proxy_opener(NoRedirect()).open(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read(2_000_000))


def geocode(address, *, fetch=_fetch):
    """(lon, lat) when the search names one place inside Japan; None when it finds none or several apart."""
    query = urllib.parse.urlencode({"q": address})
    try:
        found = fetch(urllib.request.Request(f"{GEOCODE_URL}?{query}", headers={"Accept": "application/json"}))
        points = [tuple(feature["geometry"]["coordinates"]) for feature in found]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not points or not all(len(point) == 2 and all(isinstance(v, (int, float)) for v in point)
                             and 122 <= point[0] <= 154 and 20 <= point[1] <= 46 for point in points):
        return None
    (lon, lat), scale = points[0], math.cos(math.radians(points[0][1]))
    # A partial address ("中央区") matches several towns; guessing one would route to the wrong place.
    if any(math.hypot((x - lon) * 111_320 * scale, (y - lat) * 110_540) > SAME_PLACE_METRES for x, y in points):
        return None
    return float(lon), float(lat)


def _located(db, patient, *, fetch):
    address = (patient["address"] or "").strip()
    if not address:
        return None
    digest = hashlib.sha256(address.encode()).hexdigest()
    row = db.execute("SELECT lon, lat FROM patient_locations WHERE patient_id=? AND address_sha256=?",
                     (patient["patient_id"], digest)).fetchone()
    if row is not None:
        return row["lon"], row["lat"]
    point = geocode(address, fetch=fetch)
    if point is not None:
        db.execute("INSERT INTO patient_locations VALUES(?,?,?,?,?) ON CONFLICT(patient_id) DO UPDATE SET "
                   "address_sha256=excluded.address_sha256, lon=excluded.lon, lat=excluded.lat, "
                   "located_at=excluded.located_at", (patient["patient_id"], digest, *point, time.time()))
    return point


def plan(db, settings, visit_date, *, actor, staff_id=None, fetch=_fetch):
    """The day's planned visits in driving order from the starting address and back, with arrival estimates.

    A booked time is a window the route must meet. Visits without a usable address, and visits the route cannot
    reach in their window, are listed apart, never guessed into the route. Jobs carry positions, not ledger IDs.
    """
    if settings is None:
        raise ValueError("route_unavailable")
    require_actor(db, actor)
    if staff_id is not None and type(staff_id) is not int:
        raise ValueError("bad_request")
    day = [row for row in visits.for_day(db, visit_date)
           if row["status"] == "planned" and (staff_id is None or row["staff_id"] == staff_id)]
    if len(day) > MAX_STOPS:
        raise ValueError("too_many_stops")
    origin = geocode(settings.origin, fetch=fetch)
    if origin is None:
        raise ValueError("route_failed")
    jobs, unlocated, placed = [], [], []
    for row in day:
        point = _located(db, {"patient_id": row["patient_id"], "address": row["address"]}, fetch=fetch)
        if point is None:
            unlocated.append(row["visit_id"])
            continue
        placed.append(row)
        job = {"id": len(placed), "location": list(point), "service": SERVICE_SECONDS}
        if row["start_time"]:
            booked = _seconds(row["start_time"])
            job["time_windows"] = [[max(0, booked - WINDOW_SECONDS), booked + WINDOW_SECONDS]]
        jobs.append(job)
    stops, unscheduled = [], []
    if jobs:
        vehicle = {"id": 1, "profile": "driving-car", "start": list(origin), "end": list(origin),
                   "time_window": [settings.depart, 2 * 86400]}
        body = json.dumps({"jobs": jobs, "vehicles": [vehicle]}).encode()
        request = urllib.request.Request(OPTIMIZE_URL, data=body, method="POST", headers={
            "Authorization": settings.key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            result = fetch(request)
            routes = result["routes"]
            # A successful answer with no route means no visit could be placed; they all count as unscheduled.
            steps = routes[0]["steps"] if routes else []
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise ValueError("route_failed") from None
        for step in steps:
            position = step.get("id") if step.get("type") == "job" else None
            waiting = step.get("waiting_time", 0)
            if type(position) is not int or not 1 <= position <= len(placed) or type(step.get("arrival")) is not int \
                    or type(waiting) is not int or waiting < 0:
                continue
            row = placed[position - 1]
            # Arriving early means waiting for the booked window, so the shown time is when the visit can start.
            stops.append({"visit_id": row["visit_id"], "patient_name": row["patient_name"],
                          "start_time": row["start_time"], "arrival": _clock(step["arrival"] + waiting),
                          "travel_seconds": step.get("duration")})
        routed = {stop["visit_id"] for stop in stops}
        unscheduled = [row["visit_id"] for row in placed if row["visit_id"] not in routed]
    return {"date": visit_date, "stops": stops, "unlocated": unlocated, "unscheduled": unscheduled}
