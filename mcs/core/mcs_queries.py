"""Shared ledger and extraction read contracts on borrowed connections.

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
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from mcs_util import loads_dict, search_fold
from patient_context import merged_context
from semantic_projection import PROJECTION_VERSION

JST = ZoneInfo("Asia/Tokyo")
DAY_S = 86400
UNKNOWN_TIME = -(2**63)

# extract_llm med `action` values meaning "the prescription changed"
# ('none' = mentioned without a change — counted in stats, never a
# signal). Kept in one place so both readers share the same change set.
CHANGE_ACTIONS = ("start", "stop", "change", "increase", "decrease")
MED_ACTIONS = CHANGE_ACTIONS + ("none",)
CHANGE_ACTIONS_SQL = ",".join(f"'{a}'" for a in CHANGE_ACTIONS)
TRANSITION_EVENTS_SQL = "'discharge','transfer'"

# a message (alias m) whose full body is stored and non-empty — the
# extraction eligibility rule shared by writers, coverage and read models.
# Job admission, retry timing and extraction generations are separate filters.
ELIGIBLE_MESSAGE = ("m.body_text IS NOT NULL AND m.body_text != '' "
                    "AND (m.body_state IS NULL OR m.body_state='full')")


def json_or_null(col: str) -> str:
    """JSON document argument that is NULL unless `col` is valid JSON.
    JSON functions outside current_extract_pred's CASE take their input
    through this, so a malformed row cannot raise under any join order."""
    return f"CASE WHEN json_valid({col}) THEN {col} END"


# `progress=` assignment that records a stable hold code (one `?`) while
# keeping any existing receipt; an unparseable receipt is preserved under
# invalid_progress, as the text-route hold does.
HOLD_PROGRESS_SET = (
    "progress=json_set(CASE WHEN progress IS NULL OR progress='' THEN '{}' "
    "WHEN json_valid(progress) AND json_type(progress)='object' "
    "THEN progress ELSE json_object('invalid_progress',progress) END,"
    "'$.hold_reason',?)")


def json_object_or_null(col: str) -> str:
    """Like json_or_null, but also NULL unless `col` is a JSON object."""
    return (f"CASE WHEN json_valid({col}) AND json_type({col})='object' "
            f"THEN {col} END")


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
    guards = [f"json_valid({art}.meta)"]
    parts = [f"json_type({art}.meta)='object'"]
    if content:
        guards.append(f"json_valid({art}.content)")
        parts.append(f"json_type({art}.content)='object'")
    if error_check:
        parts.append(f"COALESCE(json_extract({art}.meta,'$.error'),0)=0")
    parts.append(f"json_extract({art}.meta,'$.hash')={msg}.content_hash")
    # Legacy message artifacts may omit project_id (the public ledger API
    # permits it); their globally unique message_id supplies the scope.
    # An explicitly conflicting project must never supply this patient's facts.
    return (f" AND ({art}.project_id IS NULL OR {art}.project_id={msg}.project_id)"
            f" AND {msg}.body_state IS NOT 'deleted'"
            " AND CASE WHEN " + " AND ".join(guards)
            + " THEN " + " AND ".join(parts) + " ELSE 0 END")


CANONICAL_PROJECTION_KIND = "canonical_projection"
# T18: the PASS-only v4 read model — a distinct kind the legacy
# extract_llm `_replace_current` DELETE can never reach. When a
# current v4 row exists it outranks both older kinds. "v4" here is the
# semantic engine version, unrelated to mcs/extract/llm (extract_llm);
# always filter by kind, never by a bare version number.
V4_PROJECTION_KIND = "semantic_facts_v4"
# human ⚠ report on one extraction (card button -> ops.extract_feedback)
EXTRACT_FEEDBACK_KIND = "extract_feedback_v1"
FACT_KINDS_SQL = ("'extract_llm','canonical_projection',"
                  "'semantic_facts_v4'")


def qc_source_id(msg: str = "m", *, version: int) -> str:
    """Newest valid extraction for the QC generation being evaluated."""
    if type(version) is not int or version < 0:
        raise ValueError("extract_version_invalid")
    return ("(SELECT MAX(qx.artifact_id) FROM artifacts qx"
            f" WHERE qx.kind='extract_llm' AND qx.message_id={msg}.message_id"
            f" {current_extract_pred('qx', msg)}"
            f" AND json_type({json_or_null('qx.content')})='object'"
            f" AND json_extract({json_or_null('qx.meta')},"
            f"'$.extract_version')={version})")


def current_qc_pred(art: str = "a", msg: str = "m", *, version: int) -> str:
    """QC belongs to an exact extraction, including same-body re-extraction."""
    meta = json_or_null(f"{art}.meta")
    return (current_extract_pred(art, msg)
            + f" AND json_extract({meta},'$.extract_version')={version}"
              f" AND json_extract({meta},'$.source_artifact_id')="
              f"{qc_source_id(msg, version=version)}")


def _projection_pred(art: str, hash_ref: str, *,
                     engine_version: int | None = None,
                     require_version: bool = True) -> str:
    """Build the shared current-row predicate for both projection kinds."""
    from semantic_projection import PROJECTION_VERSION
    col = f"{art}." if art else ""
    return (f"CASE WHEN json_valid({col}content) AND json_valid({col}meta) "
            f"THEN json_type({col}content)='object' "
            f"AND json_type({col}meta)='object' "
            f"AND COALESCE(json_extract({col}content,'$._error'),0)=0 "
            f"AND COALESCE(json_extract({col}meta,'$.error'),0)=0 "
            f"AND json_extract({col}meta,'$.hash')={hash_ref} "
            + (f"AND json_extract({col}meta,'$.engine_version')="
               f"{engine_version} " if engine_version is not None else "")
            + (f"AND json_extract({col}meta,'$.projection_version')="
               f"{PROJECTION_VERSION} " if require_version else "")
            + f"AND COALESCE(json_extract({col}meta,'$.invalidated'),0)=0 "
            "ELSE 0 END")


def current_projection_pred(art: str = "c", hash_ref: str = "?", *,
                            require_version: bool = True) -> str:
    """Predicate: a canonical_projection row is usable right now —
    valid payloads, no error, hash-current to the source message, and not
    superseded by a later source save (meta.invalidated), using the
    current projection version. Only bounded maintenance may omit the
    version check. Shared by current_projection_id and candidates."""
    return _projection_pred(art, hash_ref, require_version=require_version)


def current_projection_id(msg: str = "m", *, require_version: bool = True) -> str:
    """Subquery: THE current canonical-projection row for a message —
    the newest valid artifact_id among hash-current, unexpired projections.
    The writer expires source/context/policy generations explicitly; a
    newer projection of the same generation supersedes older ones, and a new
    EMPTY projection legitimately replaces an old non-empty one (C06).
    Returns the artifact_id or NULL."""
    pred = current_projection_pred("c", f"{msg}.content_hash",
                                   require_version=require_version)
    return (f"(SELECT MAX(c.artifact_id) FROM artifacts c"
            f" WHERE c.kind='{CANONICAL_PROJECTION_KIND}'"
            f" AND c.message_id={msg}.message_id"
            f" AND c.project_id={msg}.project_id"
            f" AND {pred})")


def current_v4_pred(art: str = "v", hash_ref: str = "?", *,
                    require_version: bool = True) -> str:
    """Predicate: a semantic_facts_v4 row is usable — same contract as
    current_projection_pred plus the engine version pin."""
    return _projection_pred(art, hash_ref, engine_version=4,
                            require_version=require_version)


def current_v4_id(msg: str = "m", *, require_version: bool = True) -> str:
    """Subquery: THE current v4 read-model row for a message."""
    pred = current_v4_pred("v", f"{msg}.content_hash",
                          require_version=require_version)
    return (f"(SELECT MAX(v.artifact_id) FROM artifacts v"
            f" WHERE v.kind='{V4_PROJECTION_KIND}'"
            f" AND v.message_id={msg}.message_id"
            f" AND v.project_id={msg}.project_id"
            f" AND {pred})")


def current_fact_pred(art: str = "a", msg: str = "m", *,
                      content: bool = True,
                      error_check: bool = True) -> str:
    """AND-fragment for fact-extraction reads: v4 PASS outranks the
    canonical projection, which outranks a legacy ``extract_llm`` row;
    each must satisfy current_extract_pred. A delayed v3 writer can
    still land a row but can never displace a published v4
    generation. The caller's FROM clause must admit all three kinds —
    ``<art>.kind IN ('extract_llm','canonical_projection',
    'semantic_facts_v4')``."""
    return (current_extract_pred(art, msg, content=content,
                                 error_check=error_check)
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
        f"AND json_array_length({json_or_null('a.content')},"
        "'$.med_periods')>0").fetchall()


def med_is_patient_current(med) -> bool:
    """extract_llm med dict predicate: the mention counts as the
    patient's own actionable medication — not negated, not another
    person's, not a historical (past-status) report. Mirrors
    MED_PATIENT_CURRENT_SQL so Python-side stats agree with SQL-side
    filters (med_change_no_followup / transition co-occurrence)."""
    return isinstance(med, dict) \
        and not med.get("negated") \
        and not item_unverified(med) \
        and med.get("subject", "patient") == "patient" \
        and med.get("status", "current") != "past"


# SQL twin of med_is_patient_current, evaluated inside
# json_each(a.content,'$.meds') — the alias `je` is part of the
# contract. `IS NOT 1` matches `not med.get("negated")` because
# _validate admits only strict booleans (JSON true -> SQLite 1);
# absent keys on pre-v2 rows read as NULL and pass. ``unverified`` is
# checked with ITEM_CONFIRMED_SQL instead, the fail-closed twin of
# item_unverified.
# Raw JSON strings from json_each are not JSON documents. Guard at the
# operand so safety does not depend on SQLite predicate evaluation order.
JSON_OBJECT_SQL = "CASE WHEN je.type='object' THEN je.value ELSE '{}' END"
# SQL twin of ``not item_unverified(item)``: missing or JSON false only.
ITEM_CONFIRMED_SQL = (f"COALESCE(json_type({JSON_OBJECT_SQL},'$.unverified'),"
                      "'false')='false'")
MED_PATIENT_CURRENT_SQL = (
    f"json_extract({JSON_OBJECT_SQL},'$.negated') IS NOT 1 "
    f"AND {ITEM_CONFIRMED_SQL} "
    f"AND COALESCE(json_extract({JSON_OBJECT_SQL},'$.subject'),"
    "'patient')='patient' "
    f"AND COALESCE(json_extract({JSON_OBJECT_SQL},'$.status'),"
    "'current')!='past'")

# Evidence spans reading as capability/feasibility statements rather
# than actual prescription events — the LLM sometimes maps
# 「〜は出来ない」「〜管理はできない」 onto a change action (stop etc.).
# Change-claim readers (signals, transition stats) exclude these; the
# adherence_concern detector picks the same mentions up as a
# different, honestly-labelled signal.
MED_CAPABILITY_PATTERNS = ("出来ない", "できない", "出来ません", "できません")
MED_NOT_CAPABILITY_SQL = " AND ".join(
    f"COALESCE(json_extract({JSON_OBJECT_SQL},'$.evidence'),'') NOT LIKE '%{pattern}%'"
    for pattern in MED_CAPABILITY_PATTERNS)


def med_capability_evidence(ev) -> bool:
    """Python twin of `NOT MED_NOT_CAPABILITY_SQL` — True when the
    evidence span is a capability statement, not a prescription event."""
    return isinstance(ev, str) and any(p in ev
                                       for p in MED_CAPABILITY_PATTERNS)


def item_unverified(r: dict) -> bool:
    """An extracted item (request/symptom/med) reads as confirmed only when
    its ``unverified`` flag is missing (pre-flag and rule rows) or
    literally False — any other value fails closed."""
    return r.get("unverified", False) is not False


def iter_period_ends(content: str):
    """(period_dict, end_date) for each med_periods entry whose 'end'
    parses as YYYY-MM-DD. Undated or unparseable entries are skipped —
    a period that cannot be dated cannot be evaluated."""
    try:
        doc = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        return
    periods = doc.get("med_periods") if isinstance(doc, dict) else None
    if not isinstance(periods, list):
        return
    for p in periods:
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

    CROSS JOIN pins messages-first order, preventing the planner from
    starting with the unbounded artifacts side.
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
            CROSS JOIN json_each({json_or_null('da.content')},'$.events') ev
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
                  (SELECT 1 FROM json_each({json_or_null('a.content')},
                                           '$.meds') je
                   WHERE json_extract({JSON_OBJECT_SQL},'$.action')
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


def karte_summary_row(db, project_id: int):
    """Newest summary row in the borrowed connection, including its metadata."""
    return db.execute(
        "SELECT artifact_id,content,meta FROM artifacts "
        "WHERE kind='karte_summary' AND project_id=? "
        "ORDER BY artifact_id DESC LIMIT 1", (project_id,)).fetchone()


def karte_summary_current(db, project_id: int) -> dict | None:
    """Newest stored 連携サマリー content plus fetched_at; None if never fetched."""
    row = karte_summary_row(db, project_id)
    if not row:
        return None
    try:
        content = json.loads(row["content"])
        meta = json.loads(row["meta"] or "{}")
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(content, dict):
        return None
    content["fetched_at"] = (meta.get("fetched_at")
                             if isinstance(meta, dict) else None)
    return content


def karte_summary_block(content: dict) -> dict:
    """The read-model shape one stored 連携サマリー content maps to —
    shared by the patient rollup and the 🧾 summary line so the two
    never drift on the same artifact."""
    updater = content.get("updater") \
        if isinstance(content.get("updater"), dict) else {}
    return {"comment": content.get("comment"),
            "updated_at": content.get("updated_at"),
            "updater_profession": updater.get("profession"),
            "empty": bool(content.get("empty"))}


def iter_artifacts(db, kind: str, project_id: int = None,
                   message_id: int = None, *, descending: bool = False):
    """Read artifact history incrementally in the requested ID order."""
    q = "SELECT * FROM artifacts WHERE kind=?"
    params: list = [kind]
    if project_id is not None:
        q += " AND project_id=?"
        params.append(project_id)
    if message_id is not None:
        q += " AND message_id=?"
        params.append(message_id)
    order = " DESC" if descending else ""
    return db.execute(q + " ORDER BY artifact_id" + order, params)


def latest_artifact(db, kind):
    """(artifact_id, content dict) of the newest artifact of kind; (None, None) when absent, content None when unreadable."""
    row = db.execute("SELECT artifact_id, content FROM artifacts WHERE kind=? "
                     "ORDER BY artifact_id DESC LIMIT 1", (kind,)).fetchone()
    return (row["artifact_id"], loads_dict(row["content"])) if row else (None, None)


SEMANTIC_CONTROL_KIND = "semantic_control"


def paused(db, project_id: int) -> bool:
    """Return the latest semantic control state, failing closed on corruption."""
    connection = getattr(db, "db", db)
    row = connection.execute(
        "SELECT content FROM artifacts WHERE kind=? AND project_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (SEMANTIC_CONTROL_KIND, project_id),
    ).fetchone()
    if row is None:
        return False
    return row["content"] != "running"


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


def feedback_current(h: str = "h") -> str:
    """EXISTS predicate: the extract_feedback_v1 row ``h`` still pins
    its message's current extract_llm extraction — no newer extraction
    replaced it and no v4 read model became current. The card's ⚠ mark
    and the report listing share this one definition."""
    return f"""EXISTS (SELECT 1 FROM artifacts a
                   JOIN messages m ON m.message_id=a.message_id
                   WHERE a.kind='extract_llm'
                     AND a.message_id={h}.message_id
                     AND a.artifact_id=json_extract(
                       {json_or_null(h + '.content')},'$.artifact_id')
                     {current_extract_pred('a', 'm')}
                     AND {current_v4_id('m')} IS NULL)"""


def extract_feedback(db, project_id=None, limit=100):
    """Human ⚠ extraction reports, newest first — each pins the
    extract_llm artifact it judged (content: message_id, artifact_id,
    hash, field, note, actor, at). ``current`` is 1 while that artifact
    is still the message's extraction (the report awaits its one
    re-extract), 0 once a newer extraction replaced it."""
    where, params = "", []
    if project_id is not None:
        where, params = " AND h.project_id=?", [project_id]
    return db.execute(
        f"""SELECT h.artifact_id, h.project_id, h.message_id, h.content,
                   h.created_at, {feedback_current('h')} AS current
            FROM artifacts h WHERE h.kind='{EXTRACT_FEEDBACK_KIND}'{where}
            ORDER BY h.artifact_id DESC LIMIT ?""",
        [*params, limit]).fetchall()


# Incomplete thread root over alias m: the thread job's criterion (full or
# tombstoned replies), shared by the view, backup inventory and repair plan;
# not a claim about what the API's reply_count means.
REPLY_GAP = """m.parent_id IS NULL AND m.reply_count >
    (SELECT COUNT(*) FROM messages r WHERE r.project_id=m.project_id
     AND r.parent_id=m.message_id AND r.body_state IN ('full','deleted'))"""


def incomplete_reply_roots(db, project_id) -> int:
    """Thread roots whose reported reply_count exceeds the stored full or
    tombstoned replies — the thread job's criterion, not a completeness claim."""
    return db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE m.project_id=? AND {REPLY_GAP}",
        (project_id,)).fetchone()[0]


def thread_reply_pairs(db, *, since=None, until=None, as_of, project_id=None):
    """One root and its earliest observed full reply, including coverage gaps.

    Root posting time defines the half-open cohort; replies are observed through
    as_of, not until. Unplaceable roots remain explicitly unplaced. A missing
    reply time sorts first so a later timed reply cannot fabricate first latency.
    IDs and raw professions are internal evidence, never aggregate output.
    """
    valid = "typeof(m.posted_at_ts)='integer' AND m.posted_at_ts>0 AND m.posted_at_ts<=253402300799"
    where, params = "m.posted_at_ts<=?", [as_of]
    if since is not None:
        where += " AND m.posted_at_ts>=?"
        params.append(since)
    if until is not None:
        where += " AND m.posted_at_ts<?"
        params.append(until)
    project = ""
    if project_id is not None:
        project = " AND m.project_id=?"
        params.append(project_id)
    return db.execute(f"""
        SELECT m.project_id, m.message_id root_id, m.sender_id root_actor,
               m.sender_type root_actor_type,
               m.profession root_profession, m.posted_at_ts root_ts,
               m.body_state root_state, m.reply_count,
               r.message_id reply_id, r.sender_id reply_actor,
               r.sender_type reply_actor_type,
               r.profession reply_profession, r.posted_at_ts reply_ts,
               (SELECT COUNT(*) FROM messages f
                WHERE f.project_id=m.project_id AND f.parent_id=m.message_id
                  AND f.body_state='full') stored_full_replies
        FROM messages m LEFT JOIN messages r ON r.message_id=(
            SELECT x.message_id FROM messages x
            WHERE x.project_id=m.project_id AND x.parent_id=m.message_id
              AND x.body_state='full'
              AND (typeof(x.posted_at_ts)!='integer' OR x.posted_at_ts<=0
                   OR x.posted_at_ts>253402300799 OR x.posted_at_ts<=?)
            ORDER BY CASE WHEN typeof(x.posted_at_ts)='integer'
                               AND x.posted_at_ts>0 AND x.posted_at_ts<=253402300799
                          THEN 1 ELSE 0 END,
                     x.posted_at_ts, x.message_id LIMIT 1)
        WHERE m.parent_id IS NULL AND m.body_state IS NOT 'deleted'
          AND (NOT ({valid}) OR m.posted_at_ts IS NULL OR ({where}))
          {project}
        ORDER BY m.message_id
        """, [as_of, *params]).fetchall()


def coverage_gaps(db) -> dict:
    """Ledger-wide fetch coverage as RECORDED — rooms marked incomplete
    (with their reason code), waiting/failed fetch jobs by kind, stored
    messages without a full body, thread roots missing replies. Empty
    lists/zeros mean nothing was recorded as incomplete, never that the
    history is gapless. Archived rooms are left out."""
    live = "SELECT project_id FROM patients WHERE COALESCE(is_archived,0)=0"
    rooms = [(r["project_id"], r["fetch_reason"] or "unrecorded")
             for r in db.execute(
                 "SELECT project_id, fetch_reason FROM patients "
                 "WHERE fetch_state='incomplete' AND COALESCE(is_archived,0)=0 "
                 "ORDER BY project_id")]
    jobs = {r["kind"]: r["n"] for r in db.execute(
        f"SELECT kind, count(*) n FROM fetch_jobs WHERE state IN "
        f"('pending','failed') AND project_id IN ({live}) GROUP BY kind "
        "ORDER BY kind")}
    partial = db.execute(
        f"SELECT count(*) FROM messages WHERE COALESCE(body_state,'') NOT IN "
        f"('full','deleted') AND project_id IN ({live})").fetchone()[0]
    reply_gaps = db.execute(
        f"SELECT COUNT(*) FROM messages m WHERE m.project_id IN ({live}) "
        f"AND {REPLY_GAP}").fetchone()[0]
    return {"incomplete_rooms": rooms, "jobs": jobs,
            "partial_bodies": partial, "reply_gaps": reply_gaps}


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


def posted_epoch(posted_at: str) -> int | None:
    """Epoch seconds of a source timestamp, or None when it is unparseable."""
    if not posted_at:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(posted_at).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def register_search_fold(db) -> None:
    """Make ``mcs_fold(text)`` (``search_fold``) callable in SQL on this
    connection — read-only connections included; registered once."""
    try:
        db.execute("SELECT mcs_fold('')").fetchone()
    except sqlite3.OperationalError:
        db.create_function("mcs_fold", 1, search_fold, deterministic=True)


EXTRACTION_KINDS = ("extract_v1", "extract_llm", "canonical_projection", "semantic_facts_v4")
_LEGACY_UNSCOPED_KINDS = ("extract_v1", "extract_llm")
_CONTEXT_KINDS_IN = ",".join("?" * len(EXTRACTION_KINDS))
_CONTEXT_LEGACY_IN = ",".join("?" * len(_LEGACY_UNSCOPED_KINDS))


def _canonical_shape(content: dict) -> bool:
    for key, fields in (
            ("canonical_facts", ("fact_id", "kind", "validation_status",
                                 "workflow_status", "statement", "evidence_quote")),
            ("canonical_relations", ("left_fact_id", "right_fact_id", "type", "kind"))):
        items = content.get(key, [])
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict) or any(
                    item.get(field) is not None and not isinstance(item[field], str)
                    for field in fields):
                return False
            if key == "canonical_facts":
                ids = item.get("evidence_ids", [])
                if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
                    return False
    return True



def _kind_state(rows, content_hash: str, *, engine_version=None,
                canonical=False) -> dict:
    """Classify one message's artifacts of one extraction kind.
    `rows` = artifact rows (newest-first) as mappings; the first valid
    hash-current non-invalidated row wins — an older row matching the
    current hash legitimately becomes current again on an A→B→A edit.
    Returns {"state", "artifact_id", "last_error"}."""
    saw_rows = saw_stale = saw_error = False
    current_id = None
    current_meta = None
    for r in rows:
        saw_rows = True
        try:
            meta = json.loads(r["meta"]) if isinstance(
                r["meta"], str) else r["meta"]
            content = json.loads(r["content"]) if isinstance(
                r["content"], str) else r["content"]
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(meta, dict) or not isinstance(content, dict):
            continue
        if canonical and not _canonical_shape(content):
            continue
        if (meta.get("error") not in (None, False, 0)
                or content.get("_error") not in (None, False, 0)):
            saw_error = True
            continue
        if engine_version is not None and meta.get("engine_version") != engine_version:
            continue
        if canonical and meta.get("projection_version") != PROJECTION_VERSION:
            saw_stale = True
            continue
        if content_hash is not None \
                and meta.get("hash") == content_hash \
                and meta.get("invalidated") in (None, False, 0):
            current_id = r["artifact_id"]
            current_meta = meta
            break
        saw_stale = True
    if current_id is not None:
        state = "current"
    elif saw_stale:
        state = "stale"
    elif not saw_rows:
        state = "pending"
    else:
        # rows exist but nothing classified — parse failures or
        # error-only results are reported as unknown, never as a
        # pretend-current read
        state = "unknown"
    return {"state": state, "artifact_id": current_id,
            "last_error": saw_error, "current_meta": current_meta}



def extraction_kind_map(art_rows, content_hash) -> dict:
    by_kind: dict = {}
    for kind in EXTRACTION_KINDS:
        kind_rows = [r for r in art_rows if r["kind"] == kind]
        st = _kind_state(kind_rows, content_hash,
                         engine_version=4 if kind == "semantic_facts_v4" else None,
                         canonical=kind in ("canonical_projection", "semantic_facts_v4"))
        entry = {"state": st["state"],
                 "artifact_id": st["artifact_id"]}
        if st["last_error"]:
            entry["last_error"] = True
        cm = st.get("current_meta") or {}
        if cm.get("engine_version") is not None:
            # T18: version-pinned readers can tell a v4 PASS row
            # from a legacy current without parsing content
            entry["engine_version"] = cm["engine_version"]
        by_kind[kind] = entry
    return by_kind



def source_patient_context(db, message, *, by_kind=None, art_rows=None):
    """Read source-bound context independently of canonical extraction precedence."""
    if message.get("body_state") not in (None, "full"):
        return []
    if art_rows is None:
        art_rows = db.execute(
            "SELECT artifact_id,kind,content,meta FROM artifacts "
            "WHERE (project_id=? OR (project_id IS NULL AND kind IN (" + _CONTEXT_LEGACY_IN + ")))"
            " AND message_id=? AND kind IN (" + _CONTEXT_KINDS_IN + ") "
            "ORDER BY artifact_id DESC",
            (message["project_id"], *_LEGACY_UNSCOPED_KINDS, message["message_id"],
             *EXTRACTION_KINDS)).fetchall()
    if by_kind is None:
        by_kind = extraction_kind_map(art_rows, message["content_hash"])
    body = message.get("body_text")
    if body is None:
        row = db.execute("SELECT body_text FROM messages WHERE project_id=? AND message_id=?",
                         (message["project_id"], message["message_id"])).fetchone()
        body = row[0] if row else None
    if not isinstance(body, str) or not body:
        return []
    documents = []
    for kind in ("extract_v1", "extract_llm"):
        artifact_id = by_kind[kind]["artifact_id"]
        row = next((r for r in art_rows if r["artifact_id"] == artifact_id), None)
        documents.append(json.loads(row["content"]) if row is not None else None)
    items = merged_context(*documents, body)
    # A v4 worker fences legacy LLM writes; read its audited additive context.
    for kind in ("semantic_facts_v4", "canonical_projection"):
        entry = by_kind.get(kind, {})
        if entry.get("state") == "current":
            row = next((r for r in art_rows if r["artifact_id"] == entry["artifact_id"]), None)
            if row is not None:
                items = merged_context({"patient_context": items}, json.loads(row["content"]), body)
            break
    return items
