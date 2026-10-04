"""E1/E2 and real sender regressions using only a synthetic ledger and send stubs."""
import json
import sqlite3

import pytest

import notify_flush
import notify_urgent
from ledger import Ledger
from mcs_signals import record_station_staff
from notify_testkit import (
    CFG, NOW, _add_request, _click, _deliver, _dispatch, _extract, _intent,
    _msg, _patient, _signal_row, _spec, led,
)

__all__ = ["led"]


@pytest.fixture
def world(led, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(notify_flush.time, "time", lambda: clock[0])
    monkeypatch.setattr(notify_flush.time, "monotonic", lambda: clock[0])
    cfg = {**CFG, "notify_target": "discord:1", "urgency_escalation": {
        "mode": "on", "source": "llm", "room_cooldown_min": 1}}
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    calls = []
    monkeypatch.setattr(notify_flush, "_send",
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    _patient(led)
    _msg(led, 100, ts=int(NOW - 7200), body="PRIVATE-BODY-CANARY")
    with led.db:
        led.db.execute("UPDATE messages SET sender_id=8 WHERE message_id=100")
        record_station_staff(led.db, [{"staff_id": 7, "is_self": True}])
    return led, cfg, clock, calls


def _base(store, *, mid=100, at=NOW - 1800, state="accepted", interactive=False):
    with store.db:
        event = store.outbox_add_tx("new_messages", 1, {"message_ids": [mid]},
                                   route="interactive" if interactive else "text")
    row = store.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event,)).fetchone()
    with store.db:
        notify_urgent.capture_initial(
            store, {"urgency_escalation": {"mode": "shadow", "room_cooldown_min": 1}}, row, now=at)
    if interactive:
        assert _dispatch(store, row, now=at)["dispatched"]
        _deliver(store)
    with store.db:
        store.db.execute("UPDATE notify_outbox SET state=?,created_at=?,updated_at=? "
                         "WHERE event_id=?", (state, at - 1, at, event))
    return event


def test_high_initial_delivery_is_not_a_late_high_transition(world):
    store, cfg, clock, _ = world
    _fact(store, at=NOW - 60)
    _base(store, at=NOW - 30, interactive=True)
    _fact(store, at=NOW - 1)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    clock[0] = NOW - 30 + 1800
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert json.loads(_events(store)[0]["payload"])["stage"] == "E2:1"


def test_missing_initial_observation_never_invents_ordinary_delivery(world):
    store, cfg, _, _ = world
    _base(store, interactive=True)
    with store.db:
        store.db.execute("DELETE FROM artifacts WHERE kind='urgency_initial_v1'")
    _fact(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert json.loads(_events(store)[0]["payload"])["stage"] == "E2:1"


def test_interactive_seal_records_only_covered_initial_messages_and_allows_late_high(world):
    store, cfg, clock, _ = world
    _patient(store, 2)
    _msg(store, 101, pid=2, body="synthetic other scope")
    event = _intent(store, payload={"message_ids": [100, 101, 999]})
    assert _dispatch(store, event, cfg=cfg, now=NOW)["dispatched"]
    captured = store.db.execute(
        "SELECT message_id,content FROM artifacts WHERE kind='urgency_initial_v1'").fetchall()
    assert [row["message_id"] for row in captured] == [100]
    assert json.loads(captured[0]["content"])["urgency_source"] is None
    _deliver(store)
    state = store.db.execute("SELECT state FROM notify_outbox WHERE event_id=?",
                             (event["event_id"],)).fetchone()[0]
    assert state == "accepted"
    clock[0] = NOW + 1
    _fact(store, at=clock[0])
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert json.loads(_events(store)[0]["payload"])["stage"] == "E1"
    assert _dispatch(store, event, cfg=cfg, now=clock[0]).get("skipped") is True
    assert store.db.execute(
        "SELECT count(*) FROM artifacts WHERE kind='urgency_initial_v1'").fetchone()[0] == 1


def _fact(store, *, mid=100, urgency="high", at=NOW - 600, kind="extract_llm", **meta):
    if kind in ("canonical_projection", "semantic_facts_v4"):
        content_hash = store.db.execute("SELECT content_hash FROM messages WHERE message_id=?",
                                        (mid,)).fetchone()[0]
        store.artifact_add(kind, json.dumps({"urgency": urgency}), project_id=1, message_id=mid,
                           meta={"hash": content_hash, "engine_version": 4, **meta})
    else:
        _extract(store, mid, {"urgency": urgency}, kind=kind, **meta)
    aid = store.db.execute("SELECT max(artifact_id) FROM artifacts").fetchone()[0]
    with store.db:
        store.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (at, aid))
    return aid


def _events(store):
    return store.db.execute("SELECT * FROM notify_outbox WHERE kind='urgent_notice' "
                            "ORDER BY event_id").fetchall()


@pytest.mark.parametrize("policy", [
    {}, {"mode": "off"}, {"mode": "on"}, {"mode": "on", "room_cooldown_min": 0},
    {"mode": "on", "room_cooldown_min": True},
    {"mode": "on", "room_cooldown_min": 1, "source": "rule"},
])
def test_unapproved_or_incomplete_policy_never_enqueues(world, policy):
    store, cfg, _, calls = world
    cfg["urgency_escalation"] = policy
    _base(store)
    _fact(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    assert not _events(store) and calls == []


def test_e1_is_durable_and_independent_of_signals_switch(world):
    store, cfg, _, calls = world
    cfg["signals"] = {"notify": False}
    _base(store)
    _fact(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    event = _events(store)[0]
    assert json.loads(event["payload"])["stage"] == "E1"
    assert event["route"] == "text"
    second = Ledger(store.db.execute("PRAGMA database_list").fetchone()["file"])
    try:
        assert notify_urgent.maybe_enqueue(second, cfg)["queued"] == 0
    finally:
        second.close()
    result = notify_flush.flush(store)
    assert result["sent"] == 1 and len(calls) == 1
    assert _events(store)[0]["state"] == "accepted"
    assert "PRIVATE-BODY-CANARY" not in calls[0][0][1]
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0


def test_e2_exact_deadline_repeat_interval_and_cap(world):
    store, cfg, clock, _ = world
    _base(store, interactive=True)
    _fact(store, at=NOW - 1801)  # already high when first delivery completed
    clock[0] = NOW - .125
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    clock[0] = NOW
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert json.loads(_events(store)[0]["payload"])["stage"] == "E2:1"
    assert notify_flush.flush(store)["sent"] == 1
    clock[0] = NOW + 3600 - .125
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    clock[0] = NOW + 3600
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert json.loads(_events(store)[1]["payload"])["stage"] == "E2:2"
    assert notify_flush.flush(store)["sent"] == 1
    clock[0] += 3600
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    assert len(_events(store)) == 2


@pytest.mark.parametrize(("kind", "level", "meta", "rule", "expected"), [
    ("extract_llm", "high", {}, None, 1),
    ("extract_llm", "high", {}, "routine", 1),
    ("extract_llm", "routine", {}, "high", 0),
    ("extract_llm", "high", {"stale": True}, "high", 0),
    ("canonical_projection", "high", {}, "high", 1),
    ("canonical_projection", "high", {"invalidated": True}, "high", 0),
    ("canonical_projection", "high", {"hash": "0" * 64}, "high", 0),
    ("semantic_facts_v4", "high", {}, "high", 1),
    ("semantic_facts_v4", "high", {"invalidated": True}, "high", 0),
    ("semantic_facts_v4", "high", {"engine_version": 3}, "high", 0),
])
def test_only_current_llm_generation_can_escalate(world, kind, level, meta, rule, expected):
    store, cfg, _, _ = world
    _base(store)
    _fact(store, kind=kind, urgency=level, **meta)
    if rule:
        _fact(store, kind="extract_v1", urgency=rule)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == expected


@pytest.mark.parametrize("state", ["pending", "failed", "suppressed"])
def test_initial_delivery_must_be_proven(world, state):
    store, cfg, _, _ = world
    _base(store, state=state)
    _fact(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    assert not _events(store)


@pytest.mark.parametrize("cancel", [
    "routine", "expired", "hash", "deleted", "archived", "request", "own_post", "ack", "off",
])
def test_queued_followup_is_cancelled_before_real_send(world, cancel):
    store, cfg, _, calls = world
    _base(store)
    aid = _fact(store, kind="canonical_projection")
    if cancel == "ack":
        event = _intent(store, payload={"message_ids": [100]})
        _dispatch(store, event)
        _deliver(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    if cancel == "routine":
        _fact(store, kind="canonical_projection", urgency="routine", at=NOW - 1)
        _fact(store, kind="extract_v1", urgency="high")
    elif cancel == "expired":
        with store.db:
            store.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.invalidated',1) "
                             "WHERE artifact_id=?", (aid,))
        _fact(store, kind="extract_v1", urgency="high")
    elif cancel == "hash":
        with store.db:
            store.db.execute("UPDATE messages SET content_hash=? WHERE message_id=100",
                             ("0" * 64,))
    elif cancel == "deleted":
        with store.db:
            store.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
    elif cancel == "archived":
        with store.db:
            store.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    elif cancel == "request":
        _add_request(store)
    elif cancel == "own_post":
        _msg(store, 101, ts=int(NOW - 1))
        with store.db:
            store.db.execute("UPDATE messages SET sender_id=7 WHERE message_id=101")
    elif cancel == "ack":
        assert _click(store, _spec(store), "ack")["outcome"] == "applied"
    else:
        cfg["urgency_escalation"]["mode"] = "off"
    result = notify_flush.flush(store)
    assert result["suppressed"] == 1 and calls == []
    assert _events(store)[0]["state"] == "suppressed"


def test_rechecks_after_formatting_and_does_not_infer_completion(world, monkeypatch):
    store, cfg, _, calls = world
    _base(store)
    _fact(store)
    notify_urgent.maybe_enqueue(store, cfg)
    original = notify_flush._format_event

    def format_then_cancel(*args):
        value = original(*args)
        _add_request(store)
        return value

    monkeypatch.setattr(notify_flush, "_format_event", format_then_cancel)
    assert notify_flush.flush(store)["suppressed"] == 1
    assert calls == []
    assert store.db.execute("SELECT status FROM requests").fetchone()[0] == "open"


def test_shadow_does_not_send_or_consume_live_stage(world):
    store, cfg, _, calls = world
    _base(store)
    _fact(store)
    cfg["urgency_escalation"]["mode"] = "shadow"
    assert notify_urgent.maybe_enqueue(store, cfg)["suppressed"] == 1
    assert notify_urgent.maybe_enqueue(store, cfg)["suppressed"] == 0
    assert notify_flush.flush(store)["sent"] == 0 and calls == []
    cfg["urgency_escalation"]["mode"] = "on"
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert notify_flush.flush(store)["sent"] == 1
    assert len(_events(store)) == 2


def test_unknown_delivery_is_held_before_stale_cancellation_and_blocks_repeats(world, monkeypatch):
    store, cfg, clock, calls = world
    _base(store)
    _fact(store)
    notify_urgent.maybe_enqueue(store, cfg)

    def uncertain(*args, **kwargs):
        calls.append((args, kwargs))
        raise notify_flush._SendUncertain("synthetic_unknown")

    monkeypatch.setattr(notify_flush, "_send", uncertain)
    assert notify_flush.flush(store)["uncertain"] == 1
    event = _events(store)[0]
    assert event["state"] == "failed" and event["next_try"] is None
    assert json.loads(event["progress"])["hold_reason"] == "send_outcome_unknown"
    cfg["urgency_escalation"]["mode"] = "off"
    with store.db:
        store.db.execute("UPDATE notify_outbox SET next_try=? WHERE event_id=?", (NOW, event["event_id"]))
    assert notify_flush.flush(store)["uncertain"] == 1
    assert _events(store)[0]["state"] == "failed" and _events(store)[0]["next_try"] is None
    assert len(calls) == 1
    cfg["urgency_escalation"]["mode"] = "on"
    clock[0] += 7200
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    assert len(_events(store)) == 1


def test_minimum_budget_and_restore_keep_pending_without_send(world, tmp_path):
    store, cfg, clock, calls = world
    _base(store)
    _fact(store)
    notify_urgent.maybe_enqueue(store, cfg)
    result = notify_flush.flush(store, deadline=NOW + notify_flush.SEND_MIN_BUDGET_S - .125)
    assert result["send_budget_insufficient"] == 1 and calls == []
    assert _events(store)[0]["attempts"] == 0 and _events(store)[0]["progress"] is None
    marker = tmp_path / "data" / "restore_pending.json"
    marker.write_text("{}", encoding="utf-8")
    assert notify_flush.flush(store)["restore_pending"] == 1
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    assert calls == [] and _events(store)[0]["state"] == "pending"
    marker.unlink()
    clock[0] += notify_flush.RERENDER_RETRY_S  # restore gate deferred it
    assert notify_flush.flush(store)["sent"] == 1


def test_room_cooldown_and_daily_cap_are_durable(world):
    store, cfg, _, _ = world
    cfg["urgency_escalation"]["max_per_day"] = 1
    _base(store)
    _fact(store)
    _msg(store, 101, ts=int(NOW - 3600))
    _base(store, mid=101)
    _fact(store, mid=101)
    result = notify_urgent.maybe_enqueue(store, cfg)
    assert result["queued"] == 1 and result["deferred"]["daily_cap"] == 1
    cfg["urgency_escalation"]["max_per_day"] = 10
    result = notify_urgent.maybe_enqueue(store, cfg)
    assert result["queued"] == 0 and result["deferred"]["room_cooldown"] == 1


def test_tick_reads_history_and_restore_marker_once(world, monkeypatch, tmp_path):
    store, cfg, _, _ = world
    _base(store)
    _fact(store)
    _msg(store, 101, ts=int(NOW - 3600))
    _base(store, mid=101)
    _fact(store, mid=101)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1  # 101 is room_cooldown
    loads, marker_reads = [], []
    real_history, real_restore = notify_urgent._history, notify_urgent.notify_cards.restore_pending
    monkeypatch.setattr(notify_urgent, "_history",
                        lambda *a: loads.append(1) or real_history(*a))
    monkeypatch.setattr(notify_urgent.notify_cards, "restore_pending",
                        lambda root: marker_reads.append(1) or real_restore(root))
    result = notify_urgent.maybe_enqueue(store, cfg)
    assert result["queued"] == 0 and sum(result["deferred"].values()) == 2
    assert loads == [1] and marker_reads == [1]
    (tmp_path / "data" / "restore_pending.json").write_text("{}", encoding="utf-8")
    assert notify_urgent.maybe_enqueue(store, cfg)["deferred"] == {"restore_pending": 2}


@pytest.mark.parametrize(("llm", "frozen_urgent"), [("high", False), ("routine", True)])
def test_signal_text_warning_uses_current_urgency_not_frozen_modifier(world, llm, frozen_urgent):
    store, _, _, _ = world
    _fact(store, urgency=llm)
    _fact(store, kind="extract_v1", urgency="high")
    _signal_row(store, "s", mids=[100])
    event = _intent(store, "signal", payload={"signal_keys": ["s"], "urgent": frozen_urgent})
    text, files = notify_flush._format_event(store, event)
    assert ("緊急度: 高（AI抽出）" in text) == (llm == "high")
    assert "原投稿が urgency:high" not in text and files == []


def test_real_initial_sender_captures_ordinary_then_late_high_e1(world):
    store, cfg, clock, calls = world
    with store.db:
        base = store.outbox_add_tx("new_messages", 1, {"message_ids": [100]}, route="text")
    assert notify_flush.flush(store)["sent"] == 1
    initial = notify_urgent._initial(store.db, base, 100)
    assert initial["urgency_source"] is None and initial["observed_at"] == NOW
    clock[0] += 600.125
    _fact(store, at=clock[0] - .125)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    event = _events(store)[0]
    assert json.loads(event["payload"])["stage"] == "E1"
    checked = notify_urgent.check_delivery(store, cfg, event)
    assert checked["observed_at"] == NOW + 600
    assert notify_flush.flush(store)["sent"] == 1
    assert len(calls) == 2 and "PRIVATE-BODY-CANARY" not in calls[1][0][1]
    clock[0] += 7200
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 0
    assert len(_events(store)) == 1  # Text permits E1, never unconfirmable E2.


def test_text_initial_delivery_never_creates_e2_without_confirmation_route(world):
    store, cfg, clock, calls = world
    _fact(store, at=NOW - 3600)
    _base(store)
    clock[0] += 7200
    result = notify_urgent.maybe_enqueue(store, cfg)
    assert result["queued"] == 0
    assert result["deferred"] == {"text_confirmation_unavailable": 1}
    assert not _events(store) and not calls


def test_preexisting_text_e2_is_cancelled_at_send_time(world):
    store, _, _, calls = world
    aid = _fact(store, at=NOW - 3600)
    base = _base(store)
    with store.db:
        store.outbox_add_tx("urgent_notice", 1, {
            "message_id": 100, "hash": f"{100:064x}", "stage": "E2:1",
            "base_event_id": base, "urgency_artifact_id": aid, "shadow": False,
        }, route="text")
    assert notify_flush.flush(store)["suppressed"] == 1
    assert not calls


def test_text_initial_render_and_urgency_witness_share_writer_snapshot(world, monkeypatch):
    store, cfg, clock, calls = world
    aid = _fact(store, urgency="routine", at=NOW - 1)
    with store.db:
        base = store.outbox_add_tx("new_messages", 1, {"message_ids": [100]}, route="text")
    other = Ledger(store.db.execute("PRAGMA database_list").fetchone()["file"])
    other.db.execute("PRAGMA busy_timeout=0")
    original = notify_flush._format_event
    blocked = []

    def concurrent_high(*args):
        rendered = original(*args)
        try:
            with other.db:
                other.db.execute("UPDATE artifacts SET content=?,created_at=? WHERE artifact_id=?",
                                 (json.dumps({"urgency": "high"}), NOW + .1, aid))
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc)
            blocked.append(True)
        return rendered

    monkeypatch.setattr(notify_flush, "_format_event", concurrent_high)
    try:
        assert notify_flush.flush(store)["sent"] == 1
        assert blocked == [True]
        assert "緊急度: 高（AI抽出）" not in calls[0][0][1]
        assert notify_urgent._initial(store.db, base, 100)["urgency_source"] is None
        with other.db:
            other.db.execute("UPDATE artifacts SET content=?,created_at=? WHERE artifact_id=?",
                             (json.dumps({"urgency": "high"}), NOW + .1, aid))
        clock[0] += 1
        assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
        assert json.loads(_events(store)[0]["payload"])["stage"] == "E1"
    finally:
        other.close()


def test_page_ack_does_not_confirm_hidden_urgent_message(world):
    store, cfg, _, calls = world
    _base(store)
    _fact(store)
    mids = list(range(100, 112))
    for mid in mids[1:]:
        _msg(store, mid, parent=100, ts=int(NOW - 3600 + mid))
    _dispatch(store, _intent(store, payload={"message_ids": mids}))
    _deliver(store)
    assert _spec(store)["parts"]["pages"] == 2
    assert _click(store, _spec(store), "ack")["outcome"] == "applied"
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    _deliver(store)
    assert _click(store, _spec(store), "prev")["outcome"] == "applied"
    _deliver(store)
    assert _click(store, _spec(store), "ack")["outcome"] == "applied"
    assert notify_flush.flush(store)["suppressed"] == 1 and calls == []


def test_old_confirmation_is_not_current_after_llm_generation_arrives(world):
    store, cfg, _, _ = world
    _base(store)
    _dispatch(store, _intent(store, payload={"message_ids": [100]}))
    _deliver(store)
    assert _click(store, _spec(store), "ack")["outcome"] == "applied"
    _fact(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert json.loads(_events(store)[0]["payload"])["stage"] == "E1"


def test_unknown_self_identity_and_future_self_posts_are_not_negative_proof(world):
    store, cfg, _, _ = world
    _base(store)
    _fact(store)
    with store.db:
        store.db.execute("DELETE FROM artifacts WHERE kind='station_staff_v1'")
    result = notify_urgent.maybe_enqueue(store, cfg)
    assert result["queued"] == 0 and result["deferred"]["identity_unknown"] == 1
    with store.db:
        record_station_staff(store.db, [{"staff_id": 7, "is_self": True}])
    _msg(store, 101, ts=int(NOW + 1))
    with store.db:
        store.db.execute("UPDATE messages SET sender_id=7 WHERE message_id=101")
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1


def test_uncertain_attempt_after_day_change_reserves_current_daily_budget(world, monkeypatch):
    store, cfg, clock, _ = world
    cfg["urgency_escalation"]["max_per_day"] = 1
    _base(store)
    _fact(store)
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    clock[0] += 86400

    def uncertain(*args, **kwargs):
        raise notify_flush._SendUncertain("synthetic_unknown")

    monkeypatch.setattr(notify_flush, "_send", uncertain)
    assert notify_flush.flush(store)["uncertain"] == 1
    _msg(store, 101, ts=int(NOW - 3600))
    _base(store, mid=101)
    _fact(store, mid=101, at=clock[0] - 1)
    result = notify_urgent.maybe_enqueue(store, cfg)
    assert result["queued"] == 0 and result["deferred"]["daily_cap"] == 1


def test_partial_interactive_acceptance_is_not_delivery_of_suppressed_member(world):
    store, cfg, clock, _ = world
    _msg(store, 101, ts=int(NOW - 3000))
    clock[0] = NOW - 2000
    event = _intent(store, payload={"message_ids": [100, 101]})
    _dispatch(store, event)
    clock[0] = NOW - 1800
    # Synthetic worker receipts: one root was delivered; the other was suppressed.
    with store.db:
        store.db.execute("UPDATE notification_intent_cards SET state="
                         "CASE WHEN EXISTS(SELECT 1 FROM json_each(coverage) "
                         "WHERE value=100) THEN 'suppressed' ELSE 'delivered' END "
                         "WHERE event_id=?", (event["event_id"],))
        store.db.execute("UPDATE notify_outbox SET state='accepted',updated_at=? "
                         "WHERE event_id=?", (clock[0], event["event_id"]))
    clock[0] = NOW
    _fact(store)
    result = notify_urgent.maybe_enqueue(store, cfg)
    assert result["queued"] == 0
    assert result["deferred"]["initial_delivery_unproven"] == 1


def test_sealed_interactive_delivery_is_proven_for_e2(world):
    store, cfg, clock, _ = world
    _fact(store)
    _dispatch(store, _intent(store, payload={"message_ids": [100]}))
    _deliver(store)
    clock[0] += 1800
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    assert json.loads(_events(store)[0]["payload"])["stage"] == "E2:1"


@pytest.mark.parametrize(("state", "defer_delta", "claimed"), [
    ("assigned", None, True),
    ("deferred", 7200, True),
    ("deferred", -1, False),
    ("open", None, False),
])
def test_triage_assigned_or_live_deferral_stops_e2_and_queued_send(
        world, state, defer_delta, claimed):
    store, cfg, clock, calls = world
    _fact(store)
    _dispatch(store, _intent(store, payload={"message_ids": [100]}))
    _deliver(store)
    clock[0] += 1800
    assert notify_urgent.maybe_enqueue(store, cfg)["queued"] == 1
    until = None if defer_delta is None else clock[0] + defer_delta
    with store.db:
        store.db.execute(
            "INSERT INTO notification_triage(card_id,owner,defer_until,state,updated_at) "
            "SELECT card_id,'synthetic',?,?,? FROM notification_cards WHERE kind='thread'",
            (until, state, clock[0]))
    result = notify_flush.flush(store)
    if claimed:
        assert result["suppressed"] == 1 and calls == []
        clock[0] += 3600
        result = notify_urgent.maybe_enqueue(store, cfg)
        assert result["queued"] == 0 and result["deferred"]["triage_claimed"] == 1
    else:
        assert result["sent"] == 1 and len(calls) == 1
