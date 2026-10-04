"""📋 自分のタスク / 🗂 未確認一覧 / 🔎 患者内検索 — runner side, plus the
optional button row's component budget. Synthetic temp ledger only."""
from __future__ import annotations

import json

import pytest

import notify_cards
import notify_cmds
import notify_render
import notify_views
from notify_testkit import (
    CFG, NOW, ORIGIN, _add_request, _click, _deliver, _delivered_card, _msg,
    _spec, led, pinned_clock)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")

A = "discord:1001"


def _texts(view):
    return [i["text"] for i in view["items"]]


def test_extra_row_is_budgeted(led):
    spec = _delivered_card(led)
    assert [b["id"] for b in spec["parts"]["action_rows"][-1]] == [
        "digest", "mytasks", "unacked", "search"]
    card = notify_cards._card_row(led.db, 1)
    content = notify_render._card_content(led.db, card)
    content["manifest_id"] = 1
    base = sum(c["type"] != "meta" for c in content["containers"]) \
        + sum(f["type"] == "text" for f in content["footer"])
    # fill the card to 2 slots short of the 40-component ceiling
    rows = notify_cards._action_rows(led.db, card, content, NOW)
    fixed = sum(len(r) + 1 for r in rows[:-1])
    content["containers"] += [{"type": "text", "text": "x"}] * (
        notify_cards.MAX_COMPONENTS - base - fixed - 2)
    rows = notify_cards._action_rows(led.db, card, content, NOW)
    # one slot for the row itself leaves room for one button only
    assert [b["id"] for b in rows[-1]] == ["digest"]
    content["containers"].append({"type": "text", "text": "x"})
    rows = notify_cards._action_rows(led.db, card, content, NOW)
    assert "digest" not in [b["id"] for r in rows for b in r]


def test_my_tasks_matches_display_name_overdue_first(led):
    spec = _delivered_card(led)
    later = _add_request(led, "来週の確認", "山田 太郎（みどり訪看）", "2026-12-01")
    late = _add_request(led, "昨日の確認", "山田太郎", "2026-01-01")
    undated = _add_request(led, "期限なし", "山田 太郎", status="in_progress")
    _add_request(led, "他人", "山田 太郎子")
    _add_request(led, "完了済み", "山田 太郎", "2026-01-01", status="done")
    r = _click(led, spec, "mytasks", {"name": "山田　太郎"})
    assert (r["outcome"], r["action"]) == ("applied", "list")
    view = r["list"]
    assert view["head"] == ["未完了 3件（うち期限切れ 1件）"]
    assert _texts(view) == [
        f"・⚠ 期限切れ #{late} 昨日の確認 — 期限 2026-01-01 — 患者A",
        f"・#{later} 来週の確認 — 期限 2026-12-01 — 患者A",
        f"・#{undated} 期限なし — ⏳対応中 — 患者A"]
    assert all(i["project_id"] == 1 for i in view["items"])
    assert any("手入力の別表記" in n for n in view["notes"])
    assert any("対応がなかったことを意味しません" in n for n in view["notes"])
    # a different name is a different command — never a conflict
    other = _click(led, spec, "mytasks", {"name": "佐藤"})
    assert other["list"]["items"] == []
    stored = [json.loads(x[0]) for x in led.db.execute(
        "SELECT receipt_json FROM command_receipts")]
    assert all("list" not in s for s in stored)


def test_list_counts_respect_the_plugin_project_scope(led):
    """Head counts cover only the projects the plugin may show — the
    plugin's static project list rides in input.projects."""
    spec = _delivered_card(led)
    led.db.execute("INSERT INTO patients(project_id,patient_name,"
                   "is_archived) VALUES(2,'患者B',0)")
    _add_request(led, "範囲内", "山田", "2026-01-01")
    _add_request(led, "範囲外", "山田", "2026-01-01", pid=2)
    view = _click(led, spec, "mytasks",
                  {"name": "山田", "projects": [1]})["list"]
    assert view["head"] == ["未完了 1件（うち期限切れ 1件）"]
    assert [i["project_id"] for i in view["items"]] == [1]
    assert _click(led, spec, "mytasks", {"name": "山田"})["list"]["head"] \
        == ["未完了 2件（うち期限切れ 2件）"]
    view = _click(led, spec, "unacked", {"projects": [2]})["list"]
    assert view["head"][0] == "未確認 0件（うち担当者あり 0件）"
    for bad in ({"projects": []}, {"projects": ["1"]}, {"projects": [0]},
                {"projects": 1}, {"projects": [1] * 1001}):
        req = {"version": 1, "op": "notification", "command_id": "x:y",
               "actor": A, "token": "a" * 32, "input": bad,
               "origin": dict(ORIGIN, message_id="m-9")}
        assert notify_cmds.validate_int(req) is not None


def test_my_tasks_without_name_says_why(led):
    spec = _delivered_card(led)
    _add_request(led, "件", "山田")
    view = _click(led, spec, "mytasks")["list"]
    assert view["items"] == [] and "表示名を取得できない" in view["empty"]


def test_unacked_lists_until_acknowledged(led):
    spec = _delivered_card(led)
    view = _click(led, spec, "unacked")["list"]
    assert view["head"][0] == "未確認 1件（うち担当者あり 0件）"
    item = view["items"][0]
    assert (item["project_id"], item["group"]) == (1, "患者A")
    assert "https://www.medical-care.net/projects/medical/1" in item["text"]
    assert "https://discord.com/channels/g1/ch1/m-9" in item["text"]
    assert any("作業が済んだかどうかは表しません" in n for n in view["notes"])

    _click(led, spec, "assign")
    _deliver(led)
    view = _click(led, _spec(led), "unacked")["list"]
    assert view["head"][0] == "未確認 1件（うち担当者あり 1件）"
    assert "担当中: <@1001>" in view["items"][0]["text"]

    _click(led, _spec(led), "ack")
    _deliver(led)
    view = _click(led, _spec(led), "unacked")["list"]
    assert view["items"] == [] and view["head"][0].startswith("未確認 0件")

    # new content since the ack -> unconfirmed again
    _msg(led, 102, parent=100)
    notify_cards.sweep(led, CFG, now=NOW)
    assert _click(led, _spec(led), "unacked")["list"]["items"]


def test_unacked_leaves_out_old_cards(led):
    spec = _delivered_card(led)
    led.db.execute("UPDATE notification_cards SET updated_at=?",
                   (NOW - notify_views.UNACKED_WINDOW_S - 1,))
    led.db.commit()
    assert _click(led, spec, "unacked")["list"]["items"] == []


@pytest.mark.parametrize("transport", ["discord", "slack", "lineworks"])
def test_unacked_observes_new_content_before_a_card_refresh(led, transport):
    spec = _delivered_card(led)
    _click(led, spec, "ack")
    _deliver(led)
    led.db.execute("UPDATE notification_cards SET transport=?", (transport,))
    led.db.commit()
    assert notify_views.unacked_view(led.db, transport, NOW, [1])["items"] == []
    _msg(led, 102, parent=100, body="合成の未確認返信")
    # No sweep or click on this card has persisted a new source generation.
    view = notify_views.unacked_view(led.db, transport, NOW, [1])
    assert view["head"][0] == "未確認 1件（うち担当者あり 0件）"
    assert [item["project_id"] for item in view["items"]] == [1]
    assert notify_views.unacked_view(led.db, transport, NOW, [2])["items"] == []


def test_search_opens_modal_then_answers_hits(led):
    spec = _delivered_card(led)
    for mid, body in ((102, "昨日から 発熱 あり。解熱剤を使用"),
                      (103, "発熱なし、食欲あり")):
        _msg(led, mid, parent=100, body=body, prof="看護師")
    first = _click(led, spec, "search")
    assert first["modal"] is True and first["action"] == "search"
    r = _click(led, spec, "search", {"query": "発熱 解熱"})
    view = r["list"]
    assert view["head"][0].startswith("1件（新しい順）")
    assert view["items"] == [{"project_id": 1, "text":
                              "・09-24 08:42 看護師: 昨日から 発熱 あり。解熱剤を使用"}]
    assert any("まだ取得していない範囲は検索されません" in n
               for n in view["notes"])
    assert any(n.startswith("履歴取得:") for n in view["notes"])
    # the hits reach the clicker once — never the durable receipt
    stored = led.db.execute("SELECT receipt_json FROM command_receipts "
                            "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert "解熱剤" not in stored
    none = _click(led, spec, "search", {"query": "該当しない語"})["list"]
    assert none["items"] == [] and none["head"][0].startswith("0件")


def test_search_caps_hits_and_counts_the_rest(led):
    spec = _delivered_card(led)
    for mid in range(110, 110 + notify_views.SEARCH_HITS + 3):
        _msg(led, mid, parent=100, body="定期訪問")
    view = _click(led, spec, "search", {"query": "訪問"})["list"]
    assert len(view["items"]) == notify_views.SEARCH_HITS
    assert view["more"] == 3


@pytest.mark.parametrize("inputs", [
    {}, {"query": ""}, {"query": "x" * 121}, {"other": "x"}, "x",
    {"query": 1}])
def test_bad_input_is_rejected(inputs):
    req = {"version": 1, "op": "notification",
           "command_id": f"{'a' * 32}:{'b' * 16}", "actor": A,
           "token": "a" * 32, "origin": ORIGIN, "input": inputs}
    assert notify_cmds.validate_int(req) == "bad_input"
