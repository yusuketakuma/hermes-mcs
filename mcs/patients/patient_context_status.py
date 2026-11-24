"""Per-state patient context counts from the last stored outcomes, without patients or source text.

The counts here are the *last stored outcome* of each patient's progress,
history scans and published context, not a recomputation of the live
`_inference_coverage` state (that needs the per-patient scan the doctor
bounds separately). Every active patient lands in exactly one partition
bucket, so a malformed, missing or unverifiable record never reads as done
by subtraction. Display constants are dependency-free; SQL helpers import
the artifact kinds lazily so the notify views stay light.

Dependency loss is split in two. A *pending gap* is what
`inference_dependency_loss.read` reports as incomplete (an unresolved gap, an
unreadable proof or an uncaptured revision) and is a problem. A *retained loss
record* is a permanent negative-use barrier that does not block the patient by
itself; it is counted for display only, with releasability unclassified.
"""
import json
from functools import lru_cache
import sqlite3

from inference_version import CURRENT_VERSION, current_meta_pred

# Every status `_inference_coverage` can return (drift-tested against its source).
COVERAGE_STATES = (
    "out_of_scope", "off", "dependency_gap", "attachment_extraction_pending",
    "cloud_extraction_pending", "held", "failed", "quality_unknown", "quality_held",
    "unacquired", "incomplete", "acquisition_incomplete", "scan_pending", "audit_pending",
    "summary_pending", "excluded", "empty", "complete")

# Factual wording only: no next-step promise that the scheduler does not guarantee for every code.
STATUS_JA = {
    "out_of_scope": "患者コンテキストの作成対象外です",
    "off": "患者コンテキストの作成は無効です",
    "dependency_gap": "変更前の根拠が不足しています",
    "attachment_extraction_pending": "添付の解析が未完了",
    "cloud_extraction_pending": "クラウド解析が未完了",
    "held": "解析を保留している資料があります",
    "failed": "解析に失敗した資料があります",
    "quality_unknown": "要約の照合結果を確認できません",
    "quality_held": "要約の照合を保留しています",
    "unacquired": "原文の取得完了を未確認",
    "incomplete": "本文が未取得・不完全な投稿があります",
    "acquisition_incomplete": "履歴の取得は未完了",
    "scan_pending": "原文の解析待ち",
    "audit_pending": "原文の根拠照合待ち",
    "summary_pending": "患者コンテキストの更新待ち",
    "excluded": "解析対象の投稿はすべて削除済みです",
    "empty": "解析対象の投稿はありません",
    "complete": "解析完了",
}

UNKNOWN_STATUS_JA = "解析状況を確認できません"

# One primary bucket per active patient, in precedence order; the sum is patients_total.
PARTITION = ("invalid", "held", "failed", "dependency_gap", "published", "in_progress", "no_record")

QUALITY_STATES = ("pending", "deferred", "held", "failed", "unavailable", "unknown", "budget", "disabled")

BASIS = "last_stored_outcome"

_QUALITY_HELD = "quality_held"


def status_text(state):
    """Japanese wording of one coverage status; an unknown value is shown, never dropped."""
    return STATUS_JA.get(state, UNKNOWN_STATUS_JA) if isinstance(state, str) else UNKNOWN_STATUS_JA


@lru_cache(maxsize=1)
def _context_codes():
    from error_codes import KNOWN_ERROR_CODES
    return frozenset(code for code in KNOWN_ERROR_CODES if code.startswith("patient_context_"))


def reason_code(value):
    """Collapse a stored failure to a registered patient-context code (legacy nightly_* mapped), else other."""
    if type(value) is not str:
        return "other"
    if value.startswith("nightly_"):
        value = "patient_context_" + value[len("nightly_"):]
    return value if value in _context_codes() else "other"


def _allowed_code(value):
    return value in (_QUALITY_HELD, "other") or reason_code(value) == value


def latest_current_pred(a="a", b="b"):
    """Current-version row with no newer current row of the same kind, patient and source key."""
    def key(alias):
        return f"CASE WHEN json_valid({alias}.meta) THEN json_extract({alias}.meta,'$.source_key') END"
    return (current_meta_pred(f"{a}.meta", target=CURRENT_VERSION)
            + f" AND NOT EXISTS (SELECT 1 FROM artifacts {b} INDEXED BY idx_artifacts_lookup "
            f"WHERE {b}.kind={a}.kind "
            f"AND {b}.project_id={a}.project_id AND {b}.artifact_id>{a}.artifact_id AND "
            + current_meta_pred(f"{b}.meta", target=CURRENT_VERSION)
            + f" AND {key(b)} IS {key(a)})")


def _object(column):
    return f"CASE WHEN json_valid({column}) THEN json_type({column})='object' ELSE 0 END"


def _field(column, path, fn="json_extract"):
    return f"CASE WHEN {_object(column)} THEN {fn}({column},'$.{path}') END"


def _quality(value):
    state = value.partition(":")[0] if type(value) is str and value else None
    return None if state is None else state if state in QUALITY_STATES else "other"


def _source_current(db, pid, key, binding):
    """True only when the scanned source still exists, is usable and (if bound) is the same occurrence.

    A malformed original-source key raises ValueError, which the caller counts as an unreadable row."""
    import inference_dependency_loss as dependency_loss
    if isinstance(key, str):
        actual = dependency_loss.source_binding(db, pid, 0, source_key=key)
    else:
        row = db.execute("SELECT body_state, body_text FROM messages WHERE project_id=? AND message_id=?",
                         (pid, key)).fetchone()
        if (row is None or row[0] not in (None, "full")
                or not (isinstance(row[1], str) and row[1].strip())):
            return False
        actual = dependency_loss.source_binding(db, pid, key)
    if actual is None:
        return False
    # A legacy row without a stored binding keeps its stored outcome (never upgraded to healthy).
    return not isinstance(binding, str) or actual == binding


def _empty():
    return {"basis": BASIS, "patients_total": 0, "partition": dict.fromkeys(PARTITION, 0),
            "summary_failed": {"patients": 0, "codes": {}},
            "summary_held": {"patients": 0, "codes": {}},
            "summary_quality": dict.fromkeys((*QUALITY_STATES, "other"), 0),
            "scan_failed": {"patients": 0, "sources": 0, "codes": {}},
            "scan_held": {"patients": 0, "sources": 0, "codes": {}},
            "scan_quality": dict.fromkeys((*QUALITY_STATES, "other"), 0),
            "dependency_gap": {"patients": 0},
            "dependency_loss": {"patients": 0, "releasability": "unclassified"},
            "invalid_rows": 0}


def _add(block, code, *, sources=False):
    block["codes"][code] = block["codes"].get(code, 0) + 1
    if sources:
        block["sources"] += 1


def _scope_valid(text):
    """True for a stored source_scope the readers accept (``patient_context_refresh_sources._source_scope``)."""
    import patient_context_refresh_sources as sources
    try:
        return sources._source_scope(json.loads(text)) is not None
    except (ValueError, TypeError, RecursionError):
        return False


def state_counts(db):
    """Count active patients per last stored outcome; any SQL error propagates (the caller shows unknown).

    Rows are read by column name, so the connection is switched to `sqlite3.Row`
    for the call and its own row factory is restored afterwards."""
    previous = db.row_factory
    db.row_factory = sqlite3.Row
    try:
        return _state_counts(db)
    finally:
        db.row_factory = previous


def _state_counts(db):
    import patient_context_refresh_state as state
    import inference_dependency_loss as dependency_loss
    result = _empty()
    active = {row[0] for row in db.execute(
        "SELECT project_id FROM patients WHERE COALESCE(is_archived,0)=0")}
    result["patients_total"] = len(active)
    invalid, recorded, loss, gap = set(), set(), set(), set()
    summary_failed, summary_held, scan_failed, scan_held = set(), set(), set(), set()
    published = {}
    rows = db.execute(
        "SELECT a.project_id, a.kind, a.message_id, a.artifact_id, " + _object("a.content") + ", "
        + _field("a.meta", "source_key") + ", " + _field("a.meta", "source_key", "json_type") + ", "
        + _field("a.meta", "source_binding") + ", " + _field("a.meta", "published") + ", "
        + _field("a.content", "failure") + ", " + _field("a.content", "failure", "json_type") + ", "
        + _field("a.content", "held", "json_type") + ", "
        + _field("a.content", "quality_state") + ", " + _field("a.content", "quality_state", "json_type") + ", "
        + _field("a.content", "complete", "json_type") + ", " + _field("a.content", "audited", "json_type") + ", "
        + _field("a.meta", "source_scope", "json_type") + ", " + _field("a.meta", "source_scope") + ", "
        + _field("a.content", "source_scope", "json_type") + ", " + _field("a.content", "source_scope")
        + " FROM artifacts a JOIN patients p ON p.project_id=a.project_id "
        "WHERE a.kind IN (?,?,?,?) AND COALESCE(p.is_archived,0)=0 "
        "AND (a.kind=? OR a.message_id IS NULL) "
        "AND (a.kind!=? OR COALESCE(" + _field("a.meta", "invalidated") + ",0)=0) AND "
        + latest_current_pred(), (state.PROGRESS, state.SCAN, state.KIND, state.LEGACY_KIND,
                                  state.SCAN, state.SCAN))
    for (pid, kind, mid, artifact_id, ok, key, key_type, binding, is_published, failure, failure_type,
         held_type, quality, quality_type, complete_type, audited_type, scope_type, scope,
         progress_scope_type, progress_scope) in rows:
        recorded.add(pid)
        if (not ok or failure_type not in (None, "null", "text") or quality_type not in (None, "null", "text")
                or any(value not in (None, "null", "true", "false")
                       for value in (held_type, complete_type, audited_type))):
            result["invalid_rows"] += 1
            invalid.add(pid)
            continue
        if kind in (state.KIND, state.LEGACY_KIND):
            if is_published == 1 and scope_type not in (None, "null") and not (
                    scope_type == "array" and _scope_valid(scope)):
                # A publication whose scope the readers reject is never published; no scope or null means the whole patient.
                result["invalid_rows"] += 1
                invalid.add(pid)
                continue
            if artifact_id > published.get(pid, (-1, False))[0]:
                published[pid] = (artifact_id, is_published == 1)
            continue
        if kind == state.PROGRESS and progress_scope_type not in (None, "null") and not (
                progress_scope_type == "array" and _scope_valid(progress_scope)):
            # The progress reader rejects this scope for the patient, so the row is never in-progress work.
            result["invalid_rows"] += 1
            invalid.add(pid)
            continue
        held = held_type == "true"
        quality = _quality(quality)
        if kind == state.PROGRESS:
            if held:
                summary_held.add(pid)
                _add(result["summary_held"], reason_code(failure) if failure else _QUALITY_HELD)
            elif failure:
                summary_failed.add(pid)
                _add(result["summary_failed"], reason_code(failure))
            if quality is not None:
                result["summary_quality"][quality] += 1
            continue
        if complete_type == "true" and audited_type == "true" and not failure:
            continue
        held = held or quality == "held"
        if not (held or failure or quality):
            continue
        if key_type not in ("integer", "text"):
            if key_type is not None or mid is None:
                result["invalid_rows"] += 1
                invalid.add(pid)
                continue
            key = mid
        try:
            current = _source_current(db, pid, key, binding)
        except (ValueError, TypeError, KeyError):
            result["invalid_rows"] += 1
            invalid.add(pid)
            continue
        if not current:
            continue  # superseded by a changed, deleted or emptied source: no stored outcome applies
        if quality is not None:
            result["scan_quality"][quality] += 1
        if held:
            scan_held.add(pid)
            _add(result["scan_held"], reason_code(failure) if failure else _QUALITY_HELD, sources=True)
        elif failure:
            scan_failed.add(pid)
            _add(result["scan_failed"], reason_code(failure), sources=True)
    with_records = set()
    for pid, ok, retained in db.execute(
            "SELECT a.project_id, " + _object("a.content") + ", "
            "COALESCE(" + _field("a.content", "coverage") + ",0) IS NOT 1 "
            "AND COALESCE(" + _field("a.content", "gap") + ",0) IS NOT 1 "
            "FROM artifacts a JOIN patients p ON p.project_id=a.project_id "
            "WHERE a.kind=? AND COALESCE(p.is_archived,0)=0", (dependency_loss.KIND,)):
        with_records.add(pid)
        if not ok:
            result["invalid_rows"] += 1
            invalid.add(pid)
        elif retained:
            loss.add(pid)
    # Pending gaps come from the dependency reader itself, so a gap resolved by an
    # audited coverage row is not a problem and an unreadable proof never reads as resolved.
    for pid in sorted(with_records - invalid):
        if not dependency_loss.read(db, pid)["complete"]:
            gap.add(pid)
    result["summary_failed"]["patients"] = len(summary_failed)
    result["summary_held"]["patients"] = len(summary_held)
    result["scan_failed"]["patients"] = len(scan_failed)
    result["scan_held"]["patients"] = len(scan_held)
    result["dependency_loss"]["patients"] = len(loss)
    result["dependency_gap"]["patients"] = len(gap)
    held_patients, failed_patients = summary_held | scan_held, summary_failed | scan_failed
    for pid in active:
        bucket = ("invalid" if pid in invalid else "held" if pid in held_patients
                  else "failed" if pid in failed_patients else "dependency_gap" if pid in gap
                  else "published" if published.get(pid, (0, False))[1]
                  else "in_progress" if pid in recorded else "no_record")
        result["partition"][bucket] += 1
    return result


def _count(value):
    return type(value) is int and value >= 0


def _codes(value):
    if not isinstance(value, dict) or not all(isinstance(code, str) and _allowed_code(code) and _count(n)
                                              for code, n in value.items()):
        return None
    return {code: value[code] for code in sorted(value)}


def sanitize(value):
    """Rebuild a state_counts result from fixed keys and counts only; any malformed part makes it None."""
    try:
        empty = _empty()
        out = {"basis": BASIS}
        if value["basis"] != BASIS or not _count(value["patients_total"]):
            return None
        out["patients_total"] = value["patients_total"]
        out["partition"] = {key: value["partition"][key] for key in PARTITION}
        for name in ("summary_quality", "scan_quality"):
            out[name] = {key: value[name][key] for key in empty[name]}
        for name, keys in (("summary_failed", ("patients",)), ("summary_held", ("patients",)),
                           ("scan_failed", ("patients", "sources")), ("scan_held", ("patients", "sources"))):
            codes = _codes(value[name]["codes"])
            if codes is None:
                return None
            out[name] = {**{key: value[name][key] for key in keys}, "codes": codes}
        out["dependency_gap"] = {"patients": value["dependency_gap"]["patients"]}
        out["dependency_loss"] = {"patients": value["dependency_loss"]["patients"], "releasability": "unclassified"}
        out["invalid_rows"] = value["invalid_rows"]
    except (KeyError, TypeError):
        return None
    numbers = [out["patients_total"], out["invalid_rows"], out["dependency_gap"]["patients"],
               out["dependency_loss"]["patients"],
               *out["partition"].values(), *out["summary_quality"].values(), *out["scan_quality"].values(),
               *(n for name in ("summary_failed", "summary_held", "scan_failed", "scan_held")
                 for key, n in out[name].items() if key != "codes")]
    if not all(_count(n) for n in numbers) or sum(out["partition"].values()) != out["patients_total"]:
        return None
    return out


def has_problem(states):
    """Held, failed, pending dependency gaps or unreadable records make the diagnostic a warning.

    Retained loss records alone are not a problem: their releasability is unclassified."""
    partition = states["partition"]
    return bool(partition["invalid"] or partition["held"] or partition["failed"]
                or partition["dependency_gap"] or states["invalid_rows"])
