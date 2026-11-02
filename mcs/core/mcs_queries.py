"""Shared ledger and extraction read contracts on borrowed connections.

mcs_stats (read-only statistics) and mcs_signals (prospective review
candidates) must agree on which artifact counts as the *current*
extraction of a message: valid JSON, hash-matched to the message body,
and — where the schema marks failures — not an error record. Both
readers used to carry private copies of these predicates; this module
keeps one definition so they cannot drift.

SQL fragments and bounded reads borrow the caller's connection. Read scopes
hold no connection or memo after the caller's synchronous operation returns.
"""
import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo

from mcs_util import loads_dict, payload_hash, search_fold, strict_json, valid_hash
from inference_version import CURRENT_VERSION, current_meta_pred, is_current, read_version, valid_meta_pred
from patient_context import merged_context
from semantic_projection import PROJECTION_VERSION

JST = ZoneInfo("Asia/Tokyo")
DAY_S = 86400
UNKNOWN_TIME = -(2**63)
_proof_read_active = ContextVar('mcs_proof_read_active', default=False)
_INTERNAL_PROOF = '__mcs_internal_proof_v1'
_INTERNAL_LEAF_CLAIMS = '__mcs_leaf_claims_v1'
_message_reads = ContextVar('mcs_current_message_reads', default=None)
_MESSAGE_CACHE_ENTRIES = 128
_MESSAGE_CACHE_BYTES = 256 * 1024


def _message_read_stamp(db):
    cursor = db.execute('SELECT data_version,schema_version '
                        'FROM pragma_data_version,pragma_schema_version')
    try:
        row = cursor.fetchone()
        return (db.total_changes, db.in_transaction, row[0], row[1])
    finally:
        cursor.close()


class _MessageReads:
    def __init__(self, db):
        self.db = db
        self.stamp = _message_read_stamp(db)
        self.cache = {}
        self.bytes = 0

    def clear(self):
        self.db = None
        self.stamp = None
        self.cache.clear()
        self.bytes = 0

    def current(self):
        if self.stamp is None:
            return False
        try:
            stamp = _message_read_stamp(self.db)
        except BaseException:
            self.clear()
            raise
        if stamp != self.stamp:
            # A caller write, rollback/new view or DDL ends memo use for this
            # entire call. A later equal stamp never revives an older proof.
            self.clear()
            return False
        return True

    def remember(self, key, raw):
        if raw is not None and type(raw) not in (str, bytes):
            return
        values = (*key[1], raw)
        cost = sum(4 * len(value) if type(value) is str else len(value)
                   if type(value) is bytes else 0 for value in values)
        if len(self.cache) < _MESSAGE_CACHE_ENTRIES and self.bytes + cost <= _MESSAGE_CACHE_BYTES:
            self.cache[key] = raw
            self.bytes += cost


@contextmanager
def current_message_read_scope(db):
    """Share current documents only within one borrowed, bounded read snapshot."""
    if not isinstance(db, sqlite3.Connection) or _proof_read_active.get():
        yield
        return
    # Leave a row pending even for an empty table. Closing this statement ends
    # only our read anchor, never a caller transaction or progress handler.
    anchor = db.execute('SELECT message_id FROM (SELECT message_id FROM messages LIMIT 1) '
                        'UNION ALL SELECT NULL UNION ALL SELECT NULL')
    try:
        anchor.fetchone()
        scope = _MessageReads(db)
        token = _message_reads.set(scope)
        try:
            yield
        finally:
            scope.clear()
            _message_reads.reset(token)
    except BaseException:
        try:
            anchor.close()
        except sqlite3.Error:
            pass  # Preserve the original read/body failure.
        raise
    else:
        anchor.close()


def selected_artifact_proof_valid(db, artifact_id, *, inference_target=CURRENT_VERSION):
    """Check a marked extraction against its exact retained progress witness."""
    if type(artifact_id) is not int or artifact_id <= 0:
        return False
    db.execute('SAVEPOINT selected_artifact_proof')
    try:
        row = db.execute("SELECT kind, CASE WHEN kind='extract_llm' AND ("
            "CASE WHEN json_valid(content) THEN json_type(content,'$.__mcs_internal_proof_v1') IS NOT NULL ELSE 0 END "
            "OR CASE WHEN json_valid(meta) THEN json_type(meta,'$.internal_proof_version') IS NOT NULL "
            "OR json_type(meta,'$.internal_proof_hash') IS NOT NULL ELSE 0 END) THEN 1 ELSE 0 END marked "
            "FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if row is None:
            return False
        if row['kind'] == 'extract_llm':
            newer = _newer_marked_extract('a', 'm', inference_target)
            blocked = db.execute('SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id '
                'AND (a.project_id IS NULL OR a.project_id=m.project_id) WHERE a.artifact_id=? '
                f'AND EXISTS ({newer})', (artifact_id,)).fetchone()
            if blocked:
                return False
        if not row['marked']:
            return True
        if _proof_read_active.get():
            return False
        selected = db.execute('SELECT a.content,a.meta,a.model,m.* FROM artifacts a JOIN messages m '
            'ON a.message_id=m.message_id AND (a.project_id IS NULL OR a.project_id=m.project_id) '
            'WHERE a.artifact_id=? AND a.kind=\'extract_llm\'', (artifact_id,)).fetchone()
        if selected is None:
            return False
        content, meta = strict_json(selected['content']), strict_json(selected['meta'])
        if (not isinstance(content, dict) or not isinstance(meta, dict)
                or not isinstance(content.get(_INTERNAL_PROOF), dict)
                or type(meta.get('internal_proof_version')) is not int or meta['internal_proof_version'] != 1
                or not valid_hash(meta.get('internal_proof_hash'))
                or not isinstance(selected['model'], str) or not selected['model']):
            return False
        import extract_llm
        token = _proof_read_active.set(True)
        try:
            result = extract_llm.progress_for_message(db, selected, model=selected['model'],
                                                       expected_artifact_id=artifact_id)
            return isinstance(result, dict) and result.get('state') == 'complete'
        finally:
            _proof_read_active.reset(token)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return False
    finally:
        db.execute('RELEASE selected_artifact_proof')

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


def source_extract_pred(art: str = "a", msg: str = "m", *,
                        content: bool = True,
                        error_check: bool = True) -> str:
    """Match a valid source-bound extraction without a public version constraint.

    json_valid MUST gate every json_extract — one malformed
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
    parts.append(f"COALESCE(json_extract({art}.meta,'$.invalidated'),0)=0")
    parts.append(f"json_extract({art}.meta,'$.hash')={msg}.content_hash")
    # Legacy message artifacts may omit project_id (the public ledger API
    # permits it); their globally unique message_id supplies the scope.
    # An explicitly conflicting project must never supply this patient's facts.
    return (f" AND ({art}.project_id IS NULL OR {art}.project_id={msg}.project_id)"
            f" AND {msg}.body_state IS NOT 'deleted'"
            " AND CASE WHEN " + " AND ".join(guards)
            + " THEN " + " AND ".join(parts) + " ELSE 0 END"
            + " AND " + valid_meta_pred(f"{art}.meta"))


def _newer_marked_extract(art, msg, inference_target):
    content_marker = ("CASE WHEN json_valid(proof_hold.content) THEN "
        "json_type(proof_hold.content,'$.__mcs_internal_proof_v1') IS NOT NULL "
        "ELSE instr(proof_hold.content,'\"__mcs_internal_proof_v1\"')>0 END")
    return ("SELECT 1 FROM artifacts proof_hold WHERE proof_hold.kind='extract_llm' "
        f"AND proof_hold.message_id={art}.message_id AND proof_hold.artifact_id>{art}.artifact_id "
        f"AND (proof_hold.project_id IS NULL OR proof_hold.project_id={msg}.project_id) "
        "AND CASE WHEN json_valid(proof_hold.meta) THEN "
        "(json_type(proof_hold.meta,'$.internal_proof_version') IS NOT NULL "
        f"OR json_type(proof_hold.meta,'$.internal_proof_hash') IS NOT NULL OR ({content_marker})) "
        "AND CASE WHEN json_type(proof_hold.meta,'$.hash')='text' "
        "AND length(json_extract(proof_hold.meta,'$.hash'))=64 "
        "AND json_extract(proof_hold.meta,'$.hash') NOT GLOB '*[^0-9a-f]*' "
        f"THEN json_extract(proof_hold.meta,'$.hash')={msg}.content_hash ELSE 1 END "
        "AND COALESCE(json_extract(proof_hold.meta,'$.invalidated'),0)=0 "
        f"AND ({current_meta_pred('proof_hold.meta', target=inference_target)} "
        f"OR NOT {valid_meta_pred('proof_hold.meta')}) ELSE "
        "(instr(proof_hold.meta,'\"internal_proof_version\"')>0 "
        "OR instr(proof_hold.meta,'\"internal_proof_hash\"')>0 "
        f"OR ({content_marker})) END")


def current_extract_pred(art: str = "a", msg: str = "m", *,
                         content: bool = True, error_check: bool = True,
                         inference_target: str = CURRENT_VERSION) -> str:
    """Require the exact public inference version as well as source validity."""
    newer_marked = _newer_marked_extract(art, msg, inference_target)
    return (source_extract_pred(art, msg, content=content, error_check=error_check)
            + " AND " + current_meta_pred(f"{art}.meta", target=inference_target)
            + f" AND ({art}.kind!='extract_llm' OR NOT EXISTS ({newer_marked}))")


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
                     require_version: bool = True,
                     inference_target: str = CURRENT_VERSION) -> str:
    """Build the shared current-row predicate for both projection kinds."""
    col = f"{art}." if art else ""
    return (f"CASE WHEN json_valid({col}content) AND json_valid({col}meta) "
            f"THEN json_type({col}content)='object' "
            f"AND json_type({col}meta)='object' "
            f"AND {current_meta_pred(f'{col}meta', target=inference_target)} "
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
                            require_version: bool = True,
                            inference_target: str = CURRENT_VERSION) -> str:
    """Predicate: a canonical_projection row is usable right now —
    valid payloads, no error, hash-current to the source message, and not
    superseded by a later source save (meta.invalidated), using the
    current projection version. Only bounded maintenance may omit the
    version check. Shared by current_projection_id and candidates."""
    return _projection_pred(art, hash_ref, require_version=require_version,
                            inference_target=inference_target)


def current_projection_id(msg: str = "m", *, require_version: bool = True,
                          inference_target: str = CURRENT_VERSION) -> str:
    """Subquery: THE current canonical-projection row for a message —
    the newest valid artifact_id among hash-current, unexpired projections.
    The writer expires source/context/policy generations explicitly; a
    newer projection of the same generation supersedes older ones, and a new
    EMPTY projection legitimately replaces an old non-empty one (C06).
    Returns the artifact_id or NULL."""
    pred = current_projection_pred("c", f"{msg}.content_hash",
                                   require_version=require_version,
                                   inference_target=inference_target)
    return (f"(SELECT MAX(c.artifact_id) FROM artifacts c"
            f" WHERE c.kind='{CANONICAL_PROJECTION_KIND}'"
            f" AND c.message_id={msg}.message_id"
            f" AND c.project_id={msg}.project_id"
            f" AND {pred})")


def current_v4_pred(art: str = "v", hash_ref: str = "?", *,
                    require_version: bool = True,
                    inference_target: str = CURRENT_VERSION) -> str:
    """Predicate: a semantic_facts_v4 row is usable — same contract as
    current_projection_pred plus the engine version pin."""
    return _projection_pred(art, hash_ref, engine_version=4,
                            require_version=require_version,
                            inference_target=inference_target)


def current_v4_id(msg: str = "m", *, require_version: bool = True,
                  inference_target: str = CURRENT_VERSION) -> str:
    """Subquery: THE current v4 read-model row for a message."""
    pred = current_v4_pred("v", f"{msg}.content_hash",
                          require_version=require_version,
                          inference_target=inference_target)
    return (f"(SELECT MAX(v.artifact_id) FROM artifacts v"
            f" WHERE v.kind='{V4_PROJECTION_KIND}'"
            f" AND v.message_id={msg}.message_id"
            f" AND v.project_id={msg}.project_id"
            f" AND {pred})")


def current_fact_pred(art: str = "a", msg: str = "m", *,
                      content: bool = True,
                      error_check: bool = True,
                      inference_target: str = CURRENT_VERSION) -> str:
    """AND-fragment for fact-extraction reads: v4 PASS outranks the
    canonical projection, which outranks a legacy ``extract_llm`` row;
    each must satisfy current_extract_pred. A delayed v3 writer can
    still land a row but can never displace a published v4
    generation. The caller's FROM clause must admit all three kinds —
    ``<art>.kind IN ('extract_llm','canonical_projection',
    'semantic_facts_v4')``."""
    v4 = current_v4_id(msg, inference_target=inference_target)
    projection = current_projection_id(msg, inference_target=inference_target)
    return (current_extract_pred(art, msg, content=content,
                                 error_check=error_check,
                                 inference_target=inference_target)
            + f" AND CASE WHEN {art}.kind='{V4_PROJECTION_KIND}'"
              f" THEN {art}.artifact_id={v4}"
              f" WHEN {art}.kind='{CANONICAL_PROJECTION_KIND}'"
              f" THEN {art}.artifact_id="
              f"{projection}"
              f" AND {v4} IS NULL"
              f" ELSE {projection} IS NULL"
              f" AND {v4} IS NULL END")


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
            WHERE da.kind IN ({FACT_KINDS_SQL})
              {current_fact_pred('da', 'd')}
              AND ev.value IN ({TRANSITION_EVENTS_SQL})
              AND a.kind IN ({FACT_KINDS_SQL})
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



def karte_source_record(row, project_id, *, historical=False):
    """Shape one immutable Karte original and anchor only timezone-aware source timestamps."""
    content = loads_dict(row['content']) or {}
    body = content.get('comment')
    posted = content.get('updated_at')
    timestamp = None
    if isinstance(posted, str):
        try:
            parsed = datetime.fromisoformat(posted.replace('Z', '+00:00'))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                timestamp = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return {'message_id': 0, 'source_key': 'karte:' + str(row['artifact_id']),
            'source_artifact_id': row['artifact_id'], 'historical': historical,
            'project_id': project_id, 'body_text': body if isinstance(body, str) else '',
            'body_state': 'full' if isinstance(body, str) or content.get('empty') is True else 'partial',
            'empty': content.get('empty') is True, 'content_hash': payload_hash(content),
            'source': 'karte_summary', 'body_length': len(body) if isinstance(body, str) else 0,
            'posted_at': posted, 'posted_at_ts': timestamp, 'first_seen': 0,
            'sender_name': (content.get('updater') or {}).get('name')}

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
EXTRACTION_KINDS_SQL = ",".join(f"'{kind}'" for kind in EXTRACTION_KINDS)
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
                canonical=False, inference_target: str = CURRENT_VERSION) -> dict:
    """Classify one message's artifacts of one extraction kind.
    `rows` = artifact rows (newest-first) as mappings; the first valid
    hash-current non-invalidated row wins. Source invalidation is sticky:
    matching an earlier hash again never restores an invalidated result.
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
        if meta.get("invalidated") not in (None, False, 0):
            saw_stale = True
            continue
        if read_version(meta) is None:
            continue
        if not is_current(meta, target=inference_target):
            saw_stale = True
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



def extraction_kind_map(art_rows, content_hash, *,
                        inference_target: str = CURRENT_VERSION, db=None) -> dict:
    by_kind: dict = {}
    for kind in EXTRACTION_KINDS:
        kind_rows = [r for r in art_rows if r["kind"] == kind]
        st = _kind_state(kind_rows, content_hash,
                         inference_target=inference_target,
                         engine_version=4 if kind == "semantic_facts_v4" else None,
                         canonical=kind in ("canonical_projection", "semantic_facts_v4"))
        entry = {"state": st["state"],
                 "artifact_id": st["artifact_id"]}
        if (db is not None and kind == 'extract_llm' and entry['state'] == 'current'
                and not selected_artifact_proof_valid(db, entry['artifact_id'], inference_target=inference_target)):
            entry.update(state='unknown', artifact_id=None, _proof_unverified=True)
        if st["last_error"]:
            entry["last_error"] = True
        cm = st.get("current_meta") or {}
        if cm.get("engine_version") is not None:
            # T18: version-pinned readers can tell a v4 PASS row
            # from a legacy current without parsing content
            entry["engine_version"] = cm["engine_version"]
        by_kind[kind] = entry
    return by_kind



def _context_inputs(db, message, *, by_kind=None, art_rows=None):
    """(art_rows, by_kind, body) for a full-body message, or None when it has no body."""
    if message.get("body_state") not in (None, "full"):
        return None
    if art_rows is None:
        art_rows = db.execute(
            "SELECT artifact_id,kind,content,meta FROM artifacts "
            "WHERE (project_id=? OR (project_id IS NULL AND kind IN (" + _CONTEXT_LEGACY_IN + ")))"
            " AND message_id=? AND kind IN (" + _CONTEXT_KINDS_IN + ") "
            "ORDER BY artifact_id DESC",
            (message["project_id"], *_LEGACY_UNSCOPED_KINDS, message["message_id"],
             *EXTRACTION_KINDS)).fetchall()
    if by_kind is None:
        by_kind = extraction_kind_map(art_rows, message["content_hash"], db=db)
    body = message.get("body_text")
    if body is None:
        row = db.execute("SELECT body_text FROM messages WHERE project_id=? AND message_id=?",
                         (message["project_id"], message["message_id"])).fetchone()
        body = row[0] if row else None
    if not isinstance(body, str) or not body:
        return None
    return art_rows, by_kind, body


def source_patient_context(db, message, *, by_kind=None, art_rows=None):
    """Read source-bound context independently of canonical extraction precedence."""
    inputs = _context_inputs(db, message, by_kind=by_kind, art_rows=art_rows)
    if inputs is None:
        return []
    art_rows, by_kind, body = inputs
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


def extraction_input_signature(content, meta, model="") -> str:
    """Bind adopted content and its contract without retry or observation bookkeeping."""
    document = loads_dict(content) if isinstance(content, str) else content
    metadata = loads_dict(meta) if isinstance(meta, str) else meta
    if not isinstance(document, dict) or not isinstance(metadata, dict):
        raise ValueError("extraction_input_invalid")
    volatile = {"attempts", "auto_retry", "next_try", "config_generation",
                "generated_at", "fetched_at", "upgrade", "reprojected_from"}
    return payload_hash([document, model,
                         {key: value for key, value in metadata.items() if key not in volatile}])


def current_extraction_blobs(db, project_id: int) -> dict:
    """Read all current per-message extraction blobs for one project on borrowed db.

    No connection, transaction or display limit is owned here. Missing artifacts
    remain absent; projection/current predicates preserve source binding.
    """
    db.execute('SAVEPOINT current_extraction_blobs')
    try:
        candidates = {}
        for a in db.execute(f"""
          SELECT a.message_id, a.kind, a.content, a.artifact_id FROM artifacts a
          JOIN messages m ON m.message_id=a.message_id
          WHERE m.project_id=? AND a.kind IN ({EXTRACTION_KINDS_SQL})
            {current_extract_pred()}
            AND (a.kind IN ('extract_v1','extract_llm')
                 OR (1=1 {current_fact_pred()}))
          ORDER BY a.artifact_id
        """, (project_id,)):
            candidates.setdefault(a['message_id'], {})[a['kind']] = (a['artifact_id'], a['content'])
        arts = {}
        for mid, kinds in candidates.items():
            for kind, (artifact_id, content) in kinds.items():
                if kind == 'extract_llm' and not selected_artifact_proof_valid(db, artifact_id):
                    continue
                document = loads_dict(content)
                if document is not None and (_INTERNAL_PROOF in document or _INTERNAL_LEAF_CLAIMS in document):
                    document.pop(_INTERNAL_PROOF, None)
                    document.pop(_INTERNAL_LEAF_CLAIMS, None)
                    content = json.dumps(document, ensure_ascii=False)
                arts.setdefault(mid, {})[kind] = content
        return arts
    finally:
        db.execute('RELEASE current_extraction_blobs')


def decode_current_extractions(blobs: dict):
    """(v1, lm, llm, has_fact_source) for one message's current artifacts.

    semantic_facts_v4 (T18) shadows canonical_projection, which shadows
    extract_llm for the same message (a hash-current extract_llm row is
    kept in blobs only so its reply can be read below)."""
    llm_blob = blobs.get("extract_llm")
    has_fact_source = False
    llm = None
    try:
        v1 = json.loads(blobs["extract_v1"]) \
            if "extract_v1" in blobs else {}
        lm_blob = blobs.get("semantic_facts_v4") \
            or blobs.get("canonical_projection") \
            or llm_blob
        lm = json.loads(lm_blob) if lm_blob else {}
        has_fact_source = (lm_blob is not None and isinstance(lm, dict)
                           and not lm.get("_error"))
        if lm_blob is not None and lm_blob is llm_blob:
            llm = lm    # one decode per unshadowed extract_llm row
    except (ValueError, TypeError, RecursionError):
        v1 = lm = {}
    if not isinstance(v1, dict):
        v1 = {}
    if not isinstance(lm, dict):
        lm = {}
    if lm.get("_error"):
        lm = {}
    if not isinstance(llm, dict):
        llm = loads_dict(llm_blob) or {}
    return v1, lm, llm, has_fact_source



def source_patient_mentions(db, project_id: int) -> list[dict]:
    """Read as-written medication/symptom mentions without display decoration.

    Uses the caller's same snapshot/TX with no cap, writes or close. Rules only
    supplement names absent from the chosen fact generation and stay unverified;
    an empty result proves no clinical absence or acquisition completeness.
    """
    from mcs_util import locate_quote_span
    arts = current_extraction_blobs(db, project_id)
    posts = {r[0]: (r[1], r[2], r[3]) for r in db.execute(
        "SELECT message_id, posted_at_ts, content_hash, body_text FROM messages WHERE project_id=? AND (body_state IS NULL OR body_state='full')",
        (project_id,))}

    def dicts(value):
        return [item for item in value if isinstance(item, dict)] \
            if isinstance(value, list) else []

    out = []
    for mid, blobs in arts.items():
        if mid not in posts:
            continue
        v1, lm, _, _ = decode_current_extractions(blobs)
        rows = [("medications", x.get("name"), x) for x in dicts(lm.get("meds"))]
        rows += [("symptoms", x.get("text"), x) for x in dicts(lm.get("symptoms"))]
        named = {text for _, text, _ in rows}
        rows += [("medications", x.get("name"), {**x, "unverified": True})
                 for x in dicts(v1.get("medications")) if x.get("name") not in named]
        rows += [("symptoms", x, {"unverified": True}) for x in v1.get("symptoms") or []
                 if isinstance(x, str) and x not in named]
        for category, text, item in rows:
            if not isinstance(text, str) or not text:
                continue
            body = posts[mid][2] or ""
            quote = item.get("evidence")
            span = item.get("evidence_span")
            if isinstance(item.get("span"), dict):
                span = [item["span"].get("start"), item["span"].get("end")]
            explicit = span is not None
            if explicit:
                if (not isinstance(span, (tuple, list)) or len(span) != 2
                        or any(type(n) is not int for n in span) or not isinstance(quote, str)
                        or not 0 <= span[0] < span[1] <= len(body) or body[span[0]:span[1]] != quote):
                    span = None
            elif isinstance(quote, str) and quote:
                span = locate_quote_span(body, quote)
            else:
                # A name mention is not proof of dose/status. Keep the
                # candidate and source context, without borrowing the body
                # as evidence for every different fact in the post.
                quote = text
                span = locate_quote_span(body, text)
            mention = {k: item[k] for k in ("dose", "freq", "route", "prn", "action", "status", "negated",
                        "unverified", "onset", "duration", "severity", "time_text", "measured_on", "condition", "refers_to") if k in item}
            if span is None or not isinstance(item.get("evidence"), str):
                mention["unverified"] = True
            out.append({"category": category, "text": text, "quote": quote if span is not None else "",
                        "evidence": quote if span is not None else "", "evidence_span": list(span) if span is not None else None,
                        "reference_context": {"text": body, "span": [0, len(body)]},
                        "subject": item.get("subject") or "unspecified", "mention": mention,
                        "message_id": mid, "content_hash": posts[mid][1], "posted_at_ts": posts[mid][0]})
    return out


def current_message_artifact_query(*, facts=False, mid="?", field="content"):
    """Build the shared per-message current extraction/fact content read."""
    if field not in ('content', 'artifact_id'):
        raise ValueError('current_artifact_field_invalid')
    if facts:
        where = (
            f"a.message_id={mid} AND a.kind IN ({FACT_KINDS_SQL}) "
            "AND m.body_state IS NOT 'deleted' "
            "AND CASE WHEN json_valid(a.content) THEN "
            "json_type(a.content)='object' ELSE 0 END "
            f"{current_fact_pred('a', 'm')}")
    else:
        where = f"a.kind=? AND a.message_id={mid} {current_extract_pred()}"
    return (f"SELECT a.{field} FROM artifacts a JOIN messages m "
            "ON m.message_id=a.message_id WHERE " + where +
            " ORDER BY a.artifact_id DESC LIMIT 1")



def current_message_extract(db, kind: str, mid: int) -> dict | None:
    """Read one current message extraction document, distinct from global latest IDs."""
    return _current_message_document(db, (kind, mid), facts=False)


def _current_message_document(db, params, *, facts):
    scope = _message_reads.get()
    if (scope is not None and scope.db is db and not _proof_read_active.get()
            and ((facts and len(params) == 1 and type(params[0]) is int)
                 or (not facts and len(params) == 2
                     and type(params[0]) is str and type(params[1]) is int))
            and scope.current()):
        key = (facts, params)
        try:
            if key in scope.cache:
                return _message_document(scope.cache[key])
            raw = _current_message_content(db, params, facts=facts)
            document = _message_document(raw)
            if scope.current():
                scope.remember(key, raw if document is not None else None)
            return document
        except BaseException:
            scope.clear()
            raise
    try:
        db.execute('SAVEPOINT current_message_document')
        try:
            return _message_document(_current_message_content(db, params, facts=facts))
        finally:
            db.execute('RELEASE current_message_document')
    except BaseException:
        if scope is not None and scope.db is db:
            scope.clear()
        raise


def _current_message_content(db, params, *, facts):
    row = db.execute(current_message_artifact_query(facts=facts, field='artifact_id'), params).fetchone()
    if row is None or not selected_artifact_proof_valid(db, row[0]):
        return None
    selected = db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (row[0],)).fetchone()
    return selected['content'] if selected else None


def _message_document(raw):
    document = loads_dict(raw) if raw is not None else None
    if document is not None:
        document.pop(_INTERNAL_PROOF, None)
        document.pop(_INTERNAL_LEAF_CLAIMS, None)
    return document



def current_message_facts(db, mid: int) -> dict | None:
    """Newest usable fact artifact for a message — a hash-current
    ``canonical_projection`` shadows ``extract_llm`` (the same
    ``current_fact_pred`` shadow rule every consumer shares); when the
    projection is stale, invalidated, or absent the legacy extraction
    is read instead — never the other way around."""
    return _current_message_document(db, (mid,), facts=True)



def message_urgency_source(db, mid: int):
    """Read body state and patient identity on the caller's same connection."""
    columns = {column[1] for column in db.execute("PRAGMA table_info(patients)")}
    name = "p.patient_name" if "patient_name" in columns else "NULL"
    kind = "p.project_type" if "project_type" in columns else "NULL"
    return db.execute(f"SELECT m.body_text,m.body_state,{name} patient_name,{kind} project_type FROM messages m "
                     "LEFT JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=?", (mid,)).fetchone()



def current_message_urgency_qc(db, mid: int):
    """Read newest source-bound urgency audit without owning a transaction."""
    row = db.execute(
        "SELECT q.content FROM artifacts q"
        " JOIN messages m ON m.message_id=q.message_id"
        " WHERE q.kind='extract_qc' AND q.message_id=?"
        f" {current_extract_pred('q', 'm')}"
        f" AND json_extract({json_or_null('q.meta')},"
        "'$.source_artifact_id')="
        "  (SELECT MAX(a.artifact_id) FROM artifacts a"
        "   JOIN messages m2 ON m2.message_id=a.message_id"
        "   WHERE a.kind='extract_llm' AND a.message_id=q.message_id"
        f"   {current_extract_pred('a', 'm2')})"
        " ORDER BY q.artifact_id DESC LIMIT 1", (mid,)).fetchone()
    return loads_dict(row["content"]) if row else None


def patient_context_scope(db, pid):
    """Read the patient's nonsecret source scope on a borrowed connection."""
    row = db.execute("SELECT project_id,project_type,karte_id,is_archived,fetch_state,fetch_reason "
                     "FROM patients WHERE project_id=?", (pid,)).fetchone()
    return dict(row) if row else None


def patient_context_sources(db, pid, *, hydrate=True, mid=None):
    """Read only the patient's original records, never previous generated prose."""
    from patient_context import context_items, extract_context, project_context_items
    from patient_context_plan import source_digest
    extractions = {}
    seen = set()
    for row in db.execute(
            "SELECT a.message_id,a.artifact_id,a.kind,a.content,a.model,a.meta FROM artifacts a "
            "JOIN messages m ON m.message_id=a.message_id WHERE m.project_id=? "
            "AND (a.project_id=? OR (a.project_id IS NULL AND a.kind IN ('extract_v1','extract_llm'))) AND a.kind IN "
            f"({EXTRACTION_KINDS_SQL}) "
            + current_extract_pred("a", "m")
            + " AND (a.kind IN ('extract_v1','extract_llm') OR (1=1 " + current_fact_pred("a", "m") + ")) "
            + ("AND m.message_id=? " if mid is not None else "")
            + "ORDER BY a.artifact_id DESC", (pid, pid, mid) if mid is not None else (pid, pid)):
        if (row[0], row[2]) in seen:
            continue
        seen.add((row[0], row[2]))
        extractions.setdefault(row[0], []).append(row)
    columns = ("m.*" if hydrate else "m.message_id,m.project_id,m.content_hash,m.body_state,"
               "m.posted_at,m.posted_at_ts,m.sender_id,m.sender_name,m.parent_id,m.first_seen")
    for row in db.execute(f"SELECT {columns},length(m.body_text) AS body_length FROM messages m "
                          "WHERE project_id=? " + ("AND message_id=? " if mid is not None else "")
                          + "ORDER BY message_id", (pid, mid) if mid is not None else (pid,)):
        message = dict(row)
        rows = extractions.get(message["message_id"], [])
        message["extraction_sig"] = source_digest(sorted((r[2], extraction_input_signature(r[3], r[5], r[4])) for r in rows))
        items = source_patient_context(db, message, art_rows=list(reversed(rows))) if hydrate else []
        source = {key: message.get(key) for key in
                  ("project_id", "message_id", "content_hash", "posted_at_ts")}
        items = project_context_items(items, "detail", source)
        # Explicit headings remain usable before the ordinary extractor runs.
        items = context_items({"patient_context": items + (extract_context(message.get("body_text")) if hydrate else [])},
                              message.get("body_text"))
        yield {**message, "source": "chat", "items": items,
               "canonical_context": any(row[2] in ("canonical_projection", "semantic_facts_v4") for row in rows)}
    if mid not in (None, 0) and not (isinstance(mid, str) and mid.startswith("karte:")):
        return
    scope = patient_context_scope(db, pid)
    rows = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='karte_summary' "
                      "AND project_id=? ORDER BY artifact_id DESC", (pid,))
    latest = True
    for row in rows:
        meta = loads_dict(row[2]) or {}
        if not scope or scope["karte_id"] is None or meta.get("karte_id") != scope["karte_id"]:
            continue
        source_key = "karte:" + str(row[0])
        current_record, latest = latest, False
        if isinstance(mid, str) and mid != source_key:
            continue
        source = karte_source_record(row, pid, historical=not current_record)
        yield {**source, "items": extract_context(source["body_text"]) if hydrate else []}
