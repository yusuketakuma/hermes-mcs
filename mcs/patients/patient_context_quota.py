"""Limit automatic patient overviews with durable input-bound reservations."""
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re

from mcs_util import strict_json, valid_hash

KIND = "patient_context_generation_v1"
DAILY_LIMIT = 4
INTERVAL = 3 * 60 * 60
_JST = timezone(timedelta(hours=9))
_STATES = {"reserved", "started", "unknown", "complete", "failed", "released"}
_FIELDS = {"version", "input_key", "token", "state", "reserved_at", "lease_until",
           "started_at", "finished_at", "resumes"}


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value < 253402268400


def _arguments(pid, input_key, now):
    if type(pid) is not int or not 0 < pid < 2**63 or not valid_hash(input_key) or not _time(now):
        raise ValueError("patient_context_generation_input_invalid")


def _transaction(db):
    if not db.in_transaction:
        raise RuntimeError("patient_context_generation_transaction_required")


def _document(row):
    try:
        doc = strict_json(row[1])
        if (not isinstance(doc, dict) or set(doc) != _FIELDS
                or type(doc["version"]) is not int or doc["version"] != 1
                or not valid_hash(doc["input_key"])
                or not isinstance(doc["token"], str) or not re.fullmatch("[0-9a-f]{32}", doc["token"])
                or doc["state"] not in _STATES or not _time(doc["reserved_at"])
                or not _time(doc["lease_until"]) or doc["lease_until"] <= doc["reserved_at"]
                or type(doc["resumes"]) is not int or doc["resumes"] < 0
                or row[2] != doc["reserved_at"] or row[3] != "" or row[4] != "{}" or row[5] is not None):
            raise ValueError
        started, finished = doc["started_at"], doc["finished_at"]
        if (started is not None and (not _time(started) or started < doc["reserved_at"])
                or finished is not None and (not _time(finished) or finished < (started or doc["reserved_at"]))
                or doc["state"] == "reserved" and (started is not None or finished is not None or doc["resumes"])
                or doc["state"] in ("started", "unknown") and (started is None or finished is not None)
                or doc["state"] in ("complete", "failed") and (started is None or finished is None)
                or doc["state"] == "released" and (finished is None or doc["resumes"])):
            raise ValueError
        return doc
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError("patient_context_generation_unverified") from None


def _history(db, pid):
    records, keys, active = [], set(), 0
    for row in db.execute("SELECT artifact_id,content,created_at,model,meta,message_id FROM artifacts "
                          "WHERE kind=? AND project_id=? ORDER BY artifact_id", (KIND, pid)):
        doc = _document(row)
        if doc["state"] != "released":
            if doc["input_key"] in keys:
                raise ValueError("patient_context_generation_unverified")
            keys.add(doc["input_key"])
        active += doc["state"] in ("reserved", "started")
        records.append((row, doc))
    if active > 1:
        raise ValueError("patient_context_generation_unverified")
    return records


def _decision(records, input_key, now, catchup):
    day = datetime.fromtimestamp(now, _JST).date()
    retained = [(row, doc) for row, doc in records if doc["state"] != "released"
                and not (doc["state"] == "reserved" and doc["lease_until"] <= now)]
    used = sum(datetime.fromtimestamp(doc["reserved_at"], _JST).date() == day for _, doc in retained)
    result = {"allowed": False, "reason": None, "resume": False, "used": used, "next_at": None}
    if any(max(doc["reserved_at"], doc["started_at"] or 0, doc["finished_at"] or 0) > now for _, doc in records):
        return {**result, "reason": "patient_context_generation_clock_changed"}
    active = next((doc for _, doc in retained if doc["state"] in ("reserved", "started")
                   and doc["lease_until"] > now), None)
    if active:
        return {**result, "reason": "patient_context_generation_active",
                "next_at": active["lease_until"] if active["input_key"] == input_key else None}
    prior = next((doc for _, doc in retained if doc["input_key"] == input_key), None)
    if prior is not None:
        return {**result, "allowed": True, "resume": True}
    if used >= DAILY_LIMIT:
        tomorrow = datetime.combine(day + timedelta(days=1), datetime.min.time(), _JST).timestamp()
        return {**result, "reason": "patient_context_generation_daily_limit", "next_at": tomorrow}
    next_at = max((doc["reserved_at"] + INTERVAL for _, doc in retained), default=now)
    if not catchup and next_at > now:
        return {**result, "reason": "patient_context_generation_interval", "next_at": next_at}
    return {**result, "allowed": True}


def inspect(db, pid, input_key, *, now, catchup=False):
    """Observe generation limits without reserving a slot or authorizing a send."""
    _arguments(pid, input_key, now)
    if type(catchup) is not bool:
        raise ValueError("patient_context_generation_input_invalid")
    return _decision(_history(db, pid), input_key, now, catchup)


def _replace(db, pid, row, doc):
    value = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return db.execute("UPDATE artifacts SET content=? WHERE artifact_id=? AND kind=? AND project_id=? "
                      "AND content=?", (value, row[0], KIND, pid, row[1])).rowcount == 1


def reserve_tx(db, pid, input_key, *, now, lease_until, catchup=False):
    """Reserve or resume under the caller's writer transaction; never dispatch by this receipt alone."""
    _transaction(db)
    _arguments(pid, input_key, now)
    if not _time(lease_until) or lease_until <= now or type(catchup) is not bool:
        raise ValueError("patient_context_generation_input_invalid")
    records = _history(db, pid)
    decision = _decision(records, input_key, now, catchup)
    if not decision["allowed"]:
        return {**decision, "claim": None}
    # Only a reservation which never entered dispatch can expire into an unused slot.
    for row, doc in records:
        if doc["state"] == "reserved" and doc["lease_until"] <= now:
            if not _replace(db, pid, row, {**doc, "state": "released", "finished_at": now}):
                raise ValueError("patient_context_generation_changed")
        elif doc["state"] == "started" and doc["lease_until"] <= now and doc["input_key"] != input_key:
            # Preserve unresolved attempts without making an obsolete input own every future input.
            if not _replace(db, pid, row, {**doc, "state": "unknown"}):
                raise ValueError("patient_context_generation_changed")
    token = os.urandom(16).hex()
    prior = next(((row, doc) for row, doc in records if doc["input_key"] == input_key
                  and doc["state"] in ("started", "unknown", "complete", "failed")), None)
    if prior:
        row, doc = prior
        updated = {**doc, "token": token, "lease_until": lease_until, "state": "started",
                   "finished_at": None, "resumes": doc["resumes"] + 1}
        if not _replace(db, pid, row, updated):
            raise ValueError("patient_context_generation_changed")
        artifact_id = row[0]
    else:
        doc = {"version": 1, "input_key": input_key, "token": token, "state": "reserved",
               "reserved_at": now, "lease_until": lease_until, "started_at": None,
               "finished_at": None, "resumes": 0}
        artifact_id = db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "VALUES(?,?,NULL,?,'','{}',?)", (KIND, pid, json.dumps(doc, sort_keys=True, separators=(",", ":")), now)).lastrowid
    return {**decision, "claim": {"artifact_id": artifact_id, "input_key": input_key, "token": token}}


def _owned(db, pid, claim, now):
    if (type(pid) is not int or not 0 < pid < 2**63 or not _time(now) or not isinstance(claim, dict)
            or set(claim) != {"artifact_id", "input_key", "token"}
            or type(claim["artifact_id"]) is not int or claim["artifact_id"] <= 0):
        return None
    row = db.execute("SELECT artifact_id,content,created_at,model,meta,message_id FROM artifacts "
                     "WHERE artifact_id=? AND kind=? AND project_id=?", (claim["artifact_id"], KIND, pid)).fetchone()
    if row is None:
        return None
    doc = _document(row)
    if (doc["token"] != claim["token"] or doc["input_key"] != claim["input_key"]
            or doc["state"] not in ("reserved", "started")
            or not max(doc["reserved_at"], doc["started_at"] or 0) <= now < doc["lease_until"]):
        return None
    return row, doc


def owns(db, pid, claim, *, now):
    """Fence saving and dispatch against an expired or replaced generation owner."""
    return _owned(db, pid, claim, now) is not None


def begin_tx(db, pid, claim, *, now):
    """Mark dispatch before inference; resumed cloud work still requires CloudInferenceUnit's no-resend guard."""
    _transaction(db)
    owned = _owned(db, pid, claim, now)
    if owned is None:
        return False
    row, doc = owned
    return doc["state"] == "started" or _replace(db, pid, row, {**doc, "state": "started", "started_at": now})


def finish_tx(db, pid, claim, *, now, outcome, proven_unsent=False):
    """Finish known outcomes; unknown work retains its daily count and still requires recovery evidence."""
    _transaction(db)
    if outcome not in ("complete", "failed", "unknown", "unsent") or type(proven_unsent) is not bool:
        raise ValueError("patient_context_generation_outcome_invalid")
    if outcome == "unsent" and not proven_unsent:
        raise ValueError("patient_context_generation_unsent_unverified")
    owned = _owned(db, pid, claim, now)
    if owned is None:
        return False
    row, doc = owned
    if outcome == "unknown":
        # The caller has returned; relinquish only its usable lease. Retain the
        # count and unresolved input so another owner must recover this record.
        return doc["state"] == "started" and _replace(db, pid, row, {**doc, "state": "unknown"})
    if outcome == "unsent":
        # A refusal on a recovery attempt says nothing about the earlier attempt.
        if doc["resumes"]:
            return False
        state = "released"
    elif doc["state"] == "started":
        state = outcome
    else:
        return False
    return _replace(db, pid, row, {**doc, "state": state, "finished_at": now})
