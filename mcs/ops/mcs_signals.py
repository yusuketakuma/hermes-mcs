"""Prospective review-candidate signals — MCS-STAT-PROSPECTIVE T2.

Runs inside the main check pipeline (stage_derive) against the live
ledger. Candidates persist as append-only signal_v1 artifacts — one row
per lifecycle transition, keyed by meta.key; the current state of a
signal is its LATEST row (same pattern as semantic generations and
loop_candidate/loop_event). Notifications reach notify_outbox only when
config `signals.notify` is true — never by default, and queued intents
are suppressed at send time if the flag has since been turned off.

Honesty rules carried from the spec:
- A signal says "review may be warranted" — never that care was missed.
  Absence of a follow-up record is reported as exactly that.
- Thresholds are fixed constants unless a human approves an override
  via the ops.signal_policy command (signal_policy_v1 artifacts,
  latest wins, receipted). Viewing statistics never feeds back into
  detection (no adaptive tuning).
- evidence = stable identities (message/request ids, raw surface forms);
  context = volatile counters measured at detection time. note is
  frozen at detection so it can never disagree with its own row.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime

from mcs_queries import (CHANGE_ACTIONS, CHANGE_ACTIONS_SQL, DAY_S,
                         FACT_KINDS_SQL, ITEM_CONFIRMED_SQL, JST,
                         JSON_OBJECT_SQL, MED_NOT_CAPABILITY_SQL, MED_PATIENT_CURRENT_SQL,
                         TRANSITION_EVENTS_SQL, current_fact_pred,
                         iter_period_ends, json_or_null,
                         med_capability_evidence,
                         med_is_patient_current, med_period_artifacts,
                         item_unverified, transition_cooccurrences)
from structured_view import message_urgency

ARTIFACT_KIND = "signal_v1"
FEEDBACK_KIND = "signal_feedback_v1"

FOLLOWUP_DAYS = 7          # med_change_no_followup window
FOLLOWUP_MAX_AGE_D = 90    # only mentions within this horizon — older
                           # ones are historical, not prospective
CONC_WINDOW_H = 72         # comm_concentration window
CONC_MIN_POSTS = 10        # comm_concentration threshold
EXPIRY_AHEAD_DAYS = 3      # rx_period_expiry horizon
RX_LAPSED_MAX_D = 14       # rx_period_lapsed: how many days past a
                           # period end the "no renewal mention" alert
                           # stays live — past that the silence is a
                           # record style, not a lapsed prescription
REQ_AGE_DAYS = 30          # request_aging: open register items older than this
REQ_RESPONSE_DAYS = 3      # pharmacist_request_unanswered response window
FYI_MAX_AGE_D = 30         # horizon for FYI-type signals (request
                           # visibility / adherence / symptom coupling)
NOTIFY_COOLDOWN_S = 7 * DAY_S  # no second notify for the same key inside this
TRANSITION_LOOKBACK_D = 60   # 退院 mentions within the last N days
TRANSITION_MED_WINDOW_D = 14 # med change mentions within ±N days of it

# Human-approved threshold policy: ops.signal_policy writes
# signal_policy_v1 artifacts; the latest one overrides these defaults.
# Statistics viewing has no path into this — detection thresholds move
# only via an explicit human-confirmed command (receipted).
POLICY_KIND = "signal_policy_v1"
#            name                default               low  high
THRESHOLDS = {
    "followup_days":            (FOLLOWUP_DAYS,            1,  90),
    "followup_max_age_d":       (FOLLOWUP_MAX_AGE_D,       7, 365),
    "conc_window_h":            (CONC_WINDOW_H,            6, 336),
    "conc_min_posts":           (CONC_MIN_POSTS,           2, 200),
    "expiry_ahead_days":        (EXPIRY_AHEAD_DAYS,        1,  90),
    "rx_lapsed_days":           (RX_LAPSED_MAX_D,          1,  60),
    "req_age_days":             (REQ_AGE_DAYS,             7, 365),
    "transition_lookback_d":    (TRANSITION_LOOKBACK_D,    7, 365),
    "transition_med_window_d":  (TRANSITION_MED_WINDOW_D,  1,  60),
    "notify_cooldown_d":        (NOTIFY_COOLDOWN_S // DAY_S, 1, 90),
    "request_response_days":    (REQ_RESPONSE_DAYS,        1,  30),
    "fyi_max_age_d":            (FYI_MAX_AGE_D,            7, 180),
}


def _approved_policy(db):
    """Latest signal_policy_v1 artifact as (artifact_id, content), or
    None when absent or lacking provenance. Provenance required: a policy
    only counts when it arrived through the human-confirmed command path
    (non-empty str command_id + actor recorded by _apply_signal_policy_tx).
    A bare artifact insert cannot move thresholds — same identity
    boundary as the command envelope."""
    row = db.execute(
        "SELECT artifact_id, content FROM artifacts WHERE kind=? AND "
        "json_valid(content) ORDER BY artifact_id DESC LIMIT 1",
        (POLICY_KIND,)).fetchone()
    if row is None:
        return None
    content = json.loads(row["content"])
    if not (isinstance(content, dict)
            and isinstance(content.get("command_id"), str)
            and content["command_id"]
            and isinstance(content.get("actor"), str)
            and content["actor"]):
        return None
    return row["artifact_id"], content


def _thresholds(db):
    """Resolved thresholds: defaults overlaid by the latest approved
    signal_policy_v1 artifact. Malformed/out-of-range values in the
    artifact are ignored per key rather than failing the whole run."""
    th = {name: default for name, (default, lo, hi)
          in THRESHOLDS.items()}
    approved = _approved_policy(db)
    if approved is None:
        return th
    policy = approved[1].get("policy")
    if isinstance(policy, dict):
        for name, (_default, lo, hi) in THRESHOLDS.items():
            v = policy.get(name)
            if type(v) is int and lo <= v <= hi:
                th[name] = v
    return th


def _key(type_, pid, anchor):
    return f"{type_}:{pid}:{anchor}"


SELF_PROFILE_KIND = "self_profile_v1"


def normalize_sender_id(value) -> int | None:
    """A positive signed-64-bit sender ID, or None for unknown values."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value.strip()):
        value = int(value)
    return value if type(value) is int and 0 < value < 2**63 else None


def _latest_self_profile(db):
    """Most recent self_profile artifact (written from the MCS
    /users/self response by record_self_profile) — the fetched default
    for self identity. Returns {} when absent or unparsable."""
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind=? "
        "AND json_valid(content) ORDER BY artifact_id DESC LIMIT 1",
        (SELF_PROFILE_KIND,)).fetchone()
    try:
        d = json.loads(row["content"]) if row else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


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
    cur = _latest_self_profile(db)
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
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind=? "
        "AND json_valid(content) ORDER BY artifact_id DESC LIMIT 1",
        (STATION_STAFF_KIND,)).fetchone()
    try:
        d = json.loads(row["content"]) if row else {}
    except (json.JSONDecodeError, TypeError):
        return []
    staff = d.get("staff") if isinstance(d, dict) else None
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
    prof = _latest_self_profile(db)
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


def is_own_station_message(db, sender_id) -> bool | None:
    """Match roster IDs only; absent sender or roster IDs remain unknown."""
    sid = normalize_sender_id(sender_id)
    own = own_sender_ids(db)
    return sid in own if sid is not None and own else None


def self_sender_id(db) -> int | None:
    """Resolve the logged-in user from the roster, then the self profile."""
    staff = latest_station_staff(db)
    if any(s.get("is_self") is True for s in staff):
        # Ambiguous/invalid roster identities fail closed, even if the
        # profile has an ID. Display names never resolve an identity.
        return _station_self_id(staff)
    return normalize_sender_id(_latest_self_profile(db).get("sender_id"))


def is_self_message(db, sender_id) -> bool:
    """Whether a known sender ID belongs to the logged-in MCS user."""
    sid = normalize_sender_id(sender_id)
    return sid is not None and sid == self_sender_id(db)


def _self_sets(sig_cfg, db=None):
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
        art = _latest_self_profile(db)
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


def _med_excludes(sig_cfg):
    """signals.med_exclude_names: med surface forms never treated as
    change candidates (e.g. 在宅酸素 — a therapy, not a dispensed
    drug). Exact match after whitespace normalization."""
    sc = sig_cfg if isinstance(sig_cfg, dict) else {}
    v = sc.get("med_exclude_names")
    if not isinstance(v, list):
        return set()
    return {re.sub(r"\s+", "", x) for x in v
            if isinstance(x, str) and x.strip()}


def _self_author_pred(organizations, alias="m"):
    """SQL fragment + params excluding mentions authored by configured
    own organizations — self-authored records aren't review candidates
    FOR us. Empty config -> no exclusion."""
    if not organizations:
        return "", []
    ph = ",".join("?" * len(organizations))
    return (f" AND COALESCE({alias}.organization,'') NOT IN ({ph})",
            list(organizations))


def _self_post_exists(db, pid, ts, professions, organizations):
    """A post authored by a responder identity (pharmacist profession
    or a configured own-org) exists in the room after ts — visible
    engagement on the record. No identity configured -> never counts."""
    pred, params = [], []
    if professions:
        pred.append("profession IN ("
                    + ",".join("?" * len(professions)) + ")")
        params += list(professions)
    if organizations:
        pred.append("organization IN ("
                    + ",".join("?" * len(organizations)) + ")")
        params += list(organizations)
    if not pred:
        return False
    return db.execute(
        f"SELECT 1 FROM messages WHERE project_id=? AND posted_at_ts>?"
        " AND body_state IS NOT 'deleted'"
        f" AND ({' OR '.join(pred)}) LIMIT 1",
        (pid, ts, *params)).fetchone() is not None


def _request_registered(db, mid):
    """A registered request on the mention message is visible
    engagement — someone already turned it into a tracked item."""
    return db.execute(
        "SELECT 1 FROM requests WHERE source_message_id=? LIMIT 1",
        (mid,)).fetchone() is not None


SELF_RESPONSE_REACTIONS = ("accepted", "completed")


def _self_reaction_response(db, mid):
    """The logged-in user's 承知/完了 stamp observed on the request post
    itself (capture only). Other stamps, other people's stamps and
    unfetched or invalid metadata never count."""
    from message_metadata import get_message_metadata, get_metadata_shadow_status
    meta = get_message_metadata(db, mid)
    shadow = get_metadata_shadow_status(db, mid)
    if meta["last_error"] or (shadow["last_error"] and (
            shadow["checked_at"] or 0) >= (meta["checked_at"] or 0)):
        return False
    reactions = meta["reactions"] or ()
    return any(r["self_reacted"] and r["type"] in SELF_RESPONSE_REACTIONS
               for r in reactions)


def _med_followup_note(meds, days):
    """med_change_no_followup note for one or several meds sharing a
    post — 「A」「B」 juxtaposition keeps the single-med wording intact."""
    names = "".join(f"「{m}」" for m in meds)
    return (f"薬{names}の変更言及後{days}日以内の後続記録を確認できません"
            "でした（記録上の確認であり、対応の有無を示すものではありません）")


def _med_followup(db, now, th, sig_cfg):
    """Per (room, med surface form) episodes: flag when the LATEST
    change-action mention of a med in a non-archived room has passed the
    follow-up window with no later room post and no registered request
    on it — 'no follow-up record could be confirmed', not 'no follow-up
    happened'. Mentions beyond followup_max_age_d are historical, and a
    later mention that DID get a response suppresses the episode (the
    med is visibly being tracked).

    Episode-level keys keep one candidate per drug per room instead of
    one per message — repeated mentions of the same med were stacking
    into duplicate signals. The scan is deliberately unbounded over
    artifact history: the qualifying condition is time-dependent, and
    trickle imports can land old posts inside any past window.

    Exclusions beyond the shared patient-current predicate: mentions
    authored by configured self_organizations (our own reports are not
    review candidates for us), capability-evidence spans like
    「〜は出来ない」 (those are adherence_concern, not changes), and
    configured med_exclude_names (non-dispensed therapies)."""
    orgs, _, _ = _self_sets(sig_cfg, db)
    self_pred, self_params = _self_author_pred(orgs)
    excludes = _med_excludes(sig_cfg)
    rows = db.execute(
        f"""WITH med_msgs AS MATERIALIZED (
                SELECT m.project_id AS pid, m.message_id AS mid,
                       m.posted_at_ts AS ts,
                       TRIM(json_extract({JSON_OBJECT_SQL},'$.name')) AS med
                FROM artifacts a
                JOIN messages m ON m.message_id=a.message_id
                JOIN patients p ON p.project_id=m.project_id
                JOIN json_each({json_or_null('a.content')},'$.meds') je
                WHERE a.kind IN ({FACT_KINDS_SQL})
                  {current_fact_pred()}
                  AND m.posted_at_ts IS NOT NULL
                  AND m.posted_at_ts >= ?
                  AND COALESCE(p.is_archived,0)=0
                  {self_pred}
                  AND json_extract({JSON_OBJECT_SQL},'$.action')
                      IN ({CHANGE_ACTIONS_SQL})
                  AND json_type({JSON_OBJECT_SQL},'$.name')='text'
                  AND TRIM(json_extract({JSON_OBJECT_SQL},'$.name'))!=''
                  -- a negated / other-person / historical-report med
                  -- mention is not a change needing follow-up
                  AND {MED_PATIENT_CURRENT_SQL}
                  -- 「〜は出来ない」 capability spans are not changes
                  AND {MED_NOT_CAPABILITY_SQL}),
            latest AS (
                SELECT pid, med, MAX(ts) AS lts FROM med_msgs
                GROUP BY pid, med)
            SELECT DISTINCT mm.pid, mm.med, mm.mid
            FROM med_msgs mm
            JOIN latest l ON l.pid=mm.pid AND l.med=mm.med
            JOIN med_msgs lm ON lm.pid=l.pid AND lm.med=l.med
                            AND lm.ts=l.lts
            -- evidence rows: window elapsed, no visible follow-up
            WHERE mm.ts <= ?
              AND NOT EXISTS (SELECT 1 FROM requests r
                              WHERE r.source_message_id=mm.mid)
              AND NOT EXISTS (SELECT 1 FROM messages m2
                              WHERE m2.project_id=mm.pid
                                AND m2.posted_at_ts > mm.ts
                                AND m2.posted_at_ts <= mm.ts + ?)
              -- gate: the LATEST mention of this med must itself have
              -- passed the window with no visible follow-up
              AND lm.ts <= ?
              AND NOT EXISTS (SELECT 1 FROM requests r2
                              WHERE r2.source_message_id=lm.mid)
              AND NOT EXISTS (SELECT 1 FROM messages m3
                              WHERE m3.project_id=lm.pid
                                AND m3.posted_at_ts > lm.ts
                                AND m3.posted_at_ts <= lm.ts + ?)
            ORDER BY mm.pid, mm.med, mm.mid""",
        (now - th["followup_max_age_d"] * DAY_S, *self_params,
         now - th["followup_days"] * DAY_S,
         th["followup_days"] * DAY_S,
         now - th["followup_days"] * DAY_S,
         th["followup_days"] * DAY_S)).fetchall()
    episodes = {}
    for pid, med, mid in rows:
        if re.sub(r"\s+", "", med) in excludes:
            continue                 # configured non-dispensed name
        key = (pid, " ".join(med.split()))
        episodes.setdefault(key, []).append(mid)
    for (pid, med), mids in episodes.items():
        yield _key("med_change_no_followup", pid, med), {
            "type": "med_change_no_followup", "project_id": pid,
            "evidence": {"med": med, "message_ids": mids[:10],
                         "mention_count": len(mids)},
            "context": {"window_days": th["followup_days"]},
            "note": _med_followup_note([med], th["followup_days"])}


def _comm_concentration(db, now, th, sig_cfg):
    """Non-archived rooms whose post count in the last 72h exceeds a
    fixed threshold. Volume is not severity."""
    rows = db.execute(
        """SELECT m.project_id, COUNT(*) FROM messages m
           JOIN patients p ON p.project_id=m.project_id
           WHERE m.posted_at_ts >= ? AND m.posted_at_ts < ?
             AND COALESCE(p.is_archived,0)=0
           GROUP BY m.project_id HAVING COUNT(*) >= ?""",
        (now - th["conc_window_h"] * 3600, now,
         th["conc_min_posts"])).fetchall()
    for pid, n in rows:
        yield _key("comm_concentration", pid, "current"), {
            "type": "comm_concentration", "project_id": pid,
            "evidence": {"project_id": pid},
            "context": {"posts": n, "window_hours": th["conc_window_h"],
                        "threshold": th["conc_min_posts"]},
            "note": f"直近{th['conc_window_h']}時間の記録が{n}件と集中して"
                    "います（件数の集中であり重症度ではありません）"}


def _request_overdue(db, now, th, sig_cfg):
    """Formal register fact: open/in_progress requests past due_date."""
    rows = db.execute(
        "SELECT request_id, project_id, due_date FROM requests "
        "WHERE status IN ('open','in_progress') AND due_date IS NOT NULL"
    ).fetchall()
    today = datetime.fromtimestamp(now, JST).date()
    for rid, pid, due in rows:
        try:
            due_d = datetime.strptime(str(due), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue             # unparseable due can't ground a signal
        days = (today - due_d).days
        if days <= 0:
            continue
        yield _key("request_overdue", pid, rid), {
            "type": "request_overdue", "project_id": pid,
            "evidence": {"request_id": rid, "due_date": str(due)},
            "context": {"days_overdue": days},
            "note": f"期限日を{days}日過ぎた未完了の依頼登録があります"
                    "（依頼登録の状態であり、対応の実施有無は原記録の"
                    "確認が必要です）"}


def _request_aging(db, now, th, sig_cfg):
    """Open register items whose created_at is older than the aging
    threshold — regardless of due_date (register fact only)."""
    rows = db.execute(
        "SELECT request_id, project_id, created_at FROM requests "
        "WHERE status IN ('open','in_progress') AND created_at <= ?",
        (now - th["req_age_days"] * DAY_S,)).fetchall()
    for rid, pid, created in rows:
        days = int((now - created) / DAY_S)
        yield _key("request_aging", pid, rid), {
            "type": "request_aging", "project_id": pid,
            "evidence": {"request_id": rid},
            "context": {"days_since_created": days},
            "note": f"登録から{days}日経過した未完了の依頼登録がありま"
                    "す（登録上の状態です）"}


def _archived_pids(db):
    """Archived rooms — med_period_artifacts (shared with mcs_stats) does
    not join patients, so the rx_period detectors exclude them here."""
    return {r[0] for r in db.execute(
        "SELECT project_id FROM patients WHERE COALESCE(is_archived,0)=1")}


def _rx_period_expiry(db, now, th, sig_cfg):
    """extract_v1 med_periods whose end date lands within the horizon.
    These are parsed surface expressions (e.g. '4/8-4/21'), not
    verified prescription periods. Scans all current artifacts — the
    horizon is relative to now, so no incremental watermark applies."""
    today = datetime.fromtimestamp(now, JST).date()
    seen = set()
    archived = _archived_pids(db)
    for pid, mid, content in med_period_artifacts(db):
        if pid in archived:
            continue
        for p, end_d in iter_period_ends(content):
            days = (end_d - today).days
            if not (0 <= days <= th["expiry_ahead_days"]):
                continue
            anchor = f"{mid}:{p.get('raw') or p['end']}"
            if (mid, p.get("raw")) in seen:
                continue             # duplicate artifact copies
            seen.add((mid, p.get("raw")))
            yield _key("rx_period_expiry", pid, anchor), {
                "type": "rx_period_expiry", "project_id": pid,
                "evidence": {"message_id": mid, "raw": p.get("raw"),
                             "start": p.get("start"), "end": p["end"]},
                "context": {"days_left": days},
                "note": f"記録上の期間表現の終了日まで{days}日です"
                        "（抽出された表現であり、処方期間の確定では"
                        "ありません）"}


def _rx_period_lapsed(db, now, th, sig_cfg):
    """Patients whose furthest-out recorded period expression has
    already ended with no later period mention — the prescription may
    have lapsed ('内服切れ'). Alerting is bound to `rx_lapsed_days`
    days past the end: beyond that, the missing renewal is a chronic
    record style rather than a fresh alert."""
    today = datetime.fromtimestamp(now, JST).date()
    latest = {}                    # pid -> (end_d, mid, raw)
    archived = _archived_pids(db)
    for pid, mid, content in med_period_artifacts(db):
        if pid in archived:
            continue
        for p, end_d in iter_period_ends(content):
            cur = latest.get(pid)
            if cur is None or (end_d, mid) > (cur[0], cur[1]):
                latest[pid] = (end_d, mid, p.get("raw"))
    for pid, (end_d, mid, raw) in latest.items():
        days = (today - end_d).days
        if not (1 <= days <= th["rx_lapsed_days"]):
            continue
        yield _key("rx_period_lapsed", pid, f"{mid}:{end_d}"), {
            "type": "rx_period_lapsed", "project_id": pid,
            "evidence": {"message_id": mid, "raw": raw,
                         "end": end_d.isoformat()},
            "context": {"days_since_end": days},
            "note": f"記録上の期間表現の終了日を{days}日過ぎており、"
                    "新しい期間表現の記録はありません（抽出された表現"
                    "であり、処方の継続・切れは原記録で確認してくださ"
                    "い）"}


def _transition_reconciliation(db, now, th, sig_cfg):
    """Rooms where a typed discharge/transfer event (extract_llm
    `events`, not a body substring — '退院できません' etc. does not
    match) co-occurs with a med change-action mention within ±N days.
    Co-occurrence is a review prompt — whether reconciliation is needed
    is a human decision. Coverage is limited to messages carrying a
    current extract_llm artifact, same as the med side. Discharge posts
    authored by configured self_organizations are excluded — our own
    reports are not review candidates for us."""
    lookback = now - th["transition_lookback_d"] * DAY_S
    win = th["transition_med_window_d"] * DAY_S
    orgs, _, _ = _self_sets(sig_cfg, db)
    self_pred, self_params = _self_author_pred(orgs, "d")
    grouped = transition_cooccurrences(
        db, win_s=win,
        extra_where="AND d.posted_at_ts >= ? AND d.posted_at_ts <= ?"
                    + self_pred,
        params=(lookback, now, *self_params), exclude_archived=True)
    for dmid, (pid, mids) in grouped.items():
        change_ids = sorted(mids)
        if change_ids:
            yield _key("transition_reconciliation", pid, dmid), {
                "type": "transition_reconciliation", "project_id": pid,
                "evidence": {"discharge_message_id": dmid,
                             "med_change_message_ids": change_ids},
                "context": {"window_days": th["transition_med_window_d"]},
                "note": "退院の言及の前後に薬変更の言及があります — "
                        "処方内容の照合が必要かどうか人が原記録を確認し"
                        "てください（自動判定ではありません）"}


# requests.to spellings meaning 'addressed to a pharmacist/pharmacy' —
# a bare 薬 match would sweep in task-like free-text targets (「薬の
# 確認」), so it is restricted to role/facility words. Config
# request_targets adds exact spellings on top; _rx_request_visibility
# must mirror this negatively.
PHARM_TARGET_SQL = (f"json_extract({JSON_OBJECT_SQL},'$.to') LIKE '%薬剤師%' "
                    f"OR json_extract({JSON_OBJECT_SQL},'$.to') LIKE '%薬局%' "
                    f"OR json_extract({JSON_OBJECT_SQL},'$.to') LIKE '%調剤%'")
# a poster's own plan is not a request somebody must answer — neither
# detector fires on it (#20 order 3); a question addressed to the
# pharmacy is still a consultation that needs a reply, so it counts
REQ_ACTIONABLE_SQL = (f"COALESCE(json_extract({JSON_OBJECT_SQL},'$.kind'),'') "
                      "<> 'self_plan'")


def _pharmacist_request(db, now, th, sig_cfg):
    """extract_llm requests addressed to the pharmacy (a pharmacist-role
    target or configured request_targets) whose mention passed the
    response window with no visible responder post — 'no response could
    be confirmed on the record', never 'ignored'. Response = a post by
    self_professions/self_organizations or a registered request, plus —
    only with signals.self_reaction_response === true — the user's own
    承知/完了 stamp on the request post."""
    orgs, profs, targets = _self_sets(sig_cfg, db)
    self_reaction = (isinstance(sig_cfg, dict)
                     and sig_cfg.get("self_reaction_response") is True)
    # request_targets adds exact spellings like 「〇〇薬局さま」.
    # Empty/不明 targets never count as pharmacist-addressed.
    tgt_pred = (f" OR json_extract({JSON_OBJECT_SQL},'$.to') IN "
                f"({','.join('?' * len(targets))})") if targets else ""
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts,
                   json_extract({JSON_OBJECT_SQL},'$.action') AS act
            FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            JOIN patients p ON p.project_id=m.project_id
            JOIN json_each({json_or_null('a.content')},'$.requests') je
            WHERE a.kind IN ({FACT_KINDS_SQL})
              {current_fact_pred()}
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts >= ?
              AND m.posted_at_ts <= ?
              AND COALESCE(p.is_archived,0)=0
              AND {ITEM_CONFIRMED_SQL}
              AND {REQ_ACTIONABLE_SQL}
              AND ({PHARM_TARGET_SQL}
                   {tgt_pred})
            ORDER BY m.project_id, m.message_id""",
        (now - th["fyi_max_age_d"] * DAY_S,
         now - th["request_response_days"] * DAY_S,
         *targets)).fetchall()
    groups = {}
    for pid, mid, ts, act in rows:
        groups.setdefault((pid, mid, ts), []).append(act)
    for (pid, mid, ts), acts in groups.items():
        if _request_registered(db, mid):
            continue
        if _self_post_exists(db, pid, ts, profs, orgs):
            continue
        if self_reaction and _self_reaction_response(db, mid):
            continue
        days = int((now - ts) / DAY_S)
        acts = [a for a in acts if isinstance(a, str) and a]
        if not acts:
            continue                 # an action-less request is too thin
        yield _key("pharmacist_request_unanswered", pid, mid), {
            "type": "pharmacist_request_unanswered", "project_id": pid,
            "evidence": {"message_ids": [mid], "request_actions": acts},
            "context": {"days_unanswered": days},
            "note": f"薬剤師宛の依頼・相談の言及（「{'」「'.join(acts)}」）"
                    f"から{th['request_response_days']}日以上経過し、記録上"
                    "の応答を確認できませんでした（記録上の確認であり、"
                    "対応の有無を示すものではありません）"}


def _rx_request_visibility(db, now, th, sig_cfg):
    """Med-related requests directed at OTHER professions — early
    visibility into the prescription pipeline (a nurse asking the
    doctor for a drug is tomorrow's dispense). FYI only; pharmacist-
    addressed requests belong to pharmacist_request_unanswered."""
    orgs, profs, targets = _self_sets(sig_cfg, db)
    extra = "".join(",?" for _ in targets)
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts,
                   json_extract({JSON_OBJECT_SQL},'$.to') AS rto,
                   json_extract({JSON_OBJECT_SQL},'$.action') AS act
            FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            JOIN patients p ON p.project_id=m.project_id
            JOIN json_each({json_or_null('a.content')},'$.requests') je
            WHERE a.kind IN ({FACT_KINDS_SQL})
              {current_fact_pred()}
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts >= ?
              AND COALESCE(p.is_archived,0)=0
              AND {ITEM_CONFIRMED_SQL}
              AND {REQ_ACTIONABLE_SQL}
              AND NOT ({PHARM_TARGET_SQL})
              AND COALESCE(json_extract({JSON_OBJECT_SQL},'$.to'),'')
                  NOT IN ('','不明'{extra})
              AND (json_extract({JSON_OBJECT_SQL},'$.action') LIKE '%処方%'
                   OR json_extract({JSON_OBJECT_SQL},'$.action') LIKE '%薬%'
                   OR json_extract({JSON_OBJECT_SQL},'$.action') LIKE '%内服%'
                   OR json_extract({JSON_OBJECT_SQL},'$.action') LIKE '%残薬%'
                   OR json_extract({JSON_OBJECT_SQL},'$.action') LIKE '%一包化%')
            ORDER BY m.project_id, m.message_id""",
        (now - th["fyi_max_age_d"] * DAY_S, *targets)).fetchall()
    groups = {}
    for pid, mid, ts, rto, act in rows:
        groups.setdefault((pid, mid, ts), []).append((rto, act))
    for (pid, mid, ts), reqs in groups.items():
        if _request_registered(db, mid):
            continue
        if _self_post_exists(db, pid, ts, profs, orgs):
            continue
        detail = "」「".join(
            f"{a}（{t}宛）" if isinstance(t, str) and t else f"{a}"
            for t, a in reqs if isinstance(a, str) and a)
        if not detail:
            continue
        yield _key("rx_request_visibility", pid, mid), {
            "type": "rx_request_visibility", "project_id": pid,
            "evidence": {"message_ids": [mid],
                         "request_actions": [a for _, a in reqs]},
            "context": {},
            "note": f"他職種宛の処方関連依頼の言及があります — 「{detail}」"
                    "（薬局側の準備・照合の機会としての記録上の言及です）"}


# body phrases that flag adherence/management difficulty even when the
# extractor never produced a meds item — chosen in affirming forms so
# plain negations (〜なし/ない/ありません/ません/ていない) do not match
ADHERENCE_PATTERNS = ("飲み忘れ", "飲みのこし", "飲んでいない",
                      "飲めていない", "飲めない", "飲みきれない", "残薬が",
                      "残薬あり", "残薬がある", "自己中断", "自己中止",
                      "服薬管理が難し", "管理できな")
# capability claims ending in a negation ARE the concern — the tail
# check would eat the ない that completes them, so these match as-is
ADHERENCE_TERMINAL = ("管理は出来ない", "管理はできない",
                      "管理は出来ません", "管理はできません",
                      "管理が出来ない", "管理ができない",
                      "管理が出来ません", "管理ができません",
                      "服薬管理出来ない", "服薬管理できない",
                      "服薬管理出来ません", "服薬管理できません")
# てい/してい covers 「飲み忘れていない」「飲み忘れはしていない」;
# ません covers 「残薬ありません」 (the pattern consumes the あり)
_NEGATE_RE = re.compile(
    r"^[はがも、。\s]*(?:してい|てい|て)?(ない|なし|ありません|なく|ません)")


def _adherence_phrases(text):
    hits = [p for p in ADHERENCE_TERMINAL if p in text]
    for pat in ADHERENCE_PATTERNS:
        i = text.find(pat)
        while i >= 0:
            tail = text[i + len(pat): i + len(pat) + 10]
            if not _NEGATE_RE.match(tail):
                hits.append(pat)
                break
            i = text.find(pat, i + 1)
    return hits


def _adherence_concern(db, now, th, sig_cfg):
    """Medication-management difficulty / non-use mentions — the
    dispensing pharmacist's intervention domain (一包化・管理支援・
    残薬調整の検討余地). Sources: extracted meds marked negated or
    carrying capability evidence (「〜は出来ない」 — the same spans
    med_change_no_followup now excludes), and body phrases that never
    become meds items. Self-authored mentions are excluded."""
    orgs, profs, _ = _self_sets(sig_cfg, db)
    self_pred, self_params = _self_author_pred(orgs)
    horizon = now - th["fyi_max_age_d"] * DAY_S
    groups = {}
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts,
                   TRIM(json_extract({JSON_OBJECT_SQL},'$.name')) AS med
            FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            JOIN patients p ON p.project_id=m.project_id
            JOIN json_each({json_or_null('a.content')},'$.meds') je
            WHERE a.kind IN ({FACT_KINDS_SQL})
              {current_fact_pred()}
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts >= ?
              AND COALESCE(p.is_archived,0)=0
              {self_pred}
              AND json_type({JSON_OBJECT_SQL},'$.name')='text'
              AND TRIM(json_extract({JSON_OBJECT_SQL},'$.name'))!=''
              AND COALESCE(json_extract({JSON_OBJECT_SQL},'$.subject'),
                           'patient')='patient'
              AND COALESCE(json_extract({JSON_OBJECT_SQL},'$.status'),
                           'current')!='past'
              AND (json_extract({JSON_OBJECT_SQL},'$.negated') IS 1
                   OR NOT ({MED_NOT_CAPABILITY_SQL}))
            ORDER BY m.project_id, m.message_id""",
        (horizon, *self_params)).fetchall()
    for pid, mid, ts, med in rows:
        groups.setdefault((pid, mid, ts), []).append(med)
    # SQL-side prefilter narrows the Python phrase scan to rows that
    # could contain a pattern at all (LIKE on the raw pattern text)
    like_pred = " OR ".join(
        ["m.body_text LIKE ?"] * (len(ADHERENCE_PATTERNS)
                                + len(ADHERENCE_TERMINAL)))
    phrase_rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts,
                   m.body_text
            FROM messages m
            JOIN patients p ON p.project_id=m.project_id
            WHERE m.posted_at_ts IS NOT NULL AND m.posted_at_ts >= ?
              AND COALESCE(p.is_archived,0)=0 AND m.body_text IS NOT NULL
              AND m.body_state='full'
              AND ({like_pred})
              {self_pred}""",
        (horizon,
         *(f"%{p}%" for p in ADHERENCE_PATTERNS + ADHERENCE_TERMINAL),
         *self_params)).fetchall()
    for pid, mid, ts, body in phrase_rows:
        hits = _adherence_phrases(body or "")
        if hits:
            groups.setdefault((pid, mid, ts), []).extend(hits)
    for (pid, mid, ts), found in groups.items():
        if _request_registered(db, mid):
            continue
        if _self_post_exists(db, pid, ts, profs, orgs):
            continue
        detail = "」「".join(dict.fromkeys(found))
        yield _key("adherence_concern", pid, mid), {
            "type": "adherence_concern", "project_id": pid,
            "evidence": {"message_ids": [mid],
                         "mentions": list(dict.fromkeys(found))},
            "context": {},
            "note": f"服薬管理・残薬等に関する言及があります — 「{detail}」"
                    "（介入の検討余地を示す記録上の言及です）"}


def _discharge_notice(db, now, th, sig_cfg):
    """Bare discharge/transfer mentions with no med-change
    co-occurrence (co-occurring ones are transition_reconciliation) —
    the heads-up that a prescription-reconciliation window may be
    open. Resolves when a co-occurrence appears (the sibling signal
    takes over), when a responder posts, or at the lookback edge."""
    lookback = now - th["transition_lookback_d"] * DAY_S
    win = th["transition_med_window_d"] * DAY_S
    orgs, profs, _ = _self_sets(sig_cfg, db)
    self_pred, self_params = _self_author_pred(orgs, "d")
    grouped = transition_cooccurrences(
        db, win_s=win,
        extra_where="AND d.posted_at_ts >= ? AND d.posted_at_ts <= ?",
        params=(lookback, now), exclude_archived=True)
    covered = set(grouped)
    rows = db.execute(
        f"""SELECT DISTINCT d.project_id, d.message_id, d.posted_at_ts
            FROM messages d
            JOIN patients p ON p.project_id=d.project_id
            JOIN artifacts da ON da.message_id=d.message_id
            JOIN json_each({json_or_null('da.content')},'$.events') ev
            WHERE da.kind IN ({FACT_KINDS_SQL})
              {current_fact_pred('da', 'd')}
              AND ev.value IN ({TRANSITION_EVENTS_SQL})
              AND d.posted_at_ts >= ?
              AND COALESCE(p.is_archived,0)=0
              {self_pred}
            ORDER BY d.project_id, d.message_id""",
        (lookback, *self_params)).fetchall()
    for pid, mid, ts in rows:
        if mid in covered:
            continue                    # transition_reconciliation owns it
        if _request_registered(db, mid):
            continue
        if _self_post_exists(db, pid, ts, profs, orgs):
            continue
        yield _key("discharge_notice", pid, mid), {
            "type": "discharge_notice", "project_id": pid,
            "evidence": {"discharge_message_id": mid},
            "context": {},
            "note": "退院・転院の言及があります — 処方変更の有無を原記録"
                    "で確認する機会です（自動判定ではありません）"}


def _symptom_after_med(db, now, th, sig_cfg):
    """Same-post coupling: a change-action med mention AND a new or
    ongoing non-negated patient symptom in ONE message — an ADR-triage
    prompt. Coupling is deliberately strict (same extraction): a loose
    room-level window paired almost everything and meant nothing."""
    orgs, profs, _ = _self_sets(sig_cfg, db)
    self_pred, self_params = _self_author_pred(orgs)
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts, a.content
            FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            JOIN patients p ON p.project_id=m.project_id
            WHERE a.kind IN ({FACT_KINDS_SQL})
              {current_fact_pred()}
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts >= ?
              AND COALESCE(p.is_archived,0)=0
              {self_pred}
            ORDER BY m.project_id, m.message_id""",
        (now - th["fyi_max_age_d"] * DAY_S, *self_params)).fetchall()
    for pid, mid, ts, content_s in rows:
        try:
            content = json.loads(content_s)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(content, dict):
            continue
        med_items = content.get("meds")
        symptom_items = content.get("symptoms")
        if not isinstance(med_items, list) or not isinstance(symptom_items, list):
            continue
        meds = [m2["name"].strip() for m2 in med_items
                if isinstance(m2, dict)
                and m2.get("action") in CHANGE_ACTIONS
                and med_is_patient_current(m2)
                and not med_capability_evidence(m2.get("evidence"))
                and isinstance(m2.get("name"), str) and m2["name"].strip()]
        if not meds:
            continue
        symps = [s["text"].strip() for s in symptom_items
                 if isinstance(s, dict) and not s.get("negated")
                 and not item_unverified(s)
                 and s.get("status") in ("new", "ongoing")
                 and s.get("subject", "patient") in ("patient", None)
                 and isinstance(s.get("text"), str) and s["text"].strip()]
        if not symps:
            continue
        if _request_registered(db, mid):
            continue
        if _self_post_exists(db, pid, ts, profs, orgs):
            continue
        yield _key("symptom_after_med_change", pid, mid), {
            "type": "symptom_after_med_change", "project_id": pid,
            "evidence": {"message_ids": [mid], "meds": meds,
                         "symptoms": symps},
            "context": {},
            "note": f"薬変更言及（{'・'.join(meds)}）と同じ投稿で症状言及"
                    f"（{'・'.join(symps)}）があります — 関連は人が原記録"
                    "で判断してください（自動判定ではありません）"}


DETECTORS = (("request_overdue", _request_overdue),
             ("request_aging", _request_aging),
             ("med_change_no_followup", _med_followup),
             ("pharmacist_request_unanswered", _pharmacist_request),
             ("rx_request_visibility", _rx_request_visibility),
             ("adherence_concern", _adherence_concern),
             ("discharge_notice", _discharge_notice),
             ("symptom_after_med_change", _symptom_after_med),
             ("comm_concentration", _comm_concentration),
             ("rx_period_expiry", _rx_period_expiry),
             ("rx_period_lapsed", _rx_period_lapsed),
             ("transition_reconciliation", _transition_reconciliation))


def _insert(db, key, sig):
    db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,"
        "model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
        (ARTIFACT_KIND, sig["project_id"],
         sig["evidence"].get("message_id")
         or sig["evidence"].get("discharge_message_id"),
         json.dumps(sig, ensure_ascii=False), "mcs_signals",
         json.dumps({"key": key, "type": sig["type"]}), time.time()))


def _signal_content(content_s, stored_pid):
    """Validate a stored lifecycle row before reading or extending it."""
    try:
        content = json.loads(content_s)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(content, dict):
        return None
    pid = content.get("project_id")
    detected = content.get("detected_at", 0)
    if (type(pid) is not int or not 0 < pid <= 2**63 - 1
            or (stored_pid is not None and stored_pid != pid)
            or content.get("state") not in ("open", "resolved", "dismissed")
            or not isinstance(content.get("type"), str)
            or not isinstance(content.get("evidence"), dict)
            or type(detected) not in (int, float) or not 0 <= detected < 1e12):
        return None
    return content


def _latest_signal_states(db):
    # Keep a tombstone for an unreadable latest row. Falling back to an
    # older open row could revive a dismissed signal or duplicate a notice.
    latest = {}
    for pid, content_s, meta_s in db.execute(
            "SELECT project_id, content, meta FROM artifacts "
            "WHERE kind=? ORDER BY artifact_id", (ARTIFACT_KIND,)):
        try:
            meta = json.loads(meta_s)
        except (ValueError, TypeError, RecursionError):
            continue
        key = meta.get("key") if isinstance(meta, dict) else None
        if isinstance(key, str) and key:
            latest[key] = _signal_content(content_s, pid)
    return latest


def signal_message_ids(sig):
    """Stored message evidence only; never expand it to unrelated room posts."""
    ev = sig.get("evidence") or {}
    values = [ev.get("message_id"), ev.get("discharge_message_id")]
    for field in ("message_ids", "med_change_message_ids"):
        if isinstance(ev.get(field), list):
            values.extend(ev[field])
    return sorted({mid for mid in values if type(mid) is int and mid > 0})


def _resolution(db, old, now, th, sig_cfg):
    """Observe exclusion gates AFTER the detector has decided to resolve.

    The cause describes a sufficient exclusion of the stored evidence, not
    clinical completion. Unexplained absence stays unclassified, rather than
    being labelled care completed or condition cleared. Multiple simultaneous
    exclusions are not assigned a counterfactual causal priority.
    """
    causes = []
    stype, pid = old["type"], old["project_id"]
    ev = old["evidence"]
    try:
        if stype in ("request_overdue", "request_aging"):
            row = db.execute(
                "SELECT status,due_date,created_at FROM requests "
                "WHERE request_id=? AND project_id=?",
                (ev.get("request_id"), pid)).fetchone()
            if row is None:
                causes.append("request_missing")
            elif row["status"] not in ("open", "in_progress"):
                causes.append("request_status_changed")
            elif stype == "request_overdue" and row["due_date"] != ev.get("due_date"):
                causes.append("request_due_changed")
            elif stype == "request_aging" and row["created_at"] > now - th["req_age_days"] * DAY_S:
                causes.append("request_age_changed")
        else:
            patient = db.execute(
                "SELECT is_archived FROM patients WHERE project_id=?", (pid,)).fetchone()
            if patient is None:
                causes.append("project_missing")
            elif patient[0]:
                causes.append("project_archived")
            mids = signal_message_ids(old)
            # Med episodes may have truncated evidence; do not claim all their
            # mentions aged out or were answered from a partial evidence list.
            complete = ev.get("mention_count", len(mids)) == len(mids)
            messages = [db.execute(
                "SELECT posted_at_ts,body_state FROM messages "
                "WHERE message_id=? AND project_id=?", (mid, pid)).fetchone()
                for mid in mids]
            if mids and any(m is None for m in messages):
                causes.append("evidence_missing")
            elif mids and all(m["body_state"] == "deleted" for m in messages):
                causes.append("evidence_deleted")
            horizon = {
                "med_change_no_followup": th["followup_max_age_d"],
                "pharmacist_request_unanswered": th["fyi_max_age_d"],
                "rx_request_visibility": th["fyi_max_age_d"],
                "adherence_concern": th["fyi_max_age_d"],
                "symptom_after_med_change": th["fyi_max_age_d"],
                "discharge_notice": th["transition_lookback_d"],
                "transition_reconciliation": th["transition_lookback_d"],
            }.get(stype)
            anchors = messages[:1] if stype == "transition_reconciliation" else messages
            if stype == "transition_reconciliation":
                anchor = db.execute(
                    "SELECT posted_at_ts,body_state FROM messages WHERE message_id=?",
                    (ev.get("discharge_message_id"),)).fetchone()
                anchors = [anchor]
            if (horizon is not None and anchors and complete
                    and all(m is not None and m["posted_at_ts"] is not None
                            and m["posted_at_ts"] < now - horizon * DAY_S
                            for m in anchors)):
                causes.append("evidence_aged_out")
            engagement_types = (
                "pharmacist_request_unanswered", "rx_request_visibility",
                "adherence_concern", "symptom_after_med_change", "discharge_notice")
            if stype in engagement_types and len(mids) == 1:
                mid, message = mids[0], messages[0]
                if _request_registered(db, mid):
                    causes.append("request_registered")
                orgs, profs, _ = _self_sets(sig_cfg, db)
                if (message is not None and message["posted_at_ts"] is not None
                        and _self_post_exists(db, pid, message["posted_at_ts"], profs, orgs)):
                    causes.append("responder_post")
                if (stype == "pharmacist_request_unanswered"
                        and isinstance(sig_cfg, dict)
                        and sig_cfg.get("self_reaction_response") is True
                        and _self_reaction_response(db, mid)):
                    causes.append("self_reaction_observed")
            elif stype == "med_change_no_followup" and mids and complete:
                if all(_request_registered(db, mid) for mid in mids):
                    causes.append("request_registered")
                if all(m is not None and m["posted_at_ts"] is not None
                       and db.execute(
                           "SELECT 1 FROM messages WHERE project_id=? "
                           "AND posted_at_ts>? AND posted_at_ts<=? LIMIT 1",
                           (pid, m["posted_at_ts"],
                            m["posted_at_ts"] + th["followup_days"] * DAY_S)).fetchone()
                       for m in messages):
                    causes.append("followup_record")
            if stype in ("rx_period_expiry", "rx_period_lapsed"):
                end = datetime.strptime(ev["end"], "%Y-%m-%d").date()
                age = (datetime.fromtimestamp(now, JST).date() - end).days
                if age > (0 if stype == "rx_period_expiry" else th["rx_lapsed_days"]):
                    causes.append("evidence_aged_out")
            if stype == "comm_concentration" and not causes:
                count = db.execute(
                    "SELECT COUNT(*) FROM messages WHERE project_id=? "
                    "AND posted_at_ts>=? AND posted_at_ts<?",
                    (pid, now - th["conc_window_h"] * 3600, now)).fetchone()[0]
                if count < th["conc_min_posts"]:
                    causes.append("volume_below_threshold")
    except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
        # Measurement cannot veto the already-made lifecycle decision.
        return {"cause": "unclassified", "observed_causes": [],
                "reason": "measurement_unavailable", "prior_state": old["state"]}
    return {"cause": causes[0] if len(causes) == 1 else
            "multiple_exclusions" if causes else "unclassified",
            "observed_causes": causes, "prior_state": old["state"],
            "basis": "stored_evidence_exclusion"}


def evaluate(ledger, cfg: dict, now: float | None = None,
             deadline: float | None = None) -> dict:
    """Recompute candidates; append lifecycle transitions; enqueue
    notify intents for newly opened signals only when
    signals.notify===true. Returns counts for the run log."""
    now = now if now is not None else time.time()
    th = _thresholds(ledger.db)
    sig_cfg = cfg.get("signals")
    notify = isinstance(sig_cfg, dict) and sig_cfg.get("notify") is True

    current = {}
    ran_types = set()
    errors = []
    for stype, det in DETECTORS:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            found = dict(det(ledger.db, now, th, sig_cfg))
        except Exception as e:
            errors.append(f"{stype}:{type(e).__name__}")
            continue
        # merge only on full success: a half-scanned type must neither
        # open partial candidates nor resolve its existing signals
        current.update(found)
        ran_types.add(stype)

    existing = _latest_signal_states(ledger.db)
    if any(value is None for value in existing.values()):
        errors.append("signal_state_corrupt")

    opened = superseded = resolved = enqueued = dig_merged = 0
    newly = []
    suppression = {}
    with ledger.db:
        for key, sig in current.items():
            if key in existing and existing[key] is None:
                continue  # unknown latest state: never reopen/notify from history
            old = existing.get(key)
            if old is not None and old["state"] == "dismissed":
                counts = suppression.setdefault((sig["project_id"], sig["type"]), [0, 0])
                counts[0] += 1
                counts[1] += old.get("evidence") == sig["evidence"]
            if old is None or old["state"] == "resolved" or (
                    old["state"] == "dismissed"
                    and old.get("evidence") != sig["evidence"]):
                # new signal, condition returned after resolution, or a
                # human dismissed an earlier evidence set that has since
                # changed — a genuinely new situation, open again
                sig.update(v=1, state="open", detected_at=now,
                           resolved_at=None,
                           lifecycle={"event": "reopened" if old else "opened",
                                      "at": now,
                                      "from_state": old["state"] if old else None})
                _insert(ledger.db, key, sig)
                opened += 1
                newly.append((key, sig))
            elif old["state"] == "dismissed":
                pass   # human dismissed this exact evidence — stays down
            elif old.get("evidence") != sig["evidence"]:
                # identity-level evidence moved on — supersede with a
                # fresh open row preserving the original detection time
                # (volatile context stays frozen at each detection)
                sig.update(v=1, state="open",
                           detected_at=old.get("detected_at", now),
                           resolved_at=None,
                           lifecycle={"event": "superseded", "at": now})
                _insert(ledger.db, key, sig)
                superseded += 1
            # else: still open with identical evidence — nothing to write
        for key, old in existing.items():
            # resolve only types that actually ran this evaluation —
            # an unfinished or crashed detector must never turn
            # "not inspected" into a recorded "resolved" (the ledger
            # history is a review record, not a guess)
            # a dismissed key resolves too once its condition clears:
            # the dismissal covered that episode, and evidence such as
            # comm_concentration's bare project id would otherwise
            # silence every later recurrence in the room for good
            if (old is not None and key not in current
                    and old["state"] in ("open", "dismissed")
                    and old.get("type") in ran_types):
                row = dict(old, state="resolved", resolved_at=now,
                           lifecycle={"event": "resolved", "at": now},
                           resolution=_resolution(ledger.db, old, now, th, sig_cfg))
                row.pop("reopened_at", None)
                _insert(ledger.db, key, row)
                resolved += 1
        if notify:
            enqueued, dig_merged = _notify_opened(
                ledger, newly, now, th, sig_cfg)
        # one accumulating row per project/type/JST day — a per-evaluate
        # row grew ~288/day per suppressed pair. "at" stays the day's first
        # observation, so sums and first-observed time are unchanged;
        # "last_at" lets stats refuse a row that straddles as_of/since/until.
        # ponytail: a sub-day window drops the straddled day as partial;
        # bucket finer if sub-day windows ever matter.
        day = datetime.fromtimestamp(now, JST).date().isoformat()
        for (pid, stype), (eligible, suppressed) in suppression.items():
            meta = json.dumps({"type": stype, "day": day})
            row = ledger.db.execute(
                "SELECT artifact_id,content FROM artifacts WHERE kind=? "
                "AND project_id=? AND meta=? ORDER BY artifact_id DESC LIMIT 1",
                (FEEDBACK_KIND, pid, meta)).fetchone()
            prev = None
            if row:
                try:
                    prev = json.loads(row[1])
                except (ValueError, TypeError, RecursionError):
                    prev = None
            if (isinstance(prev, dict)
                    and type(prev.get("dismissed_candidates")) is int
                    and type(prev.get("same_evidence_suppressed")) is int):
                prev["dismissed_candidates"] += eligible
                prev["same_evidence_suppressed"] += suppressed
                prev["last_at"] = now
                ledger.db.execute(
                    "UPDATE artifacts SET content=? WHERE artifact_id=?",
                    (json.dumps(prev), row[0]))
                continue
            ledger.db.execute(
                "INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
                "VALUES(?,?,?,?,?)",
                (FEEDBACK_KIND, pid, json.dumps({
                    "v": 1, "type": stype, "at": now, "last_at": now,
                    "dismissed_candidates": eligible,
                    "same_evidence_suppressed": suppressed}),
                 meta, now))
    return {"open": len(current), "opened": opened,
            "superseded": superseded, "resolved": resolved,
            "notify_enqueued": enqueued,
            "notify_digest_merged": dig_merged,
            "notify_enabled": notify,
            "detectors_ran": sorted(ran_types), "errors": errors}


def evidence_fp(evidence) -> str:
    """Fingerprint of a signal's evidence set — the outbox pins it so the
    send path can tell 'the signal I queued' from 'the signal now' (F18)."""
    return hashlib.sha256(json.dumps(
        evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def signal_notice_text(sig: dict) -> str:
    """The notice body built from a signal ROW (not the frozen payload) —
    enqueue and send-time re-render share this so an evidence update can
    never mix an old explanation with new evidence (F18)."""
    ev = sig.get("evidence") or {}
    where = f"project {sig['project_id']}"
    for label in ("request_id", "message_id", "discharge_message_id",
                  "med"):
        if ev.get(label):
            where += f" / {label.split('_')[0]} {ev[label]}"
            break
    else:
        mids = ev.get("message_ids")
        if mids:
            where += f" / message {mids[0]}"
    return (f"[MCS] アラート ({sig['type']})\n"
            f"{where}\n{sig['note']}")


def med_followup_group_notice(sigs):
    """One notice body for med_change_no_followup signals whose latest
    mention is the same post — 'med A・B' plus a combined note instead
    of N near-identical sends. Returns None when a member lacks the
    fields a merged rendering needs (caller falls back to per-signal
    text)."""
    meds, days = [], None
    for s in sigs:
        ev = s.get("evidence") or {}
        med = ev.get("med")
        if not isinstance(med, str) or not med:
            return None
        meds.append(med)
        if days is None:
            d = (s.get("context") or {}).get("window_days")
            if type(d) is int:
                days = d
            elif type(d) is float and d.is_integer():
                days = int(d)
    if len(meds) < 2 or days is None:
        return None
    sig = sigs[0]
    return (f"[MCS] アラート ({sig['type']})\n"
            f"project {sig['project_id']} / med {'・'.join(meds)}\n"
            + _med_followup_note(meds, days))


# notification tier per signal type — 'immediate' sends its own intent
# now; 'digest' accumulates into ONE periodic digest intent listing
# every still-open member. urgency:high on the source mention
# escalates any type to immediate. signals.tiers overrides per type;
# signals.digest:false turns batching off (everything immediate);
# signals.digest_interval_h sets the flush delay (default 24h).
SIGNAL_TIERS = {
    "pharmacist_request_unanswered": "immediate",
    "discharge_notice": "immediate",
    "transition_reconciliation": "immediate",
    "med_change_no_followup": "digest",
    "rx_request_visibility": "digest",
    "adherence_concern": "digest",
    "symptom_after_med_change": "digest",
    "request_overdue": "digest",
    "request_aging": "digest",
    "comm_concentration": "digest",
    "rx_period_expiry": "digest",
    "rx_period_lapsed": "digest",
}
DIGEST_INTERVAL_H = 24


def med_group_key(sig):
    """Grouping key for same-post med_change_no_followup signals —
    (project_id, latest mention mid); None for anything else."""
    ev = sig.get("evidence") or {}
    mids = ev.get("message_ids")
    if (sig.get("type") == "med_change_no_followup"
            and isinstance(mids, list) and mids
            and type(mids[-1]) is int):
        return (sig["project_id"], mids[-1])
    return None


def sig_units(pairs):
    """[(group_key|None, item)] -> [[item...]] — items sharing a
    non-None group key merge into one render/enqueue unit (insertion
    order kept). Shared by the enqueue path and the send-time
    digest/merged renderer."""
    units, groups = [], {}
    for gkey, item in pairs:
        if gkey is None:
            units.append([item])
        elif gkey in groups:
            groups[gkey].append(item)
        else:
            members = [item]
            groups[gkey] = members
            units.append(members)
    return units


def _urgency_high(db, sig):
    """True when the signal's primary evidence message carries a
    high-urgency extraction — escalates a digest-tier signal to
    immediate delivery. Same reading as the cards and text notices
    (structured_view.message_urgency): either extractor kind (rule
    extract_v1 or extract_llm) can carry the flag."""
    ev = sig.get("evidence") or {}
    mids = ev.get("message_ids")
    mid = ((mids[-1] if isinstance(mids, list) and mids else None)
           or ev.get("discharge_message_id"))
    if type(mid) is not int:
        return False
    return message_urgency(db, mid) is not None


def _digest_text(n):
    return f"[MCS] アラートダイジェスト（{n}件）"


def open_signal_rows(db, keys):
    """signal_keys -> [(key, latest content)] for keys still open. The
    send path's member check and the digest-rescue path share this —
    'open' is always the latest artifact row's state, never the frozen
    payload's."""
    out = []
    for k in keys:
        if not isinstance(k, str) or not k:
            continue
        row = db.execute(
            """SELECT project_id, content FROM artifacts
               WHERE kind='signal_v1'
                 AND CASE WHEN json_valid(meta) THEN
                     json_extract(meta,'$.key')=? ELSE 0 END
               ORDER BY artifact_id DESC LIMIT 1""", (k,)).fetchone()
        s = _signal_content(row["content"], row["project_id"]) if row else None
        if isinstance(s, dict) and s.get("state") == "open":
            out.append((k, s))
    return out


def rescue_digest_members(ledger, payload, now, interval_h,
                          origin_id=None):
    """A quarantined signal intent strands its member keys — signals
    notify only at the open transition, so members of a held intent
    would never be re-enqueued. Fold the still-open ones into a fresh
    intent preserving the original shape: a held digest reschedules as
    a digest (interval_h out); a held merged/single intent re-enqueues
    immediately with the same signal_keys/signal_key carrier. Caller
    decides it's safe (no sent progress — salvage after a partial send
    could duplicate a delivered post). A rescued intent carries
    rescue_of=origin_id so a SECOND quarantine does not respawn —
    rescue is single-shot, not an immortal retry loop. Returns the
    rescued keys."""
    pl = payload or {}
    if pl.get("rescue_of") is not None:
        return []
    keys = [k for k in (pl.get("signal_keys") or [])
            if type(k) is str and k]
    skey = pl.get("signal_key")
    if not keys and type(skey) is str and skey:
        keys = [skey]
    live = [k for k, _ in open_signal_rows(ledger.db, keys)]
    if not live:
        return []
    if pl.get("digest") is True:
        ledger.outbox_add_tx(
            "signal", None,
            {"digest": True, "type": "signal_digest", "signal_keys": live,
             "text": _digest_text(len(live)),
             "rescue_of": origin_id},
            next_try=now + interval_h * 3600)
    else:
        new_pl = {"signal_keys": live,
                  "type": pl.get("type"),
                  "project_id": pl.get("project_id"),
                  "rescue_of": origin_id}
        if pl.get("urgent") is True:
            new_pl["urgent"] = True
        if len(live) == 1:
            new_pl["signal_key"] = live[0]
            del new_pl["signal_keys"]
        ledger.outbox_add_tx("signal", pl.get("project_id"), new_pl)
    return live


def _digest_add(ledger, key, now, th, interval_h):
    """Fold a digest-tier signal into the pending digest intent (or
    start one scheduled interval_h out). Pending-payload merge is safe:
    an unsent intent carries ids only, and the send-time renderer
    regroups same-post meds and drops members that resolved meanwhile.
    Returns (rows_added, merged_into_pending)."""
    if _notify_suppressed(ledger.db, key, now, th):
        return 0, False
    row = ledger.db.execute(
        """SELECT event_id, payload FROM notify_outbox
           WHERE kind='signal' AND state IN ('pending','failed')
             AND next_try IS NOT NULL
             AND CASE WHEN json_valid(payload) THEN
                 json_extract(payload,'$.digest')=1
                 AND json_type(payload,'$.signal_keys')='array'
                 ELSE 0 END
             AND NOT EXISTS(SELECT 1 FROM notification_intent_batches b
                            WHERE b.event_id=notify_outbox.event_id)
           ORDER BY event_id DESC LIMIT 1""").fetchone()
    if row:
        pl = json.loads(row["payload"])
        keys = pl.setdefault("signal_keys", [])
        if key not in keys:
            keys.append(key)
            pl["text"] = _digest_text(len(keys))
            ledger.db.execute(
                "UPDATE notify_outbox SET payload=?,updated_at=? "
                "WHERE event_id=?",
                (json.dumps(pl, ensure_ascii=False), now,
                 row["event_id"]))
            return 0, True
        return 0, False
    ledger.outbox_add_tx(
        "signal", None,
        {"digest": True, "type": "signal_digest",
         "signal_keys": [key], "text": _digest_text(1)},
        next_try=now + interval_h * 3600)
    return 1, False


def _notify_opened(ledger, items, now, th, sig_cfg):
    """Enqueue notify intents for signals opened this evaluation —
    immediate-tier signals get their own intent (same-post med_change
    members coalesce into ONE merged intent listing every med, since a
    per-med burst off one post reads as a duplicate send); digest-tier
    signals fold into the pending digest intent instead of pinging
    one-by-one. Signal rows/keys stay per-signal — only notifications
    merge. A med whose mention lands in a later evaluation still
    notifies on its own (merging into an already-queued NON-digest
    intent would rewrite a pending payload — kept simple on purpose).
    Returns (rows_added, keys_merged_into_digest)."""
    sc = sig_cfg if isinstance(sig_cfg, dict) else {}
    digest_on = sc.get("digest", True) is not False
    ih = sc.get("digest_interval_h")
    interval_h = (ih if type(ih) in (int, float) and 0 < ih <= 1e9
                  else DIGEST_INTERVAL_H)
    overrides = sc.get("tiers") if isinstance(sc.get("tiers"), dict) else {}
    imm, dig = [], []
    for key, sig in items:
        tier = overrides.get(sig.get("type"),
                             SIGNAL_TIERS.get(sig.get("type"),
                                              "immediate"))
        if digest_on and tier == "digest" \
                and not _urgency_high(ledger.db, sig):
            dig.append((key, sig))
        else:
            imm.append((key, sig))
    n = merged = 0
    for members in sig_units(
            [(med_group_key(s), (k, s)) for k, s in imm]):
        n += _notify(ledger, members, now, th)
    for key, _sig in dig:
        added, m = _digest_add(ledger, key, now, th, interval_h)
        n += added
        merged += 1 if m else 0
    return n, merged


def _notify_suppressed(db, key, now, th):
    """True when an undelivered intent already covers this signal key
    or one was accepted inside the cooldown — 'covers' includes the
    signal_keys[] of a merged same-post med intent."""
    covered = ("(json_extract(payload,'$.signal_key')=? OR EXISTS("
               "SELECT 1 FROM json_each(CASE WHEN "
               "json_type(payload,'$.signal_keys')='array' THEN "
               "json_extract(payload,'$.signal_keys') ELSE '[]' END) je "
               "WHERE je.value=?))")
    # quarantined intents (state='failed', next_try NULL) never
    # deliver — they must NOT count as covering the key
    if db.execute(
            "SELECT 1 FROM notify_outbox WHERE kind='signal' "
            "AND state IN ('pending','failed') AND next_try IS NOT NULL"
            " AND json_valid(payload) "
            f"AND {covered} LIMIT 1", (key, key)).fetchone():
        return True
    return db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='signal' "
        "AND state='accepted' AND json_valid(payload) "
        f"AND {covered} AND updated_at > ? LIMIT 1",
        (key, key, now - th["notify_cooldown_d"] * DAY_S)
    ).fetchone() is not None


def _notify(ledger, members, now, th):
    """Frozen-text notify intent via the existing outbox — payload
    carries ids and the fixed note, never message bodies. The send path
    re-checks at flush time: signals.notify revoked OR no member signal
    still open -> _StaleSend (terminal drop). An undelivered intent
    covering a member key is not duplicated (reopen flapping), and a
    key that was already notified inside the cooldown stays silent — a
    signal that flaps open/resolved must not spam the channel.
    members>1 is a same-post med_change group: ONE merged intent keyed
    by signal_keys[] instead of one intent per med. Returns the number
    of outbox rows added."""
    live = [(k, s) for k, s in members
            if not _notify_suppressed(ledger.db, k, now, th)]
    if not live:
        return 0
    text = (med_followup_group_notice([s for _, s in live])
            if len(live) > 1 else None)
    if len(live) == 1 or text is None:
        # singleton unit, or a member that can't render merged — fall
        # back to per-signal intents rather than losing a medication
        for key, sig in live:
            payload = {
                "text": signal_notice_text(sig),
                "signal_key": key, "type": sig["type"],
                "project_id": sig["project_id"],
                "evidence_fp": evidence_fp(sig["evidence"])}
            if _urgency_high(ledger.db, sig):
                payload["urgent"] = True
            ledger.outbox_add_tx("signal", sig["project_id"], payload)
        return len(live)
    sigs = [s for _, s in live]
    mids = sorted({m for s in sigs
                   for m in ((s.get("evidence") or {})
                             .get("message_ids") or [])
                   if type(m) is int})
    payload = {
        "text": text,
        "signal_keys": [k for k, _ in live],
        "type": "med_change_no_followup",
        "project_id": sigs[0]["project_id"],
        "evidence_fp": evidence_fp(
            {"meds": [s["evidence"]["med"] for s in sigs],
             "message_ids": mids})}
    if any(_urgency_high(ledger.db, s) for s in sigs):
        payload["urgent"] = True
    ledger.outbox_add_tx("signal", sigs[0]["project_id"], payload)
    return 1


def dismiss_reason_counts(db, project_id=None, *, since=None, until=None,
                          as_of=None) -> dict:
    """Human dismissals per signal type and reason code — every
    'dismissed' transition counts once; rows from before reason codes
    existed count as 'unclassified'. Read-only; no actor or free text.
    A dismissal is a label, not proof the signal was wrong."""
    from mcs_operations import DISMISS_REASON_CODES
    out: dict = {}
    where, params = (" AND project_id=?", (ARTIFACT_KIND, project_id)) if project_id is not None else (
        "", (ARTIFACT_KIND,))
    for (content_s,) in db.execute(
            "SELECT content FROM artifacts WHERE kind=? AND json_valid(content)"
            " AND json_extract(content,'$.state')='dismissed'" + where, params):
        c = json.loads(content_s)
        if not isinstance(c, dict):
            continue
        if since is not None or until is not None or as_of is not None:
            at = c.get("dismissed_at")
            if (not isinstance(at, (int, float)) or isinstance(at, bool)
                    or not 0 <= at < 1e12
                    or (since is not None and at < since)
                    or (until is not None and at >= until)
                    or (as_of is not None and at > as_of)):
                continue
        code = c.get("dismiss_reason_code")
        if code not in DISMISS_REASON_CODES:
            code = "unclassified"
        by_type = out.setdefault(str(c.get("type") or "unknown"), {})
        by_type[code] = by_type.get(code, 0) + 1
    return out


def current_open(db, project_id=None, limit=50):
    """Read-side listing used by mcs_view — open signals with evidence
    ids. Runs on the snapshot connection; no state is touched. The
    latest row per meta.key is the signal's current state."""
    latest = _latest_signal_states(db)
    items = [{"key": k, "type": c.get("type"),
              "project_id": c.get("project_id"),
              "detected_at": c.get("detected_at"),
              "evidence": c.get("evidence"),
              "context": c.get("context"),
              "note": c.get("note")}
             for k, c in latest.items()
             if c is not None and c["state"] == "open"
             and (project_id is None or c["project_id"] == project_id)]
    items.sort(key=lambda i: -(i["detected_at"] or 0))
    last_run = db.execute(
        "SELECT MAX(finished_at) FROM runs WHERE status != 'failed'"
    ).fetchone()[0]
    approved = _approved_policy(db)
    policy = None
    if approved is not None:
        aid, pc = approved
        policy = {"artifact_id": aid, "actor": pc["actor"],
                  "approved_at": pc.get("approved_at"),
                  "overrides": pc.get("policy")}
    return {"total": len(items), "returned": min(len(items), limit),
            "truncated": len(items) > limit, "items": items[:limit],
            "pipeline_last_run_at": last_run,
            "thresholds": _thresholds(db), "policy": policy}
