"""Measure source-bound signal lifecycle, human feedback and suppression from one snapshot."""
from __future__ import annotations

import json
import math
import statistics
from typing import TypedDict

from mcs_queries import DAY_S
import stats_contract

SIGNAL_FEEDBACK_MIN_N = 20  # #19 statistical floor, NOT a privacy k threshold


class _FeedbackSegment(TypedDict):
    start: float
    end: float
    mids: list[int]


class _FeedbackEpisode(TypedDict):
    key: str
    type: str
    pid: int
    start: float
    end: float
    state: str
    segments: list[_FeedbackSegment]
    dismissed: bool
    resolved: bool
    reopened: bool
    reopened_after_close: bool
    cause: str | None
    action_times: list[float]
    shown_times: list[float]
    ack_times: list[float]
    adoption_times: list[float]


class _FeedbackLatest(TypedDict):
    state: str
    episode: _FeedbackEpisode


def _feedback_ratio(num, den, unit):
    """Small samples expose counts, not a rate or confidence interval."""
    from stats_math import wilson_interval
    ratio = stats_contract.ratio(num, den, unit)
    ratio["wilson95"] = {"low": None, "high": None}
    if den < SIGNAL_FEEDBACK_MIN_N:
        ratio.update(value=None, reason="insufficient_n")
    else:
        ratio["wilson95"] = wilson_interval(num, den)
    return ratio


def _feedback_episodes(db, scope, types):
    """Signal-artifact scan -> (episodes, invalid_rows, legacy_rows).

    An invalid/terminal row without an observed opening has no
    denominator and voids any open episode under the same key."""
    from mcs_signals import (ARTIFACT_KIND, signal_content,
                             signal_message_ids)
    episodes: list[_FeedbackEpisode] = []
    latest: dict[str, _FeedbackLatest] = {}
    ep: _FeedbackEpisode
    invalid = legacy = 0
    for pid, content_s, meta_s, created in db.execute(
            "SELECT project_id,content,meta,created_at FROM artifacts "
            "WHERE kind=? ORDER BY artifact_id", (ARTIFACT_KIND,)):
        if scope["project_id"] is not None and pid != scope["project_id"]:
            continue
        try:
            meta = json.loads(meta_s)
        except (ValueError, TypeError, RecursionError):
            invalid += 1
            continue
        key = meta.get("key") if isinstance(meta, dict) else None
        if not isinstance(key, str) or not key:
            invalid += 1
            continue
        c = signal_content(content_s, pid)
        if c is None or c["type"] not in types:
            invalid += 1
            previous = latest.pop(key, None)
            if previous is not None:
                previous["episode"]["state"] = "unknown"
            continue
        life = c.get("lifecycle")
        at = life.get("at") if isinstance(life, dict) else None
        if at is None:
            at = c.get({"resolved": "resolved_at", "dismissed": "dismissed_at",
                        "open": "detected_at"}[c["state"]], created)
            legacy += 1
        if (not isinstance(at, (int, float)) or isinstance(at, bool)
                or not 0 <= at < 1e12):
            invalid += 1
            previous = latest.pop(key, None)
            if previous is not None:
                previous["episode"]["state"] = "unknown"
            continue
        if at > scope["as_of"]:
            continue
        prev = latest.get(key)
        if c["state"] == "open":
            if prev is None or prev["state"] != "open":
                if prev is not None:
                    prev["episode"]["reopened_after_close"] = True
                ep = {"key": key, "type": c["type"], "pid": pid, "start": at,
                      "end": scope["as_of"], "state": "open", "segments": [],
                      "dismissed": False, "resolved": False,
                      "reopened": prev is not None, "reopened_after_close": False,
                      "cause": None, "action_times": [], "shown_times": [],
                      "ack_times": [], "adoption_times": []}
                episodes.append(ep)
            else:
                ep = prev["episode"]
                ep["segments"][-1]["end"] = at
            ep["segments"].append({"start": at, "end": scope["as_of"],
                                   "mids": signal_message_ids(c)})
        elif prev is not None:
            ep = prev["episode"]
            ep["end"] = at
            ep["segments"][-1]["end"] = at
            ep["state"] = c["state"]
            if c["state"] == "dismissed":
                ep["dismissed"] = True
                ep["action_times"].append(at)
            else:
                ep["resolved"] = True
                resolution = c.get("resolution")
                cause = resolution.get("cause") if isinstance(resolution, dict) else None
                ep["cause"] = cause if isinstance(cause, str) else None
        else:
            # A terminal row without an observed opening has no denominator.
            invalid += 1
            continue
        latest[key] = {"state": c["state"], "episode": ep}
    return episodes, invalid, legacy


def _feedback_evidence(db, scope, selected) -> None:
    """Fold delivery/ack/request evidence into each selected episode."""
    # Bucketed by patient / shown key so each episode only scans its own
    # candidates instead of every request and manifest (was O(episodes x rows)).
    requests: dict = {}
    for pid, source, created in db.execute(
            "SELECT project_id,source_message_id,created_at FROM requests "
            "WHERE created_at<=?", (scope["as_of"],)):
        requests.setdefault(pid, []).append((source, created))
    # Delivered renders prove exactly which digest page/signal keys were shown.
    # An accepted outbox or card anchor alone is not proof of page delivery.
    manifests = db.execute(
        """SELECT m.manifest_id,m.shown,r.updated_at
           FROM notification_view_manifests m
           JOIN notification_renders r ON r.manifest_id=m.manifest_id
           JOIN notification_cards c ON c.card_id=m.card_id
           WHERE c.kind IN ('signal','digest') AND r.state='delivered'
             AND r.updated_at<=?""", (scope["as_of"],)).fetchall()
    acks = {}
    for mid, at in db.execute(
            "SELECT manifest_id,created_at FROM notification_acknowledgements "
            "WHERE created_at<=? AND (withdrawn_at IS NULL OR withdrawn_at>?)",
            (scope["as_of"], scope["as_of"])):
        acks.setdefault(mid, []).append(at)
    shown: dict = {}
    for mid, shown_s, delivered in manifests:
        try:
            keys = json.loads(shown_s)
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(keys, list):
            for k in {k for k in keys if isinstance(k, str)}:
                shown.setdefault(k, []).append((mid, delivered))
    for ep in selected:
        for source, created in requests.get(ep["pid"], ()):
            if any(source in segment["mids"]
                   and segment["start"] <= created <= segment["end"]
                   for segment in ep["segments"]):
                ep["adoption_times"].append(created)
        for mid, delivered in shown.get(ep["key"], ()):
            if ep["start"] <= delivered <= ep["end"]:
                ep["shown_times"].append(delivered)
                ep["ack_times"].extend(
                    at for at in acks.get(mid, [])
                    if delivered <= at <= ep["end"])
        ep["action_times"].extend(ep["ack_times"] + ep["adoption_times"])


def _feedback_suppression(db, scope, types):
    """Suppression observations -> (suppression, suppression_since, straddled)."""
    from mcs_signals import FEEDBACK_KIND
    suppression = {name: [0, 0] for name in types}
    suppression_since = {}
    straddled = {}
    for pid, content_s in db.execute(
            "SELECT project_id,content FROM artifacts WHERE kind=?",
            (FEEDBACK_KIND,)):
        try:
            c = json.loads(content_s)
        except (ValueError, TypeError, RecursionError):
            continue
        if (not isinstance(c, dict) or not isinstance(c.get("type"), str)
                or c["type"] not in types):
            continue
        if c.get("v") == 2:
            if scope["project_id"] is not None and pid != scope["project_id"]:
                continue
            observations = c.get("observations")
            if (not isinstance(observations, list) or not observations or any(
                    not isinstance(o, list) or len(o) != 3
                    or type(o[0]) not in (int, float) or not math.isfinite(o[0]) or o[0] < 0
                    or type(o[1]) is not int or type(o[2]) is not int
                    or not 0 <= o[2] <= o[1] for o in observations)):
                straddled[c["type"]] = straddled.get(c["type"], 0) + 1
                continue
            intervals = [(at, at, den, num) for at, den, num in observations]
            overflow = c.get("overflow")
            if overflow is not None:
                if (not isinstance(overflow, dict)
                        or type(overflow.get("at")) not in (int, float)
                        or type(overflow.get("last_at")) not in (int, float)
                        or not math.isfinite(overflow["at"])
                        or not math.isfinite(overflow["last_at"])
                        or not 0 <= overflow["at"] <= overflow["last_at"]
                        or type(overflow.get("dismissed_candidates")) is not int
                        or type(overflow.get("same_evidence_suppressed")) is not int
                        or not 0 <= overflow["same_evidence_suppressed"] <= overflow["dismissed_candidates"]):
                    straddled[c["type"]] = straddled.get(c["type"], 0) + 1
                    continue
                intervals.append((overflow["at"], overflow["last_at"],
                                  overflow["dismissed_candidates"], overflow["same_evidence_suppressed"]))
            if (sum(o[2] for o in intervals) != c.get("dismissed_candidates")
                    or sum(o[3] for o in intervals) != c.get("same_evidence_suppressed")
                    or min(o[0] for o in intervals) != c.get("at")
                    or max(o[1] for o in intervals) != c.get("last_at")):
                straddled[c["type"]] = straddled.get(c["type"], 0) + 1
                continue
            for at, last, den, num in intervals:
                if (at > scope["as_of"]
                        or (scope["since"] is not None and last < scope["since"])
                        or (scope["until"] is not None and at >= scope["until"])):
                    continue
                if (last > scope["as_of"]
                        or (scope["since"] is not None and at < scope["since"] <= last)
                        or (scope["until"] is not None and last >= scope["until"])):
                    straddled[c["type"]] = straddled.get(c["type"], 0) + 1
                    continue
                suppression[c["type"]][0] += den
                suppression[c["type"]][1] += num
                suppression_since[c["type"]] = min(at, suppression_since.get(c["type"], at))
            continue
        at, den, num = (c.get("at"), c.get("dismissed_candidates"),
                        c.get("same_evidence_suppressed"))
        if (not isinstance(at, (int, float)) or isinstance(at, bool)
                or not 0 <= at <= scope["as_of"]
                or type(den) is not int or type(num) is not int or not 0 <= num <= den
                or (scope["project_id"] is not None and pid != scope["project_id"])
                or (scope["until"] is not None and at >= scope["until"])):
            continue
        # a day row accumulates [at,last_at]; one crossing a boundary mixes
        # in- and out-of-window evaluations — unknown split, not zero
        last = c.get("last_at", at)
        if (scope["since"] is not None and isinstance(last, (int, float))
                and last < scope["since"]):
            continue
        if (not isinstance(last, (int, float)) or isinstance(last, bool)
                or not at <= last <= scope["as_of"]
                or (scope["since"] is not None and at < scope["since"] <= last)
                or (scope["until"] is not None and last >= scope["until"])):
            straddled[c["type"]] = straddled.get(c["type"], 0) + 1
            continue
        suppression[c["type"]][0] += den
        suppression[c["type"]][1] += num
        suppression_since[c["type"]] = min(at, suppression_since.get(c["type"], at))
    return suppression, suppression_since, straddled


def _feedback_by_type(scope, types, selected, dismissals,
                      suppression, suppression_since, straddled):
    """Aggregate selected episodes and suppression counts per detector."""
    # Enum allowlist: corrupt/legacy cause strings must not leak free text.
    causes = {"request_missing", "request_status_changed", "request_due_changed",
              "request_age_changed", "project_missing", "project_archived",
              "evidence_missing", "evidence_deleted", "evidence_aged_out",
              "request_registered", "responder_post", "self_reaction_observed",
              "followup_record", "volume_below_threshold", "multiple_exclusions"}
    by_type = {}
    for name in sorted(types):
        eps = [ep for ep in selected if ep["type"] == name]
        opened = len(eps)
        shown_n = sum(bool(ep["shown_times"]) for ep in eps)
        acked = sum(bool(ep["ack_times"]) for ep in eps)
        adopted = sum(bool(ep["adoption_times"]) for ep in eps)
        dismissed = sum(ep["dismissed"] for ep in eps)
        resolved = sum(ep["resolved"] for ep in eps)
        closed = sum(ep["state"] in ("resolved", "dismissed") for ep in eps)
        returned = sum(ep["reopened_after_close"] for ep in eps
                       if ep["state"] in ("resolved", "dismissed"))
        open_eps = [ep for ep in eps if ep["state"] == "open"]
        old_open = sum(scope["as_of"] - ep["start"] > 30 * DAY_S for ep in open_eps)
        unactioned = sum(bool(ep["shown_times"]) and not ep["action_times"]
                        and scope["as_of"] - ep["start"] > 30 * DAY_S for ep in open_eps)
        durations = sorted(min(ep["action_times"]) - ep["start"]
                           for ep in eps if ep["action_times"])
        cause_counts = {}
        for ep in eps:
            if ep["resolved"]:
                cause = (ep["cause"] if isinstance(ep["cause"], str)
                         and ep["cause"] in causes else "unclassified")
                cause_counts[cause] = cause_counts.get(cause, 0) + 1
        den, num = suppression[name]
        by_type[name] = {
            "opened": opened, "shown": shown_n, "acked": acked, "adopted": adopted,
            "dismissed_episodes": dismissed, "auto_resolved": resolved,
            "resolution_causes": cause_counts,
            "dismissal_reason_counts": dismissals.get(name, {}),
            "reopened": sum(ep["reopened"] for ep in eps),
            "closed_episodes": closed, "reopened_closed_episodes": returned,
            "open": len(open_eps), "open_over_30d": old_open,
            "shown_open_unactioned_over_30d": unactioned,
            "same_evidence_suppressed": num, "dismissed_candidate_evaluations": den,
            "suppression_first_observed_at": suppression_since.get(name),
            "suppression_rows_straddling_window": straddled.get(name, 0),
            "rates": {
                "shown": _feedback_ratio(shown_n, opened, "opened_episodes"),
                "acked": _feedback_ratio(acked, shown_n, "shown_episodes"),
                "adopted": _feedback_ratio(adopted, opened, "opened_episodes"),
                "dismissed": _feedback_ratio(dismissed, opened, "opened_episodes"),
                "reopened": _feedback_ratio(returned, closed, "closed_episodes"),
                "open_over_30d": _feedback_ratio(old_open, len(open_eps), "open_episodes"),
                "same_evidence_suppression": _feedback_ratio(
                    num, den, "dismissed_candidate_evaluations")},
            "time_to_first_action_s": {
                "samples": len(durations), "denominator": opened,
                "median": statistics.median(durations) if len(durations) >= SIGNAL_FEEDBACK_MIN_N else None,
                "p90": durations[math.ceil(len(durations) * .9) - 1] if len(durations) >= SIGNAL_FEEDBACK_MIN_N else None,
                "reason": None if len(durations) >= SIGNAL_FEEDBACK_MIN_N else "insufficient_n"}}
    return by_type


def st_signal_feedback(db, scope):
    """ST-T2: local signal lifecycle measurement, never clinical completion."""
    from mcs_signals import DETECTORS, dismiss_reason_counts
    types = {name for name, _ in DETECTORS}
    episodes, invalid, legacy = _feedback_episodes(db, scope, types)
    selected = [ep for ep in episodes
                if (scope["since"] is None or ep["start"] >= scope["since"])
                and (scope["until"] is None or ep["start"] < scope["until"])]
    _feedback_evidence(db, scope, selected)
    dismissals = dismiss_reason_counts(
        db, scope["project_id"], since=scope["since"], until=scope["until"],
        as_of=scope["as_of"])
    suppression, suppression_since, straddled = \
        _feedback_suppression(db, scope, types)
    by_type = _feedback_by_type(scope, types, selected, dismissals,
                                suppression, suppression_since, straddled)
    return stats_contract.stat_result("partial" if invalid or straddled else "ok", scope, {
        "by_type": by_type, "invalid_or_orphan_rows": invalid, "legacy_rows": legacy,
        "minimum_rate_samples": SIGNAL_FEEDBACK_MIN_N,
        "privacy_minimum": {"status": "owner_decision_required",
                            "reason": "no_existing_signal_group_minimum"},
        "definitions": {
            "cohort": "episodes opened in [since,until), observed through as_of",
            "dismissal_reason_counts": "dismissal events in [since,until), through as_of",
            "suppression": "observed dismissed candidate evaluations in [since,until)",
            "adopted": "source-linked request registered during an evidence segment; proxy, not causation",
            "acked": "non-withdrawn page acknowledgement; not adoption or clinical completion",
            "reopened": "closed cohort episodes observed returning by as_of; right-censored",
            "time_to_first_action": "ack, source-linked request or dismissal; acted episodes only",
            "resolution": "observed exclusion of stored evidence; unclassified when not grounded",
            "intervals": "Wilson95; repeated episodes/evaluations are not independent trials",
            "history": "legacy suppression and missing lifecycle history are not reconstructed"}},
        reason=("lifecycle_history_incomplete" if invalid else
                "suppression_day_straddles_window" if straddled else None))
