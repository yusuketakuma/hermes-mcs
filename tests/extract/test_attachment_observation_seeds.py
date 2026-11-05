"""OCR measurements seed context without inventing subject, unit, date or certainty."""
import copy

import pytest

import attachment_observation_seeds
import observation_series


def _source(body):
    return {"source": "attachment_ocr", "body_text": body, "content_hash": "a" * 64,
            "project_id": 1, "message_id": 0, "artifact_id": 10, "posted_at": None}


def _lab(quote, **updates):
    return {"name": "Cr", "value": "1.0", "unit": "mg/dL", "evidence": quote,
            "subject": "patient", "status": "current", **updates}


def _details(seed):
    return {row["key"]: row["value"] for row in seed["details"]}


@pytest.mark.parametrize("day", ["2026-10-01", "2026/10/1", "2026年10月1日"])
def test_laboratory_details_keep_the_written_date_and_do_not_mutate_input(day):
    body = f"本人は{day}の採血 Cr 1.0 mg/dL。"
    document = {"labs": [_lab(body)], "patient_context": []}
    before = copy.deepcopy(document)
    [seed] = attachment_observation_seeds.seeds(document, _source(body))
    assert seed["category"] == "observations" and seed["subject"] == "patient"
    assert seed["text"] == seed["evidence"] == body
    assert _details(seed) == {"observation_name": "Cr", "value": "1.0", "unit": "mg/dL", "measured_on": day}
    assert all(detail["evidence"] == body for detail in seed["details"])
    assert document == before


def test_flat_vital_keeps_the_explicit_patient_date_label_and_unit():
    body = "本人は2026年10月2日に測定した体温36.4℃。"
    [seed] = attachment_observation_seeds.seeds({"vitals": {"bt": 36.4}}, _source(body))
    assert _details(seed) == {"observation_name": "体温", "value": "36.4", "unit": "℃", "measured_on": "2026年10月2日"}


def test_seed_reaches_existing_series_without_relaxing_whole_original_validation():
    body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
    [seed] = attachment_observation_seeds.seeds({"labs": [_lab(body)]}, _source(body))
    item = {**_source(body), **seed, "quote": seed["evidence"], "source_key": "attachment_ocr:10:1",
            "source_binding": "b" * 64, "source_artifact_id": 10, "page": 1, "posted_at_ts": None}
    identity = (item["source_key"], item["content_hash"])
    result = observation_series.observation_series({"project_id": 1, "items": [item]}, source_bodies={identity: body})
    assert len(result["series"]) == 1 and not result["unconfirmed"]
    assert result["series"][0]["records"][0]["event_date_text"] == "2026-10-01"
    incomplete = observation_series.observation_series({"project_id": 1, "items": [item]},
                                                      source_bodies={identity: body + "別段落。"})
    assert not incomplete["series"] and len(incomplete["unconfirmed"]) == 1


@pytest.mark.parametrize("change", [
    {"subject": "family"}, {"subject": "other"}, {"status": "planned"}, {"unverified": True},
    {"unit": None}, {"unit": "錠"}, {"value": "9.0"}, {"evidence": "Cr 1.0 mg/dL"},
    {"measured_on": "2025-01-01"},
])
def test_unsupported_lab_fields_never_seed_a_measurement(change):
    body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
    assert attachment_observation_seeds.seeds({"labs": [_lab(body, **change)]}, _source(body)) == []


@pytest.mark.parametrize("body", [
    "本人は採血 Cr 1.0 mg/dL。", "本人は10月1日の採血 Cr 1.0 mg/dL。",
    "本人は2026-10-01の採血 Cr 1.0 mg/dLではない。",
    "本人は2026-10-01の採血 Cr 1.0 mg/dLの予定。",
    "家族は2026-10-01の採血 Cr 1.0 mg/dL。",
    "2026-10-01の採血 Cr 1.0 mg/dL。",
    "本人は2026-10-01の採血 Cr 1.0 mg/dL。これは誤記です。",
])
def test_ambiguous_date_owner_negation_plan_and_correction_do_not_seed(body):
    assert attachment_observation_seeds.seeds({"labs": [_lab(body)]}, _source(body)) == []


@pytest.mark.parametrize("body", [
    "本人の体温36.4℃。", "本人は2026-10-02に測定した体温36.4。",
    "家族は2026-10-02に測定した体温36.4℃。",
    "本人は2026-10-02に測定した体温36.4℃。その値は誤りです。",
])
def test_incomplete_or_retracted_vital_is_not_a_measurement(body):
    assert attachment_observation_seeds.seeds({"vitals": {"bt": 36.4}}, _source(body)) == []


@pytest.mark.parametrize(('body', 'kind'), [
    ('本人は文書作成日2026-10-01の書類に採血 Cr 1.0 mg/dLと記載。', 'lab'),
    ('本人は生年月日1950-01-01、体温36.4℃。', 'vital'),
    ('本人は予約日2026-10-01の採血 Cr 1.0 mg/dL。', 'lab'),
])
def test_other_date_roles_are_not_transferred_to_measured_on(body, kind):
    from measurement_sources import _labs, _vitals

    source = _source(body)
    document = {'labs': [_lab(body)]} if kind == 'lab' else {'vitals': {'bt': 36.4}}
    records, _ = (_labs(source, document, source['artifact_id'], None, True) if kind == 'lab'
                  else _vitals(source, document, source['artifact_id'], True))
    assert len(records) == 1 and records[0]['measured_on'] is None
    assert attachment_observation_seeds.seeds(document, source) == []


@pytest.mark.parametrize('label', ['作成日', '発行日', '生年月日', '誕生日', '予約日', '予定日', '投稿日時'])
def test_common_measurement_date_guard_keeps_metadata_dates_out_of_series(label):
    import clinical_values

    body = f'本人は{label}2026-10-01に測定した体温36.4℃。'
    assert clinical_values.written_day(body)[1].isoformat() == '2026-10-01'
    assert clinical_values.measurement_day(body) is None
    details = {'observation_name': '体温', 'value': '36.4', 'unit': '℃', 'measured_on': '2026-10-01'}
    item = {**_source(body), 'quote': body, 'subject': 'patient', 'category': 'observations',
            'source_key': 'attachment_ocr:10:1', 'source_binding': 'b' * 64,
            'source_artifact_id': 10, 'page': 1, 'posted_at_ts': None,
            'details': [{'key': key, 'value': value, 'evidence': body} for key, value in details.items()]}
    identity = (item['source_key'], item['content_hash'])
    series = observation_series.observation_series({'project_id': 1, 'items': [item]},
                                                   source_bodies={identity: body})
    assert not series['series'] and len(series['unconfirmed']) == 1


def test_mixed_document_and_measurement_dates_stay_unresolved():
    import clinical_values

    text = '作成日2026-10-02、本人は2026-10-01に測定した体温36.4℃。'
    assert clinical_values.measurement_day(text) is None


def test_validated_ocr_lab_reaches_common_patient_context_without_duplicate_observation():
    import mcs_queries
    body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
    document = {"labs": [_lab(body)], "patient_context": []}
    rows = mcs_queries.attachment_context_items(document, _source(body))
    assert len(rows) == 1 and rows[0]['category'] == 'observations'
    assert _details(rows[0])['measured_on'] == '2026-10-01'
