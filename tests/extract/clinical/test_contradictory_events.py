"""Synthetic opposing event claims must not become a confirmed transition."""

import pytest

import clinical_events
import extract_validation
from semantic_projection import project_v2_doc_legacy
from semantic_testkit import v2_doc, v2_fact


@pytest.mark.parametrize("body,kind,cue", [
    ("本人は退院しましたが、退院していません。", "discharge", "退院"),
    ("本人は退院しましたが退院していません。", "discharge", "退院"),
    ("本人は退院しました。実際には退院していません。", "discharge", "退院"),
    ("本人は退院しました。しかし退院していません。", "discharge", "退院"),
    ("本人は入院しましたが、入院していません。", "admission", "入院"),
    ("本人は訪問しましたが、訪問していません。", "visit", "訪問"),
])
def test_opposite_same_subject_and_time_hold_each_original_assertion(body, kind, cue):
    quotes = (f"{cue}しました", f"{cue}していません")
    records = []
    for quote in quotes:
        start = body.index(quote)
        record = clinical_events.validate_record({"kind": kind, "evidence": quote,
            "span": {"start": start, "end": start + len(quote)}}, body)
        assert record["subject"] == "patient" and record["status"] == "unknown"
        assert record["negated"] is None and record["unverified"] is True
        assert record["evidence"] == quote
        assert body[record["span"]["start"]:record["span"]["end"]] == quote
        records.append(record)
    projected = extract_validation.validate({"events": [kind], "event_records": records}, body)
    assert kind not in projected["events"]
    assert len(projected["event_records"]) == 2


@pytest.mark.parametrize("first,second", [("退院", "出院"), ("出院", "退院")])
def test_discharge_synonyms_keep_opposing_patient_statements_unverified(first, second):
    body = f"本人は本日{first}しましたが、{second}していません。"
    records = []
    for quote in (f"{first}しました", f"{second}していません"):
        start = body.index(quote)
        record = clinical_events.validate_record({"kind": "discharge", "evidence": quote,
            "span": {"start": start, "end": start + len(quote)}}, body)
        assert (record["subject"], record["status"], record["negated"]) == ("patient", "unknown", None)
        assert record["unverified"] is True and record["evidence"] == quote
        records.append(record)
    result = extract_validation.validate({"events": ["discharge"], "event_records": records}, body)
    assert "discharge" not in result["events"] and len(result["event_records"]) == 2


@pytest.mark.parametrize("body", [
    "本人は本日退院しましたが、退所していません。",
    "本人は本日退院しましたが、家族は出院していません。",
])
def test_discharge_synonyms_do_not_merge_different_events_or_subjects(body):
    quote = "退院しました"
    start = body.index(quote)
    record = clinical_events.validate_record({"kind": "discharge", "evidence": quote,
        "span": {"start": start, "end": start + len(quote)}}, body)
    assert (record["subject"], record["status"], record["negated"]) == ("patient", "current", False)
    assert not record.get("unverified") and record["evidence"] == quote


@pytest.mark.parametrize("count,expected_status", [(2, "current"), (3, "unknown")])
def test_event_candidate_cap_holds_without_discarding_original_evidence(monkeypatch, count, expected_status):
    monkeypatch.setattr(clinical_events, "_EVENT_CANDIDATE_LIMIT", 2)
    body = "本人は" + "、".join(["退院しました"] * count) + "。"
    quote = "退院しました"
    start = body.index(quote)
    proposed = {"kind": "discharge", "evidence": quote,
                "span": {"start": start, "end": start + len(quote)}}
    record = clinical_events.validate_record(proposed, body)
    assert record["status"] == expected_status and record["evidence"] == quote
    if expected_status == "unknown":
        assert record["negated"] is None and record["unverified"] is True
        result = extract_validation.validate({"events": ["discharge"], "event_records": [proposed]}, body)
        assert "discharge" not in result["events"]
        assert result["event_records"][0]["evidence"] == quote
    else:
        assert record["negated"] is False and not record.get("unverified")


def test_different_subject_and_different_kind_are_not_collapsed():
    body = "本人は退院しましたが、家族は退院していません。本人は訪問していません。"
    quote = "退院しました"
    start = body.index(quote)
    record = clinical_events.validate_record({"kind": "discharge", "evidence": quote,
        "span": {"start": start, "end": start + len(quote)}}, body)
    assert (record["subject"], record["status"], record["negated"]) == ("patient", "current", False)
    assert not record.get("unverified")


def test_explicit_past_and_current_are_not_a_same_time_conflict():
    body = "本人は以前退院しましたが、現在は退院していません。"
    quote = "退院していません"
    start = body.index(quote)
    record = clinical_events.validate_record({"kind": "discharge", "evidence": quote,
        "span": {"start": start, "end": start + len(quote)}}, body)
    assert (record["subject"], record["status"], record["negated"]) == ("patient", "current", True)
    assert not record.get("unverified")


def test_canonical_quote_with_opposing_claims_does_not_emit_event():
    body = "本人は本日退院しましたが、退院していません。"
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1", "start": 0,
                "end": len(body), "quote": body, "atom_id": "a1"}
    fact = v2_fact("f1", kind="care_event", statement=body, subject="patient:1",
                   polarity="affirmed", workflow_status="done", valid_time="current", evidence_ids=["e1"])
    result = project_v2_doc_legacy(v2_doc([fact], [evidence]))
    record, = result["event_records"]
    assert record["subject"] == "patient" and record["status"] == "unknown"
    assert record["negated"] is None and record["unverified"] is True
    assert record["evidence"] == body and record["span"] == {"start": 0, "end": len(body)}
    assert "discharge" not in result.get("events", [])


@pytest.mark.parametrize("first,second", [("退院", "出院"), ("出院", "退院")])
def test_canonical_discharge_synonyms_do_not_emit_conflicting_event(first, second):
    body = f"本人は本日{first}しましたが、{second}していません。"
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1", "start": 0,
                "end": len(body), "quote": body, "atom_id": "a1"}
    fact = v2_fact("f1", kind="care_event", statement=body, subject="patient:1",
                   polarity="affirmed", workflow_status="done", valid_time="current", evidence_ids=["e1"])
    result = project_v2_doc_legacy(v2_doc([fact], [evidence]))
    record, = result["event_records"]
    assert record["status"] == "unknown" and record["negated"] is None and record["unverified"] is True
    assert record["evidence"] == body and "discharge" not in result.get("events", [])


@pytest.mark.parametrize("quote", ["本日退院しました。", "本日退院しましたが、退院していません。"])
def test_canonical_quote_without_explicit_subject_never_inherits_patient(quote):
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1", "start": 0,
                "end": len(quote), "quote": quote, "atom_id": "a1"}
    fact = v2_fact("f1", kind="care_event", statement=quote, subject="patient:1",
                   polarity="affirmed", workflow_status="done", valid_time="current", evidence_ids=["e1"])
    result = project_v2_doc_legacy(v2_doc([fact], [evidence]))
    record, = result["event_records"]
    assert record["subject"] == "unknown" and record["unverified"] is True
    assert record["evidence"] == quote and "discharge" not in result.get("events", [])


def test_explicit_family_subject_remains_family_in_canonical_event():
    quote = "家族は本日退院しました。"
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1", "start": 0,
                "end": len(quote), "quote": quote, "atom_id": "a1"}
    fact = v2_fact("f1", kind="care_event", statement=quote, subject="role:family",
                   polarity="affirmed", workflow_status="done", valid_time="current", evidence_ids=["e1"])
    result = project_v2_doc_legacy(v2_doc([fact], [evidence]))
    record, = result["event_records"]
    assert (record["subject"], record["status"], record["negated"]) == ("family", "current", False)
    assert not record.get("unverified") and "discharge" not in result.get("events", [])


@pytest.mark.parametrize("quote,expected", [
    ("本人は退院しましたが、退院していません。", None),
    ("本人は退院しました、退院しました。", False),
    ("本人は退院されました。", False),
])
def test_ambiguous_polarity_does_not_fall_back_to_affirmative(quote, expected):
    start = quote.index("退院")
    assert clinical_events._polarity(quote, start, len(quote), start + len("退院"), "current") is expected


def test_verify_care_detail_binds_fields_to_source():
    body = "本人は訪問看護Aの訪問看護を11月9日に再開しました。"
    detail = {"event_type": "intervention_resume", "workflow_status": "performed",
              "date_text": "11月9日", "service_actor": "訪問看護A"}
    out = clinical_events.verify_care_detail(detail, body)
    assert out == {"event_type": "intervention_resume", "workflow_status": "performed",
                   "date_text": "11月9日", "service_actor": "訪問看護A"}
    for bad in ({"event_type": "not_a_type"}, {"event_type": "intervention_resume",
                "date_text": "12月1日"}, {"event_type": "intervention_resume",
                "service_actor": "存在しない事業所"}, "not_a_dict", None):
        assert clinical_events.verify_care_detail(bad, body) is None
    # The event_type's own cue must appear: a resume sentence cannot carry
    # a monitoring claim, and an unsupported type is rejected outright.
    assert clinical_events.verify_care_detail({"event_type": "monitoring"}, body) is None
    monitored = "本人は月1回モニタリングを実施しています。"
    sparse = clinical_events.verify_care_detail({"event_type": "monitoring"}, monitored)
    assert sparse == {"event_type": "monitoring", "workflow_status": "unknown",
                      "date_text": "unknown", "service_actor": "unknown"}
    # Attributes from a different sentence are not adopted.
    split = "9月1日に退院しました。訪問看護を再開しました。"
    off = {"event_type": "intervention_resume", "date_text": "9月1日"}
    assert clinical_events.verify_care_detail(off, split) is None


def test_verified_record_keeps_source_bound_care_detail():
    body = "本人は訪問し、サービスを開始しました。"
    quote = "訪問し、サービスを開始しました"
    start = body.index(quote)
    item = {"kind": "visit", "subject": "patient", "status": "unknown", "negated": None,
            "evidence": quote, "span": {"start": start, "end": start + len(quote)},
            "care_detail": {"event_type": "intervention_start",
                            "workflow_status": "performed", "date_text": None,
                            "service_actor": None}}
    record = clinical_events.validate_record(item, body)
    assert not record.get("unverified")
    assert record["care_detail"]["event_type"] == "intervention_start"
    assert record["care_detail"]["workflow_status"] == "performed"


def test_other_kind_record_verifies_via_care_detail_cue():
    body = "本人は訪問看護サービスを再開しました。"
    quote = "訪問看護サービスを再開しました"
    start = body.index(quote)
    item = {"kind": "other", "subject": "patient", "status": "unknown", "negated": None,
            "evidence": quote, "span": {"start": start, "end": start + len(quote)},
            "care_detail": {"event_type": "intervention_resume"}}
    record = clinical_events.validate_record(item, body)
    assert record["kind"] == "other" and not record.get("unverified")
    assert record["care_detail"]["event_type"] == "intervention_resume"


def test_unverified_or_bad_detail_never_attaches_care_detail():
    body = "本人は訪問しました。"
    quote = "訪問しました"
    start = body.index(quote)
    base = {"kind": "visit", "subject": "patient", "status": "unknown", "negated": None,
            "evidence": quote, "span": {"start": start, "end": start + len(quote)}}
    bad_type = dict(base, care_detail={"event_type": "bogus"})
    assert "care_detail" not in clinical_events.validate_record(bad_type, body)
    # A verified parent record does not adopt an unfounded detail: a single
    # visit is not evidence that an intervention started.
    unfounded = dict(base, care_detail={"event_type": "intervention_start"})
    record = clinical_events.validate_record(unfounded, body)
    assert not record.get("unverified") and "care_detail" not in record
    other = dict(base, kind="other",
                 care_detail={"event_type": "intervention_resume"})
    assert clinical_events.validate_record(other, body).get("unverified") is True


def test_care_detail_workflow_conflict_stays_unknown():
    body = "本人は来週訪問看護サービスを開始する予定です。"
    quote = "訪問看護サービスを開始する予定です"
    start = body.index(quote)
    item = {"kind": "visit", "subject": "patient", "status": "unknown", "negated": None,
            "evidence": quote, "span": {"start": start, "end": start + len(quote)},
            "care_detail": {"event_type": "intervention_start",
                            "workflow_status": "performed"}}
    record = clinical_events.validate_record(item, body)
    assert not record.get("unverified")
    assert record["care_detail"]["workflow_status"] == "unknown"


def test_canonical_record_carries_fact_care_detail_when_verified():
    quote = "本人は本日訪問し、サービスを開始しました"
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1", "start": 0,
                "end": len(quote), "quote": quote, "atom_id": "a1"}
    fact = v2_fact("f1", kind="care_event", statement=quote, subject="patient:1",
                   polarity="affirmed", workflow_status="performed",
                   evidence_ids=["e1"], care_detail={"event_type": "intervention_start",
                   "workflow_status": "performed", "date_text": None, "service_actor": None})
    record = clinical_events.canonical_record(fact, evidence)
    assert not record.get("unverified")
    assert record["care_detail"]["event_type"] == "intervention_start"


def test_canonical_record_rejects_unfounded_care_detail():
    quote = "本人は本日訪問しました"
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1", "start": 0,
                "end": len(quote), "quote": quote, "atom_id": "a1"}
    fact = v2_fact("f1", kind="care_event", statement=quote, subject="patient:1",
                   polarity="affirmed", workflow_status="performed",
                   evidence_ids=["e1"], care_detail={"event_type": "intervention_start",
                   "workflow_status": "performed"})
    record = clinical_events.canonical_record(fact, evidence)
    assert not record.get("unverified") and "care_detail" not in record


def test_normalized_care_detail_is_idempotent_under_revalidation():
    body = "本人は本日訪問し、サービスを開始しました。"
    once = clinical_events.verify_care_detail(
        {"event_type": "intervention_start", "workflow_status": "performed"}, body)
    assert once["date_text"] == "unknown" and once["service_actor"] == "unknown"
    twice = clinical_events.verify_care_detail(once, body)
    assert twice == once
