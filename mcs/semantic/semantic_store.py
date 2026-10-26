"""Semantic input bundles and artifact read helpers (spec §12.1).

Thread bundling, fingerprints, current-artifact lookup, and the
durable Jev daily-usage counter. Policy constants come from
semantic_policy. Projection lifecycle writes belong to semantic_v4."""
from __future__ import annotations
import semantic_llm
import semantic_transport

import math
import time

from mcs_util import payload_hash
from mcs_util import loads_dict
from inference_version import CURRENT_VERSION, VERSION_KEY, is_current
import semantic_jev as jev
from semantic_policy import (KIND_ASSESS, KIND_USAGE,
                             POLICY_VERSION, SCHEMA_VERSION)


def _member(row) -> dict:
    return {
        "project_id": row["project_id"],
        "message_id": row["message_id"],
        "parent_id": row["parent_id"],
        "revision": row["content_hash"] or "",
        "source_occurrence": row["source_occurrence"] if "source_occurrence" in row.keys() else 1,
        "posted_at": row["posted_at"] or "",
        "occurred_at": None,      # event time is a fact-level field
        "sender": {"id": row["sender_id"], "type": row["sender_type"] or "",
                   "profession": row["profession"] or ""},
        "source_metadata": {"sender_name": row["sender_name"] or "",
                            "organization": row["organization"] or ""},
        "body_original": row["body_text"] or "",
        "body_state": 'full' if row["body_state"] is None and row["body_text"] else row["body_state"] or "unknown",
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
    from clinical_chunking import PLAN_VERSION
    if local_model is None:
        local_model = semantic_transport.llm_model()
    return payload_hash({
        VERSION_KEY: CURRENT_VERSION,
        # Target roles are selection metadata; artifacts are selected per
        # target ID. Everything used to interpret the source is versioned.
        "members": sorted(({k: v for k, v in m.items() if k != "role"}
                           for m in members), key=lambda x: x["message_id"]),
        "model": model,
        "local_model": local_model,
        "prompts": [semantic_llm._FACT_PROMPT, semantic_llm._SUMMARY_PROMPT,
                    semantic_llm._REPAIR_SUFFIX, semantic_llm._FACT_V2_PROMPT,
                    semantic_llm._FACT_V2_REPAIR_SUFFIX],
        "registry": jev.REGISTRY_VERSION,
        "schema": SCHEMA_VERSION,
        "policy": POLICY_VERSION,
        "chunk_plan": PLAN_VERSION,
    })


def thread_bundle(db, project_id: int, root_id: int,
                  target_ids: list | None = None,
                  local_model: str | None = None) -> dict | None:
    """One post + its same-thread stored replies — the atomic analysis
    unit (§12.3). A message whose row vanished makes the bundle
    unbuildable (None), never silently re-scoped (AT-019)."""
    has_revisions = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_revisions'").fetchone()
    rows = db.execute("""
      SELECT project_id,message_id,parent_id,sender_id,sender_name,organization,
             sender_type,profession,posted_at,
             body_text,body_state,content_hash,reply_count
      FROM messages WHERE project_id=? AND (message_id=? OR parent_id=?)
      ORDER BY posted_at_ts, message_id
    """, (project_id, root_id, root_id)).fetchall()
    occurrences = ({row[0]: row[1] for row in db.execute(
        'SELECT message_id,MAX(seq) FROM message_revisions WHERE message_id IN '
        '(SELECT message_id FROM messages WHERE project_id=? AND (message_id=? OR parent_id=?)) '
        'GROUP BY message_id', (project_id, root_id, root_id))} if has_revisions else {})
    rows = [{**dict(row), 'source_occurrence': occurrences.get(row['message_id'], 1)} for row in rows]
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
    for row in db.execute("""
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


def current_artifact(db, kind: str, message_id: int, fp: str, policy=None):
    """Latest artifact of `kind` for `message_id` whose meta fingerprint
    still matches the live input — older generations stay recorded as
    history but are never 'current' (AT-035/056)."""
    for r in db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? "
            "AND message_id=? ORDER BY artifact_id DESC",
            (kind, message_id)):
        meta = loads_dict(r["meta"])
        if meta is None or not is_current(meta):
            continue
        if kind == KIND_ASSESS and meta.get("fact_id") is not None:
            continue
        # an invalidated row (projection expiry, or a summary/audit/coverage
        # held after its canonical document was rebuilt) is never reused
        if meta.get("invalidated"):
            continue
        if meta.get("fingerprint") == fp and (
                policy is None or meta.get("policy_fingerprint") == policy):
            content = loads_dict(r["content"])
            if content is None:
                return None
            return {"content": content, "meta": meta}
    return None


def refresh_cards_allowed(db, project_id, message_id):
    """Replay-only extraction does not adopt old external card deliveries."""
    if db is None or getattr(db, 'in_transaction', True):
        return True  # the existing refresh callback already skips borrowed transactions
    row = db.execute(
        "SELECT j.payload FROM messages m JOIN fetch_jobs j ON j.kind='semantic' "
        'AND j.project_id=m.project_id AND j.message_id=COALESCE(m.parent_id,m.message_id) '
        'WHERE m.project_id=? AND m.message_id=?', (project_id, message_id)).fetchone()
    payload = loads_dict(row[0]) if row is not None else {}
    return not (isinstance(payload, dict) and payload.get('reprocess_only') is True
                and payload.get('notification_free') is True)




def _jst_day_start(now: float) -> float:
    """Epoch of the current JST midnight — the operation's actual 'day'
    boundary (UTC midnight would reset the budget at 09:00 JST)."""
    return math.floor((now + 9 * 3600) / 86400) * 86400 - 9 * 3600


def jev_usage_today(ledger) -> int:
    """Durable daily Jev request count — shadow traffic spends real API
    budget too, so it is never unbounded (§13.5, AT-067). Sums the
    jev_requests of today's semantic_usage rows: real Jev clients reserve
    one row per request before POST (usage_reserver / manual_usage_reserver),
    while injected fake clients get one post-job delta row from run_due, so
    every external call (primary, detail, claim-audit, loop-relation,
    failed) is counted exactly once."""
    day = _jst_day_start(time.time())
    # Committed rows are append-only (AUTOINCREMENT never reuses a committed
    # id): resume from the last row already summed instead of re-parsing the
    # whole day on every call. Inside a transaction the sum may include this
    # connection's uncommitted rows, whose ids a rollback hands out again, so
    # only a read outside one moves the cache.
    cache = getattr(ledger, "_jev_usage_cache", None)
    cached_day, last_id, total = cache if cache and cache[0] == day else (day, 0, 0)
    for r in ledger.db.execute(
            "SELECT artifact_id, meta FROM artifacts WHERE kind=? AND created_at>=? "
            "AND artifact_id>? ORDER BY artifact_id", (KIND_USAGE, day, last_id)):
        meta = loads_dict(r["meta"])
        count = meta.get("jev_requests") if isinstance(meta, dict) else None
        if type(count) is not int or count < 0:
            # Unknown/corrupt usage cannot replenish a spending budget;
            # the cache stays before this row so every call re-hits it.
            raise ValueError("semantic_usage_invalid")
        total += count
        last_id = r["artifact_id"]
    if not ledger.db.in_transaction:
        ledger._jev_usage_cache = [cached_day, last_id, total]
    return total
