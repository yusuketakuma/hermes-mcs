"""extract_bench scoring contract tests — pure function, no LLM."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

import extract_bench


def test_med_match_action_any_when_unspecified():
    got = [{"name": "プレドニン", "action": "stop"}]
    assert extract_bench._match_med({"name": "プレドニン"}, got)
    assert extract_bench._match_med(
        {"name": "プレドニン", "action": "stop"}, got)
    assert not extract_bench._match_med(
        {"name": "プレドニン", "action": "start"}, got)


def test_symptom_match_negated_aware():
    got = [{"text": "発熱", "negated": True}]
    assert extract_bench._match_symptom(
        {"text": "発熱", "negated": True}, got)
    assert not extract_bench._match_symptom(
        {"text": "発熱", "negated": False}, got)
    # containment: longer extraction text still matches a short label
    got2 = [{"text": "左側頭部皮下血腫", "negated": False}]
    assert extract_bench._match_symptom({"text": "皮下血腫"}, got2)


def test_events_loose_fp_only_forbid():
    # open-vocabulary field: unlisted 'care' is not an FP, but a
    # forbid-matching event is
    case = {"id": "x", "expect": {"events": ["visit"]},
            "forbid": {"events": ["fall"]}}
    out = {"events": ["visit", "care", "fall"]}
    s = extract_bench._score_case(case, out)
    assert s["fields"]["events"] == {"tp": 1, "fp": 1, "fn": 0}
    assert "events:fall" in s["forbid_violations"]


def test_meds_strict_fp_counts_unlisted():
    case = {"id": "x", "expect": {"meds": [{"name": "A"}]}}
    out = {"meds": [{"name": "A"}, {"name": "B"}]}
    s = extract_bench._score_case(case, out)
    assert s["fields"]["meds"] == {"tp": 1, "fp": 1, "fn": 0}


def test_one_output_cannot_satisfy_two_expected_symptoms():
    case = {"id": "synthetic", "expect": {"symptoms": [
        {"text": "頭痛"}, {"text": "腹痛"}]}}
    output = {"symptoms": [{"text": "頭痛と腹痛", "negated": False}]}

    assert extract_bench._score_case(case, output)["fields"]["symptoms"] == {
        "tp": 1, "fp": 0, "fn": 1}


def test_one_to_one_matching_prefers_the_specific_medication():
    case = {"id": "synthetic", "expect": {"meds": [
        {"name": "合成薬"}, {"name": "合成薬", "action": "stop"}]}}
    output = {"meds": [{"name": "合成薬", "action": "stop"},
                       {"name": "合成薬", "action": "start"}]}

    assert extract_bench._score_case(case, output)["fields"]["meds"] == {
        "tp": 2, "fp": 0, "fn": 0}


def test_one_medication_cannot_satisfy_two_safety_attributes():
    case = {"id": "synthetic", "expect": {"meds": [
        {"name": "合成薬", "status": "past"},
        {"name": "合成薬", "status": "past"}]}}
    output = {"meds": [{"name": "合成薬", "status": "past"}]}

    fields = extract_bench._score_case(case, output)["fields"]
    assert fields["meds"] == {"tp": 1, "fp": 0, "fn": 1}
    assert fields["med_status"] == {"tp": 1, "fp": 0, "fn": 1}


def test_extract_failed_marks_error():
    s = extract_bench._score_case({"id": "x", "expect": {}}, None)
    assert s["error"] == "extract_failed"


def test_aggregate_f1():
    scores = [{"id": "a", "fields": {
        "meds": {"tp": 1, "fp": 1, "fn": 1}}}]
    agg = extract_bench._aggregate(scores)
    assert agg["meds"] == {"tp": 1, "fp": 1, "fn": 1,
                           "precision": 0.5, "recall": 0.5,
                           "f1": 0.5}


@pytest.mark.parametrize(
    ("output", "counts"),
    [(None, {"tp": 0, "fp": 0, "fn": 1}),
     ({"requests": []}, {"tp": 0, "fp": 0, "fn": 1}),
     ({"requests": [{"action": "確認", "to": "家族"}]},
      {"tp": 0, "fp": 1, "fn": 1}),
     ({"requests": [{"action": "確認してください", "to": "医師"}]},
      {"tp": 1, "fp": 0, "fn": 0})])
def test_request_expectations_contribute_to_recall(output, counts):
    case = {"id": "request", "expect": {
        "requests": [{"action": "確認", "to": "医師"}]}}
    score = extract_bench._score_case(case, output)
    assert score["fields"]["requests"] == counts


# #20 order 2: kind/condition/due_text join the exact-match tuple; an
# explicit null pins "absent" (from=null must not be invented)
@pytest.mark.parametrize(("label", "item", "counts"), [
    ({"action": "確認", "kind": "request"},
     {"action": "確認", "kind": "question"}, {"tp": 0, "fp": 1, "fn": 1}),
    ({"action": "確認", "kind": "request"},
     {"action": "確認", "kind": "request"}, {"tp": 1, "fp": 0, "fn": 0}),
    ({"action": "確認", "kind": "request"},
     {"action": "確認"}, {"tp": 0, "fp": 1, "fn": 1}),
    ({"action": "連絡", "condition": "熱が出たら"},
     {"action": "連絡", "condition": "熱が出たら"}, {"tp": 1, "fp": 0, "fn": 0}),
    ({"action": "連絡", "condition": "熱が出たら"},
     {"action": "連絡"}, {"tp": 0, "fp": 1, "fn": 1}),
    ({"action": "報告", "due": None, "due_text": "明日"},
     {"action": "報告", "due_text": "明日"}, {"tp": 1, "fp": 0, "fn": 0}),
    ({"action": "報告", "due": None, "due_text": "明日"},
     {"action": "報告", "due": "2026-09-02", "due_text": "明日"},
     {"tp": 0, "fp": 1, "fn": 1}),
    ({"action": "測定", "from": None},
     {"action": "測定", "from": "医師"}, {"tp": 0, "fp": 1, "fn": 1}),
    ({"action": "測定", "from": None},
     {"action": "測定"}, {"tp": 1, "fp": 0, "fn": 0}),
    # unverified=false pins "shown as confirmed"; unpinned ignores it
    ({"action": "連絡", "unverified": False},
     {"action": "連絡", "unverified": True}, {"tp": 0, "fp": 1, "fn": 1}),
    ({"action": "連絡", "unverified": False},
     {"action": "連絡"}, {"tp": 1, "fp": 0, "fn": 0}),
    ({"action": "連絡"},
     {"action": "連絡", "unverified": True}, {"tp": 1, "fp": 0, "fn": 0}),
])
def test_request_kind_condition_due_text_are_scored(label, item, counts):
    case = {"id": "request", "expect": {"requests": [label]}}
    score = extract_bench._score_case(case, {"requests": [item]})
    assert score["fields"]["requests"] == counts


@pytest.mark.parametrize(("expected", "output", "counts", "confusion"), [
    ({"kind": "ack"}, {"reply": {"kind": "ack", "evidence": "承知"}},
     {"tp": 1, "fp": 0, "fn": 0}, {"ack": {"ack": 1}}),
    (None, {"reply": {"kind": "ack", "evidence": "承知"}},
     {"tp": 0, "fp": 1, "fn": 0}, {"null": {"ack": 1}}),
    ({"kind": "done"}, {"reply": {"kind": "ack", "evidence": "承知"}},
     {"tp": 0, "fp": 1, "fn": 1}, {"done": {"ack": 1}}),
    ({"kind": "done"}, {}, {"tp": 0, "fp": 0, "fn": 1},
     {"done": {"null": 1}}),
    (None, {}, {"tp": 0, "fp": 0, "fn": 0}, {"null": {"null": 1}}),
])
def test_reply_is_scored_like_urgency(expected, output, counts, confusion):
    case = {"id": "reply", "expect": {"reply": expected}}
    score = extract_bench._score_case(case, output)
    assert score["fields"]["reply"] == counts
    assert score["raw"]["reply"] == (output.get("reply") or {}).get("kind")
    assert extract_bench._reply_confusion([score]) == confusion
    assert extract_bench._aggregate([score])["reply"]["tp"] == counts["tp"]
    # a failed extraction: an expected reply is a miss, null is not
    failed = extract_bench._score_case(case, None)
    assert failed["fields"]["reply"]["fn"] == int(expected is not None)
    assert extract_bench._reply_confusion([failed]) == {}


def test_forbidden_reply_dict_or_list():
    out = {"reply": {"kind": "done", "evidence": "完了"}}
    one = extract_bench._score_case(
        {"id": "r", "forbid": {"reply": {"kind": "done"}}}, out)
    many = extract_bench._score_case(
        {"id": "r", "forbid": {"reply": [{"kind": "ack"}, {"kind": "done"}]}},
        out)
    clean = extract_bench._score_case(
        {"id": "r", "forbid": {"reply": {"kind": "ack"}}}, out)
    assert one["forbid_violations"] == ["reply:done"]
    assert many["forbid_violations"] == ["reply:done"]
    assert clean["forbid_violations"] == []


def test_forbidden_request_is_reported_with_extracted_items():
    request = {"action": "中止", "to": "患者"}
    case = {"id": "request", "forbid": {"requests": [request]}}
    score = extract_bench._score_case(case, {"requests": [request]})
    assert score["forbid_violations"] == ["requests:中止"]
    assert score["raw"]["requests"] == [request]


def test_case_file_validates_offline(tmp_path):
    cases = extract_bench._load_cases(extract_bench.DEFAULT_CASES)
    assert len(cases) >= 10
    for c in cases:
        # production passes messages.posted_at verbatim (ISO-8601 with
        # offset): the date rule is measured on that shape only
        assert "posted_at" not in c or c["posted_at"].endswith("+09:00"), c["id"]
        for key in ("expect", "forbid"):
            # _section_valid carries the kind/reply-kind typo guard
            assert extract_bench._section_valid(c.get(key, {})), \
                f"{c['id']}: {key} fails _validate"
    # a non-string context/posted_at is rejected offline, not mid-run
    for key in ("context", "posted_at"):
        bad = tmp_path / f"{key}.json"
        bad.write_text(json.dumps({"cases": [
            {"id": "x", "body": "合成", key: ["合成"]}]}))
        with pytest.raises(ValueError, match=key):
            extract_bench._load_cases(str(bad))


def test_case_bodies_do_not_leak_from_few_shot_examples():
    import re
    import unicodedata
    import extract_llm

    def norm(text):
        # NFKC + drop whitespace/punctuation so a re-spaced or
        # re-punctuated copy of an example still counts as a leak
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))

    examples = norm(extract_llm._PROMPT_EXAMPLES)
    for c in extract_bench._load_cases(extract_bench.DEFAULT_CASES):
        body = norm(c["body"])
        leaks = {body[i:i + 12] for i in range(max(len(body) - 11, 1))
                 if body[i:i + 12] in examples}
        assert not leaks, (c["id"], leaks)


@pytest.mark.parametrize("section", [
    {"requests": [{"action": "確認", "kind": "explicit"}]},
    {"reply": {"kind": "acked"}},
    {"reply": [{"kind": "done"}, {"kind": "dne"}]},
])
def test_section_valid_rejects_misspelt_kinds(section):
    assert not extract_bench._section_valid(section)
    assert extract_bench._section_valid({"reply": None, "requests": []})
    # explicit null pins 'no kind' (module docstring), never a typo
    assert extract_bench._section_valid(
        {"requests": [{"action": "確認", "kind": None}]})


# U05-F06: a dropped item must fail, not become "expect nothing".
@pytest.mark.parametrize("case", [
    {"id": "bad-expect", "body": "合成",
     "expect": {"meds": [{"name": "合成薬", "status": "currnt"}],
                "urgency": "routine"}},
    {"id": "bad-forbid", "body": "合成", "expect": {},
     "forbid": {"meds": [{"name": "合成薬", "status": "currnt"}]}},
])
def test_mock_ok_rejects_dropped_expectation_items(tmp_path, case):
    import json
    from types import SimpleNamespace
    path = tmp_path / "synthetic.json"
    path.write_text(json.dumps({"cases": [case]}))
    assert extract_bench.cmd_run(SimpleNamespace(
        cases=str(path), out=None, tag="t", mock_ok=True)) == 1


def test_mock_ok_cli_runs_without_out():
    # U05-F07: the documented `run --mock-ok` usage works as written
    import subprocess
    script = Path(extract_bench.__file__)
    proc = subprocess.run([sys.executable, str(script), "run", "--mock-ok"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    bad = subprocess.run([sys.executable, str(script), "run"],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2 and "--out" in bad.stderr


def test_bench_scores_foreign_and_mixed_vitals():
    case = {'id': 'synthetic-vital', 'expect': {'vitals': {'sbp': 90}}}
    score = extract_bench._score_case(case, {'vitals': {'sbp': 90, 'dbp': 80}})
    assert score['fields']['vitals'] == {'tp': 1, 'fp': 1, 'fn': 0}
    assert extract_bench._score_case(case, None)['fields']['vitals']['fn'] == 1


def test_bench_symptom_subject_and_time_are_scored():
    assert not extract_bench._match_symptom(
        {'text': '発熱', 'subject': 'patient', 'status': 'ongoing'},
        [{'text': '発熱', 'subject': 'family', 'status': 'past', 'negated': False}])


def test_benchmark_records_cost_and_paired_corpus(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import extract_llm
    cases = tmp_path / 'synthetic.json'
    cases.write_text(json.dumps({'cases': [{
        'id': 's', 'body': '合成', 'expect': {},
        'context': '[医師] 合成コンテキスト', 'posted_at': '2026-09-01 10:00'}]}))
    # A wholly fictional approved corpus stands in for the checked-in assets;
    # separate regressions exercise the real allowlist and reject altered data.
    monkeypatch.setattr(extract_bench, '_synthetic_cases',
                        lambda: extract_bench._load_cases(str(cases)))
    seen = {}
    def fake(body, **kwargs):
        kwargs['meta_out'].update(calls=2, repairs=1, usage={'total_tokens': 12})
        seen.update(context=kwargs.get('context'),
                    posted_at=kwargs.get('posted_at'))
        return {}
    monkeypatch.setattr(extract_llm, 'llm_extract', fake)
    outputs = []
    for tag in ('before', 'after'):
        out = tmp_path / (tag + '.json')
        assert extract_bench.cmd_run(SimpleNamespace(
            cases=str(cases), out=str(out), tag=tag, mock_ok=False)) == 0
        outputs.append(str(out))
    report = json.loads(Path(outputs[0]).read_text())
    # #20 order 2: case context/posted_at reach llm_extract; the run
    # records what produced it (model + prompt head fingerprint)
    assert seen == {'context': '[医師] 合成コンテキスト',
                    'posted_at': '2026-09-01 10:00'}
    assert report['model'] == extract_llm.MODEL
    assert report['prompt_sha256'] == hashlib.sha256(
        (extract_llm._PROMPT_HEAD + extract_llm._CTX_HEAD).encode('utf-8')).hexdigest()
    assert report['reply_confusion'] == {}
    assert report['cases'][0]['performance']['calls'] == 2
    assert report['cases'][0]['performance']['usage']['total_tokens'] == 12
    assert report['performance']['p95_s'] is not None
    assert extract_bench.cmd_report(SimpleNamespace(files=outputs)) == 0
    report['corpus_sha256'] = 'different'
    Path(outputs[0]).write_text(json.dumps(report))
    assert extract_bench.cmd_report(SimpleNamespace(files=outputs)) == 2


@pytest.mark.parametrize(("durations", "expected"), [
    ([], {"p50_s": None, "p95_s": None}),
    ([12.5], {"p50_s": 12.5, "p95_s": 12.5}),
    ([9, 1, 4, 2], {"p50_s": 2, "p95_s": 9}),
    (list(range(20, 0, -1)), {"p50_s": 10, "p95_s": 19}),
])
def test_benchmark_latency_percentiles(durations, expected):
    assert extract_bench._duration_percentiles(durations) == expected
