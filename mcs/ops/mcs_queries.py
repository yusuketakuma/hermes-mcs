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
        parts.append(f"json_extract({art}.meta,'$.error') IS NOT 1")
    parts.append(f"json_extract({art}.meta,'$.hash')={msg}.content_hash")
    return " AND " + " AND ".join(parts)


CANONICAL_PROJECTION_KIND = "canonical_projection"


def current_fact_pred(art: str = "a", msg: str = "m", *,
                      error_check: bool = True) -> str:
    """AND-fragment for fact-extraction reads (T12 consumer migration):
    a hash-current ``canonical_projection`` row shadows ``extract_llm``
    for the same message; either kind must satisfy current_extract_pred.
    The caller's FROM clause must admit both kinds —
    ``<art>.kind IN ('extract_llm','canonical_projection')``."""
    return (current_extract_pred(art, msg, error_check=error_check)
            + f" AND ({art}.kind='{CANONICAL_PROJECTION_KIND}'"
              f" OR NOT EXISTS (SELECT 1 FROM artifacts c"
              f" JOIN messages cm ON cm.message_id=c.message_id"
              f" WHERE c.kind='{CANONICAL_PROJECTION_KIND}'"
              f" AND c.message_id={art}.message_id"
              f" AND json_valid(c.meta)"
              f" AND json_extract(c.meta,'$.hash')"
              f"=cm.content_hash))")


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
        and med.get("subject", "patient") == "patient" \
        and med.get("status", "current") != "past"


# SQL twin of med_is_patient_current, evaluated inside
# json_each(a.content,'$.meds') — the alias `je` is part of the
# contract. `IS NOT 1` matches `not med.get("negated")` because
# _validate admits only strict booleans (JSON true -> SQLite 1);
# absent keys on pre-v2 rows read as NULL and pass.
MED_PATIENT_CURRENT_SQL = (
    "json_extract(je.value,'$.negated') IS NOT 1 "
    "AND COALESCE(json_extract(je.value,'$.subject'),"
    "'patient')='patient' "
    "AND COALESCE(json_extract(je.value,'$.status'),"
    "'current')!='past'")


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
            WHERE da.kind IN ('extract_llm','{CANONICAL_PROJECTION_KIND}')
              {current_fact_pred('da', 'd')}
              AND ev.value IN ({TRANSITION_EVENTS_SQL})
              AND a.kind IN ('extract_llm','{CANONICAL_PROJECTION_KIND}')
              {current_fact_pred('a', 'm')}
              {pred_p}AND EXISTS
                  (SELECT 1 FROM json_each(a.content,'$.meds') je
                   WHERE json_extract(je.value,'$.action')
                       IN ({CHANGE_ACTIONS_SQL})
                     AND {MED_PATIENT_CURRENT_SQL})
              {extra_where}
            ORDER BY d.message_id, m.message_id""",
        (win_s, win_s, *params)).fetchall()
    grouped = {}
    for pid, dmid, mid in rows:
        grouped.setdefault(dmid, (pid, set()))[1].add(mid)
    return grouped
