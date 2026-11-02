"""Bounded dispatch groups keep durable publication and per-intent safety."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import notify_cards as cards
import notify_flush
import notify_spool as spool
from notify_testkit import CFG, NOW, _begin, _latest_render, _msg, _patient, _receipt, led

__all__ = ["led"]


def _events(ledger, n=3):
    events = []
    for i in range(n):
        _patient(ledger, i + 1)
        _msg(ledger, 100 + i, pid=i + 1)
        eid = ledger.outbox_add("new_messages", i + 1, {"message_ids": [100 + i]})
        events.append(dict(ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (eid,)).fetchone()))
    return events


def _flush_config(monkeypatch):
    monkeypatch.setattr(notify_flush, "_config", lambda: CFG)
    monkeypatch.setattr(notify_flush.mcs_util, "hermes_exe", lambda cfg: "/synthetic/missing-hermes")


def test_group_commits_all_renders_before_publication_and_marks_once(led, monkeypatch):
    events = _events(led, 10)
    sql, publications = [], []
    publish = spool.publish_file
    led.db.set_trace_callback(sql.append)

    def observe(directory, name, raw):
        assert not led.db.in_transaction
        assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 10
        publications.append(name)
        return publish(directory, name, raw)

    monkeypatch.setattr(spool, "publish_file", observe)
    outcomes = cards.dispatch_intents(led, events, CFG, now=NOW)
    led.db.set_trace_callback(None)
    assert len(outcomes) == len(publications) == 10
    assert all(outcome["dispatched"] and outcome["cards"] == 1 for outcome in outcomes)
    assert sum(statement == "COMMIT" for statement in sql) == 2
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE spec_published=1 "
                          "AND first_published_at=?", (NOW,)).fetchone()[0] == 10


def test_failed_item_rolls_back_its_spec_and_keeps_other_items(led, monkeypatch, capsys):
    events = _events(led)
    original, rolled_back = cards._dispatch_intent_tx, []

    def dispatch(ledger, event, cfg, now, specs):
        outcome = original(ledger, event, cfg, now, specs)
        if event["event_id"] == events[1]["event_id"]:
            rolled_back.extend(spec["delivery_id"] for spec in specs[-1:])
            raise RuntimeError("synthetic-private-error-canary")
        return outcome

    monkeypatch.setattr(cards, "_dispatch_intent_tx", dispatch)
    _flush_config(monkeypatch)
    result = notify_flush.flush(led)
    assert result["dispatched"] == 2 and result["failed"] == 1
    assert led.db.execute("SELECT COUNT(*) FROM notification_cards").fetchone()[0] == 2
    assert not led.db.execute("SELECT 1 FROM notification_intent_batches WHERE event_id=?",
                              (events[1]["event_id"],)).fetchone()
    state = led.db.execute("SELECT state,attempts FROM notify_outbox WHERE event_id=?",
                           (events[1]["event_id"],)).fetchone()
    assert tuple(state) == ("failed", 1)
    render_dir = Path(spool.notify_dirs(spool.data_root(led))["discord_render"])
    assert len(list(render_dir.glob("*.json"))) == 2
    assert not any((render_dir / (did + ".json")).exists() for did in rolled_back)
    output = capsys.readouterr()
    assert "private-error-canary" not in output.out + output.err


class _FailCommit:
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.db.rollback()
            raise sqlite3.OperationalError("synthetic commit failure")
        return self.db.__exit__(kind, value, traceback)


def test_group_commit_failure_publishes_nothing_and_rolls_back_all_rows(led, monkeypatch):
    events = _events(led)
    db = led.db
    monkeypatch.setattr(spool, "publish_file", lambda *a: pytest.fail("uncommitted render published"))
    led.db = _FailCommit(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
            cards.dispatch_intents(led, events, CFG, now=NOW)
    finally:
        led.db = db
    assert not db.in_transaction
    assert db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 3
    for table in ("notification_cards", "notification_intent_batches", "notification_renders"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_failed_publication_records_only_durable_prefix_and_recovers(led, monkeypatch):
    events = _events(led)
    original, calls = spool.publish_file, []

    def fail_second(directory, name, raw):
        assert not led.db.in_transaction
        calls.append(name)
        if len(calls) == 2:
            raise OSError("synthetic publication failure")
        return original(directory, name, raw)

    monkeypatch.setattr(spool, "publish_file", fail_second)
    with pytest.raises(OSError, match="synthetic publication failure"):
        cards.dispatch_intents(led, events, CFG, now=NOW)
    rows = led.db.execute("SELECT delivery_id,spec_published,first_published_at FROM notification_renders "
                          "ORDER BY card_id").fetchall()
    assert [(row[1], row[2]) for row in rows] == [(1, NOW), (0, None), (0, None)]
    assert not led.db.in_transaction
    monkeypatch.setattr(spool, "publish_file", original)
    assert spool.recover(led, CFG, {})["republished"] == 2
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE spec_published=1").fetchone()[0] == 3
    assert led.db.execute("SELECT first_published_at FROM notification_renders WHERE delivery_id=?",
                          (rows[0][0],)).fetchone()[0] == NOW


def test_publication_mark_commit_failure_keeps_files_recoverable(led):
    events = _events(led)
    cards.dispatch_intents(led, events, CFG, now=NOW)
    rows = led.db.execute("SELECT spec_json FROM notification_renders ORDER BY card_id").fetchall()
    specs = [json.loads(row[0]) for row in rows]
    with led.db:
        led.db.execute("UPDATE notification_renders SET spec_published=0,first_published_at=NULL")
    directories = spool.notify_dirs(spool.data_root(led))
    with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
        spool.publish_specs(_FailCommit(led.db), directories, specs, NOW + 1)
    assert not led.db.in_transaction
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE spec_published=0 "
                          "AND first_published_at IS NULL").fetchone()[0] == 3
    assert len(list(Path(directories["discord_render"]).glob("*.json"))) == 3
    spool.recover(led, CFG, {})
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE spec_published=1").fetchone()[0] == 3


@pytest.mark.parametrize("entry", ["single", "group", "publication"])
def test_transaction_owners_preserve_borrowed_transaction(led, monkeypatch, entry):
    [event] = _events(led, 1)
    led.db.execute("UPDATE patients SET patient_name='uncommitted synthetic' WHERE project_id=1")
    monkeypatch.setattr(spool, "publish_file", lambda *a: pytest.fail("borrowed transaction published"))
    try:
        with pytest.raises(ValueError, match="transaction_active"):
            if entry == "single":
                cards.dispatch_intent(led, event, CFG, now=NOW)
            elif entry == "group":
                cards.dispatch_intents(led, [event], CFG, now=NOW)
            else:
                spool.publish_specs(led.db, {}, [{}], NOW)
        assert led.db.in_transaction
        assert led.db.execute("SELECT patient_name FROM patients WHERE project_id=1").fetchone()[0] == "uncommitted synthetic"
        assert not led.db.execute("SELECT 1 FROM notification_renders").fetchone()
    finally:
        led.db.rollback()


def test_transaction_body_can_borrow_without_publishing(led, monkeypatch):
    [event] = _events(led, 1)
    specs = []
    monkeypatch.setattr(spool, "publish_file", lambda *a: pytest.fail("transaction body published"))
    with led.db:
        led.db.execute("BEGIN IMMEDIATE")
        assert cards._dispatch_intent_tx(led, event, CFG, NOW, specs)["dispatched"]
        assert led.db.in_transaction
    assert len(specs) == 1 and not led.db.in_transaction
    assert led.db.execute("SELECT spec_published FROM notification_renders").fetchone()[0] == 0


def test_group_rechecks_restore_marker_before_each_item(led, monkeypatch):
    events = _events(led)
    original = cards._dispatch_intent_tx
    root = Path(spool.data_root(led))

    def dispatch(ledger, event, cfg, now, specs):
        result = original(ledger, event, cfg, now, specs)
        (root / spool.RESTORE_MARKER).write_text('{"phase":"restored","by":"mcs_update"}')
        return result

    monkeypatch.setattr(cards, "_dispatch_intent_tx", dispatch)
    _flush_config(monkeypatch)
    result = notify_flush.flush(led)
    assert result["dispatched"] == 1 and result["restore_pending"] == result["skipped"] == 2
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 1
    for event in events[1:]:
        row = led.db.execute("SELECT state,attempts,next_try FROM notify_outbox WHERE event_id=?",
                             (event["event_id"],)).fetchone()
        assert row["state"] == "pending" and row["attempts"] == 0 and row["next_try"] > event["next_try"]
    assert (root / spool.RESTORE_MARKER).exists()


@pytest.mark.parametrize("progress, counter", [(None, "suppressed"), ('{"sending":1}', "uncertain"), ("{", "failed")])
def test_group_rechecks_archive_and_keeps_unknown_progress_held(led, monkeypatch, progress, counter):
    events = _events(led)
    with led.db:
        led.db.execute("UPDATE notify_outbox SET progress=? WHERE event_id=?", (progress, events[1]["event_id"]))
    original = cards._dispatch_intent_tx

    def dispatch(ledger, event, cfg, now, specs):
        result = original(ledger, event, cfg, now, specs)
        ledger.db.execute("UPDATE patients SET is_archived=1 WHERE project_id IN (2,3)")
        return result

    monkeypatch.setattr(cards, "_dispatch_intent_tx", dispatch)
    _flush_config(monkeypatch)
    result = notify_flush.flush(led)
    assert result["dispatched"] == 1 and result[counter] >= 1
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 1
    if progress:
        row = led.db.execute("SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
                             (events[1]["event_id"],)).fetchone()
        assert row["state"] == "failed" and row["next_try"] is None
        expected = "send_outcome_unknown" if counter == "uncertain" else "payload_or_progress_invalid"
        assert json.loads(row["progress"])["hold_reason"] == expected


def test_group_stops_new_items_at_deadline_but_commits_completed_prefix(led, monkeypatch):
    events = _events(led)
    clock = [0.0]
    original = cards._dispatch_intent_tx

    def dispatch(*args):
        outcome = original(*args)
        clock[0] = 11.0
        return outcome

    monkeypatch.setattr(cards, "_dispatch_intent_tx", dispatch)
    monkeypatch.setattr(cards, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    outcomes = cards.dispatch_intents(led, events, CFG, now=NOW, deadline=10.0)
    assert outcomes[0]["dispatched"] and outcomes[1:] == [{"deadline": True}, {"deadline": True}]
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders WHERE spec_published=1").fetchone()[0] == 1
    assert not led.db.in_transaction


def test_group_bounds_are_checked_before_any_write(led, monkeypatch):
    events = _events(led, 11)
    monkeypatch.setattr(spool, "publish_file", lambda *a: pytest.fail("invalid group published"))
    with pytest.raises(ValueError, match="group_invalid"):
        cards.dispatch_intents(led, events, CFG, now=NOW)
    assert not led.db.in_transaction and not led.db.execute("SELECT 1 FROM notification_cards").fetchone()


def test_group_same_card_reentry_does_not_duplicate_renders_or_release_unknown(led):
    [event] = _events(led, 1)
    cards.dispatch_intent(led, event, CFG, now=NOW)
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    assert _receipt(led, render, "0" * 15 + "1", result="unknown", message_id=None)["applied"]
    eid = led.outbox_add("new_messages", 1, {"message_ids": [100]})
    repeated = dict(led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (eid,)).fetchone())
    cards.dispatch_intents(led, [event, repeated], CFG, now=NOW)
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 1
    assert led.db.execute("SELECT delivery_state FROM notification_cards").fetchone()[0] == "delivery_unknown"
    assert led.db.execute("SELECT state FROM notification_delivery_attempts").fetchone()[0] == "unknown"


def test_flush_places_urgent_notice_between_ordinary_groups(led, monkeypatch):
    events = _events(led, 2)
    urgent = led.outbox_add("urgent_notice", 1, {"message_id": 100})
    rows = [events[0], dict(led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (urgent,)).fetchone()), events[1]]
    monkeypatch.setattr(led, "outbox_due", lambda *a, **kw: rows)
    _flush_config(monkeypatch)
    order = []

    def dispatch_group(ledger, pending, cfg, **kwargs):
        order.extend(event["event_id"] for event in pending)
        return [{"dispatched": True} for _ in pending]

    def dispatch_single(ledger, event, cfg, res):
        order.append(event["event_id"])
        return True

    monkeypatch.setattr(cards, "dispatch_intents", dispatch_group)
    monkeypatch.setattr(notify_flush, "_dispatch_interactive", dispatch_single)
    notify_flush.flush(led)
    assert order == [event["event_id"] for event in rows]


def test_flush_does_not_start_urgent_notice_after_group_uses_deadline(led, monkeypatch):
    [event] = _events(led, 1)
    urgent_id = led.outbox_add("urgent_notice", 1, {"message_id": 100})
    urgent = dict(led.db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (urgent_id,)).fetchone())
    monkeypatch.setattr(led, "outbox_due", lambda *a, **kw: [event, urgent])
    _flush_config(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(notify_flush, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: NOW))

    def dispatch_group(*args, **kwargs):
        clock[0] = 11.0
        return [{"dispatched": True}]

    monkeypatch.setattr(cards, "dispatch_intents", dispatch_group)
    monkeypatch.setattr(notify_flush, "_dispatch_interactive",
                        lambda *a: pytest.fail("deadline exhausted before urgent notice"))
    result = notify_flush.flush(led, deadline=10.0)
    assert result["dispatched"] == result["skipped"] == 1


def test_resealed_item_does_not_count_a_previous_items_new_spec(led, monkeypatch):
    fresh, existing = _events(led, 2)
    assert cards.dispatch_intent(led, existing, CFG, now=NOW)["dispatched"]
    saved = _latest_render(led)
    published = []
    original = spool.publish_file

    def observe(directory, name, raw):
        published.append(name)
        return original(directory, name, raw)

    monkeypatch.setattr(spool, "publish_file", observe)
    outcomes = cards.dispatch_intents(led, [fresh, existing], CFG, now=NOW)
    assert outcomes[0]["dispatched"] is True
    assert outcomes[1]["dispatched"] is False and outcomes[1]["resealed"] is True
    assert len(published) == 1
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 2
    current = led.db.execute("SELECT * FROM notification_renders WHERE card_id=?", (saved["card_id"],)).fetchone()
    assert dict(current) == dict(saved)
