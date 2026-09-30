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


def _request_rows(joined):
    return [ln for ln in joined.split("\n") if ln.startswith("依頼")]


def test_unverified_requests_render_apart_from_confirmed(db):
    joined = _render(db, {"requests": [
        {"to": "医師", "action": "状態確認"},
        {"to": "不明", "action": "採血の検討", "unverified": True},
        {"to": "訪問看護", "action": "処置変更", "due": "2026-10-02",
         "unverified": True}]})
    assert _request_rows(joined) == [
        "依頼: 医師へ状態確認",
        "依頼候補（未確認）: 採血の検討 / 訪問看護へ処置変更(期限:2026-10-02)"]


def test_unverified_only_requests_never_render_as_confirmed(db):
    """Only-unverified LLM requests do not fall back to rule requests
    either: the selected facts decided the request is uncertain."""
    joined = _render(db, {"requests": [
        {"to": "不明", "action": "往診しない", "unverified": True}]},
        {"requests": [{"kind": "doctor", "ctx": "往診依頼の文脈"}]})
    assert _request_rows(joined) == ["依頼候補（未確認）: 往診しない"]


def test_confirmed_requests_render_unchanged(db):
    joined = _render(db, {"requests": [
        {"to": "医師", "action": "a", "unverified": False},
        {"to": "医師", "action": "b"}, {"to": "医師", "action": "c"},
        {"to": "医師", "action": "d"}]})
    assert _request_rows(joined) == ["依頼: 医師へa / 医師へb / 医師へc"]


def test_request_kind_prefix_due_text_and_condition(db):
    """#20 order 3: self_plan/question get a per-item prefix inside the
    same 依頼: line, a relative deadline shows through due_text when due
    is null, and a located condition is appended in full (M2: never cut
    to 20).  Unverified items keep going to 依頼候補（未確認）."""
    joined = _render(db, {"requests": [
        {"to": "不明", "from": "ケアマネ", "kind": "self_plan",
         "action": "訪問して状況確認", "due": None, "due_text": "明日まで"},
        {"to": "家族", "kind": "question", "action": "デイ利用希望の確認"},
        {"to": "看護師", "kind": "request", "action": "医師へ連絡",
         "condition": "血圧が160を超えるようなら翌朝までに必ず"},
        {"to": "医師", "kind": "self_plan", "action": "再診",
         "unverified": True}]})
    assert _request_rows(joined) == [
        "依頼: 予定:ケアマネ→訪問して状況確認(期限:明日まで) / "
        "確認依頼:家族へデイ利用希望の確認 / "
        "看護師へ医師へ連絡(条件:血圧が160を超えるようなら翌朝までに必ず)",
        "依頼候補（未確認）: 予定:医師へ再診"]
    # a long condition (stored cap 60) stays whole; the action gives way
    # first and a cut is marked with …
    cond = "あ" * 60
    joined = _render(db, {"requests": [
        {"to": "医師", "action": "い" * 30, "condition": cond}]})
    assert _request_rows(joined) == [
        f"依頼: 医師へ{'い' * 9}…(条件:{cond})"]
    joined = _render(db, {"requests": [
        {"to": "医師", "action": "い" * 31, "condition": "う" * 20}]})
    assert _request_rows(joined) == [
        f"依頼: 医師へ{'い' * 29}…(条件:{'う' * 20})"]
    # foreign/legacy artifacts: an unhashable kind and a non-string due
    # neither abort the card nor hide a renderable due_text
    joined = _render(db, {"requests": [
        {"to": "医師", "action": "確認", "kind": ["self_plan"],
         "due": 7, "due_text": "明日まで"}]})
    assert _request_rows(joined) == ["依頼: 医師へ確認(期限:明日まで)"]


def test_malformed_unverified_flag_fails_closed(db):
    """A non-bool flag (string, number, null) is never shown as confirmed;
    items missing both to/action are dropped as before."""
    joined = _render(db, {"requests": [
        {"action": "文字列", "unverified": "false"},
        {"action": "数値", "unverified": 0},
        {"action": "null", "unverified": None},
        {"action": "c4", "unverified": "yes"},
        {"from": "家族", "unverified": True}, "not-a-dict"]})
    assert _request_rows(joined) == [
        "依頼候補（未確認）: 文字列 / 数値 / null"]


def test_llm_event_exclusions_are_not_overridden(db):
    """Rule hits cannot resurrect events omitted by the selected facts."""
    joined = _render(db, {"events": ["visit"]},
                     {"events": ["visit", "eol", "medication"]})
    assert "訪問" in joined and "看取り" not in joined and "投薬" not in joined


def test_vitals_preserve_selected_source(db):
    """A partial selected reading cannot import unrelated rule values."""
    joined = _render(db, {"vitals": {"hr": 88}},
                     {"vitals": {"sbp": 128, "dbp": 76, "bt": 36.8}})
    assert "HR 88" in joined and "BP" not in joined and "BT" not in joined


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


@pytest.mark.parametrize("kind", ["extract_v1", "extract_llm",
                                 "canonical_projection", "semantic_facts_v4"])
def test_foreign_project_artifact_cannot_supply_structured_facts(db, kind):
    db.artifact_add(kind, json.dumps({"summary": "別患者の合成情報",
                                     "medications": [{"name": "別患者薬"}]}),
                    project_id=2, message_id=1,
                    meta={"hash": "synthetic-hash", "engine_version": 4})
    assert structured_view.structured_lines(db.db, 1) == []


def test_error_rule_artifact_cannot_supply_structured_facts(db):
    db.artifact_add("extract_v1", json.dumps({"symptoms": ["合成症状"]}),
                    project_id=1, message_id=1,
                    meta={"hash": "synthetic-hash", "error": "synthetic failure"})
    assert structured_view.structured_lines(db.db, 1) == []


def test_canonical_finding_with_missing_id_does_not_break_render(db):
    _fact_artifact(db, "canonical_projection", {"canonical_facts": [
        {"kind": "preference", "statement": "不正な合成行"},
        {"fact_id": "f1", "kind": "preference", "statement": "有効な合成行"}]})
    text = "\n".join(structured_view.structured_lines(db.db, 1))
    assert "不正な合成行" not in text
    assert "有効な合成行" in text


def test_legacy_artifact_without_project_uses_message_scope(db):
    db.artifact_add("extract_llm", json.dumps({"summary": "旧形式の合成結果"}),
                    message_id=1, meta={"hash": "synthetic-hash"})
    assert structured_view.structured_lines(db.db, 1) == ["要約: 旧形式の合成結果"]
