"""Regression: the thread delivery plan orders root-first like the card
face, so a reply with a NULL posted_at_ts never precedes the root."""
from __future__ import annotations

import sqlite3

import notify_cards


def test_thread_plan_puts_root_before_null_timestamp_reply(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE messages (message_id INTEGER, parent_id "
               "INTEGER, project_id INTEGER, posted_at_ts INTEGER)")
    db.executemany("INSERT INTO messages VALUES (?,?,?,?)",
                   [(100, None, 1, 1000), (101, 100, 1, None),
                    (102, 100, 1, 2000)])
    monkeypatch.setattr(notify_cards, "_announced_ids",
                        lambda db, card: {100, 101, 102})
    monkeypatch.setattr(notify_cards, "_delivered_members",
                        lambda db, card, legacy: set())
    card = {"kind": "thread", "root_message_id": 100, "project_id": 1}
    assert notify_cards._thread_plan_ids(db, card, [102]) == [100, 101, 102]
