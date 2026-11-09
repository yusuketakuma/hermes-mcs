"""Synthetic source and snapshot boundaries for care-event references."""
import extract_llm_runner
import extract_llm_source_contract
import extract_llm_store
import json
import sqlite3

import pytest

import care_event_sources
import extract_llm
import inference_dependency_loss
import inference_version
from extract_testkit import _ledger, _message


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *a, **k: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', extract_llm._rerender_cards)
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    with ledger.db:
        ledger.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 "
                          "WHERE project_id=1")
    yield ledger
    ledger.close()


def _save(store, mid, body, *, records=None):
    store.save_messages([_message(mid=mid, body=body, posted_at="2026-10-03T09:00:00+09:00")])
    row = dict(store.db.execute("SELECT * FROM messages WHERE project_id=1 AND message_id=?", (mid,)).fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(store.db, 1, mid)
    row["_target"] = ("", extract_llm.resolved_model({}))
    integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
                 "completeness_chunks": 1,
                 "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(store, row, json.dumps({"event_records": records or []}, ensure_ascii=False),
                                        integrity=integrity)


def _event(body, *, kind="discharge", subject="patient", status="current", negated=False):
    return {"kind": kind, "subject": subject, "status": status, "negated": negated,
            "evidence": body, "span": {"start": 0, "end": len(body)}}


def _read(store, **kwargs):
    if not store.db.in_transaction:
        store.db.execute("BEGIN")
    return care_event_sources.collect(store.db, 1, cfg={}, **kwargs)


def test_source_bound_event_preserves_axes_and_borrowed_snapshot_without_content(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    before = store.db.total_changes
    result = _read(store)
    event, = result["events"]
    assert event["kind"] == "discharge" and event["event_day"] == "2026-09-01"
    assert event["subject"] == "patient" and event["negated"] is False
    assert event["project_id"] == 1 and event["message_id"] == 1 and event["artifact_id"] > 0
    assert event["span"] == [0, len(body)] and body not in json.dumps(result, ensure_ascii=False)
    assert result["coverage"]["source_complete"] is True
    assert store.db.in_transaction and store.db.total_changes == before
    store.db.rollback()


def test_local_collect_projects_verified_care_detail_record(store):
    # kind=other detail record through the local extract path: collect →
    # project → validate_record → context_clean end to end.
    body = "本人は2026年9月1日にサービス担当者会議を実施しました。"
    record = {"kind": "other", "subject": "unknown", "status": "unknown",
              "negated": None, "evidence": body,
              "span": {"start": 0, "end": len(body)},
              "care_detail": {"event_type": "care_team_conference",
                              "workflow_status": "performed",
                              "date_text": "2026年9月1日",
                              "service_actor": "unknown"}}
    _save(store, 1, body, records=[record])
    before = store.db.total_changes
    result = _read(store)
    event, = result["events"]
    assert event["kind"] == "other" and event["unverified"] is False
    assert event["subject"] == "patient" and event["negated"] is False
    assert event["care_detail"] == {"event_type": "care_team_conference",
                                    "workflow_status": "performed",
                                    "date_text": "2026年9月1日",
                                    "service_actor": "unknown"}
    assert store.db.in_transaction and store.db.total_changes == before
    store.db.rollback()


def test_borrowed_snapshot_is_required_and_zero_is_not_unparsed(store):
    body = "本人は落ち着いています。"
    _save(store, 1, body)
    assert not store.db.in_transaction
    assert care_event_sources.collect(store.db, 1, cfg={})["coverage"]["state"] == "snapshot_required"
    checked = _read(store)
    assert checked["events"] == [] and checked["coverage"]["source_complete"] is True
    store.db.rollback()
    store.save_messages([_message(mid=2, body="本人は検査を受けました。")])
    pending = _read(store)
    assert pending["coverage"]["source_complete"] is False
    assert pending["coverage"]["not_extracted"] == 1


@pytest.mark.parametrize("body,kind,subject,status,negated", [
    ("家族は2026年9月1日に入院予定です。", "admission", "family", "planned", False),
    ("本人は2026年9月1日に退院していません。", "discharge", "patient", "current", True),
    ("本人は2026年12月1日に退院予定です。", "discharge", "patient", "planned", False),
])
def test_family_negation_and_plans_keep_source_axes(store, body, kind, subject, status, negated):
    _save(store, 1, body, records=[_event(body, kind=kind, subject=subject, status=status, negated=negated)])
    event, = _read(store)["events"]
    assert (event["subject"], event["status"], event["negated"]) == (subject, status, negated)


def test_other_patient_is_never_included_and_scope_refusal_is_not_empty(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    store.ensure_patient(2)
    store.save_messages([_message(mid=2, project_id=2, body="家族の合成記録")], project_id=2)
    out = _read(store)
    assert all(event["project_id"] == 1 and event["message_id"] == 1 for event in out["events"])
    store.db.rollback()
    store.db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
    refused = care_event_sources.collect(store.db, 1, cfg={})
    assert refused["events"] == [] and refused["coverage"]["state"] == "scope_unavailable"
    assert refused["coverage"]["source_complete"] is False


def test_revision_aba_and_no_configuration_never_reuse_old_extraction(store):
    original = "本人は2026年9月1日に退院しました。"
    _save(store, 1, original, records=[_event(original)])
    store.save_messages([_message(mid=1, body="本人は別の合成記録です。")])
    store.save_messages([_message(mid=1, body=original)])
    result = _read(store)
    assert result["events"] == [] and result["coverage"]["source_complete"] is False
    assert result["coverage"]["not_extracted"] + result["coverage"]["unverified_extraction"] == 1
    without_cfg = care_event_sources.collect(store.db, 1, cfg=None)
    assert not without_cfg["events"] and not without_cfg["coverage"]["source_complete"]


def test_canonical_presence_never_silently_falls_back_to_local_result(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    store.artifact_add("canonical_projection", json.dumps({"event_records": []}), project_id=1, message_id=1,
                       meta={"hash": "old-revision", inference_version.VERSION_KEY: inference_version.LEGACY_VERSION})
    result = _read(store)
    assert result["events"] == [] and result["coverage"]["canonical_unconfirmed"] == 1
    assert result["coverage"]["source_complete"] is False


def _canonical_care_fixture(tmp_path, monkeypatch, *, audit_status="PASS", tamper=None,
                            care_detail=None, body=None, statement=None):
    """Drain one synthetic care_event fact through the real semantic pipeline."""
    import json as _json
    import semantic_testkit as st
    db = st._ledger(tmp_path)
    p = st._patient(db)
    body = body or "本人は2026年9月1日に退院しました。"
    p.messages = [st._message(1, body=body)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    fact = {"statement": statement or "退院", "kind": "care_event",
            "subject_role": "patient",
            "polarity": "affirmed", "epistemic": "asserted",
            "workflow_status": "performed", "importance": "T1",
            "evidence_quote": body}
    if care_detail is not None:
        fact["care_detail"] = care_detail

    def llm(prompt):
        if "要約器" in prompt:
            return st._summary_from_facts(prompt)
        import semantic_transport
        semantic_transport._record_inference("local")
        return _json.dumps({"facts": [fact],
                            "category_presence": dict(st.NO_FACTS, care_event="one")},
                           ensure_ascii=False)
    choice_map = {f"has_{c}": "absent" for c in st.sf.MANDATORY_CATEGORIES}
    choice_map["has_care_event"] = "present"
    st._drain(db, llm=llm, jev=st._FakeJev(choice_map=choice_map))
    if audit_status != "PASS":
        with db.db:
            db.db.execute("UPDATE artifacts SET content=json_set(content,'$.status',?) "
                          "WHERE kind='semantic_facts_audit'", (audit_status,))
    if tamper == "projection":
        with db.db:
            db.db.execute("UPDATE artifacts SET content='{' WHERE kind IN "
                          "('canonical_projection','semantic_facts_v4')")
    elif tamper == "facts":
        with db.db:
            db.db.execute("UPDATE artifacts SET content='{' WHERE kind='semantic_facts_v2'")
    return db, body


def test_audited_canonical_care_event_projects_source_bound_record(tmp_path, monkeypatch):
    db, body = _canonical_care_fixture(tmp_path, monkeypatch)
    try:
        cfg = __import__("semantic_testkit")._canonical_cfg()
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        event, = result["events"]
        assert event["kind"] == "discharge" and event["event_day"] == "2026-09-01"
        assert event["subject"] == "patient" and event["negated"] is False
        assert event["unverified"] is False and body not in json.dumps(result, ensure_ascii=False)
        assert result["coverage"]["canonical_unconfirmed"] == 0
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
    finally:
        db.close()


def test_audited_canonical_unfounded_care_detail_is_dropped(tmp_path, monkeypatch):
    # 退院の記録だけでは退院前カンファレンスの根拠にならない: detail は
    # 捨てられ、親の discharge event は検証済みのまま残る。
    detail = {"event_type": "pre_discharge_conference", "workflow_status": "performed",
              "date_text": "2026年9月1日"}
    db, _body = _canonical_care_fixture(tmp_path, monkeypatch, care_detail=detail)
    try:
        cfg = __import__("semantic_testkit")._canonical_cfg()
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        event, = result["events"]
        assert event["kind"] == "discharge" and event["unverified"] is False
        assert "care_detail" not in event
        assert result["coverage"]["canonical_unconfirmed"] == 0
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
    finally:
        db.close()


def test_audited_canonical_care_detail_reaches_projected_event(tmp_path, monkeypatch):
    # kind=other: legacy cue に載らない会議記録を detail 自身の cue で検証する。
    body = "本人は2026年9月1日にサービス担当者会議を実施しました。"
    detail = {"event_type": "care_team_conference", "workflow_status": "performed",
              "date_text": "2026年9月1日"}
    db, _body = _canonical_care_fixture(
        tmp_path, monkeypatch, care_detail=detail, body=body,
        statement="サービス担当者会議を実施しました")
    try:
        cfg = __import__("semantic_testkit")._canonical_cfg()
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        event, = result["events"]
        assert event["kind"] == "other" and event["unverified"] is False
        assert event["care_detail"] == {"event_type": "care_team_conference",
                                        "workflow_status": "performed",
                                        "date_text": "2026年9月1日",
                                        "service_actor": "unknown"}
        assert result["coverage"]["canonical_unconfirmed"] == 0
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
    finally:
        db.close()


@pytest.mark.parametrize("audit_status,tamper", [
    ("NEEDS_REVIEW", None), ("PASS", "projection"), ("PASS", "facts")])
def test_canonical_care_event_damaged_proof_never_falls_back(tmp_path, monkeypatch, audit_status, tamper):
    db, _body = _canonical_care_fixture(tmp_path, monkeypatch, audit_status=audit_status, tamper=tamper)
    try:
        cfg = __import__("semantic_testkit")._canonical_cfg()
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        assert result["events"] == [] and result["coverage"]["canonical_unconfirmed"] == 1
        assert result["coverage"]["source_complete"] is False
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
    finally:
        db.close()


def _tamper_canonical_artifact(db, kind, *, meta_update=None, meta_remove=None,
                               content=None, content_set=None):
    """Damage every stored artifact of ``kind`` in place.

    ``kind="canonical"`` damages both projection-carrying kinds, since the
    reader selects whichever row ``current_fact_pred`` prefers.
    """
    kinds = care_event_sources._CANONICAL_KINDS if kind == "canonical" else (kind,)
    rows = [row for k in kinds for row in db.db.execute(
        "SELECT artifact_id,meta,content FROM artifacts WHERE kind=?", (k,)).fetchall()]
    assert rows, kind
    for row in rows:
        meta = json.loads(row["meta"] or "{}")
        if meta_update:
            meta.update(meta_update)
        for key in meta_remove or ():
            meta.pop(key, None)
        new_content = content if content is not None else row["content"]
        if content_set is not None:
            doc = json.loads(row["content"])
            target = doc
            for key in content_set[0][:-1]:
                target = target[key]
            target[content_set[0][-1]] = content_set[1]
            new_content = json.dumps(doc, ensure_ascii=False)
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=?,content=? WHERE artifact_id=?",
                          (json.dumps(meta, ensure_ascii=False), new_content, row["artifact_id"]))


@pytest.mark.parametrize("tamper", [
    # projection binding: source fingerprint / policy / doc hash
    ("canonical", {"meta_update": {"source_binding": "wrong"}}),
    ("canonical", {"meta_update": {"fingerprint": "wrong"}}),
    ("canonical", {"meta_update": {"policy_fingerprint": "wrong"}}),
    ("canonical", {"meta_update": {"doc_hash": "wrong"}}),
    # newest audit verdict binding
    ("semantic_facts_audit", {"meta_update": {"doc_hash": "wrong"}}),
    ("semantic_facts_audit", {"content_set": (["evaluated"], False)}),
    ("semantic_facts_audit", {"content_set": (["status"], "NEEDS_REVIEW")}),
    # stored facts document and its producer proof
    ("semantic_facts_v2", {"content": "{"}),
    ("semantic_facts_v2", {"content_set": (["evidence", 0, "quote"], "x")}),
    ("semantic_facts_v2", {"meta_remove": ["provider_provenance"]}),
    ("semantic_facts_v2", {"meta_update": {"provider_provenance": {"version": 1}}}),
    ("semantic_facts_v2", {"meta_update": {"inference_engine_version": "wrong"}}),
    # chunk/manifest evidence the rebuild depends on
    ("semantic_source_manifest", {"content": "{"}),
    ("semantic_extraction_chunk_v2", {"content": "{"}),
    # projection payload itself
    ("canonical", {"content": "{"}),
    # fake repair claim without a real repair chain
    ("semantic_facts_v2", {"meta_update": {"repaired": True, "repair_artifact_id": 999999}}),
])
def test_each_canonical_evidence_link_damage_never_projects(tmp_path, monkeypatch, tamper):
    kind, ops = tamper
    db, _body = _canonical_care_fixture(tmp_path, monkeypatch)
    try:
        _tamper_canonical_artifact(db, kind, **ops)
        cfg = __import__("semantic_testkit")._canonical_cfg()
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        assert result["events"] == [] and result["coverage"]["canonical_unconfirmed"] == 1
        assert result["coverage"]["source_complete"] is False
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
    finally:
        db.close()


def test_canonical_event_records_obey_per_source_limit(tmp_path, monkeypatch):
    db, _body = _canonical_care_fixture(tmp_path, monkeypatch)
    try:
        cfg = __import__("semantic_testkit")._canonical_cfg()
        monkeypatch.setattr(care_event_sources, "_PER_SOURCE_LIMIT", 0)
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        assert result["events"] == [] and result["coverage"]["oversize"] == 1
        assert result["coverage"]["canonical_unconfirmed"] == 0
        assert result["coverage"]["source_complete"] is False
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
    finally:
        db.close()


def _canonical_repair_care_fixture(tmp_path, monkeypatch):
    """One care_event carried through real extract/repair/publish functions.

    ``extract_facts_v2``/``repair_facts_v2`` run for real while the audit,
    reservation and receipt artifacts are synthesized to the stored-answer
    contract ``_repaired_projection_producer`` replays, then ``v4.publish``
    writes the canonical artifact. This is not the ``semantic_drain``
    orchestrator path.
    """
    import hashlib as _hl
    import mcs_util
    import semantic_extraction
    import semantic_facts as sfacts
    import semantic_policy
    import semantic_publication as v4
    import semantic_store
    import semantic_testkit as st
    import semantic_transport
    from inference_dependency_loss import source_binding
    from semantic_policy import (KIND_FACTS_V2, KIND_FACT_AUDIT, KIND_FACT_REPAIR)

    db = st._ledger(tmp_path)
    p = st._patient(db)
    body = "本人は2026年9月1日に退院しました。"
    p.messages = [st._message(1, body=body)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    bundle = semantic_store.thread_bundle(db.db, 1, 1)
    member = next(m for m in bundle["members"] if m["message_id"] == 1)
    fp = bundle["source_fingerprint"]
    policy, errors = semantic_policy.runtime_config(st._canonical_cfg())
    assert not errors
    policy_fp = semantic_policy.policy_fingerprint(policy)
    common = {inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
              "fingerprint": fp, "policy_fingerprint": policy_fp}

    original_item = {"statement": "退院", "kind": "care_event",
                     "subject_role": "patient", "polarity": "affirmed",
                     "epistemic": "asserted", "workflow_status": "performed",
                     "importance": "T1", "evidence_quote": body}

    def observed(prompt):
        semantic_transport._record_inference("local")
        return json.dumps({"facts": [original_item],
                           "category_presence": dict(st.NO_FACTS, care_event="one")},
                          ensure_ascii=False)

    original = semantic_extraction.extract_facts_v2(
        observed, member, ledger=db, project_id=1, source_fingerprint=fp)["doc"]
    original_meta = {**common, "provider_provenance": {"version": 1, "stage": "facts",
        "providers": ["local"], "complete": True,
        "result_hash": mcs_util.payload_hash(original),
        "source_fingerprint": fp, "policy_fingerprint": policy_fp,
        "target_revision": member["revision"]}}
    original_id = db.artifact_add(KIND_FACTS_V2, json.dumps(original), project_id=1,
                                  message_id=1, meta=original_meta)
    original_hash = sfacts.doc_hash(original)
    fid = original["facts"][0]["fact_id"]
    rejected = {fid: "fact_not_supported"}
    audit = {"status": "NEEDS_REVIEW", "evaluated": True,
             "findings": [{"code": "fact_not_supported", "fact": fid}],
             "fact_verdicts": {fid: {"choice": "not_supported", "confidence": 0.9}}}
    audit_id = db.artifact_add(KIND_FACT_AUDIT, json.dumps(audit), project_id=1,
        message_id=1, meta={**common, "doc_hash": original_hash,
            "jev_provenance": {"version": 1, "stage": "fact_audit", "outcome": "done",
                "result_hash": mcs_util.payload_hash(audit), "doc_hash": original_hash,
                "document_hash": mcs_util.payload_hash(original)}})
    started_id = db.artifact_add(KIND_FACT_REPAIR,
        json.dumps({"status": "started", "rejected": rejected}), project_id=1,
        message_id=1, meta={**common, "doc_hash": original_hash})
    rid = "b" * 32
    reserve_id = db.artifact_add("cloud_usage_reserve", "{}", project_id=1,
                                 message_id=1, meta={"reservation_id": rid})

    repaired_item = dict(original_item, statement="退院の記録(修復)")
    repair_text = json.dumps({"facts": [repaired_item],
                              "category_presence": dict(st.NO_FACTS, care_event="one")},
                             ensure_ascii=False)
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return repair_text

    repaired = semantic_extraction.repair_facts_v2(
        answer, member, original, rejected, coverage_missing=False)
    assert repaired["repaired"] and len(prompts) == 1
    binding = source_binding(db.db, 1, 1)
    root = db.db.execute(
        "SELECT COALESCE(parent_id,message_id) FROM messages "
        "WHERE project_id=1 AND message_id=1").fetchone()[0]
    owner = {"semantic_target_id": 1, "semantic_thread_root": root,
             "semantic_phase": "facts",
             "semantic_source": mcs_util.payload_hash([(1, binding)]),
             "semantic_generation": "synthetic"}
    contract = {"backend": "openai", "model": "synthetic-sol",
                "prompt": "synthetic-contract"}
    answer_record = {"text": repair_text,
                     "prompt_sha256": _hl.sha256(prompts[0].encode()).hexdigest(),
                     "reservation_id": rid, "unit_key": "semantic:synthetic",
                     "owner": owner, "contract": contract}
    evidence = {"version": 1, "original_artifact_id": original_id,
                "audit_artifact_id": audit_id, "started_artifact_id": started_id,
                "source_binding": binding, "target_revision": member["revision"],
                "original_doc_hash": original_hash,
                "repaired_doc_hash": sfacts.doc_hash(repaired["doc"]),
                "coverage_missing": False, "answers": [answer_record]}
    completed = {"rejected": rejected, "repaired": True,
                 "owner_chunk_ids": repaired["owner_chunk_ids"],
                 "repaired_fact_ids": repaired["repaired_fact_ids"],
                 "__mcs_internal_proof_v1": evidence}
    completed_id = db.artifact_add(KIND_FACT_REPAIR, json.dumps(completed),
                                   project_id=1, message_id=1, meta=common)
    repaired_meta = {**common, "repaired": True,
                     "repair_artifact_id": completed_id,
                     "coverage_status": repaired["doc"]["coverage"]["status"],
                     "facts": len(repaired["doc"]["facts"])}
    db.artifact_add(KIND_FACTS_V2, json.dumps(repaired["doc"]), project_id=1,
                    message_id=1, meta=repaired_meta)
    repaired_hash = sfacts.doc_hash(repaired["doc"])
    new_fid = repaired["doc"]["facts"][0]["fact_id"]
    pass_audit = {"status": "PASS", "evaluated": True, "findings": [],
                  "fact_verdicts": {new_fid: {"choice": "supports", "confidence": 0.9}}}
    db.artifact_add(KIND_FACT_AUDIT, json.dumps(pass_audit), project_id=1,
        message_id=1, meta={**common, "doc_hash": repaired_hash,
            "audit_status": "PASS",
            "jev_provenance": {"version": 1, "stage": "fact_audit", "outcome": "done",
                "result_hash": mcs_util.payload_hash(pass_audit),
                "doc_hash": repaired_hash,
                "document_hash": mcs_util.payload_hash(repaired["doc"])}})

    def verify(_db, **kw):
        if (kw["reservation_artifact_id"] != reserve_id or kw["reservation_id"] != rid
                or kw["prompt"] != prompts[0] or kw["answer_text"] != repair_text
                or kw["owner"] != owner):
            raise ValueError("synthetic-receipt-invalid")
        return {}

    monkeypatch.setattr(__import__("cloud_inference"), "verify_cloud_repair_evidence", verify)
    with db.db:
        v4.publish(db, 1, 1, fp, policy_fp, member, repaired["doc"])
    return db, body, prompts


def _tamper_stored_repair_answer(db):
    """Damage one stored repair-answer element after publication."""
    import json as _json
    from semantic_policy import KIND_FACT_REPAIR
    rows = db.db.execute(
        "SELECT artifact_id, content FROM artifacts WHERE kind=?",
        (KIND_FACT_REPAIR,)).fetchall()
    for row in rows:
        doc = _json.loads(row["content"])
        proof = doc.get("__mcs_internal_proof_v1")
        if not isinstance(proof, dict):
            continue
        answers = proof.get("answers")
        if isinstance(answers, list) and answers:
            answers[0]["text"] += "changed"
            db.db.execute(
                "UPDATE artifacts SET content=? WHERE artifact_id=?",
                (_json.dumps(doc, ensure_ascii=False), row["artifact_id"]))


def test_repaired_canonical_care_event_projects_through_collect(tmp_path, monkeypatch):
    db, _body, prompts = _canonical_repair_care_fixture(tmp_path, monkeypatch)
    try:
        cfg = __import__("semantic_testkit")._canonical_cfg()
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        event, = result["events"]
        assert event["kind"] == "discharge" and event["unverified"] is False
        assert result["coverage"]["canonical_unconfirmed"] == 0
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
        # Collect replays stored-answer evidence only; it dispatches no
        # repair prompt of its own.
        assert len(prompts) == 1
    finally:
        db.close()


def test_repair_evidence_damage_never_projects(tmp_path, monkeypatch):
    db, _body, prompts = _canonical_repair_care_fixture(tmp_path, monkeypatch)
    try:
        cfg = __import__("semantic_testkit")._canonical_cfg()
        # A clean collect first proves the published projection projects;
        # the reader's verdict is not cached publish-side success.
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        assert result["events"] and result["coverage"]["canonical_unconfirmed"] == 0
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
        # Damaging one stored repair-answer element afterwards must fail the
        # read-time replay, not be masked by the earlier publish success.
        with db.db:
            _tamper_stored_repair_answer(db)
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        assert result["events"] == [] and result["coverage"]["canonical_unconfirmed"] == 1
        assert result["coverage"]["source_complete"] is False
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
        assert len(prompts) == 1
    finally:
        db.close()


def test_canonical_aba_revision_never_reads_stale_events(tmp_path, monkeypatch):
    """A real A->B->A save_messages cycle must not revive the old canonical
    generation even though the restored body hash is identical."""
    import semantic_testkit as st
    db, body = _canonical_care_fixture(tmp_path, monkeypatch)
    try:
        db.save_messages([st._message(1, body="本人は別の合成記録です。")])
        db.save_messages([st._message(1, body=body)])
        cfg = st._canonical_cfg()
        db.db.execute("BEGIN")
        before = db.db.total_changes
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        assert result["events"] == []
        assert result["coverage"]["canonical_unconfirmed"] == 1
        assert result["coverage"]["source_complete"] is False
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
    finally:
        db.close()


def _canonical_bulk_care_fixture(tmp_path, monkeypatch, count):
    """``count`` care_event facts, each bound to its own sentence.

    Statements use disjoint entity tokens so the quadratic relation scan
    emits no pairs; evidence quotes are unique verbatim sentences.
    """
    import semantic_testkit as st
    db = st._ledger(tmp_path)
    p = st._patient(db)
    body = "".join(f"記録{i}。" for i in range(count))
    p.messages = [st._message(1, body=body)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    facts = [{"statement": f"item{i:04d}", "kind": "care_event",
              "subject_role": "patient", "polarity": "affirmed",
              "epistemic": "asserted", "workflow_status": "performed",
              "importance": "T2",
              "evidence_quote": f"記録{i}。"}
             for i in range(count)]
    presence = dict(st.NO_FACTS, care_event="multiple")

    def llm(prompt):
        if "要約器" in prompt:
            return st._summary_from_facts(prompt)
        import semantic_transport
        semantic_transport._record_inference("local")
        return json.dumps({"facts": facts, "category_presence": presence},
                          ensure_ascii=False)
    choice_map = {f"has_{c}": "absent" for c in st.sf.MANDATORY_CATEGORIES}
    choice_map["has_care_event"] = "present"
    st._drain(db, llm=llm, jev=st._FakeJev(choice_map=choice_map))
    return db


@pytest.mark.parametrize("count,oversize,projected", [(256, 0, True), (257, 1, False)])
def test_canonical_event_records_real_256_257_boundary(tmp_path, monkeypatch, count, oversize, projected):
    db = _canonical_bulk_care_fixture(tmp_path, monkeypatch, count)
    try:
        import care_event_timeline
        cfg = __import__("semantic_testkit")._canonical_cfg()
        calls = []
        real_project = care_event_timeline.project
        monkeypatch.setattr(care_event_timeline, "project",
                            lambda *a, **k: calls.append(1) or real_project(*a, **k))
        db.db.execute("BEGIN")
        before = db.db.total_changes
        # The saved projection keeps every generated record; a future
        # generation-side drop would surface here before collect's limit.
        row = db.db.execute(
            "SELECT content FROM artifacts WHERE project_id=1 AND message_id=1 "
            "AND kind IN ('canonical_projection','semantic_facts_v4') "
            "ORDER BY artifact_id DESC LIMIT 1").fetchone()
        saved_records = json.loads(row["content"]).get("event_records") or []
        assert len(saved_records) == count
        result = care_event_sources.collect(db.db, 1, cfg=cfg)
        assert result["coverage"]["oversize"] == oversize
        assert bool(calls) is projected
        assert result["coverage"]["canonical_unconfirmed"] == 0
        assert result["coverage"]["source_complete"] is False
        assert db.db.in_transaction and db.db.total_changes == before
        db.db.rollback()
    finally:
        db.close()


def test_limits_and_incomplete_sources_are_reported_not_treated_as_zero(store):
    for mid, day in ((1, 1), (2, 2), (3, 3)):
        body = f"本人は2026年9月{day}日に退院しました。"
        _save(store, mid, body, records=[_event(body)])
    limited = _read(store, limit=2, event_limit=1)
    assert limited["coverage"]["truncated"] is True
    assert limited["coverage"]["event_truncated"] is True
    assert limited["coverage"]["source_complete"] is False
    assert len(limited["events"]) == 1
    store.db.rollback()
    store.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=3")
    partial = care_event_sources.collect(store.db, 1, cfg={})
    assert partial["coverage"]["incomplete"] == 1 and not partial["coverage"]["source_complete"]


@pytest.mark.parametrize("previously_observed", [False, True])
def test_deleted_original_never_claims_complete_history(store, previously_observed):
    if previously_observed:
        body = "本人は2026年9月1日に退院しました。"
        _save(store, 1, body, records=[_event(body)])
    store.save_messages([_message(mid=1, state="deleted", body="")])
    result = _read(store)
    assert result["events"] == [] and result["coverage"]["deleted"] == 1
    assert result["coverage"]["source_complete"] is False


@pytest.mark.parametrize("state,complete", [("done", True), ("pending", False), (None, False), ("other", False)])
def test_unfinished_or_unknown_fetch_jobs_block_complete_history(store, state, complete):
    store.ensure_patient(2)
    with store.db:
        store.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state) VALUES('history',1,0,?)", (state,))
        store.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state) VALUES('history',2,0,NULL)")
    result = _read(store)
    assert result["coverage"]["history_complete"] is complete
    assert result["coverage"]["source_complete"] is complete


def test_oversize_metadata_is_not_selected_or_parsed(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    raw_hash = store.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    store.artifact_add("extract_llm", "{}", project_id=1, message_id=1,
                       meta={"hash": raw_hash, inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
                             "padding": "x" * (care_event_sources._META_LIMIT + 1)})
    result = _read(store)
    assert result["events"] == [] and result["coverage"]["resource_state"] == "oversize_or_nul"
    assert result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["source_complete"] is False


@pytest.mark.parametrize("table,field,limit", [
    ("messages", "body_text", care_event_sources._BODY_LIMIT),
    ("messages", "body_html", care_event_sources._HTML_LIMIT),
    ("artifacts", "meta", care_event_sources._META_LIMIT),
    ("artifacts", "content", care_event_sources._ARTIFACT_LIMIT),
])
@pytest.mark.parametrize("long", [False, True])
def test_nul_values_are_held_before_json_or_body_decoding(store, table, field, limit, long):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    malformed = "x\0" + "y" * (limit + 1 if long else 1)
    if table == "messages":
        store.db.execute(f"UPDATE messages SET {field}=? WHERE message_id=1", (malformed,))
    else:
        store.db.execute(f"UPDATE artifacts SET {field}=? WHERE kind='extract_llm' AND message_id=1", (malformed,))
    result = care_event_sources.collect(store.db, 1, cfg={})
    assert result["events"] == [] and result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == "oversize_or_nul"
    assert result["coverage"]["source_complete"] is False and malformed not in str(result)


def test_oversize_prior_chunk_content_is_held_before_progress_parses_it(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    store.artifact_add("extract_llm_chunk", "x" * (care_event_sources._ARTIFACT_LIMIT + 1),
                       project_id=1, message_id=1, meta={})
    result = _read(store)
    assert not result["events"] and result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == "oversize_or_nul"


@pytest.mark.parametrize("table,field", [
    ("patients", "fetch_state"), ("patients", "fetch_reason"),
    ("artifacts", "model"), ("message_revisions", "content_hash"),
])
@pytest.mark.parametrize("embedded_nul", [False, True])
def test_additional_fields_are_bounded_before_reading(store, table, field, embedded_nul):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    malformed = "x\0y" if embedded_nul else "x" * (care_event_sources._HTML_LIMIT + 1)
    if table == "message_revisions":
        store.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                         "VALUES(1,1,'synthetic','full',0)")
    where = {"patients": "project_id=1", "artifacts": "kind='extract_llm' AND message_id=1",
             "message_revisions": "message_id=1"}[table]
    changed = store.db.execute(f"UPDATE {table} SET {field}=? WHERE {where}", (malformed,)).rowcount
    assert changed > 0

    class RecordingConnection:
        def __init__(self, db):
            self.db = db
            self.statements = []

        @property
        def in_transaction(self):
            return self.db.in_transaction

        def execute(self, sql, *args):
            self.statements.append(sql)
            return self.db.execute(sql, *args)

    db = RecordingConnection(store.db)
    before = store.db.total_changes
    result = care_event_sources.collect(db, 1, cfg={})
    assert result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == "oversize_or_nul"
    assert not result["events"] and result["coverage"]["source_complete"] is False
    assert malformed not in str(result) and store.db.in_transaction and store.db.total_changes == before
    if table == "patients":
        assert not any(sql.startswith("SELECT fetch_state,fetch_reason") for sql in db.statements)
    assert not any(sql.startswith("SELECT a.artifact_id,a.kind,a.meta,a.model") for sql in db.statements)


def test_revision_row_budget_is_scoped_to_patient(store, monkeypatch):
    store.save_messages([_message(mid=1, body="本人の合成記録です。")])
    store.ensure_patient(2)
    store.save_messages([_message(mid=2, project_id=2, body="別患者の合成記録です。")], project_id=2)
    store.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                     "VALUES(1,1,'synthetic','full',0)")
    for seq in range(1, 5):
        store.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                         "VALUES(2,?,'synthetic','full',0)", (seq,))
    monkeypatch.setattr(care_event_sources, "_MAX_PREFLIGHT_ROWS", 2)
    assert care_event_sources.collect(store.db, 1, cfg={})["coverage"]["resource_state"] == "bounded"
    for seq in range(2, 4):
        store.db.execute("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
                         "VALUES(1,?,'synthetic','full',0)", (seq,))
    result = care_event_sources.collect(store.db, 1, cfg={})
    assert result["coverage"]["resource_state"] == "row_limit"
    assert result["coverage"]["state"] == "resource_unconfirmed"
    assert not result["events"] and not result["coverage"]["source_complete"]


@pytest.mark.parametrize("cap,reason", [("_MAX_PREFLIGHT_ROWS", "row_limit"),
                                         ("_MAX_PREFLIGHT_BYTES", "byte_limit")])
def test_preflight_budget_exhaustion_stays_unconfirmed(store, monkeypatch, cap, reason):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    monkeypatch.setattr(care_event_sources, cap, 0 if cap == "_MAX_PREFLIGHT_ROWS" else 1)
    result = _read(store)
    assert not result["events"] and result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == reason
    assert result["coverage"]["source_complete"] is False


def test_byte_length_unavailable_is_explicitly_unconfirmed(store):
    class OlderSQLite:
        def __init__(self, db):
            self.db = db

        @property
        def in_transaction(self):
            return self.db.in_transaction

        def execute(self, sql, *args):
            if sql == "SELECT octet_length('x')":
                raise sqlite3.OperationalError("no such function: octet_length")
            return self.db.execute(sql, *args)

    store.db.execute("BEGIN")
    result = care_event_sources.collect(OlderSQLite(store.db), 1, cfg={})
    assert result["coverage"]["state"] == "resource_unconfirmed"
    assert result["coverage"]["resource_state"] == "byte_length_unavailable"
    assert not result["events"] and result["coverage"]["source_complete"] is False


def test_old_schema_missing_occurrence_binding_field_remains_unknown():
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE patients(project_id,fetch_state,fetch_reason,history_floor,initial_import,is_archived)")
        db.execute("CREATE TABLE messages(project_id,message_id,body_state,body_html,body_text,content_hash,"
                   "posted_at,posted_at_ts,parent_id,sender_id,sender_name,profession,organization)")
        db.execute("CREATE TABLE artifacts(artifact_id,project_id,message_id,kind,content,meta,model)")
        db.execute("CREATE TABLE message_revisions(message_id,seq)")
        db.execute("CREATE TABLE fetch_jobs(project_id,kind,state)")
        db.execute("CREATE TABLE attachments(message_id)")
        db.execute("BEGIN")
        result = care_event_sources.collect(db, 1, cfg={})
        assert result["coverage"]["state"] == "schema_unknown" and not result["events"]
        assert result["coverage"]["source_complete"] is False
    finally:
        db.close()


@pytest.mark.parametrize("value", [0, -1, 201, True])
def test_limits_are_bounded_without_reading(store, value):
    with pytest.raises(ValueError, match="care_event_scope_invalid"):
        care_event_sources.collect(store.db, 1, cfg={}, limit=value)
