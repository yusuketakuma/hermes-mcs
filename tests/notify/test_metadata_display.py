"""スタンプ表示はcaptureの観測だけを使い、業務状態と本文配送を維持する。"""
import json
import sqlite3

import pytest

import ledger
import mcs_signals
import mcs_view
import notify_digest
import notify_render
import notify_views
from message_metadata import (
    get_message_metadata, get_metadata_shadow_status, stamp_line)
from notify_testkit import (
    CFG, NOW, _add_request, _card, _delivered_card, _dispatch, _intent, _msg, _patient,
    led, pinned_clock,
)

__all__ = ["led", "pinned_clock"]


def _metadata(led, mid, reactions, *, source="capture", at=NOW, error=None):
    content = {} if reactions is None else {"reactions": {
        "value": reactions, "observed_at": at}}
    led.db.execute("INSERT OR REPLACE INTO message_metadata "
                   "(message_id,source,content,checked_at,last_error) VALUES(?,?,?,?,?)",
                   (mid, source, json.dumps(content), at, error))
    led.db.commit()


def _react(kind="viewed", mine=True):
    return {"type": kind, "count": 1, "self_reacted": mine}


def test_old_snapshot_and_shadow_are_unfetched(led):
    with sqlite3.connect(":memory:") as old:
        meta = get_message_metadata(old, 100)
        assert meta["reactions"] is None
        assert stamp_line(meta) == "MCS スタンプ未取得"
        shadow = get_metadata_shadow_status(old, 100, as_of=NOW)
        assert shadow["state"] == "not_attempted"
        assert shadow["reactions_observed_at"] is None
        assert "reactions" not in shadow
    _patient(led)
    _msg(led, 100)
    _metadata(led, 100, [_react()], source="shadow")
    assert get_message_metadata(led.db, 100)["reactions"] is None
    _metadata(led, 100, [])
    assert stamp_line(get_message_metadata(led.db, 100)).startswith("MCS スタンプなし · 観測 ")
    _metadata(led, 100, None)
    assert get_message_metadata(led.db, 100)["reactions"] is None


@pytest.mark.parametrize("reactions", [None, ["broken"], [_react() | {"count": -1}],
                                      [_react() | {"self_reacted": 1}]])
def test_invalid_or_missing_metadata_never_means_zero(led, reactions):
    _patient(led)
    _msg(led, 100)
    _metadata(led, 100, reactions)
    assert get_message_metadata(led.db, 100)["reactions"] is None


def test_footer_is_read_only_and_unknown_labels_cannot_mention(led):
    _patient(led)
    _msg(led, 100)
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    card = _card(led)
    before_content = notify_render._card_content(led.db, card)
    before_source = notify_render._source_fp(led.db, card)
    _, before_body = notify_render._card_body_text(led.db, card, {"shown": "[100]"})
    _metadata(led, 100, [_react("completed"), _react("<@UFAKE>\n@everyone")],
              error="reaction_error")
    content = notify_render._card_content(led.db, card)
    footer = "\n".join(item["text"] for item in content["footer"])
    assert "MCS ✅1 ❔1 · 自分 1投稿" in footer
    assert "再取得失敗" in footer and "@" not in footer
    assert content["toggles"] == {"acked": False, "has_tasks": False, "assigned": False}
    assert (content["pages"], content["shown"]) == (
        before_content["pages"], before_content["shown"])
    assert notify_render._source_fp(led.db, card) == before_source
    # stamps change only each post's own MCS line, never the rest
    after_body = notify_render._card_body_text(led.db, card, {"shown": "[100]"})[1]

    def strip(t):
        return [ln for ln in t.split("\n") if not ln.startswith("MCS ")]
    assert strip(after_body) == strip(before_body)
    assert "MCS ✅1 ❔1（自分 ✅❔） · 観測 " in after_body
    assert led.db.execute("SELECT count(*) FROM notification_acknowledgements").fetchone()[0] == 0


def test_same_name_sender_uses_id_on_card_and_snapshot(led, tmp_path):
    _patient(led)
    _msg(led, 100, sender="合成同名")
    _msg(led, 101, sender="合成同名", parent=100)
    _msg(led, 102, sender="合成同名", parent=100)
    _msg(led, 103, sender="合成同名", parent=100)
    led.db.execute("UPDATE messages SET sender_id=message_id-93")
    led.db.execute("UPDATE messages SET sender_id=NULL WHERE message_id=103")
    mcs_signals.record_station_staff(led.db, [{"staff_id": 7, "is_self": True},
                                            {"staff_id": 8, "is_self": False}])
    _metadata(led, 100, [_react()])
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101]}))
    content = notify_render._card_content(led.db, _card(led))
    text = notify_render.display_text(content)
    assert text.count("合成同名（自分）") == 1
    snap = ledger.publish_snapshot(str(tmp_path / "data" / "ledger.db"),
                                   str(tmp_path / "snap"))
    view = mcs_view.View(snap)
    try:
        messages = view.read("timeline", project=1)["items"]
        assert messages[0]["is_self_sender"] is True
        assert messages[0]["is_own_station_sender"] is True
        assert messages[0]["message_metadata"]["reactions"][0]["count"] == 1
        replies = {row["message_id"]: row for row in
                   view.read("thread", project=1, message_id=100)["items"]}
        assert all(row["is_self_sender"] is False for row in replies.values())
        assert replies[101]["is_own_station_sender"] is True
        assert replies[102]["is_own_station_sender"] is False
        assert replies[103]["is_own_station_sender"] is None
    finally:
        view.close()


def test_schema7_snapshot_reads_as_unfetched(led, tmp_path):
    _patient(led)
    _msg(led, 100)
    snap = ledger.publish_snapshot(str(tmp_path / "data" / "ledger.db"),
                                   str(tmp_path / "snap"))
    with sqlite3.connect(snap) as db:
        db.execute("DROP TABLE message_metadata")
        db.execute("PRAGMA user_version=7")
    view = mcs_view.View(snap)
    try:
        message = view.read("evidence", project=1, message_id=100)["message"]
        assert message["message_metadata"]["reactions_status"] == "not_fetched"
        assert message["message_metadata"]["reactions_age_s"] is None
        assert message["metadata_shadow"]["state"] == "not_attempted"
        assert message["is_self_sender"] is False
        assert message["is_own_station_sender"] is None
    finally:
        view.close()


def test_cli_capture_freshness_and_separate_shadow_status(led, tmp_path):
    _patient(led)
    _msg(led, 100)
    _metadata(led, 100, [], at=NOW - 7200)
    _metadata(led, 100, [_react("accepted")], source="shadow", at=NOW - 100,
              error="network_error")
    snap = ledger.publish_snapshot(str(tmp_path / "data" / "ledger.db"),
                                   str(tmp_path / "snap"))
    with sqlite3.connect(snap) as db:
        db.execute("UPDATE snapshot_meta SET generated_at=?", (NOW,))
    view = mcs_view.View(snap)
    try:
        message = view.read("evidence", project=1, message_id=100)["message"]
        capture, shadow = message["message_metadata"], message["metadata_shadow"]
        assert capture["reactions"] == [] and capture["reactions_age_s"] == 7200
        assert capture["source"] == "capture" and capture["age_as_of"] == NOW
        assert "遅延" in capture["freshness_note"]
        assert shadow["state"] == "failed" and shadow["last_error"] == "network_error"
        assert shadow["checked_at"] == NOW - 100
        assert shadow["reactions_observed_at"] == NOW - 100
        assert shadow["reactions_age_s"] == 100 and shadow["displayed"] is False
        assert "accepted" not in json.dumps(shadow)
        assert "reactions" not in shadow and "self_reacted" not in shadow
    finally:
        view.close()


@pytest.mark.parametrize(("reactions", "expected"), [
    ([], "observed"), (None, "checked_without_reactions"), (["invalid"], "invalid")])
def test_shadow_status_never_contains_reaction_values(led, reactions, expected):
    _patient(led)
    _msg(led, 100)
    _metadata(led, 100, reactions, source="shadow")
    shadow = get_metadata_shadow_status(led.db, 100, as_of=NOW - 1)
    assert shadow["state"] == expected
    assert "reactions" not in shadow and "count" not in shadow
    assert "self_reacted" not in shadow
    assert shadow["reactions_age_s"] == (0 if expected == "observed" else None)
    assert get_message_metadata(led.db, 100)["reactions"] is None


def test_large_observation_time_is_not_rendered(led):
    _patient(led)
    _msg(led, 100)
    _metadata(led, 100, [_react()])
    led.db.execute("UPDATE message_metadata SET content=?", (json.dumps({
        "reactions": {"value": [_react()], "observed_at": 10**1000}}),))
    assert get_message_metadata(led.db, 100)["reactions"] is None


@pytest.mark.parametrize(("name_length", "structured_length", "count"), [
    (60, 383, 8), (4000, 3200, 10)])
def test_stamps_and_busy_footer_fit_card_budget(
        led, monkeypatch, name_length, structured_length, count):
    from hermes_plugin.mcs_delivery import spec as spec_mod
    _patient(led, name="合" * name_length)
    mids = list(range(100, 100 + count))
    for mid in mids:
        _msg(led, mid, parent=None if mid == 100 else 100, body=f"合成本文{mid}")
        _metadata(led, mid, [_react(kind) for kind in (
            "viewed", "accepted", "thanked", "good", "completed", "future")],
                  error="reaction_error")
    _dispatch(led, _intent(led, payload={"message_ids": mids}))
    for _ in range(5):
        _add_request(led, title="題" * 100, assignee="担" * 100,
                     due="2020-01-01")
    monkeypatch.setattr(notify_render, "current_ackers", lambda *args: [
        f"discord:{10**18 + n}" for n in range(10)])
    monkeypatch.setattr(notify_render, "feedback_pending", lambda *args: True)
    led.db.execute("INSERT INTO notification_triage"
                   "(card_id,owner,state,last_actor,updated_at) "
                   "VALUES(1,'discord:1000000000000000000','assigned','a',?)", (NOW,))
    monkeypatch.setattr(notify_render, "_structured_block", lambda *args: {
        "type": "text", "text": "合" * structured_length})
    card = _card(led)
    source_fp = notify_render._source_fp(led.db, card)
    first = notify_render._card_content(led.db, card)
    assert first["pages"] > 1
    shown = []
    for page in range(first["pages"]):
        content = notify_render._card_content(
            led.db, card | {"ui_state": json.dumps({"page": page})})
        assert content["pages"] == first["pages"]
        cost = spec_mod._containers_cost(content["containers"])[1]
        cost += spec_mod._footer_cost(content["footer"])[1]
        assert cost <= spec_mod.MAX_TOTAL_TEXT
        assert notify_render._blocks_len(content["containers"]) == \
            spec_mod._containers_cost(content["containers"])[1]
        assert notify_render._footer_len(content["footer"]) == \
            spec_mod._footer_cost(content["footer"])[1]
        footer = "\n".join(item["text"] for item in content["footer"])
        assert "MCS 👀" in footer and "自分 " in footer and "再取得失敗" in footer
        assert f"{page + 1}/{first['pages']} ページ" in footer
        _, body = notify_render._card_body_text(
            led.db, card, {"shown": json.dumps(content["shown"])}, max_chars=None)
        assert "合" * structured_length in body
        for mid in content["shown"]:
            assert f"合成本文{mid}" in body
        shown.extend(content["shown"])
    assert shown == mids
    assert notify_render._source_fp(led.db, card) == source_fp


def test_unacked_remains_unacked_and_partitions_within_patient(led, pinned_clock):
    _delivered_card(led)
    _msg(led, 200)
    _dispatch(led, _intent(led, payload={"message_ids": [200]}))
    led.db.execute("UPDATE notification_cards SET message_id='delivered-2'")
    _metadata(led, 100, [_react("accepted")])
    view = notify_views.unacked_view(led.db, "discord", now=NOW)
    assert view["head"] == ["未確認 2件（うち担当者あり 0件）",
                            "MCSで本人反応あり 1件（確認状態は変えません）"]
    assert "MCSスタンプも承認・作業完了を保証せず" in view["notes"][0]
    assert "🙆" not in view["items"][0]["text"]
    assert "MCS 🙆1 · 自分 1投稿" in view["items"][1]["text"]
    assert "未確認" in view["items"][1]["text"]
    assert led.db.execute("SELECT count(*) FROM notification_acknowledgements").fetchone()[0] == 0


def test_shadow_watch_uses_current_ack_generation(led, pinned_clock):
    _delivered_card(led)
    manifest = led.db.execute(
        "SELECT * FROM notification_view_manifests ORDER BY manifest_id DESC LIMIT 1"
    ).fetchone()
    with led.db:
        led.db.execute(
            "INSERT INTO notification_acknowledgements"
            "(card_id,manifest_id,actor,command_id,created_at) VALUES(1,?,'synthetic','old-ack',?)",
            (manifest["manifest_id"], NOW),
        )
    assert led.metadata_watch_targets(now=NOW) == []
    with led.db:
        led.db.execute("UPDATE notification_cards SET source_generation=source_generation+1")
        new_manifest_id = led.db.execute(
            "INSERT INTO notification_view_manifests"
            "(card_id,render_rev,source_generation,presentation_generation,shown,created_at)"
            " VALUES(1,?,?,?, ?,?)",
            (manifest["render_rev"] + 1, manifest["source_generation"] + 1,
             manifest["presentation_generation"], manifest["shown"], NOW),
        ).lastrowid
    assert len(notify_views.unacked_view(led.db, "discord", now=NOW)["items"]) == 1
    assert [row["message_id"] for row in led.metadata_watch_targets(now=NOW)] == [100, 101]
    with led.db:
        led.db.execute(
            "INSERT INTO notification_acknowledgements"
            "(card_id,manifest_id,actor,command_id,created_at) VALUES(1,?,'synthetic','new-ack',?)",
            (new_manifest_id, NOW),
        )
    assert led.metadata_watch_targets(now=NOW) == []


def test_digest_counts_observation_window_not_post_time_or_shadow(led):
    _patient(led)
    _patient(led, 2, archived=1)
    for mid in range(100, 108):
        _msg(led, mid, pid=2 if mid == 106 else 1, ts=int(NOW - 99999))
    _metadata(led, 100, [_react()], at=NOW - 10)
    _metadata(led, 101, [_react("completed")], at=NOW - 20)
    _metadata(led, 102, [_react()], at=NOW)
    _metadata(led, 103, [_react()], at=NOW - 30)
    _metadata(led, 104, [_react()], at=NOW - 10, source="shadow")
    _metadata(led, 105, [_react(mine=False)], at=NOW - 10)
    _metadata(led, 106, [_react()], at=NOW - 10)
    _metadata(led, 107, [_react()], at=NOW - 10)
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=107")
    text = notify_digest.build_text(led.db, CFG, NOW - 20, NOW)
    assert "■ MCS 本人スタンプ観測: 2投稿" in text
    assert "完了 1" in text and "見ました 1" in text
    assert "押下時刻・操作件数・業務完了を表しません" in text
    assert "職員" not in text


def test_summary_stamp_counts_follow_project_scope(led):
    _patient(led, 1)
    _patient(led, 2)
    _msg(led, 100, pid=1)
    _msg(led, 101, pid=2)
    _metadata(led, 100, [_react("viewed")], at=NOW - 10)
    _metadata(led, 101, [_react("completed")], at=NOW - 10)
    got = notify_digest.view(led.db, CFG, "all", allowed=[1], now=NOW)
    text = notify_render.parts_text(got["parts"])
    stamps = text.split("■ MCS 本人スタンプ観測:", 1)[1].split("■", 1)[0]
    assert "1投稿" in stamps and "見ました 1" in stamps
    assert "完了 1" not in stamps
