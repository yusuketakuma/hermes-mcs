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
from datetime import datetime, timedelta

from mcs_queries import (CHANGE_ACTIONS, DAY_S, JST, MED_ACTIONS,
                         current_extract_pred, current_fact_pred,
                         iter_period_ends,
                         med_is_patient_current, med_period_artifacts,
                         transition_cooccurrences)
DEFINITION_VERSION = "2026-09-21"
STATS_SCHEMA = "stats_v1"

# engineering caps (A-8): detail 20 default / 100 max, top categories
# 100 max, time buckets 120 — row-count caps bound the response size
DETAIL_LIMIT = 100
CATEGORY_LIMIT = 100
BUCKET_LIMIT = 120

STATUSES = ("ok", "partial", "unavailable", "unsupported", "not_implemented")


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
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            raise ValueError("bad_time_arg")
        return int(dt.timestamp())
    except (ValueError, OverflowError):
        raise ValueError("bad_time_arg")


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
    except (TypeError, ValueError):
        raise ValueError("bad_limit")
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
    # parsed = a hash-current extract_llm artifact exists (any schema
    # version — v1 rows stay readable until lazily replaced by v2)
    parsed = db.execute(
        f"""SELECT COUNT(*) FROM messages m WHERE 1=1{w}
            AND EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind='extract_llm'
                          AND a.message_id=m.message_id
                          {current_extract_pred(content=False)})""",
        p).fetchone()[0]
    stale = db.execute(
        f"""SELECT COUNT(*) FROM messages m WHERE 1=1{w}
            AND EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind='extract_llm' AND a.message_id=m.message_id
                          AND json_valid(a.meta)
                          AND json_extract(a.meta,'$.hash')!=m.content_hash)""",
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
    return _result("ok", scope, {
        "stages": {
            "fetched": _ratio(fetched, total, "messages"),
            "parsed_current_revision": _ratio(parsed, total, "messages"),
            "stat_ready_timestamped": _ratio(timed, total, "messages")},
        "body_states": {s: n for s, n in body_states},
        "stale_parsed": stale,
        "extract_meta_unparseable": meta_bad,
        "notes": ["body_absent_vs_unparsed kept separate",
                  "stale_parsed = extraction exists but for an older "
                  "content revision"]})


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
        "rooms_by_state": {s: n for s, n in state_rows},
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
        dt = datetime.fromtimestamp(ts, JST)
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
                   MAX(m.sender_name IS NULL) nameless
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
        "unknown_sender_posts": sum(n for _, n, nameless in rows
                                    if nameless),
        "notes": ["post count is not a performance score; "
                  "delegate posting and division of labour skew it",
                  "grouped by sender_id — distinct senders sharing a "
                  "display name stay separate"]})


# ---------------- meds (extract_llm source) ----------------

def _med_rows(db, scope):
    """(project_id, message_id, med_dict, posted_at_ts) for current-
    revision extract_llm artifacts in scope. artifacts has no
    UNIQUE(kind, message_id), so duplicate current-hash rows are
    deduplicated per message_id here rather than double-counted."""
    w, p = _where(scope)
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, a.content, m.posted_at_ts
            FROM artifacts a JOIN messages m ON m.message_id=a.message_id
            WHERE a.kind IN ('extract_llm','canonical_projection')
              {current_fact_pred()}
              AND json_array_length(a.content,'$.meds')>0{w}
            ORDER BY a.artifact_id""",
        p).fetchall()
    seen = set()
    for pid, mid, content, ts in rows:
        if mid in seen:
            continue
        seen.add(mid)
        for med in (json.loads(content).get("meds") or []):
            # negated / other-person / historical-report mentions are
            # not the patient's current medication activity — the same
            # predicate the prospective signal applies
            if not med_is_patient_current(med):
                continue
            yield pid, mid, med, ts


def st_meds(db, scope):
    """ST-007: med actions by raw name x month. Names are NOT normalized
    to ingredients (drug_map unavailable) — noted, not hidden."""
    per_month = {}
    action_counts = {}
    names = set()
    for pid, mid, med, ts in _med_rows(db, scope):
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
        "notes": ["names are raw surface forms, NOT ingredient-"
                  "normalized (drug_map not implemented)",
                  "mention counts, not deduplicated change events",
                  "'none' = mentioned without a change action"]})


def st_med_mentions(db, scope):
    """ST-008: distinct med names per room in scope."""
    per_room = {}
    for pid, mid, med, ts in _med_rows(db, scope):
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
    for pid, mid, med, ts in _med_rows(db, scope):
        if med.get("action") not in CHANGE_ACTIONS:
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
    today = datetime.fromtimestamp(scope["as_of"], JST).date()
    horizon = today + timedelta(days=14)
    per_room = {}
    items = []
    seen = set()
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
    w, p = _where(scope)
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts, a.content
            FROM artifacts a JOIN messages m ON m.message_id=a.message_id
            WHERE a.kind IN ('extract_llm','canonical_projection')
              {current_fact_pred()}
              AND json_array_length(a.content,'$.meds')>0
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts <= ?{w}""",
        [cutoff, *p]).fetchall()
    seen, total, no_follow = set(), 0, []
    for pid, mid, ts, content in rows:
        if mid in seen:
            continue
        seen.add(mid)
        meds = json.loads(content).get("meds") or []
        # CHANGE_ACTIONS only — a "none" (no-change) mention is not a
        # change; negated/other-person/historical mentions are filtered
        # by the same predicate the signal detector uses
        if not any(med_is_patient_current(x)
                   and x.get("action") in CHANGE_ACTIONS
                   for x in meds):
            continue
        total += 1
        tracked = db.execute(
            "SELECT 1 FROM requests WHERE source_message_id=? LIMIT 1",
            (mid,)).fetchone()
        follow = db.execute(
            "SELECT COUNT(*) FROM messages WHERE project_id=? "
            "AND posted_at_ts > ? AND posted_at_ts <= ?",
            (pid, ts, ts + 7 * DAY_S)).fetchone()[0]
        if not tracked and follow == 0:
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
        "WHERE status IN ('open','in_progress')" +
        (" AND project_id = ?" if scope["project_id"] is not None else ""),
        ([scope["project_id"]] if scope["project_id"] is not None else [])
    ).fetchall()
    buckets = {"not_yet_due": 0, "0-7d": 0, "8-30d": 0, "31-90d": 0,
               "over_90d": 0, "no_due": 0}
    items = []
    for rid, pid, status, due in rows:
        age_d = None
        unparseable = False
        if due:
            try:
                age_d = (scope["as_of"] - _parse_when(str(due))) / DAY_S
            except (ValueError, TypeError, OverflowError):
                unparseable = True
        if due is None:
            buckets["no_due"] += 1
        elif unparseable:
            buckets["no_due"] += 1  # counted but flagged, not hidden
        elif age_d < 0:
            buckets["not_yet_due"] += 1
        else:
            buckets["over_90d" if age_d > 90 else
                    "31-90d" if age_d > 30 else
                    "8-30d" if age_d > 7 else "0-7d"] += 1
        items.append({"request_id": rid, "project_id": pid,
                      "status": status, "due_date": due,
                      "due_unparseable": unparseable or None,
                      "days_since_due": round(age_d) if age_d is not None
                      else None})
    items.sort(key=lambda r: (r["days_since_due"] is None,
                              -(r["days_since_due"] or 0)))
    return _result("partial", scope, {
        "formal_open_requests": _items(items, scope["limit"]),
        "age_buckets": buckets,
        "oldest_open_due": min((i["due_date"] for i in items
                                if i["due_date"]), default=None),
        "text_candidates": {"status": "unavailable",
                            "reason": "needs interaction_links"},
        "notes": ["overdue age measured from due date, not creation",
                  "--since/--until do not filter the request register"],
        },
        reason="text-derived open-loop candidates not computable; "
               "formal request register only")


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
            stats[name] = REGISTRY[name]["fn"](db, scope)
        except (json.JSONDecodeError, TypeError, KeyError,
                AttributeError, OverflowError, sqlite3.Error) as e:
            stats[name] = _result("unavailable", scope, {},
                                  reason=f"data_error:{type(e).__name__}")
    return {"stats": stats}
