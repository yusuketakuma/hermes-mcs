"""Validate bounded observations for two matching initial-history page walks."""
import hashlib
import re

from mcs_util import payload_hash

PER_PAGE = 10
_FIELDS = {"v", "scope", "phase", "digest", "prior", "last", "mismatches"}
_OBSERVATION = {"v", "project_id", "since", "page", "per_page", "has_next", "digest"}


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def scope(project_id, cohort_id):
    return payload_hash(["initial-history-walk-v1", project_id, cohort_id, "history", 0,
                         PER_PAGE, "pinned", 1, 1])


def observation(project_id, since, page, per_page, has_next, root_ids):
    return {"v": 1, "project_id": project_id, "since": since, "page": page,
            "per_page": per_page, "has_next": has_next,
            "digest": payload_hash(["history-page-v1", project_id, since, page,
                                     per_page, has_next, root_ids])}


def start(project_id, cohort_id, *, mismatches=0, prior=None):
    key = scope(project_id, cohort_id)
    return {"v": 1, "scope": key, "phase": "scan", "digest": key,
            "prior": prior, "last": None, "mismatches": mismatches}


def validate(payload, project_id, cohort_id):
    value = payload.get("walk")
    page = payload.get("page", 1)
    if (not isinstance(value, dict) or value.keys() != _FIELDS
            or type(value["v"]) is not int or value["v"] != 1
            or value["scope"] != scope(project_id, cohort_id)
            or value["phase"] not in ("scan", "accepted")
            or not _hash(value["digest"])
            or value["prior"] is not None and not _hash(value["prior"])
            or value["last"] is not None and not _hash(value["last"])
            or type(value["mismatches"]) is not int or value["mismatches"] < 0
            or type(page) is not int or page < 1
            or (page == 1) != (value["last"] is None)
            or page == 1 and value["digest"] != value["scope"]
            or value["phase"] == "accepted" and (page == 1 or value["prior"] != value["digest"])):
        raise ValueError("initial_history_walk_invalid")
    return value


def accepted(payload, project_id, cohort_id):
    try:
        return payload.get("since") == 0 and validate(payload, project_id, cohort_id)["phase"] == "accepted"
    except (ValueError, TypeError, AttributeError):
        return False


def capture_proof(value, project_id, cohort_id):
    return (isinstance(value, dict) and value.keys() == {"v", "scope", "digest"}
            and type(value["v"]) is int and value["v"] == 1
            and value["scope"] == scope(project_id, cohort_id) and _hash(value["digest"]))


def advance(payload, batch, project_id, cohort_id, start_page, budget, *, checkpoint_safe):
    walk = validate(payload, project_id, cohort_id)
    observations = batch.page_observations
    if (not isinstance(observations, list) or type(batch.pages) is not int
            or not 0 <= batch.pages <= budget or len(observations) != batch.pages
            or type(batch.reached) is not bool):
        raise ValueError("initial_history_observations_invalid")
    for i, obs in enumerate(observations):
        if (not isinstance(obs, dict) or obs.keys() != _OBSERVATION
                or type(obs["v"]) is not int or obs["v"] != 1
                or type(obs["project_id"]) is not int or obs["project_id"] != project_id
                or type(obs["since"]) is not int or obs["since"] != 0
                or type(obs["page"]) is not int or obs["page"] != start_page+i
                or type(obs["per_page"]) is not int or obs["per_page"] != PER_PAGE
                or type(obs["has_next"]) is not bool or not _hash(obs["digest"])
                or i < len(observations)-1 and not obs["has_next"]):
            raise ValueError("initial_history_observations_invalid")
    if batch.reached != bool(observations and not observations[-1]["has_next"]):
        raise ValueError("initial_history_observations_invalid")
    if not checkpoint_safe:
        return "blocked"
    cursor = payload.get("page", 1)
    for obs in observations:
        if obs["page"] == cursor-1:
            if obs["digest"] != walk["last"]:
                payload["walk"] = start(project_id, cohort_id, mismatches=walk["mismatches"]+1)
                payload["page"] = 1
                return "mismatch"
            continue
        if obs["page"] != payload.get("page", 1):
            raise ValueError("initial_history_observations_invalid")
        walk["digest"] = hashlib.sha256(b"history-fold-v1\0" + bytes.fromhex(walk["digest"])
                                         + bytes.fromhex(obs["digest"])).hexdigest()
        walk["last"] = obs["digest"]
        payload["page"] = obs["page"]+1
    if not batch.reached or batch.error:
        return "progress" if payload.get("page", 1) > cursor else "blocked"
    if walk["prior"] == walk["digest"]:
        walk["phase"] = "accepted"
        return "accepted"
    mismatches = walk["mismatches"] + (walk["prior"] is not None)
    payload["walk"] = start(project_id, cohort_id, mismatches=mismatches, prior=walk["digest"])
    payload["page"] = 1
    return "rescan"
