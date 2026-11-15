"""Source-only planning and current-bound private progress; synthetic inputs only."""
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
import extract_schema
import extract_validation
import json
from pathlib import Path

import pytest

import clinical_chunking
import extract_llm
import cloud_inference
import inference_dependency_loss
from inference_version import LEGACY_VERSION, VERSION_KEY
from mcs_util import text_chunks
from patient_context import CATEGORIES
from test_extraction_review import _message, db

__all__ = ["db"]


def _source_bound_row(db):
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(db.db, row["project_id"], row["message_id"])
    assert isinstance(row["_source_binding"], str)
    return row


def test_shared_plan_contract_and_dense_short_corpus():
    cases = json.loads((Path(__file__).resolve().parents[3] / "evaluation/semantic_completeness_cases.json").read_text())["cases"]
    case = next(case for case in cases if case["id"] == "dense-multi-drug-16plus")
    source = case["messages"][0]["body"]
    assert len(source) < 3000
    parts = clinical_chunking.plan_chunks(source)
    assert len(parts) > 1 and "".join(parts) == source
    assert all(len(part) <= 3000 for part in parts)
    assert clinical_chunking.PLAN_VERSION == 2


@pytest.mark.parametrize("source", ["", " \n\t", "未知の自由記載です。", "Ａ\n家族：\n服薬なし。\n", "😀" * 3020])
@pytest.mark.parametrize("size", [1, 50, 3000])
def test_every_character_is_covered_once(source, size):
    parts = clinical_chunking.plan_chunks(source, size)
    assert "".join(parts) == source and all(len(part) <= size for part in parts)


@pytest.mark.parametrize("source", [
    "合成薬Aを1錠服用しています。",
    "処方薬：合成薬A\n用法：1日2回、朝夕\n対応：継続",
    "本人\n処方薬：合成薬A\n用法：1日2回\n対応：継続",
    "症状：合成の痛み\n対応：合成診療所へ相談\n転帰：未確認",
    "この自由文は候補語を含まずに保存すべき詳細を伝えています。",
])
def test_small_related_units_remain_one_call(source):
    assert clinical_chunking.plan_chunks(source) == [source]


def test_dense_comma_drug_list_never_cuts_names_from_doses():
    drugs = [f"合成薬{letter}{i + 10}mg朝夕1錠" for i, letter in enumerate("ABCDEFGHIJKLMN")]
    source = "母の過去の処方：" + "、".join(drugs) + "を中止。"
    parts = clinical_chunking.plan_chunks(source)
    assert len(parts) > 1 and "".join(parts) == source
    for drug in drugs:
        assert any(drug in part for part in parts)
    assert "を中止。" in parts[-1]


@pytest.mark.parametrize("gap", ["", " \t", "　", "\n"])
def test_dense_item_scanning_preserves_neighbours_and_every_character(gap):
    drugs = [f"合成薬A{i}mg" for i in range(1, 101)]
    source = ("、" + gap).join(drugs) + "、朝1日2回、用量: 1錠、　"
    parts = clinical_chunking.plan_chunks(source)
    assert "".join(parts) == source
    assert all(any(drug in part for part in parts) for drug in drugs)
    assert all(len(part) <= 3000 for part in parts)
    assert "朝1日2回、用量: 1錠、　" in parts[-1]


def test_low_density_inference_stays_single_and_reference_cannot_supply_evidence(monkeypatch):
    calls = []
    source = "前の文は家族の薬でした。\n本人の記載は独居です。"
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", 22)
    monkeypatch.setattr(extract_llm_context, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_engine, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_prepare, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_proofs, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_store, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    def infer(prompt, **kwargs):
        core = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        calls.append(core)
        if "独居" in core:
            return {"symptoms": [], "patient_context": [{"category": "medication_management", "subject": "family",
                    "text": "家族の薬", "evidence": "前の文は家族の薬でした。"}]}
        return {"symptoms": []}
    monkeypatch.setattr(extract_llm, "_llm_call", infer)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    out = extract_llm.llm_extract(source)
    assert out is None or not out.get("patient_context")
    assert len(calls) > 1
    calls.clear()
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", 3000)
    monkeypatch.setattr(extract_llm_context, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_engine, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_prepare, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_proofs, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_store, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm, "_llm_call", lambda prompt, **kwargs: calls.append("独居です。") or {"symptoms": []})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    assert extract_llm.llm_extract("独居です。") == {"symptoms": []}
    assert calls == ["独居です。"]


def test_actual_model_is_pinned_through_calls_and_final_stamp(db, monkeypatch):
    body = "独居です。"
    db.save_messages([_message(body=body)])
    current = ["synthetic-model-a"]
    monkeypatch.setattr(extract_llm, "load_config", lambda: {"local_llm": {"model": current[0]}})
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {"local_llm": {"model": current[0]}})
    calls = []
    def infer(prompt, **kwargs):
        calls.append(extract_llm._resolved_llm()[1])
        current[0] = "synthetic-model-b"
        return {"symptoms": [], "patient_context": [{"category": "living", "text": "独居です", "evidence": "独居です", "subject": "patient"}]}
    monkeypatch.setattr(extract_llm, "_llm_call", infer)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    assert extract_llm.run_pending(db, limit=1, budget_s=30)["done"] == 1
    final = db.db.execute("SELECT model,meta FROM artifacts WHERE kind='extract_llm'").fetchone()
    assert final["model"] == "synthetic-model-a"
    assert json.loads(final["meta"])["actual_model"] == "synthetic-model-a"
    row = db.db.execute("SELECT * FROM messages").fetchone()
    assert extract_llm.progress_for_message(db.db, row, model="synthetic-model-a")["state"] == "complete"
    assert extract_llm.progress_for_message(db.db, row, model="synthetic-model-b")["state"] == "attention"
    assert calls == ["synthetic-model-a"]


def test_progress_reads_only_bound_empty_checkpoints_and_final_binding(db, monkeypatch):
    body = "【家族】\n合成の長い記載です。" + "A" * 200
    db.save_messages([_message(body=body)])
    row = _source_bound_row(db)
    row["_target"] = ("", "synthetic")
    row["_context"] = extract_llm._thread_context(db.db, row)
    parts = clinical_chunking.plan_chunks(body, 100)
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", 100)
    monkeypatch.setattr(extract_llm_context, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_engine, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_prepare, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_proofs, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_store, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    assert extract_llm._persist_chunks(db, row, {0: {}}, row["_context"], chunk_size=100)
    db.db.execute("PRAGMA query_only=ON")
    before = db.db.total_changes
    result = extract_llm.progress_for_message(db.db, row, model="synthetic")
    assert result == {"state": "processing", "completed": 1, "total": len(parts), "backend": "legacy"}
    assert db.db.total_changes == before
    assert extract_llm.progress_for_message(db.db, row, model=None)["state"] == "attention"
    assert extract_llm.progress_for_message(db.db, row, model="other")["completed"] == 0
    monkeypatch.setattr(extract_llm, "PLAN_VERSION", 999)
    monkeypatch.setattr(extract_llm_context, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
    monkeypatch.setattr(extract_llm_errors, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
    monkeypatch.setattr(extract_llm_progress, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
    monkeypatch.setattr(extract_llm_proofs, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
    monkeypatch.setattr(extract_llm_store, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
    assert extract_llm.progress_for_message(db.db, row, model="synthetic")["completed"] == 0


def test_planner_source_digest_and_schema_preserve_safety_fields():
    assert len(extract_llm._LOADED_SOURCE_DIGESTS["clinical_chunking"]) == 64
    props = extract_schema.SCHEMA["schema"]["properties"]
    assert props["meds"]["items"]["properties"]["negated"]["type"] == "boolean"
    assert set(props["patient_context"]["items"]["properties"]["category"]["enum"]) == CATEGORIES
    assert extract_llm.MAX_TOKENS == 1600 and extract_llm.TIMEOUT == 300


def test_diagnostic_scoped_revalidation():
    body = "\n".join(f"独居の完全合成詳細{i:02d}です。" + "合成補足" * 5 for i in range(12))
    piece = text_chunks(body, 160)[0]
    doc = {"patient_context": [{"category": "living", "text": piece, "evidence": piece, "subject": "patient"}]}
    scoped = extract_validation.validate(doc, piece)
    global_result = extract_validation.validate(scoped, body)
    assert global_result is not None, (len(piece), scoped)


def test_every_real_adapter_call_uses_pinned_model_during_config_edit(monkeypatch):
    body = "A" * 90 + "B" * 90
    current = ["synthetic-a"]
    models = []
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", 90)
    monkeypatch.setattr(extract_llm_context, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_engine, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_prepare, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_proofs, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm_store, '_CHUNK_SIZE', extract_llm._CHUNK_SIZE)
    monkeypatch.setattr(extract_llm, "load_config", lambda: {"local_llm": {"model": current[0]}})
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {"local_llm": {"model": current[0]}})
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_choose_slot", lambda **kwargs: 0)
    monkeypatch.setattr(extract_llm_provider, '_choose_slot', extract_llm._choose_slot)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    def chat(prompt, **kwargs):
        models.append(kwargs["model"])
        current[0] = "synthetic-b"
        return {"status": 200, "text": '{"symptoms":[]}', "finish_reason": "stop"}
    monkeypatch.setattr(extract_llm.local_llm, "chat", chat)
    assert extract_llm.llm_extract(body) is not None
    assert models == ["synthetic-a", "synthetic-a"]
    assert extract_llm._resolved_llm()[1] == "synthetic-b"  # scope exited, no global override


def test_dense_planning_bypasses_batch_even_when_source_is_short(db, monkeypatch):
    cases = json.loads((Path(__file__).resolve().parents[3] / "evaluation/semantic_completeness_cases.json").read_text())["cases"]
    source = next(case for case in cases if case["id"] == "dense-multi-drug-16plus")["messages"][0]["body"]
    for mid in (1, 2):
        db.save_messages([_message(mid=mid, body=source)])
    calls = []
    repairs = []
    def infer(prompt, **kwargs):
        assert "items" not in kwargs.get("schema", {}).get("schema", {}).get("properties", {})
        calls.append(prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0])
        repairs.append("未収載" in prompt)
        return {"symptoms": []}
    monkeypatch.setattr(extract_llm, "_llm_call", infer)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    pieces = clinical_chunking.plan_chunks(source)
    first = extract_llm.run_pending(db, limit=3, budget_s=200, batch_k=2)
    # Optional quality work may wait, but every mandatory first candidate
    # must progress under the same budget, then be reused on the next pass.
    assert repairs.count(False) == 2 * len(pieces)
    assert first["done"] + first["deferred"] == 2
    second = extract_llm.run_pending(db, limit=3, budget_s=200, batch_k=2)
    assert first["done"] + second["done"] == 2
    assert repairs.count(False) == 2 * len(pieces)
    assert 0 < repairs.count(True) <= 2 * len(pieces)
    assert all(calls.count(piece) <= 4 for piece in pieces)


def test_pending_thin_confirmation_is_attention_even_with_bound_final(db):
    body = "未知の合成記載" * 50
    db.save_messages([_message(body=body)])
    row = _source_bound_row(db)
    row["_target"] = ("", "synthetic")
    row["_context"] = extract_llm._thread_context(db.db, row)
    assert extract_llm._replace_current(db, row, '{"summary":"合成概要"}', integrity={"thin": True})
    result = extract_llm.progress_for_message(db.db, row, model="synthetic")
    assert result["state"] == "attention"


def test_shared_predicate_and_family_intro_survive_as_reference_only():
    source = "母の過去の処方：" + "、".join(f"合成薬{letter}{i + 10}mg朝夕1錠" for i, letter in enumerate("ABCDEFGHIJKLMN")) + "を中止。"
    parts = clinical_chunking.plan_chunks(source)
    assert len(parts) > 1
    offset = len(parts[0])
    reference = extract_llm._piece_reference(source, offset, len(parts[1]))
    assert "母の過去の処方" in reference and "を中止" in reference
    doc = {"patient_context": [{"category": "medication_management", "subject": "family",
                               "text": "母の過去の処方", "evidence": source}]}
    assert extract_validation.validate_piece(doc, parts[1], source, ctx=True) is None


def test_compact_hints_omit_empty_optional_keys_but_keep_false_semantics():
    hints = {"v": 1, "medications": [{"name": "合成薬A", "negated": False}],
             "events": [], "vitals": {}, "summary": "", "labs": None}
    block = extract_llm._hint_block(hints)
    assert '"v"' not in block and '"events"' not in block and '"labs"' not in block
    assert '"negated":false' in block


def test_previous_plan_checkpoints_are_retained_but_not_reused(db, monkeypatch):
    db.save_messages([_message(body="独居です。")])
    row = _source_bound_row(db)
    row["_target"] = ("", "synthetic")
    with monkeypatch.context() as old:
        old.setattr(extract_llm, "PLAN_VERSION", 1)
        old.setattr(extract_llm_context, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
        old.setattr(extract_llm_errors, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
        old.setattr(extract_llm_progress, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
        old.setattr(extract_llm_proofs, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
        old.setattr(extract_llm_store, 'PLAN_VERSION', extract_llm.PLAN_VERSION)
        assert extract_llm._persist_chunks(db, row, {0: {}}, chunk_size=100)
        assert extract_llm._saved_chunk_state(db.db, row, chunk_size=100) == ({}, {0: {}})
    assert extract_llm.PLAN_VERSION == clinical_chunking.PLAN_VERSION == 2
    assert extract_llm._saved_chunk_state(db.db, row, chunk_size=100) == ({}, {})
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 1
    assert extract_llm._persist_chunks(db, row, {0: {}}, chunk_size=100)
    assert extract_llm._saved_chunk_state(db.db, row, chunk_size=100) == ({}, {0: {}})


@pytest.mark.parametrize("generation", ["missing", "legacy"])
def test_old_generation_checkpoint_is_retained_but_never_reused(db, generation):
    db.save_messages([_message(body="独居です。")])
    row = _source_bound_row(db)
    row["_target"] = ("", "synthetic")
    assert extract_llm._persist_chunks(db, row, {0: {}}, chunk_size=100)
    assert extract_llm._saved_chunk_state(db.db, row, chunk_size=100) == ({}, {0: {}})
    artifact = db.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()
    meta = json.loads(artifact["meta"])
    if generation == "missing":
        del meta[VERSION_KEY]
    else:
        meta[VERSION_KEY] = LEGACY_VERSION
    db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), artifact["artifact_id"]))
    assert extract_llm._saved_chunk_state(db.db, row, chunk_size=100) == ({}, {})
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 1


def test_missing_or_aba_source_binding_cannot_write_or_reuse_checkpoint(db):
    body = "本人の合成記録です。"
    db.save_messages([_message(body=body)])
    row = _source_bound_row(db)
    row["_target"] = ("", "synthetic")
    without_binding = {key: value for key, value in row.items() if key != "_source_binding"}
    assert not extract_llm._persist_chunks(db, without_binding, {0: {}}, chunk_size=100)
    assert not extract_llm._replace_current(db, without_binding, '{}')
    assert extract_llm._persist_chunks(db, row, {0: {}}, chunk_size=100)
    assert extract_llm._saved_chunk_state(db.db, row, chunk_size=100) == ({}, {0: {}})
    db.save_messages([_message(body="本人の別の合成記録です。")])
    db.save_messages([_message(body=body)])
    fresh = _source_bound_row(db)
    fresh["_target"] = ("", "synthetic")
    assert fresh["content_hash"] == row["content_hash"]
    assert fresh["_source_binding"] != row["_source_binding"]
    assert not extract_llm._persist_chunks(db, row, {0: {}}, chunk_size=100)
    assert not extract_llm._replace_current(db, row, '{}')
    assert extract_llm._saved_chunk_state(db.db, fresh, chunk_size=100) == ({}, {})
    assert extract_llm._persist_chunks(db, fresh, {0: {}}, chunk_size=100)
    assert extract_llm._saved_chunk_state(db.db, fresh, chunk_size=100) == ({}, {0: {}})
