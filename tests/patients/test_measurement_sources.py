"""Synthetic source-to-measurement coverage and source identity checks."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import extract_llm
import pytest
import inference_dependency_loss
import inference_version
import mcs_queries
import measurement_sources
import measurement_timeline
import rollup
from extract_testkit import _ledger, _message
from semantic_projection import PROJECTION_VERSION


def _setup(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    with ledger.db:
        ledger.db.execute("UPDATE patients SET fetch_state='complete', history_floor=-1 WHERE project_id=1")
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *a, **k: None)
    return ledger


def _save(ledger, mid, body, doc, posted):
    ledger.save_messages([_message(mid=mid, body=body, posted_at=posted)])
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, 1, mid)
    quality = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
               "completeness_chunks": 1,
               "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(ledger, row, json.dumps(doc, ensure_ascii=False), integrity=quality)


def _collect(ledger, *, msgs=None):
    db = ledger.db
    messages = (db.execute("SELECT * FROM messages WHERE project_id=1 ORDER BY message_id").fetchall()
                if msgs is None else msgs)
    blobs = mcs_queries.current_extraction_blobs(db, 1)
    refs = rollup._clinical_artifact_refs(db, 1, blobs)
    return measurement_sources.collect_measurements(db, 1, messages, blobs, refs, cfg={})


def _lab(body, value):
    return {"name": "Cr", "value": value, "unit": "mg/dL", "evidence": body,
            "subject": "patient", "status": "current"}



def test_completion_seal_preserves_current_measurement_reference(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [_lab(body, "1.0")]}, "2026-10-01T09:00:00+09:00")
        row = ledger.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind='extract_llm'").fetchone()
        assert "__mcs_leaf_claims_v1" in json.loads(row["content"])
        changes_before = ledger.db.total_changes
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        assert "__mcs_leaf_claims_v1" not in json.loads(blobs[1]["extract_llm"])
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        assert refs[(1, "extract_llm")] == row["artifact_id"]
        messages = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        result = measurement_sources.collect_measurements(ledger.db, 1, messages, blobs, refs, cfg={})
        assert result["coverage"]["unprocessed"] == 0
        [item] = measurement_timeline.project_measurements(result["records"], source_complete=False)["items"]
        assert item["value"] == "1.0" and item["source_ref"]["artifact_id"] == row["artifact_id"]
        assert ledger.db.total_changes == changes_before
        assert ledger.db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                                 (row["artifact_id"],)).fetchone()[0] == row["content"]


def test_changed_clinical_blob_cannot_reuse_a_current_measurement_reference(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [_lab(body, "1.0")]}, "2026-10-01T09:00:00+09:00")
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        assert (1, "extract_llm") in refs
        document = json.loads(blobs[1]["extract_llm"])
        document["labs"][0]["value"] = "9.0"
        blobs[1]["extract_llm"] = json.dumps(document, ensure_ascii=False)
        assert rollup._clinical_artifact_refs(ledger.db, 1, blobs) == {}
        messages = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        result = measurement_sources.collect_measurements(ledger.db, 1, messages, blobs, refs, cfg={})
        assert result["records"] == [] and result["coverage"]["unprocessed"] == 1


def test_lab_source_context_rejects_negation_or_uncertainty_outside_quote():
    quote = "2026-10-01の採血 Cr 1.0 mg/dL"
    for suffix in ("ではない。", "かもしれない。", "の可能性がある。", "か？"):
        body = "本人は" + quote + suffix
        message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
                   "posted_at": "2026-10-01T09:00:00+09:00", "body_text": body}
        records, invalid = measurement_sources._labs(message, {"labs": [_lab(quote, "1.0")]}, 1, None, True)
        assert invalid == 0 and records[0]["verification"] == "unverified"
        projected = measurement_timeline.project_measurements(records, source_complete=True)["items"][0]
        assert projected["state"] == "unknown" and projected["value"] is None


@pytest.mark.parametrize("suffix", [
    "その数値は誤りです。", "その数値は間違っています。",
    "その測定値は誤っていました！", "先ほどの結果は誤りです、正しくは未確認。",
    "その数値は誤っていないと思っていたが誤りでした。",
    "その数値は誤りではないかもしれません。", "その数値は未確認です。",
    "その数値は正しいと思っていましたが、血圧の値は誤りです。",
    "その数値は誤りではありませんか？",
    "その数値は正しいですが、体温の値は誤りです。",
    "その数値は正しいですか、血圧の値は誤りです。",
    "その数値は正しいか確認中です。", "その数値は誤りではないか確認中です。",
    "その体温の値は誤りです。",
    "血圧の値は誤りです、体温の値も誤りです。",
    "その数値は正しいですが、血圧と体温の値は誤りです。",
    "その数値は正しいですが、家族の数値は誤りで、本人の体温も誤りです。",
    "その数値は正しいでしょうか。", "その数値は誤りではないでしょうか。",
    "その数値は正しいですが、別の日ではなくこの数値は誤りです。",
    "本人の体温は誤りです。",
    "家族の血圧は正しいが、本人の体温は誤りです。",
    "別の日は正しいが本人の体温は誤りです。",
])
def test_adjacent_correction_never_confirms_a_vital(suffix):
    body = "本人の2026-10-02測定で体温36.4℃でした。" + suffix
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-03T09:00:00+09:00", "body_text": body}
    [record], invalid = measurement_sources._vitals(message, {"vitals": {"bt": 36.4}}, 1, True)
    assert invalid == 0 and record["verification"] == "unverified"
    [item] = measurement_timeline.project_measurements([record], source_complete=True)["items"]
    assert item["state"] == "unknown" and item["value"] is None


@pytest.mark.parametrize("suffix", [
    "その数値は誤りではありません。", "その数値は間違っていません。",
    "家族の数値は誤りです。", "先ほどの予定は誤りです。",
    "その数値は正しいですが、血圧の値は誤りです。",
    "その数値は誤りではありませんが、別の日の体温は誤りです。",
    "血圧の値は誤りです。",
    "家族の体温は誤りです。",
])
def test_adjacent_unrelated_or_negated_correction_preserves_vital(suffix):
    body = "本人の2026-10-02測定で体温36.4℃でした。" + suffix
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-03T09:00:00+09:00", "body_text": body}
    [record], invalid = measurement_sources._vitals(message, {"vitals": {"bt": 36.4}}, 1, True)
    assert invalid == 0 and record["verification"] == "quote_supported"


@pytest.mark.parametrize(('target', 'first', 'following'), [
    ('bt', '本人の体温36.8℃。', '明日も体温を確認します。'),
    ('bt', '本人の体温36.8℃。', '体温の値を明日も確認します。'),
    ('sbp', '本人の血圧132/80。', '血圧は安定しているので経過を確認します。'),
    ('dbp', '本人の血圧132/80。', '血圧は安定しているので経過を確認します。'),
    ('bt', '本人の体温36.8℃。', '体温が安定しているため経過を観察します。'),
    ('sbp', '本人の血圧132/80。', '血圧は安定したことから経過を確認する予定です。'),
    ('spo2', '本人のSpO2 92%。', 'SpO2は安定しておりますので経過を観察しています。'),
    ('bs', '本人の血糖120。', '明日も血糖を確認します。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していません。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していないです。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していなかった。'),
    ('bt', '本人の体温36.8℃。', '家族の体温が正確に測れたか不明です。'),
    ('bt', '本人の体温36.8℃。', '家族の体温計が故障しました。'),
    ('bt', '本人の体温36.8℃。', '家族の体温計が故障しましたが、家族だけがその体温計を使用しました。'),
    ('bt', '本人の体温36.8℃。', '家族の体温計が故障しましたが、本人は別の体温計を使用しました。'),
    ('bt', '本人の体温36.8℃。', '血圧計が故障しました。'),
])
def test_followup_plan_or_diagnosis_does_not_retract_prior_measurement(target, first, following):
    body = first + following
    assert not measurement_sources._adjacent_measurement_retraction(body, (0, len(first)), target=target)


@pytest.mark.parametrize(('target', 'first', 'following'), [
    ('bt', '本人の体温36.8℃。', '体温の値は未確認です。'),
    ('bt', '本人の体温36.8℃。', '体温が正確に測定できているか不明です。'),
    ('bt', '本人の体温36.8℃。', '体温の値を明日も確認しますが、正確か不明です。'),
    ('bt', '本人の体温36.8℃。', '体温が信頼できるか未確認です。'),
    ('bt', '本人の体温36.8℃。', '体温を確認する必要があります。'),
    ('bt', '本人の体温36.8℃。', '体温は正しいか確認中です。'),
    ('bt', '本人の体温36.8℃。', '体温は正しいですか？'),
    ('bt', '本人の体温36.8℃。', '体温は誤りではありませんか？'),
    ('bt', '本人の体温36.8℃。', '体温は間違っていないかもしれません。'),
    ('bt', '本人の体温36.8℃。', '体温計が故障していました。'),
    ('bt', '本人の体温36.8℃。', '測定器が壊れていました。'),
    ('bt', '本人の体温36.8℃。', '家族の体温計が故障しましたが、本人もその体温計を使用しました。'),
    ('bt', '本人の体温36.8℃。', '家族の体温計が故障しましたが、それを使いました。'),
    ('bt', '本人の体温36.8℃。', '家族の体温計が故障しましたが、私も使用しました。'),
    ('bt', '本人の体温36.8℃。', '家族の体温計が故障しましたが、同じ体温計を使いました。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していませんか？'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していないとは限りません。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していないとは言えません。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していない可能性があります。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していなかったと思います。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していないと確認中です。'),
    ('bt', '本人の体温36.8℃。', '体温計は故障していなかったが、故障しました。'),
    ('sbp', '本人の血圧132/80。', '血圧の数値は誤りです。'),
    ('sbp', '本人の血圧132/80。', '血圧が正しく測れたか不明です。'),
    ('dbp', '本人の血圧132/80。', '血圧が信頼できるか未確認です。'),
    ('bs', '本人の血糖120。', 'その数値が間違っていました。'),
    ('bs', '本人の血糖120。', '血糖の測定が正確か未確認です。'),
    ('bs', '本人の血糖120。', '食後の血糖も確認が必要です。'),
    ('spo2', '本人のSpO2 92%。', 'SpO2の値は誤りです。'),
    ('spo2', '本人のSpO2 92%。', 'SpO2低下は肺炎の可能性があり受診予定です。'),
    ('spo2', '本人のSpO2 92%。', 'SpO2低下は肺炎の可能性があり、測定値は不明です。'),
    ('spo2', '本人のSpO2 92%。', 'SpO2低下は測定器不良の可能性があり受診予定です。'),
    ('spo2', '本人のSpO2 92%。', 'SpO2低下は誤表示の可能性があり受診予定です。'),
    ('spo2', '本人のSpO2 92%。', 'SpO2は安定しているので経過を確認しますが、値は不明です。'),
    ('bt', '本人の体温36.8℃。', '体温が安定しているため経過を観察しますか？'),
    ('Cr', '本人のCr 1.1mg/dL。', 'Crの測定が正確か未確認です。'),
    ('未登録項目', '本人の未登録項目 1。', '体温計が故障していました。'),
])
def test_uncertain_reading_or_broken_instrument_still_holds(target, first, following):
    body = first + following
    assert measurement_sources._adjacent_measurement_retraction(body, (0, len(first)), target=target)


@pytest.mark.parametrize(('target', 'value', 'following'), [
    ('bt', 36.8, '明日も体温を確認します。'),
    ('bt', 36.8, '体温計は故障していません。'),
    ('bt', 36.8, '体温計は故障していなかった。'),
    ('sbp', 132, '血圧は安定しているので経過を確認します。'),
    ('dbp', 80, '血圧は安定しているので経過を確認します。'),
    ('bs', 120, '明日も血糖を確認します。'),
    ('spo2', 92, 'SpO2は安定しておりますので経過を観察しています。'),
])
def test_followup_plan_preserves_source_bound_vital(target, value, following):
    first = {'bt': '本人の2026-10-02測定で体温36.8℃。',
             'sbp': '本人の2026-10-02測定で血圧132/80。',
             'dbp': '本人の2026-10-02測定で血圧132/80。',
             'bs': '本人の2026-10-02測定で血糖120。',
             'spo2': '本人の2026-10-02測定でSpO2 92%。'}[target]
    message = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64,
               'posted_at': '2026-10-03T09:00:00+09:00', 'body_text': first + following}
    [record], invalid = measurement_sources._vitals(message, {'vitals': {target: value}}, 1, True)
    assert invalid == 0 and record['verification'] == 'quote_supported'
    assert record['evidence'] == first and record['value'] == value


def test_followup_plan_preserves_source_bound_lab():
    first = '本人の2026-10-02採血でCr 1.1mg/dLでした。'
    message = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64,
               'posted_at': '2026-10-03T09:00:00+09:00',
               'body_text': first + '明日もCrを確認します。'}
    [record], invalid = measurement_sources._labs(message, {'labs': [_lab(first, '1.1')]}, 1, None, True)
    assert invalid == 0 and record['verification'] == 'quote_supported'
    assert record['evidence'] == first and record['value'] == '1.1'


@pytest.mark.parametrize('following', [
    '体温が正確に測定できているか不明です。',
    '体温の値を明日も確認しますが、正確か不明です。',
    '体温が信頼できるか未確認です。',
])
def test_uncertain_measurement_quality_is_not_published_as_current(following):
    first = '本人の2026-10-02測定で体温36.8℃でした。'
    message = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64,
               'posted_at': '2026-10-03T09:00:00+09:00', 'body_text': first + following}
    [record], invalid = measurement_sources._vitals(message, {'vitals': {'bt': 36.8}}, 1, True)
    assert invalid == 0 and record['verification'] == 'unverified'
    [item] = measurement_timeline.project_measurements([record], source_complete=True)['items']
    assert item['state'] == 'unknown' and item['value'] is None


def test_uncertain_lab_measurement_quality_is_not_confirmed():
    first = '本人の2026-10-02採血でCr 1.1mg/dLでした。'
    message = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64,
               'posted_at': '2026-10-03T09:00:00+09:00',
               'body_text': first + 'Crの測定が正確か未確認です。'}
    [record], invalid = measurement_sources._labs(message, {'labs': [_lab(first, '1.1')]}, 1, None, True)
    assert invalid == 0 and record['verification'] == 'unverified'


def test_unspecified_meal_time_stays_unverified():
    first = '本人の2026-10-02測定で血糖120。'
    message = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64,
               'posted_at': '2026-10-03T09:00:00+09:00',
               'body_text': first + '食後の血糖も確認が必要です。'}
    [record], invalid = measurement_sources._vitals(message, {'vitals': {'bs': 120}}, 1, True)
    assert invalid == 0 and record['verification'] == 'unverified'
    [item] = measurement_timeline.project_measurements([record], source_complete=True)['items']
    assert item['state'] == 'unknown' and item['value'] is None


@pytest.mark.parametrize('following', [
    'SpO2低下は肺炎の可能性があり受診予定です。',
    'SpO2低下は肺炎の可能性があり、測定値は不明です。',
    'SpO2低下は測定器不良の可能性があり受診予定です。',
    'SpO2低下は誤表示の可能性があり受診予定です。',
    'SpO2は安定しているので経過を確認しますが、値は不明です。',
])
def test_diagnostic_uncertainty_does_not_confirm_original_reading(following):
    first = '本人の2026-10-02測定でSpO2 92%でした。'
    message = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64,
               'posted_at': '2026-10-03T09:00:00+09:00', 'body_text': first + following}
    [record], invalid = measurement_sources._vitals(message, {'vitals': {'spo2': 92}}, 1, True)
    assert invalid == 0 and record['verification'] == 'unverified'
    [item] = measurement_timeline.project_measurements([record], source_complete=True)['items']
    assert item['state'] == 'unknown' and item['value'] is None


@pytest.mark.parametrize('name', ['血糖', 'BS', 'Glu'])
@pytest.mark.parametrize(('following', 'verification'), [
    ('血糖測定器が故障していました。', 'unverified'),
    ('血糖測定器は故障していないとは限りません。', 'unverified'),
    ('血糖測定器は故障していません。', 'quote_supported'),
    ('血圧計が故障していました。', 'quote_supported'),
    ('家族の血糖測定器が故障しました。', 'quote_supported'),
    ('家族の血糖測定器が故障しましたが、それを使いました。', 'unverified'),
    ('家族の血糖測定器が故障しましたが、私も使用しました。', 'unverified'),
    ('家族の血糖測定器が故障しましたが、家族だけがその血糖測定器を使用しました。', 'quote_supported'),
    ('家族の血糖測定器が故障しましたが、本人は別の血糖測定器を使用しました。', 'quote_supported'),
    ('家族の血糖測定器が故障しましたが、本人もその血糖測定器を使用しました。', 'unverified'),
])
def test_lab_glucose_instrument_fault_uses_analyte_and_owner(name, following, verification):
    first = f'本人の2026-10-02採血で{name} 120mg/dLでした。'
    message = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64,
               'posted_at': '2026-10-03T09:00:00+09:00', 'body_text': first + following}
    lab = {**_lab(first, '120'), 'name': name}
    [record], invalid = measurement_sources._labs(message, {'labs': [lab]}, 1, None, True)
    assert invalid == 0 and record['verification'] == verification


@pytest.mark.parametrize('following', [
    '家族の体温計が故障しましたが、本人もその体温計を使用しました。',
    '家族の体温計が故障しましたが、それを使いました。',
    '家族の体温計が故障しましたが、私も使用しました。',
    '体温計は故障していないとは限りません。',
    '体温計は故障していないとは言えません。',
    '体温計は故障していない可能性があります。',
])
def test_uncertain_or_shared_thermometer_holds_patient_vital(following):
    first = '本人の2026-10-02測定で体温36.8℃でした。'
    message = {'project_id': 1, 'message_id': 1, 'content_hash': 'a' * 64,
               'posted_at': '2026-10-03T09:00:00+09:00',
               'body_text': first + following}
    [record], invalid = measurement_sources._vitals(message, {'vitals': {'bt': 36.8}}, 1, True)
    assert invalid == 0 and record['verification'] == 'unverified'
    [item] = measurement_timeline.project_measurements([record], source_complete=True)['items']
    assert item['state'] == 'unknown' and item['value'] is None


def test_named_spo2_correction_does_not_confirm_oxygen_saturation():
    body = "本人の2026-10-02測定でSpO2 97%でした。SpO2の値は誤りです。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-03T09:00:00+09:00", "body_text": body}
    [record], invalid = measurement_sources._vitals(message, {"vitals": {"spo2": 97}}, 1, True)
    assert invalid == 0 and record["verification"] == "unverified"


def test_named_lab_correction_does_not_confirm_the_old_value():
    quote = "本人の2026-10-02採血でCr 1.1mg/dLでした。"
    body = quote + "Crの値は誤りです。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-03T09:00:00+09:00", "body_text": body}
    [record], invalid = measurement_sources._labs(message, {"labs": [_lab(quote, "1.1")]}, 1, None, True)
    assert invalid == 0 and record["verification"] == "unverified"


@pytest.mark.parametrize("name, other", [("Cr", "CRP"), ("CRP", "Cr"), ("cr", "cRp")])
def test_different_lab_name_does_not_retract_a_measurement(name, other):
    quote = f"本人の2026-10-02採血で{name} 1.1mg/dLでした。"
    body = quote + f"{other}の値は誤りです。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-03T09:00:00+09:00", "body_text": body}
    lab = {**_lab(quote, "1.1"), "name": name}
    [record], invalid = measurement_sources._labs(message, {"labs": [lab]}, 1, None, True)
    assert invalid == 0 and record["verification"] == "quote_supported"


def test_adjacent_correction_bounds_repeated_target_checks(monkeypatch):
    first = "本人の採血Cr 1.1mg/dLでした。"
    checked = []
    monkeypatch.setattr(measurement_sources, "source_axes", lambda *_args, **_kwargs: checked.append(1))
    ordinary = first + "Cr " * 2000 + "を記録しました。"
    assert not measurement_sources._adjacent_measurement_retraction(ordinary, (0, len(first)), target="Cr")
    assert checked == []
    uncertain = first + "Cr " * 65 + "は誤りです。"
    assert measurement_sources._adjacent_measurement_retraction(uncertain, (0, len(first)), target="Cr")
    oversized = first + "測" * 20001
    assert measurement_sources._adjacent_measurement_retraction(oversized, (0, len(first)), target="Cr")
    assert measurement_sources._adjacent_measurement_retraction(
        first + " " * 20001 + "未確認です。", (0, len(first)), target="Cr")
    assert measurement_sources._adjacent_measurement_retraction("Cr" + "測" * 20001, (0, 2), target="Cr")
    assert checked == []


def test_lab_conditions_are_retained_only_when_bound_to_the_measurement():
    body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-01T09:00:00+09:00", "body_text": body}
    lab = {**_lab(body, "1.0"), "condition": "空腹時"}
    [record], invalid = measurement_sources._labs(message, {"labs": [lab]}, 1, None, True)
    assert invalid == 0 and record["verification"] == "quote_supported" and record["condition"] == "空腹時"
    lab["condition"] = "食後"
    [record], invalid = measurement_sources._labs(message, {"labs": [lab]}, 1, None, True)
    assert invalid == 0 and record["verification"] == "unverified" and record["condition"] is None

    body = "家族は食後。本人は2026-10-01の採血 Cr 1.0 mg/dL。"
    message["body_text"] = body
    lab = {**_lab(body, "1.0"), "condition": "食後"}
    [record], invalid = measurement_sources._labs(message, {"labs": [lab]}, 1, None, True)
    assert invalid == 0 and record["verification"] == "unverified" and record["condition"] is None


@pytest.mark.parametrize("prefix,expected", [
    ("", "current"),
    ("家族から報告がありました。", "current"),
    ("家族の話です。", "unknown"),
    ("家族の情報です。", "unknown"),
    ("家族の記録です。", "unknown"),
    ("母の話です。", "unknown"),
])
def test_family_topic_does_not_let_an_ambiguous_person_become_patient(prefix, expected):
    quote = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
    body = prefix + quote
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-01T09:00:00+09:00", "body_text": body}
    [record], invalid = measurement_sources._labs(
        message, {"labs": [{**_lab(quote, "1.0"), "condition": "空腹時"}]}, 1, None, True)
    assert invalid == 0
    [item] = measurement_timeline.project_measurements([record], source_complete=True)["items"]
    assert item["state"] == expected
    assert item["value"] == ("1.0" if expected == "current" else None)


def test_family_topic_does_not_let_a_bare_vital_belong_to_the_patient():
    body = "家族の話です。本人は2026-10-01に体温36.5℃を測定しました。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-01T09:00:00+09:00", "body_text": body}
    [record], invalid = measurement_sources._vitals(message, {"vitals": {"bt": 36.5}}, 1, True)
    assert invalid == 0 and record["verification"] == "unverified"


def test_legacy_snapshot_missing_capture_or_revision_schema_is_unknown():
    for missing in ("initial_import", "message_revisions", "is_archived"):
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            capture = "" if missing == "initial_import" else ", initial_import TEXT"
            archived = "" if missing == "is_archived" else ", is_archived INTEGER DEFAULT 0"
            db.execute("CREATE TABLE patients (project_id INTEGER, patient_name TEXT, "
                       "fetch_state TEXT, fetch_reason TEXT, history_floor INTEGER" + capture + archived + ")")
            db.execute("INSERT INTO patients (project_id, patient_name, fetch_state, history_floor) "
                       "VALUES (1, '合成患者', 'complete', -1)")
            db.execute("CREATE TABLE messages (message_id INTEGER, project_id INTEGER, body_text TEXT, "
                       "body_state TEXT, content_hash TEXT, posted_at TEXT, posted_at_ts INTEGER, "
                       "parent_id INTEGER, sender_id INTEGER, sender_name TEXT, sender_type TEXT, "
                       "profession TEXT, organization TEXT)")
            db.execute("INSERT INTO messages (message_id, project_id, body_state, body_text) "
                       "VALUES (1, 1, 'full', '合成本文')")
            db.execute("CREATE TABLE artifacts (artifact_id INTEGER, project_id INTEGER, message_id INTEGER, "
                       "kind TEXT, content TEXT, meta TEXT, model TEXT)")
            if missing != "message_revisions":
                db.execute("CREATE TABLE message_revisions (message_id INTEGER, seq INTEGER)")
            db.execute("CREATE TABLE fetch_jobs (project_id INTEGER, kind TEXT, state TEXT)")
            db.execute("CREATE TABLE attachments (message_id INTEGER)")
            db.execute("PRAGMA user_version=9")
            messages = db.execute("SELECT * FROM messages").fetchall()
            result = measurement_sources.collect_measurements(db, 1, messages, {}, {}, cfg={})
            coverage = result["coverage"]
            assert result["records"] == []
            assert coverage["source_complete"] is False
            assert coverage["local_checks_complete"] is None
            assert coverage["source_schema_available"] is False
            assert coverage["history_basis"] == "schema_unavailable"
            assert coverage["scope_state"] == "schema_unknown"
            assert coverage["stored"] is None and coverage["unprocessed"] is None
            assert db.in_transaction
            projected = measurement_timeline.project_measurements(result["records"],
                                                                    source_complete=coverage["source_complete"])
            assert projected["coverage"]["source_complete"] is False


def test_complete_borrowed_snapshot_projects_source_index_and_actual_measurement_order(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        old = "本人は2026-09-01の空腹時採血 Cr 1.0 mg/dL。"
        recent = "本人は2026-10-01の空腹時採血 Cr 1.5 mg/dL。"
        _save(ledger, 1, old, {"labs": [{**_lab(old, "1.0"), "condition": "空腹時"}]}, "2026-10-05T09:00:00+09:00")
        _save(ledger, 2, recent, {"labs": [{**_lab(recent, "1.5"), "condition": "空腹時"}]}, "2026-10-01T09:00:00+09:00")
        ledger.db.execute("BEGIN")
        result = _collect(ledger)
        assert ledger.db.in_transaction
        assert result["coverage"]["local_checks_complete"] is True
        assert result["coverage"]["source_complete"] is False
        assert result["coverage"]["source_schema_available"] is True
        assert result["coverage"]["full"] == 2
        assert result["coverage"]["upstream_snapshot_proven"] is False
        held = measurement_timeline.project_measurements(
            result["records"], source_complete=result["coverage"]["source_complete"])
        assert [item["state"] for item in held["items"]] == ["observed", "observed"]
        # Projection's complete-input behavior is independently tested; this
        # caller cannot prove that its earlier inputs shared the read snapshot.
        series = measurement_timeline.project_measurements(result["records"], source_complete=True)
        assert [item["state"] for item in series["items"]] == ["history", "current"]
        for item in series["items"]:
            source = ledger.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (item["source_ref"]["artifact_id"],)).fetchone()
            original = json.loads(source[0])[item["source_ref"]["field"]][item["source_ref"]["index"]]
            body = ledger.db.execute("SELECT body_text,content_hash FROM messages WHERE message_id=?", (item["message_id"],)).fetchone()
            start, end = item["evidence_span"]
            assert body[0][start:end] == original["evidence"] and body[1] == item["content_hash"]
        assert ledger.db.execute("SELECT 1").fetchone()[0] == 1


def test_complete_snapshot_without_written_conditions_keeps_observations_uncompared(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        old = "本人は2026-09-01の採血 Cr 1.0 mg/dL。"
        recent = "本人は2026-10-01の採血 Cr 1.5 mg/dL。"
        _save(ledger, 1, old, {"labs": [_lab(old, "1.0")]}, "2026-10-05T09:00:00+09:00")
        _save(ledger, 2, recent, {"labs": [_lab(recent, "1.5")]}, "2026-10-01T09:00:00+09:00")
        result = _collect(ledger)
        assert result["coverage"]["local_checks_complete"] is True
        assert result["coverage"]["source_complete"] is False
        series = measurement_timeline.project_measurements(result["records"], source_complete=True)
        assert [(item["message_id"], item["state"], item["condition"], item["value"])
                for item in series["items"]] == [(1, "observed", None, "1.0"),
                                               (2, "observed", None, "1.5")]
        assert all(item["source_ref"]["field"] == "labs" for item in series["items"])
        assert ledger.db.execute("SELECT 1").fetchone()[0] == 1


def test_unparsed_partial_or_omitted_message_never_proves_coverage(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [_lab(body, "1.0")]}, "2026-10-01T09:00:00+09:00")
        ledger.save_messages([_message(mid=2, body="未解析の合成本文", posted_at="2026-10-02T09:00:00+09:00")])
        result = _collect(ledger)
        assert result["coverage"]["source_complete"] is False
        assert result["coverage"]["unprocessed"] == 1
        assert result["coverage"]["local_checks_complete"] is False
        series = measurement_timeline.project_measurements(result["records"], source_complete=False)
        assert all(item["state"] != "current" for item in series["items"])
        only_first = ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchall()
        assert _collect(ledger, msgs=only_first)["coverage"]["ids_match"] is False


def test_canonical_measurement_is_counted_unprojected_without_legacy_fallback(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [_lab(body, "1.0")]}, "2026-10-01T09:00:00+09:00")
        chash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
        ledger.artifact_add("canonical_projection", json.dumps({"canonical_facts": [{"kind": "vital_lab"}]}),
            project_id=1, message_id=1,
            meta={"hash": chash, "projection_version": PROJECTION_VERSION,
                  inference_version.VERSION_KEY: inference_version.CURRENT_VERSION})
        result = _collect(ledger)
        assert result["records"] == []
        assert result["coverage"]["canonical_unverified"] == 1
        assert result["coverage"]["local_checks_complete"] is False
        assert result["coverage"]["canonical_vital_unprojected"] == 1
        assert result["coverage"]["source_complete"] is False


def test_deleted_and_unacquired_are_counted_separately(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        ledger.save_messages([_message(mid=1, body="", state="deleted")])
        with ledger.db:
            ledger.db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
        result = _collect(ledger)
        assert result["coverage"]["deleted"] == 1
        assert result["coverage"]["local_checks_complete"] is False
        assert result["coverage"]["acquisition_complete"] is False
        assert result["coverage"]["source_complete"] is False


def test_audited_canonical_measurement_uses_fact_and_exact_evidence_not_legacy(tmp_path, monkeypatch):
    import semantic_extraction
    import semantic_facts
    import semantic_policy
    import semantic_store
    import semantic_transport
    import semantic_publication
    from semantic_testkit import _cfg, facts_llm

    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        ledger.save_messages([_message(mid=1, body=body, posted_at="2026-10-01T09:00:00+09:00")])
        cfg = _cfg("enforce", loop_mode="off", fact_source="canonical", fact_source_gate="g6-v1:test")
        scfg, errors = semantic_policy.semantic_config(cfg)
        assert not errors
        policy = semantic_policy.policy_fingerprint(scfg)
        bundle = semantic_store.thread_bundle(ledger.db, 1, 1, local_model=semantic_transport.llm_model(cfg))
        member = bundle["members"][0]
        llm = facts_llm([{"statement": "Cr 1.0 mg/dL", "kind": "vital_lab", "subject_role": "patient",
                          "polarity": "affirmed", "workflow_status": "reported", "importance": "T1",
                          "evidence_quote": body}], presence={"vital_lab": "one"})
        generated = semantic_extraction.extract_facts_v2(llm, member, source_fingerprint=bundle["source_fingerprint"])
        assert generated["complete"]
        doc = generated["doc"]
        fact = next(f for f in doc["facts"] if f["kind"] == "vital_lab" and f["provenance"] == "local_llm")
        fact.update(valid_time="current", epistemic="reported", patient_context={
            "category": "observations", "text": body, "evidence": body, "subject": "patient",
            "details": [{"key": k, "value": v, "evidence": body} for k, v in
                        (("observation_name", "Cr"), ("value", "1.0"),
                         ("unit", "mg/dL"), ("measured_on", "2026-10-01"), ("condition", "空腹時"))]})
        doc = semantic_facts.validate_facts_doc(doc)
        doc_hash = semantic_facts.doc_hash(doc)
        meta = {inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
                "fingerprint": bundle["source_fingerprint"], "policy_fingerprint": policy}
        source_id = ledger.artifact_add("semantic_facts_v2", json.dumps(doc, ensure_ascii=False),
                                        project_id=1, message_id=1, meta=meta)
        ledger.artifact_add("semantic_facts_audit", json.dumps({"status": "PASS", "evaluated": True,
            "findings": [], "fact_verdicts": {}}), project_id=1, message_id=1, meta={**meta, "doc_hash": doc_hash})
        with ledger.db:
            assert semantic_publication.publish(ledger, 1, 1, bundle["source_fingerprint"], policy, member, doc)
        messages = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        result = measurement_sources.collect_measurements(ledger.db, 1, messages, blobs, refs, cfg=cfg)
        assert result["coverage"]["canonical_unverified"] == 0
        assert result["coverage"]["canonical_vital_unprojected"] == 0
        assert result["records"][0]["condition"] == "空腹時"
        series = measurement_timeline.project_measurements(result["records"],
                                                            source_complete=result["coverage"]["source_complete"])
        [row] = series["items"]
        assert result["coverage"]["local_checks_complete"] is True
        assert row["state"] == "observed" and row["value"] == "1.0"
        [proved] = measurement_timeline.project_measurements(result["records"], source_complete=True)["items"]
        assert proved["state"] == "current" and proved["value"] == "1.0"
        assert row["source_ref"]["artifact_id"] == source_id and row["source_ref"]["field"] == "facts"
        evidence_id = row["source_ref"]["evidence_id"]
        assert evidence_id in doc["facts"][row["source_ref"]["index"]]["evidence_ids"]
        evidence = next(e for e in doc["evidence"] if e["evidence_id"] == evidence_id)
        assert body[slice(*row["evidence_span"])] == evidence["quote"]
        unknown_cfg = measurement_sources.collect_measurements(ledger.db, 1, messages, blobs, refs)["coverage"]
        assert unknown_cfg["source_complete"] is False and unknown_cfg["local_checks_complete"] is False
        original = _message(mid=1, body=body, posted_at="2026-10-01T09:00:00+09:00")
        ledger.save_messages([replace(original, sender_name="変更された合成投稿者")])
        ledger.save_messages([original])
        fresh = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        stale = measurement_sources.collect_measurements(ledger.db, 1, fresh, blobs, refs, cfg=cfg)
        assert stale["coverage"]["source_complete"] is False
        assert stale["coverage"]["local_checks_complete"] is False
        assert stale["records"] == []


def test_tampered_source_binding_blocks_coverage_without_reviving_prior_value(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [_lab(body, "1.0")]}, "2026-10-01T09:00:00+09:00")
        row = ledger.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='extract_llm'").fetchone()
        meta = json.loads(row["meta"])
        meta["source_binding"] = "0" * 64
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), row["artifact_id"]))
        result = _collect(ledger)
        assert result["coverage"]["source_complete"] is False
        assert result["coverage"]["analysis_attention"] == 1
        assert result["coverage"]["local_checks_complete"] is False
        series = measurement_timeline.project_measurements(result["records"], source_complete=False)
        assert all(item["state"] == "unknown" for item in series["items"])


def test_unknown_config_cannot_be_treated_as_a_completed_model_contract(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [_lab(body, "1.0")]}, "2026-10-01T09:00:00+09:00")
        messages = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        result = measurement_sources.collect_measurements(ledger.db, 1, messages, blobs, refs)
        assert result["coverage"]["source_complete"] is False
        assert result["coverage"]["analysis_attention"] == 1
        assert result["coverage"]["local_checks_complete"] is False


def test_borrowed_input_row_cannot_forge_a_different_source_body(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [_lab(body, "1.0")]}, "2026-10-01T09:00:00+09:00")
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row["body_text"] = "改変された合成本文"
        result = _collect(ledger, msgs=[row])
        assert result["coverage"]["source_complete"] is False
        assert result["coverage"]["incomplete"] == 1
        assert result["coverage"]["local_checks_complete"] is False
        assert result["records"] == []


@pytest.mark.parametrize("state,complete", [("done", True), ("pending", False),
                                           ("failed", False), (None, False)])
def test_history_jobs_must_be_complete_for_current_measurements(tmp_path, monkeypatch, state, complete):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        ledger.ensure_patient(2)
        with ledger.db:
            ledger.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state) VALUES('history',1,0,?)", (state,))
            ledger.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,state) VALUES('history',2,0,NULL)")
        result = _collect(ledger)
        assert result["coverage"]["local_checks_complete"] is complete
        assert result["coverage"]["source_complete"] is False
        [item] = measurement_timeline.project_measurements(
            result["records"], source_complete=result["coverage"]["source_complete"])["items"]
        assert item["state"] == "observed" and item["value"] == "1.0"
        [proved] = measurement_timeline.project_measurements(
            result["records"], source_complete=complete)["items"]
        assert proved["state"] == ("current" if complete else "observed")


def test_first_seen_deleted_source_cannot_prove_current_measurements(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        ledger.save_messages([_message(mid=2, body="", state="deleted")])
        result = _collect(ledger)
        assert result["coverage"]["deleted"] == 1 and not result["coverage"]["source_complete"]
        assert result["coverage"]["local_checks_complete"] is False
        [item] = measurement_timeline.project_measurements(result["records"], source_complete=False)["items"]
        assert item["state"] == "observed" and item["value"] == "1.0"


def test_unread_attachment_blocks_current_only_for_its_patient(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        ledger.ensure_patient(2)
        ledger.save_messages([_message(mid=2, project_id=2, body="合成の別患者記録")], project_id=2)
        with ledger.db:
            ledger.db.execute("INSERT INTO attachments(message_id,file_id,state) VALUES(2,'synthetic-other','pending')")
        other_only = _collect(ledger)
        assert other_only["coverage"]["attachment_count"] == 0
        assert other_only["coverage"]["local_checks_complete"]
        assert not other_only["coverage"]["source_complete"]
        [current] = measurement_timeline.project_measurements(other_only["records"], source_complete=True)["items"]
        assert current["state"] == "current"
        with ledger.db:
            ledger.db.execute("INSERT INTO attachments(message_id,file_id,state) VALUES(1,'synthetic-own','pending')")
        own = _collect(ledger)
        assert own["coverage"]["attachment_count"] == 1 and not own["coverage"]["source_complete"]
        assert own["coverage"]["local_checks_complete"] is False
        [observed] = measurement_timeline.project_measurements(own["records"], source_complete=False)["items"]
        assert observed["state"] == "observed" and observed["value"] == "1.0"


def test_fetch_failure_reason_blocks_acquisition_even_if_state_says_complete(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        with ledger.db:
            ledger.db.execute("UPDATE patients SET fetch_reason='synthetic_fetch_error' WHERE project_id=1")
        result = _collect(ledger)
        assert not result["coverage"]["acquisition_complete"]
        assert not result["coverage"]["local_checks_complete"]
        assert not result["coverage"]["source_complete"]
        assert result["coverage"]["history_basis"] == "incomplete"


@pytest.mark.parametrize("correction", ["先ほどの数値は違います。", "先ほどの件は誤りでした。",
                                         "前回の記載を訂正します。", "先ほどの数値は正しくありません。"])
def test_separate_correction_holds_current_even_if_extraction_has_no_measurements(tmp_path, monkeypatch, correction):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        _save(ledger, 2, correction, {}, "2026-10-02T09:00:00+09:00")
        result = _collect(ledger)
        assert result["coverage"]["correction_candidates"] == 1
        assert result["coverage"]["local_checks_complete"] is False
        assert not result["coverage"]["source_complete"]
        [item] = measurement_timeline.project_measurements(result["records"], source_complete=False)["items"]
        assert item["state"] == "observed" and item["value"] == "1.0"


def test_correction_unrelated_family_or_negated_does_not_block_current(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        _save(ledger, 2, "家族の数値は誤りでした。", {}, "2026-10-02T09:00:00+09:00")
        _save(ledger, 3, "先ほどの数値は誤りではありません。", {}, "2026-10-03T09:00:00+09:00")
        result = _collect(ledger)
        assert result["coverage"]["correction_candidates"] == 0
        assert result["coverage"]["local_checks_complete"]
        assert not result["coverage"]["source_complete"]
        [item] = measurement_timeline.project_measurements(result["records"], source_complete=True)["items"]
        assert item["state"] == "current"


def test_ambiguous_correction_after_an_unrelated_family_value_is_not_ignored():
    assert measurement_sources._correction_candidate("家族の数値は参考で、先ほどの件は誤りでした。")
    assert not measurement_sources._correction_candidate("先ほどの数値は間違っていません。")
    assert not measurement_sources._correction_candidate("先ほどの数値は正しいです。")


def test_same_sentence_correction_does_not_verify_the_measurement():
    body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dLですが、実際には違います。"
    message = {"project_id": 1, "message_id": 1, "content_hash": "a" * 64,
               "posted_at": "2026-10-01T09:00:00+09:00", "body_text": body}
    [record], invalid = measurement_sources._labs(
        message, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]}, 1, None, True)
    assert invalid == 0 and record["verification"] == "unverified"
    [item] = measurement_timeline.project_measurements([record], source_complete=False)["items"]
    assert item["state"] == "unknown" and item["value"] is None


def test_collection_preserves_caller_transaction_and_rollback(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        ledger.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
        before = ledger.db.total_changes
        assert not _collect(ledger)["coverage"]["source_complete"]
        assert ledger.db.in_transaction and ledger.db.total_changes == before
        ledger.db.rollback()
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


def test_input_iterator_write_is_not_committed_by_the_read_savepoint(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        def messages():
            ledger.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
            yield from ()

        assert not ledger.db.in_transaction
        result = measurement_sources.collect_measurements(ledger.db, 1, messages(), {}, {}, cfg={})
        assert not result["coverage"]["source_complete"] and ledger.db.in_transaction
        ledger.db.rollback()
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


def test_stale_caller_artifact_cannot_be_verified_by_newer_extraction(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        messages = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        previous = ledger.db.execute("SELECT meta,model FROM artifacts WHERE artifact_id=?",
                                     (refs[(1, "extract_llm")],)).fetchone()
        ledger.artifact_add("extract_llm", json.dumps({"labs": []}), project_id=1, message_id=1,
                            model=previous["model"], meta=json.loads(previous["meta"]))
        result = measurement_sources.collect_measurements(ledger.db, 1, messages, blobs, refs, cfg={})
        assert result["records"] == [] and result["coverage"]["unprocessed"] == 1
        assert not result["coverage"]["source_complete"]


@pytest.mark.parametrize("bad_body", ["合成\0隠れた値", "測" * 7000, "x" * 20001, "\ud800"])
def test_unsafe_input_body_preserves_other_grounded_values_as_observed(tmp_path, monkeypatch, bad_body):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        for mid, value in ((1, "1.0"), (2, "1.5")):
            body = f"本人は2026-10-01の空腹時採血 Cr {value} mg/dL。"
            _save(ledger, mid, body, {"labs": [{**_lab(body, value), "condition": "空腹時"}]},
                  "2026-10-01T09:00:00+09:00")
        rows = [dict(row) for row in ledger.db.execute("SELECT * FROM messages WHERE project_id=1 ORDER BY message_id")]
        rows[1]["body_text"] = bad_body
        result = _collect(ledger, msgs=rows)
        assert result["coverage"]["truncated"] and not result["coverage"]["source_complete"]
        [item] = measurement_timeline.project_measurements(result["records"], source_complete=False)["items"]
        assert item["state"] == "observed" and item["value"] == "1.0"


@pytest.mark.parametrize("bad_blob", ["x\0hidden", "測" * 400000, "\ud800"])
def test_unsafe_input_artifact_is_held_before_json_parsing(tmp_path, monkeypatch, bad_blob):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        rows = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        blobs[1]["extract_llm"] = bad_blob
        result = measurement_sources.collect_measurements(ledger.db, 1, rows, blobs, refs, cfg={})
        assert result["records"] == [] and result["coverage"]["truncated"]
        assert not result["coverage"]["source_complete"]


def test_message_iterator_is_bounded_before_opening_read_savepoint(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        monkeypatch.setattr(measurement_sources, "_MAX_MESSAGES", 2)
        row = {"project_id": 1, "message_id": 1, "body_text": "合成", "body_state": "full"}
        seen = []

        def messages():
            for n in range(4):
                seen.append(n)
                if n == 2:
                    ledger.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
                if n == 3:
                    raise AssertionError("iterator consumed beyond the overflow sentinel")
                yield row

        result = measurement_sources.collect_measurements(ledger.db, 1, messages(), {}, {}, cfg={})
        assert seen == [0, 1, 2] and result["coverage"]["truncated"]
        assert not result["coverage"]["source_complete"] and ledger.db.in_transaction
        ledger.db.rollback()
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


def test_record_limit_keeps_the_first_grounded_value_as_observed(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        for mid, value in ((1, "1.0"), (2, "1.5")):
            body = f"本人は2026-10-01の空腹時採血 Cr {value} mg/dL。"
            _save(ledger, mid, body, {"labs": [{**_lab(body, value), "condition": "空腹時"}]},
                  "2026-10-01T09:00:00+09:00")
        monkeypatch.setattr(measurement_sources, "_MAX_RECORDS", 1)
        result = _collect(ledger)
        assert result["coverage"]["truncated"] and not result["coverage"]["source_complete"]
        [item] = measurement_timeline.project_measurements(result["records"], source_complete=False)["items"]
        assert item["state"] == "observed" and item["value"] == "1.0"


def test_combined_input_byte_budget_does_not_claim_complete(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        monkeypatch.setattr(measurement_sources, "_MAX_INPUT_BYTES", 1)
        result = _collect(ledger)
        assert result["records"] == [] and result["coverage"]["truncated"]
        assert not result["coverage"]["source_complete"]


def test_text_budget_counts_utf8_bytes_and_rejects_embedded_nul():
    assert measurement_sources._bounded_text("測", 3) == 3
    assert measurement_sources._bounded_text("測測", 5) is None
    assert measurement_sources._bounded_text("a\0b", 10) is None


def test_per_source_item_limit_cannot_report_complete(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        monkeypatch.setattr(measurement_sources, "_MAX_PER_SOURCE", 1)
        complete = _collect(ledger)["coverage"]
        assert complete["local_checks_complete"] and not complete["source_complete"]
        monkeypatch.setattr(measurement_sources, "_MAX_PER_SOURCE", 0)
        limited = _collect(ledger)
        assert limited["coverage"]["truncated"] and not limited["coverage"]["source_complete"]
        assert limited["coverage"]["local_checks_complete"] is False
        assert limited["records"] == []


def test_same_id_does_not_authorize_a_different_input_body(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        original = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        altered = {**original, "body_text": body + " 合成の別本文"}
        result = _collect(ledger, msgs=[original, altered])
        assert not result["coverage"]["source_complete"]
        assert len(result["records"]) == 1 and result["records"][0]["source_body"] == body


def test_custom_artifact_mapping_is_not_executed_inside_the_savepoint(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        class SideEffectDict(dict):
            def get(self, *args):
                ledger.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
                return super().get(*args)

        result = measurement_sources.collect_measurements(ledger.db, 1, [], SideEffectDict(), {}, cfg={})
        assert result["coverage"]["truncated"] and not result["coverage"]["source_complete"]
        assert not ledger.db.in_transaction
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


@pytest.mark.parametrize("code", [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_FULL,
                                  sqlite3.SQLITE_IOERR | (3 << 8)])
def test_auto_rollback_preserves_original_read_error(monkeypatch, code):
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE marker(value INTEGER)")
        db.execute("INSERT INTO marker VALUES (7)")
        error = sqlite3.OperationalError("synthetic SQLite failure")
        error.sqlite_errorcode = code

        def fail(connection, *_args, **_kwargs):
            connection.rollback()
            raise error

        monkeypatch.setattr(measurement_sources, "_collect_measurements", fail)
        with pytest.raises(sqlite3.OperationalError) as caught:
            measurement_sources.collect_measurements(db, 1, [], {}, {})
        assert caught.value is error and caught.value.sqlite_errorcode == code
        assert not db.in_transaction and db.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0


def test_read_failure_keeps_borrowed_transaction_open(monkeypatch):
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE marker(value INTEGER)")
        db.execute("INSERT INTO marker VALUES (7)")
        error = ValueError("synthetic read failure")

        def fail(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(measurement_sources, "_collect_measurements", fail)
        with pytest.raises(ValueError) as caught:
            measurement_sources.collect_measurements(db, 1, [], {}, {})
        assert caught.value is error and db.in_transaction
        assert db.execute("SELECT value FROM marker").fetchall() == [(7,)]
        db.rollback()
        assert db.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0


def test_successful_read_does_not_hide_release_failure(monkeypatch):
    with closing(sqlite3.connect(":memory:")) as db:
        monkeypatch.setattr(measurement_sources, "_collect_measurements", lambda *_args, **_kwargs: {})
        db.set_authorizer(lambda action, arg1, arg2, *_: sqlite3.SQLITE_DENY
                          if action == sqlite3.SQLITE_SAVEPOINT and arg1 == "RELEASE"
                          and arg2 == "measurement_sources_read" else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                measurement_sources.collect_measurements(db, 1, [], {}, {})
        finally:
            db.set_authorizer(None)
            db.rollback()


@pytest.mark.parametrize("field,value", [
    ("is_archived", 1),
    ("fetch_state", "forbidden"),
    ("fetch_reason", "forbidden"),
    ("fetch_reason", "permission_denied"),
    ("fetch_reason", "access_revoked"),
])
def test_revoked_or_archived_patient_never_returns_prior_measurement(tmp_path, monkeypatch, field, value):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        before = _collect(ledger)
        assert len(before["records"]) == 1
        with ledger.db:
            ledger.db.execute(f"UPDATE patients SET {field}=? WHERE project_id=1", (value,))
        denied = _collect(ledger)
        assert denied["records"] == []
        assert denied["coverage"]["scope_state"] == "scope_unavailable"
        assert denied["coverage"]["source_complete"] is False
        assert denied["coverage"]["local_checks_complete"] is None
        assert denied["coverage"]["stored"] is None
        assert body not in json.dumps(denied, ensure_ascii=False)
        assert measurement_timeline.project_measurements(
            denied["records"], source_complete=denied["coverage"]["source_complete"])["items"] == []


def test_other_patient_input_cannot_expose_measurement(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        ledger.ensure_patient(2)
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        result = measurement_sources.collect_measurements(ledger.db, 2, [row], blobs, refs, cfg={})
        assert result["records"] == []
        assert result["coverage"]["scope_state"] == "observed"
        assert result["coverage"]["source_complete"] is False
        assert result["coverage"]["local_checks_complete"] is False
        assert body not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("stale", ["invalidated", "old_hash"])
def test_canonical_policy_does_not_revive_local_measurement_when_projection_is_stale(tmp_path, monkeypatch, stale):
    from semantic_testkit import _cfg

    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        digest = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
        meta = {"hash": digest if stale == "invalidated" else "f" * 64,
                "projection_version": PROJECTION_VERSION, inference_version.VERSION_KEY: inference_version.CURRENT_VERSION}
        if stale == "invalidated":
            meta["invalidated"] = True
        ledger.artifact_add("canonical_projection", json.dumps({"canonical_facts": []}),
                            project_id=1, message_id=1, meta=meta)
        messages = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        assert "canonical_projection" not in blobs[1] and "extract_llm" in blobs[1]
        cfg = _cfg("enforce", loop_mode="off", fact_source="canonical", fact_source_gate="g6-v1:test")
        result = measurement_sources.collect_measurements(ledger.db, 1, messages, blobs, refs, cfg=cfg)
        assert result["records"] == [] and result["coverage"]["canonical_unverified"] == 1
        assert result["coverage"]["local_checks_complete"] is False


def test_adjacent_written_correction_does_not_leave_a_supported_measurement(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        quote = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        body = quote + "先ほどの検査値は誤っていました。"
        _save(ledger, 1, body, {"labs": [{**_lab(quote, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        result = _collect(ledger)
        assert result["coverage"]["correction_candidates"] == 1
        assert result["coverage"]["local_checks_complete"] is False
        [item] = measurement_timeline.project_measurements(result["records"], source_complete=False)["items"]
        assert item["state"] == "unknown" and item["value"] is None


@pytest.mark.parametrize("sentence", [
    "先ほどの検査値は誤っていません。",
    "家族の検査値は誤っていました。",
    "他の患者の検査値は誤っていました。",
    "先ほどの予定は誤っていました。",
])
def test_correction_of_unrelated_or_not_wrong_measurement_does_not_block_patient(sentence):
    assert not measurement_sources._correction_candidate(sentence)


@pytest.mark.parametrize("sentence", [
    "家族の検査値は誤っていましたが、本人の数値も誤っていました。",
    "他の患者の検査値について報告し、本人の値は誤っていました。",
    "家族の検査を確認し、数値は誤っていました。",
])
def test_foreign_measurement_does_not_hide_a_patient_or_ambiguous_correction(sentence):
    assert measurement_sources._correction_candidate(sentence)


def test_canonical_policy_without_projection_does_not_fall_back_to_local(tmp_path, monkeypatch):
    from semantic_testkit import _cfg

    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        messages = ledger.db.execute("SELECT * FROM messages WHERE project_id=1").fetchall()
        blobs = mcs_queries.current_extraction_blobs(ledger.db, 1)
        refs = rollup._clinical_artifact_refs(ledger.db, 1, blobs)
        cfg = _cfg("enforce", loop_mode="off", fact_source="canonical", fact_source_gate="g6-v1:test")
        result = measurement_sources.collect_measurements(ledger.db, 1, messages, blobs, refs, cfg=cfg)
        assert not result["records"] and result["coverage"]["canonical_unverified"] == 1


def test_other_patients_stale_projection_does_not_hold_local_measurement(tmp_path, monkeypatch):
    with closing(_setup(tmp_path, monkeypatch)) as ledger:
        body = "本人は2026-10-01の空腹時採血 Cr 1.0 mg/dL。"
        _save(ledger, 1, body, {"labs": [{**_lab(body, "1.0"), "condition": "空腹時"}]},
              "2026-10-01T09:00:00+09:00")
        ledger.ensure_patient(2)
        ledger.save_messages([_message(mid=2, project_id=2, body="本人の合成記録")], project_id=2)
        ledger.artifact_add("canonical_projection", "{}", project_id=2, message_id=2,
                            meta={"hash": "f" * 64, "invalidated": True})
        result = _collect(ledger)
        assert len(result["records"]) == 1 and result["coverage"]["canonical_unverified"] == 0
        assert result["records"][0]["value"] == "1.0"
