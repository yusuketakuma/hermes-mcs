"""A/B uses the real formatter and scores a reference on the same grounded unit."""
import extract_llm_runner
import extract_validation
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import extract_bench as bench
import extract_llm as llm

CORPUS = Path(bench.DEFAULT_CASES).with_name("extract_cases_patient_state.json")


def cases():
    return bench._load_cases(str(CORPUS))


def output(case, *, refs=True):
    label = case["expect"]["patient_context"][0]
    detail = {"key": label["detail_key"], "value": label["value_contains"], "evidence": case["body"]}
    if refs:
        detail["refers_to"] = "S1"
    raw = {"patient_context": [{"category": label["category"], "text": case["body"],
                               "evidence": case["body"], "subject": "patient", "details": [detail]}]}
    return extract_validation.validate(raw, case["body"], ctx=True, references=bench._case_context(case, "on")[1])


def test_all_four_reference_cases_and_two_contamination_cases_are_fictional_and_well_formed():
    source = cases()
    assert len(source) == 6 and sum("expect" in case for case in source) == 4
    assert bench.cmd_run(SimpleNamespace(cases=str(CORPUS), mock_ok=True)) == 0
    for case in source[:4]:
        text, mapping = bench._case_context(case, "on")
        assert "[S1]" in text and len(text) <= 600 and len(mapping) == 1
        assert bench._case_context(case, "off") == (None, {})
        result = bench._score_case(case, output(case))
        assert result["fields"]["patient_context"] == {"tp": 1, "fp": 0, "fn": 0}
        assert not result["forbid_violations"]
        assert bench._score_case(case, output(case, refs=False))["fields"]["patient_context"]["fn"] == 1


def test_a_forged_known_token_on_another_source_never_scores_as_a_correct_reference():
    case = cases()[0]
    out = output(case)
    out["patient_context"][0]["details"][0]["refers_to"]["message_id"] = 123
    assert bench._score_case(case, out)["fields"]["patient_context"]["fn"] == 1


def test_reference_and_value_must_match_on_one_detail_not_two_siblings():
    case = cases()[0]
    out = output(case)
    first = out["patient_context"][0]["details"][0]
    missing_ref = {key: value for key, value in first.items() if key != "refers_to"}
    other = {**first, "key": "followup"}
    out["patient_context"][0]["details"] = [missing_ref, other]
    assert bench._score_case(case, out)["fields"]["patient_context"]["fn"] == 1


@pytest.mark.parametrize("index", [4, 5])
def test_context_only_value_and_other_person_cannot_become_a_target_reference(index):
    case = cases()[index]
    text, mapping = bench._case_context(case, "on")
    assert text and mapping
    label = case["forbid"]["patient_context"][0]
    raw = {"patient_context": [{"category": label["category"], "text": case["body"],
        "evidence": case["body"], "subject": "patient", "details": [{"key": label["detail_key"],
        "value": label["value_contains"], "evidence": case["body"], "refers_to": "S1"}]}]}
    out = extract_validation.validate(raw, case["body"], references=mapping)
    assert all("refers_to" not in detail for item in out.get("patient_context", [])
               for detail in item.get("details", []))
    assert out["_items_dropped"] >= 1


def test_on_off_cli_path_passes_effective_context_and_records_its_digest(tmp_path, monkeypatch):
    source = cases()[0]
    received = []
    monkeypatch.setattr(bench, "_synthetic_cases", lambda: [source])
    path = tmp_path / "approved-synthetic.json"
    path.write_text(json.dumps({"cases": [source]}))

    def infer(body, **kwargs):
        received.append((kwargs["context"], kwargs.get("references", {})))
        return output(source, refs=bool(kwargs.get("references")))

    monkeypatch.setattr(llm, "llm_extract", infer)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', llm.llm_extract)
    reports = []
    for mode in ("off", "on"):
        out = tmp_path / (mode + ".json")
        assert bench.cmd_run(SimpleNamespace(cases=str(path), mock_ok=False, out=str(out),
                                              tag=mode, patient_state=mode)) == 0
        reports.append(json.loads(out.read_text()))
    assert received[0] == (None, {}) and "[S1]" in received[1][0]
    assert reports[0]["cases"][0]["context_sha256"] != reports[1]["cases"][0]["context_sha256"]
    assert reports[0]["patient_state_mode"] == "off" and reports[1]["patient_state_mode"] == "on"
    assert reports[1]["cases"][0]["validated_output"]
    assert reports[1]["actual_model"] == llm._resolved_llm()[1]


def test_off_mode_can_call_a_baseline_engine_without_the_reference_parameter(tmp_path, monkeypatch):
    case = cases()[0]
    monkeypatch.setattr(bench, "_synthetic_cases", lambda: [case])
    path = tmp_path / "approved-synthetic.json"
    path.write_text(json.dumps({"cases": [case]}))
    calls = []

    def old_signature(body, *, meta_out=None, context=None, posted_at=None):
        calls.append((body, context, posted_at))
        return {}

    monkeypatch.setattr(llm, "llm_extract", old_signature)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', llm.llm_extract)
    assert bench.cmd_run(SimpleNamespace(cases=str(path), mock_ok=False, out=str(tmp_path / "out.json"),
                                          tag="baseline", patient_state="off")) == 0
    assert calls == [(case["body"], None, case["posted_at"])]


def test_a_case_without_history_has_the_same_effective_input_and_digest_in_both_modes(tmp_path, monkeypatch):
    plain = next(case for case in bench._load_cases(bench.DEFAULT_CASES) if not case.get("patient_state"))
    monkeypatch.setattr(bench, "_synthetic_cases", lambda: [plain])
    path = tmp_path / "approved-synthetic.json"
    path.write_text(json.dumps({"cases": [plain]}))
    received = []

    def infer(body, **kwargs):
        received.append((kwargs.get("context"), kwargs.get("references", {})))
        return {}

    monkeypatch.setattr(llm, "llm_extract", infer)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', llm.llm_extract)
    digests = []
    for mode in ("off", "on"):
        out = tmp_path / (mode + ".json")
        assert bench.cmd_run(SimpleNamespace(cases=str(path), mock_ok=False, out=str(out),
                                              tag=mode, patient_state=mode)) == 0
        report = json.loads(out.read_text())
        assert report["patient_state_mode"] == mode
        digests.append(report["cases"][0]["context_sha256"])
    assert received[0] == received[1]
    assert digests[0] == digests[1]
