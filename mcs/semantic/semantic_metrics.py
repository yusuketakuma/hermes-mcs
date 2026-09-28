"""Semantic audit history and coverage for the current generation."""
from __future__ import annotations

import json

from semantic_policy import (KIND_AUDIT, KIND_SUMMARY, policy_fingerprint,
                             semantic_config)
from semantic_store import _current, thread_bundle


def _object(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def audit_history(ledger) -> dict:
    """Count artifact history; repeated attempts and obsolete sources remain."""
    statuses, repairs, findings = {}, {}, {}
    for row in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=?", (KIND_AUDIT,)):
        meta, content = _object(row["meta"]), _object(row["content"])
        status = meta.get("audit_status")
        status = status if isinstance(status, str) and status else "unparsed"
        statuses[status] = statuses.get(status, 0) + 1
        if meta.get("repair_count") == 1:
            repair_status = status if status != "unparsed" else "?"
            repairs[repair_status] = repairs.get(repair_status, 0) + 1
        recorded_findings = content.get("findings")
        for finding in recorded_findings if isinstance(recorded_findings, list) else []:
            code = finding.get("code") if isinstance(finding, dict) else None
            if isinstance(code, str) and code:
                findings[code] = findings.get(code, 0) + 1
    return {"audit_statuses": statuses, "repaired_audits": repairs,
            "finding_codes": findings}


def current_quality(ledger, cfg: dict | None) -> dict:
    """Count one current terminal summary/audit pair per scoped message.

    Configuration is supplied by the caller; a snapshot reader never opens
    host configuration implicitly. Completion is audit coverage, not clinical
    correctness: NEEDS_REVIEW completes an audit but does not count as PASS.
    """
    if cfg is None:
        return {"available": False, "reason": "config_not_supplied"}
    scfg, errors = semantic_config(cfg)
    if errors:
        return {"available": False, "reason": "config_invalid"}
    project_ids = scfg["project_ids"]
    rows = ledger.db.execute(
        "SELECT project_id,message_id,COALESCE(parent_id,message_id) root_id "
        "FROM messages ORDER BY project_id,root_id,message_id").fetchall()
    total = len(rows)
    rows = [row for row in rows if project_ids is None
            or row["project_id"] in project_ids]
    out = {"available": True, "scope": "stored_messages_in_configured_projects",
           "completion_basis": "terminal_summary_audit_pair",
           "mode": scfg["mode"], "summary_mode": scfg["summary_mode"],
           "policy_fingerprint": policy_fingerprint(scfg),
           "total_messages": total, "scoped_messages": len(rows),
           "excluded_messages": total - len(rows),
           "denominator": len(rows), "complete": 0, "incomplete": len(rows),
           "audit_statuses": {}, "incomplete_reasons": {},
           "completion_rate": None, "pass_rate": None}
    if scfg["mode"] == "off" or scfg["summary_mode"] == "off":
        out.update(available=False, reason="summary_disabled")
        # Disabled coverage is not a processing backlog.
        out.update(complete=None, incomplete=None)
        return out
    # the fingerprint binds the local model; resolve it from the
    # supplied config, never from the host's config file
    import semantic
    local_model = semantic.llm_model(cfg)
    last_key, bundle, members = None, None, {}
    for row in rows:
        key = row["project_id"], row["root_id"]
        if key != last_key:
            bundle = thread_bundle(ledger, *key, local_model=local_model)
            members = ({m["message_id"]: m for m in bundle["members"]}
                       if bundle else {})
            last_key = key
        reason = "thread_missing"
        if bundle is not None:
            mid, fp = row["message_id"], bundle["source_fingerprint"]
            audit = _current(ledger, KIND_AUDIT, mid, fp,
                             out["policy_fingerprint"])
            summary = _current(ledger, KIND_SUMMARY, mid, fp,
                               out["policy_fingerprint"])
            reason = "current_pair_missing"
            if audit is not None and summary is not None:
                meta, content = audit["meta"], audit["content"]
                summary_meta, summary_content = summary["meta"], summary["content"]
                status = meta.get("audit_status")
                member = members.get(mid)
                if member is None:
                    out["incomplete_reasons"]["thread_member_missing"] = \
                        out["incomplete_reasons"].get("thread_member_missing", 0) + 1
                    continue
                reason = "current_pair_incomplete"
                if (isinstance(content, dict) and isinstance(summary_content, dict)
                        and status in ("PASS", "NEEDS_REVIEW")
                        and content.get("status") == status
                        and content.get("target_message_id") == mid
                        and summary_content.get("target_message_id") == mid
                        and summary_content.get("audit_status") == status
                        and summary_meta.get("audit_status") == status
                        and not summary_meta.get("stale")
                        and summary_meta.get("target_revision") == member["revision"]
                        and meta.get("publication_mode") == scfg["summary_mode"]
                        and summary_meta.get("publication_mode") == scfg["summary_mode"]):
                    out["complete"] += 1
                    out["audit_statuses"][status] = out["audit_statuses"].get(status, 0) + 1
                    continue
        out["incomplete_reasons"][reason] = out["incomplete_reasons"].get(reason, 0) + 1
    out["incomplete"] = out["denominator"] - out["complete"]
    if out["denominator"]:
        out["completion_rate"] = out["complete"] / out["denominator"]
        out["pass_rate"] = out["audit_statuses"].get("PASS", 0) / out["denominator"]
    return out
