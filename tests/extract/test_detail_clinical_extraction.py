"""Synthetic full-detail and bounded dense-output extraction checks."""
import json

import pytest

import extract_llm
from patient_context import DETAIL_KEYS
from test_extraction_review import _message, db

__all__ = ["db"]


def test_unrecoverable_length_stops_shrink_input_and_keep_retry_cap(db, monkeypatch):
    body = "服薬状況：" + "完全合成の詳細記載" * 80
    db.save_messages([_message(body=body)])
    seen = []
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm, "_choose_slot", lambda **kw: 0)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)

    def length(prompt, **kw):
        seen.append(prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0])
        assert kw["max_tokens"] == 1600
        return {"status": 200, "text": '{"summary":"合成の途中出力"}',
                "finish_reason": "length"}

    monkeypatch.setattr(extract_llm.local_llm, "chat", length)
    for expected in range(1, 6):
        result = extract_llm.run_pending(db, limit=1, budget_s=200)
        assert result["failed"] == 1 and result["done"] == 0
        row = db.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='extract_llm'").fetchone()
        meta = json.loads(row["meta"])
        assert meta["error"] and meta["attempts"] == expected
        meta["next_try"] = 0
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), row["artifact_id"]))
        db.db.commit()
    assert extract_llm.run_pending(db, limit=1, budget_s=200)["selected"] == 0
    assert seen[0] == body and len(set(seen)) > 1
    row = db.db.execute("SELECT * FROM messages").fetchone()
    splits, saved = extract_llm._saved_chunk_state(db, row)
    assert 1 in splits.values() and saved == {}



def _context(body, category="medication_management", details=None):
    return {"category": category, "text": body, "evidence": body,
            "subject": "patient", **({"details": details} if details is not None else {})}


@pytest.mark.parametrize("key", sorted(DETAIL_KEYS))
def test_all_detail_keys_preserve_raw_value_and_nested_source(key):
    value = "疑い・未確認" if key == "certainty" else f"合成原文{key}"
    body = f"合成の記載 {key}：{value}。"
    detail = {"key": key, "value": value, "evidence": body}
    source = _context(body, details=[detail])
    out = extract_llm._validate({"patient_context": [source]}, body)
    assert out["patient_context"] == [source]
    merged = extract_llm._merge([out, out])
    assert merged["patient_context"] == [source]
    assert not extract_llm._improves({"patient_context": [_context(body)]}, out)


@pytest.mark.parametrize("bad", [
    {"key": "unknown"}, {"key": []}, {"value": 5}, {"value": "確定"},
    {"evidence": "別の合成記載"}, {"evidence": []},
])
def test_invalid_detail_does_not_turn_uncertainty_into_a_fact(bad):
    body = "合成薬Aとの関連は疑いです。別の合成記載です。"
    parent = "合成薬Aとの関連は疑いです。"
    detail = {"key": "certainty", "value": "疑い", "evidence": parent}
    source = _context(parent, "adverse_events", [detail, {**detail, **bad}])
    drops = {}
    out = extract_llm._validate({"patient_context": [source]}, body, drops)
    assert out["patient_context"][0]["details"] == [detail]
    assert out["_items_dropped"] == 1
    assert "patient_context.details" in drops["items"]


def test_detail_evidence_must_stay_inside_parent_and_be_unique():
    body = "合成薬Aが被疑薬です。症状は未確認です。症状は未確認です。"
    parent = "合成薬Aが被疑薬です。"
    source = _context(parent, "adverse_events", [
        {"key": "certainty", "value": "未確認", "evidence": "症状は未確認です。"}])
    out = extract_llm._validate({"patient_context": [source]}, body)
    assert out["patient_context"][0]["details"] == []


def test_context_detail_enrichment_is_a_nonregressing_repair_and_merge():
    body = "合成薬Aの残薬は3錠で、飲み忘れは未確認です。"
    first = {"key": "residual_quantity", "value": "3錠", "evidence": body}
    second = {"key": "missed_doses", "value": "未確認", "evidence": body}
    prior = extract_llm._validate({"patient_context": [_context(body, details=[first])]}, body)
    better = extract_llm._validate({"patient_context": [_context(body, details=[first, second])]}, body)
    assert extract_llm._improves(better, prior)
    merged = extract_llm._merge([prior, better])
    assert merged["patient_context"] == better["patient_context"]
    assert prior["patient_context"][0]["details"] == [first]


@pytest.mark.parametrize(("name", "value", "unit", "analyte"), [
    ("体重", 48.5, "kg", "weight"), ("身長", 157, "cm", "height"),
    ("Cr", 1.2, "mg/dL", "creatinine"), ("eGFR", 50, "mL/min/1.73m2", "egfr"),
    ("AST", 28, "U/L", "ast"), ("ALT", 30, "U/L", "alt"),
    ("血糖", 110, "mg/dL", "blood_glucose"), ("HbA1c", 6.3, "%", "hba1c"),
])
def test_observation_date_unit_and_condition_stay_source_bound(name, value, unit, analyte):
    body = f"2026年10月7日08時30分の空腹時測定：{name} {value}{unit}。"
    raw = {"name": name, "value": value, "unit": unit, "evidence": body,
           "subject": "patient", "status": "past",
           "measured_on": "2026年10月7日08時30分", "condition": "空腹時"}
    lab = extract_llm._validate({"labs": [raw]}, body)["labs"][0]
    assert lab["measured_on"] == raw["measured_on"] and lab["condition"] == "空腹時"
    assert lab["normalized"]["analyte"] == analyte
    assert lab["normalized"]["confirmation"] == "quote_supported"
    assert lab["normalized"]["measured_on"] == "2026-10-07"
    assert lab["normalized"]["reported_measured_on"] == raw["measured_on"]
    assert lab["normalized"]["status"] == "past"


@pytest.mark.parametrize(("subject", "status"), [("family", "current"), ("other", "past"),
                                                  ("patient", "planned")])
def test_family_other_and_planned_measurements_cannot_be_confirmed_patient_values(subject, status):
    body = "2026年10月7日測定の体重48.5kg。"
    lab = extract_llm._validate({"labs": [{"name": "体重", "value": 48.5, "unit": "kg",
                                         "subject": subject, "status": status, "evidence": body}]}, body)["labs"][0]
    assert lab["subject"] == subject and lab["status"] == status
    assert lab["unverified"] and lab["normalized"]["confirmation"] == "unverified"
    assert lab["normalized"]["measured_on"] is None


def test_missing_sampling_fields_are_never_filled_from_posting_time():
    body = "Cr 1.2mg/dL。"
    lab = extract_llm._validate({"labs": [{"name": "Cr", "value": 1.2, "unit": "mg/dL",
                                         "evidence": body, "measured_on": "2026年10月7日",
                                         "condition": "空腹時"}]}, body)["labs"][0]
    assert "measured_on" not in lab and "condition" not in lab
    assert lab["unverified"] and lab["normalized"]["measured_on"] is None


def _fake_backend(monkeypatch, reply):
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")
    monkeypatch.setattr(extract_llm, "_choose_slot", lambda **kw: 0)
    monkeypatch.setattr(extract_llm.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: True)

    def chat(prompt, **kwargs):
        assert kwargs["max_tokens"] == 1600
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        return reply(piece)

    monkeypatch.setattr(extract_llm.local_llm, "chat", chat)


def test_actual_length_stop_splits_only_when_needed_and_preserves_full_coverage(db, monkeypatch):
    body = "\n".join(f"独居の完全合成詳細{i:02d}です。" + f"合成補足{i:02d}" * 5 for i in range(12))
    db.save_messages([_message(body=body)])
    seen = []

    def reply(piece):
        seen.append(piece)
        if len(piece) > 180:
            return {"status": 200, "text": '{"summary":"途中"}', "finish_reason": "length"}
        return {"status": 200, "text": json.dumps({"patient_context": [_context(piece, "living")]}),
                "finish_reason": "stop"}

    _fake_backend(monkeypatch, reply)
    result = extract_llm.run_pending(db, limit=1, budget_s=200)
    assert result["done"] == 1 and result["failed"] == 0
    artifact = db.db.execute("SELECT content,meta FROM artifacts WHERE kind='extract_llm'").fetchone()
    content, meta = json.loads(artifact[0]), json.loads(artifact[1])
    def compact(text):
        return "".join(text.split())
    assert compact("".join(entry["text"] for entry in content["patient_context"])) == compact(body)
    assert meta["extract_version"] == 6 and meta["patient_context_version"] == 2
    assert meta["integrity"]["length_stops"] >= 1
    assert meta["integrity"]["chunk_size"] < extract_llm._CHUNK_SIZE
    assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 0


def test_adaptive_layout_and_complete_chunks_survive_later_interruption(db, monkeypatch):
    body = "\n".join(f"独居の合成行{i:02d}です。" + "合成補足" * 5 for i in range(10))
    db.save_messages([_message(body=body)])
    seen, completed = [], []
    aborted = [False]

    def reply(piece):
        seen.append(piece)
        if piece == body:
            return {"status": 200, "text": '{}', "finish_reason": "length"}
        if completed and not aborted[0]:
            aborted[0] = True
            raise RuntimeError("synthetic interrupted generation")
        completed.append(piece)
        return {"status": 200, "text": json.dumps({"patient_context": [_context(piece, "living")]}),
                "finish_reason": "stop"}

    _fake_backend(monkeypatch, reply)
    with pytest.raises(RuntimeError, match="synthetic interrupted"):
        extract_llm.run_pending(db, limit=1, budget_s=200)
    row = db.db.execute("SELECT * FROM messages").fetchone()
    size = extract_llm._saved_chunk_size(db, row)
    splits, saved = extract_llm._saved_chunk_state(db, row, chunk_size=size)
    assert size == extract_llm._CHUNK_SIZE and splits and saved
    assert not db.artifacts("extract_llm")
    assert db.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0
    first = completed[0]
    assert extract_llm.run_pending(db, limit=1, budget_s=200)["done"] == 1
    assert seen.count(body) == 1 and seen.count(first) == 1


def test_old_generation_valid_failed_and_checkpointed_rows_reenter_normal_pending(db, monkeypatch):
    for mid in (1, 2):
        db.save_messages([_message(mid=mid, body=f"独居の合成記載{mid}です。")])
        row = db.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
        meta = {"hash": row["content_hash"], "extract_version": 5, "patient_context_version": 1}
        if mid == 2:
            meta.update(error=1, attempts=5, auto_retry=3, next_try=10**12)
        db.artifact_add("extract_llm", json.dumps({"summary": "旧合成結果"}),
                        project_id=1, message_id=mid, meta=meta)
        db.artifact_add("extract_llm_chunk", json.dumps({"summary": "旧合成checkpoint"}),
                        project_id=1, message_id=mid, meta={"hash": row["content_hash"],
                        "ver": 5, "chunk_size": 3000, "chunk": 0,
                        "context": extract_llm._chunk_context(row, None)})
    calls = []
    def infer(body, **kwargs):
        calls.append(body)
        assert kwargs["chunks_in"] == {}
        return {"patient_context": [_context(body, "living")]}
    monkeypatch.setattr(extract_llm, "llm_extract", infer)
    result = extract_llm.run_pending(db, limit=3, budget_s=200)
    assert result["done"] == 2 and len(calls) == 2
    assert all(json.loads(row[0])["extract_version"] == 6 for row in db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_llm'"))


def test_detail_grammar_uses_shared_whitelist_and_keeps_token_budget():
    item = extract_llm._SCHEMA["schema"]["properties"]["patient_context"]["items"]
    assert set(item["properties"]["details"]["items"]["properties"]["key"]["enum"]) == DETAIL_KEYS
    assert extract_llm._BATCH_ITEM["properties"]["patient_context"]["items"] == item
    assert extract_llm.MAX_TOKENS == 1600



def test_five_information_groups_and_provenance_survive_inference_and_storage(db, monkeypatch):
    groups = [
        ("medication_management", "合成薬Aは他院処方で実際は朝1錠、残薬は4錠、飲み忘れは未確認。", {
            "drug_name": "合成薬A", "medication_kind": "他院処方", "actual_dose": "1錠",
            "actual_frequency": "朝", "residual_quantity": "4錠", "missed_doses": "未確認"}),
        ("adverse_events", "合成薬Aの関連は疑いで、発疹は10月7日から。対応は合成診療所へ確認中、転帰は未確認。", {
            "drug_name": "合成薬A", "symptom": "発疹", "onset": "10月7日", "response": "合成診療所へ確認中",
            "outcome": "未確認", "certainty": "疑い"}),
        ("observations", "観測日は2026年10月7日、同日の空腹時採血でCr 1.2mg/dL。", {
            "observation_name": "Cr", "value": "1.2", "unit": "mg/dL", "measured_on": "2026年10月7日",
            "condition": "空腹時採血", "observed_on": "2026年10月7日"}),
        ("medication_management", "嚥下に難しさがあり、自己管理は困難。合成家族が一包化で支援し、変更後は飲み忘れが減った。", {
            "swallowing": "嚥下に難しさがあり", "self_management": "困難", "supporter": "合成家族",
            "support_method": "一包化", "outcome": "飲み忘れが減った"}),
        ("followup", "合成看護師が10月9日までに残薬を再確認。最終確認日は10月6日で、確認者は合成薬剤師、確認状態は未確認。", {
            "followup": "残薬を再確認", "assignee": "合成看護師", "due_text": "10月9日",
            "last_confirmed_on": "10月6日", "confirmed_by": "合成薬剤師", "certainty": "未確認"}),
    ]
    body = "\n".join(text for _, text, _ in groups)
    source = {"patient_context": [
        _context(text, category, [{"key": key, "value": value, "evidence": text}
                                  for key, value in fields.items()])
        for category, text, fields in groups]}
    db.save_messages([_message(body=body)])
    def infer(prompt, **kwargs):
        core = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        return {"patient_context": [item for item in source["patient_context"] if item["evidence"] in core]}
    monkeypatch.setattr(extract_llm, "_llm_call", infer)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kwargs: True)
    assert extract_llm.run_pending(db, limit=1, budget_s=200)["done"] == 1
    saved = json.loads(db.db.execute("SELECT content FROM artifacts WHERE kind='extract_llm'").fetchone()[0])
    assert saved["patient_context"] == source["patient_context"]
    assert len(saved["patient_context"]) == 5
    assert all(entry["evidence"] in body and detail["evidence"] in entry["evidence"]
               for entry in saved["patient_context"] for detail in entry["details"])


def test_parallel_dense_rows_keep_independent_source_bound_layouts(db, monkeypatch):
    bodies = {mid: "\n".join(f"独居の完全合成行{mid}-{i:02d}です。" + "合成補足" * 3
                             for i in range(8)) for mid in (1, 2)}
    for mid, body in bodies.items():
        db.save_messages([_message(mid=mid, body=body)])

    def reply(piece):
        if len(piece) > 130:
            return {"status": 200, "text": '{}', "finish_reason": "length"}
        return {"status": 200, "text": json.dumps({"patient_context": [_context(piece, "living")]}),
                "finish_reason": "stop"}

    _fake_backend(monkeypatch, reply)
    result = extract_llm.run_pending(db, limit=3, budget_s=200, workers=2)
    assert result["done"] == 2 and result["failed"] == 0
    for row in db.db.execute("SELECT message_id,content FROM artifacts WHERE kind='extract_llm'"):
        entries = json.loads(row["content"])["patient_context"]
        assert "".join("".join(item["text"].split()) for item in entries) == "".join(bodies[row["message_id"]].split())
    assert not db.artifacts("extract_llm_chunk")


@pytest.mark.parametrize("bad", [{"ver": 5}, {"context": "f" * 64}, {"hash": "f" * 64},
                                  {"chunk_size": 0}, {"chunk_size": True}, {"chunk": -1.0}])
def test_adaptive_layout_cannot_cross_generation_context_or_shape(db, bad):
    db.save_messages([_message(body="独居です。")])
    row = db.db.execute("SELECT * FROM messages").fetchone()
    meta = {"hash": row["content_hash"], "ver": 6, "context": extract_llm._chunk_context(row, None),
            "chunk": -1, "chunk_size": 10, **bad}
    db.artifact_add("extract_llm_chunk", '{}', project_id=1, message_id=1, meta=meta)
    assert extract_llm._saved_chunk_size(db, row) == extract_llm._CHUNK_SIZE


@pytest.mark.parametrize("cached_empty", [False, True])
def test_late_nested_split_does_not_repeat_completed_original_chunk(db, monkeypatch, cached_empty):
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", 120)
    body = "初回完了の合成区間" + "A" * 110 + "後半密な合成区間" + "B" * 170
    db.save_messages([_message(body=body)])
    row = db.db.execute("SELECT * FROM messages").fetchone()
    roots = extract_llm.text_chunks(body, 120)
    if cached_empty:
        extract_llm._persist_chunks(db, row, {0: {}})
        assert extract_llm._saved_chunks(db, row)[0] == {}
    seen = []
    target = roots[1]
    def reply(piece):
        seen.append(piece)
        if piece == target or (piece != roots[0] and "B" in piece and len(piece) > 30):
            return {"status": 200, "text": '{}', "finish_reason": "length"}
        return {"status": 200, "text": '{"summary":"合成の部分結果","urgency":"routine"}',
                "finish_reason": "stop"}
    _fake_backend(monkeypatch, reply)
    result = extract_llm.run_pending(db, limit=1, budget_s=200)
    assert result["done"] == 1 and result["failed"] == 0
    assert seen.count(roots[0]) == (0 if cached_empty else 1)
    assert seen.count(target) == 1
    assert len([piece for piece in seen if "B" in piece]) > 2


def test_piece_hints_and_repair_hints_cannot_include_sibling_details(monkeypatch):
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", 70)
    body = "親前半の処方確認です。" + "A" * 65 + "後半の残薬確認です。" + "B" * 55
    prompts = []
    def rules(row):
        return {"v": 1, "patient_context": [_context(row["body_text"], "medication_management")]}
    monkeypatch.setattr(extract_llm, "_rule_hints", rules)
    repaired = set()
    def infer(prompt, **kwargs):
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        prompts.append(prompt)
        other = next(part for part in extract_llm.text_chunks(body, 70) if part != piece)
        # Each supplied rule/context field belongs to this target piece only.
        hint_prefix = prompt.split("<<<\n", 1)[0]
        assert other not in hint_prefix
        if piece not in repaired:
            repaired.add(piece)
            return {"labs": [{"name": "合成項目", "value": "x", "evidence": "ない引用"}]}
        return {"summary": "合成修復結果"}
    monkeypatch.setattr(extract_llm, "_llm_call", infer)
    out = extract_llm.llm_extract(body, hints=rules({"body_text": body}))
    assert out is not None and len(prompts) >= 4


def test_child_of_initial_single_chunk_never_gets_whole_body_thin_repair(monkeypatch):
    body = "独居の完全合成記載です。" + "C" * 330
    seen = []
    def reply(piece):
        seen.append(piece)
        if piece == body:
            return {"status": 200, "text": '{}', "finish_reason": "length"}
        return {"status": 200, "text": '{"summary":"合成部分概要"}', "finish_reason": "stop"}
    _fake_backend(monkeypatch, reply)
    out = extract_llm.llm_extract(body)
    assert out is not None
    assert len(seen) == 1 + len(extract_llm.text_chunks(body, len(body) // 2))


def test_repair_length_stop_splits_pending_piece_and_preserves_completed_sibling(db, monkeypatch):
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", 100)
    body = "先に完了する合成区間" + "D" * 90 + "修復が密な合成区間" + "E" * 75
    db.save_messages([_message(body=body)])
    roots = extract_llm.text_chunks(body, 100)
    calls = []
    def reply(piece):
        calls.append(piece)
        if piece == roots[1] and calls.count(piece) == 1:
            return {"status": 200, "text": json.dumps({"labs": [{"name": "合成項目", "value": "x",
                                                                    "evidence": "ない引用"}]}),
                    "finish_reason": "stop"}
        if piece == roots[1]:
            return {"status": 200, "text": '{}', "finish_reason": "length"}
        return {"status": 200, "text": '{"summary":"合成部分結果"}', "finish_reason": "stop"}
    _fake_backend(monkeypatch, reply)
    assert extract_llm.run_pending(db, limit=1, budget_s=200)["done"] == 1
    assert calls.count(roots[0]) == 1 and calls.count(roots[1]) == 2


def test_newest_broken_leaf_suppresses_older_completion(db):
    body = "合成本文です。"
    db.save_messages([_message(body=body)])
    row = db.db.execute("SELECT * FROM messages").fetchone()
    extract_llm._persist_chunks(db, row, {0: {}})
    old = db.db.execute("SELECT meta FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    db.artifact_add("extract_llm_chunk", '{broken', project_id=1, message_id=1, meta=json.loads(old))
    assert extract_llm._saved_chunks(db, row) == {}


@pytest.mark.parametrize("fault", [{"split_size": True}, {"split_size": 0}, {"split_size": 99999},
                                   {"chunk": "0:-1"}, {"chunk": "00:1"}, {"chunk": []},
                                   {"piece_sha256": "f" * 64}])
def test_adversarial_local_split_records_cannot_authorize_cached_children(db, fault):
    body = "完全合成区間です。" * 10
    db.save_messages([_message(body=body)])
    row = db.db.execute("SELECT * FROM messages").fetchone()
    size = len(body) // 2
    extract_llm._persist_chunks(db, row, {0: {}}, split_size=size)
    splits = {0: size}
    extract_llm._persist_chunks(db, row, {"0:0": {}}, splits=splits)
    meta = json.loads(db.db.execute("SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id LIMIT 1").fetchone()[0])
    meta.update(fault)
    db.artifact_add("extract_llm_chunk", '{}', project_id=1, message_id=1, meta=meta)
    loaded_splits, saved = extract_llm._saved_chunk_state(db, row)
    if isinstance(fault.get("chunk"), (str, list)):
        # A different malformed node cannot replace a valid root's record.
        assert 0 in loaded_splits
    else:
        assert loaded_splits == {} and saved == {}


def test_processing_revision_invalidates_old_global_hints_checkpoint(db):
    import hashlib
    body = "独居の合成本文です。"
    db.save_messages([_message(body=body)])
    row = db.db.execute("SELECT * FROM messages").fetchone()
    old_context = hashlib.sha256(json.dumps([row["posted_at"], None, extract_llm.MODEL,
                                extract_llm._PROMPT_HEAD, extract_llm._SCHEMA],
                                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    db.artifact_add("extract_llm_chunk", '{}', project_id=1, message_id=1,
                    meta={"hash": row["content_hash"], "ver": 6, "chunk": 0,
                          "chunk_size": 3000, "context": old_context})
    assert old_context != extract_llm._chunk_context(row, None)
    assert extract_llm._saved_chunks(db, row) == {}



def test_root_split_requires_explicit_piece_hash_but_legacy_empty_root_remains_valid(db):
    body = "合成の本文です。" * 12
    db.save_messages([_message(body=body)])
    row = db.db.execute("SELECT * FROM messages").fetchone()
    # Legacy complete index 0 may have no piece hash; it still proves coverage.
    meta = {"hash": row["content_hash"], "ver": 6, "context": extract_llm._chunk_context(row, None),
            "chunk": 0, "chunk_size": 3000}
    db.artifact_add("extract_llm_chunk", '{}', project_id=1, message_id=1, meta=meta)
    assert extract_llm._saved_chunks(db, row) == {0: {}}
    # A newer split with no hash supersedes the root, but cannot prove a child plan.
    db.artifact_add("extract_llm_chunk", '{}', project_id=1, message_id=1,
                    meta={**meta, "split_size": 50})
    splits, saved = extract_llm._saved_chunk_state(db, row)
    assert splits == {} and saved == {}


def test_repair_length_attempt_is_counted_before_local_split(monkeypatch):
    body = "完全合成の詳細" * 40
    first = [True]
    def reply(piece):
        if piece == body and first[0]:
            first[0] = False
            return {"status": 200, "text": json.dumps({"labs": [{"name": "合成項目", "value": "x",
                                                                    "evidence": "ない引用"}]}),
                    "finish_reason": "stop"}
        if piece == body:
            return {"status": 200, "text": '{}', "finish_reason": "length"}
        return {"status": 200, "text": '{"summary":"合成の部分結果"}', "finish_reason": "stop"}
    _fake_backend(monkeypatch, reply)
    meta = {}
    assert extract_llm.llm_extract(body, meta_out=meta) is not None
    assert meta["repairs"] == 1 and meta["length_stops"] == 1
