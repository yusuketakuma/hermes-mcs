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
    # The visit anchor sits inside the detail instance's own
    # cue-to-predicate span — one event carrying two verbs.
    body = "本人は介護サービスを訪問し、開始しました。"
    quote = "介護サービスを訪問し、開始しました"
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


_CROSS_BODY = ("本人は介護サービスAを訪問し、2026年11月9日に開始しました。"
               "2026年12月10日に介護サービスBを開始しました。")
_CROSS_QUOTE = ("介護サービスAを訪問し、2026年11月9日に開始しました。"
                "2026年12月10日に介護サービスBを開始しました")


def _cross_item(detail):
    start = _CROSS_BODY.index(_CROSS_QUOTE)
    return {"kind": "visit", "subject": "patient", "status": "unknown",
            "negated": None, "evidence": _CROSS_QUOTE,
            "span": {"start": start, "end": start + len(_CROSS_QUOTE)},
            "care_detail": detail}


def test_care_detail_attributes_must_bind_to_the_anchor_instance():
    # Each attribute binds independently somewhere in the quote, but a
    # date from one instance paired with a provider from another is a
    # crossed pair — the detail describes neither event and is dropped.
    for detail in (
            {"event_type": "intervention_start", "workflow_status": "performed",
             "date_text": "2026年11月9日", "service_actor": "介護サービスB"},
            {"event_type": "intervention_start", "workflow_status": "performed",
             "date_text": "2026年12月10日", "service_actor": "介護サービスA"},
            # Both attributes bound — but only to the second instance,
            # which is not the event this visit record anchors.
            {"event_type": "intervention_start", "workflow_status": "performed",
             "date_text": "2026年12月10日", "service_actor": "介護サービスB"}):
        record = clinical_events.validate_record(_cross_item(detail), _CROSS_BODY)
        assert not record.get("unverified") and "care_detail" not in record
    # The pair bound to the anchor's own instance stays attached.
    good = {"event_type": "intervention_start", "workflow_status": "performed",
            "date_text": "2026年11月9日", "service_actor": "介護サービスA"}
    record = clinical_events.validate_record(_cross_item(good), _CROSS_BODY)
    assert not record.get("unverified")
    assert record["care_detail"]["date_text"] == "2026年11月9日"
    assert record["care_detail"]["service_actor"] == "介護サービスA"


def test_canonical_record_care_detail_attributes_share_the_anchor_instance():
    # The canonical quote carries its own subject — unlike the local path,
    # no surrounding body is available for axis resolution.
    quote = ("本人は介護サービスAを訪問し、2026年11月9日に開始しました。"
             "2026年12月10日に介護サービスBを開始しました")
    evidence = {"evidence_id": "e1", "message_id": "m1", "revision": "r1",
                "start": 0, "end": len(quote), "quote": quote,
                "atom_id": "a1"}

    def fact(detail):
        return v2_fact("f1", kind="care_event", statement=quote,
                       subject="patient:1", polarity="affirmed",
                       workflow_status="performed", evidence_ids=["e1"],
                       care_detail=detail)

    crossed = clinical_events.canonical_record(fact(
        {"event_type": "intervention_start", "workflow_status": "performed",
         "date_text": "2026年11月9日", "service_actor": "介護サービスB"}),
        evidence)
    assert not crossed.get("unverified") and "care_detail" not in crossed
    good = clinical_events.canonical_record(fact(
        {"event_type": "intervention_start", "workflow_status": "performed",
         "date_text": "2026年11月9日", "service_actor": "介護サービスA"}),
        evidence)
    assert not good.get("unverified")
    assert good["care_detail"]["service_actor"] == "介護サービスA"


def test_canonical_record_carries_fact_care_detail_when_verified():
    quote = "本人は介護サービスを訪問し、開始しました"
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


def test_neighbouring_same_type_event_carries_no_detail():
    # Same sentence is not the same instance: the visit and the service
    # start are separate predicates, so the verified visit record keeps no
    # detail even when the detail verifies against its own mention.
    body = "本人は訪問し、2026年9月1日に介護サービスを開始しました。"
    quote = "訪問し、2026年9月1日に介護サービスを開始しました"
    start = body.index(quote)
    item = {"kind": "visit", "subject": "patient", "status": "unknown",
            "negated": None, "evidence": quote,
            "span": {"start": start, "end": start + len(quote)},
            "care_detail": {"event_type": "intervention_start",
                            "workflow_status": "performed",
                            "date_text": "2026年9月1日",
                            "service_actor": "unknown"}}
    record = clinical_events.validate_record(item, body)
    assert not record.get("unverified") and "care_detail" not in record


def test_independent_acp_mentions_stay_two_instances():
    # A bare ACP cue and a compound mention with its own predicate are
    # independent instances — the bare mention must not absorb the
    # other's date and neither may claim the shared attribute.
    body = "本人はACPを実施し、終末期の話し合いを11月に行いました。"
    detail = {"event_type": "acp_discussion", "date_text": "11月"}
    assert clinical_events.verify_care_detail(detail, body) is None
    # A bare cue that genuinely explains one mention still merges: the
    # compound sits inside the bare instance's predicate span.
    merged = "本人はACPの意向を11月に確認しました。"
    out = clinical_events.verify_care_detail(detail, merged)
    assert out is not None and out["date_text"] == "11月"


@pytest.mark.parametrize("event_type,body,kept", [
    # A contract start is not a service-provision start, and ordinary
    # contract wording is not a contract-start event.
    ("service_contract_start", "本人はサービス契約を締結しました。", True),
    ("service_contract_start", "本人は訪問看護サービスを開始しました。", False),
    ("service_contract_start", "契約内容を確認しました。", False),
    ("intervention_start", "本人はサービス契約を開始しました。", False),
    # A short stay is respite care, never facility entry.
    ("respite", "本人は短期入所を利用しました。", True),
    ("facility_entry", "本人は施設に入所しました。", True),
    ("facility_entry", "本人は短期入所を利用しました。", False),
    # Joint guidance stays discharge-specific.
    ("discharge_joint_guidance", "本人は退院時共同指導を受けました。", True),
    ("discharge_joint_guidance", "本人は共同指導を受けました。", False),
    # DNAR alone is not an ACP discussion or a policy change.
    ("acp_discussion", "本人はDNARを希望しています。", False),
    ("acp_discussion", "本人とACPについて話し合いました。", True),
    ("care_policy_change", "本人はDNARとしました。", False),
    ("care_policy_change", "本人の看取り方針を変更しました。", True),
    # Noun-only cues still need the event predicate in the same sentence.
    ("pre_discharge_conference", "退院前カンファレンスを開催しました。", True),
    ("pre_discharge_conference", "退院前カンファレンスの資料です。", False),
    ("care_team_conference", "サービス担当者会議を開催しました。", True),
    ("care_team_conference", "担当者会議の議事録です。", False),
    # A predicate elsewhere in the sentence does not prove the event:
    # preparing materials or conducting other guidance is not holding the
    # conference itself.
    ("care_team_conference",
     "本人はサービス担当者会議の資料作成を実施しました。", False),
    ("pre_discharge_conference",
     "本人は退院前カンファレンスの資料を確認し、別の指導を実施しました。",
     False),
    ("discharge_joint_guidance",
     "本人は退院時共同指導の説明資料を確認しました。", False),
    # The event noun bound through an event nominalisation or the
    # guidance's own receipt does qualify.
    ("care_team_conference",
     "本人はサービス担当者会議の開催を実施しました。", True),
    ("care_team_conference",
     "本人はサービス担当者会議の開催が決まりました。", True),
    ("discharge_joint_guidance",
     "本人は退院時共同指導の実施を受けました。", True),
    ("acp_discussion", "本人はACPの意向を確認しました。", True),
    ("respite", "本人はショートステイのパンフレットを確認しました。", False),
    ("respite", "本人はショートステイを利用しました。", True),
    ("care_need_certification_change",
     "本人の要介護認定の結果が更新されました。", True),
    ("care_need_certification_change",
     "本人の要介護認定の案内が届きました。", False),
])
def test_care_detail_cue_semantics_reject_lookalikes(event_type, body, kept):
    out = clinical_events.verify_care_detail(
        {"event_type": event_type, "workflow_status": "performed"}, body)
    assert (out is not None) is kept


@pytest.mark.parametrize("event_type,body", [
    ("intervention_start", "本人は訪問看護サービスを開始しました。"),
    ("intervention_pause", "本人は訪問看護サービスを中断しました。"),
    ("intervention_resume", "本人は訪問看護サービスを再開しました。"),
    ("intervention_end", "本人は訪問看護サービスを終了しました。"),
    ("service_contract_start", "本人はサービス契約を締結しました。"),
    ("pre_discharge_conference", "退院前カンファレンスを開催しました。"),
    ("discharge_joint_guidance", "本人は退院時共同指導を受けました。"),
    ("care_team_conference", "サービス担当者会議を開催しました。"),
    ("care_plan_change", "本人のケアプランを変更しました。"),
    ("monitoring", "本人はモニタリングを実施しています。"),
    ("care_need_certification_change",
     "本人の要介護認定の結果が更新されました。"),
    ("respite", "本人は短期入所を利用しました。"),
    ("facility_entry", "本人は施設に入所しました。"),
    ("facility_exit", "本人は施設を退所しました。"),
    ("death", "本人が亡くなりました。"),
    ("acp_discussion", "本人とACPについて話し合いました。"),
    ("care_policy_change", "本人の看取り方針を変更しました。"),
])
def test_every_care_detail_type_has_a_verified_positive(event_type, body):
    assert set(clinical_events.CARE_DETAIL_TYPES) == {
        "intervention_start", "intervention_pause", "intervention_resume",
        "intervention_end", "service_contract_start",
        "pre_discharge_conference", "discharge_joint_guidance",
        "care_team_conference", "care_plan_change", "monitoring",
        "care_need_certification_change", "respite", "facility_entry",
        "facility_exit", "death", "acp_discussion", "care_policy_change"}
    out = clinical_events.verify_care_detail(
        {"event_type": event_type, "workflow_status": "performed"}, body)
    assert out is not None and out["event_type"] == event_type


def test_care_detail_attributes_bind_to_the_nearest_predicate():
    # A date belonging to a neighbouring event in the same sentence is not
    # adopted by this event type.
    mixed = "本人は11月にモニタリングを実施し、訪問看護を再開しました。"
    off = {"event_type": "intervention_resume", "date_text": "11月"}
    assert clinical_events.verify_care_detail(off, mixed) is None
    on = {"event_type": "intervention_resume", "date_text": "11月"}
    assert clinical_events.verify_care_detail(
        on, "本人は訪問看護を11月に再開しました。") is not None
    # An organization named only as a transfer/referral source (から/より)
    # is not the service actor.
    sourced = "本人はA病院からの紹介で訪問看護Bを再開しました。"
    actor = {"event_type": "intervention_resume", "service_actor": "A病院"}
    assert clinical_events.verify_care_detail(actor, sourced) is None
    provider = {"event_type": "intervention_resume", "service_actor": "訪問看護B"}
    assert clinical_events.verify_care_detail(provider, sourced) is not None


def test_care_detail_attributes_reject_ambiguous_same_type_instances():
    # Two instances of the same event type in one sentence leave the
    # attribute's instance unprovable — it is not adopted.
    pair = ("訪問看護Aを11月に開始し、介護保険の更新手続きを行った上で、"
            "訪問看護Bを12月に開始しました。")
    dated = {"event_type": "intervention_start", "date_text": "11月"}
    assert clinical_events.verify_care_detail(dated, pair) is None
    acted = {"event_type": "intervention_start", "service_actor": "訪問看護A"}
    assert clinical_events.verify_care_detail(acted, pair) is None
    # A single instance still binds, and instances in different sentences
    # stay independent.
    single = "訪問看護Aを11月に開始しました。訪問看護Bを12月に開始しました。"
    assert clinical_events.verify_care_detail(dated, single) is not None
    # acp compound cues describing one mention do not count as a second
    # instance.
    acp = "本人はACPの意向を11月に確認しました。"
    acp_dated = {"event_type": "acp_discussion", "date_text": "11月"}
    assert clinical_events.verify_care_detail(acp_dated, acp) is not None


def test_care_detail_rejects_material_work_as_the_event():
    # Preparing meeting or service materials is not holding the conference
    # or starting the service itself.
    conference = {"event_type": "care_team_conference",
                  "workflow_status": "performed"}
    assert clinical_events.verify_care_detail(
        conference,
        "本人はサービス担当者会議について資料作成を実施しました。") is None
    assert clinical_events.verify_care_detail(
        conference,
        "本人はサービス担当者会議の資料を作成しました。") is None
    assert clinical_events.verify_care_detail(
        conference,
        "本人はサービス担当者会議を開催しました。") is not None
    start = {"event_type": "intervention_start",
             "workflow_status": "performed"}
    assert clinical_events.verify_care_detail(
        start, "本人は訪問看護サービスの資料作成を開始しました。") is None
    assert clinical_events.verify_care_detail(
        start, "本人は訪問看護について資料作成を開始しました。") is None
    assert clinical_events.verify_care_detail(
        start, "本人は訪問看護サービスを開始しました。") is not None


def test_same_type_instances_bind_their_own_attributes():
    # Two separately quoted instances of one event type keep their own
    # explicit attributes; a crossed pairing or a quote spanning both
    # instances cannot prove either binding.
    body = ("本人は訪問看護を11月に開始しました。"
            "本人は訪問介護を12月に開始しました。")

    def record(quote, date_text):
        start = body.index(quote)
        return {"kind": "other", "subject": "patient", "status": "unknown",
                "negated": None, "evidence": quote,
                "span": {"start": start, "end": start + len(quote)},
                "care_detail": {"event_type": "intervention_start",
                                "workflow_status": "performed",
                                "date_text": date_text}}

    one = clinical_events.validate_record(
        record("訪問看護を11月に開始しました", "11月"), body)
    two = clinical_events.validate_record(
        record("訪問介護を12月に開始しました", "12月"), body)
    assert one["care_detail"]["date_text"] == "11月"
    assert two["care_detail"]["date_text"] == "12月"
    crossed = clinical_events.validate_record(
        record("訪問看護を11月に開始しました", "12月"), body)
    assert crossed.get("unverified") is True and "care_detail" not in crossed
    merged_quote = ("訪問看護を11月に開始しました。"
                    "本人は訪問介護を12月に開始しました")
    merged = clinical_events.validate_record(
        record(merged_quote, "11月"), body)
    assert merged.get("unverified") is True and "care_detail" not in merged


@pytest.mark.parametrize("body,quote,kind,subject,status,negated,clean", [
    # A normal multi-sentence quote verifies when the whole touched
    # context (quote sentences plus one neighbour each side) is clean.
    ("前の記録です。本人は2026年9月1日に退院しました。"
     "本人はその後通院しています。",
     "本人は2026年9月1日に退院しました。本人はその後通院しています。",
     "discharge", "patient", "current", False, True),
    # A correction in the neighbouring sentence still fails closed.
    ("本人は退院しました。本人はその後検査を受けました。"
     "訂正：退院の記載は誤りです。",
     "本人は退院しました。本人はその後検査を受けました。",
     "discharge", "patient", "current", False, False),
    # Third-party hearsay framing disqualifies the assertion.
    ("家族から本人が退院したと聞きました。本人は退院しました。",
     "本人は退院しました。", "discharge", "patient", "current", False, False),
    # An opposing same-subject mention in a neighbouring sentence is caught
    # by the polarity scan even without an explicit correction marker.
    ("本人は退院しました。本人は退院していません。",
     "本人は退院しました。", "discharge", "patient", "current", False, False),
    # Quotation marks anywhere in the touched context disqualify it.
    ("本人は退院しました。「確認済み」の記録です。",
     "本人は退院しました。", "discharge", "patient", "current", False, False),
    # A correction two or more sentences away still fails closed: the
    # whole bounded body is scanned for retraction markers.
    ("本人は退院しました。経過は安定しています。翌日の記録です。"
     "訂正：退院の記載は誤りです。",
     "本人は退院しました。", "discharge", "patient", "current", False, False),
    # An opposing same-subject mention far from the quote is caught by the
    # whole-body polarity scan.
    ("本人は退院しました。食事は全量摂取できています。"
     "夜間は良眠でした。翌朝の状態も安定しています。"
     "本人は退院していません。",
     "本人は退院しました。", "discharge", "patient", "current", False, False),
    # Third-party framing persisting from the body opening disqualifies a
    # quote sentences later.
    ("家族の話では、本人の調子を聞きました。検査は問題ありません。"
     "本人は退院しました。",
     "本人は退院しました。", "discharge", "patient", "current", False, False),
    # Unrelated clean sentences at any distance do not block the quote.
    ("前の記録です。本人は退院しました。経過観察を続けています。"
     "次回の予定を確認しました。退院後の生活について話し合いました。",
     "本人は退院しました。", "discharge", "patient", "current", False, True),
    # Distant quotation marks or uncertainty wording without attribution
    # or opposition are outside the asserted context's guarantee.
    ("「確認済み」の記録です。前置きです。間の文です。本人は退院しました。",
     "本人は退院しました。", "discharge", "patient", "current", False, True),
    ("検査値は不明です。前置きです。間の文です。本人は退院しました。",
     "本人は退院しました。", "discharge", "patient", "current", False, True),
])
def test_context_clean_bounds_quote_context(body, quote, kind, subject,
                                            status, negated, clean):
    start = body.index(quote)
    assert clinical_events.context_clean(
        body, start, start + len(quote), kind=kind, subject=subject,
        status=status, negated=negated) is clean


def test_context_clean_detail_anchor_compares_same_service_mentions():
    detail = {"event_type": "intervention_start",
              "workflow_status": "performed"}
    denied = "本人は訪問看護を開始しました。訪問看護は開始していません。"
    span = denied.index("訪問看護を開始しました")
    end = span + len("訪問看護を開始しました")
    assert clinical_events.context_clean(
        denied, span, end, kind="other", subject="patient", status="current",
        negated=False, detail=detail) is False
    other_service = "本人は訪問看護を開始しました。訪問介護は開始していません。"
    span = other_service.index("訪問看護を開始しました")
    end = span + len("訪問看護を開始しました")
    assert clinical_events.context_clean(
        other_service, span, end, kind="other", subject="patient",
        status="current", negated=False, detail=detail) is True
