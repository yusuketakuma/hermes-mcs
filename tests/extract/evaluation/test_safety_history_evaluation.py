"""Fully synthetic allergy / adverse-reaction / ACP corpus, offline scorer and gen-11 baseline."""
import ast
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

import extract
import extract_bench
import extract_bench_assertions as assertions
import extract_llm
import extract_validation

ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "evaluation" / "extract_cases_safety_history.json"
BASELINE = ROOT / "evaluation" / "extract_baseline_gen11_safety_history.json"
CASES = json.loads(CORPUS.read_bytes())["cases"]
BY_ID = {case["id"]: case for case in CASES}
ACP = {"care_detail": {"event_type": "acp_discussion"}}


def _claims(case, labels):
    """A stored output asserting exactly what each label names, quoting the whole body."""
    body = case["body"]
    out = {"meds": list(labels.get("meds", [])), "event_records": [
        {"kind": "other", "subject": label.get("subject", "patient"),
         "status": label.get("status", "current"), "negated": label.get("negated", False),
         "evidence": body, "care_detail": {**label["care_detail"], "workflow_status": "unknown"}}
        for label in labels.get("event_records", [])]}
    out["patient_context"] = [
        {"category": label["category"], "text": label["value_contains"], "evidence": body,
         "subject": label.get("subject", "patient")} for label in labels.get("patient_context", [])]
    if "allergies" in labels:
        out["allergies"] = [
            {"allergen": label.get("allergen"), "kind": label.get("kind", "allergy"),
             "reaction": label.get("reaction"), "subject": label.get("subject", "patient"),
             "status": label.get("status", "current"), "negated": label.get("negated", False),
             "certainty": label.get("certainty", "confirmed"), "evidence": body}
            for label in labels["allergies"]]
    return out


def _score(args, tmp_path, stored, cases=CORPUS):
    outputs, report = tmp_path / "outputs.json", tmp_path / "report.json"
    outputs.write_text(json.dumps(stored, ensure_ascii=False))
    code = extract_bench.cmd_score(SimpleNamespace(
        cases=str(cases), outputs=str(outputs), tag=args, out=str(report)))
    return code, (json.loads(report.read_text()) if report.exists() else None)


def _stored(outputs, **extra):
    return {"validated": True, "generations": {"llm_extract": 12},
            "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
            "outputs": outputs, **extra}


def test_corpus_is_approved_distinct_and_covers_every_planned_category():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)) == 39
    assert all(case_id.startswith(("alg-", "adr-", "acp-")) for case_id in ids)
    assert {case["category"] for case in CASES} == {
        "アレルギー確定", "副作用歴", "否定", "家族", "別患者", "予定・仮定",
        "引用された過去記録", "疑い", "ACP実施", "ACP予定・否定・家族の意向", "対照"}
    approved = extract_bench._synthetic_cases()
    assert all(case in approved for case in CASES)
    # No id is shared with an older corpus, so id-keyed tests keep their cases.
    assert len({case["id"] for case in approved}) == len(approved)
    for case in CASES:
        assert case["posted_at"].endswith("+09:00"), case["id"]
        assert "expect" in case or "forbid" in case, case["id"]
        for key in ("expect", "forbid"):
            assert extract_bench._section_valid(case.get(key, {}), forbid=key == "forbid"), (
                case["id"], key)


def test_case_bodies_do_not_leak_from_few_shot_examples():
    def norm(text):
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))

    examples = norm(extract_llm._PROMPT_EXAMPLES)
    for case in CASES:
        text = norm(case["body"] + case.get("context", ""))
        assert not {text[i:i + 12] for i in range(max(len(text) - 11, 1))
                    if text[i:i + 12] in examples}, case["id"]


def test_output_matching_every_expectation_misses_nothing_and_asserts_nothing_forbidden():
    scores = [extract_bench._score_case(case, _claims(case, case.get("expect", {}))) for case in CASES]
    fields = extract_bench._aggregate(scores)
    assert {name: (row["fp"], row["fn"]) for name, row in fields.items()
            if name.split(".")[0] in ("allergies", "event_records", "patient_context", "meds")} == {
        name: (0, 0) for name in (
            "allergies", "event_records", "event_records.acp_discussion", "meds", "patient_context",
            "patient_context.adverse_events", "patient_context.allergies", "patient_context.preferences")}
    assert (fields["allergies"]["tp"], fields["event_records.acp_discussion"]["tp"]) == (12, 4)
    assert extract_bench._false_assertions(scores) == {"total": 0, "by_field": {}, "by_detail": {}}


@pytest.mark.parametrize("case", [case for case in CASES if "forbid" in case], ids=lambda case: case["id"])
def test_every_forbid_label_is_violated_by_the_assertion_it_names(case):
    score = extract_bench._score_case(case, _claims(case, case["forbid"]))
    assert len(score["forbid_violations"]) == sum(len(labels) for labels in case["forbid"].values())
    counts = extract_bench._false_assertions([score])
    assert counts["total"] == len(score["forbid_violations"])
    assert counts["by_field"] == {field: len(labels) for field, labels in case["forbid"].items()}


def test_false_assertion_counts_split_by_category_and_care_type():
    scores = [extract_bench._score_case(case, _claims(case, case["forbid"]))
              for case in (BY_ID["alg-family-daughter"], BY_ID["acp-planned"])]
    assert extract_bench._false_assertions(scores) == {
        "total": 6,
        "by_field": {"allergies": 2, "event_records": 2, "patient_context": 2},
        "by_detail": {"event_records.acp_discussion": 2, "patient_context.allergies": 2}}


def test_family_allergy_filed_without_a_subject_is_a_wrong_assertion():
    # The heading rules emit "unspecified", which downstream reads as the patient's own.
    case = BY_ID["alg-family-daughter"]
    filed = {"patient_context": [{"category": "allergies", "text": "卵アレルギー",
                                  "evidence": case["body"], "subject": "unspecified"}]}
    assert extract_bench._score_case(case, filed)["forbid_violations"] == ["patient_context:allergies"]
    filed["patient_context"][0]["subject"] = "family"
    assert extract_bench._score_case(case, filed)["forbid_violations"] == []


def _allergy(case, **axes):
    return {"allergies": [{"allergen": None, "kind": "allergy", "reaction": None, "subject": "patient",
                           "status": "current", "negated": False, "certainty": "confirmed",
                           "evidence": case["body"], **axes}]}


@pytest.mark.parametrize(("case_id", "allergen", "allowed"), [
    ("alg-family-daughter", "卵", "family"), ("alg-family-heading", "架空果実M", "family"),
    ("adr-family-spouse", "合成薬N", "family"), ("alg-other-roommate", "架空薬P", "other"),
    ("adr-other-same-surname", "合成薬Q", "other")])
def test_someone_elses_allergy_asserted_without_a_subject_is_a_wrong_assertion(case_id, allergen, allowed):
    # An assertion that leaves the subject open is read downstream as the patient's own.
    case = BY_ID[case_id]
    unowned = extract_bench._score_case(case, _allergy(case, allergen=allergen, subject="unknown"))
    assert unowned["forbid_violations"] == [f"allergies:{allergen}"]
    owned = extract_bench._score_case(case, _allergy(case, allergen=allergen, subject=allowed))
    assert owned["forbid_violations"] == []


@pytest.mark.parametrize(("case_id", "allergen", "allowed"), [
    ("alg-planned-test", None, "planned"), ("adr-conditional-instruction", "合成薬R", "conditional"),
    ("alg-to-be-asked", "架空薬S", "planned")])
def test_a_planned_or_hypothetical_allergy_asserted_without_a_time_is_a_wrong_assertion(
        case_id, allergen, allowed):
    case = BY_ID[case_id]
    untimed = extract_bench._score_case(case, _allergy(case, allergen=allergen, status="unknown"))
    assert untimed["forbid_violations"] == [f"allergies:{allergen or '*'}"]
    timed = extract_bench._score_case(case, _allergy(case, allergen=allergen, status=allowed))
    assert timed["forbid_violations"] == []


def test_another_patients_acp_asserted_without_a_subject_is_a_wrong_assertion():
    case = BY_ID["acp-other-patient"]
    record = {"kind": "other", "subject": "unknown", "status": "current", "negated": False,
              "evidence": case["body"],
              "care_detail": {"event_type": "acp_discussion", "workflow_status": "unknown"}}
    score = extract_bench._score_case(case, {"event_records": [record]})
    assert score["forbid_violations"] == ["event_records:acp_discussion"]
    assert score["forbid_counts"] == {"event_records": 1, "event_records.acp_discussion": 1}
    score = extract_bench._score_case(case, {"event_records": [{**record, "subject": "other"}]})
    assert score["forbid_violations"] == []


@pytest.mark.parametrize("change", [
    {"subject": "family"}, {"negated": True}, {"status": "planned"}, {"kind": "allergy"},
    {"reaction": "発疹"}, {"unverified": True}, {"evidence": "原文に無い引用"}, {"allergen": "合成薬Z"}])
def test_an_allergy_assertion_with_one_wrong_axis_is_not_a_hit(change):
    case = BY_ID["adr-history-heading"]
    right = _claims(case, case["expect"])
    assert extract_bench._score_case(case, right)["fields"]["allergies"] == {"tp": 1, "fp": 0, "fn": 0}
    wrong = {**right, "allergies": [{**right["allergies"][0], **change}]}
    assert extract_bench._score_case(case, wrong)["fields"]["allergies"] == {"tp": 0, "fp": 1, "fn": 1}


def test_an_unsourced_assertion_shown_as_confirmed_still_violates_a_forbid_label():
    case = BY_ID["alg-context-only"]
    claim = {"allergen": "架空薬W", "kind": "allergy", "subject": "patient", "status": "current",
             "negated": False, "certainty": "confirmed", "evidence": "アレルギー：架空薬W"}
    assert extract_bench._score_case(case, {"allergies": [claim]})["forbid_violations"] == ["allergies:*"]
    held = {"allergies": [{**claim, "unverified": True}]}
    assert extract_bench._score_case(case, held)["forbid_violations"] == []


@pytest.mark.parametrize("change", [
    {"subject": "other"}, {"negated": True}, {"status": "current"},
    {"care_detail": {"event_type": "care_policy_change"}}, {"care_detail": None}, {"unverified": True}])
def test_an_acp_record_with_one_wrong_axis_is_not_a_hit(change):
    case = BY_ID["acp-planned"]
    right = _claims(case, case["expect"])
    assert extract_bench._score_case(case, right)["fields"]["event_records.acp_discussion"] == {
        "tp": 1, "fp": 0, "fn": 0}
    wrong = {"event_records": [{**right["event_records"][0], **change}]}
    assert extract_bench._score_case(case, wrong)["fields"]["event_records.acp_discussion"]["tp"] == 0


def test_an_acp_label_matches_any_legacy_kind_but_a_kind_label_still_pins_it():
    record = {"kind": "eol", "subject": "patient", "status": "current", "negated": False,
              "care_detail": {"event_type": "acp_discussion", "workflow_status": "unknown"}}
    assert extract_bench._match_event(ACP, [record])
    assert not extract_bench._match_event({**ACP, "kind": "care"}, [record])
    assert not extract_bench._match_event({"subject": "patient"}, [record])


def test_acp_claims_through_the_production_validator():
    # The generation-11 validator binds an ACP record to the source axes: it keeps
    # three of four expected records (the "ご本人と…話し合いを行いました" wording stays
    # unverified) and never lets a claimed "done" ride a planned, negated,
    # conditional or other-patient source.
    def claim(case, label, **axes):
        quote = next(part for part in re.split(r"(?<=。)", case["body"])
                     if re.search("ACP|人生会議", part))
        return {"kind": "other", "subject": "patient", "status": label.get("status", "current"),
                "negated": False, "evidence": quote, "care_detail": dict(label["care_detail"]), **axes}

    expected = [case for case in CASES if "event_records" in case.get("expect", {})]
    scores = [extract_bench._score_case(case, extract_validation.validate(
        {"events": [], "event_records": [claim(case, label) for label in case["expect"]["event_records"]]},
        case["body"])) for case in expected]
    row = extract_bench._aggregate(scores)["event_records.acp_discussion"]
    assert (row["tp"], row["fn"]) == (3, 1)
    assert [score["id"] for score in scores if score["fields"]["event_records"]["fn"]] == [
        "acp-done-natural-wording"]
    for case_id in ("acp-planned", "acp-not-yet", "acp-conditional", "acp-other-patient"):
        case = BY_ID[case_id]
        out = extract_validation.validate({"events": [], "event_records": [
            claim(case, ACP, status="current")]}, case["body"])
        assert extract_bench._score_case(case, out)["forbid_violations"] == [], case_id


def test_a_failed_extraction_stays_in_every_recall_denominator():
    for case_id, field in (("adr-history-heading", "allergies"), ("acp-planned", "event_records"),
                           ("acp-planned", "event_records.acp_discussion")):
        score = extract_bench._score_case(BY_ID[case_id], None)
        assert score["error"] == "extract_failed"
        assert score["fields"][field] == {"tp": 0, "fp": 0, "fn": 1}


@pytest.mark.parametrize("labels", [
    [{"alergen": "合成薬A"}], [{"allergen": ""}], [{"allergen": 3}], [{"kind": "intolerance"}],
    [{"subject": "unknown"}], [{"subject": "unspecified"}], [{"status": "unknown"}],
    [{"status": "ongoing"}], [{"negated": "false"}], [{"certainty": "probable"}],
    ["合成薬A"], {"allergen": "合成薬A"}])
def test_misspelt_or_off_vocabulary_allergy_labels_are_rejected(labels):
    assert not assertions.labels_valid(labels)
    assert not extract_bench._section_valid({"allergies": labels})


@pytest.mark.parametrize("label", [{"subject": "unknown"}, {"status": "unknown"}])
def test_an_abstaining_allergy_label_is_a_forbid_label_only(label):
    # Nothing is expected to abstain, but an abstaining assertion must be nameable as wrong.
    assert assertions.labels_valid([label], forbid=True)
    assert extract_bench._section_valid({"allergies": [label]}, forbid=True)
    assert not assertions.labels_valid([label])
    assert not assertions.labels_valid([{"subject": "unspecified"}], forbid=True)
    assert not any("unknown" in (label.get("subject"), label.get("status"))
                   for case in CASES for label in case.get("expect", {}).get("allergies", []))


@pytest.mark.parametrize("detail", [
    {"event_type": "acp"}, {"event_typo": "acp_discussion"}, "acp_discussion",
    {"event_type": "acp_discussion", "workflow_status": "finished"}])
def test_misspelt_care_detail_labels_are_rejected(detail):
    assert not extract_bench._section_valid({"event_records": [{"care_detail": detail}]})
    assert extract_bench._section_valid({"event_records": [
        {"care_detail": {"event_type": "acp_discussion", "workflow_status": "planned"},
         "status": "planned"}]})


def test_context_label_subject_accepts_unspecified_only_as_a_known_value():
    label = {"category": "allergies", "value_contains": "卵"}
    assert extract_bench._section_valid({"patient_context": [{**label, "subject": "unspecified"}]})
    assert not extract_bench._section_valid({"patient_context": [{**label, "subject": "unknown"}]})


def test_baseline_fixture_is_the_rule_extractors_patient_context_for_this_corpus():
    baseline = json.loads(BASELINE.read_bytes())
    assert baseline["corpus_sha256"] == hashlib.sha256(CORPUS.read_bytes()).hexdigest()
    assert baseline["scored_fields"] == ["patient_context"]
    # Not measured is not a result: the fixture carries no counts and no LLM outputs.
    assert not {"fields", "false_assertion", "cases"} & set(baseline)
    assert baseline["unmeasured"]["llm_extract"]["generation"] == 11
    assert all(set(out) == {"patient_context"} for out in baseline["outputs"].values())
    assert baseline["generations"] == {
        "rule": extract.RULE_VERSION, "patient_context": extract_llm.PATIENT_CONTEXT_VERSION}, (
        "the rule or patient-context generation changed: add a new baseline fixture "
        "for the new generation and keep this one for generation 11")
    assert baseline["outputs"] == {
        case["id"]: {"patient_context": extract.extract_message(
            case["body"], case["posted_at"]).get("patient_context", [])} for case in CASES}


def test_baseline_scores_offline_and_names_what_it_does_not_measure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda *args, **kwargs: pytest.fail("score must not call a model"))
    report = tmp_path / "baseline.json"
    assert extract_bench.cmd_score(SimpleNamespace(
        cases=str(CORPUS), outputs=str(BASELINE), tag="gen11-rule", out=str(report))) == 0
    result = json.loads(report.read_text())
    assert {name: [row["tp"], row["fp"], row["fn"]] for name, row in result["fields"].items()} == {
        "patient_context": [8, 0, 4], "patient_context.allergies": [3, 0, 1],
        "patient_context.adverse_events": [3, 0, 1], "patient_context.preferences": [2, 0, 2]}
    # The one wrong assertion: a family member's allergy under an allergy heading.
    assert result["false_assertion"] == {"total": 1, "by_field": {"patient_context": 1},
                                         "by_detail": {"patient_context.allergies": 1}}
    assert [(case["id"], case["forbid_violations"]) for case in result["cases"]
            if case["forbid_violations"]] == [("alg-family-heading", ["patient_context:allergies"])]
    assert result["source"] == "stored_outputs"
    assert result["generations"] == {"rule": 29, "patient_context": 4}
    assert result["outputs_sha256"] == hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    assert (result["tag"], result["n_cases"], result["errors"]) == ("gen11-rule", 39, [])
    # Unmeasured stays null and out-of-scope fields carry no number.
    assert result["call_stats"] is None and result["performance"] is None and result["model"] is None
    assert result["out_of_scope_fields"] == [
        "allergies", "event_records", "events", "meds", "requests", "symptoms"]
    assert result["fields_without_output"] == ["allergies"]
    printed = capsys.readouterr().out
    assert "false_assertion total=1" in printed and "欄なし（未取得）: allergies" in printed
    # The report is comparable with itself by `report`, and an older report reads as not counted.
    assert extract_bench.cmd_report(SimpleNamespace(files=[str(report), str(report)])) == 0
    assert "total 1" in capsys.readouterr().out
    result.pop("false_assertion")
    old = tmp_path / "old.json"
    old.write_text(json.dumps(result))
    assert extract_bench.cmd_report(SimpleNamespace(files=[str(old), str(report)])) == 0
    assert "未集計" in capsys.readouterr().out


def test_score_accepts_a_run_report_and_keeps_its_call_statistics(tmp_path):
    rows = [{"id": case["id"], "validated_output": _claims(case, case.get("expect", {}))}
            for case in CASES]
    rows[0] = {"id": CASES[0]["id"], "error": "extract_failed", "validated_output": {"allergies": []}}
    code, report = _score("gen12", tmp_path, {
        "tag": "run", "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "prompt_sha256": "0" * 64, "model": "synthetic-model", "call_stats": {"calls": 39},
        "patient_state_mode": "on",
        "generations": {"llm_extract": 12, "patient_context": 5}, "cases": rows})
    assert code == 0
    assert report["errors"] == [CASES[0]["id"]] and report["e2e_success_rate"] == 0.974
    assert report["fields"]["allergies"]["fn"] == 1
    assert (report["model"], report["call_stats"]) == ("synthetic-model", {"calls": 39})
    assert report["generations"] == {"llm_extract": 12, "patient_context": 5}
    assert report["patient_state_mode"] == "on"
    assert report["scored_fields"] is None and report["out_of_scope_fields"] == []
    assert report["fields_without_output"] == []


@pytest.mark.parametrize(("change", "reason"), [
    (lambda stored: stored["outputs"].pop("alg-ctl-unrelated"), "outputs_case_ids_mismatch"),
    (lambda stored: stored["outputs"].update({"not-in-corpus": {}}), "outputs_case_ids_mismatch"),
    (lambda stored: stored.update(corpus_sha256="0" * 64), "outputs_corpus_mismatch"),
    (lambda stored: stored.pop("corpus_sha256"), "outputs_corpus_mismatch"),
    (lambda stored: stored.pop("validated"), "outputs_must_be_validated"),
    (lambda stored: stored.update(validated="yes"), "outputs_must_be_validated"),
    (lambda stored: stored.pop("generations"), "outputs_generations_required"),
    (lambda stored: stored.update(generations={"llm_extract": "12"}), "outputs_generations_required"),
    (lambda stored: stored.update(generations={"semantic": 2}), "outputs_generations_required"),
    (lambda stored: stored["outputs"].update({"alg-ctl-unrelated": []}), "outputs_shape_invalid"),
    (lambda stored: stored.update(scored_fields=["patient_context.allergies"]),
     "outputs_scored_fields_invalid"),
    (lambda stored: stored.update(outputs=None), "outputs_missing"),
])
def test_score_refuses_inputs_it_cannot_score_honestly(tmp_path, capsys, change, reason):
    stored = _stored({case["id"]: {} for case in CASES})
    change(stored)
    code, report = _score("bad", tmp_path, stored)
    assert (code, report) == (2, None)
    assert capsys.readouterr().err.strip() == reason


@pytest.mark.parametrize(("change", "reason"), [
    # A report that does not name its generation cannot be compared with a baseline.
    (lambda report: report.pop("generations"), "outputs_generations_required"),
    (lambda report: report.update(generations={"llm_extract": "12"}), "outputs_generations_required"),
    # Neither an error nor an output is "not measured", never a failed extraction.
    (lambda report: report["cases"][1].pop("validated_output"), "outputs_shape_invalid"),
    (lambda report: report["cases"][1].update(validated_output=None), "outputs_shape_invalid"),
    (lambda report: report["cases"][1].update(validated_output=[]), "outputs_shape_invalid"),
    (lambda report: report.pop("prompt_sha256"), "outputs_not_a_run_report"),
    (lambda report: report["cases"].pop(), "outputs_case_ids_mismatch"),
])
def test_score_refuses_a_run_report_it_cannot_score_honestly(tmp_path, capsys, change, reason):
    report = {"corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
              "prompt_sha256": "0" * 64, "generations": {"llm_extract": 12, "patient_context": 5},
              "cases": [{"id": case["id"], "validated_output": {}} for case in CASES]}
    change(report)
    code, written = _score("bad", tmp_path, report)
    assert (code, written) == (2, None)
    assert capsys.readouterr().err.strip() == reason


def test_score_refuses_unreadable_outputs_without_echoing_them(tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{合成の壊れた入力")
    assert extract_bench.cmd_score(SimpleNamespace(
        cases=str(CORPUS), outputs=str(broken), tag="bad", out=str(tmp_path / "r.json"))) == 2
    assert capsys.readouterr().err.strip() == "score_input_invalid"


def test_bench_modules_are_never_imported_by_runtime_code():
    names = {"extract_bench", "extract_bench_assertions", "extract_bench_stored"}
    offenders = []
    for top in ("mcs", "mcs_standalone", "hermes_plugin", "adapters", "lineworks_adapter"):
        for path in (ROOT / top).rglob("*.py"):
            if "eval" in path.relative_to(ROOT).parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                imported = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                            else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                if names & {name.split(".")[-1] for name in imported}:
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    # The stored-output reader and the assertion scorer themselves stay model-free.
    for name in ("extract_bench_assertions.py", "extract_bench_stored.py"):
        tree = ast.parse((ROOT / "mcs/extract/eval" / name).read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names} | {node.module for node in ast.walk(tree)
                                                if isinstance(node, ast.ImportFrom)}
        assert imported <= {"__future__", "clinical_events", "mcs_util"}, name
