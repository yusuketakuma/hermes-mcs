"""Semantic input bundle + artifact read helpers (spec §12.1):
thread bundling, fingerprints, current-artifact lookup, and the
durable Jev daily-usage counter. Policy constants come from semantic_policy (a leaf)."""
from __future__ import annotations

import math
import time

from mcs_requests import payload_hash
from mcs_util import loads_dict
import semantic_jev as jev
from semantic_policy import (KIND_ASSESS, KIND_FACT_PROJ, KIND_USAGE,
                             POLICY_VERSION, SCHEMA_VERSION)




def _member(row) -> dict:
    return {
        "project_id": row["project_id"],
        "message_id": row["message_id"],
        "parent_id": row["parent_id"],
        "revision": row["content_hash"] or "",
        "posted_at": row["posted_at"] or "",
        "occurred_at": None,      # event time is a fact-level field
        "sender": {"id": row["sender_id"], "type": row["sender_type"] or "",
                   "profession": row["profession"] or ""},
        "source_metadata": {"sender_name": row["sender_name"] or "",
                            "organization": row["organization"] or ""},
        "body_original": row["body_text"] or "",
        "body_state": row["body_state"] or "unknown",
        "reply_count": row["reply_count"],
    }


def bundle_fingerprint(members: list, model: str = jev.JEV_MODEL,
                       local_model: str | None = None) -> str:
    """Content+context revision fingerprint: any body edit, context
    change, model/registry/schema bump invalidates prior results
    (INV-15). Canonical JSON — never a lossy string concat.
    ``local_model`` defaults to the host-configured local model; a
    snapshot reader passes the one resolved from its supplied config
    so it never opens host configuration implicitly."""
    import semantic
    import semantic_llm
    from clinical_chunking import PLAN_VERSION
    if local_model is None:
        local_model = semantic.llm_model()
    return payload_hash({
        # Target roles are selection metadata; artifacts are selected per
        # target ID. Everything used to interpret the source is versioned.
        "members": sorted(({k: v for k, v in m.items() if k != "role"}
                           for m in members), key=lambda x: x["message_id"]),
        "model": model,
        "local_model": local_model,
        "prompts": [semantic._FACT_PROMPT, semantic._SUMMARY_PROMPT,
                    semantic._REPAIR_SUFFIX, semantic_llm._FACT_V2_PROMPT,
                    semantic_llm._FACT_V2_REPAIR_SUFFIX],
        "registry": jev.REGISTRY_VERSION,
        "schema": SCHEMA_VERSION,
        "policy": POLICY_VERSION,
        "chunk_plan": PLAN_VERSION,
    })


def thread_bundle(ledger, project_id: int, root_id: int,
                  target_ids: list | None = None,
                  local_model: str | None = None) -> dict | None:
    """One post + its same-thread stored replies — the atomic analysis
    unit (§12.3). A message whose row vanished makes the bundle
    unbuildable (None), never silently re-scoped (AT-019)."""
    rows = ledger.db.execute("""
      SELECT project_id,message_id,parent_id,sender_id,sender_name,organization,
             sender_type,profession,posted_at,
             body_text,body_state,content_hash,reply_count
      FROM messages WHERE project_id=? AND (message_id=? OR parent_id=?)
      ORDER BY posted_at_ts, message_id
    """, (project_id, root_id, root_id)).fetchall()
    root = [r for r in rows if r["message_id"] == root_id]
    if not root:
        return None
    member_ids = {r["message_id"] for r in rows}
    if target_ids is not None and (
            not isinstance(target_ids, list) or not target_ids
            or any(type(mid) is not int or mid not in member_ids
                   for mid in target_ids)):
        raise ValueError("semantic_target_scope")
    members = [_member(r) for r in rows]
    attachments = {mid: [] for mid in member_ids}
    for row in ledger.db.execute("""
      SELECT a.message_id,a.attachment_id,a.file_id,a.name,a.bytes,a.sha256,a.state
      FROM attachments a JOIN messages m ON m.message_id=a.message_id
      WHERE m.project_id=? AND (m.message_id=? OR m.parent_id=?)
        AND a.state != 'withdrawn'
      ORDER BY a.attachment_id
    """, (project_id, root_id, root_id)):
        attachment = dict(row)
        message_id = attachment.pop("message_id")
        # A reply committed after the member snapshot belongs to the next bundle.
        if message_id in attachments:
            attachments[message_id].append(attachment)
    for m in members:
        m["attachments"] = attachments[m["message_id"]]
        m["role"] = ("target" if target_ids
                     and m["message_id"] in target_ids else
                     "root" if m["parent_id"] is None else "context")
    missing_replies = max(0, (root[0]["reply_count"] or 0) - (len(rows) - 1))
    quality = "full" if not missing_replies and all(
        m["body_state"] in ("full", "deleted") for m in members) \
        else "partial"
    fp = bundle_fingerprint(members, local_model=local_model)
    return {"bundle_id": f"bundle_{project_id}_{root_id}_{fp[:12]}",
            "account_scope": "mcs",
            "project_id": project_id, "root_id": root_id,
            "members": members, "content_quality": quality,
            "context_complete": quality == "full",
            "missing_replies": missing_replies,
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


def _current(ledger, kind: str, message_id: int, fp: str, policy=None):
    """Latest artifact of `kind` for `message_id` whose meta fingerprint
    still matches the live input — older generations stay recorded as
    history but are never 'current' (AT-035/056)."""
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? "
            "AND message_id=? ORDER BY artifact_id DESC",
            (kind, message_id)):
        meta = loads_dict(r["meta"])
        if meta is None:
            continue
        if kind == KIND_ASSESS and meta.get("fact_id") is not None:
            continue
        if kind == KIND_FACT_PROJ and meta.get("invalidated"):
            continue
        if meta.get("fingerprint") == fp and (
                policy is None or meta.get("policy_fingerprint") == policy):
            content = loads_dict(r["content"])
            if content is None:
                return None
            return {"content": content, "meta": meta}
    return None


def invalidate_projections(ledger, scfg: dict) -> int:
    """Persist source/policy expiry for readers of the published snapshot."""
    from semantic_policy import policy_fingerprint
    policy = policy_fingerprint(scfg)
    off = scfg.get("mode") == "off"
    enabled = not off and scfg.get("fact_source") == "canonical"
    rows = ledger.db.execute("""
      SELECT a.artifact_id,a.project_id,a.meta,m.parent_id,a.message_id
      FROM artifacts a LEFT JOIN messages m ON m.message_id=a.message_id
      WHERE a.kind IN ('canonical_projection','semantic_facts_v4')
        AND CASE WHEN json_valid(a.meta) AND json_type(a.meta)='object' THEN
             COALESCE(json_extract(a.meta,'$.invalidated'),0)=0
             OR (? AND json_extract(a.meta,'$.invalidated')=1
                 AND json_extract(a.meta,'$.invalidated_reason')='fact_source')
             END
    """, (enabled or off,)).fetchall()
    metas = [loads_dict(r["meta"]) for r in rows]
    current_ids = set()
    if enabled and any(meta and meta.get("invalidated") for meta in metas):
        from mcs_queries import current_projection_id, current_v4_id
        # Evaluate the normal reader predicates against a prospective snapshot,
        # without exposing any row before source/policy and PASS checks succeed.
        current_ids = {r[0] for r in ledger.db.execute(f"""
          WITH artifacts AS (
            SELECT artifact_id,kind,project_id,message_id,content,
                   CASE WHEN json_valid(meta) THEN
                     CASE WHEN json_extract(meta,'$.invalidated')=1
                           AND json_extract(meta,'$.invalidated_reason')='fact_source'
                          THEN json_remove(meta,'$.invalidated')
                          ELSE meta END
                     ELSE meta END AS meta
            FROM main.artifacts
            WHERE kind IN ('canonical_projection','semantic_facts_v4')
          )
          SELECT a.artifact_id FROM artifacts a JOIN messages m
            ON m.message_id=a.message_id AND m.project_id=a.project_id
          WHERE m.body_state IS NOT 'deleted'
            AND a.artifact_id=CASE WHEN a.kind='canonical_projection'
                THEN {current_projection_id()} ELSE {current_v4_id()} END
        """)}
    bundles, expired, revived, projects = {}, [], [], set()
    local_model = None
    for row, meta in zip(rows, metas):
        if meta is None:
            expired.append(("off" if off else "metadata", row["artifact_id"]))
            projects.add(row["project_id"])
            continue
        key = (row["project_id"], row["parent_id"] or row["message_id"])
        # OFF remains a permanent, config-read-free revocation. Unknown
        # historical invalidations are excluded above and never acquire a reason.
        reason = "off" if off else "fact_source" if not enabled else "policy"
        if enabled and meta.get("policy_fingerprint") == policy:
            reason = "source"
            if key not in bundles:
                if local_model is None:
                    # one config read per scan, not one per thread
                    import semantic
                    local_model = semantic.llm_model()
                bundle = thread_bundle(ledger, *key,
                                       local_model=local_model)
                bundles[key] = bundle["source_fingerprint"] if bundle else None
            if bundles[key] is not None and meta.get("fingerprint") == bundles[key]:
                if not meta.get("invalidated"):
                    continue
                if row["artifact_id"] in current_ids:
                    from semantic_v4 import reproject_doc
                    doc, _ = reproject_doc(ledger, row["message_id"], meta)
                    if doc is not None:
                        revived.append((row["artifact_id"],))
                        projects.add(row["project_id"])
                        continue
        expired.append((reason, row["artifact_id"]))
        projects.add(row["project_id"])
    if expired or revived:
        with ledger.db:
            ledger.db.executemany(
                "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'),"
                "'$.invalidated_reason',?) "
                "WHERE artifact_id=?", expired)
            ledger.db.executemany(
                "UPDATE artifacts SET meta=json_remove(meta,'$.invalidated',"
                "'$.invalidated_reason') WHERE artifact_id=?", revived)
            ledger.db.executemany(
                "DELETE FROM artifacts WHERE kind='patient_rollup' AND project_id=?",
                [(pid,) for pid in projects])
    return len(expired)


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
        meta = loads_dict(r["meta"])
        count = meta.get("jev_requests") if isinstance(meta, dict) else None
        if type(count) is not int or count < 0:
            # Unknown/corrupt usage cannot replenish a spending budget.
            raise ValueError("semantic_usage_invalid")
        total += count
    return total
