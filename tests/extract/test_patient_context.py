"""Fully invented patient context, with source freshness and bounded summary regressions."""
import json

import extract
import rollup
from extract_testkit import _hash, _ledger, _message
from patient_context import context_items, extract_context


def test_multiline_sections_preserve_history_negation_and_long_detail():
    detail = "合成の経過記載。" * 250
    body = ("【既往歴】\n数年前に合成疾患の治療。\n"
            "アレルギー：なし\n現病歴\n" + detail + "\n"
            "■未対応の見出し\n混ぜない情報\n"
            "服薬管理：家族が管理。\n方針：本人は通所を希望、家族は未確認。")
    doc = extract.extract_message(body, "2026-10-06T10:00:00+09:00")
    items = doc["patient_context"]
    assert [x["category"] for x in items] == [
        "history", "allergies", "course", "medication_management", "preferences"]
    assert items[1]["text"] == "なし"
    assert items[2]["text"] == detail and "混ぜない" not in items[2]["evidence"]
    assert context_items(doc, body) == items
    assert all(i["subject"] == "unspecified" for i in items)


def test_only_locatable_and_contained_context_survives():
    body = "合成の独居情報です。\n"
    valid = {"category": "living", "text": "独居", "evidence": "合成の独居情報です。",
             "subject": "patient"}
    assert context_items({"patient_context": [valid]}, body) == [valid]
    for patch in ({"category": []}, {"subject": []}, {"text": "同居"},
                  {"evidence": "別投稿の情報"}, {"evidence": 1}):
        assert context_items({"patient_context": [{**valid, **patch}]}, body) == []
    assert context_items({"patient_context": [valid]}, body * 2) == []
    assert extract_context("詳細のない連絡です。") == []


def test_rule_backfill_and_edit_replace_saved_context(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.save_messages([_message(body="既往歴：合成疾患A。")])
        assert extract.run_pending(led)["done"] == 1
        row = led.db.execute("SELECT content,meta FROM artifacts WHERE kind='extract_v1'").fetchone()
        assert json.loads(row["content"])["patient_context"][0]["text"] == "合成疾患A。"
        assert json.loads(row["meta"])["hash"] == _hash(led)
        assert extract.run_pending(led)["done"] == 0
        led.save_messages([_message(body="既往歴：合成疾患B。")])
        extract.run_pending(led)
        current = rollup.build_rollup(led, 1)["patient_context"]["chat"]["history"]
        assert len(current) == 1 and current[0]["text"] == "合成疾患B。"
        assert current[0]["message_id"] == 1
    finally:
        led.close()


def test_rollup_keeps_memo_separate_and_uses_newest_report_per_category(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.save_messages([
            _message(1, "アレルギー：未確認\n既往歴：合成疾患。", posted_at="2026-10-05T10:00:00+09:00"),
            _message(2, "アレルギー：なし", posted_at="2026-10-06T10:00:00+09:00")])
        extract.run_pending(led)
        # A canonical projection doesn't contain the new additive context.
        led.artifact_add("canonical_projection", json.dumps({"v": 1}),
                         project_id=1, message_id=2, meta={"hash": _hash(led, 2)})
        led.karte_summary_store(1, 100, {"comment": "アレルギー：合成薬の記載。", "updated_at": "2026-10-04"})
        context = rollup.build_rollup(led, 1)["patient_context"]
        assert context["chat"]["allergies"][0]["text"] == "なし"
        assert context["chat"]["history"][0]["message_id"] == 1
        assert context["memo"]["allergies"][0]["text"] == "合成薬の記載。"
        assert context["chat"]["allergies"][0]["state"] == "reported"
        led.save_messages([_message(2, "", state="deleted")])
        context = rollup.build_rollup(led, 1)["patient_context"]
        assert context["chat"]["allergies"][0]["text"] == "未確認"
    finally:
        led.close()


def test_every_category_alias_heads_its_own_category():
    # one definition per alias: no later override decides where a heading goes
    from patient_context import LABELS
    for category, labels in LABELS.items():
        for label in labels:
            body = f"{label}：完全合成の記載。"
            items = context_items({"patient_context": extract_context(body)}, body)
            assert [i["category"] for i in items] == [category], label


def test_category_order_and_display_headings_are_fixed():
    # notify_views shows categories in this order under labels[0]
    from patient_context import LABELS
    assert [(k, v[0]) for k, v in LABELS.items()] == [
        ("demographics", "基本情報"), ("diagnoses", "主病名"), ("history", "既往歴"),
        ("course", "現病歴"), ("services_provided", "実施内容"), ("incidents", "事故・ヒヤリハット"),
        ("adl", "ADL"), ("living", "生活環境"), ("care_level", "介護度"),
        ("care_services", "利用サービス"), ("care_team", "担当"), ("contacts", "キーパーソン"),
        ("allergies", "アレルギー"), ("devices", "医療処置"), ("preferences", "療養方針"),
        ("medication_management", "服薬管理"), ("nutrition", "食事"), ("excretion", "排泄"),
        ("skin_wound", "皮膚・褥瘡"), ("oral_health", "口腔"), ("cognition", "認知機能"),
        ("adverse_events", "薬物有害事象"), ("observations", "検査・観測値"),
        ("plan_goals", "計画・目標"), ("orders_documents", "指示・連携文書"), ("followup", "変更後の経過")]
    assert "副作用歴" in LABELS["adverse_events"] and "副作用歴" not in LABELS["allergies"]
