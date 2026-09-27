"""Shared extraction-scan contract for the read side.

mcs_stats (read-only statistics) and mcs_signals (prospective review
candidates) must agree on which artifact counts as the *current*
extraction of a message: valid JSON, hash-matched to the message body,
and — where the schema marks failures — not an error record. Both
readers used to carry private copies of these predicates; this module
keeps one definition so they cannot drift.

Constants, SQL fragments, and row iterators only — the module holds no
connection and does no I/O of its own (same contract as mcs_util).
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
DAY_S = 86400

# extract_llm med `action` values meaning "the prescription changed"
# ('none' = mentioned without a change — counted in stats, never a
# signal). Kept in one place so both readers share the same change set.
CHANGE_ACTIONS = ("start", "stop", "change", "increase", "decrease")
MED_ACTIONS = CHANGE_ACTIONS + ("none",)
CHANGE_ACTIONS_SQL = ",".join(f"'{a}'" for a in CHANGE_ACTIONS)
TRANSITION_EVENTS_SQL = "'discharge','transfer'"


def current_extract_pred(art: str = "a", msg: str = "m", *,
                         content: bool = True,
                         error_check: bool = True) -> str:
    """AND-fragment: the artifact row is the message's current valid
    extraction. json_valid MUST gate every json_extract — one malformed
    meta/content row would otherwise raise and kill the whole query.

    `content=False` is for the "parsed" definition in data_quality:
    an extraction attempt counts as parsed even when its content did
    not survive JSON round-trip. `error_check=False` is for extract_v1,
    which does not mark failures in meta."""
    parts = []
    if content:
        parts.append(f"json_valid({art}.content)")
    parts.append(f"json_valid({art}.meta)")
    if error_check:
        parts.append(f"COALESCE(json_extract({art}.meta,'$.error'),0)=0")
    parts.append(f"json_extract({art}.meta,'$.hash')={msg}.content_hash")
    return " AND " + " AND ".join(parts)


CANONICAL_PROJECTION_KIND = "canonical_projection"
# T18: the PASS-only v4 read model — a distinct kind the legacy
# extract_llm `_replace_current` DELETE can never reach. When a
# current v4 row exists it outranks both older kinds.
V4_PROJECTION_KIND = "semantic_facts_v4"
FACT_KINDS_SQL = ("'extract_llm','canonical_projection',"
                  "'semantic_facts_v4'")


def qc_source_id(msg: str = "m", *, version: int) -> str:
    """Newest valid extraction for the QC generation being evaluated."""
    if type(version) is not int or version < 0:
        raise ValueError("extract_version_invalid")
    return ("(SELECT MAX(qx.artifact_id) FROM artifacts qx"
            f" WHERE qx.kind='extract_llm' AND qx.message_id={msg}.message_id"
            f" {current_extract_pred('qx', msg)}"
            " AND json_type(qx.content)='object'"
            f" AND json_extract(qx.meta,'$.extract_version')={version})")


def current_qc_pred(art: str = "a", msg: str = "m", *, version: int) -> str:
    """QC belongs to an exact extraction, including same-body re-extraction."""
    return (current_extract_pred(art, msg)
            + f" AND json_extract({art}.meta,'$.extract_version')={version}"
              f" AND json_extract({art}.meta,'$.source_artifact_id')="
              f"{qc_source_id(msg, version=version)}")


def _projection_pred(art: str, hash_ref: str, *,
                     engine_version: int | None = None) -> str:
    """Build the shared current-row predicate for both projection kinds."""
    col = f"{art}." if art else ""
    return (f"CASE WHEN json_valid({col}content) AND json_valid({col}meta) "
            f"THEN json_type({col}content)='object' "
            f"AND json_type({col}meta)='object' "
            f"AND COALESCE(json_extract({col}content,'$._error'),0)=0 "
            f"AND COALESCE(json_extract({col}meta,'$.error'),0)=0 "
            f"AND json_extract({col}meta,'$.hash')={hash_ref} "
            + (f"AND json_extract({col}meta,'$.engine_version')="
               f"{engine_version} " if engine_version is not None else "")
            + f"AND COALESCE(json_extract({col}meta,'$.invalidated'),0)=0 "
            "ELSE 0 END")


def current_projection_pred(art: str = "c", hash_ref: str = "?") -> str:
    """Predicate: a canonical_projection row is usable right now —
    valid payloads, no error, hash-current to the source message, and not
    superseded by a later source save (meta.invalidated). Shared by
    current_projection_id and mcs_requests.candidates."""
    return _projection_pred(art, hash_ref)


def current_projection_id(msg: str = "m") -> str:
    """Subquery: THE current canonical-projection row for a message —
    the newest valid artifact_id among hash-current, unexpired projections.
    The writer expires source/context/policy generations explicitly; a
    newer projection of the same generation supersedes older ones, and a new
    EMPTY projection legitimately replaces an old non-empty one (C06).
    Returns the artifact_id or NULL."""
    return (f"(SELECT MAX(c.artifact_id) FROM artifacts c"
            f" WHERE c.kind='{CANONICAL_PROJECTION_KIND}'"
            f" AND c.message_id={msg}.message_id"
            f" AND c.project_id={msg}.project_id"
            f" AND {current_projection_pred('c', f'{msg}.content_hash')})")


def current_v4_pred(art: str = "v", hash_ref: str = "?") -> str:
    """Predicate: a semantic_facts_v4 row is usable — same contract as
    current_projection_pred plus the engine version pin."""
    return _projection_pred(art, hash_ref, engine_version=4)


def current_v4_id(msg: str = "m") -> str:
    """Subquery: THE current v4 read-model row for a message."""
    return (f"(SELECT MAX(v.artifact_id) FROM artifacts v"
            f" WHERE v.kind='{V4_PROJECTION_KIND}'"
            f" AND v.message_id={msg}.message_id"
            f" AND v.project_id={msg}.project_id"
            f" AND {current_v4_pred('v', f'{msg}.content_hash')})")


def qc_v4_source_id(msg: str = "m") -> str:
    """The v4 row a QC audit binds to. Deliberately separate from
    qc_source_id: a bare ``version=4`` on the legacy extract_llm scan
    can never select this kind — the consumer must opt in."""
    return (f"(SELECT MAX(v.artifact_id) FROM artifacts v"
            f" WHERE v.kind='{V4_PROJECTION_KIND}'"
            f" AND v.message_id={msg}.message_id"
            f" AND {current_v4_pred('v', f'{msg}.content_hash')}"
            f" AND json_extract(v.meta,'$.extract_version')=4)")


def current_fact_pred(art: str = "a", msg: str = "m", *,
                      error_check: bool = True) -> str:
    """AND-fragment for fact-extraction reads: v4 PASS outranks the
    canonical projection, which outranks a legacy ``extract_llm`` row;
    each must satisfy current_extract_pred. A delayed v3 writer can
    still land a row but can never displace a published v4
    generation. The caller's FROM clause must admit all three kinds —
    ``<art>.kind IN ('extract_llm','canonical_projection',
    'semantic_facts_v4')``."""
    return (current_extract_pred(art, msg, error_check=error_check)
            + f" AND CASE WHEN {art}.kind='{V4_PROJECTION_KIND}'"
              f" THEN {art}.artifact_id={current_v4_id(msg)}"
              f" WHEN {art}.kind='{CANONICAL_PROJECTION_KIND}'"
              f" THEN {art}.artifact_id="
              f"{current_projection_id(msg)}"
              f" AND {current_v4_id(msg)} IS NULL"
              f" ELSE {current_projection_id(msg)} IS NULL"
              f" AND {current_v4_id(msg)} IS NULL END")


def med_period_artifacts(db):
    """Current extract_v1 artifacts carrying med_periods — the only
    source of period expressions today."""
    return db.execute(
        "SELECT m.project_id, m.message_id, a.content "
        "FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        "WHERE a.kind='extract_v1'"
        f"{current_extract_pred(error_check=False)} "
        "AND json_array_length(a.content,'$.med_periods')>0").fetchall()


def med_is_patient_current(med) -> bool:
    """extract_llm med dict predicate: the mention counts as the
    patient's own actionable medication — not negated, not another
    person's, not a historical (past-status) report. Mirrors
    MED_PATIENT_CURRENT_SQL so Python-side stats agree with SQL-side
    filters (med_change_no_followup / transition co-occurrence)."""
    return isinstance(med, dict) \
        and not med.get("negated") \
        and not med.get("unverified") \
        and med.get("subject", "patient") == "patient" \
        and med.get("status", "current") != "past"


# SQL twin of med_is_patient_current, evaluated inside
# json_each(a.content,'$.meds') — the alias `je` is part of the
# contract. `IS NOT 1` matches `not med.get("negated")` because
# _validate admits only strict booleans (JSON true -> SQLite 1);
# absent keys on pre-v2 rows read as NULL and pass.
MED_PATIENT_CURRENT_SQL = (
    "json_extract(je.value,'$.negated') IS NOT 1 "
    "AND json_extract(je.value,'$.unverified') IS NOT 1 "
    "AND COALESCE(json_extract(je.value,'$.subject'),"
    "'patient')='patient' "
    "AND COALESCE(json_extract(je.value,'$.status'),"
    "'current')!='past'")

# Evidence spans reading as capability/feasibility statements rather
# than actual prescription events — the LLM sometimes maps
# 「〜は出来ない」「〜管理はできない」 onto a change action (stop etc.).
# Change-claim readers (signals, transition stats) exclude these; the
# adherence_concern detector picks the same mentions up as a
# different, honestly-labelled signal.
MED_CAPABILITY_PATTERNS = ("出来ない", "できない", "出来ません", "できません")
MED_NOT_CAPABILITY_SQL = " AND ".join(
    f"COALESCE(json_extract(je.value,'$.evidence'),'') NOT LIKE '%{pattern}%'"
    for pattern in MED_CAPABILITY_PATTERNS)


def med_capability_evidence(ev) -> bool:
    """Python twin of `NOT MED_NOT_CAPABILITY_SQL` — True when the
    evidence span is a capability statement, not a prescription event."""
    return isinstance(ev, str) and any(p in ev
                                       for p in MED_CAPABILITY_PATTERNS)


def iter_period_ends(content: str):
    """(period_dict, end_date) for each med_periods entry whose 'end'
    parses as YYYY-MM-DD. Undated or unparseable entries are skipped —
    a period that cannot be dated cannot be evaluated."""
    for p in (json.loads(content).get("med_periods") or []):
        if not isinstance(p, dict) or not p.get("end"):
            continue
        try:
            end_d = datetime.strptime(str(p["end"]), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        yield p, end_d


def transition_cooccurrences(db, *, win_s: int, extra_where: str = "",
                             params=(), exclude_archived: bool = False):
    """{discharge_mid: (project_id, {med_mid, ...})} — typed
    discharge/transfer extract_llm events co-occurring with a med
    change-action mention within ±win_s seconds in the same room.

    CROSS JOIN pins messages-first order: letting SQLite start from the
    unbounded artifacts side made this scan ~12s on the real ledger.
    `extra_where` appends caller-specific filters (signal lookback vs
    stats scope) — both reference the d/m aliases below."""
    join_p = ("CROSS JOIN patients p ON p.project_id=d.project_id "
              if exclude_archived else "")
    pred_p = ("AND COALESCE(p.is_archived,0)=0 "
              if exclude_archived else "")
    rows = db.execute(
        f"""SELECT DISTINCT d.project_id, d.message_id, m.message_id
            FROM messages d
            {join_p}CROSS JOIN artifacts da ON da.message_id=d.message_id
            CROSS JOIN json_each(da.content,'$.events') ev
            CROSS JOIN messages m ON m.project_id=d.project_id
                 AND m.posted_at_ts BETWEEN d.posted_at_ts-?
                                        AND d.posted_at_ts+?
            CROSS JOIN artifacts a ON a.message_id=m.message_id
            WHERE da.kind IN ('extract_llm','{CANONICAL_PROJECTION_KIND}',
                              '{V4_PROJECTION_KIND}')
              {current_fact_pred('da', 'd')}
              AND ev.value IN ({TRANSITION_EVENTS_SQL})
              AND a.kind IN ('extract_llm','{CANONICAL_PROJECTION_KIND}',
                             '{V4_PROJECTION_KIND}')
              {current_fact_pred('a', 'm')}
              {pred_p}AND EXISTS
                  (SELECT 1 FROM json_each(a.content,'$.meds') je
                   WHERE json_extract(je.value,'$.action')
                       IN ({CHANGE_ACTIONS_SQL})
                     AND {MED_PATIENT_CURRENT_SQL}
                     AND {MED_NOT_CAPABILITY_SQL})
              {extra_where}
            ORDER BY d.message_id, m.message_id""",
        (win_s, win_s, *params)).fetchall()
    grouped = {}
    for pid, dmid, mid in rows:
        grouped.setdefault(dmid, (pid, set()))[1].add(mid)
    return grouped


def staff_directory(db, project_id=None):
    """Name -> facility directory of senders observed in MCS posts.

    One row per (sender_name, organization) pair — a person posting
    from several facilities appears once per facility, and pharmacy
    staff surface under their pharmacy name in ``organization`` (the
    sender's MCS ``stations`` list), which is what links them to the
    facility they serve. `project_id` scopes the directory to one
    patient's room; None covers every project."""
    where = "sender_name IS NOT NULL AND sender_name != ''"
    params = []
    if project_id is not None:
        where += " AND project_id=?"
        params.append(project_id)
    return db.execute(
        f"""SELECT sender_name, profession, organization,
                   COUNT(*) messages, COUNT(DISTINCT project_id) projects,
                   MIN(posted_at) first_seen, MAX(posted_at) last_seen
            FROM messages WHERE {where}
            GROUP BY sender_name, organization
            ORDER BY last_seen DESC""", params).fetchall()


def resolve_staff(db, name, project_id=None):
    """Canonical ``name（facility）`` for a free-text staff reference.

    The link materializes only when the directory resolves the name to
    exactly one facility within the scope — an ambiguous or unknown
    name is returned unchanged rather than guessing. A name already
    carrying a ``（…）`` annotation is treated as resolved so re-saves
    stay idempotent."""
    base = (name or "").strip()
    if not base or "（" in base:
        return base or None
    orgs = {r["organization"].strip() for r in staff_directory(db, project_id)
            if r["sender_name"].strip() == base and r["organization"]}
    if len(orgs) == 1:
        return f"{base}（{next(iter(orgs))}）"
    return base
