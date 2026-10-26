"""Wholly fictional requests and administrative notices retain their meaning."""
import extract_validation
import hashlib
import json
from contextlib import closing

import pytest

import extract
import ledger
import structured_view
from inference_version import CURRENT_VERSION, VERSION_KEY

POSTED = "2026-08-23T09:00:00+09:00"


@pytest.mark.parametrize("separator", ["。", "\n", "\r\n", "；", "!", "？"])
def test_request_context_is_its_complete_sentence(separator):
    request = "設備が故障した場合は、保守担当へご連絡ください"
    body = separator.join(["発注資料の保管場所を整理しました", request,
                           "次の議題は倉庫の鍵の受け渡しです"])
    result = extract.extract_message(body, POSTED)
    assert result["requests"] == [{"kind": "contact", "ctx": request}]


def test_long_request_is_not_cut_mid_condition_or_instruction():
    request = "予備部品と点検記録を照合し、" + "各保管棚の在庫番号と作動状況を記録してから、" * 5 + "保守担当へご連絡ください"
    assert len(request) > 80
    assert extract.extract_message(request + "。", POSTED)["requests"] == [
        {"kind": "contact", "ctx": request}]


def test_one_sentence_matching_confirm_and_request_is_not_duplicated():
    result = extract.extract_message("備品の数をご確認をお願いします。", POSTED)
    assert result["requests"] == [{"kind": "confirm", "ctx": "備品の数をご確認をお願いします"}]


@pytest.mark.parametrize("body", [
    "診療所宛ての封筒を仕分けました。",
    "訪問診療医の連絡先一覧を事務室へ届けます。",
    "往診医への郵便物を整理しました。",
    "診療報酬の事務研修を開催します。",
    "診療の方針について事務担当と打ち合わせました。",
    "診療に関する説明資料を研修室へ配布します。",
])
def test_administrative_encounter_words_do_not_become_exams(body):
    assert "exam" not in extract.extract_message(body, POSTED).get("events", [])
    result = extract_validation.validate({"events": ["exam"]}, body)
    assert result is None or (result["events"] == []
                              and all(record["unverified"] for record in result["event_records"]))


@pytest.mark.parametrize("body,status,confirmed", [
    ("本日、本人は診察を受けました。", "current", True),
    ("昨日、本人の往診を実施しました。", "past", False),
    ("来週、本人の診療を予約しました。", "planned", False),
    ("本人の訪問診療を予定しています。", "planned", False),
    ("明日の診察について相談しました。", "planned", False),
    ("往診医が来て診てくれました。", "current", False),
])
def test_actual_and_planned_encounters_remain_mentions(body, status, confirmed):
    for result in (extract.extract_message(body, POSTED),
                   extract_validation.validate({"events": ["exam"]}, body)):
        assert ("exam" in result.get("events", [])) is confirmed
        records = [record for record in result["event_records"] if record["kind"] == "exam"]
        assert records and all(record["status"] == status for record in records)
        for record in records:
            span = record["span"]
            assert body[span["start"]:span["end"]] == record["evidence"]


@pytest.mark.parametrize("body,kind,subject,status,negated,confirmed", [
    ("本人は現在、入院しました。", "admission", "patient", "current", False, True),
    ("本人は過去に入院しました。", "admission", "patient", "past", False, False),
    ("本人は明日入院予定です。", "admission", "patient", "planned", False, False),
    ("本人が入院した場合は連絡してください。", "admission", "patient", "conditional", False, False),
    ("本人は入院はありません。", "admission", "patient", "current", True, False),
    ("母は現在、入院しました。", "admission", "family", "current", False, False),
    ("他者は現在、入院しました。", "admission", "other", "current", False, False),
    ("本人は本日退院しました。", "discharge", "patient", "current", False, True),
])
def test_rule_event_records_keep_person_period_and_polarity(body, kind, subject, status, negated, confirmed):
    result = extract.extract_message(body, POSTED)
    record, = [record for record in result["event_records"] if record["kind"] == kind]
    assert (record["subject"], record["status"], record["negated"]) == (subject, status, negated)
    assert not record.get("unverified")
    assert (kind in result.get("events", [])) is confirmed
    assert body[record["span"]["start"]:record["span"]["end"]] == record["evidence"]
    if kind == "discharge":
        assert "admission" not in result.get("events", [])


@pytest.mark.parametrize("body", [
    "本人は救急搬送されました。", "本人は急性期病棟で過ごしました。",
    "本人はオピオイドを使用しています。", "本人はモルヒネを使用しています。",
    "本人はACPについて相談しました。",
])
def test_rule_keyword_does_not_invent_admission_or_eol(body):
    result = extract.extract_message(body, POSTED)
    assert not {"admission", "eol"}.intersection(result.get("events", []))
    assert not any(record["kind"] in ("admission", "eol") for record in result.get("event_records", []))


def test_rule_only_classification_tags_and_event_types_keep_their_contract():
    body = "本人は服薬しています。処方を確認しました。写真を添付します。本人は現在入院しました。"
    result = extract.extract_message(body, POSTED)
    assert {"adherence", "medication", "media_ref", "admission"}.issubset(result["events"])
    assert all(isinstance(event, str) for event in result["events"])
    assert all(record["kind"] in ("admission",) for record in result["event_records"])
    assert all(type(record["negated"]) is bool for record in result["event_records"])


def test_stale_rules_rebuild_and_render_without_unrelated_sentences(tmp_path):
    body = "診療所宛ての封筒を用意しました。設備が故障した場合は、保守担当へご連絡ください。次は鍵の説明です。"
    digest = hashlib.sha256(body.encode()).hexdigest()
    with closing(ledger.Ledger(str(tmp_path / "ledger.db"))) as store:
        store.db.execute(
            "INSERT INTO messages(message_id,project_id,body_text,body_state,content_hash,posted_at) "
            "VALUES(1,1,?,'full',?,?)", (body, digest, POSTED))
        store.db.commit()
        store.artifact_add("extract_v1", json.dumps({
            "events": ["exam"], "requests": [{"kind": "contact", "ctx": "obsolete synthetic fragment"}]}),
            project_id=1, message_id=1, meta={VERSION_KEY: CURRENT_VERSION, "hash": digest, "rule_version": extract.RULE_VERSION - 1})
        assert extract.run_pending(store)["done"] == 1
        current = json.loads(store.db.execute(
            "SELECT content FROM artifacts WHERE kind='extract_v1' AND message_id=1"
        ).fetchone()[0])
        assert current["requests"][0]["ctx"] == "設備が故障した場合は、保守担当へご連絡ください"
        lines = "\n".join(structured_view.structured_lines(store.db, 1))
        assert "依頼: 連絡:設備が故障した場合は、保守担当へご連絡ください" in lines
        assert "受診/検査" not in lines and "obsolete" not in lines
        assert "封筒" not in lines and "鍵の説明" not in lines
        assert store.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0] == body
