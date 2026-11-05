"""Medication references are frozen, source-scoped, and never exported with cloud prompts."""
import extract_llm_context
import extract_llm_engine
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import time

import pytest

import drug_map
import extract_llm
import extraction_context
import extraction_medications as references
import inference_dependency_loss
from inference_version import CURRENT_VERSION, VERSION_KEY
from extract_testkit import _message
from medications_testkit import _dictionary, store

__all__ = ["store"]


def configured(store, tmp_path):
    dictionary = _dictionary(tmp_path)
    cfg = {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"),
                        "sha256": dictionary.sha256}}
    with store.db:
        drug_map._save_progress(store.db, {"dictionary": [dictionary.dict_id, dictionary.sha256,
                                                          False, drug_map.RESOLVER_VERSION], "cursor": 0})
    return cfg


def snapshot(store, tmp_path, body):
    cfg = configured(store, tmp_path)
    value = references.build(store.db, cfg, body, private=True, deadline=time.monotonic() + 10)
    assert references.valid(value, body)
    return cfg, value


def test_private_candidates_are_source_scoped_and_cloud_block_is_public(store, tmp_path):
    body = "キラナを服用。共通架空は未確認。"
    cfg, value = snapshot(store, tmp_path, body)
    assert references.current(store.db, cfg, value, body=body)
    local = references.block(value, body, 0, body, private=True)
    public = references.block(value, body, 0, body, private=False)
    assert "架空成分甲" in local and "架空成分甲" not in public
    assert "doseへ規格を転記しない" in local and "doseへ規格を転記しない" in public
    child = references.block(value, body, len("キラナを服用。"), "共通架空は未確認。", private=True)
    assert '"surface":"キラナ"' not in child
    crossing = references.block(value, body, 1, body[1:], private=True)
    assert '"surface":"キラナ"' not in crossing


def test_tampered_candidates_are_rejected_against_the_approved_dictionary(store, tmp_path):
    body = "キラナを服用。"
    cfg, value = snapshot(store, tmp_path, body)
    edited = deepcopy(value)
    edited["parts"][0]["hint"]["matches"][0]["candidates"][0]["display"] = "架空の別薬"
    assert references.valid(edited, body)  # Shape is not proof of dictionary provenance.
    assert not references.current(store.db, cfg, edited, body=body)
    changed = {"drug_map": {**cfg["drug_map"], "sha256": "0" * 64}}
    assert not references.current(store.db, changed, value, body=body)


def test_unapproved_dictionary_does_not_supply_candidates(store, tmp_path):
    cfg = configured(store, tmp_path)
    body = "キラナを服用。"
    with store.db:
        drug_map._save_progress(store.db, {"dictionary": ["different", "0" * 64,
                                                          False, drug_map.RESOLVER_VERSION], "cursor": 0})
    value = references.build(store.db, cfg, body, private=True, deadline=time.monotonic() + 10)
    assert references.valid(value, body) and value["status"] == "inactive"
    assert not references.has_private(value)
    assert "架空成分甲" not in references.block(value, body, 0, body, private=True)


def test_multiple_root_pieces_retain_exact_full_source_offsets(store, tmp_path):
    body = "本文。" * 1100 + "キラナを服用。"
    cfg, value = snapshot(store, tmp_path, body)
    assert len(value["parts"]) > 1 and references.current(store.db, cfg, value, body=body)
    assert references.block(value, body, 3300, body[3300:], private=True).count("架空成分甲") == 1
    changed = deepcopy(value)
    changed["parts"][-1]["start"] += 1
    assert not references.valid(changed, body)


def test_single_local_and_cloud_prompts_keep_the_same_source_rules(store, tmp_path, monkeypatch):
    body = "キラナを服用。"
    _, value = snapshot(store, tmp_path, body)
    prompts = []
    def answer(prompt, **kwargs):
        prompts.append(prompt)
        return {"symptoms": [], "meds": [{"name": "キラナ", "dose": None, "action": "none",
            "subject": "patient", "status": "current", "negated": False, "evidence": "キラナを服用"}]}
    monkeypatch.setattr(extract_llm, "_llm_call", answer)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    for provider in (None, object()):
        result = extract_llm.llm_extract(body, hints={}, medication_hints=value,
                                        medication_guard=lambda: True, provider=provider)
        assert result["meds"][0]["name"] == "キラナ"
    assert "架空成分甲" in prompts[0]
    assert "架空成分甲" not in prompts[-1] and "doseへ規格を転記しない" in prompts[-1]


def test_reference_change_stops_before_the_first_model_call(store, tmp_path, monkeypatch):
    body = "キラナを服用。"
    _, value = snapshot(store, tmp_path, body)
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *a, **k: pytest.fail("dispatched stale hints"))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    assert extract_llm.llm_extract(body, hints={}, medication_hints=value,
                                  medication_guard=lambda: False) is extract_llm._DEFERRED


def test_guard_rechecks_at_the_transport_after_waiting(monkeypatch):
    monkeypatch.setattr(extract_llm._CALL_NOTES, "medication_source_changed", False, raising=False)
    monkeypatch.setattr(extract_llm._CALL_NOTES, "medication_guard", lambda: False, raising=False)
    monkeypatch.setattr(extract_llm.local_llm, "bounded_request", lambda *a: pytest.fail("sent stale hints"))
    with pytest.raises(extract_llm.bounded_http.NotSentTimeout, match="medication_reference_changed"):
        extract_llm._opener_request("http://localhost", "POST", {}, 1)
    assert extract_llm._CALL_NOTES.medication_source_changed is True


def test_batch_reference_blocks_stay_with_their_target(store, tmp_path):
    body = "キラナを服用。"
    _, value = snapshot(store, tmp_path, body)
    other = "家族から連絡。"
    public = references.build(None, {}, other, private=False, deadline=time.monotonic() + 1)
    prompt = extract_llm._batch_prompt([(body, None, {}), (other, None, {})],
                                      medication_hints=[value, public])
    first, second = prompt.split("対象1だけの薬剤確認:")
    assert "架空成分甲" in first and "架空成分甲" not in second
    with pytest.raises(ValueError, match="medication_batch_scope_invalid"):
        extract_llm._batch_prompt([(body, None, {})], medication_hints=[])


def test_legacy_prompt_bytes_remain_unchanged_without_snapshot():
    body = "対象の本文"
    assert references.block(None, body, 0, body, private=True) == ""
    old = extract_llm._batch_prompt([(body, None, {})])
    assert old == extract_llm._batch_prompt([(body, None, {})], medication_hints=[None])
    assert "薬剤抽出の確認" not in old


def test_protocol_snapshot_freezes_without_dictionary_access():
    body = "対象の本文"
    value = references.build(None, {}, body, private=False, deadline=time.monotonic() + 1)
    assert references.valid(json.loads(json.dumps(value)), body)
    assert not references.valid(value, body + "変更")


def frozen_attempt(store, *, attempt=None):
    if store.db.execute("SELECT 1 FROM messages WHERE message_id=1").fetchone() is None:
        store.save_messages([_message(mid=1, project_id=1, body="完全合成の対象本文")])
    row = dict(store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row.update(_source_binding=inference_dependency_loss.source_binding(store.db, 1, 1),
               _target=("", "fictional-model"))
    attempt = {"auto_retry": 0} if attempt is None else attempt
    frame = extraction_context.prepare(store, row, model="fictional-model",
        contract=extract_llm._context_contract(source_digest=extract_llm._PRIOR_CONTEXT_SOURCE),
        attempt=attempt, enabled=False, base_context=None, version=extract_llm.EXTRACT_VERSION)
    return row, frame


def checkpoint(store, row, frame):
    meta = {"hash": row["content_hash"], "ver": extract_llm.EXTRACT_VERSION,
            VERSION_KEY: CURRENT_VERSION, "context": extract_llm._chunk_context(
                {**row, "_frame": frame}, frame["text"], checkpoint=True)}
    return store.artifact_add("extract_llm_chunk", '{"symptoms":[]}',
                              project_id=1, message_id=1, model="fictional-model", meta=meta)


def test_old_unfinished_attempt_is_selected_before_adding_new_dictionary_inputs(store):
    row, frame = frozen_attempt(store)
    checkpoint(store, row, frame)
    picked = extract_llm._pending_context_frame(store.db, row, attempt={"auto_retry": 0}, enabled=False)
    assert picked["artifact_id"] == frame["artifact_id"] and "medication_hints" not in picked


def test_other_retry_checkpoint_does_not_block_the_current_attempt(store):
    row, older = frozen_attempt(store, attempt={"auto_retry": 1})
    checkpoint(store, row, older)
    row, current = frozen_attempt(store)
    checkpoint(store, row, current)
    picked = extract_llm._pending_context_frame(store.db, row, attempt={"auto_retry": 0}, enabled=False)
    assert picked["artifact_id"] == current["artifact_id"]


def test_same_body_from_a_new_source_revision_does_not_reuse_old_input(store):
    row, old = frozen_attempt(store)
    checkpoint(store, row, old)
    store.save_messages([replace(_message(mid=1, project_id=1, body=row["body_text"]), sender_name="合成別投稿者")])
    current = dict(store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    current.update(_target=row["_target"],
                   _source_binding=inference_dependency_loss.source_binding(store.db, 1, 1))
    assert current["_source_binding"] != row["_source_binding"]
    assert extract_llm._pending_context_frame(store.db, current, attempt={"auto_retry": 0}, enabled=False) is None


def test_ambiguous_frozen_frames_hold_instead_of_selecting_an_older_copy(store):
    row, frame = frozen_attempt(store)
    checkpoint(store, row, frame)
    saved = store.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (frame["artifact_id"],)).fetchone()
    store.artifact_add(extraction_context.KIND, saved["content"], project_id=1, message_id=1,
                       model="fictional-model", meta=json.loads(saved["meta"]))
    with pytest.raises(ValueError, match="pending_context_unverifiable"):
        extract_llm._pending_context_frame(store.db, row, attempt={"auto_retry": 0}, enabled=False)


def test_unverifiable_checkpoint_cannot_be_treated_as_unsent(store):
    row, _ = frozen_attempt(store)
    store.artifact_add("extract_llm_chunk", '{"symptoms":[]}', project_id=1, message_id=1,
        model="fictional-model", meta={"hash": row["content_hash"], "ver": extract_llm.EXTRACT_VERSION,
                                       VERSION_KEY: CURRENT_VERSION, "context": "0" * 64})
    with pytest.raises(ValueError, match="pending_context_unverifiable"):
        extract_llm._pending_context_frame(store.db, row, attempt={"auto_retry": 0}, enabled=False)


def test_dictionary_read_error_is_a_safe_unsent_reference_change(store, tmp_path, monkeypatch):
    body = "キラナを服用。"
    cfg, value = snapshot(store, tmp_path, body)
    def broken(*args):
        raise sqlite3.OperationalError("synthetic lookup failure")
    monkeypatch.setattr(drug_map, "active_dictionary", broken)
    assert references.current(store.db, cfg, value, body=body) is False


def test_omitting_one_ambiguous_candidate_cannot_be_presented_as_a_single_match(store, tmp_path):
    body = "共通架空を服用。"
    cfg, value = snapshot(store, tmp_path, body)
    edited = deepcopy(value)
    row = edited["parts"][0]["hint"]["matches"][0]
    row["candidates"] = row["candidates"][:1]
    row["candidate_count"] = 1
    assert references.valid(edited, body)
    assert not references.current(store.db, cfg, edited, body=body)


def test_expired_run_does_not_prepare_a_new_dictionary_frame(store, monkeypatch):
    monkeypatch.setattr(extract_llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(references, "build", lambda *a, **k: pytest.fail("prepared after deadline"))
    jobs, skipped, blocked = extract_llm._prepare_jobs(store, [{}, {}], ("", "model"), False,
                                                       deadline=time.monotonic() - 1)
    assert jobs == [] and skipped == 0 and blocked == 2


def test_transport_reference_change_does_not_poison_endpoint_health(monkeypatch):
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **_: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm._CALL_NOTES, "medication_source_changed", False, raising=False)
    def changed(*args, **kwargs):
        extract_llm._CALL_NOTES.medication_source_changed = True
        kwargs["error_out"]["kind"] = "not_sent"
        return None
    monkeypatch.setattr(extract_llm.local_llm, "chat", changed)
    assert extract_llm._llm_call("synthetic prompt") is extract_llm._DEFERRED
    assert extract_llm._CALL_NOTES.transport is None
    assert extract_llm._CALL_NOTES.defer_reason == "currentness_unverified"
