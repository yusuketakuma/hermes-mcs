"""Who "we" are in MCS: the logged-in user and own-station roster.

Resolves self identity from the fetched self_profile_v1 and
station_staff_v1 artifacts (plus config signals.* overrides). Used from
every layer — ledger, ingest, notify, views and the signal engine — so it
lives in core and callers import this owner directly.
"""
import json
import re
import time

from mcs_queries import latest_artifact as _latest_artifact


SELF_PROFILE_KIND = "self_profile_v1"


def normalize_sender_id(value) -> int | None:
    """A positive signed-64-bit sender ID, or None for unknown values."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value.strip()):
        value = int(value)
    return value if type(value) is int and 0 < value < 2**63 else None


def latest_self_profile(db):
    """Most recent self_profile artifact (written from the MCS
    /users/self response by record_self_profile) — the fetched default
    for self identity. Returns {} when absent or unparsable."""
    return _latest_artifact(db, SELF_PROFILE_KIND)[1] or {}


def record_self_profile(db, prof) -> bool:
    """Persist a fetched MCS self profile as an append-only artifact —
    skipped when byte-identical to the latest row so repeated ticks
    don't spam the log. Caller holds the transaction. Returns True
    when a row was written."""
    sid = normalize_sender_id(prof.get("sender_id"))
    if sid is None:
        sid = _station_self_id(latest_station_staff(db))
    doc = {"sender_id": sid, "name": prof.get("name"),
           "professions": [x for x in (prof.get("professions") or [])
                           if isinstance(x, str) and x],
           "organizations": [x for x in (prof.get("organizations") or [])
                             if isinstance(x, str) and x],
           "fetched_at": int(time.time())}
    base = {k: doc[k] for k in
            ("sender_id", "name", "professions", "organizations")}
    cur = latest_self_profile(db)
    if cur and all(cur.get(k) == v for k, v in base.items()):
        return False
    db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at)"
        " VALUES(?,NULL,?,?,?)",
        (SELF_PROFILE_KIND, json.dumps(doc, ensure_ascii=False),
         json.dumps({"type": "self_profile"}), time.time()))
    return True


STATION_STAFF_KIND = "station_staff_v1"


def latest_station_staff(db) -> list:
    """The newest stored MCS station roster (station_staff_v1) — [] when
    absent or unparsable."""
    d = _latest_artifact(db, STATION_STAFF_KIND)[1]
    staff = d.get("staff") if d is not None else None
    return [s for s in staff if isinstance(s, dict)] \
        if isinstance(staff, list) else []


def record_station_staff(db, staff: list) -> bool:
    """Persist a changed roster and fill a missing self-profile ID.

    Caller holds the transaction. Returns True when either artifact
    was appended; an unchanged roster can still repair the profile.
    """
    changed = staff != latest_station_staff(db)
    if changed:
        db.execute(
            "INSERT INTO artifacts(kind,project_id,content,meta,created_at)"
            " VALUES(?,NULL,?,?,?)",
            (STATION_STAFF_KIND,
             json.dumps({"staff": staff, "fetched_at": int(time.time())},
                        ensure_ascii=False),
             json.dumps({"type": "station_staff"}), time.time()))
    # /users/self can omit its ID. Repair the existing profile even
    # when this roster is unchanged; preserve its org/profession defaults.
    sid = _station_self_id(staff)
    prof = latest_self_profile(db)
    if sid is not None and prof \
            and normalize_sender_id(prof.get("sender_id")) is None:
        changed = record_self_profile(db, {**prof, "sender_id": sid}) or changed
    return changed


def _station_self_id(staff) -> int | None:
    ids = {normalize_sender_id(s.get("staff_id")) for s in staff
           if isinstance(s, dict) and s.get("is_self") is True}
    return next(iter(ids)) if len(ids) == 1 else None


def own_sender_ids(db) -> frozenset[int]:
    """Own-station member IDs, including the logged-in user when listed."""
    return frozenset(sid for s in latest_station_staff(db)
                     if (sid := normalize_sender_id(s.get("staff_id")))
                     is not None)


class _Unset:
    """Marks an omitted preloaded identity; a class because candidate constants are typed values."""


def is_own_station_message(db, sender_id, *, own=_Unset) -> bool | None:
    """Match roster IDs only; absent sender or roster IDs remain unknown."""
    sid = normalize_sender_id(sender_id)
    if own is _Unset:
        own = own_sender_ids(db)
    return sid in own if sid is not None and own else None


def self_sender_id(db) -> int | None:
    """Resolve the logged-in user from the roster, then the self profile."""
    staff = latest_station_staff(db)
    if any(s.get("is_self") is True for s in staff):
        # Ambiguous/invalid roster identities fail closed, even if the
        # profile has an ID. Display names never resolve an identity.
        return _station_self_id(staff)
    return normalize_sender_id(latest_self_profile(db).get("sender_id"))


def is_self_message(db, sender_id, *, self_id=_Unset) -> bool:
    """Whether a known sender ID belongs to the logged-in MCS user (``self_id``: a preloaded self_sender_id)."""
    sid = normalize_sender_id(sender_id)
    if sid is None:
        return False
    return sid == (self_sender_id(db) if self_id is _Unset else self_id)


def self_sets(sig_cfg, db=None):
    """Resolved 'us' identities — config signals.* first, then the
    fetched MCS self profile (self_profile_v1 artifact) as defaults:
    - self_organizations: org names whose posts are OUR actions —
      self-authored mentions are not review candidates for us, and
      their posts count as responder engagement.
    - self_professions: professions whose posts count as pharmacist
      engagement for response checks (config/artifact, else 薬剤師).
    - request_targets: extra requests.to values meaning 'addressed to
      us' — pharmacist-role targets match automatically (config only).
    An explicitly configured list wins even when EMPTY — `[]` is how an
    operator says 'no self org/profession', distinct from 'unset'."""
    sc = sig_cfg if isinstance(sig_cfg, dict) else {}
    def _lst(key):
        v = sc.get(key)
        if not isinstance(v, list):
            return None          # unset or invalid -> fall back
        return [x for x in v if isinstance(x, str) and x]
    orgs, profs = _lst("self_organizations"), _lst("self_professions")
    if (orgs is None or profs is None) and db is not None:
        art = latest_self_profile(db)
        if orgs is None:
            orgs = [x for x in (art.get("organizations") or [])
                    if isinstance(x, str) and x]
        if profs is None and art:
            # a fetched profile is authoritative — an empty
            # specialist list means 'no listed profession', not
            # 'unset'
            profs = [x for x in (art.get("professions") or [])
                     if isinstance(x, str) and x]
    return (orgs or [], (["薬剤師"] if profs is None else profs),
            _lst("request_targets") or [])
