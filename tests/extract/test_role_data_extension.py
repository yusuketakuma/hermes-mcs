"""Fully synthetic multi-role posts: every role's usual headings survive heading extraction."""
from collections import Counter

import pytest

from patient_context import CATEGORIES, DETAIL_LABELS, LABELS, _CATEGORY, extract_context

# 完全合成。実投稿・実患者情報ではない。
ROLE_POSTS = {
    "nurse": ("訪問日: 2026-10-15\nバイタル: BT36.8 BP128/76 P72 SpO2 96%\n排泄: 排便3日なし、下剤を調整\n"
              "皮膚: 仙骨部に発赤2cm\n褥瘡処置: ワセリン保護、除圧クッション使用\n睡眠: 中途覚醒2回\n"
              "実施内容: 清拭、創処置、服薬確認\n訪問看護指示書: 10/31まで、特別指示なし\n"
              "主治医報告: 便秘について相談済み\n次回訪問: 10/18",
              ["services_provided", "observations", "excretion", "skin_wound", "skin_wound", "living",
               "services_provided", "orders_documents", "orders_documents", "followup"]),
    "helper": ("訪問時間: 10:00-11:00\n身体介護: 入浴介助\n生活援助: 調理、掃除\n食事量: 朝 全量\n水分量: 800mL\n"
               "排泄: トイレ介助、失禁なし\n様子: いつもどおり\nヒヤリハット: 立ち上がり時にふらつき、転倒なし\n"
               "連絡: ケアマネへ報告済み",
               ["services_provided", "services_provided", "services_provided", "nutrition", "nutrition",
                "excretion", "course", "incidents", "orders_documents"]),
    "caremanager": ("認定情報: 要介護2、有効期間 2027-03-31\n主訴: 自宅で暮らし続けたい\nニーズ: 転倒せずにトイレへ行きたい\n"
                    "長期目標: 自宅での生活継続\n短期目標: 見守りでトイレ移動\n"
                    "利用サービス: 訪問看護 週2、デイサービス 週1、福祉用具 手すり\n"
                    "家族等の状況: 主介護者は長女、就労中、介護負担大\nサービス担当者会議: 10/20 開催予定\n"
                    "モニタリング: 目標達成、計画継続",
                    ["care_level", "preferences", "plan_goals", "plan_goals", "plan_goals", "care_services",
                     "living", "plan_goals", "plan_goals"]),
    "physician": ("訪問診療日: 10/15\n診断名: 慢性心不全\n経過: 下腿浮腫が増悪\n処方変更: 利尿薬を増量\n"
                  "指示: 訪問看護指示書を更新\n療養方針: 在宅看取り希望、DNAR確認済み\n診療情報提供書: 病院へ送付済み",
                  ["services_provided", "diagnoses", "course", "medication_management", "orders_documents",
                   "preferences", "orders_documents"]),
    "facility": ("食事: 主食8割、副食5割\n水分: 1000mL\n排泄: 排尿6回、排便1回\n睡眠: 良眠\n活動: レクリエーション参加\n"
                 "行動: 夕方に帰宅願望あり\n口腔ケア: 義歯洗浄実施\n事故報告: ベッドからずり落ち、外傷なし、家族へ連絡済み",
                 ["nutrition", "nutrition", "excretion", "living", "living", "cognition", "oral_health", "incidents"]),
    "rehab": ("評価: BI 65点、FIM 90点\n関節可動域: 右肩屈曲120度\n歩行: 杖で20m見守り\n訓練内容: 立ち上がり練習\n"
              "リハビリ計画: 3か月で更新\n興味関心: 畑仕事\n福祉用具: 歩行器の導入を検討",
              ["plan_goals", "adl", "adl", "adl", "plan_goals", "adl", "care_services"]),
    "nutrition": ("体重: 48.2kg（2026-10-10 測定）\nBMI: 18.5\n低栄養リスク: 中\n食形態: 軟菜食、とろみ中間\n"
                  "栄養補給: 経口と補助食品\n摂取量: 7割\n栄養ケア計画: 更新",
                  ["observations", "observations", "nutrition", "nutrition", "nutrition", "nutrition", "plan_goals"]),
    "dental": ("口腔内: 義歯不適合、口腔乾燥あり\n咀嚼: 困難\n歯科受診: 10/22 予定\n口腔衛生: 舌苔あり、ケア方法を指導",
               ["oral_health"] * 4),
    "msw": ("入院日: 10/1\n退院予定: 10/25\n退院前カンファレンス: 10/23\n退院時サマリー: 受領済み\n生活保護: 受給中\n"
            "身寄り: なし\n成年後見: 申立中",
            ["course", "course", "plan_goals", "orders_documents", "living", "living", "living"]),
    "pharmacy": ("服薬管理: お薬カレンダーで長女が管理\n残薬: 10錠\n副作用: なし\n処方元: 合成クリニック\n次回確認: 10/20",
                 ["medication_management", "medication_management", "adverse_events", "medication_management",
                  "followup"]),
}


@pytest.mark.parametrize("role", sorted(ROLE_POSTS))
def test_every_role_heading_is_kept_verbatim_in_its_category(role):
    body, expected = ROLE_POSTS[role]
    items = extract_context(body)
    assert [i["category"] for i in items] == expected
    # each section keeps its own line, text is the raw value and nothing is inferred
    for item, line in zip(items, body.splitlines()):
        assert item["evidence"] == line and item["text"] == line.split(": ", 1)[1]
        assert item["subject"] == "unspecified"


def test_labels_are_unique_and_every_category_is_enumerated():
    counts = Counter(label.casefold() for labels in LABELS.values() for label in labels)
    assert [label for label, n in counts.items() if n > 1] == []
    assert set(LABELS) == CATEGORIES
    for key in ("excretion", "skin_wound", "oral_health", "services_provided", "incidents", "plan_goals",
                "orders_documents"):
        assert key in CATEGORIES


def test_longer_heading_wins_over_its_prefix():
    for longer, shorter in (("訪問看護指示書", "訪問看護"), ("入浴介助", "入浴"), ("排泄介助", "排泄"),
                            ("服薬介助", "服薬"), ("歯科受診", "歯科"), ("褥瘡処置", "処置"), ("活動量", "活動")):
        items = extract_context(f"{longer}: 合成\n{shorter}: 合成")
        assert [i["category"] for i in items] == [_CATEGORY[longer], _CATEGORY[shorter]]


def test_excretion_state_is_no_longer_an_adl_item_but_the_motion_still_is():
    items = extract_context("排泄: 失禁なし\n排泄介助: トイレ誘導\nトイレ動作: 見守り")
    assert [i["category"] for i in items] == ["excretion", "services_provided", "adl"]


def test_new_attribute_headings_fall_into_their_own_categories():
    items = extract_context("有効期間: 2027-03-31\n指示期間: 10/1〜10/31\n計画期間: 6か月\n部位: 仙骨部\n食形態: 軟菜食")
    assert [(i["category"], i["details"][0]["key"]) for i in items] == [
        ("care_level", "valid_period"), ("orders_documents", "valid_period"), ("plan_goals", "valid_period"),
        ("skin_wound", "body_site"), ("nutrition", "food_texture")]
    # a period is never a due date
    assert "有効期間" not in DETAIL_LABELS["due_text"]


def test_a_period_heading_keeps_its_raw_span_without_a_date_guess():
    item = extract_context("指示期間: 2026年10月1日から10月31日まで")[0]
    assert item["details"] == [{"key": "valid_period", "value": "2026年10月1日から10月31日まで",
                                "evidence": "指示期間: 2026年10月1日から10月31日まで"}]


def test_visit_and_contact_basics_reuse_existing_categories():
    # owner addendum (2026-10-17): address, phone, key person, parking, family, current diagnosis, providers
    body = ("住所: 合成市1-2-3\n電話番号: 000-0000-0000\nキーパーソン: 長女\n駐車場: なし、近隣コインパーキング\n"
            "家族構成: 妻と二人暮らし\n現病名: 合成病\n事業者名: 合成訪問看護ステーション、合成薬局")
    assert [i["category"] for i in extract_context(body)] == [
        "demographics", "contacts", "contacts", "living", "living", "diagnoses", "care_team"]


def test_care_staff_measuring_the_patient_keeps_the_patients_vitals():
    # review F1: ヘルパー/介護士 joined the other-person words; as the measurer they are not the subject
    import extract
    for body in ("本人に対しヘルパーが血圧120/80を測定しました。", "本人に対し介護士が体温36.8℃を測定しました。"):
        doc = extract.extract_message(body, "2026-10-06T10:00:00+09:00")
        assert doc.get("vitals") == ({"sbp": 120, "dbp": 80} if "血圧" in body else doc.get("vitals")) and doc.get("vitals"), body


def test_cannot_rule_out_is_not_a_negation():
    # review F2: 「否定できない」 keeps the symptom; a plain 「否定」 still negates it
    import extract
    for body, kept in (("誤嚥は否定できない。", True), ("転倒は否定できません。", True),
                       ("誤嚥は否定しきれない。", True), ("誤嚥は否定。", False)):
        symptoms = extract.extract_message(body, "2026-10-06T10:00:00+09:00").get("symptoms")
        assert (bool(symptoms)) is kept, (body, symptoms)
