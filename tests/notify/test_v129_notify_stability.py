"""v1.0.29 notify stability: gc/recover keep filesystem work out of the
write transaction, deterministic storage errors quarantine one command,
and the snapshot dirty flag survives a mark made during a publish.
Synthetic fixtures + a temp DB only."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import notify_cards
import notify_cmds
import notify_spool
from notify_testkit import CFG, NOW, _dispatch, _intent, _latest_render, _seed_thread
from notify_testkit import led, pinned_clock

__all__ = ["led", "pinned_clock"]  # shared isolated-ledger fixture

pytestmark = pytest.mark.usefixtures("pinned_clock")


def test_gc_scans_cmd_results_outside_the_write_transaction(led, monkeypatch):
    root = notify_spool.data_root(led)
    notify_spool.ensure_dirs(root)
    results = Path(notify_spool.notify_dirs(root)["cmd_results"])
    old = results / "old.json"
    old.write_text("{}")
    os.utime(old, (NOW - 30 * 86400, NOW - 30 * 86400))
    db = notify_cards.connection(led)
    seen = []
    real = os.scandir

    def spy(path):
        if str(path) == str(results):
            seen.append(db.in_transaction)
        return real(path)

    monkeypatch.setattr(notify_cards.os, "scandir", spy)
    out = notify_cards.gc(led, CFG, now=NOW)
    assert seen == [False]
    assert out["result_files"] == 1 and not old.exists()


def test_recover_publishes_outside_the_transaction_and_skips_moved_rows(led, monkeypatch):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    spec = Path(notify_spool.notify_dirs(notify_spool.data_root(led))["discord_render"]) / (
        render["delivery_id"] + ".json")
    spec.unlink()
    db = notify_cards.connection(led)
    real = notify_spool.publish_file
    seen = []

    def publish(directory, name, raw):
        if name != render["delivery_id"] + ".json":
            return real(directory, name, raw)
        seen.append(db.in_transaction)
        # a concurrent writer cancels the render while the file is written
        db.execute("UPDATE notification_renders SET state='cancelled', updated_at=1 "
                   "WHERE delivery_id=?", (render["delivery_id"],))
        db.commit()
        return real(directory, name, raw)

    monkeypatch.setattr(notify_spool, "publish_file", publish)
    notify_spool.recover(led, CFG, {})
    assert seen == [False]
    row = db.execute("SELECT state, updated_at FROM notification_renders WHERE delivery_id=?",
                     (render["delivery_id"],)).fetchone()
    assert (row["state"], row["updated_at"]) == ("cancelled", 1)


def _two_commands(led, tmp_path):
    root = tmp_path / "data"
    notify_spool.ensure_dirs(str(root))
    for name in ("a.json", "b.json"):
        (root / "cmd_int" / name).write_text(json.dumps(
            {"version": 1, "op": "refresh", "command_id": name}))
    return root


def test_deterministic_storage_error_quarantines_only_that_command(led, tmp_path, monkeypatch):
    root = _two_commands(led, tmp_path)
    monkeypatch.setattr(notify_cmds, "validate_int", lambda req: None)
    monkeypatch.setattr(notify_cards, "sweep", lambda *a, **k: None)

    def dispatch(ledger, req, cfg, root_):
        if req["command_id"] == "a.json":
            raise sqlite3.IntegrityError("synthetic constraint")
        return {"outcome": "applied"}

    monkeypatch.setattr(notify_cmds, "dispatch", dispatch)
    result = {"errors": []}
    assert notify_cmds.drain_int_commands(led, result, CFG, str(root)) == 2
    assert sorted(n for n in os.listdir(root / "cmd_int") if not n.startswith(".")) == ["a.json.invalid"]
    answer = json.loads((root / "cmd_results" / "a.json.json").read_text())
    assert answer["outcome"] == "rejected"
    assert answer["error"] == "storage_rejected:IntegrityError"


def test_busy_storage_still_stops_the_drain_without_consuming(led, tmp_path, monkeypatch):
    root = _two_commands(led, tmp_path)
    monkeypatch.setattr(notify_cmds, "validate_int", lambda req: None)

    def dispatch(*a):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(notify_cmds, "dispatch", dispatch)
    with pytest.raises(sqlite3.OperationalError):
        notify_cmds.drain_int_commands(led, {"errors": []}, CFG, str(root))
    assert sorted(n for n in os.listdir(root / "cmd_int") if not n.startswith(".")) == ["a.json", "b.json"]


def test_dirty_mark_during_publish_survives_the_clear(led):
    db = notify_cards.connection(led)
    with db:
        notify_cards.mark_snapshot_dirty(db)
    generation = notify_cards.snapshot_dirty(led)
    assert generation
    with db:                                   # lands while the copy runs
        notify_cards.mark_snapshot_dirty(db)
    notify_cards.clear_snapshot_dirty(led, generation)
    assert notify_cards.snapshot_dirty(led)
    notify_cards.clear_snapshot_dirty(led, notify_cards.snapshot_dirty(led))
    assert not notify_cards.snapshot_dirty(led)
