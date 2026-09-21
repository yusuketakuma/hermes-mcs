#!/usr/bin/env python3
"""Phase J input model — schema constants, thread bundle, fingerprint.

Owns the semantic layer's vocabulary (artifact kinds, enums, versions)
and its atomic analysis input: the thread bundle, the content
fingerprint that invalidates stale results, and the `state` shape sent
to Jev. No pipeline logic lives here — semantic.py orchestrates and
semantic_llm/semantic_audit/semantic_loops/semantic_notice do the work.
"""
import json
import math
import time

from mcs_requests import payload_hash
import semantic_jev as jev

SCHEMA_VERSION = "2026-09-20"
POLICY_VERSION = "2026-09-20"
JOB_KIND = "semantic"

KIND_BUNDLE = "semantic_bundle"
KIND_ASSESS = "semantic_assess"
KIND_FACTS = "semantic_facts"
KIND_SUMMARY = "semantic_summary"
KIND_AUDIT = "semantic_audit"
KIND_LOOP = "loop_candidate"
KIND_LOOP_EVENT = "loop_event"
KIND_PLAN = "notify_plan"
KIND_USAGE = "semantic_usage"
SEMANTIC_KINDS = (KIND_BUNDLE, KIND_ASSESS, KIND_FACTS, KIND_SUMMARY,
                  KIND_AUDIT, KIND_LOOP, KIND_LOOP_EVENT, KIND_PLAN,
                  KIND_USAGE)

MODES = ("off", "shadow", "assist", "enforce")
TECH_STATUSES = ("complete", "partial", "pending", "retry_wait",
                 "error", "stale")
VERDICTS = ("MATCH", "UNDETERMINED", "NO_MATCH")
AUDIT_STATUSES = ("PASS", "REPAIR_REQUIRED", "NEEDS_REVIEW", "PENDING",
                  "STALE")
FACT_KINDS = ("medication_event", "symptom", "explicit_request",
              "pending_item", "schedule", "preference", "observation",
              "other")
FACT_STATUSES = ("considered", "planned", "order_reported",
                 "execution_reported", "cancelled", "not_stated",
                 "conflicting")
POLARITIES = ("affirmed", "negated", "uncertain")
CLAIM_KINDS = ("reported_fact", "inference", "limitation")
CLAIM_SECTIONS = ("medication", "status", "pharmacy", "followup",
                  "progress", "flow", "other")

# Safety ceiling for one local-LLM prompt. Above it the model's context
# window could silently drop input — an oversize target is flagged
# input_oversize -> NEEDS_REVIEW instead of being chopped (§12.3).
PROMPT_CHAR_LIMIT = 28000


# ---------- fixed input bundle (spec §12.1) ----------

def _member(row) -> dict:
    return {
        "message_id": row["message_id"],
        "parent_id": row["parent_id"],
        "revision": row["content_hash"] or "",
        "posted_at": row["posted_at"] or "",
        "occurred_at": None,      # event time is a fact-level field
        "sender": {"type": row["sender_type"] or "",
                   "profession": row["profession"] or ""},
        "body_original": row["body_text"] or "",
        "body_state": row["body_state"] or "unknown",
    }


def bundle_fingerprint(members: list, model: str = jev.JEV_MODEL) -> str:
    """Content+context revision fingerprint: any body edit, context
    change, model/registry/schema bump invalidates prior results
    (INV-15). Canonical JSON — never a lossy string concat."""
    return payload_hash({
        "members": sorted(({"m": m["message_id"], "r": m["revision"]}
                          for m in members), key=lambda x: x["m"]),
        "model": model,
        "registry": jev.REGISTRY_VERSION,
        "schema": SCHEMA_VERSION,
        "policy": POLICY_VERSION,
    })


def thread_bundle(ledger, project_id: int, root_id: int,
                  target_ids: list | None = None) -> dict | None:
    """One post + its same-thread stored replies — the atomic analysis
    unit (§12.3). A message whose row vanished makes the bundle
    unbuildable (None), never silently re-scoped (AT-019)."""
    rows = ledger.db.execute("""
      SELECT message_id,parent_id,sender_type,profession,posted_at,
             body_text,body_state,content_hash
      FROM messages WHERE project_id=? AND (message_id=? OR parent_id=?)
      ORDER BY posted_at_ts, message_id
    """, (project_id, root_id, root_id)).fetchall()
    root = [r for r in rows if r["message_id"] == root_id]
    if not root:
        return None
    members = [_member(r) for r in rows]
    for m in members:
        m["role"] = ("target" if target_ids
                     and m["message_id"] in target_ids else
                     "root" if m["parent_id"] is None else "context")
    quality = "full" if all(
        m["body_state"] in ("full", "deleted") for m in members) \
        else "partial"
    fp = bundle_fingerprint(members)
    return {"bundle_id": f"bundle_{project_id}_{root_id}_{fp[:12]}",
            "account_scope": "mcs",
            "project_id": project_id, "root_id": root_id,
            "members": members, "content_quality": quality,
            "source_fingerprint": fp,
            "registry_version": jev.REGISTRY_VERSION,
            "schema_version": SCHEMA_VERSION,
            "notification_policy_version": POLICY_VERSION}


def jev_state(bundle: dict, target_id: int) -> dict | None:
    """Jev input: opaque ids, body text, sender type/profession —
    patient display names and unrelated threads are never sent
    (INV-05, §21.1)."""
    target = ctx = None
    members = bundle["members"]
    target = next((m for m in members if m["message_id"] == target_id),
                  None)
    if target is None:
        return None
    ctx = [{"id": f"m{m['message_id']}", "role": m["role"],
            "posted_at": m["posted_at"], "sender": m["sender"],
            "text": m["body_original"]}
           for m in members if m["message_id"] != target_id]
    return {"target": {"id": f"m{target_id}", "role": "target",
                       "posted_at": target["posted_at"],
                       "sender": target["sender"],
                       "text": target["body_original"]},
            "context": ctx}


# ---------- artifact helpers ----------

def _current(ledger, kind: str, message_id: int, fp: str):
    """Latest artifact of `kind` for `message_id` whose meta fingerprint
    still matches the live input — older generations stay recorded as
    history but are never 'current' (AT-035/056)."""
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? "
            "AND message_id=? ORDER BY artifact_id DESC",
            (kind, message_id)):
        try:
            meta = json.loads(r["meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if meta.get("fingerprint") == fp:
            try:
                content = json.loads(r["content"])
            except (json.JSONDecodeError, TypeError):
                return None
            return {"content": content, "meta": meta}
    return None


def _jst_day_start(now: float) -> float:
    """Epoch of the current JST midnight — the operation's actual 'day'
    boundary (UTC midnight would reset the budget at 09:00 JST)."""
    return math.floor((now + 9 * 3600) / 86400) * 86400 - 9 * 3600


def jev_usage_today(ledger) -> int:
    """Durable daily Jev request count — shadow traffic spends real API
    budget too, so it is never unbounded (§13.5, AT-067). Counts the
    per-attempt semantic_usage rows written by run_due — one row per job
    attempt carrying that attempt's request DELTA, so every external
    call (primary, detail, claim-audit, loop-relation, failed) is
    counted exactly once."""
    day = _jst_day_start(time.time())
    total = 0
    for r in ledger.db.execute(
            "SELECT meta FROM artifacts WHERE kind=? AND created_at>=?",
            (KIND_USAGE, day)):
        try:
            total += int(json.loads(r["meta"] or "{}")
                         .get("jev_requests", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return total
