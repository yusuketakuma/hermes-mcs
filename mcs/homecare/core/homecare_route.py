"""Order a day's planned visits with two external services, sending addresses only and only when asked.

Addresses go to the GSI address search (国土地理院) to become coordinates; coordinates go to
openrouteservice to order the visits by road. Names, IDs, notes and phone numbers are never sent. The feature
is off unless a private settings file names the openrouteservice key and the starting address.
"""
from __future__ import annotations

import hashlib
import json
import os
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
MAX_STOPS = 40
_TIMEOUT = 15


class RouteSettings:
    """``{"openrouteservice_key": ..., "origin_address": ...}`` from a file only its owner can read."""

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
        self.key, self.origin = key, origin.strip()


def _fetch(request):
    """HTTPS to the two fixed hosts only: no proxy, no redirects."""
    host = urllib.parse.urlsplit(request.full_url).hostname
    if host not in ("msearch.gsi.go.jp", "api.openrouteservice.org"):
        raise ValueError("route_failed")
    with no_proxy_opener(NoRedirect()).open(request, timeout=_TIMEOUT) as response:
        return json.loads(response.read(2_000_000))


def geocode(address, *, fetch=_fetch):
    """(lon, lat) of the first match inside Japan, or None."""
    query = urllib.parse.urlencode({"q": address})
    try:
        found = fetch(urllib.request.Request(f"{GEOCODE_URL}?{query}", headers={"Accept": "application/json"}))
        lon, lat = found[0]["geometry"]["coordinates"]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    if not (isinstance(lon, (int, float)) and isinstance(lat, (int, float)) and 122 <= lon <= 154 and 20 <= lat <= 46):
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

    Visits without a usable address are listed apart, never guessed into the route.
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
    jobs, unlocated = [], []
    for row in day:
        point = _located(db, {"patient_id": row["patient_id"], "address": row["address"]}, fetch=fetch)
        if point is None:
            unlocated.append(row["visit_id"])
        else:
            jobs.append({"id": row["visit_id"], "location": list(point), "service": SERVICE_SECONDS})
    stops = []
    if jobs:
        body = json.dumps({"jobs": jobs, "vehicles": [{"id": 1, "profile": "driving-car",
                                                       "start": list(origin), "end": list(origin)}]}).encode()
        request = urllib.request.Request(OPTIMIZE_URL, data=body, method="POST", headers={
            "Authorization": settings.key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            result = fetch(request)
            steps = result["routes"][0]["steps"]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise ValueError("route_failed") from None
        by_id = {row["visit_id"]: row for row in day}
        stops = [{"visit_id": step["id"], "patient_name": by_id[step["id"]]["patient_name"],
                  "start_time": by_id[step["id"]]["start_time"], "arrival_offset": step.get("arrival"),
                  "travel_seconds": step.get("duration")}
                 for step in steps if step.get("type") == "job" and step.get("id") in by_id]
        unlocated.extend(job["id"] for job in jobs if job["id"] not in {stop["visit_id"] for stop in stops})
    return {"date": visit_date, "stops": stops, "unlocated": unlocated}
