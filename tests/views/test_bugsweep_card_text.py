"""Card summaries show every lab and whole quotes; the 💊 view labels dictionary state truthfully.
Synthetic ledger, fictional dictionary and stubbed sources only."""
import hashlib
import json

import pytest

import drug_map
import mcs_queries
import notify_views
import read_model
import structured_view
from medications_testkit import DOCUMENT
from notify_testkit import CFG, MED, _extract, _llm_extract, _msg, _patient, _seed_thread, led

__all__ = ["led"]


def _dictionary(tmp_path, led):
    raw = json.dumps(DOCUMENT, ensure_ascii=False).encode()
    path = tmp_path / "fictional-drug-map.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    drug_map.derive(led, drug_map.load(path, expected_sha256=sha))
    return {**CFG, "drug_map": {"path": str(path), "sha256": sha}}


def test_card_shows_every_lab_value():
    body = "。".join(f"L{i} {i}mg/dL" for i in range(9))
    labs = [{"name": f"L{i}", "value": i, "unit": "mg/dL", "evidence": f"L{i} {i}mg/dL"} for i in range(9)]
    text = "\n".join(structured_view._lab_lines({"labs": labs}, body))
    assert all(f"L{i} {i}mg/dL" in text for i in range(9))


def test_urgency_reason_is_not_cut_before_its_negating_tail(monkeypatch):
    reason = "昨夜から息苦しさがあり救急要請を検討したとのことだが現在は呼吸苦なしで落ち着いている"
    assert len(reason) > 40
    monkeypatch.setattr(structured_view.mcs_queries, "current_message_extract", lambda db, kind, mid: {})
    monkeypatch.setattr(structured_view.mcs_queries, "current_message_facts",
                        lambda db, mid: {"summary": "合成の要約"})
    monkeypatch.setattr(structured_view.clinical_urgency, "message_urgency_details",
                        lambda *a, **k: {"source": "llm", "reasons": [reason], "held": False, "kind": "clinical"})
    monkeypatch.setattr(structured_view, "urgency_qc_suffix", lambda db, mid: "")
    monkeypatch.setattr(structured_view, "_source_row", lambda db, mid: ((reason, "full", None, None), True))
    monkeypatch.setattr(structured_view, "_drug_refs", lambda db, mid: [])
    monkeypatch.setattr(structured_view, "_pharmacy_detail_lines", lambda db, mid: [])
    lines = structured_view.structured_lines(object(), 1)
    assert f"🚨 緊急度高 — 根拠:「{reason}」" in lines


@pytest.mark.parametrize("store", [_extract, _llm_extract], ids=["rules", "llm"])
def test_pharmacy_detail_quote_keeps_its_negating_tail(led, store):
    value = "本人は薬を冷蔵庫に移したと話しているが、訪問時に確認すると実際には冷蔵保管していない"
    assert len(value) > 40
    _patient(led, 1)
    _msg(led, 100, body=value)
    store(led, 100, {"patient_context": [{
        "category": "medication_management", "text": value, "evidence": value, "subject": "patient",
        "details": [{"key": "storage_place", "value": value, "evidence": value}]}]})
    assert structured_view._pharmacy_detail_lines(led.db, 100) == [f"服薬管理（保管場所）: 「{value}」"]


def test_pharmacy_detail_without_source_quote_is_not_shown(led, monkeypatch):
    _patient(led, 1)
    body = "以前は冷蔵庫で保管していました。"
    _msg(led, 100, body=body)
    item = {"category": "medication_management", "text": body, "evidence": body, "subject": "patient",
            "details": [{"key": "storage_place", "value": "冷蔵庫"}]}
    _extract(led, 100, {"patient_context": [item]})
    assert structured_view._pharmacy_detail_lines(led.db, 100) == []
    # A reader returning the old detail verbatim must still fail the same display check.
    monkeypatch.setattr(read_model, "message_patient_context", lambda db, row: [item])
    assert structured_view._pharmacy_detail_lines(led.db, 100) == []


def test_attachment_medication_matched_now_is_shown_as_current(led, tmp_path, monkeypatch):
    import attachment_extraction
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": []})
    cfg = {**_dictionary(tmp_path, led), "attachment_ocr": {"enabled": True}}
    body = "キラナ5mgを服用中"
    source = {"document": {"meds": [{**MED, "evidence": body}]}, "body_text": body, "attachment_id": 7,
              "page": 1, "extraction_artifact_id": 1, "source_key": "attachment_ocr:1:1"}
    monkeypatch.setattr(attachment_extraction, "iter_sources", lambda *a, **k: iter([source]))
    monkeypatch.setattr(mcs_queries, "attachment_ocr_coverage", lambda *a, **k: {"complete": True})
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = json.dumps(view, ensure_ascii=False)
    assert "候補: 架空成分甲" in text
    assert "次回の処理で反映" not in text
    assert any(note.startswith("辞書 fictional-v1@") for note in view["notes"])


def test_rule_fallback_medications_are_not_promised_a_next_run_match(led, tmp_path):
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": []})
    _extract(led, 101, {"medications": [{"name": "キラナ", "dose": "5mg"}],
                        "rx_actions": [{"action": "start", "ctx": "キラナ錠開始"}]})
    cfg = _dictionary(tmp_path, led)
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = json.dumps(view, ensure_ascii=False)
    assert "キラナ" in text and "開始:キラナ錠開始" in text
    assert "次回の処理で反映" not in text
    assert text.count("辞書照合の対象外") == 2


@pytest.mark.parametrize("llm", [{}, {"summary": "合成"}])
def test_rule_medication_marker_only_when_another_source_owns_annotations(llm):
    v1 = {"medications": [{"name": "キラナ", "dose": "5mg"}]}
    _, unverified = structured_view._med_entries(llm, v1, context=("キラナ5mg", None, "patient"))
    assert structured_view.dictionary_unmatched(unverified[0][1]) is bool(llm)
