"""Structured display preserves typed exclusions and source attribution."""
import json

import pytest

import ledger
import structured_view


@pytest.fixture
def db(tmp_path):
    store = ledger.Ledger(str(tmp_path / "ledger.db"))
    with store.db:
        store.db.execute(
            "INSERT INTO messages(message_id,project_id,body_state,content_hash) "
            "VALUES(1,1,'full','synthetic-hash')")
    yield store
    store.close()


def _render(db, llm, rules=None):
    for kind, content in (("extract_llm", llm), ("extract_v1", rules or {})):
        db.artifact_add(kind, json.dumps(content), project_id=1, message_id=1,
                        meta={"hash": "synthetic-hash"})
    return "\n".join(structured_view.structured_lines(db.db, 1))


def test_filters_and_labels(db):
    joined = _render(db, {
        "meds": [{"name": "家族薬", "subject": "family"},
                 {"name": "予定薬", "status": "planned"},
                 {"name": "現行薬", "action": "none"}],
        "events": ["transfer", "fall", "family_contact"],
        "requests": [{"to": "医師", "from": "家族",
                      "action": "状態確認", "due": "2026-10-01"}],
    })
    assert "家族薬" not in joined
    assert "予定薬[予定]" in joined
    assert "転院/移動" in joined and "転倒" in joined and "家族連絡" in joined
    assert "家族→医師へ状態確認(期限:2026-10-01)" in joined


def test_resolved_cancels_rule_positive(db):
    joined = _render(db, {"symptoms": [{"text": "発熱", "status": "resolved"}]},
                     {"symptoms": ["発熱"]})
    assert "発熱" not in joined


def test_rule_fallback_skipped_when_llm_excluded_medication(db):
    joined = _render(db, {"meds": [{"name": "家族薬", "subject": "family"}]},
                     {"medications": [{"name": "家族薬"}]})
    assert "家族薬" not in joined


def _fact_artifact(db, kind, content, chash="synthetic-hash"):
    db.artifact_add(kind, json.dumps(content), project_id=1, message_id=1,
                    meta={"hash": chash})


def test_current_projection_shadows_extract_llm(db):
    """T5: a hash-current canonical_projection owns the fact source —
    legacy extract_llm for the same message must not show through."""
    _fact_artifact(db, "extract_llm", {"meds": [{"name": "旧薬"}]})
    _fact_artifact(db, "canonical_projection",
                   {"meds": [{"name": "新薬", "action": "stop"}],
                    "canonical_facts": []})
    joined = "\n".join(structured_view.structured_lines(db.db, 1))
    assert "旧薬" not in joined
    assert "新薬" in joined


def test_stale_projection_falls_back_to_extract_llm(db):
    """T5 failure axis: revision changed -> the projection's hash no
    longer matches -> readers show the current extract_llm, never the
    stale canonical content."""
    _fact_artifact(db, "canonical_projection",
                   {"meds": [{"name": "陳腐薬"}]}, chash="outdated")
    _fact_artifact(db, "extract_llm", {"meds": [{"name": "現行薬"}]})
    joined = "\n".join(structured_view.structured_lines(db.db, 1))
    assert "陳腐薬" not in joined
    assert "現行薬" in joined


def test_canonical_only_categories_render_as_findings(db):
    """T5: allergy/adverse/vital/preference/observation facts have no
    legacy slot — they render from canonical_facts, never vanish."""
    content = {"canonical_facts": [
        {"fact_id": "f1", "kind": "allergy_intolerance",
         "statement": "ペニシリンアレルギー", "subject": "patient:1",
         "evidence_quote": "ペニシリン"},
        {"fact_id": "f2", "kind": "adverse_drug_event",
         "statement": "嘔気が出た", "subject": "patient:1",
         "evidence_quote": "嘔気"},
        {"fact_id": "f3", "kind": "vital_lab",
         "statement": "BP 120/80", "subject": "patient:1",
         "evidence_quote": "120/80"},
        {"fact_id": "f4", "kind": "preference",
         "statement": "午前の訪問希望", "subject": "patient:1",
         "evidence_quote": None},
        {"fact_id": "f5", "kind": "other_observation",
         "statement": "独居", "subject": "patient:1",
         "evidence_quote": None}]}
    _fact_artifact(db, "canonical_projection", content)
    joined = "\n".join(structured_view.structured_lines(db.db, 1))
    assert "アレルギー・不耐" in joined and "ペニシリンアレルギー" in joined
    assert "有害事象" in joined and "嘔気が出た" in joined
    assert "バイタル・検査" in joined and "BP 120/80" in joined
    assert "希望" in joined and "午前の訪問希望" in joined
    assert "独居" in joined


def test_canonical_facts_do_not_duplicate_legacy_slots(db):
    """Facts that already project into legacy slots (meds/symptoms/
    requests/events) must not repeat in the canonical findings line."""
    content = {
        "meds": [{"name": "現行薬", "action": "none"}],
        "canonical_facts": [
            {"fact_id": "f1", "kind": "medication_event",
             "statement": "現行薬を継続", "subject": "patient:1",
             "evidence_quote": "現行薬"},
            {"fact_id": "f2", "kind": "allergy_intolerance",
             "statement": "ペニシリンアレルギー", "subject": "patient:1",
             "evidence_quote": "ペニシリン"}]}
    _fact_artifact(db, "canonical_projection", content)
    joined = "\n".join(structured_view.structured_lines(db.db, 1))
    assert "現行薬を継続" not in joined      # shown via 薬剤 slot only
    assert "アレルギー・不耐｜ペニシリンアレルギー" in joined
