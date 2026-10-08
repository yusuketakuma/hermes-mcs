"""完全合成の訪問診療予定日を、既存の日付解決・更新・表示経路で確認する。"""
import json

import pytest

import extract
import extract_llm
import rollup
import structured_view
from extract_testkit import _hash, _ledger, _message


@pytest.mark.parametrize("body,posted,expected", [
    ("次回の訪問診療予定日：2032/6/7", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療予定日：6/7", "2032-06-01", "2032-06-07"),
    ("次回訪問診療予定日：2032年6月7日", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療の予定日：2032年6月7日", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療予定日は 2032/6/7です。", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療予定日：1/8", "2032-12-29", "2033-01-08"),
    ("次回の訪問診療予定日：2032/1/8", "2032-12-29", "2032-01-08"),
    ("次回の訪問診療予定日：2032/6/7", "日時不明", "2032-06-07"),
    ("次回6/7 訪問予定", "2032-06-01", "2032-06-07"),
    ("次回は6/7です。", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療は2032年6月7日です。", "2032-06-01", "2032-06-07"),
    ("次の訪問診療は6月7日です。", "2032-06-01", "2032-06-07"),
    ("次の訪問診療の予定日時：2032-06-07 10:30", "2032-06-01", "2032-06-07"),
    ("次回往診予定：2032/6/7", "2032-06-01", "2032-06-07"),
    ("次回の診察日程は6月7日です。", "2032-06-01", "2032-06-07"),
    ("次回 訪問診療 予定日時 : 2032-06-07", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療予定日：２０３２／６／７", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療予定日：２０３２－０６－０７", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療予定日：\n2032年6月7日\n以上。", "2032-06-01", "2032-06-07"),
    ("次回往診予定：\r\n 2032-06-07", "2032-06-01", "2032-06-07"),
    ("次回6月7日（水）訪問診療予定です。", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療は、6/7に訪問診療予定です。", "2032-06-01", "2032-06-07"),
    ("次回の訪問診療は6/7、合成薬Aは中止です。", "2032-06-01", "2032-06-07"),
    ("次回訪問診療予定日時：\n2032/6/7 10:30", "2032-06-01", "2032-06-07"),
    ("次回の日程は、6/7訪問診療です。", "2032-06-01", "2032-06-07"),
    ("担当医師：架空医師A\n次回の訪問診療予定日：6/7", "2032-06-01", "2032-06-07"),
    ("診療担当医師名：架空医師A\n次回の訪問診療予定日：6/7", "2032-06-01", "2032-06-07"),
    ("【担当看護師】\n架空看護師A\n次回往診予定：6/7", "2032-06-01", "2032-06-07"),
    ("架空太郎医師：\n記録を共有します。\n次回訪問診療予定日：2032/6/7", "2032-06-01", "2032-06-07"),
    ("架空花子看護師：\n記録を共有します。\n次回往診予定：6/7", "2032-06-01", "2032-06-07"),
])
def test_extended_visit_schedule_label_uses_existing_year_resolution(body, posted, expected):
    assert extract.extract_message(body, posted)["next_planned"] == expected


@pytest.mark.parametrize("body,posted", [
    ("次回の訪問診療予定日：6/7", "日時不明"),
    ("次回の訪問診療予定日：2031/2/29", "2031-02-01"),
    ("次回の訪問診療予定日：2032/6/712", "2032-06-01"),
    ("次回は未定。6/7に検査しました。", "2032-06-01"),
    ("次回は未定！6/7に検査しました。", "2032-06-01"),
    ("次回は未定\n6/7に検査しました。", "2032-06-01"),
    ("次回の訪問診療予定日：\n検査日は2032/6/7です。", "2032-06-01"),
    ("次回の訪問診療予定日：相談後に調整。検査日は2032/6/7。", "2032-06-01"),
    ("次の訪問診療は未定。2032/6/7は検査日です。", "2032-06-01"),
    ("次回の訪問診療予定日：\n2032/6/7に検査を受けました。", "2032-06-01"),
    ("次回の訪問診療予定日：\n\n2032/6/7", "2032-06-01"),
    ("次回の訪問診療は来週の水曜日です。", "2032-06-01"),
    ("次回の訪問診療予定日：明日", "2032-06-01"),
    ("次回の訪問診療予定日：未定（以前の予定2032/6/7）。", "2032-06-01"),
    ("次回6/7は中止となりました。", "2032-06-01"),
    ("次回の訪問診療は6/7ですがキャンセルしました。", "2032-06-01"),
    ("次回の訪問診療予定日：2032/6/7（延期）", "2032-06-01"),
    ("次回の訪問診療予定日時：2032-06-07 10:30は中止です。", "2032-06-01"),
    ("次回の訪問診療は6/7訪問診療予定でしたがキャンセルです。", "2032-06-01"),
    ("次回延期（旧6/7）。", "2032-06-01"),
    ("母は次回の訪問診療を2032/6/7に予定しています。", "2032-06-01"),
    ("母の次回の訪問診療は2032/6/7です。", "2032-06-01"),
    ("担当医師本人は次回往診予定：2032/6/7です。", "2032-06-01"),
    ("以前の記録：次回の訪問診療予定日：2032/6/7。", "2032-06-01"),
    ("次回は母の6/7訪問診療です。本人の予定は未定です。", "2032-06-01"),
    ("次回の訪問診療は6/7に予定していましたが中止しました。", "2032-06-01"),
    ("【母】\n担当医師：架空医師A\n次回の訪問診療予定日：6/7", "2032-06-01"),
    ("【医師本人】\n次回の訪問診療予定日：6/7", "2032-06-01"),
    ("【担当医師の症状】\n次回の訪問診療予定日：6/7", "2032-06-01"),
    ("【医師】\n次回訪問診療予定日：6/7", "2032-06-01"),
    ("架空太郎医師の症状：\n次回訪問診療予定日：6/7", "2032-06-01"),
    ("担当医師の発熱：\n次回診察予定日：2032/6/7", "2032-06-01"),
    ("担当医師の呼吸困難：\n次回診察予定日：2032/6/7", "2032-06-01"),
])
def test_missing_or_invalid_visit_schedule_does_not_take_another_sentences_date(body, posted):
    assert "next_planned" not in extract.extract_message(body, posted)


@pytest.mark.parametrize("body", [
    "次回の訪問診療は6/7訪問です。",
    "次回の訪問診療は6/7訪問診療予定です。",
    "次回6/7訪問診療予定です。",
    "次の訪問診療の予定日時：\n2032/6/7",
    "次回の日程は、6/7訪問診療です。",
])
def test_long_or_trailing_planned_visit_does_not_become_past_visit_date(body):
    doc = extract.extract_message(body, "2032-06-01")
    assert doc["next_planned"] == "2032-06-07"
    assert "visit_date" not in doc


def test_cancelled_explicit_visit_is_neither_next_plan_nor_past_visit():
    body = "次回の訪問診療は6/7訪問診療予定でしたがキャンセルです。"
    doc = extract.extract_message(body, "2032-06-01")
    assert "next_planned" not in doc and "visit_date" not in doc


def test_provider_signature_override_is_limited_to_schedule_reading():
    body = "架空太郎医師：\n次回訪問診療予定日：2032/6/7"
    start = body.index("2032")
    assert extract.patient_source_scope(body, start, len(body)) == "other"
    assert extract.patient_source_scope(body, start, len(body), provider_fields=True) == "planned"


def test_rule_version_refresh_updates_old_schedule_artifact_and_current_consumers(tmp_path):
    store = _ledger(tmp_path)
    try:
        store.ensure_patient(1)
        body = "本日の訪問報告です。次回の訪問診療予定日：2032/6/7。"
        store.save_messages([_message(body=body, posted_at="2032-06-01T09:00:00+09:00")])
        store.artifact_add("extract_v1", json.dumps({"v": 1}), project_id=1, message_id=1,
                           meta={"hash": _hash(store), "rule_version": extract.RULE_VERSION - 1})
        assert extract.run_pending(store) == {"done": 1, "pids": [1]}
        assert extract.run_pending(store) == {"done": 0, "pids": []}
        assert rollup.build_rollup(store, 1)["next_planned"] == "2032-06-07"
        assert "次回予定: 2032-06-07" in structured_view.structured_lines(store.db, 1)
        row = store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm._rule_hints(row)["next_planned"] == "2032-06-07"
        artifact = store.artifacts("extract_v1")[0]
        assert json.loads(artifact["meta"])["rule_version"] == extract.RULE_VERSION
    finally:
        store.close()
