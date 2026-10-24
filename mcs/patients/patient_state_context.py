"""Freeze bounded, source-grounded pharmacy references for one extraction attempt."""
from datetime import datetime
import json
import re

import extract_quality
from clinical_values import patient_item_scope
from mcs_queries import (ELIGIBLE_MESSAGE, FACT_KINDS_SQL, JST, current_fact_pred, current_v4_id,
                         json_or_null)
from mcs_util import loads_dict
from mcs_util import payload_hash as _digest
from patient_context import reference_item
import patient_context
from stats_math import finite_number as _finite

KIND = "extract_llm_context_v1"
STATE_MAX = 600
TOTAL_MAX = 1200
_NONCURRENT = re.compile(
    r"未確認|未確定|不明|疑い|可能性|かもしれ|予定|計画|今後|以前|過去|前回|中止|否定|ありません|ではない|なし|なかった")


def _canonical_report(document, report):
    quality = document.get("canonical_quality")
    if (not isinstance(quality, dict) or quality.get("coverage_status") != "complete"
            or quality.get("open_obligation_ids") != [] or quality.get("limitations") != []):
        return False
    facts, relations = document.get("canonical_facts"), document.get("canonical_relations")
    if not isinstance(facts, list) or not isinstance(relations, list):
        return False
    disputed = set()
    for relation in relations:
        if not isinstance(relation, dict):
            return False
        if relation.get("type") in ("CONTRADICTION", "UNRESOLVED"):
            for key in ("left_fact_id", "right_fact_id"):
                value = relation.get(key)
                if isinstance(value, str):
                    disputed.add(value)
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        quote = fact.get("evidence_quote")
        if (isinstance(fact.get("fact_id"), str) and fact["fact_id"] not in disputed
                and fact.get("validation_status") == "verified"
                and fact.get("subject") == f"patient:{report['project_id']}"
                and fact.get("polarity") == "affirmed"
                and fact.get("epistemic") in ("asserted", "reported")
                and fact.get("workflow_status") in ("performed", "reported", "done")
                and isinstance(quote, str) and report["evidence"] in quote):
            return True
    return False


def _clean_legacy(meta, version):
    integrity = meta.get("integrity")
    if meta.get("extract_version") != version or not isinstance(integrity, dict):
        return False
    validation = integrity.get("validation")
    if not isinstance(validation, dict) or any(
            type(validation.get(key)) is not int or validation[key] != 0
            for key in ("items_dropped", "evidence_dropped")):
        return False
    if not extract_quality.completeness_resolved(meta):
        return False
    return not any(key in meta and (not isinstance(meta[key], dict)
                   or meta[key].get("applied") is not True) for key in ("qc_fix", "human_fix"))


def _held_sql(version):
    return f"""EXISTS(SELECT 1 FROM artifacts h WHERE h.kind='extract_upgrade_v1'
        AND h.project_id=m.project_id AND h.message_id=m.message_id
        AND json_extract({json_or_null('h.content')},'$.state')='held_prior'
        AND json_extract({json_or_null('h.content')},'$.hash')=m.content_hash
        AND json_extract({json_or_null('h.content')},'$.prior_artifact_id')=a.artifact_id
        AND json_extract({json_or_null('h.content')},'$.target_version')={version})"""


def history(db, target, *, version):
    """Read at most 200 earlier source messages, with current reader precedence."""
    target = dict(target)
    stamp = target.get("posted_at_ts")
    if (type(version) is not int or version < 1
            or not _finite(stamp) or type(target.get("project_id")) is not int
            or type(target.get("message_id")) is not int):
        return []
    rows = db.execute(f"""
        WITH earlier AS MATERIALIZED (
          SELECT * FROM messages m WHERE m.project_id=? AND m.message_id!=?
          AND {ELIGIBLE_MESSAGE} AND m.posted_at_ts < ?
          AND m.posted_at_ts > -1e308 AND m.posted_at_ts < 1e308
          ORDER BY m.posted_at_ts DESC,m.message_id DESC LIMIT 200)
        SELECT m.*,a.artifact_id,a.kind,a.content,a.meta,a.model,
          {_held_sql(version)} AS held
        FROM earlier m
        LEFT JOIN artifacts a ON a.message_id=m.message_id
          AND a.kind IN ({FACT_KINDS_SQL})
          {current_fact_pred()}
        ORDER BY m.posted_at_ts DESC,m.message_id DESC,a.artifact_id DESC
    """, (target["project_id"], target["message_id"], stamp))
    reports, seen = [], set()
    for raw in rows:
        row = dict(raw)
        if row["message_id"] in seen:
            continue
        seen.add(row["message_id"])
        reports.extend(_reports(row, version))
    return reports


def _reports(row, version):
    document, meta = loads_dict(row["content"]) or {}, loads_dict(row["meta"]) or {}
    if row["kind"] not in ("extract_llm", "canonical_projection", "semantic_facts_v4"):
        return []
    reports = patient_context.pharmacy_state_items(document, row)
    for report in reports:
        report["reference_eligible"] = (not row.get("held") and not document.get("_error")
            and ((_clean_legacy(meta, version) and isinstance(meta.get("actual_model"), str)
                  and bool(meta["actual_model"]) and meta["actual_model"] == row["model"])
                 if row["kind"] == "extract_llm" else _canonical_report(document, report)))
    return reports


def reference_current(db, target, reference, *, version):
    """Check the frozen antecedent itself, without replacing it or walking reference chains."""
    reference = reference_item(reference)
    target = dict(target)
    if (type(version) is not int or version < 1
            or reference is None or not _finite(target.get("posted_at_ts"))
            or reference["project_id"] != target.get("project_id")
            or reference["message_id"] == target.get("message_id")
            or reference["posted_at_ts"] >= target["posted_at_ts"]):
        return False
    row = db.execute(f"""SELECT m.*,a.artifact_id,a.kind,a.content,a.meta,a.model,
        {_held_sql(version)} AS held
        FROM messages m JOIN artifacts a ON a.message_id=m.message_id
          AND a.kind IN ({FACT_KINDS_SQL})
          {current_fact_pred()}
        WHERE m.project_id=? AND m.message_id=? AND {ELIGIBLE_MESSAGE}
        ORDER BY a.artifact_id DESC LIMIT 1""", (reference["project_id"], reference["message_id"])).fetchone()
    if row is None:
        return False
    row = dict(row)
    if row["content_hash"] != reference["content_hash"] or row["posted_at_ts"] != reference["posted_at_ts"]:
        return False
    # The identity was frozen at inference; changes to other posts or the 200-row
    # window do not invalidate this exact, still-current source.
    group = [report for report in _reports(row, version)
             if report["key"] == reference["key"] and report["drug_names"] == reference["drug_names"]]
    _, candidates = format_state(group)
    expected = {key: value for key, value in reference.items() if key != "token"}
    return any(candidate == expected for candidate in candidates.values())


def format_state(reports, *, budget=STATE_MAX):
    """Offer unambiguous patient reports as bounded dated references, never current facts."""
    text = "【以前の投稿の参考情報・対象本文の事実へ転記しない】\n"
    refs, blocked = {}, set()
    groups = {}
    for report in reports:
        # Other people's reports must not hide this patient's earlier report.
        if report["subject"] != "patient":
            continue
        key = (report["message_id"], tuple(report["drug_names"]), report["key"])
        groups.setdefault(key, []).append(report)
    for (_, names, key), group in groups.items():
        identity = (names, key)
        if identity in blocked:
            continue
        blocked.add(identity)  # A newer ambiguous/negative report forbids older fallback.
        report = group[0]
        parent = report["parent"]
        if (len(group) != 1 or len(names) > 1 or not report.get("reference_eligible")
                or not _finite(report.get("posted_at_ts"))
                or _NONCURRENT.search(parent["text"])
                or patient_item_scope(report, parent["evidence"], default="patient",
                                      surface=names[0] if names else None) != "patient"):
            continue
        token = f"S{len(refs) + 1}"
        try:
            date = datetime.fromtimestamp(report["posted_at_ts"], JST).strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            continue
        line = (f"[{token}] {date} {key} 薬剤=" + json.dumps(list(names), ensure_ascii=False)
                + " 値=" + json.dumps(report["value"], ensure_ascii=False) + "\n")
        if len(text) + len(line) > min(budget, STATE_MAX):
            continue
        refs[token] = {field: report[field] for field in (
            "key", "value", "drug_names", "project_id", "message_id", "content_hash",
            "fingerprint", "parent_fingerprint", "posted_at_ts")}
        text += line
    return (text if refs else ""), refs


def _binding(target, model, contract, attempt, enabled):
    return _digest({"target": _source(target),
        "model": model, "contract": contract, "attempt": attempt, "enabled": enabled})


def load(db, artifact_id, target):
    """Read an intact frozen frame without consulting today's flag or background."""
    if type(artifact_id) is not int:
        return None
    row = db.execute("SELECT * FROM artifacts WHERE artifact_id=? AND kind=? AND project_id=? "
                     "AND message_id=?", (artifact_id, KIND, target["project_id"], target["message_id"])).fetchone()
    if row is None:
        return None
    doc, meta = loads_dict(row["content"]), loads_dict(row["meta"])
    if not isinstance(doc, dict) or not isinstance(meta, dict):
        return None
    payload = {key: doc.get(key) for key in ("binding", "source", "text", "refs", "enabled")}
    if (meta.get("hash") != target["content_hash"] or not patient_context.valid_reference_hash(doc.get("binding"))
            or not isinstance(doc.get("text"), (str, type(None))) or type(doc.get("enabled")) is not bool
            or not isinstance(doc.get("refs"), dict) or doc.get("source") != _source(target)
            or (doc["text"] is not None and len(doc["text"]) > TOTAL_MAX)):
        return None
    try:
        if _digest(payload) != doc.get("digest"):
            return None
    except (TypeError, ValueError, RecursionError):
        return None
    return {**doc, "artifact_id": artifact_id}


def _source(target):
    return {key: target.get(key) for key in (
        "project_id", "message_id", "content_hash", "posted_at", "posted_at_ts", "parent_id")}


def prepare(ledger, target, *, model, contract, attempt, enabled, base_context, version):
    """Persist the full attempt frame before inference; retries reuse it byte for byte."""
    target = dict(target)
    if (type(enabled) is not bool or not isinstance(base_context, (str, type(None)))
            or not isinstance(model, str) or not model or not isinstance(contract, str) or not contract
            or not isinstance(attempt, dict) or not patient_context.valid_reference_hash(target.get("content_hash"))
            or any(type(target.get(key)) is not int or target[key] <= 0
                   for key in ("project_id", "message_id"))):
        raise ValueError("invalid_context_options")
    if base_context is not None and len(base_context) > TOTAL_MAX:
        raise ValueError("context_budget_exceeded")
    binding = _binding(target, model, contract, attempt, enabled)
    # Do not include mutable history or summary text in the lookup binding.
    with ledger.db:
        if not ledger.db.in_transaction:
            ledger.db.execute("BEGIN IMMEDIATE")
        source = ledger.db.execute("SELECT * FROM messages m WHERE project_id=? AND message_id=? "
                                   f"AND {current_v4_id()} IS NULL",
                                   (target["project_id"], target["message_id"])).fetchone()
        if (source is None or any(source[key] != target.get(key) for key in (
                "content_hash", "posted_at", "posted_at_ts", "parent_id")) or source["body_state"] not in (None, "full")
                or not source["body_text"]):
            raise ValueError("context_source_changed")
        for row in ledger.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
                                     "AND message_id=? ORDER BY artifact_id DESC",
                                     (KIND, target["project_id"], target["message_id"])):
            old = load(ledger.db, row[0], target)
            if old is not None and old["binding"] == binding:
                return old
            doc, meta = loads_dict(row["content"]), loads_dict(row["meta"])
            if isinstance(meta, dict) and patient_context.valid_reference_hash(meta.get("hash")) and meta["hash"] != target["content_hash"]:
                continue
            if not isinstance(doc, dict) or not isinstance(meta, dict) or doc.get("binding") == binding:
                raise ValueError("context_snapshot_invalid")
        prefix = (base_context or "") + ("\n" if base_context and not base_context.endswith("\n") else "")
        state, refs = (format_state(history(ledger.db, target, version=version),
                                   budget=TOTAL_MAX - len(prefix)) if enabled else ("", {}))
        if state:
            text = prefix + state
        else:
            text = base_context
        payload = {"binding": binding, "source": _source(target), "text": text, "refs": refs, "enabled": enabled}
        doc = {**payload, "digest": _digest(payload)}
        aid = ledger.artifact_add_tx(KIND, json.dumps(doc, ensure_ascii=False),
                                    project_id=target["project_id"], message_id=target["message_id"],
                                    model=model, meta={"hash": target["content_hash"]})
    return {**doc, "artifact_id": aid}
