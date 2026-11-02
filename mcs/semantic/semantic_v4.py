"""v4 publication, stage receipts, and fail-closed legacy migration.

The canonical chain in semantic_drain already walks S0–S7 (prep →
extract_facts_v2 → local validation + audit_facts_v2 → one reserved
repair → re-audit → projection → summary → claim audit). This module
mints that single self-correcting pipeline as engine v4:

- ``v4_stage`` receipts record every stage transition with
  ``engine_version=4``, the source fingerprint, the policy fingerprint
  and exact dependency ids (doc_hash) — the durable S0–S8 ledger a
  reviewer can replay.
- ``semantic_facts_v4`` is the PASS-only read model: v3-shaped content
  plus ``extract_version=4`` and full fact/evidence/doc_hash lineage.
  It is a DIFFERENT artifact kind, so the legacy ``_replace_current``
  DELETE (``kind='extract_llm'``) can never reach it, and
  ``current_fact_pred`` prefers it over every older generation — a
  delayed v3 writer can land but can never displace a published v4 row.
- ``v4_diagnostic`` carries non-PASS findings (PENDING/NEEDS_REVIEW/
  STALE) — a separate kind no ordinary extraction reader selects.
- ``v4_retire_cohort`` / ``v4_retire_item`` persist fixed conversion
  targets, scheduling reservations, and hold reasons. New inference
  stays closed until the adapters can enforce the total token ceiling.
  Physical cleanup additionally needs a separate verified recovery
  manifest; a conversion receipt never authorizes payload deletion.
"""
from __future__ import annotations

from inference_version import CURRENT_VERSION, VERSION_KEY, is_current, current_meta_pred
import semantic_facts
import semantic_store
import semantic_transport

import hashlib
import json
import time
from mcs_util import loads_dict, payload_hash, strict_json
from mcs_queries import (current_projection_id, current_v4_id, current_v4_pred,
                         json_object_or_null)
from semantic_policy import (SCHEMA_VERSION, JOB_KIND, KIND_AUDIT, KIND_COVERAGE,
                             KIND_FACTS_V2, KIND_FACT_AUDIT, KIND_FACT_PROJ,
                             KIND_FACT_REPAIR, KIND_LOOP, KIND_SUMMARY,
                             policy_fingerprint)

ENGINE_VERSION = 4
KIND_V4 = "semantic_facts_v4"
KIND_V4_SKIPPED = "semantic_facts_v4_skipped"
KIND_V4_STAGE = "v4_stage"
KIND_V4_DIAG = "v4_diagnostic"


def _repaired_projection_producer(db, pid, mid, fp, policy, member, doc, meta):
    """Verify a retained QC repair by replaying its bound answers without I/O."""
    import bounded_http
    import cloud_inference
    import semantic_audit
    import semantic_extraction
    import semantic_runtime
    from inference_dependency_loss import source_binding

    def row(artifact_id, kind):
        if type(artifact_id) is not int or artifact_id <= 0:
            raise ValueError('repair_reference_invalid')
        result = db.execute('SELECT content,meta FROM artifacts WHERE artifact_id=? AND kind=? '
                            'AND project_id=? AND message_id=?', (artifact_id, kind, pid, mid)).fetchone()
        if result is None:
            raise ValueError('repair_reference_missing')
        return strict_json(result['content']), strict_json(result['meta'])

    try:
        completed, completed_meta = row(meta.get('repair_artifact_id'), KIND_FACT_REPAIR)
        evidence = completed['__mcs_internal_proof_v1']
        if (not isinstance(evidence, dict) or type(evidence.get('version')) is not int
                or evidence['version'] != 1 or completed.get('repaired') is not True
                or completed_meta.get('fingerprint') != fp or completed_meta.get('policy_fingerprint') != policy
                or not is_current(completed_meta) or completed_meta.get('invalidated')
                or evidence.get('target_revision') != member['revision']
                or not isinstance(evidence.get('source_binding'), str)
                or evidence['source_binding'] != source_binding(db, pid, mid)
                or evidence.get('repaired_doc_hash') != semantic_facts.doc_hash(doc)):
            return None
        original, original_meta = row(evidence.get('original_artifact_id'), KIND_FACTS_V2)
        audit, audit_meta = row(evidence.get('audit_artifact_id'), KIND_FACT_AUDIT)
        started, started_meta = row(evidence.get('started_artifact_id'), KIND_FACT_REPAIR)
        if (not all(isinstance(x, dict) for x in (original, original_meta, audit, audit_meta, started, started_meta))
                or not (evidence['original_artifact_id'] < evidence['audit_artifact_id']
                        < evidence['started_artifact_id'] < meta['repair_artifact_id'])
                or any(not is_current(x) or x.get('invalidated') or x.get('fingerprint') != fp
                       or x.get('policy_fingerprint') != policy for x in (original_meta, audit_meta, started_meta))
                or original_meta.get('repaired') or semantic_facts.doc_hash(original) != evidence['original_doc_hash']
                or audit_meta.get('doc_hash') != evidence['original_doc_hash']
                or started_meta.get('doc_hash') != evidence['original_doc_hash']
                or started.get('status') != 'started' or started.get('rejected') != completed.get('rejected')
                or audit.get('status') != 'NEEDS_REVIEW' or audit.get('evaluated') is not True):
            return None
        audit_proof = audit_meta.get('jev_provenance')
        if (not isinstance(audit_proof, dict) or type(audit_proof.get('version')) is not int
                or audit_proof['version'] != 1 or audit_proof.get('stage') != 'fact_audit'
                or audit_proof.get('outcome') != 'done' or audit_proof.get('doc_hash') != evidence['original_doc_hash']
                or audit_proof.get('result_hash') != payload_hash(audit)
                or audit_proof.get('document_hash') != payload_hash(original)):
            return None
        rejected, coverage_missing, candidate = semantic_audit.fact_repair_plan(audit['findings'], 0)
        if (not candidate or rejected != completed.get('rejected')
                or coverage_missing is not evidence.get('coverage_missing')):
            return None
        base, base_contracts = {}, []
        rebuilt, error = semantic_extraction.rebuild_facts_v2_cached(
            member, db, fp, pid, original, original_meta, producer_out=base, contracts_out=base_contracts)
        if (error is not None or rebuilt is None or payload_hash(rebuilt) != payload_hash(original)
                or not base.get('complete')):
            return None
        answers = evidence.get('answers')
        chunks = original.get('chunks')
        if (not isinstance(answers, list) or not answers or not isinstance(chunks, list)
                or len(answers) > len(chunks)):
            return None
        index = 0
        repair_contracts = []
        repair_scope = None

        def reply(prompt):
            nonlocal index, repair_scope
            if index >= len(answers):
                raise semantic_runtime.RuntimeStale('repair_evidence_missing')
            answer = answers[index]
            index += 1
            if (not isinstance(answer, dict) or set(answer) != {'text', 'prompt_sha256', 'reservation_id', 'contract', 'unit_key', 'owner'}
                    or not isinstance(answer['text'], str)
                    or len(answer['text'].encode('utf-8')) > bounded_http.MAX_RESPONSE_BYTES
                    or answer['prompt_sha256'] != hashlib.sha256(prompt.encode('utf-8')).hexdigest()
                    or not isinstance(answer['reservation_id'], str) or not isinstance(answer['unit_key'], str)
                    or not isinstance(answer['owner'], dict)):
                raise semantic_runtime.RuntimeStale('repair_answer_invalid')
            rows = db.execute("SELECT artifact_id FROM artifacts WHERE kind='cloud_usage_reserve' "
                "AND project_id=? AND message_id=? AND CASE WHEN json_valid(meta) "
                "THEN json_extract(meta,'$.reservation_id')=? ELSE 0 END LIMIT 2",
                (pid, mid, answer['reservation_id'])).fetchall()
            if len(rows) != 1:
                raise semantic_runtime.RuntimeStale('repair_reservation_invalid')
            if not evidence['started_artifact_id'] < rows[0]['artifact_id'] < meta['repair_artifact_id']:
                raise semantic_runtime.RuntimeStale('repair_reservation_order')
            owner = answer['owner']
            scope = (answer['unit_key'], owner)
            if repair_scope is None:
                repair_scope = scope
            elif scope != repair_scope:
                raise semantic_runtime.RuntimeStale('repair_scope_changed')
            root = db.execute('SELECT COALESCE(parent_id,message_id) FROM messages WHERE project_id=? '
                              'AND message_id=?', (pid, mid)).fetchone()
            if (root is None or owner.get('semantic_target_id') != mid
                    or owner.get('semantic_thread_root') != root[0]
                    or owner.get('semantic_phase') != 'facts'
                    or owner.get('semantic_source') != payload_hash([(mid, evidence['source_binding'])])
                    or not isinstance(owner.get('semantic_generation'), str) or not owner['semantic_generation']
                    or not answer['unit_key'].startswith('semantic:')):
                raise semantic_runtime.RuntimeStale('repair_receipt_invalid')
            try:
                cloud_inference._cloud_repair_answer_evidence(
                    db, reservation_artifact_id=rows[0]['artifact_id'], reservation_id=answer['reservation_id'],
                    route='semantic_backlog', project_id=pid, message_id=mid, unit_key=answer['unit_key'],
                    owner=owner, prompt=prompt, answer_text=answer['text'], contract=answer['contract'])
            except ValueError:
                raise semantic_runtime.RuntimeStale('repair_receipt_invalid') from None
            repair_contracts.append({'provider': answer['contract']['backend'], 'contract': answer['contract'],
                                     'reservation_id': answer['reservation_id']})
            return answer['text']

        replay = semantic_extraction.repair_facts_v2(reply, member, original, rejected,
                                                    coverage_missing=coverage_missing)
        if (index != len(answers) or not replay['repaired']
                or replay['owner_chunk_ids'] != completed.get('owner_chunk_ids')
                or replay['repaired_fact_ids'] != completed.get('repaired_fact_ids')
                or payload_hash(replay['doc']) != payload_hash(doc)):
            return None
        return {'providers': sorted(set(base['providers']) | {p['provider'] for p in repair_contracts}),
                'base': base_contracts, 'repair': repair_contracts}
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError, OverflowError, UnicodeError,
            semantic_runtime.RuntimeGuardError):
        return None


def _projection_provenance(db, pid, mid, fp, policy, member, doc, content, *, sequence_out=None):
    from inference_dependency_loss import source_binding
    from semantic_projection import project_v2_doc_legacy

    providers, complete, jev = [], False, "unknown"
    repair_producers = None
    preflight = None
    if sequence_out is not None:
        sequence_out['sequence'] = None
    doc_hash = semantic_facts.doc_hash(doc)
    if payload_hash(project_v2_doc_legacy(doc)) != payload_hash(content):
        return {"version": 1, "stage": "projection", "providers": providers, "complete": complete,
                "result_hash": payload_hash(content), "source_fingerprint": fp, "policy_fingerprint": policy,
                "target_revision": member["revision"], "source_binding": source_binding(db, pid, mid), "jev": jev}
    for kind in (KIND_FACTS_V2, KIND_FACT_AUDIT):
        row = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? "
                         "AND message_id=? ORDER BY artifact_id DESC LIMIT 1", (kind, pid, mid)).fetchone()
        if row is None:
            continue
        try:
            data, meta = strict_json(row["content"]), strict_json(row["meta"])
            if (not isinstance(data, dict) or not isinstance(meta, dict) or not is_current(meta)
                    or meta.get("invalidated") or meta.get("fingerprint") != fp
                    or meta.get("policy_fingerprint") != policy):
                continue
            if kind == KIND_FACTS_V2:
                if meta.get('repaired') is True and payload_hash(data) == payload_hash(doc):
                    repair_producers = _repaired_projection_producer(db, pid, mid, fp, policy, member, data, meta)
                    if repair_producers is not None:
                        providers, complete = repair_producers['providers'], True
                    continue
                proof = meta.get("provider_provenance")
                if (payload_hash(data) == payload_hash(doc) and isinstance(proof, dict)
                        and type(proof.get("version")) is int and proof["version"] == 1
                        and proof.get("stage") == "facts" and proof.get("result_hash") == payload_hash(data)
                        and proof.get("source_fingerprint") == fp and proof.get("policy_fingerprint") == policy
                        and proof.get("target_revision") == member["revision"]
                        and type(proof.get("complete")) is bool and isinstance(proof.get("providers"), list)
                        and all(isinstance(p, str) and p in ("local", "anthropic", "openai")
                                for p in proof["providers"])
                        and len(set(proof["providers"])) == len(proof["providers"])
                        and (not proof["complete"] or bool(proof["providers"]))):
                    from semantic_extraction import rebuild_facts_v2_cached
                    producer = {}
                    chunk_sequence = {}
                    rebuilt, error = rebuild_facts_v2_cached(member, db, fp, pid, data, meta,
                                                             producer_out=producer, sequence_out=chunk_sequence)
                    if (error is None and rebuilt is not None and payload_hash(rebuilt) == payload_hash(data)
                            and producer.get("providers") == proof["providers"]
                            and producer.get("complete") is proof["complete"]):
                        providers, complete = proof["providers"], proof["complete"]
                        preflight = chunk_sequence.get('preflight')
            else:
                proof = meta.get("jev_provenance")
                if (meta.get("doc_hash") == doc_hash and isinstance(proof, dict)
                        and type(proof.get("version")) is int and proof["version"] == 1
                        and proof.get("stage") == "fact_audit" and proof.get("doc_hash") == doc_hash
                        and proof.get("document_hash") == payload_hash(doc)
                        and proof.get("result_hash") == payload_hash(data)
                        and proof.get("outcome") in ("done", "partial", "skipped", "unknown")
                        and proof.get("outcome") in ("done", "partial")):
                    from semantic_audit import FACT_SUPPORT_OPTIONS, _probability
                    verdicts = data.get("fact_verdicts")
                    facts = doc.get("facts")
                    if not isinstance(verdicts, dict) or not verdicts or not isinstance(facts, list):
                        continue
                    fact_ids = {fact["fact_id"] for fact in facts
                                if isinstance(fact, dict) and fact.get("validation_status") == "verified"}
                    if (not set(verdicts).issubset(fact_ids)
                            or any(not isinstance(answer, dict) or answer.get("choice") not in FACT_SUPPORT_OPTIONS
                                   or not _probability(answer.get("confidence")) for answer in verdicts.values())):
                        continue
                    if (proof["outcome"] == "done" and data.get("evaluated") is True
                            and data.get("status") in ("PASS", "NEEDS_REVIEW") and set(verdicts) == fact_ids):
                        jev = "done"
                    elif proof["outcome"] == "partial" and data.get("evaluated") is False:
                        jev = "partial"
        except (KeyError, TypeError, ValueError, RecursionError, OverflowError):
            continue
    result = {"version": 1, "stage": "projection", "providers": providers, "complete": complete,
              "result_hash": payload_hash(content), "source_fingerprint": fp, "policy_fingerprint": policy,
              "target_revision": member["revision"], "source_binding": source_binding(db, pid, mid), "jev": jev}
    if repair_producers is not None:
        result['producer_stages'] = {'base': repair_producers['base'], 'repair': repair_producers['repair']}
    if sequence_out is not None and complete and len(providers) == 1:
        if preflight == 'complete':
            sequence_out['sequence'] = ['jev', providers[0]]
    return result


def record_stage(ledger, pid: int, mid: int, fp: str, policy: str,
                 stage: str, status: str, tx: bool = False,
                 **detail) -> None:
    """One durable stage receipt; storage errors propagate to the caller.
    ``tx=True`` inside a caller-held ``with ledger.db`` block (the
    non-tx variant commits immediately and would break the atomic
    publication boundary).

    A receipt identical (apart from ``ts``) to the latest one for the
    same generation and stage adds no information and is skipped — a
    job waiting on a durable resource outage re-walks S0–S2 every drain
    without consuming attempts, and must not append rows forever."""
    receipt = {"stage": stage, "status": status, **detail}
    # json_extract on a malformed meta row raises; the guarded document
    # is NULL for such rows, so malformed historical receipts are skipped.
    meta_doc = json_object_or_null("meta")
    prev = ledger.db.execute(
        "SELECT content FROM artifacts WHERE kind=? AND message_id=? "
        f"AND {current_meta_pred('meta')} "
        f"AND json_extract({meta_doc},'$.stage')=? "
        f"AND json_extract({meta_doc},'$.fingerprint')=? "
        f"AND json_extract({meta_doc},'$.policy_fingerprint')=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (KIND_V4_STAGE, mid, stage, fp, policy)).fetchone()
    if prev is not None:
        last = loads_dict(prev["content"])
        if isinstance(last, dict):
            last.pop("ts", None)
            if last == receipt:
                return
    add = ledger.artifact_add_tx if tx else ledger.artifact_add
    add(
        KIND_V4_STAGE,
        json.dumps({"stage": stage, "status": status,
                    "ts": time.time(), **detail},
                   ensure_ascii=False, allow_nan=False),
        project_id=pid, message_id=mid,
        meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
              "inference_requests": semantic_transport.inference_requests(),
              "stage": stage, "engine_version": ENGINE_VERSION})


def stage_ledger(ledger, mid: int, fp: str) -> list:
    """Ordered S0–S8 receipts for one generation — the replayable
    evidence that repair was reserved before dispatch, audits were
    evaluated, and publication only followed an all-PASS chain."""
    out = []
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? "
            "AND message_id=? ORDER BY artifact_id",
            (KIND_V4_STAGE, mid)):
        meta, content = loads_dict(r["meta"]), loads_dict(r["content"])
        if isinstance(meta, dict) and is_current(meta) and isinstance(content, dict) \
                and meta.get("fingerprint") == fp:
            out.append({"stage": meta.get("stage"), **content})
    return out


def current_v4(ledger, mid: int, content_hash: str):
    """Newest valid published v4 row bound to the CURRENT source hash
    — mirrors ``current_projection_pred`` (error-free, hash-current,
    not invalidated) for the v4 kind."""
    row = ledger.db.execute(
        "SELECT a.artifact_id,a.content,a.meta FROM artifacts a "
        "JOIN messages m ON m.message_id=a.message_id "
        "AND m.project_id=a.project_id WHERE a.kind=? "
        "AND a.message_id=? AND m.content_hash=? AND "
        + current_v4_pred("a", "m.content_hash")
        + " ORDER BY a.artifact_id DESC LIMIT 1",
        (KIND_V4, mid, content_hash)).fetchone()
    if row is None:
        return None
    content, meta = loads_dict(row["content"]), loads_dict(row["meta"])
    return ({"artifact_id": row["artifact_id"], "content": content, "meta": meta}
            if content is not None and meta is not None else None)


def stage_request_following(ledger, bundle, doc, *, policy):
    """Persist a current-source candidate; never publish, change Loop IDs or mint grants."""
    import semantic_facts as sf
    from semantic_projection import project_v2_doc_legacy, project_v2_facts

    clean = sf.validate_facts_doc(doc)
    current = semantic_store.thread_bundle(ledger.db, bundle["project_id"], bundle["root_id"])
    if current is None or current["source_fingerprint"] != bundle["source_fingerprint"]:
        raise sf.ContractError("request_following_source_stale")
    source = clean["source"]
    member = next((m for m in current["members"]
                   if str(m["message_id"]) == source["message_id"]), None)
    if member is None or member["body_state"] == "deleted" \
            or source["revision"] != member["revision"] \
            or source["source_fingerprint"] != current["source_fingerprint"] \
            or source["content_quality"] != (
                "full" if member["body_state"] == "full" else "partial") \
            or source["body_codepoints"] != len(member["body_original"]) \
            or source["content_hash"] != hashlib.sha256(
                member["body_original"].encode("utf-8")).hexdigest():
        raise sf.ContractError("request_following_source_mismatch")
    if any(member["body_original"][e["start"]:e["end"]] != e["quote"]
           for e in clean["evidence"]):
        raise sf.ContractError("request_following_evidence_mismatch")
    if not isinstance(policy, str) or not policy.strip():
        raise sf.ContractError("request_following_policy_required")
    candidate = {
        "source": clean["source"], "source_doc_hash": semantic_facts.doc_hash(doc),
        "doc_hash": semantic_facts.doc_hash(clean),
        "projection": project_v2_doc_legacy(clean, request_details=True),
        "loop_facts": project_v2_facts(clean, request_details=True),
        "evidence": clean["evidence"],
        "promotion_ready": False,
        "required_promotion_gates": [
            "request_detail_audit", "loop_identity_owner_decision",
            "extraction_generation_owner_decision", "human_200_g6", "calibration"],
    }
    record_stage(ledger, current["project_id"], member["message_id"], current["source_fingerprint"],
                 policy, "request_following_candidate", "PENDING", candidate=candidate)
    return candidate


def extract_request_following(llm_fn, ledger, bundle, message_id, *, policy,
                              deadline=None, chunk_size=3000, retry_coverage=False):
    """Extract into a separate candidate generation and source-bound PENDING staging."""
    import semantic_facts as sf
    from semantic_extraction import extract_facts_v2

    if not isinstance(policy, str) or not policy.strip():
        raise sf.ContractError("request_following_policy_required")
    current = semantic_store.thread_bundle(ledger.db, bundle["project_id"], bundle["root_id"])
    if current is None or current["source_fingerprint"] != bundle["source_fingerprint"]:
        raise sf.ContractError("request_following_source_stale")
    member = next((m for m in current["members"] if m["message_id"] == message_id), None)
    if member is None or member["body_state"] == "deleted":
        raise sf.ContractError("request_following_source_mismatch")
    result = extract_facts_v2(
        llm_fn, member, deadline, ledger=ledger, project_id=current["project_id"],
        source_fingerprint=current["source_fingerprint"], chunk_size=chunk_size,
        retry_coverage=retry_coverage, request_following=True)
    candidate = (stage_request_following(ledger, current, result["doc"], policy=policy)
                 if result["extraction_complete"] else None)
    record_stage(
        ledger, current["project_id"], member["message_id"], current["source_fingerprint"],
        policy, "request_following_extraction", "PENDING",
        generation=result["generation"], extraction_complete=result["extraction_complete"],
        doc_hash=semantic_facts.doc_hash(result["doc"]))
    return {"extraction": result, "candidate": candidate}


def publish(ledger, pid: int, mid: int, fp: str, policy: str,
            member: dict, v2_doc: dict) -> int | None:
    """Atomically mint the PASS-only v4 row (S8). MUST be called inside
    the caller's ``with ledger.db`` block so the read model switch and
    the status receipt commit together. Idempotent on doc_hash — a
    replayed PASS for the same audited document reuses the row — unless
    it was minted by another projection version, which a new row
    supersedes."""
    from semantic_projection import (PROJECTION_VERSION,
                                      project_v2_doc_legacy,
                                      projection_current)
    from semantic_audit import published_quantity_findings
    if published_quantity_findings(v2_doc):
        return None
    doc_hash = semantic_facts.doc_hash(v2_doc)
    existing = current_v4(ledger, mid, member["revision"])
    if existing is not None and projection_current(existing["meta"], doc_hash):
        return existing["artifact_id"]
    content = project_v2_doc_legacy(v2_doc)
    from inference_dependency_loss import source_binding, capture_tx, record_tx
    ledger.db.execute('UPDATE artifacts SET artifact_id=artifact_id WHERE 0')
    footprint = capture_tx(ledger.db, pid, mid, artifact_ids=[existing['artifact_id']] if existing else [])
    artifact_id = ledger.artifact_add_tx(
        KIND_V4, json.dumps(content, ensure_ascii=False,
                            allow_nan=False),
        project_id=pid, message_id=mid, model=semantic_transport.llm_model(),
        meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
              "inference_requests": semantic_transport.inference_requests(),
              "source_binding": source_binding(ledger.db, pid, mid),
              "schema": 2, "hash": member["revision"],
              "extract_version": ENGINE_VERSION,
              "engine_version": ENGINE_VERSION,
              "fact_source": "canonical",
              "doc_hash": doc_hash,
              "provider_provenance": _projection_provenance(ledger.db, pid, mid, fp, policy, member, v2_doc, content),
              "projection_version": PROJECTION_VERSION})
    record_tx(ledger.db, footprint, replacement_id=artifact_id)
    ledger.bump_inference_analysis_epoch_tx(pid)
    return artifact_id


# Rows re-projected per tick — the pass is a pure function of stored
# documents (no model call), but it still writes one artifact per row
# and must never crowd the shared run lock's deadline.
REPROJECT_LIMIT = 25


def reproject_doc(db, mid: int, meta: dict):
    """The audited v2 document a current projection/v4 row was rendered
    from, or ``(None, reason)``. Selected exactly the way the drain
    selects it (the newest ``semantic_facts_v2`` row of the generation's
    fingerprint) and accepted only when it is still the document the row
    binds (``doc_hash``), its coverage is complete, and the stored fact
    audit of that generation PASSed it (``fact_audit_verdict``)."""
    from semantic_audit import fact_audit_verdict
    from semantic_store import current_artifact
    fp, policy = meta.get("fingerprint"), meta.get("policy_fingerprint")
    doc_hash = meta.get("doc_hash")
    if not (isinstance(fp, str) and isinstance(policy, str)
            and isinstance(doc_hash, str)):
        return None, "unbound_meta"
    prev_v2 = current_artifact(db, KIND_FACTS_V2, mid, fp)
    doc = prev_v2["content"] if prev_v2 else None
    try:
        ok = doc is not None and semantic_facts.doc_hash(doc) == doc_hash
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        return None, "doc_unavailable"
    if not isinstance(doc.get("coverage"), dict) \
            or doc["coverage"].get("status") != "complete":
        return None, "coverage_incomplete"
    from semantic_audit import published_quantity_findings
    if published_quantity_findings(doc):
        return None, "quantity_unverified"
    if fact_audit_verdict(current_artifact(db, KIND_FACT_AUDIT, mid, fp, policy),
                          doc_hash) != "PASS":
        return None, "audit_not_pass"
    return doc, None


def reproject_stale(ledger, scfg: dict,
                    limit: int = REPROJECT_LIMIT) -> dict:
    """Bounded deterministic re-projection (no model call): supersede
    CURRENT ``canonical_projection`` / ``semantic_facts_v4`` rows whose
    ``meta.projection_version`` is missing or older than
    ``PROJECTION_VERSION`` with a row rendered by the current
    ``project_v2_doc_legacy`` from the same stored audited document.

    Only a row the read side would select right now is touched (the
    shared ``current_projection_id``/``current_v4_id`` predicates:
    hash-current, error-free, not invalidated, newest) on a message whose
    body is not deleted, so invalidated, superseded and non-PASS
    generations (which never have a v4 row) are never resurrected. The
    new row copies the old binding meta verbatim — fingerprint, policy,
    hash, doc_hash, engine/extract version — and only advances
    ``projection_version`` (plus a ``reprojected_from`` lineage id);
    readers pick it as the newest ``artifact_id`` and the drain's own
    reuse checks accept it. A row whose document cannot be re-derived
    safely is marked ``reproject_skipped`` (so it cannot starve the
    bound); an old-version row stays outside the current read model. Run it
    after ``invalidate_projections`` in the same tick."""
    from semantic_projection import PROJECTION_VERSION, project_v2_doc_legacy
    out = {"reprojected": 0, "skipped": 0, "skip_reasons": {}}
    # run_due first invalidates rows while fact_source is noncanonical,
    # hiding them from readers and this pass. On canonical return it
    # revives only current, PASS rows expired for that switch; this pass
    # then re-derives them from their own fingerprint/policy/doc_hash.
    if scfg.get("mode") == "off" or limit < 1:
        return out
    rows = ledger.db.execute(
        "SELECT a.artifact_id,a.kind,a.project_id,a.message_id,a.model,"
        "a.meta FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id AND m.project_id=a.project_id "
        "WHERE a.kind IN (?,?) AND m.body_state IS NOT 'deleted' "
        "AND a.artifact_id=CASE WHEN a.kind=? "
        f"THEN {current_projection_id('m', require_version=False)} ELSE {current_v4_id('m', require_version=False)} END "
        # CASE, not AND: the subquery term above is evaluated last, so a
        # bare json_extract here would raise on a malformed meta row.
        "AND CASE WHEN json_valid(a.meta) AND json_type(a.meta)='object' "
        "THEN COALESCE(json_extract(a.meta,'$.projection_version'),0)<? "
        "AND COALESCE(json_extract(a.meta,'$.reproject_skipped'),0)<? END "
        "ORDER BY a.artifact_id LIMIT ?",
        (KIND_FACT_PROJ, KIND_V4, KIND_FACT_PROJ, PROJECTION_VERSION,
         PROJECTION_VERSION, limit)).fetchall()
    reinterpreted = {}
    for row in rows:
        meta = loads_dict(row["meta"])
        content, doc = None, None
        try:
            doc, reason = (reproject_doc(ledger.db, row["message_id"], meta)
                           if meta is not None
                           else (None, "metadata_unreadable"))
            if doc is not None:
                content = json.dumps(project_v2_doc_legacy(doc),
                                     ensure_ascii=False, allow_nan=False)
        except Exception:
            # a stored v2 doc of the wrong shape (KeyError/unhashable)
            # must be marked skipped, or it stays first in the ORDER BY
            # and blocks every later row forever
            reason = "projection_failed"
        with ledger.db:
            if content is None:
                ledger.db.execute(
                    "UPDATE artifacts SET meta=json_set(meta,"
                    "'$.reproject_skipped',?) WHERE artifact_id=?",
                    (PROJECTION_VERSION, row["artifact_id"]))
                out["skipped"] += 1
                out["skip_reasons"][reason] = \
                    out["skip_reasons"].get(reason, 0) + 1
                continue
            new_meta = dict(meta)
            new_meta.pop("reproject_skipped", None)
            new_meta["projection_version"] = PROJECTION_VERSION
            new_meta["reprojected_from"] = row["artifact_id"]
            import inference_dependency_loss as dependency_loss
            ledger.db.execute('UPDATE artifacts SET artifact_id=artifact_id WHERE 0')
            footprint = dependency_loss.capture_tx(ledger.db, row['project_id'], row['message_id'], artifact_ids=[row['artifact_id']])
            new_id = ledger.artifact_add_tx(
                row["kind"], content,
                project_id=row["project_id"],
                message_id=row["message_id"], model=row["model"] or "",
                meta=new_meta)
            dependency_loss.record_tx(ledger.db, footprint, replacement_id=new_id)
            ledger.bump_inference_analysis_epoch_tx(row['project_id'])
        out["reprojected"] += 1
        if doc is not None:
            reinterpreted.setdefault(
                (row["project_id"], row["message_id"]), (doc, meta))
    local_model = semantic_transport.llm_model() if reinterpreted else None
    for (pid, mid), (doc, meta) in reinterpreted.items():
        _reinterpret_loops(ledger, scfg, pid, mid, doc, meta, out, local_model)
    used = _rebuild_hint_docs(ledger, scfg, limit - len(rows), out)
    _hold_upgraded_repairs(ledger, scfg, limit - len(rows) - used, out)
    return out


# A fact entry's provenance text; a non-object entry or non-text value
# reads as '' (same operand guard as mcs_queries.JSON_OBJECT_SQL).
_FACT_OBJECT = "CASE WHEN f.type='object' THEN f.value ELSE '{}' END"
_PROVENANCE = (f"CASE WHEN json_type({_FACT_OBJECT},'$.provenance')='text' "
               f"THEN json_extract({_FACT_OBJECT},'$.provenance') ELSE '' END")

_HELD_KINDS = (KIND_SUMMARY, KIND_AUDIT, KIND_COVERAGE, KIND_FACT_AUDIT,
               KIND_LOOP)


def _count(out: dict, key: str) -> None:
    out["skip_reasons"][key] = out["skip_reasons"].get(key, 0) + 1


def _member_bundle(ledger, pid: int, mid: int, fp, local_model=None):
    """(bundle, member) of the message's live thread, or (None, None)
    when the stored generation ``fp`` is no longer the live one."""
    from semantic_store import thread_bundle
    row = ledger.db.execute(
        "SELECT COALESCE(parent_id,message_id) r FROM messages "
        "WHERE project_id=? AND message_id=?", (pid, mid)).fetchone()
    bundle = thread_bundle(ledger.db, pid, row["r"], local_model=local_model) if row else None
    if bundle is None or bundle["source_fingerprint"] != fp:
        return None, None
    member = next((m for m in bundle["members"] if m["message_id"] == mid), None)
    return (bundle, member) if member is not None else (None, None)


def _reinterpret_loops(ledger, scfg, pid, mid, doc, meta, out, local_model=None) -> None:
    """Loop candidates follow the current projection of the same audited
    document: items it no longer yields stop being current (no model)."""
    from semantic_projection import project_v2_facts
    if scfg.get("loop_mode", "off") == "off" \
            or meta.get("policy_fingerprint") != policy_fingerprint(scfg):
        return
    bundle, _ = _member_bundle(ledger, pid, mid, meta.get("fingerprint"), local_model)
    if bundle is None:
        return
    from semantic_loops import update_loops
    try:
        update_loops(ledger, pid, bundle, {mid: project_v2_facts(doc)}, None,
                     scfg, time.monotonic() + 30)
    except Exception:
        _count(out, "loop_reinterpret_failed")


def _hold_derivatives(ledger, pid: int, mid: int, fp: str,
                      reason: str) -> None:
    """Stop everything derived from a stored canonical document that is no
    longer trusted from being current: every projection of the message's
    generation, and the generation's summary/audit/coverage/fact audit and Loop
    candidates. Rows stay as history; a summary is also marked stale so
    the send gate holds any queued notice. Caller holds the transaction."""
    # Every publication of this message's generation, not only the one bound
    # to the held document: readers pick the newest non-invalidated row, so
    # an older document's row of the same generation would otherwise become
    # current again. Other generations, messages and patients stay as they are.
    ledger.db.execute('UPDATE artifacts SET artifact_id=artifact_id WHERE 0')
    adopted = ledger.db.execute(
        "SELECT 1 FROM artifacts WHERE project_id=? AND message_id=? "
        "AND kind IN (?,?) AND " + current_meta_pred("meta") +
        " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.fingerprint')=? "
        "AND json_extract(meta,'$.invalidated') IS NOT 1 END LIMIT 1",
        (pid, mid, KIND_FACT_PROJ, KIND_V4, fp)).fetchone()
    import inference_dependency_loss as dependency_loss
    old_ids = [row[0] for row in ledger.db.execute(
        "SELECT artifact_id FROM artifacts WHERE project_id=? AND message_id=? AND kind IN (?,?) "
        "AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.fingerprint')=? "
        "AND json_extract(meta,'$.invalidated') IS NOT 1 END", (pid, mid, KIND_FACT_PROJ, KIND_V4, fp))]
    footprint = dependency_loss.capture_tx(ledger.db, pid, mid, artifact_ids=old_ids)
    ledger.db.execute(
        "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'),"
        "'$.invalidated_reason',?) WHERE project_id=? AND message_id=? "
        "AND kind IN (?,?) AND CASE WHEN json_valid(meta) "
        "AND json_type(meta)='object' THEN json_extract(meta,'$.fingerprint')=? "
        "AND json_extract(meta,'$.invalidated') IS NOT 1 END",
        (reason, pid, mid, KIND_FACT_PROJ, KIND_V4, fp))
    if adopted:
        dependency_loss.record_tx(ledger.db, footprint)
        ledger.bump_inference_analysis_epoch_tx(pid)
    held = ("WHERE project_id=? AND message_id=? AND kind IN ({}) "
            "AND CASE WHEN json_valid(meta) AND json_type(meta)='object' "
            "THEN json_extract(meta,'$.fingerprint')=? "
            "AND json_extract(meta,'$.invalidated') IS NOT 1 END")
    # the summary first: its stale mark is what the send gate reads
    ledger.db.execute(
        "UPDATE artifacts SET meta=json_set(meta,'$.stale',json('true')) "
        + held.format("?"), (pid, mid, "semantic_summary", fp))
    ledger.db.execute(
        "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'),"
        "'$.invalidated_reason',?) " + held.format(",".join("?" * len(_HELD_KINDS))),
        (reason, pid, mid, *_HELD_KINDS, fp))


def _seed_rebuild(ledger, scfg, pid: int, mid: int, meta: dict):
    """Queue one notification-free re-run of the message's thread unless
    the generation is parked/failed (no new retry budget) or queued.
    Runs inside the caller's transaction."""
    if meta.get("needs_review"):
        return "needs_review"
    if meta.get("repaired"):
        # re-running would re-audit the unrepaired base under a spent
        # repair: the held thread waits for an explicit manual retry
        return "repaired"
    scope = scfg.get("project_ids")
    if scope is not None and pid not in scope:
        return "out_of_scope"
    row = ledger.db.execute(
        "SELECT COALESCE(parent_id,message_id) r FROM messages "
        "WHERE project_id=? AND message_id=?", (pid, mid)).fetchone()
    state = ledger.job_state(JOB_KIND, pid, row["r"]) if row else None
    if state in ("failed", "pending"):
        return f"job_{state}"
    ledger.semantic_seed_tx(pid, [mid], {"source": "hint_rebuild",
                                          "notification_free": True})
    return None


def _rebuild_hint_docs(ledger, scfg: dict, limit: int, out: dict) -> int:
    """Bounded pass over current semantic_facts_v2 documents whose hint
    merge predates the evidence-span rule (``FACTS_V2_BUILD``).

    Each is re-derived from its cached chunks with no model/Jev call and
    marked once (``hint_rebuild``) so it never re-enters the pass:
    ``same`` keeps every derived row as is; ``replaced`` stores the new
    document and holds the old one's derivatives; ``skipped:<reason>``
    (unrebuildable) holds them too. A rebuilt or unrebuildable thread is
    re-queued at most once, never when failed/parked/repaired. Only for
    ``fact_source=canonical``, where the document feeds every consumer."""
    from semantic_extraction import FACTS_V2_BUILD, rebuild_facts_v2_cached
    from semantic_facts import ContractError, validate_facts_doc
    stats = {"same": 0, "replaced": 0, "skipped": 0, "history": 0, "seeded": 0}
    # shadow/legacy summaries and Loops come from legacy facts, not from
    # this document: holding them by fingerprint would touch unrelated rows
    if limit < 1 or scfg.get("fact_source") != "canonical":
        return 0
    rows = ledger.db.execute(
        "SELECT a.artifact_id,a.project_id,a.message_id,a.model,a.content,"
        "a.meta FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id AND m.project_id=a.project_id "
        "WHERE a.kind=? AND m.body_state IS NOT 'deleted' "
        "AND a.artifact_id=(SELECT MAX(b.artifact_id) FROM artifacts b "
        "WHERE b.kind=a.kind AND b.message_id=a.message_id) "
        # nested CASE: every JSON operand is proven parseable before it is
        # read, whatever order SQLite evaluates AND terms in
        "AND CASE WHEN json_valid(a.meta) AND json_valid(a.content) THEN "
        "CASE WHEN json_type(a.meta)='object' "
        "AND json_type(a.content,'$.facts')='array' "
        "THEN json_extract(a.meta,'$.build') IS NULL "
        "AND json_extract(a.meta,'$.hint_rebuild') IS NULL "
        f"AND EXISTS(SELECT 1 FROM json_each(a.content,'$.facts') f "
        f"WHERE instr({_PROVENANCE},'extract_v1')>0 "
        f"AND instr({_PROVENANCE},'+')>0) END END "
        "ORDER BY a.artifact_id LIMIT ?", (KIND_FACTS_V2, limit)).fetchall()

    def mark(row, value):
        ledger.db.execute(
            "UPDATE artifacts SET meta=json_set(meta,'$.hint_rebuild',?) "
            "WHERE artifact_id=?", (value, row["artifact_id"]))

    local_model = semantic_transport.llm_model() if rows else None
    for row in rows:
        pid, mid = row["project_id"], row["message_id"]
        meta, doc = loads_dict(row["meta"]) or {}, loads_dict(row["content"])
        fp = meta.get("fingerprint")
        new, reason = None, None
        try:
            _, member = _member_bundle(ledger, pid, mid, fp, local_model)
            if member is None:
                reason = "history"   # not the live generation: never reused
            elif meta.get("repaired"):
                reason = "repaired"  # cannot be re-derived from cached chunks
            else:
                old = json.dumps(validate_facts_doc(doc), sort_keys=True)
                producer = {}
                new, reason = rebuild_facts_v2_cached(member, ledger.db, fp, pid,
                                                      doc, meta, producer_out=producer)
                if new is not None and old == json.dumps(
                        validate_facts_doc(new), sort_keys=True):
                    new, reason = None, "same"
        except (ContractError, KeyError, TypeError, ValueError):
            new, reason = None, "document_invalid"
        except Exception:
            # anything else still marks the row, or it would stay first in
            # the ORDER BY and block every later document every tick
            new, reason = None, "rebuild_failed"
        if reason in ("history", "same"):
            with ledger.db:
                if reason == "same":
                    ledger.db.execute(
                        "UPDATE artifacts SET meta=json_set(meta,'$.build',?) "
                        "WHERE artifact_id=?", (FACTS_V2_BUILD, row["artifact_id"]))
                mark(row, reason)
            stats[reason] += 1
            continue
        # The new document, the mark, the hold and the re-queue commit
        # together. Loop candidates are re-derived by the drain once the
        # rebuilt document passes its audit, never from an unaudited one.
        try:
            with ledger.db:
                if new is not None:
                    new_meta = {k: v for k, v in meta.items()
                                if k != "hint_rebuild"}
                    if isinstance(new_meta.get("provider_provenance"), dict):
                        new_meta["provider_provenance"] = {
                            **new_meta["provider_provenance"],
                            "providers": producer.get("providers", []),
                            "complete": producer.get("complete") is True,
                            "result_hash": payload_hash(new),
                            "source_fingerprint": fp,
                            "policy_fingerprint": meta.get("policy_fingerprint"),
                            "target_revision": member["revision"],
                        }
                    new_meta.update(
                        build=FACTS_V2_BUILD, rebuilt_from=row["artifact_id"],
                        coverage_status=new["coverage"]["status"],
                        facts=len(new["facts"]),
                        open_obligations=len(new["coverage"]["open_obligation_ids"]))
                    ledger.artifact_add_tx(
                        KIND_FACTS_V2, json.dumps(new, ensure_ascii=False,
                                                  allow_nan=False),
                        project_id=pid, message_id=mid,
                        model=row["model"] or "", meta=new_meta)
                    mark(row, "replaced")
                else:
                    mark(row, f"skipped:{reason}")
                _hold_derivatives(ledger, pid, mid, fp, "hint_rebuild")
                held = _seed_rebuild(ledger, scfg, pid, mid, meta)
        except Exception:
            # nothing above is half-applied; still hold the old document's
            # derivatives and mark it so it cannot block later rows
            new, reason, held = None, "write_failed", "write_failed"
            with ledger.db:
                mark(row, "skipped:write_failed")
                _hold_derivatives(ledger, pid, mid, fp, "hint_rebuild")
        if new is not None:
            stats["replaced"] += 1
        else:
            stats["skipped"] += 1
            _count(out, f"hint_rebuild:{reason}")
        if held is None:
            stats["seeded"] += 1
        else:
            _count(out, f"hint_rebuild_seed:{held}")
    if any(stats.values()):
        # reported only when the pass did something: an idle tick keeps
        # the reproject result unchanged
        out["hint_rebuild"] = stats
    return len(rows)


def _upgraded_obligations(ledger, row, meta, doc) -> tuple[str, list]:
    """Prove which obligations a pre-fix repair turned from an adjudicated
    absence into coverage: ``("held", ids)``, ``("clear", [])`` or
    ``("unproven:<why>", [])``. The proof is the generation's completed
    repair receipt and the stored pre-repair document it names, both
    earlier than the repaired document; both documents must satisfy the
    canonical contract and bind this message, revision and generation.
    Anything missing, invalid or ambiguous is never asserted — a broken
    candidate is never skipped in favour of another one."""
    from semantic_facts import ContractError, validate_facts_doc
    fp, mid, rid = meta.get("fingerprint"), row["message_id"], row["artifact_id"]

    def bound(candidate):
        """The contract-normalised document when it binds this generation."""
        clean = validate_facts_doc(candidate)
        source = clean["source"]
        if source["message_id"] != str(mid) or source["source_fingerprint"] != fp:
            raise ContractError("contract:repair_proof_unbound")
        return clean

    try:
        repaired = bound(doc)
    except (ContractError, KeyError, TypeError, ValueError):
        return ("unproven:repaired_document", [])
    hashes = set()
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
            "AND project_id=? AND artifact_id<?",
            (KIND_FACT_REPAIR, mid, row["project_id"], rid)):
        rmeta, content = loads_dict(r["meta"]), loads_dict(r["content"])
        if rmeta and rmeta.get("fingerprint") == fp and content \
                and content.get("repaired") is True:
            hashes.add(rmeta.get("doc_hash"))
    if len(hashes) != 1 or not isinstance(next(iter(hashes)), str):
        return ("unproven:receipt", [])
    base_hash = hashes.pop()
    bases = []
    for r in ledger.db.execute(
            "SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
            "AND project_id=? AND artifact_id<?",
            (KIND_FACTS_V2, mid, row["project_id"], rid)):
        bmeta, base = loads_dict(r["meta"]), loads_dict(r["content"])
        if not bmeta or bmeta.get("fingerprint") != fp or bmeta.get("repaired"):
            continue
        try:
            if semantic_facts.doc_hash(base) != base_hash:
                continue
        except (KeyError, TypeError):
            continue
        try:
            clean = bound(base)
        except (ContractError, KeyError, TypeError, ValueError):
            return ("unproven:base_document", [])
        if (clean["source"]["revision"], clean["source"]["content_hash"]) != \
                (repaired["source"]["revision"], repaired["source"]["content_hash"]):
            return ("unproven:base_document", [])
        bases.append({o["obligation_id"]: o["status"] for o in clean["obligations"]})
    if not bases or any(b != bases[0] for b in bases):
        return ("unproven:base_document", [])
    upgraded = [o["obligation_id"] for o in repaired["obligations"]
                if o["status"] == "covered"
                and bases[0].get(o["obligation_id"]) == "explicit_no_fact"]
    return ("held", upgraded) if upgraded else ("clear", [])


def _hold_upgraded_repairs(ledger, scfg: dict, limit: int, out: dict) -> None:
    """Bounded check of current repaired canonical documents stored before
    a repair stopped turning an adjudicated absence into coverage.

    Each is checked once (``repair_checked``). A proven upgrade holds the
    generation exactly like an unrebuildable hint document — derivatives
    stop being current and the document is never reused, with no model or
    Jev call, no re-queue and no new repair; it waits for a manual retry.
    A plain repaired document, or one whose proof is missing or
    contradictory, is left as it is."""
    if limit < 1 or scfg.get("fact_source") != "canonical":
        return
    rows = ledger.db.execute(
        "SELECT a.artifact_id,a.project_id,a.message_id,a.content,a.meta "
        "FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id AND m.project_id=a.project_id "
        "WHERE a.kind=? AND m.body_state IS NOT 'deleted' "
        "AND a.artifact_id=(SELECT MAX(b.artifact_id) FROM artifacts b "
        "WHERE b.kind=a.kind AND b.message_id=a.message_id) "
        "AND CASE WHEN json_valid(a.meta) THEN CASE WHEN json_type(a.meta)='object' "
        "THEN json_extract(a.meta,'$.repaired') IS 1 "
        "AND json_extract(a.meta,'$.repair_checked') IS NULL "
        "AND json_extract(a.meta,'$.hint_rebuild') IS NULL END END "
        "ORDER BY a.artifact_id LIMIT ?", (KIND_FACTS_V2, limit)).fetchall()
    for row in rows:
        meta, doc = loads_dict(row["meta"]) or {}, loads_dict(row["content"]) or {}
        try:
            verdict, _ = _upgraded_obligations(ledger, row, meta, doc)
        except Exception:
            verdict = "unproven:check_failed"
        with ledger.db:
            ledger.db.execute(
                "UPDATE artifacts SET meta=json_set(meta,'$.repair_checked',?) "
                "WHERE artifact_id=?", (verdict, row["artifact_id"]))
            if verdict == "held":
                # the drain refuses a skipped document: never reused again
                ledger.db.execute(
                    "UPDATE artifacts SET meta=json_set(meta,'$.hint_rebuild',"
                    "'skipped:repair_absence') WHERE artifact_id=?",
                    (row["artifact_id"],))
                _hold_derivatives(ledger, row["project_id"], row["message_id"],
                                  meta.get("fingerprint"), "repair_absence")
        _count(out, f"repair_absence:{verdict}")






def diagnostic(ledger, pid: int, mid: int, fp: str, policy: str,
               status: str, findings: list, doc_hash=None,
               tx: bool = False) -> None:
    """Record an outcome as a diagnostic receipt — the separate kind
    guarantees no extraction reader can ever select it as current content.
    Idempotent per (fp, status, doc_hash): a
    retried drain must not stack identical rows. ``tx=True`` inside a
    caller-held ``with ledger.db`` block."""
    from semantic_store import current_artifact
    prev = current_artifact(ledger.db, KIND_V4_DIAG, mid, fp, policy)
    if prev is not None \
            and prev["content"].get("status") == status \
            and prev["content"].get("doc_hash") == doc_hash \
            and prev["content"].get("findings") == findings:
        return
    add = ledger.artifact_add_tx if tx else ledger.artifact_add
    add(
        KIND_V4_DIAG,
        json.dumps({"status": status, "findings": findings,
                    "target_message_id": mid, "doc_hash": doc_hash},
                   ensure_ascii=False, allow_nan=False),
        project_id=pid, message_id=mid,
        meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
              "inference_requests": semantic_transport.inference_requests(),
              "engine_version": ENGINE_VERSION, "audit_status": status,
              "diagnostic": True})


def invalidate_projections(db, scfg: dict) -> int:
    """Persist source/policy expiry for readers of the published snapshot."""
    from semantic_projection import projection_transition
    policy = policy_fingerprint(scfg)
    off = scfg.get("mode") == "off"
    enabled = not off and scfg.get("fact_source") == "canonical"
    rows = db.execute("""
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
        current_ids = {r[0] for r in db.execute(f"""
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
                THEN {current_projection_id(require_version=False)} ELSE {current_v4_id(require_version=False)} END
        """)}
    bundles, expired, revived, adopted_projects = {}, [], [], set()
    local_model = None
    for row, meta in zip(rows, metas):
        key = (row["project_id"], row["parent_id"] or row["message_id"])
        source_fingerprint = None
        revivable = False
        # These predicates bound source/document I/O only; the pure projection
        # contract below owns the actual keep/expire/revive decision.
        if enabled and meta is not None and meta.get("policy_fingerprint") == policy:
            if key not in bundles:
                if local_model is None:
                    # one config read per scan, not one per thread
                    local_model = semantic_transport.llm_model()
                bundle = semantic_store.thread_bundle(db, *key, local_model=local_model)
                bundles[key] = bundle["source_fingerprint"] if bundle else None
            source_fingerprint = bundles[key]
            if (source_fingerprint is not None
                    and meta.get("fingerprint") == source_fingerprint
                    and meta.get("invalidated")
                    and row["artifact_id"] in current_ids):
                doc, _ = reproject_doc(db, row["message_id"], meta)
                revivable = doc is not None
        action, reason = projection_transition(meta, off=off, enabled=enabled,
            policy=policy, source_fingerprint=source_fingerprint, revivable=revivable)
        if action == "keep":
            continue
        if action == "revive":
            revived.append((row["artifact_id"],))
        else:
            expired.append((reason, row["artifact_id"]))
        if is_current(meta):
            adopted_projects.add(row['project_id'])
    if expired or revived:
        with db:
            if not db.in_transaction:
                db.execute('BEGIN IMMEDIATE')
            import inference_dependency_loss as dependency_loss
            expiring = {artifact_id for _, artifact_id in expired}
            footprints = [dependency_loss.capture_tx(db, row['project_id'], row['message_id'], artifact_ids=[row['artifact_id']])
                          for row in rows if row['artifact_id'] in expiring]
            db.executemany(
                "UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true'),"
                "'$.invalidated_reason',?) "
                "WHERE artifact_id=?", expired)
            db.executemany(
                "UPDATE artifacts SET meta=json_remove(meta,'$.invalidated',"
                "'$.invalidated_reason') WHERE artifact_id=?", revived)
            for footprint in footprints:
                dependency_loss.record_tx(db, footprint)
            from ledger import bump_inference_analysis_epoch_tx
            for pid in adopted_projects:
                bump_inference_analysis_epoch_tx(db, pid)
    return len(expired)


def publish_fact_projection(ledger, member, pid, mid, fp, policy,
                               v2_doc, doc_hash) -> None:
    """Publish (or reuse) the legacy-shaped projection of an audited doc."""
    # T12: audited canonical docs also publish a legacy-shaped
    # projection so the read side (stats/queries/rollup/signals)
    # keeps working during migration — the artifact carries the
    # message hash so "current" predicates bind it like an
    # extract_llm row.
    # audited is only set by a break right after doc_hash was taken
    # for this v2_doc, so doc_hash is the audited document's hash
    from semantic_store import current_artifact
    from semantic_projection import (PROJECTION_VERSION,
                                     project_v2_doc_legacy,
                                     projection_current)
    prev_proj = current_artifact(ledger.db, KIND_FACT_PROJ, mid, fp, policy)
    # a row minted by an older projection version (or from another
    # audited document) is stale — supersede it with a fresh row
    if prev_proj is None \
            or not projection_current(prev_proj["meta"], doc_hash):
        record_stage(ledger, pid, mid, fp, policy,
                        "s5_projection", "done",
                        doc_hash=doc_hash)
        with ledger.db:
            from inference_dependency_loss import source_binding, capture_tx, record_tx
            ledger.db.execute('UPDATE artifacts SET artifact_id=artifact_id WHERE 0')
            prior_ids = [row[0] for row in ledger.db.execute(
                'SELECT artifact_id FROM artifacts WHERE kind=? AND project_id=? AND message_id=? AND '
                + current_meta_pred('meta') + " AND CASE WHEN json_valid(meta) THEN "
                "json_extract(meta,'$.fingerprint')=? AND json_extract(meta,'$.policy_fingerprint')=? "
                "AND json_extract(meta,'$.invalidated') IS NOT 1 END ORDER BY artifact_id DESC LIMIT 1",
                (KIND_FACT_PROJ, pid, mid, fp, policy))] if prev_proj else []
            footprint = capture_tx(ledger.db, pid, mid, artifact_ids=prior_ids)
            content = project_v2_doc_legacy(v2_doc)
            new_id = ledger.artifact_add_tx(
                KIND_FACT_PROJ,
                json.dumps(content, ensure_ascii=False, allow_nan=False),
                project_id=pid, message_id=mid,
                model=semantic_transport.llm_model(),
                meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp,
                      "policy_fingerprint": policy,
                      "source_binding": source_binding(ledger.db, pid, mid),
                      "schema": SCHEMA_VERSION,
                      "hash": member["revision"],
                      "doc_hash": doc_hash,
                      "provider_provenance": _projection_provenance(ledger.db, pid, mid, fp, policy, member, v2_doc, content),
                      "projection_version": PROJECTION_VERSION})
            record_tx(ledger.db, footprint, replacement_id=new_id)
            ledger.bump_inference_analysis_epoch_tx(pid)
    else:
        record_stage(ledger, pid, mid, fp, policy,
                        "s5_projection", "reused",
                        doc_hash=doc_hash)



def _publish_skipped(ledger, pid, mid, fp, policy, member, doc):
    """Store a separately typed, fully bound non-Jev read model."""
    from semantic_audit import fact_audit_verdict, optional_fact_audit
    from semantic_projection import PROJECTION_VERSION, project_v2_doc_legacy
    from inference_dependency_loss import source_binding, capture_tx, record_tx

    def latest(kind):
        row = ledger.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? "
                                "AND project_id=? AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                                (kind, pid, mid)).fetchone()
        if row is None:
            return None
        try:
            content, meta = strict_json(row["content"]), strict_json(row["meta"])
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
        if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                or meta.get("fingerprint") != fp or meta.get("policy_fingerprint") != policy
                or meta.get("invalidated")):
            return None
        return {"id": row["artifact_id"], "content": content, "meta": meta}

    facts, fact_audit, summary, audit = (latest(kind) for kind in
        (KIND_FACTS_V2, KIND_FACT_AUDIT, KIND_SUMMARY, KIND_AUDIT))
    if not all((facts, fact_audit, summary, audit)):
        return None
    doc_hash = semantic_facts.doc_hash(doc)
    document_hash = payload_hash(doc)
    if payload_hash(facts["content"]) != document_hash:
        return None
    reason = audit["meta"].get("jev_skip_reason")
    if (audit["content"].get("status") != "SKIPPED" or audit["meta"].get("audit_status") != "SKIPPED"
            or audit["meta"].get("jev_evaluated") is not False or audit["meta"].get("code_checks") != "passed"
            or summary["meta"].get("audit_status") != "SKIPPED"
            or summary["meta"].get("jev_skip_reason") != reason
            or summary["meta"].get("jev_evaluated") is not False
            or summary["meta"].get("code_checks") != "passed"
            or audit["meta"].get("summary_hash") != payload_hash(summary["content"])
            or summary["meta"].get("summary_hash") != payload_hash(summary["content"])
            or summary["meta"].get("target_revision") != member["revision"]):
        return None
    fact_content, fact_meta = fact_audit["content"], fact_audit["meta"]
    if fact_content.get("status") == "SKIPPED":
        if (fact_meta.get("doc_hash") != doc_hash or fact_content.get("document_hash") != document_hash
                or fact_content.get("jev_skip_reason") != reason or fact_content.get("code_checks") != "passed"
                or optional_fact_audit(doc, member, fp, reason, fact_content)["status"] != "SKIPPED"):
            return None
    elif fact_audit_verdict({"meta": fact_meta, "content": fact_content}, doc_hash) != "PASS":
        return None
    content = project_v2_doc_legacy(doc)
    meta = {VERSION_KEY: CURRENT_VERSION, "fingerprint": fp, "policy_fingerprint": policy,
            "source_binding": source_binding(ledger.db, pid, mid), "hash": member["revision"],
            "doc_hash": doc_hash, "document_hash": document_hash, "engine_version": ENGINE_VERSION,
            "projection_version": PROJECTION_VERSION, "audit_status": "SKIPPED", "jev_evaluated": False,
            "jev_skip_reason": reason, "code_checks": "passed", "fact_artifact_id": facts["id"],
            "fact_audit_artifact_id": fact_audit["id"], "summary_artifact_id": summary["id"],
            "audit_artifact_id": audit["id"], "inference_requests": facts["meta"].get("inference_requests"),
            "provider_provenance": _projection_provenance(ledger.db, pid, mid, fp, policy, member, doc, content)}
    prior = latest(KIND_V4_SKIPPED)
    if prior is not None and prior["content"] == content and prior["meta"] == meta:
        return prior["id"]
    footprint = capture_tx(ledger.db, pid, mid, artifact_ids=[prior["id"]] if prior else [])
    artifact_id = ledger.artifact_add_tx(KIND_V4_SKIPPED, json.dumps(content, ensure_ascii=False, allow_nan=False),
                                         project_id=pid, message_id=mid, meta=meta)
    record_tx(ledger.db, footprint, replacement_id=artifact_id)
    ledger.bump_inference_analysis_epoch_tx(pid)
    return artifact_id


def current_skipped(db, pid, mid, fp, policy, member):
    """Read the distinct skipped projection only while all receipts still bind."""
    from semantic_audit import fact_audit_verdict, optional_fact_audit
    from semantic_projection import PROJECTION_VERSION, project_v2_doc_legacy
    from semantic_send_gate import skipped_summary_current
    from inference_dependency_loss import source_binding

    def latest(kind):
        row = db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind=? "
                         "AND project_id=? AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
                         (kind, pid, mid)).fetchone()
        if row is None:
            return None
        try:
            content, meta = strict_json(row["content"]), strict_json(row["meta"])
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
        if (not isinstance(content, dict) or not isinstance(meta, dict) or not is_current(meta)
                or meta.get("fingerprint") != fp or meta.get("policy_fingerprint") != policy
                or meta.get("invalidated")):
            return None
        return {"id": row["artifact_id"], "content": content, "meta": meta}

    try:
        skipped, facts, fact_audit, summary, audit = (latest(kind) for kind in
            (KIND_V4_SKIPPED, KIND_FACTS_V2, KIND_FACT_AUDIT, KIND_SUMMARY, KIND_AUDIT))
        if not all((skipped, facts, fact_audit, summary, audit)):
            return None
        meta, doc = skipped["meta"], facts["content"]
        reason = meta.get("jev_skip_reason")
        document_hash, doc_hash = payload_hash(doc), semantic_facts.doc_hash(doc)
        if (meta.get("audit_status") != "SKIPPED" or meta.get("jev_evaluated") is not False
                or meta.get("code_checks") != "passed" or meta.get("hash") != member["revision"]
                or meta.get("engine_version") != ENGINE_VERSION or meta.get("projection_version") != PROJECTION_VERSION
                or meta.get("source_binding") != source_binding(db, pid, mid)
                or meta.get("document_hash") != document_hash or meta.get("doc_hash") != doc_hash
                or meta.get("fact_artifact_id") != facts["id"] or meta.get("fact_audit_artifact_id") != fact_audit["id"]
                or meta.get("summary_artifact_id") != summary["id"] or meta.get("audit_artifact_id") != audit["id"]
                or skipped["content"] != project_v2_doc_legacy(doc)
                or not skipped_summary_current(db, pid, mid, fp, policy, member["revision"],
                                               summary["meta"].get("publication_mode"))
                or audit["meta"].get("jev_skip_reason") != reason):
            return None
        fact_content, fact_meta = fact_audit["content"], fact_audit["meta"]
        if fact_content.get("status") == "SKIPPED":
            if (fact_meta.get("doc_hash") != doc_hash or fact_content.get("document_hash") != document_hash
                    or fact_content.get("jev_skip_reason") != reason or fact_content.get("code_checks") != "passed"
                    or optional_fact_audit(doc, member, fp, reason, fact_content)["status"] != "SKIPPED"):
                return None
        elif fact_audit_verdict({"meta": fact_meta, "content": fact_content}, doc_hash) != "PASS":
            return None
        return skipped
    except (KeyError, TypeError, ValueError, RecursionError, OverflowError):
        return None


def publish_target_tx(ledger, pid: int, mid: int, fp: str, policy: str,
                members: dict, v2_docs_by_target: dict,
                final_status: str, findings: list,
                fact_source: str) -> None:
    """T18 S8 — called INSIDE the caller's ``with ledger.db`` so the
    read-model switch (or its diagnostic) commits atomically with the
    status receipt. For canonical facts, an all-PASS chain mints the
    ``semantic_facts_v4`` read model. Eligible fully-bound SKIPPED results
    mint ``semantic_facts_v4_skipped``; other outcomes leave a
    ``v4_diagnostic`` receipt that no extraction reader can select."""
    from semantic_store import current_artifact
    if fact_source != "canonical":
        return
    doc = v2_docs_by_target.get(mid)
    if doc is None:
        prev_v2 = current_artifact(ledger.db, KIND_FACTS_V2, mid, fp)
        # never publish from a document the hint rebuild could not re-derive
        doc = prev_v2["content"] if prev_v2 and prev_v2["meta"].get(
            "hint_rebuild") in (None, "same") else None
    doc_hash = semantic_facts.doc_hash(doc) if doc else None
    if final_status == "SKIPPED" and doc is not None:
        vid = _publish_skipped(ledger, pid, mid, fp, policy, members[mid], doc)
        if vid is not None:
            record_stage(ledger, pid, mid, fp, policy, "s8_publish", "SKIPPED", tx=True,
                         artifact_id=vid, doc_hash=doc_hash)
            return
    if final_status == "PASS" and doc is not None:
        vid = publish(ledger, pid, mid, fp, policy,
                         members[mid], doc)
        record_stage(ledger, pid, mid, fp, policy, "s8_publish",
                        "PASS", tx=True, artifact_id=vid,
                        doc_hash=doc_hash)
    else:
        diagnostic(ledger, pid, mid, fp, policy, final_status,
                      findings, doc_hash=doc_hash, tx=True)
        record_stage(ledger, pid, mid, fp, policy, "s8_publish",
                        final_status, tx=True, doc_hash=doc_hash)
