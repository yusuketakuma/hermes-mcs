"""Patient context stays verbatim, source-bound and preserved through local extraction."""
import extract_llm_context
import extract_llm_engine
import extract_llm_prompts
import extract_llm_runner
import extract_quality
import extract_schema
import extract_validation
import json

import pytest

import extract_llm
import inference_dependency_loss
import inference_version
from patient_context import CATEGORIES
from test_extraction_review import _message, db

__all__ = ["db"]


def item(category="living", text="独居です", subject="patient", evidence=None):
    return {"category": category, "text": text, "subject": subject,
            "evidence": text if evidence is None else evidence}


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_all_categories_require_verbatim_source_value(category):
    raw = item(category, text=f"合成記載{category}")
    body = f"対象本文に{raw['text']}と明記されています。"
    out = extract_validation.validate({"patient_context": [raw]}, body)
    assert out["patient_context"] == [raw]
    assert ("patient_context", json.dumps({key: value for key, value in raw.items()
                                         if key != "evidence"}, sort_keys=True,
                                        ensure_ascii=False)) in extract_quality.facts(out)


@pytest.mark.parametrize("bad", [
    {"category": "unknown"}, {"category": []}, {"category": {}},
    {"subject": "unspecified"}, {"subject": []}, {"subject": None},
    {"text": "自立です"}, {"text": ""}, {"text": 123},
    {"evidence": "参考だけの記載"}, {"evidence": None}, {"evidence": []},
])
def test_invalid_patient_context_drops_only_its_item(bad):
    valid = item()
    invalid = {**valid, **bad}
    drops = {}
    out = extract_validation.validate({"patient_context": [valid, invalid]}, "独居です。", drops)
    assert out["patient_context"] == [valid]
    assert out["_items_dropped"] == 1 and "patient_context" in drops["items"]
    assert "patient_context" in " ".join(extract_quality.repair_issues(drops, out))


@pytest.mark.parametrize(("body", "raw"), [
    ("独居です。独居です。", item()),
    ("独居です。要介護です。", item(text="要介護です", evidence="独居です")),
    (None, item()),
])
def test_missing_ambiguous_or_uncontained_source_never_survives(body, raw):
    out = extract_validation.validate({"patient_context": [raw], "summary": "合成概要"}, body)
    assert out["patient_context"] == []
    assert out["_items_dropped"] == 1 and out["_evidence_dropped"] == 1


def test_patient_context_schema_and_batch_grammar_share_contract():
    schema = extract_schema.SCHEMA["schema"]["properties"]["patient_context"]["items"]
    assert set(schema["required"]) == {"category", "text", "evidence", "subject"}
    assert set(schema["properties"]["category"]["enum"]) == CATEGORIES
    assert schema == extract_schema.BATCH_ITEM["properties"]["patient_context"]["items"]
    assert extract_llm._BATCH_HEAD.startswith(extract_llm._PROMPT_SPEC)
    assert "patient_context" in extract_llm._PROMPT_SPEC


def test_patient_context_chunk_merge_keeps_distinct_subjects_and_reports():
    first, changed, family = item(), item(text="同居です"), item(subject="family")
    merged = extract_validation.merge([{"patient_context": [first]},
                                {"patient_context": [first, changed, family]}])
    assert merged["patient_context"] == [first, changed, family]
    assert not extract_quality.improves({"summary": "合成概要"}, merged)
    assert extract_quality.improves(merged, {"patient_context": [first]})


def test_patient_context_llm_single_and_chunks_remain_source_bound(monkeypatch):
    first, second = item(), item("cognition", "会話は明瞭です")
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        {"symptoms": [], "patient_context": [first]})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    assert extract_llm.llm_extract("独居です。")["patient_context"] == [first]
    body = "独居です。" + "あ" * 3100 + "会話は明瞭です。"
    def infer(prompt, **kwargs):
        core = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        items = [item for item in (first, second) if item["evidence"] in core]
        return {"symptoms": [], **({"patient_context": items} if items else {})}
    monkeypatch.setattr(extract_llm, "_llm_call", infer)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    assert extract_llm.llm_extract(body)["patient_context"] == [first, second]
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        {"symptoms": [], "patient_context": [first]})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    lifted = extract_llm.llm_extract("本文は別の記載です。", context="独居です。")
    assert lifted is None


@pytest.mark.parametrize("generation", [inference_version.LEGACY_VERSION, inference_version.CURRENT_VERSION])
def test_new_detail_contract_marks_results_and_repends_existing_v5(db, generation):
    db.save_messages([_message(body="独居です。")])
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(db.db, row["project_id"], row["message_id"])
    old = {"hash": row["content_hash"], "extract_version": 5}
    if generation == inference_version.CURRENT_VERSION:
        old[inference_version.VERSION_KEY] = generation
    old_content = {"summary": "旧合成概要"}
    old_id = db.artifact_add("extract_llm", json.dumps(old_content), project_id=1, message_id=1, meta=old)
    assert extract_llm.EXTRACT_VERSION == inference_version.LLM_EXTRACT_VERSION
    assert not extract_llm._current(db, 1, row["content_hash"])
    content = {"patient_context": [item()]}
    assert extract_llm._replace_current(db, row, json.dumps(content))
    saved = db.artifacts("extract_llm", project_id=1, message_id=1)
    current = [entry for entry in saved if inference_version.is_current(json.loads(entry["meta"]))]
    assert len(current) == 1 and json.loads(current[0]["content"]) == content
    new_meta = json.loads(current[0]["meta"])
    assert new_meta["extract_version"] == extract_llm.EXTRACT_VERSION
    assert new_meta["patient_context_version"] == extract_llm.PATIENT_CONTEXT_VERSION == 4
    assert extract_llm._current(db, 1, row["content_hash"])
    prior = [entry for entry in saved if entry["artifact_id"] == old_id]
    if generation == inference_version.LEGACY_VERSION:
        assert len(prior) == 1 and json.loads(prior[0]["content"]) == old_content
        assert json.loads(prior[0]["meta"]) == old
    else:
        assert prior == []


def test_patient_context_prompt_change_invalidates_saved_chunk(db, monkeypatch):
    db.save_messages([_message(body="独居です。")])
    row = dict(db.db.execute("SELECT * FROM messages").fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(db.db, row["project_id"], row["message_id"])
    assert extract_llm._persist_chunks(db, row, {0: {"patient_context": [item()]}})
    assert extract_llm._saved_chunk_state(db.db, row)[1]
    monkeypatch.setattr(extract_llm, "_PROMPT_HEAD", extract_llm._PROMPT_HEAD + "契約変更")
    monkeypatch.setattr(extract_llm_context, '_PROMPT_HEAD', extract_llm._PROMPT_HEAD)
    monkeypatch.setattr(extract_llm_prompts, '_PROMPT_HEAD', extract_llm._PROMPT_HEAD)
    monkeypatch.setattr(extract_llm_runner, '_PROMPT_HEAD', extract_llm._PROMPT_HEAD)
    assert not extract_llm._saved_chunk_state(db.db, row)[1]


@pytest.mark.parametrize("body", ["独居です。", "既往があります。", "認知機能は良好です。",
                                  "食事は常食です。", "主病名の記載です。"])
def test_patient_context_narrative_cannot_be_prefiltered_as_no_signal(body):
    assert not extract_llm._low_signal(body, {"v": 1})


def test_incomplete_chunk_urgency_cannot_silence_lexical_net():
    absent = extract_validation.validate({"summary": "至急連絡の合成記載"}, "至急連絡")
    merged = extract_validation.merge([{"urgency": "routine"}, absent])
    assert merged.get("urgency") != "routine"
    assert extract_validation.merge([{"urgency": "routine"}, {"urgency": "routine"}])["urgency"] == "routine"
