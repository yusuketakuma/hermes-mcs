"""Synthetic checks for reading only the latest lifecycle content per key."""
import json
import sqlite3

import mcs_signals


def test_history_validates_once_per_key_and_preserves_latest_tombstones(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,"
               "project_id INTEGER,content TEXT,meta TEXT)")
    valid = json.dumps({"project_id": 1, "state": "open", "type": "synthetic",
                        "detected_at": 1, "evidence": {}})
    rows = [("signal_v1", 1, valid, json.dumps({"key": f"synthetic-{i % 40}"}))
            for i in range(800)]
    rows += [
        ("signal_v1", 1, "{broken", '{"key":"synthetic-0"}'),
        ("signal_v1", 1, valid, "{broken"),
        ("signal_v1", 1, valid, '[]'),
        ("signal_v1", 1, valid, '{"key":3}'),
        ("signal_v1", 2, valid, '{"key":"synthetic-1"}'),
    ]
    db.executemany("INSERT INTO artifacts(kind,project_id,content,meta) VALUES(?,?,?,?)",
                   rows)
    original = mcs_signals._signal_content
    # Reference the old append-only fold, including unreadable latest rows.
    expected = {}
    for _, pid, content, meta in rows:
        try:
            meta = json.loads(meta)
        except ValueError:
            continue
        key = meta.get("key") if isinstance(meta, dict) else None
        if isinstance(key, str) and key:
            expected[key] = original(content, pid)
    calls = []

    def counted(content, pid):
        calls.append(1)
        return original(content, pid)

    monkeypatch.setattr(mcs_signals, "_signal_content", counted)
    try:
        actual = mcs_signals._latest_signal_states(db)
    finally:
        db.close()
    assert actual == expected
    assert list(actual) == list(expected)
    assert actual["synthetic-0"] is None
    assert actual["synthetic-1"] is None
    assert len(calls) == 40
