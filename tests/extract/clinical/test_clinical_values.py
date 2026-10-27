"""Fully synthetic clinical normalization regressions through real extraction."""
import extract_quality
import extract_validation

import json

import pytest

import clinical_values
import extract_llm
import inference_dependency_loss
import structured_view
from extract_testkit import _hash, _ledger, _message


@pytest.mark.parametrize("name", ["ﾛｷｿﾆﾝ錠６０ｍｇ", "ろきそにん錠60mg"])
def test_medication_surface_when_width_or_kana_varies(name, monkeypatch):
    # Given: a fictional record using a real drug spelling.
    body = f"{name}を中止しました"
    raw = {"name": name, "dose": "６０ｍｇ", "action": "stop",
           "status": "past", "subject": "patient", "negated": False,
           "evidence": body}
    monkeypatch.setattr(extract_llm, "_llm_call",
                        lambda *a, **kw: {"meds": [raw]})
    # When: the production single-message extraction validates the response.
    med = extract_llm.llm_extract(body)["meds"][0]
    # Then: raw values survive, with surface-only, unresolved identity.
    assert med["name"] == name and med["dose"] == "６０ｍｇ"
    assert med["normalized"] == {
        "surface": "ロキソニン錠60mg", "base": "ロキソニン",
        "strength": "60mg", "form": "錠",
        "resolution": "unresolved", "method": "surface"}
    assert med["evidence"] == body


@pytest.mark.parametrize("names", [
    ("タキソール", "タキソテール"), ("サクシン", "サクシゾン"),
    ("ロキソニン", "ロキソプロフェン"), ("降圧薬", "利尿薬"),
    ("アムロジピンOD錠5mg「合成甲」", "アムロジピンOD錠5mg「合成乙」"),
    ("合成配合剤A/B", "合成配合剤AB"),
])
def test_medication_identity_when_similar_or_ambiguous(names):
    # Given: two names whose equivalence has not been established.
    chunks = [extract_validation.validate({"meds": [{"name": n}]}, n) for n in names]
    # When: validated chunks merge in the real extraction path.
    meds = extract_validation.merge(chunks)["meds"]
    # Then: no name, manufacturer, class or LASA pair is merged.
    assert [m["name"] for m in meds] == list(names)
    assert all(m["normalized"]["resolution"] == "unresolved" for m in meds)
    assert meds[0]["normalized"]["surface"] != meds[1]["normalized"]["surface"]


def test_lab_candidate_when_explicit_sampling_date_and_fullwidth_units():
    # Given: an exact quotation containing an explicit sampling date.
    body = "2026年10月1日採血：Ｃｒ １．２０ｍｇ／ｄＬ"
    raw = {"name": "クレアチニン", "value": "1.20",
           "unit": "ｍｇ／ｄＬ", "evidence": body}
    # When: shared extraction validation runs.
    lab = extract_validation.validate({"labs": [raw]}, body)["labs"][0]
    # Then: typed values are additive; no dose/unit conversion or human approval.
    assert lab["value"] == "1.20" and lab["unit"] == "ｍｇ／ｄＬ"
    assert lab["normalized"] == {
        "analyte": "creatinine", "measurement_type": "creatinine",
        "value": "1.2", "value_kind": "decimal", "unit": "mg/dL",
        "measured_on": "2026-10-01", "evidence": body,
        "confirmation": "quote_supported"}
    assert not lab.get("unverified", False)


@pytest.mark.parametrize("name,value,unit,quote", [
    ("Cr", 1.2, "mg/dL", "Cr 12mg/dL"),
    ("Cr", 1.2, "mg/dL", "Cr 1.2μmol/L"),
    ("Cr", 1.2, "mg/dL", "Cr 2mg/dL、CRP 1.2mg/dL"),
    ("Cr", 1.2, "mg/dL", "Cr 1.2mg/dLextra"),
    ("Cr", 1.2, "mg/dL", "Cr 1.2mg/dL/kg"),
    ("Cr", 1.2, "mg/dL", "Cr -1.2mg/dL"),
    ("Cr", -1.2, "mg/dL", "Cr -1.2mg/dL"),
    ("Cr", 1.2, "mg/dL", "Cr <1.2mg/dL"),
    ("Cr", "123456789012345678901", None, "Cr 123456789012345678901"),
    ("Cr", 1.2, "mg/dL", "母のCr 1.2mg/dL"),
    ("Cr", 1.2, "mg/dL", "夫はCr 1.2mg/dL"),
    ("Cr", 1.2, "mg/dL", "母もCr 1.2mg/dL"),
    ("Cr", 1.2, "mg/dL", "家族 Cr 1.2mg/dL"),
    ("Cr", 1.2, "mg/dL", "夫:Cr 1.2mg/dL"),
    ("Cr", 1.2, "mg/dL", "Cr 1.2mg/dLを目標に検査予定"),
    ("Cr", 1.2, "mg/dL", "基準範囲 Cr 1.2mg/dL"),
    ("Cr", 1.2, "mg/dL", "Cr 1.2mg/dL、Cr 1.2mg/dL"),
    ("K", 4.1, None, "Kさん4.1回"),
    ("K", 4.1, None, "K4.1回"),
    ("Cr", 1.2, None, "Cr1.2"),
    ("Cr", 1.2, "mg/dL", None),
])
def test_lab_candidate_when_quote_does_not_establish_measurement(name, value, unit, quote):
    # Given: a malformed, ambiguous or non-patient measurement assertion.
    raw = {"name": name, "value": value, "unit": unit}
    if quote is not None:
        raw["evidence"] = quote
    drops = {}
    # When: production quote verification and normalization run together.
    lab = extract_validation.validate({"labs": [raw]}, quote or "合成本文", drops)["labs"][0]
    # Then: the original assertion survives only as a repairable candidate.
    assert lab["unverified"] is True
    assert lab["normalized"]["confirmation"] == "unverified"
    assert lab["normalized"]["measured_on"] is None
    assert drops["labs"]
    assert extract_quality.repair_issues(drops, {"labs": [lab]})


@pytest.mark.parametrize("quote", [
    "2026-10-01連絡。Cr 1.2mg/dL",
    "2026-10-01 Cr 1.2mg/dL",
    "2026-10-01採血Cr 1.2mg/dL、2026-10-02再検査",
    "2026-02-30採血Cr 1.2mg/dL",
    "昨日の採血Cr 1.2mg/dL",
])
def test_lab_time_when_date_is_not_unambiguous_sampling_date(quote):
    # Given: a quote with no unambiguous valid sampling date.
    raw = {"name": "Cr", "value": 1.2, "unit": "mg/dL", "evidence": quote}
    # When: extraction validation runs.
    lab = extract_validation.validate({"labs": [raw]}, quote)["labs"][0]
    # Then: no posting date, relative date or invalid date is substituted.
    assert lab["normalized"]["measured_on"] is None


@pytest.mark.parametrize("quote,value,unit", [
    ("Cr 106μmol/L", 106, "μmol/L"),
    ("Cr 1.2mg/dL", 1.2, "mg/dL"),
    ("Cr 1.2mg/dL、大丈夫です", 1.2, "mg/dL"),
    ("Cr 1.2mg/dL、大丈夫が", 1.2, "mg/dL"),
    ("食事の工夫でCr 1.2mg/dL", 1.2, "mg/dL"),
    ("Cr 0.00001mg/dL", 0.00001, "mg/dL"),
    ("BNPは正常", "正常", None),
    ("合成検査Q 陰性", "陰性", None),
])
def test_lab_value_when_supported_without_conversion(quote, value, unit):
    # Given: supported quantitative and qualitative synthetic records.
    raw = {"name": quote.split()[0] if " " in quote else "BNP",
           "value": value, "unit": unit, "evidence": quote}
    # When: extraction normalizes the value.
    lab = extract_validation.validate({"labs": [raw]}, quote)["labs"][0]
    # Then: numeric values are not converted; text does not become a number.
    expected = "0.00001" if value == 0.00001 else str(value)
    assert lab["normalized"]["value"] == expected
    assert lab["normalized"]["confirmation"] == "quote_supported"
    assert lab["normalized"]["value_kind"] == (
        "text" if isinstance(value, str) else "decimal")


def test_lab_display_when_old_artifact_has_unsupported_values(tmp_path, monkeypatch):
    # An old-format JSON can be rendered defensively as a pure transformation;
    # its missing inference version must not be relabelled as a current result.
    body = "2026-10-01採血Cr 1.2mg/dL。HbA1c 7.2%"
    old = {"labs": [
        {"name": "Cr", "value": 1.2, "unit": "mg/dL", "evidence": body.split("。")[0]},
        {"name": "HbA1c", "value": 8.2, "unit": "%", "evidence": "HbA1c 7.2%",
         "normalized": {"confirmation": "quote_supported"}},
        {"name": "BNP", "value": 20}]}
    lines = structured_view._lab_lines(old, body)
    assert "検査: Cr 1.2mg/dL(測定日:2026-10-01)" in lines
    assert "検査候補（未確認）: HbA1c 8.2%・BNP 20" in lines

    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *args, **kwargs: None)
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body=body), _message(mid=2, body=body)])
        old_id = db.artifact_add("extract_llm", json.dumps(old), project_id=1, message_id=1,
            meta={"hash": _hash(db, 1), "extract_version": extract_llm.EXTRACT_VERSION})
        assert structured_view.structured_lines(db.db, 1) == []
        # A separately reprocessed message uses the real validator and writer,
        # including the source-binding and completed-chunk integrity evidence.
        validated = extract_validation.validate({"labs": [old["labs"][0]]}, body)
        source = dict(db.db.execute("SELECT * FROM messages WHERE message_id=2").fetchone())
        source["_source_binding"] = inference_dependency_loss.source_binding(db.db, 1, 2)
        source["_target"] = ("", extract_llm.resolved_model({}))
        integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0}, "completeness_chunks": 1,
                     "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
        assert extract_llm._replace_current(db, source, json.dumps(validated, ensure_ascii=False),
                                            integrity=integrity)
        assert "検査: Cr 1.2mg/dL(測定日:2026-10-01)" in structured_view.structured_lines(db.db, 2)
        assert db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (old_id,)).fetchone()
    finally:
        db.close()


def test_lab_merge_when_dates_or_confirmation_differ():
    # Given: two dated samples, then a mismatched candidate.
    chunks = [extract_validation.validate({"labs": [{
        "name": "Cr", "value": v, "unit": "mg/dL", "evidence": q}]}, q)
        for v, q in [(1.2, "2026-10-01採血Cr 1.2mg/dL"),
                     (1.3, "2026-10-02採血Cr 1.3mg/dL"),
                     (9, "Cr 1.2mg/dL")]]
    # When: the real chunk merger combines the normalized outputs.
    labs = extract_validation.merge(chunks)["labs"]
    # Then: an unverified candidate cannot overwrite supported measurements.
    assert [lb["value"] for lb in labs] == [1.2, 1.3, 9]
    assert [lb["normalized"]["measured_on"] for lb in labs] == [
        "2026-10-01", "2026-10-02", None]


def test_lab_repair_when_evidence_becomes_supported(monkeypatch):
    # Given: a first quote-less assertion and its evidence-grounding repair.
    body = "Cr 1.2mg/dL"
    replies = iter([{"labs": [{"name": "Cr", "value": 1.2, "unit": "mg/dL"}]},
                    {"labs": [{"name": "Cr", "value": 1.2, "unit": "mg/dL",
                               "evidence": body}]}])
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *a, **kw: next(replies))
    # When: the production bounded repair pass runs.
    lab = extract_llm.llm_extract(body)["labs"][0]
    # Then: derived metadata does not block acceptance of the grounding repair.
    assert lab["normalized"]["confirmation"] == "quote_supported"
    assert not lab.get("unverified", False)


@pytest.mark.parametrize("flag", [True, None, "false"])
def test_lab_confirmation_when_source_marks_unverified(flag):
    # Given: a source confirmation flag that is not literally False.
    body = "Cr 1.2mg/dL"
    # When: persisted/returned assertions are revalidated.
    lab = extract_validation.validate({"labs": [{
        "name": "Cr", "value": 1.2, "unit": "mg/dL", "evidence": body,
        "unverified": flag}]}, body)["labs"][0]
    # Then: normalization cannot promote the original candidate.
    assert lab["unverified"] is True


def test_lab_flag_when_not_stated_in_quote():
    # Given: an invented high flag on an otherwise supported number.
    body = "HbA1c 7.2%"
    # When: local extraction checks the assertion.
    lab = extract_validation.validate({"labs": [{
        "name": "HbA1c", "value": 7.2, "unit": "%", "flag": "high",
        "evidence": body}]}, body)["labs"][0]
    # Then: no clinical out-of-range assertion becomes a verified AI fact.
    assert lab["unverified"] is True


def test_helper_digest_when_normalization_is_loaded():
    # Given/When: the production extractor has loaded its normalizer.
    # Then: running workers expose the helper digest for restart coordination.
    assert clinical_values.__file__
    assert len(extract_llm._LOADED_SOURCE_DIGESTS["clinical_values"]) == 64


def test_sampling_date_comes_from_the_clause_holding_the_matched_reading():
    from clinical_values import lab_candidate
    quote = "Cr上昇の既往あり、2026年10月1日採血でCr 1.2mg/dL"
    lab = lab_candidate("Cr", "1.2", "mg/dL", quote)
    assert lab["measured_on"] == "2026-10-01"


def test_family_reporter_and_mixed_source_do_not_discard_patient_lab():
    from clinical_values import lab_candidate
    for quote in ("娘からの報告。本人はCr 0.9 mg/dL。",
                  "母は発熱。本人はCr 0.9 mg/dL。"):
        assert lab_candidate("Cr", 0.9, "mg/dL", quote)["confirmation"] == "quote_supported"


def test_registered_patient_name_is_required_to_confirm_named_lab_quote():
    quote = "架空花子さんはCr 0.9mg/dLです。"
    assert clinical_values.lab_candidate("Cr", 0.9, "mg/dL", quote)["confirmation"] == "unverified"
    assert clinical_values.lab_candidate("Cr", 0.9, "mg/dL", quote, patient_name="架空花子")["confirmation"] == "quote_supported"
    assert clinical_values.lab_candidate("Cr", 0.9, "mg/dL", quote, patient_name="別の架空患者")["confirmation"] == "unverified"


def test_item_scope_group_default_is_unknown_without_explicit_patient_source():
    item = {"evidence": "Cr 0.9mg/dL"}
    assert clinical_values.patient_item_scope(item, "Cr 0.9mg/dL", default="unknown") == "unknown"
    assert clinical_values.patient_item_scope(item, "本人はCr 0.9mg/dL", default="unknown") == "patient"


@pytest.mark.parametrize("name,value,unit,quote,expected", [
    ("K", "4.1", "%", "K 4.1%", "unverified"),
    ("Cr", "1.2", "g", "Cr 1.2g", "unverified"),
    ("INR", "2.1", "kg", "INR 2.1kg", "unverified"),
    ("HbA1c", "7.2", "mg/dL", "HbA1c 7.2mg/dL", "unverified"),
    ("K", "4.1", "mEq/L", "K 4.1mEq/L", "quote_supported"),
    ("K", "4.1", "mmol/L", "K 4.1mmol/L", "quote_supported"),
    ("Cr", "1.2", "mg/dL", "Cr 1.2mg/dL", "quote_supported"),
    ("HbA1c", "7.2", "%", "HbA1c 7.2%", "quote_supported"),
    ("INR", "2.1", None, "INR 2.1", "quote_supported"),
    ("カリウム", "4.1", None, "カリウム4.1%", "unverified"),
    ("INR", "2.1", None, "INR 2.1kg", "unverified"),
    ("カリウム", "4.1", None, "カリウム4.1mEq/L", "quote_supported"),
    ("体重", "52", "kg", "体重52kg", "quote_supported"),
    ("eGFR", "45", "mL/min/1.73m2", "eGFR 45mL/min/1.73m2", "quote_supported"),
])
def test_lab_candidate_unit_must_fit_the_named_item(name, value, unit, quote, expected):
    assert clinical_values.lab_candidate(name, value, unit, quote)["confirmation"] == expected
