"""Synthetic QC evidence survives a protected extract replacement."""

from contextlib import nullcontext
import json
import time

import pytest

import cloud_policy
import extract_llm as worker
import mcs_util
from extract_testkit import _extract_artifact, _hash, _message, _qc_artifact
from test_anthropic_runtime import _current_artifacts, _use_real_cloud_accounting, routed  # noqa: F401


@pytest.mark.parametrize("damage", ["none", "qc_feedback", "occurrence", "null_marker", "bool_version"])
def test_qc_proof_replays_after_atomic_replacement(routed, monkeypatch, tmp_path, damage):  # noqa: F811
    """Exercise the internal checkpoint/commit contract, not cloud admission."""
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    cfg["cloud_llm"]["provider"] = "openai"
    monkeypatch.setattr(worker, "HOME", str(tmp_path))
    monkeypatch.setattr(cloud_policy, "provider_key", lambda *_args: "synthetic-key")
    monkeypatch.setattr(worker.cloud_backlog, "request_slot", lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, "owner_alive", lambda: True)

    body = "本人に発熱と咳があります。本人の血圧120/80です。"
    led.save_messages([_message(1, body, project_id=1)])
    source_id = _extract_artifact(led, 1, {"symptoms": [{"text": "頭痛", "status": "ongoing",
        "subject": "patient", "evidence": "頭痛"}]}, _hash(led))
    qc_id = _qc_artifact(led, 1, source_id, _hash(led), {"qc": "done", "items": [
        {"section": "symptoms", "verdict": "NO_MATCH", "item": {"text": "頭痛"},
         "unused": "この項目は再生に不要"}], "unrelated": "保存しない合成値"})
    with led.db:
        led.db.execute("BEGIN IMMEDIATE")
        cloud_policy.capture_history_tx(led.db, "synthetic-qc-replay", time.time())

    rows, _ = worker._select_pending(led, 1, None, None, False)
    assert len(rows) == 1 and rows[0]["qc_src"] == source_id
    # Production admission intentionally excludes existing QC-flagged work.
    assert not worker._cloud_reextract(led, rows[0])
    jobs, _, blocked = worker._prepare_jobs(led, rows, ("", "gpt-6-luna"), False, allow_cloud=False)
    assert not blocked and len(jobs) == 1
    row, context, _, hints, qc = jobs[0]
    assert qc and qc["src"] == source_id and qc["qc"] == qc_id
    prior = worker._prior_identity(worker._prior_extract(led.db, row))
    selection, errors = cloud_policy.resolve_profile(cfg, route="reextract", purpose="backlog")
    assert not errors and selection is not None
    row["_provider_contract"] = worker._cloud_contract(selection=selection)
    row["_target"] = ("", selection["model"])
    row["_proof_vital_policy"] = None
    lease = worker._claim(led, row)
    assert lease is not None
    saved = worker._capture_checkpoint_state(led.db, row, context)

    sends = []
    real_client = worker.openai_client.OpenAIClient
    class Client(real_client):
        def request(self, method, request, timeout, **_kwargs):
            assert timeout > 0 and not led.db.in_transaction
            sends.append((request["model"], request["reasoning"]["effort"], request["input"]))
            answer = {"symptoms": [{"text": text, "status": "ongoing", "subject": "patient", "evidence": text}
                                   for text in ("発熱", "咳")]}
            if request["reasoning"]["effort"] == "high":
                answer["vitals"] = {"sbp": 120, "dbp": 80}
            return 200, {}, json.dumps({"status": "completed", "model": self.model, "service_tier": "default",
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": json.dumps(answer, ensure_ascii=False)}]}],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3,
                          "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}}).encode()
    monkeypatch.setattr(worker.openai_client, "OpenAIClient", Client)
    deadline = time.monotonic() + 600
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1, message_id=1,
        unit_key="1", deadline=deadline, current=lambda: worker._owns_current_source(led, row, lease),
        dispatch=lambda fn, **_kwargs: fn(), owner={"lease": lease}, selection=selection)
    chunks, meta, states = {}, {}, []

    def checkpoint(key, value):
        accepted = worker._persist_chunks(led, row, {key: value}, context, chunk_size=row["_chunk_size"],
            splits=row["_splits"], lease=lease, guard=row["_checkpoint_guard"])
        if accepted:
            states.append(value.get("_quality_status") or "completed")
            if isinstance(value, worker._EvidenceLeaf):
                identity = row["_checkpoint_identity"].get(key)
                value.quality_started_id = identity[0] if identity is not None else None
        return accepted

    result = worker.llm_extract(body, context=context, hints=hints, deadline=deadline, meta_out=meta,
        posted_at=row["posted_at"], chunks_in=saved, chunks_out=chunks, feedback=qc["notes"],
        quality_qc_src=source_id, on_chunk=checkpoint, chunk_size=row["_chunk_size"], provider=provider)
    assert isinstance(result, dict)
    assert states == ["pending", "started", "completed"]
    assert [(model, effort) for model, effort, _ in sends] == [
        ("gpt-6-luna", "medium"), ("gpt-6.1-sol", "high")]
    assert "頭痛" in sends[0][2] and "裏付けがありません" in sends[0][2]
    row["_provider_evidence"] = meta.pop("_provider_evidence")
    result["_model"] = selection["model"]
    outcome = worker._commit_extraction_atomic(led, row, json.dumps(result, ensure_ascii=False), context,
        meta, {"qc": qc_id, "applied": True}, None, tuple(chunks), check_current=False,
        expected_prior=prior)
    assert outcome == "written"
    assert not led.artifacts("extract_llm_chunk", message_id=1)
    assert led.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (source_id,)).fetchone() is None
    [final] = _current_artifacts(led)
    content, final_meta = json.loads(final["content"]), json.loads(final["meta"])
    proof = content["__mcs_internal_proof_v1"]
    assert proof["qc"]["source_id"] == source_id and proof["qc"]["audit_id"] == qc_id
    assert "unrelated" not in proof["qc"]["audit_content"]
    assert "unused" not in proof["qc"]["audit_content"]["items"][0]
    message = led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    assert worker.progress_for_message(led.db, message, model=selection["model"])["state"] == "complete"
    assert worker.progress_for_message(led.db, message, model=selection["model"],
        expected_artifact_id=final["artifact_id"])["state"] == "complete"
    assert worker.progress_for_message(led.db, message, model=selection["model"],
        expected_artifact_id=final["artifact_id"] + 1000)["state"] == "attention"

    with led.db:
        if damage == "qc_feedback":
            proof["qc"]["audit_content"]["items"][0]["item"] = "別の合成項目"
            final_meta["internal_proof_hash"] = mcs_util.payload_hash(proof)
            led.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                (json.dumps(content, ensure_ascii=False), json.dumps(final_meta, ensure_ascii=False), final["artifact_id"]))
        elif damage == "occurrence":
            led.db.execute("UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1")
        elif damage == "null_marker":
            content["__mcs_internal_proof_v1"] = None
            final_meta.pop("internal_proof_version", None)
            final_meta.pop("internal_proof_hash", None)
            led.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                (json.dumps(content, ensure_ascii=False), json.dumps(final_meta, ensure_ascii=False), final["artifact_id"]))
        elif damage == "bool_version":
            final_meta["internal_proof_version"] = True
            led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                (json.dumps(final_meta, ensure_ascii=False), final["artifact_id"]))
    message = led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    assert worker.progress_for_message(led.db, message, model=selection["model"])["state"] == (
        "complete" if damage == "none" else "attention")
    if damage in ("null_marker", "bool_version"):
        import processing_route
        assert processing_route.provider_states(led.db, 1, 1, cfg=cfg)["openai"] == "unknown"
    if damage == "none":
        import processing_route
        assert processing_route.provider_states(led.db, 1, 1, cfg=cfg)["openai"] == "done"
        with led.db:
            led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                           "VALUES(?,NULL,?,?,?,?,?)", ("extract_llm", 1, final["content"],
                           final["model"], final["meta"], final["created_at"]))
        assert worker.progress_for_message(led.db, message, model=selection["model"],
            expected_artifact_id=final["artifact_id"])["state"] == "attention"
        assert processing_route.provider_states(led.db, 1, 1, cfg=cfg)["openai"] == "unknown"
    assert len(sends) == 2


def _repair_run(routed, monkeypatch, tmp_path, repair_vitals):  # noqa: F811
    """Shared partial-repair run: medium pass leaves vitals gaps, the cloud
    repair improves it and returns with evidence, then stops."""
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    cfg["cloud_llm"]["provider"] = "openai"
    monkeypatch.setattr(worker, "HOME", str(tmp_path))
    monkeypatch.setattr(cloud_policy, "provider_key", lambda *_args: "synthetic-key")
    monkeypatch.setattr(worker.cloud_backlog, "request_slot", lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, "owner_alive", lambda: True)

    body = "本人に発熱と咳があります。本人の血圧120/80です。"
    led.save_messages([_message(1, body, project_id=1)])
    source_id = _extract_artifact(led, 1, {"symptoms": [{"text": "頭痛", "status": "ongoing",
        "subject": "patient", "evidence": "頭痛"}]}, _hash(led))
    qc_id = _qc_artifact(led, 1, source_id, _hash(led), {"qc": "done", "items": [
        {"section": "symptoms", "verdict": "NO_MATCH", "item": {"text": "頭痛"}}]})
    with led.db:
        led.db.execute("BEGIN IMMEDIATE")
        cloud_policy.capture_history_tx(led.db, "synthetic-qc-replay", time.time())

    rows, _ = worker._select_pending(led, 1, None, None, False)
    jobs, _, blocked = worker._prepare_jobs(led, rows, ("", "gpt-6-luna"), False, allow_cloud=False)
    assert not blocked and len(jobs) == 1
    row, context, _, hints, qc = jobs[0]
    prior = worker._prior_identity(worker._prior_extract(led.db, row))
    selection, errors = cloud_policy.resolve_profile(cfg, route="reextract", purpose="backlog")
    assert not errors and selection is not None
    row["_provider_contract"] = worker._cloud_contract(selection=selection)
    row["_target"] = ("", selection["model"])
    row["_proof_vital_policy"] = None
    lease = worker._claim(led, row)
    saved = worker._capture_checkpoint_state(led.db, row, context)

    sends = []
    real_client = worker.openai_client.OpenAIClient
    class Client(real_client):
        def request(self, method, request, timeout, **_kwargs):
            sends.append(request["model"])
            answer = {"symptoms": [{"text": text, "status": "ongoing", "subject": "patient", "evidence": text}
                                   for text in ("発熱", "咳")]}
            if request["reasoning"]["effort"] == "high":
                answer["vitals"] = dict(repair_vitals)
            return 200, {}, json.dumps({"status": "completed", "model": self.model, "service_tier": "default",
                "output": [{"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": json.dumps(answer, ensure_ascii=False)}]}],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3,
                          "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}}).encode()
    monkeypatch.setattr(worker.openai_client, "OpenAIClient", Client)
    deadline = time.monotonic() + 600
    provider = worker._CloudProvider(led, cfg, route="reextract", project_id=1, message_id=1,
        unit_key="1", deadline=deadline, current=lambda: worker._owns_current_source(led, row, lease),
        dispatch=lambda fn, **_kwargs: fn(), owner={"lease": lease}, selection=selection)
    chunks, meta = {}, {}

    def checkpoint(key, value):
        accepted = worker._persist_chunks(led, row, {key: value}, context, chunk_size=row["_chunk_size"],
            splits=row["_splits"], lease=lease, guard=row["_checkpoint_guard"])
        if accepted and isinstance(value, worker._EvidenceLeaf):
            identity = row["_checkpoint_identity"].get(key)
            value.quality_started_id = identity[0] if identity is not None else None
        return accepted

    result = worker.llm_extract(body, context=context, hints=hints, deadline=deadline, meta_out=meta,
        posted_at=row["posted_at"], chunks_in=saved, chunks_out=chunks, feedback=qc["notes"],
        quality_qc_src=source_id, on_chunk=checkpoint, chunk_size=row["_chunk_size"], provider=provider)
    assert isinstance(result, dict) and len(sends) == 2
    row["_provider_evidence"] = meta.pop("_provider_evidence")
    result["_model"] = selection["model"]
    return led, row, context, result, meta, qc_id, prior, chunks, sends


def test_partial_repair_still_commits_as_terminal_unresolved(routed, monkeypatch, tmp_path):  # noqa: F811
    """A won repair that leaves hint gaps is a legal terminal state: the commit
    must not be rejected, progress stays attention, and nothing reselects."""
    led, row, context, result, meta, qc_id, prior, chunks, sends = _repair_run(
        routed, monkeypatch, tmp_path, {"sbp": 120})
    assert any(entry.get("state") == "unresolved" for entry in meta["completeness"].values())
    outcome = worker._commit_extraction_atomic(led, row, json.dumps(result, ensure_ascii=False), context,
        meta, {"qc": qc_id, "applied": True}, None, tuple(chunks), check_current=False,
        expected_prior=prior)
    assert outcome == "written"
    assert not led.artifacts("extract_llm_chunk", message_id=1)
    message = led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    assert worker.progress_for_message(led.db, message, model=result["_model"])["state"] == "attention"
    rows, _ = worker._select_pending(led, 1, None, None, False)
    assert not rows and len(sends) == 2


@pytest.mark.parametrize("damage", ["state_resolved", "remaining_wrong", "extra_key",
                                    "chunks_wrong", "missing_leaf"])
def test_final_integrity_rejects_tampered_completeness(routed, monkeypatch, tmp_path, damage):  # noqa: F811
    """The committed completeness claim is bound to the adopted leaves'
    content: altering a single record — or the leaf count — rejects the
    write, and unresolved leaves can never mint a resolved claim."""
    led, row, context, result, meta, qc_id, prior, chunks, sends = _repair_run(
        routed, monkeypatch, tmp_path, {"sbp": 120})
    completeness = meta["completeness"]
    if damage == "chunks_wrong":
        meta["completeness_chunks"] = len(completeness) + 1
    elif damage == "missing_leaf":
        completeness.pop(next(iter(completeness)))
    else:
        key = next(k for k, v in completeness.items() if v["state"] == "unresolved")
        if damage == "state_resolved":
            completeness[key] = {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}
        elif damage == "remaining_wrong":
            completeness[key]["remaining_hint_candidates"] = 0
        elif damage == "extra_key":
            completeness[key] = {**completeness[key], "injected": True}
    outcome = worker._commit_extraction_atomic(led, row, json.dumps(result, ensure_ascii=False), context,
        meta, {"qc": qc_id, "applied": True}, None, tuple(chunks), check_current=False,
        expected_prior=prior)
    assert outcome == "rejected"
    # nothing replaced the prior artifact — the rejected write left it current
    assert [json.loads(row["content"])["symptoms"][0]["text"]
            for row in _current_artifacts(led)] == ["頭痛"]


def test_dispatch_unknown_history_leaf_commits_and_stays_attention(tmp_path, monkeypatch):
    """A leaf whose optional repair dispatch was interrupted keeps the
    dispatch-unknown execution history as a legal terminal record — the
    integrity claim binds to the persisted content, progress stays
    attention, nothing reselects, and the history is never promoted."""
    import copy
    from extract_testkit import _ledger

    db = _ledger(tmp_path)
    body = "本人に発熱と咳があります。本人の血圧120/80です。"
    db.save_messages([_message(body=body)])
    observations = {"symptoms": [
        {"text": "発熱", "status": "ongoing", "subject": "patient", "evidence": "発熱"},
        {"text": "咳", "status": "ongoing", "subject": "patient", "evidence": "咳"}]}
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise KeyboardInterrupt("synthetic interruption after optional dispatch reservation")
        return copy.deepcopy(observations)

    monkeypatch.setattr(worker, "_llm_call", model)
    try:
        with pytest.raises(KeyboardInterrupt):
            worker.run_pending(db, budget_s=300)
        result = worker.run_pending(db, budget_s=300)
        assert result["done"] == 1 and len(calls) == 2
        [row] = db.artifacts("extract_llm")
        integrity = json.loads(row["meta"])["integrity"]
        assert {entry["state"] for entry in integrity["completeness"].values()} == {
            "dispatch_unknown"}
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert worker.progress_for_message(db.db, source, model=worker.MODEL)["state"] == "attention"
        assert worker.run_pending(db, budget_s=300)["selected"] == 0 and len(calls) == 2
    finally:
        db.close()


@pytest.mark.parametrize("damage", ["state_resolved", "remaining_wrong", "state_bogus",
                                    "remaining_bool", "extra_key", "state_pending"])
def test_inline_proof_rejects_tampered_terminal_records(routed, monkeypatch, tmp_path, damage):  # noqa: F811
    """The unresolved record must reproduce from the saved piece: flipped
    states, wrong counts, type deviations and resumption markers all fail."""
    led, row, context, result, meta, qc_id, prior, chunks, sends = _repair_run(
        routed, monkeypatch, tmp_path, {"sbp": 120})
    with led.db:
        led.db.execute("BEGIN IMMEDIATE")
        proof = worker._extract_inline_proof(led.db, row, context, tuple(chunks))
    document = json.loads(json.dumps(result, ensure_ascii=False))
    assert worker._verify_inline_extract_proof(led.db, row, context, document, proof)

    leaf = next(item for item in proof["rows"]
                if '"quality_status": "unresolved"' in item["meta"])
    damaged = json.loads(leaf["meta"])
    if damage == "state_resolved":
        damaged["quality_status"] = "hint_gaps_resolved"
        damaged["quality_remaining"] = 0
    elif damage == "remaining_wrong":
        damaged["quality_remaining"] = 0
    elif damage == "state_bogus":
        damaged["quality_status"] = "resolved"
    elif damage == "remaining_bool":
        damaged["quality_remaining"] = True
    elif damage == "state_pending":
        damaged["quality_status"] = "pending"
    leaf["meta"] = json.dumps(damaged, ensure_ascii=False)
    if damage == "extra_key":
        content = json.loads(leaf["content"])
        content["_quality_result"] = {"state": "unresolved", "remaining_hint_candidates": 1,
                                      "injected": True}
        leaf["content"] = json.dumps(content, ensure_ascii=False)
    assert not worker._verify_inline_extract_proof(led.db, row, context, document, proof)
