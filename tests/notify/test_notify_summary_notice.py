"""📊 daily summary as a card-less ``op=notice`` render (#31 phase 2):
seal → render → begin/receipt → outbox accepted, bounded resend, kill
switch and stale-day suppression, plus the 📊 command entries.
Synthetic temp ledger only."""
from __future__ import annotations

import json

import pytest

import notify_cards
import notify_digest
from adapters.common import spec as spec_mod
from adapters.common import summary
from notify_testkit import (CFG, NOW, _add_request, _begin, _patient,
                            _receipt, led)

__all__ = ["led"]

DAILY = {**CFG, "daily_digest": {"enabled": True, "hour_jst": 0}}


def _enqueue(led, cfg=DAILY, now=NOW):
    _patient(led, 1, name="患者A")
    led.db.commit()
    assert notify_digest.maybe_enqueue(led, cfg, now=now) == 1
    return led.db.execute("SELECT * FROM notify_outbox WHERE kind="
                          "'daily_digest' ORDER BY event_id DESC").fetchone()


def _notices(led, ev):
    return notify_cards._notice_renders(led.db, ev["event_id"])


def _state(led, ev):
    return led.db.execute("SELECT state, route FROM notify_outbox WHERE "
                          "event_id=?", (ev["event_id"],)).fetchone()


def test_cards_on_queues_an_interactive_notice_with_text_fallback(led):
    ev = _enqueue(led)
    payload = json.loads(ev["payload"])
    assert ev["route"] == "interactive"
    assert payload["parts"]["containers"][0]["type"] == "heading"
    assert payload["text"].startswith("【🌅 MCS 日次サマリー")
    off = {k: v for k, v in DAILY.items() if k != "notify"}
    led.db.execute("DELETE FROM notify_outbox")
    led.db.commit()
    assert notify_digest.maybe_enqueue(
        led, {**off, "notify_target": "discord:1"}, now=NOW + 86400) == 1
    assert led.db.execute("SELECT route FROM notify_outbox").fetchone()[0] \
        == "text"


def test_notice_delivered_accepts_the_outbox_event(led):
    ev = _enqueue(led)
    out = notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
    assert out == {"dispatched": True}
    [r] = _notices(led, ev)
    spec = json.loads(r["spec_json"])
    assert (r["card_id"], r["op"], spec["op"]) == (None, "notice", "notice")
    assert "kind" not in spec and spec["parts"]["action_rows"] == []
    spec_mod.validate(spec)                  # the Discord v1 contract
    assert spec["delivery"]["intent_event_ids"] == [ev["event_id"]]
    assert _begin(led, r, n=1)["granted"] is True
    _receipt(led, r, f"{1:016x}", message_id="m-1", n=2)
    assert _state(led, ev)["state"] == "accepted"
    # a re-entry never issues a second notice
    assert notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW) \
        == {"skipped": True}


def test_real_failure_reissues_until_the_budget_then_holds(led):
    ev = _enqueue(led)
    for n in range(notify_cards.MAX_RESEND):
        notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
        r = _notices(led, ev)[-1]
        assert r["render_rev"] == n + 1
        _begin(led, r, n=10 + n)
        _receipt(led, r, f"{10 + n:016x}", result="not_sent",
                 message_id=None, error_code="http_500", n=100 + n)
        assert led.db.execute("SELECT next_try FROM notify_outbox WHERE "
                              "event_id=?", (ev["event_id"],)).fetchone()[0] \
            == NOW                           # re-armed now
    out = notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
    assert out == {"error": "resend_exhausted"}
    assert len(_notices(led, ev)) == notify_cards.MAX_RESEND
    assert json.loads(led.db.execute(
        "SELECT progress FROM notify_outbox WHERE event_id=?",
        (ev["event_id"],)).fetchone()[0])["hold_reason"] == "resend_exhausted"


@pytest.mark.parametrize("payload", ["not-json", '{"parts":{}}'])
def test_invalid_frozen_notice_payload_holds_with_reason(led, payload):
    ev = _enqueue(led)
    led.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?",
                   (payload, ev["event_id"]))
    led.db.commit()
    out = notify_cards.dispatch_intent(
        led, dict(led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                 (ev["event_id"],)).fetchone()), DAILY, now=NOW)
    assert out == {"error": "payload_invalid"}
    row = led.db.execute("SELECT state,progress FROM notify_outbox WHERE "
                         "event_id=?", (ev["event_id"],)).fetchone()
    assert row["state"] == "failed"
    assert json.loads(row["progress"]) == {"hold_reason": "payload_invalid"}


def test_kill_switch_falls_back_to_text_only_while_provably_unsent(led):
    ev = _enqueue(led)
    notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
    off = {**DAILY, "notify_target": "discord:1",
           "notify": {**DAILY["notify"], "interactive": None}}
    out = notify_cards.dispatch_intent(led, dict(ev), off, now=NOW)
    assert out == {"reverted": True}
    assert tuple(_state(led, ev)) == ("pending", "text")
    assert [r["state"] for r in _notices(led, ev)] == ["cancelled"]


def test_kill_switch_never_reverts_a_granted_notice(led):
    ev = _enqueue(led)
    notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
    _begin(led, _notices(led, ev)[0], n=1)        # granted, outcome unknown
    off = {**DAILY, "notify": {**DAILY["notify"], "interactive": None}}
    assert "reverted" not in notify_cards.dispatch_intent(
        led, dict(ev), off, now=NOW)
    assert _state(led, ev)["route"] == "interactive"


@pytest.mark.parametrize("why", ["disabled", "newer_day"])
def test_stale_or_disabled_day_is_suppressed(led, why):
    ev = _enqueue(led)
    notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
    cfg = DAILY
    if why == "disabled":
        cfg = {**DAILY, "daily_digest": {"enabled": False}}
    else:
        led.outbox_add("daily_digest", None, {"text": "x", "date": "z"})
    assert notify_cards.dispatch_intent(led, dict(ev), cfg, now=NOW) \
        == {"suppressed": True}
    assert _state(led, ev)["state"] == "suppressed"


def test_gc_strips_an_undelivered_notice_spec(led):
    ev = _enqueue(led)
    notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
    r = _notices(led, ev)[0]
    _begin(led, r, n=1)
    _receipt(led, r, f"{1:016x}", result="not_sent", message_id=None,
             error_code="http_500", n=2)
    notify_cards.gc(led, DAILY, now=NOW)
    assert _notices(led, ev)[0]["spec_json"] is None


def test_lineworks_notice_fits_or_seals_the_overflow():
    parts = {"containers": [{"type": "heading", "text": "見出し"},
                            {"type": "text", "text": "■ 取得状況\n" + "あ" * 1500}],
             "footer": [{"type": "text", "text": "※ 注記"}]}
    scope = {"application_id": "a", "channel_id": "c", "team_id": "t",
             "transport": "lineworks"}
    spec = notify_cards._notice_spec(7, parts, CFG, scope, 1, "lineworks")
    names = [p.get("name") for p in spec["parts"]["manifest"]]
    assert names[:2] == [None, "v1|notice|7"] and "display#1" in names
    from adapters.lineworks import cards as lw
    lw.validate(spec)


# ---------- command entries (snapshot, read-only) -----------------------

def _snapshot(led, tmp_path):
    path = tmp_path / "root" / "data" / "snapshots" / "ledger-snapshot.db"
    path.parent.mkdir(parents=True)
    (tmp_path / "root" / "config.json").write_text(json.dumps(
        {"notify_max_age_h": 48, "mcs_password": "never-read"}))
    led.db.commit()
    dst = __import__("sqlite3").connect(path)
    led.db.backup(dst)
    dst.close()
    return path


def test_answer_reads_the_snapshot_within_the_caller_scope(led, tmp_path):
    _patient(led, 1, name="患者A")
    _patient(led, 2, name="患者B")
    path = _snapshot(led, tmp_path)
    got = summary.answer(path, "all", allowed=[2], dialect="discord", now=NOW)
    assert got["text"].startswith("## 📊 MCS サマリー")
    assert "患者B" in got["text"] or "1人" in got["text"]
    assert "患者A" not in got["text"]
    assert "error" in summary.answer(path, "all", allowed=[], now=NOW)
    assert "名前" in summary.answer(path, "mine", now=NOW)["error"]
    assert "error" not in summary.answer(path, "mine name:山田", now=NOW)
    assert "error" in summary.answer(tmp_path / "missing.db", "all", now=NOW)
    assert summary._config(path) == {"notify_max_age_h": 48}


def test_answer_survives_snapshot_removal_after_read(led, tmp_path, monkeypatch):
    path = _snapshot(led, tmp_path)
    digest, _ = summary._modules()
    view = digest.view

    def removing_view(*args, **kwargs):
        got = view(*args, **kwargs)
        path.unlink()
        return got

    monkeypatch.setattr(digest, "view", removing_view)
    assert "text" in summary.answer(path, "all", now=NOW)


def test_discord_mcs_summary_op(led, tmp_path, monkeypatch):
    import hermes_plugin
    path = _snapshot(led, tmp_path)
    settings = {"snapshot": str(path), "allowed_user_ids": {"1001"},
                "allowed_chat_ids": {"42"}, "project_ids": frozenset({1})}
    ident = {"user_id": "1001", "chat_id": "42", "scope_id": None,
             "profile": None, "message_id": None}
    out = hermes_plugin._dispatch({"op": "summary", "scope": "days:2"},
                                  settings, ident)
    assert out.startswith("## 📊 MCS サマリー")
    denied = hermes_plugin._dispatch({"op": "summary"}, settings,
                                     {**ident, "user_id": "9"})
    assert json.loads(denied)["error"] == "user_not_allowed"
    assert json.loads(hermes_plugin._dispatch(
        {"op": "summary", "x": 1}, settings, ident))["error"] == "unknown_field"


def test_split_name():
    assert summary.split_name("mine name:山田 days:2") == ("mine days:2", "山田")
    assert summary.split_name("") == ("", "")



def test_a_moved_route_reissues_an_unsent_notice(led):
    ev = _enqueue(led)
    notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
    moved = {**DAILY, "notify": {**DAILY["notify"], "route_epoch": 2}}
    notify_cards.dispatch_intent(led, dict(ev), moved, now=NOW)
    first, second = _notices(led, ev)
    assert (first["state"], second["state"]) == ("cancelled", "queued")
    assert second["route_epoch"] == 2


def test_kill_switch_without_a_text_target_suppresses(led):
    ev = _enqueue(led)
    notify_cards.dispatch_intent(led, dict(ev), DAILY, now=NOW)
    off = {**DAILY, "notify": {**DAILY["notify"], "interactive": None}}
    off.pop("notify_target", None)
    assert notify_cards.dispatch_intent(led, dict(ev), off, now=NOW) \
        == {"suppressed": True}


def test_hermes_summary_keeps_names_out(led, tmp_path):
    import hermes_plugin
    _patient(led, 1, name="患者A")
    led.db.execute("UPDATE patients SET station_name='みどり'")
    _add_request(led, "残薬", "山田", "2000-01-01")     # listed by room
    path = _snapshot(led, tmp_path)
    out = hermes_plugin._dispatch(
        {"op": "summary", "scope": "station:みどり"},
        {"snapshot": str(path), "allowed_user_ids": {"1001"},
         "allowed_chat_ids": {"42"}, "project_ids": frozenset({1})},
        {"user_id": "1001", "chat_id": "42", "scope_id": None,
         "profile": None, "message_id": None})
    assert "期限切れタスク 1件: project 1" in out and "患者A" not in out
    assert "患者A" in summary.answer(path, "station:みどり", now=NOW)["text"]


def test_answer_survives_an_unreadable_snapshot(tmp_path):
    bad = tmp_path / "root" / "data" / "snapshots" / "ledger-snapshot.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database")
    assert summary.answer(bad, "all", now=NOW) == {
        "error": summary.SNAPSHOT_MISSING}
