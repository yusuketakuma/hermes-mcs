"""Semantic input bundles and artifact read helpers (spec §12.1).

Thread bundling, fingerprints, current-artifact lookup, and the
durable Jev daily-usage counter. Policy constants come from
semantic_policy. Projection lifecycle writes belong to semantic_publication."""
from __future__ import annotations
import semantic_llm
import semantic_transport

import json
import math
import sqlite3
import time

import mcs_util

from mcs_util import payload_hash
from mcs_util import load_config, loads_dict, strict_json
from inference_version import CURRENT_VERSION, LEGACY_VERSION, VERSION_KEY, is_current, read_version
import semantic_jev as jev
from semantic_policy import (JOB_KIND, KIND_ASSESS, KIND_AUDIT, KIND_FACT_AUDIT, KIND_FACTS,
                             KIND_FACTS_V2, KIND_LOOP, KIND_LOOP_EVENT, KIND_SUMMARY, KIND_USAGE,
                             POLICY_VERSION, SCHEMA_VERSION, policy_fingerprint)


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
        try:
            meta = strict_json(r["meta"])
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise ValueError("semantic_usage_invalid") from None
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


_REPROCESS_JOB_MAX_BYTES = 1024 * 1024
KIND_LOOP_COMPLETION = 'semantic_loop_completion'


def _partial_assessment(answers, scfg):
    """Validate and reduce previously answered proposition chunks to codes."""
    if (not isinstance(answers, dict)
            or set(answers) - set(jev.active_propositions(scfg))):
        raise ValueError("semantic_partial_assessment_invalid")
    out = {}
    for name, answer in answers.items():
        value = answer.get("noul") if isinstance(answer, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("semantic_partial_assessment_invalid")
        out[name] = {"noul": value, "verdict": jev.verdict_for(
            value, scfg["match_threshold"], scfg["nomatch_threshold"])}
    return out


def _verified_prefilter(ledger, pid, mid):
    import extract_llm
    import patient_context_refresh
    from mcs_queries import current_extract_pred
    try:
        source = ledger.db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?',
                                   (pid, mid)).fetchone()
        raw = ledger.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm' AND message_id=? "
                                "AND (project_id IS NULL OR project_id=?) ORDER BY artifact_id DESC LIMIT 1",
                                (mid, pid)).fetchone()
        meta = strict_json(raw['meta']) if raw is not None else None
        content = strict_json(raw['content']) if raw is not None else None
        current = (ledger.db.execute(
            "SELECT 1 FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
            f"WHERE a.artifact_id=? AND m.project_id=? {current_extract_pred()}",
            (raw['artifact_id'], pid)).fetchone() if raw is not None else None)
        return bool(source is not None and isinstance(meta, dict) and isinstance(content, dict)
                    and current is not None and is_current(meta)
                    and not meta.get('invalidated') and meta.get('prefilter') == 'no_signal'
                    and meta.get('hash') == source['content_hash']
                    and patient_context_refresh._saved_cloud_contract(meta) is None
                    and extract_llm._low_signal(source['body_text'], extract_llm._rule_hints(dict(source))))
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return False


def _valid_primary_assessment(content, meta, model, mid, fp, policy, scfg, *, prefiltered=False):
    try:
        if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                or meta.get('invalidated') or meta.get('stale') or meta.get('fact_id') is not None
                or meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy
                or meta.get('schema') != SCHEMA_VERSION or model != jev.JEV_MODEL
                or type(content.get('target_message_id')) is not int or content['target_message_id'] != mid):
            return False
        verdicts = content.get('verdicts')
        if _partial_assessment(verdicts, scfg) != verdicts:
            return False
        status = meta.get('technical_status')
        if status != 'complete':
            return (status in ('retry_wait', 'error', 'pending', 'skipped')
                    and (not verdicts or (meta.get('registry') == jev.REGISTRY_VERSION
                                          and meta.get('model') == jev.JEV_MODEL)))
        if meta.get('registry') != jev.REGISTRY_VERSION or meta.get('model') != jev.JEV_MODEL:
            return False
        return (set(verdicts) == set(jev.active_propositions(scfg))
                or (verdicts == {} and prefiltered and meta.get('skipped') == 'prefilter_no_signal'
                    and type(meta.get('jev_requests')) is int and meta['jev_requests'] == 0))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return False


def _primary_assessment_history(db, pid, mid, fp, policy):
    assessment, previous = None, []
    for stored in db.execute('SELECT content,meta,model FROM artifacts WHERE kind=? AND project_id=? '
                             'AND message_id=? ORDER BY artifact_id DESC', (KIND_ASSESS, pid, mid)):
        content, meta = strict_json(stored['content']), strict_json(stored['meta'])
        if not isinstance(content, dict) or not isinstance(meta, dict):
            raise ValueError('semantic_assessment_history_invalid')
        if meta.get('fact_id') is not None:
            if not _valid_detail_assessment(content, meta, mid):
                raise ValueError('semantic_assessment_detail_invalid')
            continue
        if (type(meta.get('fingerprint')) is not str or not meta['fingerprint']
                or type(meta.get('policy_fingerprint')) is not str or not meta['policy_fingerprint']):
            raise ValueError('semantic_assessment_binding_invalid')
        if meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy:
            continue
        if assessment is None:
            assessment = {'content': content, 'meta': meta, 'model': stored['model']}
        else:
            previous.append((content, meta, stored['model']))
    return assessment, previous


def _valid_primary_history(content, meta, previous, mid, fp, policy, scfg):
    verdicts = content.get('verdicts')
    for old, old_meta, old_model in previous:
        old_status = old_meta.get('technical_status')
        if old_status not in ('complete', 'retry_wait', 'error', 'pending', 'skipped'):
            return False
        if meta.get('technical_status') != 'skipped' and old_status == 'complete':
            continue
        old_verdicts = old.get('verdicts')
        if (not is_current(old_meta) or old_meta.get('invalidated') or old_meta.get('stale')
                or old_meta.get('fingerprint') != fp or old_meta.get('policy_fingerprint') != policy
                or old_meta.get('schema') != SCHEMA_VERSION or old_model != jev.JEV_MODEL
                or (old_verdicts and (old_meta.get('registry') != jev.REGISTRY_VERSION
                                      or old_meta.get('model') != jev.JEV_MODEL))
                or type(old.get('target_message_id')) is not int or old['target_message_id'] != mid
                or _partial_assessment(old_verdicts, scfg) != old_verdicts
                or any(verdicts.get(key) != value for key, value in old_verdicts.items())):
            return False
    return True


def _reprocess_job_payload(ledger, row, project_id, root):
    """Read a bounded, supported job without changing uncertain evidence."""
    raw = row['payload']
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > _REPROCESS_JOB_MAX_BYTES:
        return None
    try:
        value = strict_json(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or read_version(value) not in (CURRENT_VERSION, LEGACY_VERSION):
        return None
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 128 or (type(node) is int and not -(2**63) <= node < 2**63):
            return None
        if isinstance(node, dict):
            stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)
    if 'targets' not in value:
        return value  # Older jobs use their root as the default target.
    targets = value['targets']
    if (not isinstance(targets, list) or not targets
            or any(type(mid) is not int or not 0 < mid < 2**63 for mid in targets)):
        return None
    if ledger.db.execute(
            'SELECT 1 FROM json_each(?) target WHERE NOT EXISTS '
            '(SELECT 1 FROM messages WHERE project_id=? AND message_id=target.value '
            'AND (message_id=? OR parent_id=?)) LIMIT 1',
            (json.dumps(targets), project_id, root, root)).fetchone():
        return None
    return value


def _valid_detail_assessment(content, meta, mid):
    import semantic_assessment
    return (meta.get('schema') == semantic_assessment.ASSESSMENT_SCHEMA_VERSION
            and content.get('schema') == semantic_assessment.ASSESSMENT_SCHEMA_VERSION
            and meta.get('technical_status') == 'complete'
            and isinstance(meta.get('fact_id'), str) and isinstance(meta.get('dimension'), str)
            and content.get('fact_id') == meta['fact_id'] and content.get('dimension') == meta['dimension']
            and type(content.get('target_message_id')) is int and content['target_message_id'] == mid
            and isinstance(content.get('choice'), str)
            and semantic_assessment._finite_confidence(content.get('confidence')) is not None)


def _loop_skip_ref(db, pid, root, fp, policy, reason):
    """Identify the newest valid loop-skip record for this generation."""
    row = db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                     'AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                     ('semantic_loop_skipped', pid, root)).fetchone()
    if row is None:
        return None
    try:
        content, meta = strict_json(row['content']), strict_json(row['meta'])
        if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                or meta.get('invalidated') or meta.get('stale') or meta.get('schema') != SCHEMA_VERSION
                or meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy
                or content != {'status': 'SKIPPED', 'reason': reason}):
            return None
        return [row['artifact_id'], mcs_util.payload_hash([content, meta])]
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return None


def _loop_completion_manifest(db, pid, bundle, targets, scfg, *, facts_by_target=None, skipped=False,
                              deadline=None):
    """Bind actual fact and loop artifacts for a completed target set."""
    import semantic_runtime as runtime
    import semantic_loops
    import semantic_projection

    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise runtime.RuntimeBudgetShort('loop_completion')

    check_deadline()
    fp, policy = bundle['source_fingerprint'], policy_fingerprint(scfg)
    members = {member['message_id']: member for member in bundle['members']}
    if (not isinstance(targets, list) or not targets or len(targets) != len(set(targets))
            or any(type(mid) is not int or mid not in members for mid in targets)):
        return None
    refs, candidates, events, rejections = [], {}, {}, []
    fact_kind = KIND_FACTS_V2 if scfg.get('fact_source') == 'canonical' else KIND_FACTS
    for mid in targets:
        check_deadline()
        found = None
        for row in db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                              'AND message_id=? ORDER BY artifact_id DESC', (fact_kind, pid, mid)):
            check_deadline()
            meta = strict_json(row['meta'])
            if not isinstance(meta, dict):
                return None
            if meta.get('fingerprint') != fp:
                continue
            content = strict_json(row['content'])
            if (not isinstance(content, dict) or not is_current(meta) or meta.get('invalidated')
                    or meta.get('stale') or meta.get('schema') != SCHEMA_VERSION):
                return None
            facts = (semantic_projection.project_v2_facts(content) if fact_kind == KIND_FACTS_V2
                     else content.get('facts'))
            if (not isinstance(facts, list) or facts_by_target is not None and facts != facts_by_target.get(mid)):
                return None
            found = row
            refs.append([fact_kind, mid, row['artifact_id'], mcs_util.payload_hash([content, meta])])
            break
        if found is None:
            return None
        if fact_kind == KIND_FACTS_V2:
            audit = db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                               'AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                               (KIND_FACT_AUDIT, pid, mid)).fetchone()
            if audit is None:
                return None
            audit_content, audit_meta = strict_json(audit['content']), strict_json(audit['meta'])
            if (not isinstance(audit_content, dict) or not isinstance(audit_meta, dict)
                    or not is_current(audit_meta) or audit_meta.get('invalidated') or audit_meta.get('stale')
                    or audit_meta.get('fingerprint') != fp or audit_meta.get('policy_fingerprint') != policy
                    or audit_content.get('status') not in ('PASS', 'SKIPPED')):
                return None
            refs.append([KIND_FACT_AUDIT, mid, audit['artifact_id'],
                         mcs_util.payload_hash([audit_content, audit_meta])])
        assessments = 0
        for row in db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? '
                              'AND message_id=? ORDER BY artifact_id', (KIND_ASSESS, pid, mid)):
            check_deadline()
            meta = strict_json(row['meta'])
            if not isinstance(meta, dict):
                return None
            if meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy:
                continue
            content = strict_json(row['content'])
            if (not isinstance(content, dict)
                    or (not is_current(meta) if meta.get('fact_id') is None
                        else not _valid_detail_assessment(content, meta, mid))):
                return None
            refs.append([KIND_ASSESS, mid, row['artifact_id'], mcs_util.payload_hash([content, meta])])
            if meta.get('fact_id') is None:
                assessments += 1
        if not assessments:
            return None
    def loop_rows():
        for kind in (KIND_LOOP, KIND_LOOP_EVENT, semantic_loops.KIND_LOOP_REJECTED):
            for mid in sorted(members):
                check_deadline()
                yield from db.execute('SELECT artifact_id,kind,message_id,content,meta FROM artifacts '
                                      'WHERE kind=? AND project_id=? AND message_id=? ORDER BY artifact_id',
                                      (kind, pid, mid))
    for row in loop_rows():
        check_deadline()
        meta = strict_json(row['meta'])
        if not isinstance(meta, dict):
            return None
        if meta.get('fingerprint') != fp or meta.get('policy_fingerprint') != policy:
            continue
        content = strict_json(row['content'])
        if not isinstance(content, dict) or not is_current(meta):
            return None
        refs.append([row['kind'], row['message_id'], row['artifact_id'], mcs_util.payload_hash([content, meta])])
        if meta.get('invalidated') or meta.get('stale'):
            continue
        if row['kind'] == KIND_LOOP:
            source = members.get(row['message_id'])
            origin = content.get('origin')
            if (not semantic_loops._valid_candidate(content, meta) or not isinstance(origin, dict)
                    or source is None or origin.get('message_id') != row['message_id']
                    or origin.get('revision') != source['revision'] or not origin.get('evidence')
                    or content.get('root_id') != bundle['root_id']
                    or content.get('project_id') != pid or content.get('account_scope') != bundle['account_scope']
                    or meta.get('registry') != jev.REGISTRY_VERSION):
                return None
            candidates[row['artifact_id']] = (row['message_id'], content)
        elif row['kind'] == KIND_LOOP_EVENT:
            key = (content.get('loop_artifact_id'), content.get('trigger_message_id'))
            if (type(key[0]) is not int or type(key[1]) is not int or key in events
                    or content.get('trigger_message_id') != row['message_id']
                    or meta.get('registry') != jev.REGISTRY_VERSION
                    or content.get('relation') not in jev.LOOP_RELATION_OPTIONS
                    or content.get('reported_relation') not in jev.LOOP_RELATION_OPTIONS
                    or type(content.get('confidence')) not in (int, float)
                    or not math.isfinite(content['confidence']) or not 0 <= content['confidence'] <= 1):
                return None
            events[key] = content
        else:
            rejections.append((content, meta))
    for content, meta in rejections:
        check_deadline()
        scope = meta.get('retry_scope')
        if not isinstance(scope, str):
            return None
        rejected = semantic_loops.rejected_relation_pair(content, meta, policy, scope)
        if rejected is None:
            return None
        (aid, mid, revision), _ = rejected
        if (aid in candidates and mid in targets and revision == members[mid]['revision']
                and (skipped or (aid, mid) not in events)):
            return None
    if not skipped:
        for aid, (origin_id, candidate) in candidates.items():
            for mid in targets:
                check_deadline()
                if mid == origin_id:
                    continue
                event = events.get((aid, mid))
                if (event is None or event.get('root_id') != bundle['root_id']
                        or event.get('origin_revision') != members[origin_id]['revision']
                        or event.get('trigger_revision') != members[mid]['revision']
                        or event.get('candidate_state') != candidate.get('state')):
                    return None
    check_deadline()
    digest = mcs_util.payload_hash({'targets': sorted(targets), 'artifacts': refs})
    check_deadline()
    return digest


def _classification_only_state(ledger, row, scfg, cfg, bundle, fp, policy):
    """Validate current raw and primary assessment evidence for classification only."""
    import semantic_audit
    import semantic_facts
    import semantic_publication as v4
    import extract_llm
    import patient_context_refresh
    import semantic_intake_hold

    db, pid, mid = ledger.db, row['project_id'], row['message_id']
    root = row['parent_id'] or mid
    try:
        if semantic_intake_hold.has_unstored(db, pid) or semantic_intake_hold.has_held_root(db, pid, root):
            return 'held'
        job = db.execute("SELECT state,substr(payload,1,?) AS payload FROM fetch_jobs WHERE kind=? "
                         "AND project_id=? AND message_id=?",
                         (_REPROCESS_JOB_MAX_BYTES + 1, JOB_KIND, pid, root)).fetchone()
        if job is not None:
            if _reprocess_job_payload(ledger, job, pid, root) is None:
                return 'held'
            if job['state'] == 'failed':
                return 'failed'
            if job['state'] != 'done':
                return 'pending'
        source = db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?', (pid, mid)).fetchone()
        if source is None:
            return 'held'
        if (scfg['extract_qc'] == 'annotate'
                and not patient_context_refresh._extraction_qc_ready(db, pid, scfg, message_id=mid)):
            return 'held'
        complete, selected_meta = patient_context_refresh._raw_extraction_current(db, source, cfg)
        latest = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
                            "AND (project_id IS NULL OR project_id=?) ORDER BY artifact_id DESC LIMIT 1",
                            (extract_llm.KIND, mid, pid)).fetchone()
        if not complete or latest is None:
            return 'held'
        raw, raw_meta = strict_json(latest['content']), strict_json(latest['meta'])
        if (not isinstance(raw, dict) or not isinstance(raw_meta, dict) or raw_meta != selected_meta
                or not is_current(raw_meta) or raw_meta.get('invalidated')):
            return 'held'
        # A newer adverse or unparseable audit never becomes a classification success.
        for kind in (KIND_AUDIT, KIND_FACT_AUDIT):
            audit = db.execute('SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND message_id=? '
                               'ORDER BY artifact_id DESC LIMIT 1', (kind, pid, mid)).fetchone()
            if audit is None:
                continue
            content, meta = strict_json(audit['content']), strict_json(audit['meta'])
            if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                    or not isinstance(meta.get('fingerprint'), str)
                    or not isinstance(meta.get('policy_fingerprint'), str)):
                return 'held'
            if meta.get('fingerprint') == fp and meta.get('policy_fingerprint') == policy:
                status, findings = content.get('status'), content.get('findings', [])
                if (meta.get('invalidated') or status not in ('PASS', 'SKIPPED')
                        or not isinstance(findings, list) or any(not isinstance(item, dict)
                            or not isinstance(item.get('code'), str) for item in findings)):
                    return 'held'
                if kind == KIND_FACT_AUDIT:
                    facts = current_artifact(db, KIND_FACTS_V2, mid, fp)
                    if facts is None or meta.get('doc_hash') != semantic_facts.doc_hash(facts['content']):
                        return 'held'
                    if status == 'PASS':
                        if findings or content.get('evaluated') is not True or v4.reproject_doc(db, mid, meta)[0] is None:
                            return 'held'
                    else:
                        member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
                        reason = content.get('jev_skip_reason')
                        if (content.get('evaluated') is not False or content.get('code_checks') != 'passed'
                                or content.get('document_hash') != mcs_util.payload_hash(facts['content'])
                                or semantic_audit.optional_fact_audit(facts['content'], member, fp, reason, content,
                                    match_threshold=scfg['match_threshold'])['status'] != 'SKIPPED'):
                            return 'held'
                else:
                    import semantic_send_gate
                    member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
                    mode = meta.get('publication_mode')
                    if (member is None or mode not in ('shadow', 'assist', 'enforce')
                            or semantic_send_gate._paired_summary(db, pid, mid, fp, policy,
                                member['revision'], mode, status) is None):
                        return 'held'
                if kind == KIND_AUDIT and status == 'PASS':
                    if (any(item['code'] in ('fact_contradicts', 'fact_not_supported', 'fact_ambiguous',
                                              'fact_low_confidence') for item in findings)
                            or semantic_audit.audit_status_for(findings, findings, True, True) != 'PASS'):
                        return 'held'
                elif kind == KIND_AUDIT and (meta.get('jev_evaluated') is not False or meta.get('code_checks') != 'passed'
                      or meta.get('jev_skip_reason') not in ('not_configured', 'disabled', 'daily_budget',
                          'backlog_reserved', 'service_unavailable', 'circuit_open')
                      or any(item['code'] != 'support_unevaluated' for item in findings)):
                    return 'held'
        assessment, previous = _primary_assessment_history(db, pid, mid, fp, policy)
        if assessment is None:
            return 'held'
        content, meta, model = assessment['content'], assessment['meta'], assessment['model']
        if (not _valid_primary_assessment(content, meta, model, mid, fp, policy, scfg,
                                          prefiltered=_verified_prefilter(ledger, pid, mid))
                or not _valid_primary_history(content, meta, previous, mid, fp, policy, scfg)):
            return 'held'
        if meta.get('technical_status') == 'complete':
            if meta.get('registry') != jev.REGISTRY_VERSION or meta.get('model') != jev.JEV_MODEL:
                return 'held'
            return 'current'
        if (meta.get('technical_status') != 'skipped' or meta.get('jev_skip_reason') not in
                ('not_configured', 'disabled', 'daily_budget', 'backlog_reserved', 'service_unavailable', 'circuit_open')):
            return 'held'
        return 'current_skipped'
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return 'held'


def _loop_only_state(ledger, row, scfg, cfg, bundle, fp, policy, deadline=None):
    """Require a current classification and a bound completed loop pass."""
    import semantic_runtime as runtime
    base = _classification_only_state(ledger, row, scfg, cfg, bundle, fp, policy)
    if base not in ('current', 'current_skipped'):
        return base
    db, pid, mid = ledger.db, row['project_id'], row['message_id']
    try:
        receipt = db.execute('SELECT content,meta,model FROM artifacts WHERE kind=? AND project_id=? '
                             'AND message_id=? ORDER BY artifact_id DESC LIMIT 1',
                             (KIND_LOOP_COMPLETION, pid, mid)).fetchone()
        if receipt is None:
            return 'held'
        content, meta = strict_json(receipt['content']), strict_json(receipt['meta'])
        if not isinstance(content, dict) or not isinstance(meta, dict):
            return 'held'
        targets, status = content.get('target_ids'), content.get('status')
        member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
        if (not isinstance(targets, list) or not targets or any(type(item) is not int for item in targets)
                or targets != sorted(set(targets)) or mid not in targets or member is None
                or set(content) != {'status', 'target_ids', 'loop_mode', 'semantic_mode',
                                    'manifest', 'skip_reason', 'skip_ref'}
                or status not in ('DONE', 'SKIPPED') or content['loop_mode'] != scfg['loop_mode']
                or content['semantic_mode'] != scfg['mode']
                or not is_current(meta) or meta.get('invalidated') or meta.get('stale')
                or meta.get('schema') != SCHEMA_VERSION or meta.get('fingerprint') != fp
                or meta.get('policy_fingerprint') != policy or meta.get('target_revision') != member['revision']
                or meta.get('execution_generation') != runtime.execution_generation(scfg)
                or meta.get('loop_status') != status or meta.get('skip_reason') != content.get('skip_reason')
                or meta.get('content_hash') != mcs_util.payload_hash(content) or receipt['model'] != jev.JEV_MODEL):
            return 'held'
        if status == 'SKIPPED':
            reason = content.get('skip_reason')
            if (reason not in ('not_configured', 'disabled', 'daily_budget', 'backlog_reserved',
                               'service_unavailable', 'circuit_open')
                    or content.get('skip_ref') is None
                    or content['skip_ref'] != _loop_skip_ref(db, pid, bundle['root_id'], fp, policy, reason)):
                return 'held'
        elif content.get('skip_reason') is not None or content.get('skip_ref') is not None:
            return 'held'
        current = thread_bundle(db, pid, bundle['root_id'], targets)
        if current is None or current['source_fingerprint'] != fp:
            return 'held'
        states = []
        for target in targets:
            if deadline is not None and time.monotonic() >= deadline:
                return 'held'
            source = db.execute('SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m '
                                'JOIN patients p ON p.project_id=m.project_id '
                                'WHERE m.project_id=? AND m.message_id=?', (pid, target)).fetchone()
            if source is None:
                return 'held'
            state = _classification_only_state(ledger, source, scfg, cfg, current, fp, policy)
            if state not in ('current', 'current_skipped'):
                return 'held'
            states.append(state)
        if status == 'DONE' and 'current_skipped' in states:
            return 'held'
        manifest = _loop_completion_manifest(db, pid, current, targets, scfg,
                                             skipped=status == 'SKIPPED', deadline=deadline)
        if manifest is None or manifest != content.get('manifest'):
            return 'held'
        return 'current_skipped' if status == 'SKIPPED' or 'current_skipped' in states else 'current'
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError,
            runtime.RuntimeBudgetShort):
        return 'held'


def reprocess_target_state(ledger, row, scfg, *, cfg=None, deadline=None):
    """Observe one stored source without mistaking job completion for coverage."""
    from mcs_queries import current_v4_id, paused
    import semantic_publication as v4
    pid, mid = row['project_id'], row['message_id']
    if row['fetch_reason'] in ('forbidden', 'permission_denied', 'access_revoked'):
        return 'permission'
    if pid is None or row['patient_id'] is None:
        return 'scope'
    if scfg['project_ids'] is not None and pid not in scfg['project_ids']:
        return 'scope'
    if paused(ledger.db, pid):
        return 'paused'
    if row['body_state'] == 'deleted':
        return 'deleted'
    if row['body_state'] == 'full' and row['body_text'] == '':
        return 'empty_body'
    if row['body_state'] not in (None, 'full') or not row['body_text']:
        return 'body_unavailable'
    root = row['parent_id'] or mid
    bundle = thread_bundle(ledger.db, pid, root, [mid])
    if bundle is None:
        return 'held'
    fp, policy = bundle['source_fingerprint'], policy_fingerprint(scfg)
    if scfg['summary_mode'] == 'off' and scfg['loop_mode'] == 'off':
        return _classification_only_state(ledger, row, scfg, cfg if cfg is not None else load_config(), bundle, fp, policy)
    if scfg['summary_mode'] == 'off':
        return _loop_only_state(ledger, row, scfg, cfg if cfg is not None else load_config(),
                                bundle, fp, policy, deadline=deadline)
    audit = current_artifact(ledger.db, KIND_AUDIT, mid, fp, policy)
    summary = current_artifact(ledger.db, KIND_SUMMARY, mid, fp, policy)
    if audit and summary and audit['content'].get('status') == 'SKIPPED':
        import semantic_send_gate
        member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
        if (member is not None and semantic_send_gate.verified_skipped_summary_current(
                ledger.db, pid, mid, fp, policy, member['revision'], scfg['summary_mode'],
                bundle=bundle, fact_source=scfg.get('fact_source'))):
            if scfg.get('fact_source') != 'canonical':
                if current_artifact(ledger.db, KIND_FACTS, mid, fp) is not None:
                    return 'current_skipped'
            elif v4.current_skipped(ledger.db, pid, mid, fp, policy, member):
                return 'current_skipped'
        return 'held'
    if audit and summary and audit['content'].get('status') == 'PASS':
        import semantic_send_gate
        member = next((item for item in bundle['members'] if item['message_id'] == mid), None)
        if (member is None or not semantic_send_gate.passed_summary_current(
                ledger.db, pid, mid, fp, policy, member['revision'], scfg['summary_mode'],
                bundle=bundle, fact_source=scfg.get('fact_source'))):
            return 'held'
        if scfg.get('fact_source') != 'canonical':
            if current_artifact(ledger.db, KIND_FACTS, mid, fp) is not None:
                return 'current'
        vid = ledger.db.execute(f'SELECT {current_v4_id()} FROM messages m '
                                'WHERE m.message_id=?', (mid,)).fetchone()[0]
        published = ledger.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?',
                                       (vid,)).fetchone() if vid is not None else None
        meta = loads_dict(published[0]) if published is not None else {}
        if (meta and meta.get('fingerprint') == fp
                and meta.get('policy_fingerprint') == policy):
            doc, _ = v4.reproject_doc(ledger.db, mid, meta)
            if doc is not None:
                return 'current'
    job = ledger.db.execute("SELECT state,substr(payload,1,?) AS payload FROM fetch_jobs WHERE kind=? "
                            'AND project_id=? AND message_id=?',
                            (_REPROCESS_JOB_MAX_BYTES + 1, JOB_KIND, pid, root)).fetchone()
    if job is not None and is_current(loads_dict(job['payload'])):
        if job['state'] == 'failed':
            return 'failed'
        if audit and audit['content'].get('status') == 'NEEDS_REVIEW':
            return 'held'
        if job['state'] == 'done':
            return 'repair_required'
        return 'pending'
    return 'pending'
