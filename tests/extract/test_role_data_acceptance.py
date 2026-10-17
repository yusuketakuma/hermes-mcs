"""Synthetic role boundaries stay distinguishable through extraction and private views."""
import pytest

import extract
import notify_views
import patient_context_view as views
from extract_testkit import _message
from test_read_model import _artifact, _db, _hash_of


@pytest.fixture
def records(tmp_path):
    db = _db(tmp_path, ())
    yield db
    db.close()


def _store(db, bodies, profession):
    for mid, body in enumerate(bodies, 1):
        posted = f"2026-10-{mid:02d}T09:00:00+09:00"
        db.save_messages([_message(mid, body, posted_at=posted, profession=profession)])
        saved = db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
        _artifact(db, "extract_v1", mid, extract.extract_message(saved, posted), {"hash": _hash_of(db, mid)})
    return views.evidence(db.db, 1, cfg={})


@pytest.mark.parametrize("profession,category,bodies", [
    ("施設介護職", "cognition", ["認知機能: 本日は未評価、異常の有無は未確認。",
                                  "認知機能: 本日は評価し、異常なしとの記録。"]),
    ("訪問介護員", "services_provided", ["実施内容: 入浴介助は明日に実施予定。",
                                          "実施内容: 本日、入浴介助を実施した。",
                                          "訪問中止: 入浴介助は本人の希望で中止、実施していない。"]),
    ("介護支援専門員", "care_level", ["有効期間: 2026年9月30日まで。期限切れ、更新結果は未確認。",
                                        "有効期間: 2026年10月1日から2027年3月31日までと記載。"]),
    ("歯科衛生士", "oral_health", ["口腔ケア: 長女が義歯洗浄を実施したと報告。投稿者自身は実施していない。",
                                    "口腔ケア: 本人が義歯洗浄を実施したと報告。"]),
    ("医療ソーシャルワーカー", "orders_documents", ["退院時サマリー: 病院へ送付した。受領は未確認。",
                                                      "退院時サマリー: 病院から受領したとの返信を確認。"]),
])
def test_reported_states_are_not_collapsed_or_promoted(records, profession, category, bodies):
    ev = _store(records, bodies, profession)
    background = views.background(ev)
    items = next(s["items"] for s in background["sections"] if s["category"] == category)
    assert [i["quote"] for i in items] == list(reversed(bodies))
    assert all(i["subject"] == "unspecified" and i["temporality"] is None for i in items)
    assert background["summary"] == [] and background["as_of"] is None
    rendered = notify_views.patient_context_list(
        records.db, 1, "background", cfg={}, since=0, now=1_800_000_000)
    for body in bodies:
        assert any(row["text"].startswith(body + "\n") for row in rendered["items"])
    if profession == "歯科衛生士":
        assert all(i["sender"] == "sender" and i["profession"] == profession for i in items)
        assert "長女が" in items[1]["quote"] and "本人が" in items[0]["quote"]
    if category == "care_level":
        assert all([d["key"] for d in i["details"]] == ["valid_period"] for i in items)
        assert "期限切れ" in items[1]["details"][0]["value"]


def test_meal_time_and_wound_site_stay_with_their_own_report(records):
    bodies = ["食事量: 朝食は8割。\n部位: 仙骨部\n皮膚: 仙骨部は発赤あり、右踵は未評価。",
              "食事量: 夕食は3割。\n部位: 右踵\n皮膚: 右踵に発赤なし、仙骨部は未評価。"]
    ev = _store(records, bodies, "訪問看護師・管理栄養士")
    meals = [i for i in ev["items"] if i["category"] == "nutrition"]
    assert [(i["message_id"], i["text"]) for i in meals] == [(2, "夕食は3割。"), (1, "朝食は8割。")]
    sites = [i for i in ev["items"] if i["category"] == "skin_wound" and i["details"]]
    assert [(i["message_id"], i["details"][0]["value"]) for i in sites] == [(2, "右踵"), (1, "仙骨部")]
    skin = [i["quote"] for i in views.timeline(ev)["items"]
            if i["category"] == "skin_wound" and "未評価" in i["quote"]]
    assert skin == ["皮膚: 仙骨部は発赤あり、右踵は未評価。", "皮膚: 右踵に発赤なし、仙骨部は未評価。"]


@pytest.mark.parametrize("profession,posts", [
    ("理学療法士", [("FIM 第1版", "65", "10月1日"), ("FIM 第2版", "70", "10月1日")]),
    ("薬剤師", [("残薬確認時体重", "45", "10月1日"), ("残薬確認時体重", "45", "10月2日")]),
])
def test_scale_versions_and_equal_values_on_different_days_are_not_conflicts(records, profession, posts):
    bodies = [f"検査項目: {name}\n測定値: {value}\n測定日: {day}" for name, value, day in posts]
    ev = _store(records, bodies, profession)
    assert views.discrepancies(ev)["items"] == []
    timeline = views.timeline(ev)["items"]
    assert {i["message_id"] for i in timeline} == {1, 2}
    for mid, (name, value, day) in enumerate(posts, 1):
        details = {d["key"]: d["value"] for i in timeline if i["message_id"] == mid for d in i["details"]}
        assert details == {"observation_name": name, "value": value, "measured_on": day}
        assert all(i["posted_at_ts"] and i["quote"] in bodies[mid - 1]
                   for i in timeline if i["message_id"] == mid)
