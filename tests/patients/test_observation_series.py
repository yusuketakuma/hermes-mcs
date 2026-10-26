"""Synthetic observations retain source, subject, date and uncertainty."""
from datetime import datetime, timezone
from contextlib import closing
from dataclasses import replace

import pytest

import observation_series
import patient_context_refresh
import patient_context_view
from patient_context import extract_context
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of


def _item(mid, field, value, *, posted, subject="patient", group=0, unit_text=None):
    quote = f"{field}: {value}" if unit_text is None else unit_text
    return {"project_id": 1, "category": "observations", "source": "chat", "message_id": mid,
            "content_hash": f"{mid:064x}", "posted_at_ts": posted, "subject": subject,
            "_heading_group": group, "quote": quote, "text": quote,
            "details": [{"key": field, "value": value, "evidence": quote}]}


def _block(mid, day, *, unit="mg/dL", condition="空腹", subject="patient", value="1.0", posted=None):
    posted = posted if posted is not None else datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp()
    fields = [("observation_name", "Cr"), ("value", value), ("unit", unit),
              ("condition", condition), ("measured_on", day)]
    owner = "本人" if subject == "patient" else "家族"
    quote = f"{owner}は{day}の{condition}時の採血 Cr {value} {unit}。"
    item = _item(mid, "observation_name", "Cr", posted=posted, subject=subject, unit_text=quote)
    item["details"] = [{"key": key, "value": val, "evidence": quote} for key, val in fields if val is not None]
    return [item]


def _view(items, coverage=None, *, source_bodies=None):
    if source_bodies is None:
        source_bodies = {(item["message_id"], item["content_hash"]): item["quote"] for item in items}
    return observation_series.observation_series({"project_id": 1, "items": items, "coverage": coverage},
                                                 source_bodies=source_bodies)


def test_series_keeps_posting_order_source_values_and_measured_date_separate():
    old = _block(1, "2026-09-01", posted=datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    new = _block(2, "2026-10-02", value="1.5")
    coverage = {"inference": {"complete": False, "sources": {"pending": 1}}}
    result = _view(list(reversed(new)) + list(reversed(old)), coverage)
    [series] = result["series"]
    assert (series["subject"], series["name"], series["unit"], series["condition"]) == (
        "patient", "Cr", "mg/dL", "空腹")
    assert [record["event_date_text"] for record in series["records"]] == ["2026-09-01", "2026-10-02"]
    assert [record["items"][0]["message_id"] for record in series["records"]] == [1, 2]
    assert series["records"][0]["posted_at_ts"] != series["records"][0]["event_date_text"]
    assert result["coverage"] is coverage and result["order_basis"] == "posting_time_within_series"
    assert result["unconfirmed"] == []


def test_interleaved_series_are_ordered_within_each_condition():
    base = datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp()
    rows = [_block(mid, "2026-10-01", condition=condition, posted=base + mid)[0]
            for mid, condition in ((1, "空腹"), (2, "食後"), (3, "空腹"), (4, "食後"))]
    result = _view(list(reversed(rows)))
    grouped = {series["condition"]: [record["items"][0]["message_id"] for record in series["records"]]
               for series in result["series"]}
    assert grouped == {"空腹": [1, 3], "食後": [2, 4]}
    assert result["order_basis"] == "posting_time_within_series"


def test_subject_unit_and_condition_never_mix_and_foreign_patient_is_rejected():
    patient = _block(1, "2026-10-01")
    other_unit = _block(2, "2026-10-01", unit="μmol/L")
    other_condition = _block(3, "2026-10-01", condition="食後")
    family = _block(4, "2026-10-01", subject="family")
    foreign = _block(5, "2026-10-01")
    for item in foreign:
        item["project_id"] = 2
    result = _view(list(reversed(patient + other_unit + other_condition + family + foreign)))
    assert {(series["unit"], series["condition"]) for series in result["series"]} == {
        ("mg/dL", "空腹"), ("μmol/L", "空腹"), ("mg/dL", "食後")}
    assert all(record["references"][0]["message_id"] == 4 for record in result["unconfirmed"])
    assert result["scope_rejected"] == len(foreign)
    assert "0000000000000000000000000000000000000000000000000000000000000005" not in str(result)


def test_measurement_and_observation_dates_are_not_merged_as_the_same_axis():
    measured = _block(1, "2026-10-01")
    observed = _block(2, "2026-10-01")
    date_detail = next(detail for detail in observed[0]["details"] if detail["key"] == "measured_on")
    date_detail["key"] = "observed_on"
    result = _view(list(reversed(measured + observed)))
    assert {series["event_date_kind"] for series in result["series"]} == {"measured_on", "observed_on"}
    assert all(len(series["records"]) == 1 for series in result["series"])


@pytest.mark.parametrize("change", ["missing_condition", "missing_unit", "multiple_values", "missing_date",
                                     "multiple_dates", "future", "negative", "unverified", "subject_conflict"])
def test_ambiguous_or_unconfirmed_records_are_retained_without_a_series(change):
    items = _block(1, "2026-10-01")
    if change == "missing_condition":
        items[0]["details"] = [detail for detail in items[0]["details"] if detail["key"] != "condition"]
    elif change == "missing_unit":
        items[0]["details"] = [detail for detail in items[0]["details"] if detail["key"] != "unit"]
    elif change == "multiple_values":
        items[0]["details"].append({"key": "value", "value": "2.0", "evidence": items[0]["quote"]})
    elif change == "missing_date":
        items[0]["details"] = [detail for detail in items[0]["details"] if detail["key"] != "measured_on"]
    elif change == "multiple_dates":
        items[0]["details"].append({"key": "observed_on", "value": "2026-10-02", "evidence": items[0]["quote"]})
    elif change == "future":
        items = _block(1, "2026-10-05")
    elif change == "negative":
        items[0]["quote"] = "この値ではありません"
    elif change == "unverified":
        items[0]["unverified"] = True
    else:
        items[0]["reported_subject"] = "family"
    result = _view(list(reversed(items)), {"inference": {"complete": False}})
    assert result["series"] == []
    assert sum(len(record["references"]) for record in result["unconfirmed"]) == len(items)
    assert all(record["relation"] == "unknown" for record in result["unconfirmed"])
    assert result["coverage"]["inference"]["complete"] is False


def test_unknown_or_nonunique_owner_is_not_joined_to_other_assertions():
    source = _block(1, "2026-10-01")[0]
    items = [_item(1, detail["key"], detail["value"], posted=source["posted_at_ts"]) for detail in source["details"]]
    for item in items:
        item.pop("_heading_group")
    result = _view(items)
    assert result["series"] == []
    assert len(result["unconfirmed"]) == len(items)


def test_conflicting_relative_day_is_not_used_as_a_series_date():
    items = _block(1, "2026-09-01")
    date_item = items[0]
    date_item["quote"] = date_item["quote"].replace("2026-09-01", "2026-09-01（昨日）")
    for detail in date_item["details"]:
        detail["evidence"] = date_item["quote"]
    result = _view(list(reversed(items)))
    assert result["series"] == []
    assert len(result["unconfirmed"]) == 1
    assert result["unconfirmed"][0]["references"][0]["message_id"] == date_item["message_id"]
    assert date_item["quote"] not in str(result)


def test_condition_from_a_different_sentence_cannot_be_attached_to_measurement():
    item = _block(1, "2026-10-01")[0]
    item["quote"] += "別の検査は食後。"
    for detail in item["details"]:
        detail["evidence"] = item["quote"]
        if detail["key"] == "condition":
            detail["value"] = "食後"
    result = _view([item])
    assert result["series"] == [] and result["unconfirmed"]
    assert "食後" not in str(result)


def test_original_body_must_be_supplied_and_match_the_complete_single_sentence():
    item = _block(1, "2026-10-01")[0]
    ev = {"project_id": 1, "items": [item], "coverage": {"inference": {"complete": False}}}
    key = (item["message_id"], item["content_hash"])
    for supplied in (None, {}, {(1, "0" * 64): item["quote"]},
                     {key: "家族の話です。" + item["quote"]}, {key: item["quote"] + "\n別の文。"}):
        result = observation_series.observation_series(ev, source_bodies=supplied)
        assert result["series"] == [] and result["unconfirmed"]
        assert item["quote"] not in str(result)
    item["reference_context"] = {"text": "合成の借用原文は返さない"}
    result = observation_series.observation_series(ev, source_bodies={key: item["quote"]})
    assert result["series"]
    assert "合成の借用原文は返さない" not in str(result)


@pytest.mark.parametrize("posted", [None, True, "1790000000", float("inf"), float("nan"), 10 ** 1000])
def test_unconfirmed_source_reference_does_not_expose_an_invalid_post_time(posted):
    item = _block(1, "2026-10-01")[0]
    item["posted_at_ts"] = posted
    result = _view([item])
    assert result["series"] == []
    [reference] = result["unconfirmed"][0]["references"]
    assert reference["posted_at_ts"] is None and reference["message_id"] == 1
    assert item["quote"] not in str(result)


def test_unconfirmed_source_reference_keeps_a_valid_post_time():
    item = _block(1, "2026-10-01")[0]
    item["subject"] = "family"
    result = _view([item])
    assert result["unconfirmed"][0]["references"][0]["posted_at_ts"] == item["posted_at_ts"]
    assert item["quote"] not in str(result)


@pytest.mark.parametrize("body,expected", [
    ("家族の話です。2026-10-01の空腹時の採血 Cr 1.0 mg/dL。", False),
    ("本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。", True),
    ("本人は家族と一緒に2026-10-01の空腹時の採血 Cr 1.0 mg/dL。", False),
])
def test_actual_evidence_and_refresh_require_explicit_patient_bound_to_measurement(tmp_path, monkeypatch, body, expected):
    with closing(_db(tmp_path, ())) as db:
        db.save_messages([replace(_message(1, body=body), posted_at="2026-10-03T09:00:00+09:00")], project_id=1)
        details = [{"key": key, "value": value, "evidence": body} for key, value in (
            ("observation_name", "Cr"), ("value", "1.0"), ("unit", "mg/dL"),
            ("condition", "空腹"), ("measured_on", "2026-10-01"))]
        item = {"category": "observations", "subject": "patient", "text": body, "evidence": body, "details": details}
        _artifact(db, "extract_llm", 1, {"patient_context": [item]}, {"hash": _hash_of(db, 1)})
        source = {**dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()), "source": "chat"}
        facts = patient_context_refresh._facts(source, [item], night_id="2026-10-03", discovery="new_report")
        assert facts and patient_context_refresh._validated_facts(source, facts) == facts
        monkeypatch.setattr(patient_context_refresh, "context_view_input", lambda *a, **k: {
            "facts": facts, "as_of": source["posted_at_ts"], "summary": [], "coverage": {}})
        monkeypatch.setattr(patient_context_refresh, "inference_coverage", lambda *a, **k: {"complete": False})
        ev = patient_context_view.evidence(db.db, 1)
        result = observation_series.observation_series(
            ev, source_bodies={(1, source["content_hash"]): body})
        assert bool(result["series"]) is expected
        if not expected:
            assert result["unconfirmed"]
            assert body not in str(result)


def test_actual_split_headings_do_not_transfer_a_prior_person_to_a_measurement(tmp_path, monkeypatch):
    body = "家族の話です。\n検査項目: Cr\n測定値: 1.0\n単位: mg/dL\n測定条件: 空腹\n測定日: 2026-10-01"
    with closing(_db(tmp_path, ())) as db:
        db.save_messages([replace(_message(1, body=body), posted_at="2026-10-03T09:00:00+09:00")], project_id=1)
        items = extract_context(body)
        _artifact(db, "extract_v1", 1, {"patient_context": items}, {"hash": _hash_of(db, 1)})
        source = {**dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()), "source": "chat"}
        facts = patient_context_refresh._facts(source, items, night_id="2026-10-03", discovery="new_report")
        assert facts and patient_context_refresh._validated_facts(source, facts) == facts
        monkeypatch.setattr(patient_context_refresh, "context_view_input", lambda *a, **k: {
            "facts": facts, "as_of": source["posted_at_ts"], "summary": [], "coverage": {}})
        monkeypatch.setattr(patient_context_refresh, "inference_coverage", lambda *a, **k: {"complete": False})
        result = observation_series.observation_series(patient_context_view.evidence(db.db, 1),
            source_bodies={(1, source["content_hash"]): body})
        assert result["series"] == []
        assert result["unconfirmed"]


def test_real_evidence_cannot_strip_a_prior_family_sentence(tmp_path, monkeypatch):
    body = "家族の話です。本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。"
    quote = body.split("。", 1)[1]
    with closing(_db(tmp_path, ())) as db:
        db.save_messages([replace(_message(1, body=body), posted_at="2026-10-03T09:00:00+09:00")], project_id=1)
        details = [{"key": key, "value": value, "evidence": quote} for key, value in (
            ("observation_name", "Cr"), ("value", "1.0"), ("unit", "mg/dL"),
            ("condition", "空腹"), ("measured_on", "2026-10-01"))]
        item = {"category": "observations", "subject": "patient", "text": quote, "evidence": quote, "details": details}
        _artifact(db, "extract_llm", 1, {"patient_context": [item]}, {"hash": _hash_of(db, 1)})
        source = {**dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()), "source": "chat"}
        facts = patient_context_refresh._facts(source, [item], night_id="2026-10-03", discovery="new_report")
        assert facts and patient_context_refresh._validated_facts(source, facts) == facts
        monkeypatch.setattr(patient_context_refresh, "context_view_input", lambda *a, **k: {
            "facts": facts, "as_of": source["posted_at_ts"], "summary": [], "coverage": {}})
        monkeypatch.setattr(patient_context_refresh, "inference_coverage", lambda *a, **k: {"complete": False})
        ev = patient_context_view.evidence(db.db, 1)
        assert any(row.get("quote") == quote for row in ev["items"])
        result = observation_series.observation_series(ev, source_bodies={(1, source["content_hash"]): body})
        assert result["series"] == [] and result["unconfirmed"]
        assert body not in str(result) and quote not in str(result)


def test_invalid_project_scope_does_not_echo_sources():
    items = _block(1, "2026-10-01")
    result = observation_series.observation_series({"items": items, "coverage": {"unknown": True}})
    assert result["scope_valid"] is False
    assert result["scope_rejected"] is None
    assert result["series"] == result["unconfirmed"] == []
