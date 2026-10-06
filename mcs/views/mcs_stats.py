"""Cross-project statistics over the published ledger snapshot.

Implements the foundation tiers of MCS-STAT-PROSPECTIVE-20260921:
registry + shared calculation contract + T0/T1 stats, and the med
stats that the extract_llm artifact set can support today.

Rules carried over from the spec:
- Read-only: every function takes the View's already-validated read
  connection. No writes, no own connections, no job scheduling (INV-02).
- One fixed input: stats run against the single snapshot generation the
  caller opened — never re-open mid-run, never fall back to the live DB.
- Ratios always carry numerator/denominator/unit; a zero denominator is
  null + reason, never 0% and never NaN/Infinity (A-7).
- Missing capability is reported as unavailable/unsupported with a
  reason — never faked as an empty success (INV-06, §12).
- Details carry ids only (project_id / message_id); bodies and patient
  names stay on the protected per-patient views (§11).
"""
from __future__ import annotations

import json
import sqlite3
import math
import statistics
from datetime import datetime, timedelta
from typing import TypedDict

from mcs_queries import (CHANGE_ACTIONS, DAY_S, FACT_KINDS_SQL, JST,
                         MED_ACTIONS,
                         current_fact_pred,
                         iter_period_ends, json_or_null,
                         med_capability_evidence,
                         med_is_patient_current, med_period_artifacts,
                         transition_cooccurrences, thread_reply_pairs)
from project_metadata_view import get_project_metadata
from drug_map import current_refs
DEFINITION_VERSION = "2026-10-05"

# engineering caps (A-8): detail 20 default / 100 max, top categories
# 100 max, time buckets 120 — row-count caps bound the response size
DETAIL_LIMIT = 100
CATEGORY_LIMIT = 100
BUCKET_LIMIT = 120


def _ratio(num: int, den: int, unit: str) -> dict:
    """Ratio with explicit denominator — 0 denominator is null, not 0%."""
    return {"numerator": num, "denominator": den, "unit": unit,
            "value": (num / den) if den else None,
            "reason": None if den else "denominator_zero"}


def _parse_when(text: str) -> int:
    """YYYY-MM-DD -> JST midnight; datetime requires an explicit offset.
    A bare `until` date is that day's 0:00 — to include a whole day the
    caller passes the next date (A-6: since <= t < until)."""
    if not isinstance(text, str):
        raise ValueError("bad_time_arg")
    try:
        if "T" not in text and " " not in text:
            d = datetime.strptime(text, "%Y-%m-%d")
            return int(d.replace(tzinfo=JST).timestamp())
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        if dt.tzinfo is None:
            raise ValueError("bad_time_arg")
        return int(dt.timestamp())
    except (ValueError, OverflowError):
        raise ValueError("bad_time_arg") from None


def _scope(args: dict, snapshot_ts: int) -> dict:
    """Resolve since/until/as_of once. Half-open [since, until) —
    including a whole day needs the next date as `until`. as_of defaults
    to the snapshot's generated_at and may not exceed it (A-6)."""
    since = _parse_when(args["since"]) if args.get("since") else None
    until = _parse_when(args["until"]) if args.get("until") else None
    if since is not None and until is not None and until <= since:
        raise ValueError("bad_period")
    as_of = _parse_when(args["as_of"]) if args.get("as_of") else snapshot_ts
    if as_of > snapshot_ts:
        raise ValueError("as_of_after_snapshot")
    if until is not None and until > as_of:
        until = as_of
        # clamping may invert the range — surface it, don't silently
        # return an empty success
        if since is not None and until <= since:
            raise ValueError("bad_period")
    try:
        limit = min(max(int(args.get("limit") or 20), 1), DETAIL_LIMIT)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("bad_limit") from None
    return {"since": since, "until": until, "as_of": as_of,
            "project_id": args.get("project"), "limit": limit}


def _where(scope: dict, col: str = "m.posted_at_ts") -> tuple[str, list]:
    sql, params = "", []
    if scope["since"] is not None:
        sql += f" AND {col} >= ?"
        params.append(scope["since"])
    if scope["until"] is not None:
        sql += f" AND {col} < ?"
        params.append(scope["until"])
    elif scope.get("as_of") is not None:
        # as_of freezes the window even without --until (refstats pins
        # it): nothing posted after it counts; undated rows keep their
        # pre-existing treatment
        sql += f" AND ({col} IS NULL OR {col} <= ?)"
        params.append(scope["as_of"])
    if scope["project_id"] is not None:
        sql += " AND m.project_id = ?"
        params.append(scope["project_id"])
    return sql, params


def _items(rows: list, limit: int) -> dict:
    return {"total": len(rows), "returned": min(len(rows), limit),
            "truncated": len(rows) > limit, "items": rows[:limit]}


def _result(status: str, scope: dict, data: dict, reason=None) -> dict:
    out = {"status": status, "definition_version": DEFINITION_VERSION,
           "scope": {"since": scope["since"], "until": scope["until"],
                     "as_of": scope["as_of"],
                     "project_id": scope["project_id"]}}
    if reason:
        out["reason"] = reason
    out.update(data)
    return out


def _as_of_day(scope):
    """Return the local calendar day only when the stored timestamp is representable."""
    try:
        return datetime.fromtimestamp(scope["as_of"], JST).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------- T0 ----------------

def st_data_quality(db, scope):
    """ST-002: three-stage coverage — fetched / parsed / stat-ready."""
    w, p = _where(scope)
    total = db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE 1=1{w}", p).fetchone()[0]
    fetched = db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE body_state='full'{w}",
        p).fetchone()[0]
    body_states = db.execute(
        f"SELECT COALESCE(body_state,'null'), COUNT(*) FROM messages m"
        f" WHERE 1=1{w} GROUP BY 1", p).fetchall()
    # parsed = the message has a current extraction of any fact
    # generation — the same current_fact_pred rule the fact stats read,
    # so a v4-current message (no current extract_llm row by design)
    # is not undercounted or reported stale
    current = (f"SELECT 1 FROM artifacts a WHERE a.kind IN ({FACT_KINDS_SQL})"
               f" AND a.message_id=m.message_id"
               f" {current_fact_pred(content=False)}")
    parsed = db.execute(
        f"""SELECT COUNT(*) FROM messages m WHERE 1=1{w}
            AND EXISTS ({current})""",
        p).fetchone()[0]
    stale = db.execute(
        f"""SELECT COUNT(*) FROM messages m WHERE 1=1{w}
            AND EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind IN ({FACT_KINDS_SQL})
                          AND a.message_id=m.message_id
                          AND json_valid(a.meta)
                          AND json_extract(a.meta,'$.hash')!=m.content_hash)
            AND NOT EXISTS ({current})""",
        p).fetchone()[0]
    timed = db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE posted_at_ts NOT NULL{w}",
        p).fetchone()[0]
    # extract artifacts whose meta is not valid JSON: invisible to both
    # 'parsed' and 'stale_parsed' yet permanently unparsed upstream
    meta_bad = db.execute(
        f"""SELECT COUNT(*) FROM artifacts a JOIN messages m
            ON m.message_id=a.message_id
            WHERE a.kind='extract_llm' AND NOT json_valid(a.meta){w}""",
        p).fetchone()[0]
    # low-signal prefilter markers: 'parsed' includes them (the message
    # IS processed — never pending again), but the count stays visible
    # so "no extraction" is never conflated with "extracted nothing"
    prefiltered = db.execute(
        f"""SELECT COUNT(*) FROM artifacts a JOIN messages m
            ON m.message_id=a.message_id
            WHERE a.kind='extract_llm' AND json_valid(a.meta)
              AND json_extract(a.meta,'$.prefilter') IS NOT NULL{w}""",
        p).fetchone()[0]
    return _result("ok", scope, {
        "stages": {
            "fetched": _ratio(fetched, total, "messages"),
            "parsed_current_revision": _ratio(parsed, total, "messages"),
            "stat_ready_timestamped": _ratio(timed, total, "messages")},
        "body_states": dict(body_states),
        "stale_parsed": stale,
        "extract_meta_unparseable": meta_bad,
        "extract_prefiltered": prefiltered,
        "notes": ["body_absent_vs_unparsed kept separate",
                  "stale_parsed = extraction exists but for an older "
                  "content revision",
                  "extract_prefiltered = marked no-signal without an "
                  "LLM call"]})


# ---------------- T1 ----------------

def st_overview(db, scope):
    """ST-001: rooms / patients / posts / senders / orgs / period."""
    w, p = _where(scope)
    rooms = db.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    state_rows = db.execute(
        "SELECT CASE WHEN is_archived=1 THEN 'archived' "
        "       WHEN fetch_state IS NULL THEN 'unknown' "
        "       ELSE fetch_state END s, COUNT(*) FROM patients "
        "GROUP BY s").fetchall()
    posts = db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE 1=1{w}", p).fetchone()[0]
    active_rooms = db.execute(
        f"SELECT COUNT(DISTINCT m.project_id) FROM messages m"
        f" WHERE 1=1{w}", p).fetchone()[0]
    senders = db.execute(
        f"SELECT COUNT(DISTINCT m.sender_id) FROM messages m"
        f" WHERE sender_id IS NOT NULL{w}", p).fetchone()[0]
    orgs = db.execute(
        f"SELECT COUNT(DISTINCT m.organization) FROM messages m"
        f" WHERE organization NOT NULL AND organization!=''{w}",
        p).fetchone()[0]
    rng = db.execute(
        "SELECT MIN(posted_at_ts), MAX(posted_at_ts) FROM messages"
    ).fetchone()
    return _result("ok", scope, {
        "rooms_total": rooms,
        "rooms_by_state": dict(state_rows),
        # rooms ≠ confirmed patients — never reported as patient count
        "patient_count_note": "rooms, not confirmed patients",
        "rooms_with_posts_in_scope": active_rooms,
        "posts_in_scope": posts,
        "distinct_senders_in_scope": senders,
        "distinct_organizations_in_scope": orgs,
        "stored_range": {"first_posted_ts": rng[0],
                         "last_posted_ts": rng[1]}})


def st_patient_activity(db, scope):
    """ST-003: per-room post counts over 7/14/30d windows ending as_of."""
    out = {}
    for days in (7, 14, 30):
        since = scope["as_of"] - days * DAY_S
        w = " AND m.posted_at_ts >= ? AND m.posted_at_ts < ?"
        params = [since, scope["as_of"]]
        if scope["project_id"] is not None:
            w += " AND m.project_id = ?"
            params.append(scope["project_id"])
        rows = db.execute(
            f"""SELECT m.project_id, COUNT(*) posts,
                       COUNT(DISTINCT date(m.posted_at_ts,'unixepoch','+9 hours')) active_days,
                       COUNT(DISTINCT m.sender_id) senders,
                       COUNT(DISTINCT NULLIF(m.profession,'')) professions,
                       COUNT(DISTINCT NULLIF(m.organization,'')) orgs
                FROM messages m WHERE 1=1{w}
                GROUP BY m.project_id ORDER BY posts DESC""",
            params).fetchall()
        out[f"last_{days}d"] = _items(
            [{"project_id": r[0], "posts": r[1], "active_days": r[2],
              "senders": r[3], "professions": r[4], "orgs": r[5]}
             for r in rows], scope["limit"])
    return _result("ok", scope, {
        "windows": out,
        "notes": ["post volume is not severity",
                  "active_days = days with at least one post, JST",
                  "windows are fixed N*86400s ending at as_of; "
                  "--since/--until do not apply"]})


def st_professions(db, scope):
    """ST-004: posts / senders / rooms by recorded profession."""
    w, p = _where(scope)
    rows = db.execute(
        f"""SELECT COALESCE(NULLIF(m.profession,''),'unknown') prof,
                   COUNT(*) posts, COUNT(DISTINCT m.sender_id) senders,
                   COUNT(DISTINCT m.project_id) rooms
            FROM messages m WHERE 1=1{w}
            GROUP BY prof ORDER BY posts DESC""", p).fetchall()
    return _result("ok", scope, {
        "by_profession": _items(
            [{"profession": r[0], "posts": r[1], "senders": r[2],
              "rooms": r[3]} for r in rows], CATEGORY_LIMIT),
        "notes": ["raw recorded profession values — no profession_map "
                  "normalization yet",
                  "non-posting staff never appear; not a roster"]})


def st_workload(db, scope):
    """ST-005: JST weekday x time-band distribution."""
    w, p = _where(scope)
    rows = db.execute(
        f"SELECT m.posted_at_ts, m.project_id FROM messages m"
        f" WHERE m.posted_at_ts NOT NULL{w}", p).fetchall()
    untimed = db.execute(
        f"SELECT COUNT(*) FROM messages m"
        f" WHERE m.posted_at_ts IS NULL{w}", p).fetchone()[0]
    bands = {}
    room_seen = {}
    for ts, pid in rows:
        try:
            dt = datetime.fromtimestamp(ts, JST)
        except (ValueError, OverflowError, OSError) as error:
            return _result("unavailable", scope, {},
                           reason=f"data_error:{type(error).__name__}")
        weekend = dt.weekday() >= 5
        day = 8 <= dt.hour < 18
        band = ("weekend" if weekend else
                "weekday_day" if day else "weekday_night")
        key = (dt.weekday(), dt.strftime("%a"), band)
        bands[key] = bands.get(key, 0) + 1
        room_seen.setdefault(key, set()).add(pid)
    by_day_band = [{"weekday": k[1], "band": k[2], "posts": n,
                    "rooms": len(room_seen[k])}
                   for k, n in sorted(bands.items())]
    return _result("ok", scope, {
        "by_weekday_band": _items(by_day_band, BUCKET_LIMIT),
        "untimed_posts_excluded": untimed,
        "band_definition": "weekday_day=Mon-Fri 08:00-18:00 JST; "
                           "weekday_night=other weekday hours; "
                           "weekend=Sat/Sun",
        "notes": ["public holidays not distinguished — no holiday "
                  "calendar wired in",
                  "night posts do not imply night work or urgency"]})


def st_doc_burden(db, scope):
    """ST-006: sender concentration — top1/5/10 share + HHI."""
    w, p = _where(scope)
    # group by sender_id — same display name ≠ same sender
    rows = db.execute(
        f"""SELECT COALESCE(m.sender_id,-1) sid, COUNT(*) n,
                   SUM(m.sender_name IS NULL) nameless
            FROM messages m WHERE 1=1{w}
            GROUP BY sid ORDER BY n DESC""", p).fetchall()
    total = sum(n for _, n, _ in rows)
    def share(k):
        return _ratio(sum(n for _, n, _ in rows[:k]), total, "posts")
    hhi = sum((n / total) ** 2 for _, n, _ in rows) if total else None
    return _result("ok", scope, {
        "top1_share": share(1), "top5_share": share(5),
        "top10_share": share(10),
        "hhi": round(hhi, 4) if hhi is not None else None,
        "hhi_reason": None if hhi is not None else "denominator_zero",
        "sender_count": len(rows),
        # F24: identity missing is a ROW-level sender_id absence (the -1
        # bucket), not "some post lacked a display name"; name gaps are
        # a separate metric now — one NULL name no longer marks every
        # post by that sender unknown
        "unknown_sender_posts": sum(n for sid, n, _ in rows if sid == -1),
        "nameless_sender_posts": sum(nl or 0 for _, _, nl in rows),
        "notes": ["post count is not a performance score; "
                  "delegate posting and division of labour skew it",
                  "grouped by sender_id — distinct senders sharing a "
                  "display name stay separate"]})


# ---------------- meds (extract_llm source) ----------------

def _med_messages(db, scope, extra_sql="", extra_params=()):
    """(project_id, message_id, posted_at_ts, meds) per message with a
    current-revision fact artifact carrying meds, in scope. artifacts has
    no UNIQUE(kind, message_id), so duplicate current-hash rows are
    deduplicated per message_id here rather than double-counted — the
    NEWEST row wins, as in every display reader (structured_view)."""
    w, p = _where(scope)
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts, a.content
            FROM artifacts a JOIN messages m ON m.message_id=a.message_id
            WHERE a.kind IN ({FACT_KINDS_SQL})
              {current_fact_pred()}
              {extra_sql}{w}
            ORDER BY a.artifact_id DESC""",
        [*extra_params, *p])
    seen = set()
    for pid, mid, ts, content in rows:
        if mid in seen:
            continue
        seen.add(mid)
        meds = json.loads(content).get("meds") or []
        if meds:
            yield pid, mid, ts, meds


def _med_rows(db, scope, msgs=None):
    """(project_id, message_id, med_dict, posted_at_ts) for the
    patient's current med mentions in scope (see _med_messages);
    ``msgs`` reuses an already-materialized _med_messages list."""
    for pid, mid, ts, meds in (_med_messages(db, scope) if msgs is None
                               else msgs):
        for med in meds:
            # negated / other-person / historical-report mentions are
            # not the patient's current medication activity — the same
            # predicate the prospective signal applies
            if not med_is_patient_current(med):
                continue
            yield pid, mid, med, ts


def st_meds(db, scope):
    """ST-007: med actions by raw name x month. Names are NOT normalized
    to ingredients; dictionary candidates are a separate annotation."""
    per_month = {}
    action_counts = {}
    names = set()
    # one scan feeds both the name/month table and the candidate block
    msgs = list(_med_messages(db, scope))
    for _pid, _mid, med, ts in _med_rows(db, scope, msgs):
        month = datetime.fromtimestamp(ts, JST).strftime("%Y-%m") \
            if ts is not None else "unknown"
        name = (med.get("name") or "").strip() or "(unnamed)"
        action = med.get("action") if med.get("action") in MED_ACTIONS \
            else "other"
        names.add(name)
        action_counts[action] = action_counts.get(action, 0) + 1
        key = (name, month)
        per_month[key] = per_month.get(key, 0) + 1
    top = sorted(per_month.items(), key=lambda kv: -kv[1])
    return _result("ok", scope, {
        "action_totals": action_counts,
        "distinct_names": len(names),
        "by_name_month": _items(
            [{"name": k[0], "month": k[1], "mentions": n}
             for k, n in top], CATEGORY_LIMIT),
        "by_ingredient_candidate": _med_candidate_breakdown(db, msgs),
        "notes": ["names are raw surface forms, NOT ingredient-normalized",
                  "ingredient annotations are dictionary candidates, unconfirmed",
                  "mention counts, not deduplicated change events",
                  "'none' = mentioned without a change action"]})


def _med_candidate_breakdown(db, msgs):
    """Same mention denominator as ST-007, with unavailable annotations
    explicit; ``msgs`` is st_meds' materialized _med_messages list."""
    counts = dict.fromkeys(
        ("resolved", "ambiguous", "unresolved", "generic", "unavailable"), 0)
    ingredients = {}
    total = 0
    for _pid, mid, _ts, meds in msgs:
        refs = {ref["i"]: ref for ref in current_refs(db, mid)}
        for i, med in enumerate(meds):
            if not med_is_patient_current(med):
                continue
            total += 1
            ref = refs.get(i)
            status = ref["status"] if ref and ref["name"] == med.get("name") \
                else "unavailable"
            counts[status] += 1
            if status != "resolved" or ref is None:
                continue
            for cand in ref["cands"]:
                if cand["kind"] != "ingredient" or cand["candidate"] is not True:
                    continue
                key = (ref["dict_id"], ref["dict_sha256"],
                       ref["resolver_version"], cand["system"],
                       cand["code"], cand["display"])
                ingredients[key] = ingredients.get(key, 0) + 1
    return {
        "status": "ok" if total > counts["unavailable"] else "unavailable",
        "mentions": total,
        "by_resolution": {k: _ratio(n, total, "mention_share")
                          for k, n in counts.items()},
        "items": _items([
            {"dict_id": k[0], "dict_sha256": k[1], "resolver_version": k[2],
             "system": k[3], "code": k[4], "display": k[5],
             "candidate": True, "mentions": n,
             "share": _ratio(n, total, "mention_share")}
            for k, n in sorted(ingredients.items(), key=lambda kv: (-kv[1], kv[0]))
        ], CATEGORY_LIMIT)}


def st_med_mentions(db, scope):
    """ST-008: distinct med names per room in scope."""
    per_room = {}
    for pid, _mid, med, _ts in _med_rows(db, scope):
        name = (med.get("name") or "").strip()
        if name:
            per_room.setdefault(pid, set()).add(name)
    rows = sorted(({"project_id": pid, "distinct_med_names": len(s)}
                   for pid, s in per_room.items()),
                  key=lambda r: -r["distinct_med_names"])
    return _result("ok", scope, {
        "rooms_with_mentions": len(rows),
        "by_room": _items(rows, scope["limit"]),
        "notes": ["distinct names in period ≠ currently used drugs",
                  "negated / other-person / historical mentions are "
                  "excluded by med_is_patient_current"]})


def st_med_change_burden(db, scope):
    """ST-009: med change actions per room over 7/14/30d windows.
    Windows are JST calendar days ending on the as_of date — unlike
    patient_activity's exact N*86400s epoch windows (noted below)."""
    changes = {}  # (pid, day) -> count ; pid -> total
    for pid, _mid, med, ts in _med_rows(db, scope):
        # capability statements (「〜は出来ない」) are not change events —
        # the same exclusion the signals and transition stats apply
        if med.get("action") not in CHANGE_ACTIONS \
                or med_capability_evidence(med.get("evidence")):
            continue
        day = datetime.fromtimestamp(ts, JST).date().isoformat() \
            if ts is not None else "unknown"
        key = (pid, day)
        changes[key] = changes.get(key, 0) + 1
    as_of_day = datetime.fromtimestamp(scope["as_of"], JST) \
        .date().isoformat()
    out = {}
    for days in (7, 14, 30):
        cutoff = (datetime.fromtimestamp(scope["as_of"], JST)
                  - timedelta(days=days - 1)).date().isoformat()
        per_room = {}
        for (pid, day), n in changes.items():
            if (day != "unknown" and cutoff <= day <= as_of_day
                    and (scope["project_id"] is None
                         or pid == scope["project_id"])):
                per_room[pid] = per_room.get(pid, 0) + n
        out[f"last_{days}d"] = _items(
            [{"project_id": pid, "change_mentions": n}
             for pid, n in sorted(per_room.items(), key=lambda x: -x[1])],
            scope["limit"])
    busiest = sorted((({"project_id": pid, "day": day, "changes": n}
                       for (pid, day), n in changes.items())),
                     key=lambda r: -r["changes"])
    return _result("ok", scope, {
        "windows": out,
        "busiest_days": _items(busiest, scope["limit"]),
        "notes": ["mention-level dedup only — the same change reported "
                  "by three professions counts three times",
                  "change count is not clinical instability",
                  "windows are JST calendar days ending on the as_of "
                  "date; patient_activity uses exact N*86400s windows "
                  "instead"]})


def st_adherence_events(db, scope):
    """ST-010: adherence problem mentions (残薬/飲み忘れ etc.) need
    valid_facts polarity; extract_llm events don't carry them."""
    return _result("unavailable", scope, {},
                   reason="needs valid_facts (fact-level polarity); "
                          "extract_llm events cannot distinguish "
                          "present/absent/improved adherence problems")


def st_rx_expiry(db, scope):
    """ST-012: extract_v1 med_periods ending within the horizon, per
    room. Period expressions are parsed surface forms (e.g. '4/8-4/21')
    — NOT verified prescription periods, and not linked to specific
    drug names."""
    today = _as_of_day(scope)
    if today is None:
        return _result("unavailable", scope, {}, reason="data_error:timestamp_unrepresentable")
    try:
        horizon = today + timedelta(days=14)
    except OverflowError:
        return _result("unavailable", scope, {}, reason="data_error:timestamp_unrepresentable")
    per_room = {}
    items = []
    seen = set()
    posted_ok = {}
    for pid, mid, content in med_period_artifacts(db):
        for p, end_d in iter_period_ends(content):
            if not (today <= end_d <= horizon):
                continue
            key = (pid, mid, p.get("raw") or p["end"])
            if key in seen:
                continue
            seen.add(key)
            if scope["project_id"] is not None \
                    and pid != scope["project_id"]:
                continue
            if mid not in posted_ok:
                # as_of freeze: a period posted after as_of did not
                # exist yet; undated rows keep their treatment (_where)
                posted_ok[mid] = db.execute(
                    "SELECT 1 FROM messages WHERE message_id=? AND "
                    "(posted_at_ts IS NULL OR posted_at_ts <= ?)",
                    (mid, scope["as_of"])).fetchone() is not None
            if not posted_ok[mid]:
                continue
            per_room[pid] = per_room.get(pid, 0) + 1
            items.append({"project_id": pid, "message_id": mid,
                          "raw": p.get("raw"), "start": p.get("start"),
                          "end": p["end"],
                          "days_left": (end_d - today).days})
    items.sort(key=lambda r: r["days_left"])
    return _result("ok", scope, {
        "expiring_periods": _items(items, scope["limit"]),
        "rooms_with_expiring": len(per_room),
        "horizon_days": 14,
        "notes": ["surface-form period expressions from extract_v1 — "
                  "not verified prescription periods",
                  "periods are not linked to specific drug names",
                  "a period without an end date cannot be evaluated"]})


def st_med_change_followup(db, scope):
    """ST-T2: retrospective count of med change mentions (>=7d old at
    as_of) with no subsequent room post within 7d. 'No follow-up
    record found' — never 'no follow-up happened'. A request row
    referencing the message counts as a visible follow-up."""
    cutoff = scope["as_of"] - 7 * DAY_S
    total, no_follow = 0, []
    for pid, mid, ts, meds in _med_messages(
            db, scope, "AND m.posted_at_ts IS NOT NULL"
            " AND m.posted_at_ts <= ?", (cutoff,)):
        # CHANGE_ACTIONS only — a "none" (no-change) mention is not a
        # change; negated/other-person/historical mentions are filtered
        # by the same predicate the signal detector uses
        if not any(med_is_patient_current(x)
                   and x.get("action") in CHANGE_ACTIONS
                   and not med_capability_evidence(x.get("evidence"))
                   for x in meds):
            continue
        total += 1
        tracked = db.execute(
            "SELECT 1 FROM requests WHERE source_message_id=? "
            "AND created_at<=? LIMIT 1", (mid, scope["as_of"])).fetchone()
        if tracked:
            continue
        follow = db.execute(
            "SELECT 1 FROM messages WHERE project_id=? "
            "AND posted_at_ts > ? AND posted_at_ts <= ? LIMIT 1",
            (pid, ts, ts + 7 * DAY_S)).fetchone()
        if not follow:
            no_follow.append({"project_id": pid, "message_id": mid})
    return _result("ok", scope, {
        "change_mentions_7d_plus": _ratio(total, total, "messages"),
        "no_followup_record": _items(
            sorted(no_follow, key=lambda r: r["message_id"]),
            scope["limit"]),
        "notes": ["'follow-up record' = any later room post OR a "
                  "request registered against the message — neither "
                  "proves a clinical response occurred",
                  "absence is reported as 'not confirmable in records', "
                  "not as missed work"]})


def st_transition_reconciliation(db, scope):
    """ST-T2: typed discharge/transfer events (extract_llm `events`)
    co-occurring with med change-action mentions within ±14 days in
    the same room. Co-occurrence count only — reconciliation need is
    a human decision."""
    w, p = _where(scope, "d.posted_at_ts")
    # the ±14d window must not reach past the frozen window end either:
    # a med change posted after until/as_of did not exist yet
    if scope["until"] is not None:
        w, p = w + " AND m.posted_at_ts < ?", [*p, scope["until"]]
    elif scope.get("as_of") is not None:
        w, p = w + " AND m.posted_at_ts <= ?", [*p, scope["as_of"]]
    grouped = transition_cooccurrences(
        db, win_s=14 * DAY_S, extra_where=w, params=p)
    items = [{"project_id": pid, "discharge_message_id": dmid,
              "med_change_message_ids": sorted(mids)}
             for dmid, (pid, mids) in grouped.items()]
    return _result("ok", scope, {
        "cooccurrences": _items(items, scope["limit"]),
        "notes": ["co-occurrence of typed LLM events + med mentions — "
                  "not proof that reconciliation is needed or missing",
                  "discharge/transfer detected via extract_llm events; "
                  "coverage limited to extracted messages"]})


def st_open_loop_aging(db, scope):
    """ST-024 (formal side only): open requests by overdue-age bucket.
    Text-derived candidates require interaction_links — reported
    separately as unavailable. due_date is stored as a validated
    YYYY-MM-DD (mcs_requests), but out-of-band rows may not parse —
    those surface as due_unparseable, not silently as no_due."""
    rows = db.execute(
        "SELECT request_id, project_id, status, due_date FROM requests "
        "WHERE status IN ('open','in_progress') "
        # as_of freeze: requests registered later did not exist yet;
        # status itself is the snapshot's, not the status at as_of
        "AND (created_at IS NULL OR created_at <= ?)" +
        (" AND project_id = ?" if scope["project_id"] is not None else ""),
        [scope["as_of"]] +
        ([scope["project_id"]] if scope["project_id"] is not None else [])
    ).fetchall()
    buckets = {"not_yet_due": 0, "0-7d": 0, "8-30d": 0, "31-90d": 0,
               "over_90d": 0, "no_due": 0}
    as_of_day = _as_of_day(scope)
    if as_of_day is None:
        return _result("unavailable", scope, {}, reason="data_error:timestamp_unrepresentable")
    items = []
    for rid, pid, status, due in rows:
        age_d = None
        unparseable = False
        if due is not None:
            try:
                # whole JST calendar days past the due date — a request
                # is not overdue on its own due day
                due_day = datetime.fromtimestamp(
                    _parse_when(str(due)), JST).date()
                age_d = (as_of_day - due_day).days
            except (ValueError, TypeError, OverflowError, OSError):
                unparseable = True
        if due is None:
            buckets["no_due"] += 1
        elif unparseable:
            buckets["no_due"] += 1  # counted but flagged, not hidden
        elif age_d <= 0:
            buckets["not_yet_due"] += 1
        else:
            buckets["over_90d" if age_d > 90 else
                    "31-90d" if age_d > 30 else
                    "8-30d" if age_d > 7 else "0-7d"] += 1
        items.append({"request_id": rid, "project_id": pid,
                      "status": status, "due_date": due,
                      "due_unparseable": unparseable or None,
                      "days_since_due": age_d})
    items.sort(key=lambda r: (r["days_since_due"] is None,
                              -(r["days_since_due"] or 0)))
    return _result("partial", scope, {
        "formal_open_requests": _items(items, scope["limit"]),
        "age_buckets": buckets,
        "oldest_open_due": min((i["due_date"] for i in items
                                if i["due_date"] is not None
                                and not i["due_unparseable"]), default=None),
        "text_candidates": {"status": "unavailable",
                            "reason": "needs interaction_links"},
        "notes": ["overdue age measured from due date, not creation",
                  "--since/--until do not filter the request register"],
        },
        reason="text-derived open-loop candidates not computable; "
               "formal request register only")


def st_canonical_facts(db, scope):
    """ST-T5: verified canonical facts carried by current
    canonical_projection artifacts — the coverage pharmacists actually
    read. Counting never re-derives claims: facts are enumerated once
    per fact_id on the newest current projection; a stale projection
    (hash drift / invalidated) contributes nothing."""
    w, p = _where(scope)
    rows = db.execute(
        f"""SELECT a.content FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            WHERE a.kind IN ('canonical_projection','semantic_facts_v4')
              {current_fact_pred()}
              AND json_array_length({json_or_null('a.content')},
                                    '$.canonical_facts')>0{w}
            ORDER BY a.artifact_id DESC""", p).fetchall()
    total = evidenced = 0
    by_kind: dict = {}
    seen: set = set()
    for (content,) in rows:
        facts = json.loads(content).get("canonical_facts") or []
        for f in facts:
            if not isinstance(f, dict):
                continue
            fid = f.get("fact_id")
            if not isinstance(fid, str) or not fid or fid in seen:
                continue
            seen.add(fid)
            total += 1
            kind = f.get("kind") or "unknown"
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if f.get("evidence_quote"):
                evidenced += 1
    return _result("ok", scope, {
        "total": total, "evidenced": evidenced, "by_kind": by_kind,
        "notes": ["facts counted once per fact_id on the newest "
                  "current projection — stale/invalidated generations "
                  "are excluded by the shared current_fact_pred rule",
                  "kinds with no legacy slot (allergy/adverse/vitals/"
                  "preference/observation) appear only here"]})


def st_card_parts(db, scope):
    """ST-T7: durable render-part coverage — planned vs delivered parts
    across every issued render. A render's plan is 'incomplete' while
    any part remains pending or reached a non-delivered terminal state,
    so a delivered card can never masquerade as a complete render."""
    rows = db.execute(
        "SELECT state, COUNT(*) c FROM notification_render_parts "
        "GROUP BY state").fetchall()
    by_state = {r["state"]: r["c"] for r in rows}
    renders = db.execute(
        "SELECT parts_state, COUNT(*) c FROM notification_renders "
        "WHERE parts_state IS NOT NULL AND parts_state != 'none' "
        "GROUP BY parts_state").fetchall()
    by_render = {r["parts_state"]: r["c"] for r in renders}
    incomplete = by_render.get("pending", 0) + by_render.get("incomplete", 0)
    return _result("ok", scope, {
        "total_parts": sum(by_state.values()),
        "by_state": by_state,
        "renders_complete": by_render.get("complete", 0),
        "renders_incomplete": incomplete,
        "notes": ["complete requires every planned part delivered — "
                  "a settled card alone never counts",
                  "unavailable attachments are pre-settled not_sent "
                  "parts: visible in by_state, never silently omitted"]})


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
    from semantic_evaluation import wilson_interval
    ratio = _ratio(num, den, unit)
    ratio["wilson95"] = {"low": None, "high": None}
    if den < SIGNAL_FEEDBACK_MIN_N:
        ratio.update(value=None, reason="insufficient_n")
    else:
        ratio["wilson95"] = wilson_interval(num, den)
    return ratio


def st_signal_feedback(db, scope):
    """ST-T2: local signal lifecycle measurement, never clinical completion."""
    from mcs_signals import (ARTIFACT_KIND, DETECTORS, FEEDBACK_KIND,
                             _signal_content, dismiss_reason_counts,
                             signal_message_ids)
    types = {name for name, _ in DETECTORS}
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
        c = _signal_content(content_s, pid)
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

    selected = [ep for ep in episodes
                if (scope["since"] is None or ep["start"] >= scope["since"])
                and (scope["until"] is None or ep["start"] < scope["until"])]
    requests = db.execute(
        "SELECT project_id,source_message_id,created_at FROM requests "
        "WHERE created_at<=?", (scope["as_of"],)).fetchall()
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
    shown = []
    for mid, shown_s, delivered in manifests:
        try:
            keys = json.loads(shown_s)
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(keys, list):
            shown.append((mid, {k for k in keys if isinstance(k, str)}, delivered))
    for ep in selected:
        for pid, source, created in requests:
            if pid == ep["pid"] and any(
                    source in segment["mids"]
                    and segment["start"] <= created <= segment["end"]
                    for segment in ep["segments"]):
                ep["adoption_times"].append(created)
        for mid, keys, delivered in shown:
            if ep["key"] in keys and ep["start"] <= delivered <= ep["end"]:
                ep["shown_times"].append(delivered)
                ep["ack_times"].extend(
                    at for at in acks.get(mid, [])
                    if delivered <= at <= ep["end"])
        ep["action_times"].extend(ep["ack_times"] + ep["adoption_times"])

    dismissals = dismiss_reason_counts(
        db, scope["project_id"], since=scope["since"], until=scope["until"],
        as_of=scope["as_of"])
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
                or last < at or last > scope["as_of"]
                or (scope["since"] is not None and at < scope["since"] <= last)
                or (scope["until"] is not None and last >= scope["until"])):
            straddled[c["type"]] = straddled.get(c["type"], 0) + 1
            continue
        suppression[c["type"]][0] += den
        suppression[c["type"]][1] += num
        suppression_since[c["type"]] = min(at, suppression_since.get(c["type"], at))

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
    return _result("partial" if invalid or straddled else "ok", scope, {
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


def st_interaction_latency(db, scope, *, profession_map=None, privacy_policy=None):
    """Structural first thread-reply latency; owner policy gates all role cells.

    Policy requires min_pairs, min_actors (per endpoint) and min_projects, each
    >=2. These are owner inputs, not claimed privacy guarantees. Map destinations
    are fixed role categories, never caller-supplied free text. Multiple recorded
    professions form one sorted role-set cell, without duplication or weighting.
    """
    roles = {"doctor", "nurse", "pharmacist", "care_manager", "care_worker",
             "family", "other"}
    mapping = {"医師": "doctor", "看護師": "nurse", "薬剤師": "pharmacist",
               "ケアマネ": "care_manager", "介護士": "care_worker", "家族": "family"}
    if profession_map is not None:
        if (not isinstance(profession_map, dict)
                or any(not isinstance(k, str) or not k or not isinstance(v, str) or v not in roles
                       for k, v in profession_map.items())):
            raise ValueError("bad_profession_map")
        mapping = profession_map
    if privacy_policy is not None:
        keys = {"min_pairs", "min_actors", "min_projects"}
        if (not isinstance(privacy_policy, dict) or set(privacy_policy) != keys
                or any(type(v) is not int or v < 2 for v in privacy_policy.values())):
            raise ValueError("bad_interaction_privacy_policy")

    def timed(ts):
        return type(ts) is int and 0 < ts <= 253402300799

    roster = {}
    sources = {"message_profession": 0, "care_team": 0, "unknown": 0}

    def role(pid, actor, actor_type, raw, ts):
        if type(actor) is not int or actor <= 0:
            sources["unknown"] += 1
            return None
        names = raw.split(", ") if isinstance(raw, str) and raw else None
        source = "message_profession"
        if names is None:
            if pid not in roster:
                roster[pid] = get_project_metadata(db, pid, "care_team", now=scope["as_of"])
            metadata = roster[pid]
            matches = [row for row in metadata["rows"] if row["id"] == actor]
            # Current roster is not evidence of past employment or past role.
            if (metadata["current_known"] and timed(ts)
                    and metadata["last_complete_at"] <= ts and len(matches) == 1
                    and actor_type and matches[0]["type"] == actor_type):
                names = matches[0]["professions"]
                source = "care_team"
        if (not names or any(not isinstance(n, str) or n not in mapping for n in names)):
            sources["unknown"] += 1
            return None
        sources[source] += 1
        return tuple(sorted({mapping[n] for n in names}))

    rows = thread_reply_pairs(db, since=scope["since"], until=scope["until"],
                              as_of=scope["as_of"], project_id=scope["project_id"])
    counts = dict.fromkeys(("roots", "unplaced_root_time", "incomplete_source",
                            "no_observed_reply", "invalid_reply_time",
                            "negative_latency", "valid_time_pairs", "unknown_role_pairs"), 0)
    groups = {}
    for row in rows:
        counts["roots"] += 1
        if not timed(row["root_ts"]):
            counts["unplaced_root_time"] += 1
            continue
        if (row["root_state"] != "full" or type(row["reply_count"]) is not int
                or row["reply_count"] < 0
                or row["reply_count"] > row["stored_full_replies"]):
            counts["incomplete_source"] += 1
            continue
        if row["reply_id"] is None:
            counts["no_observed_reply"] += 1
            continue
        if not timed(row["reply_ts"]):
            counts["invalid_reply_time"] += 1
            continue
        latency = row["reply_ts"] - row["root_ts"]
        if latency < 0:
            counts["negative_latency"] += 1
            continue
        counts["valid_time_pairs"] += 1
        start = role(row["project_id"], row["root_actor"], row["root_actor_type"],
                     row["root_profession"], row["root_ts"])
        end = role(row["project_id"], row["reply_actor"], row["reply_actor_type"],
                   row["reply_profession"], row["reply_ts"])
        if start is None or end is None:
            counts["unknown_role_pairs"] += 1
            continue
        group = groups.setdefault((start, end), {"times": [], "roots": set(),
                                               "replies": set(), "projects": set()})
        group["times"].append(latency)
        group["roots"].add(row["root_actor"])
        group["replies"].add(row["reply_actor"])
        group["projects"].add(row["project_id"])
    cells = []
    suppressed = False
    for (start, end), group in sorted(groups.items()):
        if (privacy_policy is None
                or len(group["times"]) < privacy_policy["min_pairs"]
                or min(len(group["roots"]), len(group["replies"])) < privacy_policy["min_actors"]
                or len(group["projects"]) < privacy_policy["min_projects"]):
            suppressed = True
            continue
        times = sorted(group["times"])
        cells.append({"from_roles": list(start), "to_roles": list(end), "n": len(times),
                      "median_s": statistics.median(times),
                      "p90_s": times[math.ceil(len(times) * .9) - 1]})
    # No suppressed cell labels/counts or grand-total latency: avoid subtraction.
    # valid - unknown - released n (or 2*valid - role_sources) would expose the
    # suppressed count, so both are withheld whenever a cell can be suppressed.
    # valid itself is roots minus the other exclusions, so the residual
    # valid - released n (= unknown + suppressed) gets complementary
    # suppression: below min_pairs, every role cell is withheld.
    # ponytail: residual is checked on pair count only, not actors/projects.
    if privacy_policy is None or suppressed:
        counts["unknown_role_pairs"] = None
        sources = None
    residual = counts["valid_time_pairs"] - sum(c["n"] for c in cells)
    if cells and suppressed and residual < privacy_policy["min_pairs"]:
        cells = []
    result = _result("partial", scope, {
        "coverage": counts, "role_sources": sources,
        "valid_time_share": _ratio(counts["valid_time_pairs"], counts["roots"], "roots"),
        "role_cells": cells,
        "role_cells_reason": ("owner_policy_required" if privacy_policy is None else
                              "small_cell" if suppressed else None),
        "privacy_policy": privacy_policy,
        "definitions": {
            "cohort": "root posting time in [since,until); replies through as_of",
            "latency": "first stored full thread reply posting time minus root posting time",
            "multiple_roles": "one role-set pair per root, no weighting or duplication",
            "care_team": "fresh complete roster, exact actor ID, acquired no later than post",
            "coverage": "unplaceable roots included separately; incomplete sources excluded"},
        "notes": ["Posting time is not care action time or clinical completion.",
                  "No observed reply does not mean no care or no response.",
                  "History completeness is not verified; first observed reply may not be first actual reply.",
                  "Do not use for individual performance evaluation; no automatic export."]})
    result["scope"].pop("project_id")
    result["scope"]["project_filter_applied"] = scope["project_id"] is not None
    return result


REGISTRY = {
    # name -> {tier, needs, fn}
    "overview": {"tier": "T1", "needs": ["metadata"], "fn": st_overview},
    "data_quality": {"tier": "T0", "needs": ["metadata"],
                     "fn": st_data_quality},
    "patient_activity": {"tier": "T1", "needs": ["metadata"],
                         "fn": st_patient_activity},
    "professions": {"tier": "T1", "needs": ["metadata", "profession_map"],
                    "fn": st_professions},
    "workload": {"tier": "T1", "needs": ["metadata"], "fn": st_workload},
    "doc_burden": {"tier": "T1", "needs": ["metadata"], "fn": st_doc_burden},
    "meds": {"tier": "T1", "needs": ["med_events", "drug_map"],
             "fn": st_meds},
    "med_mentions": {"tier": "T1", "needs": ["med_events", "drug_map"],
                     "fn": st_med_mentions},
    "med_change_burden": {"tier": "T1", "needs": ["med_events"],
                          "fn": st_med_change_burden},
    "adherence_events": {"tier": "T1", "needs": ["valid_facts"],
                         "fn": st_adherence_events},
    "rx_expiry": {"tier": "T1",
                  "needs": ["med_periods (extract_v1 surface forms)"],
                  "fn": st_rx_expiry},
    "open_loop_aging": {"tier": "T2",
                        "needs": ["interaction_links", "episode_links"],
                        "fn": st_open_loop_aging},
    "med_change_followup": {"tier": "T2", "needs": ["med_events"],
                            "fn": st_med_change_followup},
    "transition_reconciliation": {"tier": "T2",
                                  "needs": ["med_events"],
                                  "fn": st_transition_reconciliation},
    "canonical_facts": {"tier": "T2",
                        "needs": ["canonical_projection"],
                        "fn": st_canonical_facts},
    "card_parts": {"tier": "T2",
                   "needs": ["notification_render_parts"],
                   "fn": st_card_parts},
    "signal_feedback": {"tier": "T2", "needs": ["signal_v1", "notification_manifests"],
                        "fn": st_signal_feedback},
    "interaction_latency": {"tier": "T2",
                            "needs": ["thread_reply", "project_metadata_v1 care_team",
                                      "profession_map", "owner_privacy_policy"],
                            "fn": st_interaction_latency},
}

PRESETS = {
    "operational": ["data_quality", "patient_activity",
                    "med_change_burden", "open_loop_aging",
                    "med_change_followup", "rx_expiry",
                    "transition_reconciliation"],
    "pharmacy": ["meds", "med_mentions", "adherence_events"],
}


def run_stats(db, snapshot_ts: int, args: dict) -> dict:
    """Dispatch --stat / --preset / --list over one snapshot generation."""
    names = []
    if args.get("list"):
        return {"registry": [
            {"name": n, "tier": d["tier"], "needs": d["needs"]}
            for n, d in REGISTRY.items()]}
    if args.get("stat"):
        if args["stat"] not in REGISTRY:
            raise ValueError("unknown_stat")
        names = [args["stat"]]
    elif args.get("preset"):
        if args["preset"] not in PRESETS:
            raise ValueError("unknown_preset")
        names = PRESETS[args["preset"]]
    else:
        raise ValueError("stat_or_preset_required")
    scope = _scope(args, snapshot_ts)
    stats = {}
    for name in names:
        try:
            if name == "interaction_latency":
                stats[name] = st_interaction_latency(
                    db, scope, profession_map=args.get("profession_map"),
                    privacy_policy=args.get("interaction_privacy_policy"))
            else:
                stats[name] = REGISTRY[name]["fn"](db, scope)
        except (json.JSONDecodeError, RecursionError, TypeError, KeyError,
                AttributeError, OverflowError, sqlite3.Error) as e:
            stats[name] = _result("unavailable", scope, {},
                                  reason=f"data_error:{type(e).__name__}")
    return {"stats": stats}
