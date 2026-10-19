"""Wholly fictional requests and administrative notices retain their meaning."""
import extract_validation
import hashlib
import json
from contextlib import closing

import pytest

import extract
import ledger
import structured_view

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
    assert extract_validation.validate({"events": ["exam"]}, body) is None


@pytest.mark.parametrize("body", [
    "本日、本人は診察を受けました。",
    "昨日、本人の往診を実施しました。",
    "来週、本人の診療を予約しました。",
    "本人の訪問診療を予定しています。",
    "明日の診察について相談しました。",
    "往診医が来て診てくれました。",
])
def test_actual_and_planned_encounters_remain_mentions(body):
    assert "exam" in extract.extract_message(body, POSTED)["events"]
    assert extract_validation.validate({"events": ["exam"]}, body)["events"] == ["exam"]


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
            project_id=1, message_id=1, meta={"hash": digest, "rule_version": extract.RULE_VERSION - 1})
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
