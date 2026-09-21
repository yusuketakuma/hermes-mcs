"""Synthetic-DB tests for the prospective signal layer: detector
correctness, open/resolved lifecycle, dedup, notify gating, honest
wording. No live DB, network, or real patient data."""
import json
import sqlite3
import time

import pytest

import mcs_signals

SCHEMA = """
CREATE TABLE messages (message_id INTEGER PRIMARY KEY, project_id INTEGER,
                       parent_id INTEGER, sender_id INTEGER,
                       sender_name TEXT, sender_type TEXT, profession TEXT,
                       organization TEXT, posted_at TEXT, posted_at_ts INTEGER,
                       body_text TEXT, body_state TEXT, content_hash TEXT,
                       reply_count INTEGER DEFAULT 0);
CREATE TABLE artifacts (artifact_id INTEGER PRIMARY KEY, kind TEXT,
                        project_id INTEGER, message_id INTEGER,
                        content TEXT, model TEXT, meta TEXT, created_at REAL);
CREATE TABLE requests (request_id INTEGER PRIMARY KEY, project_id INTEGER,
                       status TEXT, due_date TEXT, updated_at REAL);
CREATE TABLE notify_outbox(
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT,
    project_id INTEGER, payload TEXT, state TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0, next_try REAL, accepted_ref TEXT,
    created_at REAL, updated_at REAL, progress TEXT);
"""

NOW = 1789975073.0   # 2026-09-21 JST
DAY = 86400


class FakeLedger:
    """evaluate() touches only .db and .outbox_add_tx."""
    def __init__(self, db):
        self.db = db
    def outbox_add_tx(self, kind, project_id, payload):
        cur = self.db.execute(
            "INSERT INTO notify_outbox(kind,project_id,payload,state,"
            "next_try,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?)",
            (kind, project_id, json.dumps(payload, ensure_ascii=False),
             time.time(), time.time(), time.time()))
        return cur.lastrowid


@pytest.fixture
def led():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    yield FakeLedger(conn)
    conn.close()


def _msg(db, mid, pid=1, ts=NOW - 30 * DAY, chash="h1"):
    db.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, None, 1, "n", "staff", "看護師", "org",
         "2026-08-22T10:00:00+09:00", ts, "b", "full", chash, 0))


def _extract_llm(db, mid, chash, meds):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps({"meds": meds}), json.dumps({"hash": chash})))


def _extract_v1(db, mid, chash, periods):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',?,?,?)",
        (mid, json.dumps({"med_periods": periods}),
         json.dumps({"hash": chash})))


def _states(db):
    out = {}
    for content, meta in db.execute(
            "SELECT content, meta FROM artifacts WHERE kind='signal_v1'"):
        out[json.loads(meta)["key"]] = json.loads(content)["state"]
    return out


def _ev(lg, cfg=None, now=NOW):
    return mcs_signals.evaluate(lg, cfg or {}, now=now)


# --- detectors ---

def test_request_overdue_detected_and_wording(led):
    led.db.execute("INSERT INTO requests VALUES (1,1,'open','2026-09-10',0)")
    led.db.execute("INSERT INTO requests VALUES (2,1,'done','2026-09-10',0)")
    led.db.execute("INSERT INTO requests VALUES (3,1,'cancelled','2026-09-10',0)")
    led.db.execute("INSERT INTO requests VALUES (4,1,'open','2026-10-01',0)")
    res = _ev(led)
    assert res["open"] == 1
    sig = mcs_signals.current_open(led.db)["items"][0]
    assert sig["type"] == "request_overdue"
    assert "依頼登録" in sig["note"] and "確認" in sig["note"]


def test_med_change_no_followup(led):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    res = _ev(led)
    assert res["open"] == 1
    sig = mcs_signals.current_open(led.db)["items"][0]
    assert sig["type"] == "med_change_no_followup"
    assert "確認できませんでした" in sig["note"]
    assert "対応の有無を示すものではありません" in sig["note"]


def test_med_change_with_followup_not_flagged(led):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 29 * DAY)   # later post inside window
    assert _ev(led)["open"] == 0


def test_med_change_recent_mention_not_yet_candidate(led):
    _msg(led.db, 1, ts=NOW - 2 * DAY)    # window hasn't elapsed
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "start"}])
    assert _ev(led)["open"] == 0


def test_med_none_action_not_flagged(led):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "none"}])
    assert _ev(led)["open"] == 0


def test_comm_concentration(led):
    for i in range(mcs_signals.CONC_MIN_POSTS):
        _msg(led.db, 100 + i, ts=NOW - 3600)
    assert _ev(led)["open"] == 1
    sig = mcs_signals.current_open(led.db)["items"][0]
    assert sig["type"] == "comm_concentration"
    assert "重症度ではありません" in sig["note"]


def test_rx_period_expiry(led):
    _msg(led.db, 1)
    _extract_v1(led.db, 1, "h1",
                [{"start": "2026-09-01", "end": "2026-09-30",
                  "raw": "9/1-9/30"},
                 {"start": "2020-01-01", "end": "2020-02-01",
                  "raw": "old"}])
    res = _ev(led)
    assert res["open"] == 1
    sig = mcs_signals.current_open(led.db)["items"][0]
    assert sig["type"] == "rx_period_expiry"
    assert sig["evidence"]["days_left"] == 9
    assert "確定ではありません" in sig["note"]


# --- lifecycle / dedup ---

def test_signal_resolves_when_condition_clears(led):
    led.db.execute("INSERT INTO requests VALUES (1,1,'open','2026-09-10',0)")
    _ev(led)
    assert _states(led.db)["request_overdue:1:1"] == "open"
    led.db.execute("UPDATE requests SET status='done' WHERE request_id=1")
    _ev(led)
    assert _states(led.db)["request_overdue:1:1"] == "resolved"


def test_no_duplicate_artifacts_on_rerun(led):
    led.db.execute("INSERT INTO requests VALUES (1,1,'open','2026-09-10',0)")
    _ev(led)
    _ev(led)
    _ev(led)
    n = led.db.execute("SELECT COUNT(*) FROM artifacts "
                     "WHERE kind='signal_v1'").fetchone()[0]
    assert n == 1


def test_resolved_signal_reopens(led):
    led.db.execute("INSERT INTO requests VALUES (1,1,'open','2026-09-10',0)")
    _ev(led)
    led.db.execute("UPDATE requests SET status='done' WHERE request_id=1")
    _ev(led)
    led.db.execute("UPDATE requests SET status='open' WHERE request_id=1")
    _ev(led)
    c = json.loads(led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1'").fetchone()[0])
    assert c["state"] == "open" and c.get("reopened_at")


# --- notify gating ---

def test_notify_off_by_default(led):
    led.db.execute("INSERT INTO requests VALUES (1,1,'open','2026-09-10',0)")
    _ev(led, cfg={})
    n = led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    assert n == 0


def test_notify_enqueues_signal_kind_when_enabled(led):
    led.db.execute("INSERT INTO requests VALUES (1,1,'open','2026-09-10',0)")
    res = _ev(led, cfg={"signals": {"notify": True}})
    row = led.db.execute("SELECT kind, payload FROM notify_outbox").fetchone()
    assert res["notify_enqueued"] == 1 and row[0] == "signal"
    pl = json.loads(row[1])
    assert "レビュー候補" in pl["text"] and pl["signal_key"]


def test_notify_only_on_new_open_not_refresh(led):
    led.db.execute("INSERT INTO requests VALUES (1,1,'open','2026-09-10',0)")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    _ev(led, cfg=cfg)   # still open -> refresh, no new intent
    n = led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    assert n == 1
