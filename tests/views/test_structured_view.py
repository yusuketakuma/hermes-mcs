"""Structured display preserves typed exclusions and source attribution."""
import json

import pytest

import ledger
import structured_view
from semantic_projection import PROJECTION_VERSION


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
    # owner rule 2026-10-05: the 要約 lists every request, no count cap
    joined = _render(db, {"requests": [
        {"to": "医師", "action": "a", "unverified": False},
        {"to": "医師", "action": "b"}, {"to": "医師", "action": "c"},
        {"to": "医師", "action": "d"}]})
    assert _request_rows(joined) == ["依頼: 医師へa / 医師へb / 医師へc / 医師へd"]


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
    # owner rule 2026-10-05: action and condition are never cut
    cond = "あ" * 60
    joined = _render(db, {"requests": [
        {"to": "医師", "action": "い" * 30, "condition": cond}]})
    assert _request_rows(joined) == [
        f"依頼: 医師へ{'い' * 30}(条件:{cond})"]
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
        "依頼候補（未確認）: 文字列 / 数値 / null / c4"]


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
                    meta={"hash": chash, "projection_version": PROJECTION_VERSION})


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
    assert "アレルギー・不耐｜（対象:patient:1）ペニシリンアレルギー" in joined


@pytest.mark.parametrize("kind", ["canonical_projection", "semantic_facts_v4"])
@pytest.mark.parametrize("attrs,marker", [
    ({"polarity": "negated"}, "極性:negated"),
    ({"epistemic": "speculated"}, "確度:speculated"),
    ({"subject": "role:family"}, "対象:role:family"),
    ({"workflow_status": "planned"}, "状態:planned"),
    ({"polarity": "unknown", "epistemic": "unknown"}, "極性:unknown、確度:unknown"),
    ({"event_time": "2026-09-01"}, "時点:2026-09-01"),
])
def test_projected_findings_keep_qualifiers_before_full_text(db, kind, attrs, marker):
    """Evidence-verified findings preserve subject, negation, certainty and time."""
    from semantic_facts import validate_facts_doc
    from semantic_projection import project_v2_doc_legacy
    from semantic_testkit import v2_doc, v2_fact
    import notify_render

    quote = "合成記録の内容について関連資料と過去の確認記録を参照しながら補足説明を記入しました。末尾に限定条件。"
    statement = "合成所見" * 25
    source = {"message_id": "1", "revision": "synthetic-hash",
              "content_hash": "synthetic-hash", "body_codepoints": len(quote),
              "content_quality": "full", "attachments_complete": True,
              "source_fingerprint": "sf-synthetic"}
    atom = {"atom_id": "atom-a", "kind": "clause", "start": 0,
            "end": len(quote), "text_hash": "synthetic-atom-hash"}
    evidence = {"evidence_id": "ev-a", "message_id": "1",
                "revision": "synthetic-hash", "start": 0, "end": len(quote),
                "quote": quote, "atom_id": "atom-a"}
    fact = v2_fact("fact-a", kind="other_observation", statement=statement,
                   evidence_ids=["ev-a"], **attrs)
    doc = validate_facts_doc(v2_doc(
        [fact], [evidence], source=source, atoms=[atom],
        chunks=[{"chunk_id": "chunk-a", "core_atom_ids": ["atom-a"], "status": "done"}]))
    content = project_v2_doc_legacy(doc)
    db.artifact_add(kind, json.dumps(content), project_id=1, message_id=1,
                    meta={"hash": "synthetic-hash", "engine_version": 4,
                          "projection_version": PROJECTION_VERSION})
    text = "\n".join(structured_view.structured_lines(db.db, 1))
    assert marker in text and text.index(marker) < text.index(statement[:60])
    # owner rule 2026-10-05: the statement and its evidence are never cut
    assert statement in text and "末尾に限定条件" in text
    assert text in notify_render._structured_block(db.db, 1)["text"]


def test_legacy_canonical_finding_without_qualifiers_keeps_display(db):
    _fact_artifact(db, "canonical_projection", {"canonical_facts": [
        {"fact_id": "f1", "kind": "other_observation",
         "statement": "合成所見", "evidence_quote": "合成根拠"}]})
    assert structured_view.structured_lines(db.db, 1) == ["所見｜合成所見（根拠:合成根拠）"]


@pytest.mark.parametrize("value", [None, 7, False, [], {}, "", " "])
def test_malformed_canonical_qualifiers_preserve_readable_finding(db, value):
    fact = {"fact_id": "f1", "kind": "other_observation",
            "statement": "合成所見", "epistemic": "reported"}
    fact.update({key: value for key in ("subject", "polarity", "workflow_status",
                                      "event_time", "quantity", "action",
                                      "valid_time", "actor")})
    _fact_artifact(db, "canonical_projection", {"canonical_facts": [fact]})
    assert structured_view.structured_lines(db.db, 1) == [
        "所見｜（確度:reported）合成所見"]


@pytest.mark.parametrize("kind", ["extract_v1", "extract_llm",
                                 "canonical_projection", "semantic_facts_v4"])
def test_foreign_project_artifact_cannot_supply_structured_facts(db, kind):
    # Preserve the reader's negative assertion against malformed legacy data.
    db.db.execute("DROP TRIGGER g1_artifacts_msg_ins")
    db.artifact_add(kind, json.dumps({"summary": "別患者の合成情報",
                                     "medications": [{"name": "別患者薬"}]}),
                    project_id=2, message_id=1,
                    meta={"hash": "synthetic-hash", "engine_version": 4,
                          "projection_version": PROJECTION_VERSION})
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
    assert structured_view.structured_lines(db.db, 1) == ["旧形式の合成結果"]


@pytest.mark.parametrize("field", ["summary", "points", "events", "labs",
                                 "symptoms", "meds", "requests",
                                 "canonical_facts", "vital_flags"])
@pytest.mark.parametrize("value", [7, "wrong-shape", {"bad": 1}, None])
def test_malformed_selected_fields_keep_other_structured_content(db, field, value):
    content = {"summary": "合成要約", "points": ["合成要点"], field: value}
    joined = _render(db, content)
    expected = "合成要点" if field == "summary" else "合成要約"
    assert expected in joined


@pytest.mark.parametrize("value", [7, "wrong-shape", {"bad": 1}, None, [None]])
def test_unreadable_selected_fields_never_restore_rule_mentions(db, value):
    joined = _render(db, {"summary": "合成要約", "symptoms": value,
                          "meds": value, "requests": value}, {
        "symptoms": ["合成除外症状"],
        "medications": [{"name": "合成除外薬"}],
        "requests": [{"kind": "confirm", "ctx": "合成除外依頼"}]})
    assert joined == "合成要約"


def test_unhashable_labels_and_event_items_do_not_hide_valid_facts(db):
    with db.db:
        db.db.execute("UPDATE messages SET body_text='合成検査1' WHERE message_id=1")
    joined = _render(db, {
        "events": [{"bad": "visit"}, ["visit"], "visit"],
        "labs": [{"name": "合成検査", "value": 1, "flag": ["high"],
                  "evidence": "合成検査1"}],
        "symptoms": [{"text": "合成症状", "severity": ["severe"]}],
        "meds": [{"name": "合成薬", "action": ["start"], "route": ["oral"]}],
        "canonical_facts": [
            {"fact_id": "bad", "kind": ["preference"], "statement": "不正所見"},
            {"fact_id": "valid", "kind": "preference", "statement": "合成所見"}]})
    for text in ["区分: 訪問", "検査: 合成検査 1", "症状: 合成症状",
                 "薬剤: 合成薬", "希望｜合成所見"]:
        assert text in joined
    assert "不正所見" not in joined


def test_malformed_rule_collections_and_contexts_do_not_stop_display(db):
    joined = _render(db, {}, {
        "events": [{"bad": 1}, "media_ref"], "symptoms": 7,
        "medications": 7, "med_periods": {"start": "2026-10-01"},
        "rx_actions": [{"action": ["start"], "ctx": "合成文脈"},
                       {"action": "start", "ctx": 7}],
        "requests": [{"kind": ["confirm"], "ctx": "合成依頼"}, {"ctx": 7}]})
    assert joined == "区分: 添付\n依頼: 合成依頼"


def test_empty_selected_collections_keep_supported_rule_fallback(db):
    joined = _render(db, {"symptoms": [], "meds": [], "requests": []}, {
        "symptoms": ["合成症状"], "medications": [{"name": "合成薬"}],
        "requests": [{"kind": "confirm", "ctx": "合成依頼"}]})
    assert "症状: 合成症状" in joined
    assert "薬剤候補（未確認）: 合成薬" in joined
    assert "依頼: 確認:合成依頼" in joined


@pytest.mark.parametrize("kind", ["extract_llm", "canonical_projection"])
@pytest.mark.parametrize("flag", [True, "false", 0, None, [], {}])
def test_unverified_labs_render_apart_from_confirmed(db, kind, flag):
    with db.db:
        db.db.execute(
            "UPDATE messages SET body_text=? WHERE message_id=1",
            ("合成確認検査1。合成候補検査2mg/dL高。合成旧形式検査3。",))
    _fact_artifact(db, kind, {"labs": [
        {"name": "合成確認検査", "value": 1, "unverified": False,
         "evidence": "合成確認検査1"},
        {"name": "合成候補検査", "value": 2, "unit": "mg/dL",
         "flag": "high", "unverified": flag, "evidence": "合成候補検査2mg/dL高"},
        {"name": "合成旧形式検査", "value": 3, "evidence": "合成旧形式検査3"}]})
    assert structured_view.structured_lines(db.db, 1) == [
        "検査: 合成確認検査 1・合成旧形式検査 3",
        "検査候補（未確認）: 合成候補検査 2mg/dL(高)"]


def test_unverified_only_labs_keep_candidate_label_and_total_limit(db):
    joined = _render(db, {"labs": [
        {"name": f"合成候補{n}", "value": n, "unverified": True}
        for n in range(7)]})
    assert joined.startswith("検査候補（未確認）: ")
    assert "合成候補5 5" in joined
    assert "合成候補6" not in joined
    assert "\n検査: " not in joined


def test_mixed_labs_preserve_total_limit(db):
    with db.db:
        db.db.execute("UPDATE messages SET body_text=? WHERE message_id=1",
                      ("。".join(f"合成検査{n}:{n}" for n in range(7)),))
    joined = _render(db, {"labs": [
        {"name": f"合成検査{n}", "value": n, "unverified": n % 2 == 0,
         "evidence": f"合成検査{n}:{n}"}
        for n in range(7)]})
    assert joined.splitlines() == [
        "検査: 合成検査1 1・合成検査3 3・合成検査5 5",
        "検査候補（未確認）: 合成検査0 0・合成検査2 2・合成検査4 4"]


@pytest.mark.parametrize("kind", ["extract_llm", "canonical_projection", "semantic_facts_v4"])
def test_empty_current_facts_do_not_restore_rule_events_or_vitals(db, kind):
    db.artifact_add("extract_v1", json.dumps({"events": ["eol", "media_ref"],
                                              "vitals": {"spo2": 10}}),
                    project_id=1, message_id=1, meta={"hash": "synthetic-hash"})
    db.artifact_add(kind, "{}", project_id=1, message_id=1,
                    meta={"hash": "synthetic-hash", "engine_version": 4,
                          "projection_version": PROJECTION_VERSION})
    assert structured_view.structured_lines(db.db, 1) == ["区分: 添付"]


@pytest.mark.parametrize("kind", [None, "extract_llm", "canonical_projection", "semantic_facts_v4"])
def test_absent_or_stale_facts_keep_rule_events_and_vitals(db, kind):
    db.artifact_add("extract_v1", json.dumps({"events": ["visit"],
                                              "vitals": {"hr": 72}}),
                    project_id=1, message_id=1, meta={"hash": "synthetic-hash"})
    if kind is not None:
        db.artifact_add(kind, "{}", project_id=1, message_id=1,
                        meta={"hash": "synthetic-stale", "engine_version": 4,
                              "projection_version": PROJECTION_VERSION})
    assert structured_view.structured_lines(db.db, 1) == ["区分: 訪問", "バイタル: HR 72"]


def test_plain_summary_uses_one_fixed_order():
    import structured_view
    lines = ["区分: 依頼", "🚨 緊急度: 高", "合成の概要", "症状: 合成症状",
             "薬剤: 合成薬 5mg[開始]", "要点: 合成の要点", "次回予定: 合成日",
             "依頼: 医師へ合成確認", "バイタル: BT 37.0"]
    ordered = sorted(lines, key=structured_view._summary_rank)
    assert ordered == ["🚨 緊急度: 高", "合成の概要", "要点: 合成の要点",
                       "依頼: 医師へ合成確認", "薬剤: 合成薬 5mg[開始]",
                       "症状: 合成症状", "バイタル: BT 37.0", "次回予定: 合成日",
                       "区分: 依頼"]


@pytest.mark.parametrize("field", ["jev", "extracted"])
@pytest.mark.parametrize("value", [[], {}, 7, True, "wrong-verdict"])
def test_malformed_qc_verdict_never_stops_display_or_vetoes_urgency(db, field, value):
    source = db.artifact_add("extract_llm", json.dumps({"urgency": "high"}),
        project_id=1, message_id=1, meta={"hash": "synthetic-hash"})
    verdict = {"extracted": "high", "jev": "routine", "confidence": 0.99, field: value}
    db.artifact_add("extract_qc", json.dumps({"urgency": verdict}),
        project_id=1, message_id=1,
        meta={"hash": "synthetic-hash", "source_artifact_id": source})
    assert structured_view.urgency_qc_disagreement(db.db, 1) is None
    assert structured_view.urgency_qc_suffix(db.db, 1) == ""


@pytest.mark.parametrize("key", [[], {}, 7, None])
def test_malformed_vital_flag_key_keeps_valid_summary(db, key):
    joined = _render(db, {"summary": "合成要約",
        "vital_flags": [{"key": key, "value": 98}]})
    assert joined == "合成要約"


@pytest.mark.parametrize("plain", [False, True])
def test_all_layouts_use_the_same_urgency_label(plain):
    assert structured_view._head_lines({}, {}, "llm", plain=plain) == ["🚨 緊急度高"]


def test_legacy_held_urgency_keeps_the_reason_without_model_source_label(db):
    with db.db:
        db.db.execute("UPDATE messages SET body_text='本人は落ち着いています。' WHERE message_id=1")
    joined = _render(db, {"urgency": "high", "summary": "合成要約"})
    assert "対象人物・時点の根拠を確認。元の判定は高" in joined
    assert "AI判定" not in joined and "AI抽出" not in joined
