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
import time
from datetime import datetime

from mcs_queries import (CHANGE_ACTIONS, CHANGE_ACTIONS_SQL, DAY_S, JST,
                         MED_NOT_CAPABILITY_SQL, MED_PATIENT_CURRENT_SQL,
                         TRANSITION_EVENTS_SQL, current_fact_pred,
                         iter_period_ends, med_capability_evidence,
                         med_is_patient_current, med_period_artifacts,
                         transition_cooccurrences)

ARTIFACT_KIND = "signal_v1"

FOLLOWUP_DAYS = 7          # med_change_no_followup window
FOLLOWUP_MAX_AGE_D = 90    # only mentions within this horizon — older
                           # ones are historical, not prospective
CONC_WINDOW_H = 72         # comm_concentration window
CONC_MIN_POSTS = 10        # comm_concentration threshold
EXPIRY_AHEAD_DAYS = 14     # rx_period_expiry horizon
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
    "req_age_days":             (REQ_AGE_DAYS,             7, 365),
    "transition_lookback_d":    (TRANSITION_LOOKBACK_D,    7, 365),
    "transition_med_window_d":  (TRANSITION_MED_WINDOW_D,  1,  60),
    "notify_cooldown_d":        (NOTIFY_COOLDOWN_S // DAY_S, 1, 90),
    "request_response_days":    (REQ_RESPONSE_DAYS,        1,  30),
    "fyi_max_age_d":            (FYI_MAX_AGE_D,            7, 180),
}


def _thresholds(db):
    """Resolved thresholds: defaults overlaid by the latest approved
    signal_policy_v1 artifact. Malformed/out-of-range values in the
    artifact are ignored per key rather than failing the whole run."""
    th = {name: default for name, (default, lo, hi)
          in THRESHOLDS.items()}
    row = db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND "
        "json_valid(content) ORDER BY artifact_id DESC LIMIT 1",
        (POLICY_KIND,)).fetchone()
    if row is None:
        return th
    content = json.loads(row["content"])
    # provenance required: a policy only counts when it arrived through
    # the human-confirmed command path (command_id + actor recorded by
    # _apply_signal_policy_tx). A bare artifact insert cannot move
    # thresholds — same identity boundary as the command envelope.
    if not (isinstance(content, dict)
            and isinstance(content.get("command_id"), str)
            and content["command_id"]
            and isinstance(content.get("actor"), str)
            and content["actor"]):
        return th
    policy = content.get("policy")
    if isinstance(policy, dict):
        for name, (default, lo, hi) in THRESHOLDS.items():
            v = policy.get(name)
            if type(v) is int and lo <= v <= hi:
                th[name] = v
    return th


def _key(type_, pid, anchor):
    return f"{type_}:{pid}:{anchor}"


SELF_PROFILE_KIND = "self_profile_v1"


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
    doc = {"sender_id": prof.get("sender_id"), "name": prof.get("name"),
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
        f" AND ({' OR '.join(pred)}) LIMIT 1",
        (pid, ts, *params)).fetchone() is not None


def _request_registered(db, mid):
    """A registered request on the mention message is visible
    engagement — someone already turned it into a tracked item."""
    return db.execute(
        "SELECT 1 FROM requests WHERE source_message_id=? LIMIT 1",
        (mid,)).fetchone() is not None


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
        f"""WITH med_msgs AS (
                SELECT m.project_id AS pid, m.message_id AS mid,
                       m.posted_at_ts AS ts,
                       TRIM(json_extract(je.value,'$.name')) AS med
                FROM artifacts a
                JOIN messages m ON m.message_id=a.message_id
                JOIN patients p ON p.project_id=m.project_id
                JOIN json_each(a.content,'$.meds') je
                WHERE a.kind IN ('extract_llm','canonical_projection')
                  {current_fact_pred()}
                  AND m.posted_at_ts IS NOT NULL
                  AND m.posted_at_ts >= ?
                  AND COALESCE(p.is_archived,0)=0
                  {self_pred}
                  AND json_extract(je.value,'$.action')
                      IN ({CHANGE_ACTIONS_SQL})
                  AND json_type(je.value,'$.name')='text'
                  AND TRIM(json_extract(je.value,'$.name'))!=''
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


def _rx_period_expiry(db, now, th, sig_cfg):
    """extract_v1 med_periods whose end date lands within the horizon.
    These are parsed surface expressions (e.g. '4/8-4/21'), not
    verified prescription periods. Scans all current artifacts — the
    horizon is relative to now, so no incremental watermark applies."""
    today = datetime.fromtimestamp(now, JST).date()
    seen = set()
    for pid, mid, content in med_period_artifacts(db):
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
PHARM_TARGET_SQL = ("json_extract(je.value,'$.to') LIKE '%薬剤師%' "
                    "OR json_extract(je.value,'$.to') LIKE '%薬局%' "
                    "OR json_extract(je.value,'$.to') LIKE '%調剤%'")


def _pharmacist_request(db, now, th, sig_cfg):
    """extract_llm requests addressed to the pharmacy (a pharmacist-role
    target or configured request_targets) whose mention passed the
    response window with no visible responder post — 'no response could
    be confirmed on the record', never 'ignored'. Response = a post by
    self_professions/self_organizations or a registered request."""
    orgs, profs, targets = _self_sets(sig_cfg, db)
    # request_targets adds exact spellings like 「〇〇薬局さま」.
    # Empty/不明 targets never count as pharmacist-addressed.
    tgt_pred = (f" OR json_extract(je.value,'$.to') IN "
                f"({','.join('?' * len(targets))})") if targets else ""
    rows = db.execute(
        f"""SELECT m.project_id, m.message_id, m.posted_at_ts,
                   json_extract(je.value,'$.action') AS act
            FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            JOIN patients p ON p.project_id=m.project_id
            JOIN json_each(a.content,'$.requests') je
            WHERE a.kind IN ('extract_llm','canonical_projection')
              {current_fact_pred()}
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts >= ?
              AND m.posted_at_ts <= ?
              AND COALESCE(p.is_archived,0)=0
              AND COALESCE(json_extract(je.value,'$.unverified'),0)!=1
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
                   json_extract(je.value,'$.to') AS rto,
                   json_extract(je.value,'$.action') AS act
            FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            JOIN patients p ON p.project_id=m.project_id
            JOIN json_each(a.content,'$.requests') je
            WHERE a.kind IN ('extract_llm','canonical_projection')
              {current_fact_pred()}
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts >= ?
              AND COALESCE(p.is_archived,0)=0
              AND COALESCE(json_extract(je.value,'$.unverified'),0)!=1
              AND NOT ({PHARM_TARGET_SQL})
              AND COALESCE(json_extract(je.value,'$.to'),'')
                  NOT IN ('','不明'{extra})
              AND (json_extract(je.value,'$.action') LIKE '%処方%'
                   OR json_extract(je.value,'$.action') LIKE '%薬%'
                   OR json_extract(je.value,'$.action') LIKE '%内服%'
                   OR json_extract(je.value,'$.action') LIKE '%残薬%'
                   OR json_extract(je.value,'$.action') LIKE '%一包化%')
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
                      "服薬管理が難し", "服薬管理でき", "管理できな")
# capability claims ending in a negation ARE the concern — the tail
# check would eat the ない that completes them, so these match as-is
ADHERENCE_TERMINAL = ("管理は出来ない", "管理はできない",
                      "管理は出来ません", "管理はできません",
                      "管理が出来ない", "管理ができない",
                      "管理が出来ません", "管理ができません")
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
                   TRIM(json_extract(je.value,'$.name')) AS med
            FROM artifacts a
            JOIN messages m ON m.message_id=a.message_id
            JOIN patients p ON p.project_id=m.project_id
            JOIN json_each(a.content,'$.meds') je
            WHERE a.kind IN ('extract_llm','canonical_projection')
              {current_fact_pred()}
              AND m.posted_at_ts IS NOT NULL
              AND m.posted_at_ts >= ?
              AND COALESCE(p.is_archived,0)=0
              {self_pred}
              AND json_type(je.value,'$.name')='text'
              AND TRIM(json_extract(je.value,'$.name'))!=''
              AND COALESCE(json_extract(je.value,'$.subject'),
                           'patient')='patient'
              AND COALESCE(json_extract(je.value,'$.status'),
                           'current')!='past'
              AND (json_extract(je.value,'$.negated') IS 1
                   OR NOT {MED_NOT_CAPABILITY_SQL})
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
            JOIN json_each(da.content,'$.events') ev
            WHERE da.kind IN ('extract_llm','canonical_projection')
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
            WHERE a.kind IN ('extract_llm','canonical_projection')
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
        meds = [m2["name"].strip() for m2 in content.get("meds") or []
                if isinstance(m2, dict)
                and m2.get("action") in CHANGE_ACTIONS
                and med_is_patient_current(m2)
                and not med_capability_evidence(m2.get("evidence"))
                and isinstance(m2.get("name"), str) and m2["name"].strip()]
        if not meds:
            continue
        symps = [s["text"].strip() for s in content.get("symptoms") or []
                 if isinstance(s, dict) and not s.get("negated")
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

    # latest parseable state row per key — artifacts are append-only,
    # so artifact_id order is the lifecycle order
    existing = {}
    for aid, content_s, meta_s in ledger.db.execute(
            "SELECT artifact_id, content, meta FROM artifacts "
            "WHERE kind=? ORDER BY artifact_id",
            (ARTIFACT_KIND,)).fetchall():
        try:
            meta = json.loads(meta_s or "{}")
            content = json.loads(content_s or "{}")
        except (json.JSONDecodeError, TypeError):
            continue             # opaque row: skipped, not fatal
        if (isinstance(meta, dict) and meta.get("key")
                and isinstance(content, dict)
                and content.get("state") in ("open", "resolved",
                                             "dismissed")):
            existing[meta["key"]] = content

    opened = superseded = resolved = enqueued = dig_merged = 0
    newly = []
    with ledger.db:
        for key, sig in current.items():
            old = existing.get(key)
            if old is None or old["state"] == "resolved" or (
                    old["state"] == "dismissed"
                    and old.get("evidence") != sig["evidence"]):
                # new signal, condition returned after resolution, or a
                # human dismissed an earlier evidence set that has since
                # changed — a genuinely new situation, open again
                sig.update(v=1, state="open", detected_at=now,
                           resolved_at=None)
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
                           resolved_at=None)
                _insert(ledger.db, key, sig)
                superseded += 1
            # else: still open with identical evidence — nothing to write
        for key, old in existing.items():
            # resolve only types that actually ran this evaluation —
            # an unfinished or crashed detector must never turn
            # "not inspected" into a recorded "resolved" (the ledger
            # history is a review record, not a guess)
            if (key not in current and old["state"] == "open"
                    and old.get("type") in ran_types):
                row = dict(old, state="resolved", resolved_at=now)
                row.pop("reopened_at", None)
                _insert(ledger.db, key, row)
                resolved += 1
        if notify:
            enqueued, dig_merged = _notify_opened(
                ledger, newly, now, th, sig_cfg)
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
    return (f"[MCS] レビュー候補 ({sig['type']})\n"
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
    return (f"[MCS] レビュー候補 ({sig['type']})\n"
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
    immediate delivery. Mirrors notifier._urgency: either extractor
    kind (rule extract_v1 or extract_llm) can carry the flag."""
    ev = sig.get("evidence") or {}
    mids = ev.get("message_ids")
    mid = ((mids[-1] if isinstance(mids, list) and mids else None)
           or ev.get("discharge_message_id"))
    if type(mid) is not int:
        return False
    for kind in ("extract_llm", "extract_v1"):
        row = db.execute(
            "SELECT content FROM artifacts WHERE message_id=? "
            "AND kind=? ORDER BY artifact_id DESC LIMIT 1",
            (mid, kind)).fetchone()
        if not row or not row["content"]:
            continue
        try:
            doc = json.loads(row["content"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(doc, dict) and doc.get("urgency") == "high":
            return True
    return False


def _digest_text(n):
    return f"[MCS] レビュー候補ダイジェスト（{n}件）"


def open_signal_rows(db, keys):
    """signal_keys -> [(key, latest content)] for keys still open. The
    send path's member check and the digest-rescue path share this —
    'open' is always the latest artifact row's state, never the frozen
    payload's."""
    out = []
    for k in keys:
        row = db.execute(
            """SELECT content FROM artifacts
               WHERE kind='signal_v1' AND json_valid(meta)
                 AND json_valid(content)
                 AND json_extract(meta,'$.key')=?
               ORDER BY artifact_id DESC LIMIT 1""", (k,)).fetchone()
        try:
            s = json.loads(row["content"]) if row and row["content"] \
                else {}
        except (json.JSONDecodeError, TypeError):
            s = {}
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
             AND json_valid(payload)
             AND json_extract(payload,'$.digest')=1
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
    interval_h = (ih if type(ih) in (int, float) and ih > 0
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
    for key, sig in dig:
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


def current_open(db, project_id=None, limit=50):
    """Read-side listing used by mcs_view — open signals with evidence
    ids. Runs on the snapshot connection; no state is touched. The
    latest row per meta.key is the signal's current state."""
    params = [ARTIFACT_KIND]
    sql = ("SELECT artifact_id, project_id, content, meta "
           "FROM artifacts WHERE kind=? AND json_valid(content) "
           "AND json_valid(meta)")
    if project_id is not None:
        sql += " AND project_id=?"
        params.append(project_id)
    sql += " ORDER BY artifact_id"
    latest = {}
    for aid, pid, content_s, meta_s in db.execute(sql, params):
        meta = json.loads(meta_s)
        content = json.loads(content_s)
        if (isinstance(meta, dict) and isinstance(content, dict)
                and meta.get("key")):
            latest[meta["key"]] = content
    items = [{"key": k, "type": c.get("type"),
              "project_id": c.get("project_id"),
              "detected_at": c.get("detected_at"),
              "evidence": c.get("evidence"),
              "context": c.get("context"),
              "note": c.get("note")}
             for k, c in latest.items() if c.get("state") == "open"]
    items.sort(key=lambda i: -(i["detected_at"] or 0))
    last_run = db.execute(
        "SELECT MAX(finished_at) FROM runs WHERE status != 'failed'"
    ).fetchone()[0]
    prow = db.execute(
        "SELECT artifact_id, content FROM artifacts WHERE kind=? AND "
        "json_valid(content) ORDER BY artifact_id DESC LIMIT 1",
        (POLICY_KIND,)).fetchone()
    policy = None
    if prow is not None:
        pc = json.loads(prow["content"])
        if (isinstance(pc, dict) and pc.get("command_id")
                and pc.get("actor")):
            policy = {"artifact_id": prow["artifact_id"],
                      "actor": pc["actor"],
                      "approved_at": pc.get("approved_at"),
                      "overrides": pc.get("policy")}
    return {"total": len(items), "returned": min(len(items), limit),
            "truncated": len(items) > limit, "items": items[:limit],
            "pipeline_last_run_at": last_run,
            "thresholds": _thresholds(db), "policy": policy}
