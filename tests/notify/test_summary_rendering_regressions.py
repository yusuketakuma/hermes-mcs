"""Synthetic regressions over the real summary, card, and platform render paths."""
import json
import sys

import pytest

import notify_cards
import notify_digest
import notify_flush
import notify_render
import summary_review
from adapters.common import summary
from adapters.discord import cards as discord_cards
from adapters.lineworks import cards as lineworks_cards
from adapters.slack import cards as slack_cards
from discord_testkit import _fake_discord
from ledger import publish_snapshot
from mcs_signals import record_station_staff
from notify_testkit import (
    CFG, NOW, _add_request, _card, _click, _deliver, _dispatch, _extract, _intent,
    _latest_render, _msg, _patient, _seed_thread, _signal_row, _spec, led, pinned_clock,
)
from test_summary_review import _setup

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def _render(parts, transport, monkeypatch) -> str:
    """Run the actual renderer with the already existing SDK-shaped stub."""
    scopes = {
        "discord": {"application_id": "a", "channel_id": "c", "guild_id": "g"},
        "slack": {"application_id": "a", "channel_id": "c", "team_id": "t"},
        "lineworks": {"application_id": "a", "channel_id": "c", "team_id": "t"},
    }
    if transport == "plain":
        return notify_render.parts_text(parts, "plain")
    scope = {**scopes[transport], "transport": transport}
    spec = notify_cards._notice_spec(7, parts, CFG, scope, 1, transport)
    if transport == "slack":
        _, blocks = slack_cards.render(spec)
        assert len(blocks) <= 50
        return "\n".join(
            b["text"]["text"] if "text" in b else b["elements"][0]["text"]
            for b in blocks if b["type"] != "divider")
    if transport == "discord":
        monkeypatch.setitem(sys.modules, "discord", _fake_discord())
        view = discord_cards.build_view(spec)
        texts = [child.content for child in view.items[0].children if hasattr(child, "content")]
        assert sum(map(len, texts)) <= 4000
        # Discord shows markup characters as same-length fullwidth look-alikes
        return "\n".join(texts).replace("＿", "_")
    face = lineworks_cards.render(spec)
    face_text = face["text"]
    assert isinstance(face_text, str) and len(face_text) <= 1000
    # The real sealed overflow is part of the same notice, never discarded.
    return face_text + "".join(
        spec["parts"]["thread_body_parts"][int(p["part_id"].rsplit(":", 1)[1]) - 1]
        for p in spec["parts"]["manifest"] if (p.get("name") or "").startswith("display#"))


@pytest.mark.parametrize("transport", ["plain", "slack", "discord", "lineworks"])
def test_empty_and_long_scoped_summary_keeps_disclosures_on_every_renderer(
        led, tmp_path, monkeypatch, transport):
    # Given: many allowed rooms, partial acquisition, and one excluded patient.
    for pid in range(1, 45):
        _patient(led, pid, name=f"Synthetic patient {pid} " + "名" * 40)
        _msg(led, 100 + pid, pid=pid, ts=int(NOW - 100), body="BODY-CANARY")
        led.db.execute("UPDATE messages SET first_seen=?", (NOW - 10,))
    _patient(led, 99, name="OUTSIDE-NAME-CANARY")
    _msg(led, 999, pid=99, body="OUTSIDE-BODY-CANARY")
    led.db.execute("UPDATE patients SET fetch_state='incomplete',fetch_reason='network_error' "
                   "WHERE project_id=1")
    led.db.commit()
    snapshot = publish_snapshot(str(tmp_path / "data" / "ledger.db"), str(tmp_path / "snapshot"))
    assert snapshot is not None
    # When: the actual command helper reads a scoped snapshot and each renderer consumes parts.
    answer = summary.answer(snapshot, "days:7", allowed=list(range(1, 45)),
                            now=NOW, dialect="plain")
    rendered = _render(answer["parts"], transport, monkeypatch)
    empty = _render(notify_digest.view(led.db, {}, "", allowed=[], now=NOW)["parts"],
                    transport, monkeypatch)
    # Then: long results are visibly folded, acquisition and epistemic limitations survive.
    assert "BODY-CANARY" not in rendered and "OUTSIDE-NAME-CANARY" not in rendered
    assert "project 99" not in rendered
    assert "他" in rendered
    assert "取得状況" in rendered and "network_error" in rendered
    assert "欠落なしの保証ではありません" in rendered
    assert "取得状況" in empty and "新着 0件" in empty
    assert "対応がなかったことを意味せず" in empty


def test_archived_patients_do_not_leak_tasks_signals_or_summary_updates(led):
    # Given: an archived room with each non-message summary source.
    _patient(led, 1)
    _patient(led, 2, name="ARCHIVED-NAME-CANARY", archived=1)
    _msg(led, 200, pid=2)
    _add_request(led, pid=2, src_mid=200, due="2000-01-01")
    _signal_row(led, "archived", pid=2, mids=[200], stype="adherence_concern")
    for at in (NOW - 2 * 86400, NOW - 10):
        led.artifact_add("karte_summary", '{"empty":false}', project_id=2)
        led.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id="
                       "(SELECT max(artifact_id) FROM artifacts)", (at,))
    led.db.commit()
    # When: building an unrestricted private summary.
    parts = notify_digest.view(led.db, {"signals": {"notify": True}}, "", now=NOW)["parts"]
    # Then: archive filtering applies to every source, not only the new-message query.
    out = notify_render.parts_text(parts)
    assert "ARCHIVED-NAME-CANARY" not in out and "project 2" not in out
    assert "未完了タスク 0件" in out and "アラート 0件" in out
    assert "連携サマリー更新" not in out


def _metadata(led, reactions, *, error=None):
    content = {"reactions": {"value": reactions, "observed_at": NOW - 10},
               "mentions": {"value": [{"type": "user", "id": 7}], "observed_at": NOW - 10}}
    led.db.execute("INSERT INTO message_metadata VALUES(1,'capture',?,?,?)",
                   (json.dumps(content), NOW - 5, error))
    led.db.commit()


@pytest.mark.parametrize(("reply_at", "error"), [(NOW + 10, None), (None, "network_error")])
def test_digest_uses_snapshot_bounded_response_observation(led, reply_at, error):
    # Given: a mention with either a future reply or failed historical UI observation.
    _patient(led)
    _msg(led, 1, ts=int(NOW - 100))
    with led.db:
        record_station_staff(led.db, [{"staff_id": 7, "is_self": True}])
        led.db.execute("UPDATE messages SET sender_id=8 WHERE message_id=1")
    _metadata(led, [] if error is None else [
        {"type": "completed", "count": 1, "self_reacted": True}], error=error)
    if reply_at is not None:
        _msg(led, 2, parent=1, ts=int(reply_at))
        with led.db:
            led.db.execute("UPDATE messages SET sender_id=7 WHERE message_id=2")
    # When: digesting the recorded response state at NOW.
    parts = notify_digest.view(led.db, {}, "", now=NOW)["parts"]
    # Then: neither future replies nor failed capture suppress observation gaps.
    out = notify_render.parts_text(parts)
    assert "自分宛で応答未観測" in out and "message 1 " in out
    assert "自分の返信観測なし" in out
    assert "記録が見つからない≠対応がなかった" in out


@pytest.mark.parametrize("kind", ["thread", "signal"])
def test_late_urgency_updates_delivered_card_without_new_intent(led, kind):
    # Given: an already delivered ordinary card.
    _seed_thread(led, mids=(100,))
    if kind == "signal":
        _signal_row(led, "s", mids=[100])
        event = _intent(led, "signal", payload={"signal_keys": ["s"], "project_id": 1})
    else:
        event = _intent(led, payload={"message_ids": [100]})
    _dispatch(led, event)
    _deliver(led)
    before = _latest_render(led)
    count = led.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0]
    # When: current AI high urgency lands after the first card was delivered.
    _extract(led, 100, {"urgency": "high"}, kind="extract_llm")
    notify_cards.sweep(led, CFG, now=NOW + 1)
    # Then: an update of the existing card exposes the source; it is not a new alert.
    after = _latest_render(led)
    assert after["op"] == "update" and after["render_rev"] == before["render_rev"] + 1
    assert "［緊急度高・AI判定］" in notify_render.display_text(
        json.loads(after["spec_json"])["parts"])
    assert led.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0] == count


@pytest.mark.parametrize(("kind", "stale", "level", "expected"), [
    ("extract_llm", False, "high", "llm"),
    ("extract_v1", False, "high", "rule"),
    ("extract_llm", True, "high", None),
    ("extract_llm", False, "routine", None),
])
def test_signal_card_and_message_notice_follow_same_current_urgency_source(
        led, monkeypatch, kind, stale, level, expected):
    # Given: a synthetic signal and a generation-bound extraction.
    _patient(led)
    _msg(led, 100)
    _extract(led, 100, {"urgency": level}, kind=kind, stale=stale)
    _signal_row(led, "s", mids=[100])
    event = _intent(led, "signal", payload={"signal_keys": ["s"], "project_id": 1})
    monkeypatch.setattr(notify_flush, "_config", lambda: CFG)
    # When: both actual notification surfaces render the same evidence.
    _dispatch(led, event)
    face = notify_render.display_text(notify_render._card_content(led.db, _card(led)))
    text, _ = notify_flush._format_event(
        led, _intent(led, payload={"message_ids": [100]}))
    # Then: stale/routine artifacts don't produce a high badge; sources remain distinct.
    if expected is None:
        assert "緊急度高" not in face and "緊急語あり" not in face
        assert "緊急度: 高" not in text and "緊急語を含む" not in text
    else:
        # the card tag and the text notice each keep the source distinct
        assert notify_render.URGENCY_TAG[expected] in face
        assert ("AI抽出" if expected == "llm" else "機械照合") in text


@pytest.mark.parametrize("field", ["content", "meta"])
def test_recursive_summary_json_returns_stable_reader_error_without_writes(tmp_path, field):
    # Given: a corrupted candidate artifact in an otherwise valid synthetic comparison.
    store, candidate = _setup(tmp_path)
    try:
        with store.db:
            store.db.execute(f"UPDATE artifacts SET {field}=? WHERE artifact_id=?",
                             ("[" * (sys.getrecursionlimit() + 100) + "0"
                              + "]" * (sys.getrecursionlimit() + 100), candidate))
        before = store.db.total_changes
        # When: reviewing the corrupted source.
        with pytest.raises(ValueError, match="candidate_" + ("meta_" if field == "meta" else "") + "malformed"):
            summary_review.comparison(store.db, 1, 1, candidate)
        # Then: a stable reader error leaves source, approval, and delivery untouched.
        assert store.db.total_changes == before
        assert not store.artifacts("semantic_adoption")
    finally:
        store.close()


def test_cli_mine_requires_the_same_name_as_private_command(led, monkeypatch, capsys):
    import mcs_util
    # Given: the CLI has only synthetic storage/configuration.
    _patient(led)
    monkeypatch.setattr(mcs_util, "DB", str(led.db.execute("PRAGMA database_list").fetchone()["file"]))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    # When: the caller asks for mine without an identity.
    result = notify_digest.main(["--print", "mine"])
    # Then: the CLI reports the same missing identity, never a misleading empty summary.
    assert result == 2
    out = capsys.readouterr()
    assert out.out == "" and "名前" in out.err


def test_page_confirmation_does_not_confirm_hidden_items_or_complete_requests(led):
    # Given: a real multi-page card with one recorded open request.
    mids = list(range(100, 112))
    _seed_thread(led, mids=mids)
    _add_request(led, "synthetic request")
    _dispatch(led, _intent(led, payload={"message_ids": mids}))
    _deliver(led)
    current = _spec(led)
    shown = json.loads(led.db.execute(
        "SELECT shown FROM notification_view_manifests WHERE manifest_id=?",
        (current["parts"]["manifest_id"],)).fetchone()[0])
    assert current["parts"]["pages"] > 1 and 0 < len(shown) < len(mids)
    # When: confirming this page, then navigating to the previous page.
    assert _click(led, current, "ack")["outcome"] == "applied"
    _deliver(led)
    assert _click(led, _spec(led), "prev")["outcome"] == "applied"
    # Then: the exact confirmed set is retained; neither hidden items nor requests are done.
    acked = json.loads(led.db.execute(
        "SELECT m.shown FROM notification_acknowledgements a "
        "JOIN notification_view_manifests m ON m.manifest_id=a.manifest_id "
        "WHERE a.withdrawn_at IS NULL").fetchone()[0])
    assert acked == shown
    ack_button = next(b for row in _spec(led)["parts"]["action_rows"]
                      for b in row if b["id"] == "ack")
    assert ack_button["style"] == "secondary"
    assert led.db.execute("SELECT status FROM requests").fetchone()[0] == "open"


@pytest.mark.parametrize("source", ["extract_llm", "extract_v1"])
def test_long_signal_explanation_cannot_hide_current_urgency_badge(led, source):
    # Given: a high-urgency signal whose explanation exceeds a physical card page.
    _patient(led)
    _msg(led, 100)
    _extract(led, 100, {"urgency": "high"}, kind=source)
    _signal_row(led, "s", mids=[100])
    row = led.db.execute("SELECT artifact_id,content FROM artifacts "
                         "WHERE kind='signal_v1'").fetchone()
    content = json.loads(row["content"])
    content["note"] = "synthetic long explanation " * 400
    with led.db:
        led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                       (json.dumps(content), row["artifact_id"]))
    # When: dispatching through actual page packing and sealed notice rendering.
    _dispatch(led, _intent(led, "signal", payload={"signal_keys": ["s"], "project_id": 1}))
    spec = _spec(led)
    # Then: source-labeled urgency stays visible even after physical truncation.
    face = notify_render.display_text(spec["parts"])
    assert notify_render.URGENCY_TAG[
        "llm" if source == "extract_llm" else "rule"] in face
    assert notify_render._text_cost(spec["parts"]) <= notify_render.CARD_TEXT_BUDGET
