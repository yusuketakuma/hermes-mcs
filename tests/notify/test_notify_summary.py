"""📊 MCS summary (#31) and the shared display model (#32): scopes,
folding, per-chat dialects and the clicker-only view. Synthetic temp
ledger only."""
from __future__ import annotations

import json

import pytest

import notify_digest
import notify_render
from adapters.common import text
from adapters.slack.actions import _parts_blocks
from notify_testkit import (
    NOW, _add_request, _click, _delivered_card, _patient, led, pinned_clock)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


@pytest.mark.parametrize("raw,want", [
    ("", {"mine": False, "stations": [], "projects": [], "days": 1}),
    ("all", {"mine": False, "stations": [], "projects": [], "days": 1}),
    ("mine station:みどり station:あおば project:3,4 days:7",
     {"mine": True, "stations": ["みどり", "あおば"], "projects": [3, 4],
      "days": 7}),
    ("担当 施設:みどり 日数:2",
     {"mine": True, "stations": ["みどり"], "projects": [], "days": 2}),
])
def test_parse_scope(raw, want):
    assert notify_digest.parse_scope(raw) == want


@pytest.mark.parametrize("raw", ["days:8", "days:0", "project:1,x", "foo",
                                 "station:", "mine:1"])
def test_parse_scope_rejects(raw):
    assert isinstance(notify_digest.parse_scope(raw), str)


def _room(led, pid, name, station):
    _patient(led, pid, name=name)
    led.db.execute("UPDATE patients SET station_name=? WHERE project_id=?",
                   (station, pid))
    led.db.commit()


def test_scopes_narrow_and_caller_scope_always_applies(led):
    _room(led, 1, "患者A", "みどり訪問看護")
    _room(led, 2, "患者B", "あおば薬局")
    _room(led, 3, "患者C", "みどり訪問看護")
    _add_request(led, "残薬", "山田 太郎", pid=1)
    _add_request(led, "他人", "佐藤", pid=3)
    _add_request(led, "完了", "山田太郎", pid=2, status="done")

    def keep(raw, **kw):
        return notify_digest.scope_projects(
            led.db, notify_digest.parse_scope(raw), **kw)

    assert keep("all") is None
    assert keep("mine", name="山田　太郎") == {1}
    assert keep("mine", name="") == set()             # no name: nobody
    assert keep("station:みどり") == {1, 3}
    assert keep("station:みどり project:3,2") == {3}  # kinds AND
    assert keep("all", allowed=[2]) == {2}
    assert keep("station:みどり", allowed=[2]) == set()


def test_view_lists_only_scoped_patients_with_names(led):
    _room(led, 1, "患者A", "みどり訪問看護")
    _room(led, 2, "患者B", "あおば薬局")
    _add_request(led, "期限切れ", "山田", "2000-01-01", pid=1)
    _add_request(led, "範囲外", "山田", "2000-01-01", pid=2)
    got = notify_digest.view(led.db, {}, "station:みどり days:3", now=NOW)
    out = notify_render.parts_text(got["parts"])
    assert "患者A" in out and "患者B" not in out
    assert "対象: 施設 みどり 1人・直近3日" in out
    assert "未完了タスク 1件（期限切れ 1" in out
    assert "記録が見つからないことは対応がなかったことを意味せず" in out
    got = notify_digest.view(led.db, {}, "all", allowed=[2], now=NOW)
    assert "患者A" not in notify_render.parts_text(got["parts"])
    assert "error" in notify_digest.view(led.db, {}, "days:9", now=NOW)


def _long_parts(n):
    return {"containers": [
        {"type": "heading", "text": "見出し"},
        {"type": "text", "fold": True, "text": "\n".join(
            ["■ 新着"] + [f"・患者{i:03d} " + "x" * 40 for i in range(n)])},
        {"type": "text", "text": "■ 取得状況\n・なし"}],
        "footer": [{"type": "text", "text": "※ 注記"}]}


def test_fit_parts_folds_the_longest_list_and_keeps_the_rest():
    parts = notify_render.fit_parts(_long_parts(200), 1000)
    out = notify_render.display_text(parts)
    assert len(out) <= 1000
    assert "見出し" in out and "■ 取得状況\n・なし" in out and "※ 注記" in out
    kept = out.count("・患者")
    assert f"・…他{200 - kept}件" in out
    small = _long_parts(2)
    assert notify_render.fit_parts(small) == small


@pytest.mark.parametrize("n", range(100, 160, 3))
def test_fit_parts_stays_within_the_worker_spec_budget(n):
    """Regression: folding measured visible text only, so a folded
    daily summary landed just under 4000 while the worker's spec check
    (+4 per container / footer line) rejected it and the card was never
    sent."""
    from adapters.common import spec
    parts = _long_parts(n)
    parts["containers"].insert(1, {"type": "text", "text": "要約" * (n % 40)})
    parts["footer"].append({"type": "text", "text": "脚注1\n脚注2\n脚注3"})
    out = notify_render.fit_parts(parts)
    cost = (spec._containers_cost(out["containers"])[1]
            + spec._footer_cost(out["footer"])[1])
    assert cost <= spec.MAX_TOTAL_TEXT


def test_fetch_gap_disclosure_is_never_folded(led):
    """Regression: the coverage block outlived longer lists being
    folded — it must survive even when it is the longest list left."""
    _room(led, 1, "患者A", "みどり")
    for pid in range(2, 40):
        _room(led, pid, f"患者{pid}", "みどり")
        led.db.execute("UPDATE patients SET fetch_state='incomplete',"
                       "fetch_reason='network_error' WHERE project_id=?",
                       (pid,))
    led.db.commit()
    parts = notify_digest.build(led.db, {}, NOW - 86400, NOW, names=True)
    out = notify_render.display_text(notify_render.fit_parts(parts, 600))
    assert "■ 取得状況（記録ベース）" in out and "・取得未完了のルーム 38" in out
    assert "…他" not in out.split("■ 取得状況")[1]


def test_days_window_is_not_cut_by_notify_max_age(led):
    _room(led, 1, "患者A", "みどり")
    old = NOW - 5 * 86400
    led.db.execute(
        "INSERT INTO messages(message_id,project_id,sender_name,profession,"
        "posted_at,posted_at_ts,body_text,content_hash,body_state,first_seen)"
        " VALUES(900,1,'職員','医師','x',?,'本文',?,'full',?)",
        (int(old), "0" * 64, old))
    led.db.commit()
    got = notify_digest.view(led.db, {"notify_max_age_h": 48}, "days:7",
                             now=NOW)
    assert "新着 1件" in notify_render.parts_text(got["parts"])


def test_mine_without_a_name_asks_for_one(led):
    assert "名前" in notify_digest.view(led.db, {}, "mine", now=NOW)["error"]
    assert isinstance(notify_digest.parse_scope("days:²"), str)
    assert isinstance(notify_digest.parse_scope("project:１"), str)


def test_parts_text_dialects():
    parts = _long_parts(1)
    assert notify_render.parts_text(parts, "discord").startswith("## 見出し")
    assert "-# ※ 注記" in notify_render.parts_text(parts, "discord")
    assert notify_render.parts_text(parts, "slack").startswith("*見出し*")
    plain = notify_render.parts_text(parts, "plain")
    assert plain.startswith("【見出し】") and plain.endswith("※ 注記")


def test_slack_answer_is_a_block_kit_card():
    blocks = _parts_blocks({"action": "digest", "parts": _long_parts(2)})
    assert blocks[0] == {"type": "header", "text": {
        "type": "plain_text", "text": "見出し"}}
    assert blocks[-1]["type"] == "context"
    assert _parts_blocks({"action": "list", "parts": _long_parts(2)}) is None
    assert _parts_blocks({"action": "digest"}) is None


def test_modal_inputs_default_to_all_and_the_clicker():
    assert text.digest_inputs({}, "山田") == {"query": "all", "name": "山田"}
    assert text.digest_inputs({"query": " mine  days:2 ", "name": "佐藤"},
                              "山田") == {"query": "mine days:2",
                                          "name": "佐藤"}
    assert [f["id"] for f in text.modal_fields("digest", None, "山田")] == [
        "query", "name"]
    answer = text.view_answer({"outcome": "applied", "action": "digest",
                               "text": "## 見出し"}, lambda p: True)
    assert answer == [("## 見出し", None)]


def test_card_click_opens_scope_modal_then_answers_in_card_dialect(led):
    spec = _delivered_card(led)
    _add_request(led, "残薬", "山田 太郎", "2000-01-01")
    first = _click(led, spec, "digest")
    assert (first["outcome"], first["modal"]) == ("applied", True)
    r = _click(led, spec, "digest", {"query": "mine", "name": "山田 太郎"})
    assert (r["outcome"], r["action"]) == ("applied", "digest")
    assert r["text"].startswith("## 📊 MCS サマリー")      # discord card
    assert "担当（記録上: 山田 太郎）" in r["text"]
    assert "期限切れタスク 1件" in r["text"]
    stored = [json.loads(row["receipt_json"]) for row in led.db.execute(
        "SELECT receipt_json FROM command_receipts")]
    assert stored and not any("parts" in s or "text" in s for s in stored)
    bad = _click(led, spec, "digest", {"query": "days:99"})
    assert "絞込みを解釈できません" in bad["text"]


def test_daily_scope_is_validated_in_setup():
    import mcs_setup
    for scope, ok in (("station:みどり days:2", True), ("mine", False),
                      ("days:99", False), (3, False)):
        errs, _ = mcs_setup.validate_config(
            {"mcs_login_id": "u", "notify_target": "local",
             "daily_digest": {"enabled": True, "scope": scope}})
        assert any(e.startswith("daily_digest.scope") for e in errs) is not ok


def test_cli_prints_without_names_unless_asked(led, monkeypatch, capsys):
    import mcs_util
    _room(led, 1, "患者A", "みどり訪問看護")
    path = led.db.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setattr(mcs_util, "DB", path)
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    assert notify_digest.main(["--print", "station:みどり"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("【📊 MCS サマリー") and "患者A" not in out
    assert notify_digest.main(["--print", "days:99"]) == 2
