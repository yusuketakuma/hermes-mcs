"""📋 自分のタスク / 🗂 未確認一覧 / 🔎 患者内検索 — runner side, plus the
optional button row's component budget. Synthetic temp ledger only."""
from __future__ import annotations

import json
from itertools import count

import pytest

import notify_cards
import notify_cmds
import notify_render
from notify_testkit import (
    CFG, NOW, ORIGIN, _begin, _dispatch, _intent, _latest_render, _msg,
    _receipt, _seed_thread, _settle_bodies, _token_for, led)

__all__ = ["led"]

A = "discord:1001"
_N = count(1)


@pytest.fixture(autouse=True)
def _pin_wall_clock(monkeypatch):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)


def _spec(led, card_id=1):
    return json.loads(_latest_render(led, card_id)["spec_json"])


def _deliver(led):
    r = _latest_render(led)
    n = next(_N)
    _begin(led, r, n=n)
    _receipt(led, r, f"{n:016x}", message_id="m-9", n=5000 + n)
    _settle_bodies(led, r)


def _card(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    _deliver(led)
    return _spec(led)


def _click(led, spec, action, inputs=None, actor=A):
    tok = _token_for(spec, action)
    req = {"version": 1, "op": "notification",
           "command_id": f"{tok}:{next(_N):016x}", "actor": actor,
           "token": tok, "origin": dict(ORIGIN, message_id="m-9")}
    if inputs:
        req["input"] = inputs
    assert notify_cmds.validate_int(req) is None
    return notify_cards.apply_notification(led, req, CFG, now=NOW)


def _request(led, title, assignee=None, due=None, status="open", pid=1):
    rid = led.db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,"
        "title,assignee,due_date,status,revision,created_at,updated_at) "
        "VALUES(?,100,?,?,?,?,?,1,?,?)",
        (pid, "0" * 64, title, assignee, due, status, NOW, NOW)).lastrowid
    led.db.commit()
    return rid


def _texts(view):
    return [i["text"] for i in view["items"]]


def test_extra_row_is_budgeted(led):
    spec = _card(led)
    assert [b["id"] for b in spec["parts"]["action_rows"][-1]] == [
        "mytasks", "unacked", "search"]
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
    assert [b["id"] for b in rows[-1]] == ["mytasks"]
    content["containers"].append({"type": "text", "text": "x"})
    rows = notify_cards._action_rows(led.db, card, content, NOW)
    assert "mytasks" not in [b["id"] for r in rows for b in r]


def test_my_tasks_matches_display_name_overdue_first(led):
    spec = _card(led)
    later = _request(led, "来週の確認", "山田 太郎（みどり訪看）", "2026-12-01")
    late = _request(led, "昨日の確認", "山田太郎", "2026-01-01")
    undated = _request(led, "期限なし", "山田 太郎", status="in_progress")
    _request(led, "他人", "山田 太郎子")
    _request(led, "完了済み", "山田 太郎", "2026-01-01", status="done")
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


def test_my_tasks_without_name_says_why(led):
    spec = _card(led)
    _request(led, "件", "山田")
    view = _click(led, spec, "mytasks")["list"]
    assert view["items"] == [] and "表示名を取得できない" in view["empty"]


def test_unacked_lists_until_acknowledged(led):
    spec = _card(led)
    view = _click(led, spec, "unacked")["list"]
    assert view["head"] == ["未確認 1件（うち担当者あり 0件）"]
    item = view["items"][0]
    assert (item["project_id"], item["group"]) == (1, "患者A")
    assert "https://www.medical-care.net/projects/medical/1" in item["text"]
    assert "https://discord.com/channels/g1/ch1/m-9" in item["text"]
    assert any("作業が済んだかどうかは表しません" in n for n in view["notes"])

    _click(led, spec, "assign")
    _deliver(led)
    view = _click(led, _spec(led), "unacked")["list"]
    assert view["head"] == ["未確認 1件（うち担当者あり 1件）"]
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
    spec = _card(led)
    led.db.execute("UPDATE notification_cards SET updated_at=?",
                   (NOW - notify_render.UNACKED_WINDOW_S - 1,))
    led.db.commit()
    assert _click(led, spec, "unacked")["list"]["items"] == []


def test_search_opens_modal_then_answers_hits(led):
    spec = _card(led)
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
    spec = _card(led)
    for mid in range(110, 110 + notify_render.SEARCH_HITS + 3):
        _msg(led, mid, parent=100, body="定期訪問")
    view = _click(led, spec, "search", {"query": "訪問"})["list"]
    assert len(view["items"]) == notify_render.SEARCH_HITS
    assert view["more"] == 3


@pytest.mark.parametrize("inputs", [
    {}, {"query": ""}, {"query": "x" * 121}, {"other": "x"}, "x",
    {"query": 1}])
def test_bad_input_is_rejected(inputs):
    req = {"version": 1, "op": "notification",
           "command_id": f"{'a' * 32}:{'b' * 16}", "actor": A,
           "token": "a" * 32, "origin": ORIGIN, "input": inputs}
    assert notify_cmds.validate_int(req) == "bad_input"
