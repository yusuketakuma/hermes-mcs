"""Tests for the prospective signal layer against a REAL Ledger on
tmp_path (same pattern as test_mcs_ingestion): detector correctness,
open/resolved lifecycle, append-only history, notify gating, honest
wording. No network or live data."""
import json

import pytest

import ledger as ledger_mod
import mcs_signals

NOW = 1789975073.0   # 2026-09-21 JST
DAY = 86400


@pytest.fixture
def led(tmp_path):
    lg = ledger_mod.Ledger(str(tmp_path / "ledger.db"))
    yield lg
    lg.db.close()


def _msg(db, mid, pid=1, ts=NOW - 30 * DAY, chash="h1", body="b"):
    db.execute(
        "INSERT INTO messages(message_id,project_id,parent_id,sender_id,"
        "sender_name,sender_type,profession,organization,posted_at,"
        "posted_at_ts,body_text,body_state,content_hash,reply_count,"
        "is_unread,first_seen,updated_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, None, 1, "n", "staff", "看護師", "org",
         "2026-08-22T10:00:00+09:00", ts, body, "full", chash, 0, 0,
         ts, ts))
    db.execute(
        "INSERT OR IGNORE INTO patients(project_id,is_archived,"
        "created_at,last_seen) VALUES (?,0,?,?)", (pid, ts, ts))


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


def _req(db, status, due=None, src_mid=1, created=NOW):
    db.execute(
        "INSERT INTO requests(project_id,source_message_id,source_hash,"
        "title,status,due_date,revision,created_at,updated_at) "
        "VALUES (1,?,?,?,?,?,1,?,?)",
        (src_mid, "h" * 64, "t", status, due, created, created))


def _states(db):
    """latest state per signal key (append-only model)"""
    out = {}
    for content, meta in db.execute(
            "SELECT content, meta FROM artifacts WHERE kind='signal_v1' "
            "ORDER BY artifact_id"):
        out[json.loads(meta)["key"]] = json.loads(content)["state"]
    return out


def _ev(lg, cfg=None, now=NOW):
    return mcs_signals.evaluate(lg, cfg or {}, now=now)


# --- detectors ---

def test_request_overdue_detected_and_wording(led):
    _req(led.db, "open", due="2026-09-10")
    _req(led.db, "done", due="2026-09-10")
    _req(led.db, "cancelled", due="2026-09-10")
    _req(led.db, "open", due="2026-10-01")
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


def test_request_aging(led):
    _req(led.db, "open", created=NOW - 40 * DAY)   # aged -> candidate
    _req(led.db, "open", created=NOW - 5 * DAY)    # fresh -> not
    res = _ev(led)
    sigs = mcs_signals.current_open(led.db)["items"]
    assert res["open"] == 1 and sigs[0]["type"] == "request_aging"
    assert sigs[0]["context"]["days_since_created"] == 40


def test_med_followup_skips_old_and_archived(led):
    # mention older than FOLLOWUP_MAX_AGE_D -> historical, not flagged
    _msg(led.db, 1, ts=NOW - 200 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    # recent mention but on an archived room -> not actionable
    _msg(led.db, 2, pid=2, ts=NOW - 30 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
    assert _ev(led)["open"] == 0


def test_med_followup_suppressed_by_any_request(led):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    # any registered request — even cancelled — is visible engagement
    _req(led.db, "cancelled", src_mid=1)
    assert _ev(led)["open"] == 0


def test_transition_reconciliation(led):
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院しました")
    _msg(led.db, 2, ts=NOW - 3 * DAY)
    _extract_llm(led.db, 2, "h1", [{"name": "薬A", "action": "change"}])
    _ev(led)
    sigs = mcs_signals.current_open(led.db)["items"]
    sig = [s for s in sigs
           if s["type"] == "transition_reconciliation"]
    assert len(sig) == 1
    assert sig[0]["evidence"]["discharge_message_id"] == 1
    assert sig[0]["evidence"]["med_change_message_ids"] == [2]


def test_transition_requires_med_change(led):
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院しました")
    _msg(led.db, 2, ts=NOW - 3 * DAY)
    _extract_llm(led.db, 2, "h1", [{"name": "薬A", "action": "none"}])
    assert _ev(led)["open"] == 0


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
    assert sig["context"]["days_left"] == 9
    assert "確定ではありません" in sig["note"]


# --- lifecycle (append-only transitions) ---

def test_signal_resolves_when_condition_clears(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    assert _states(led.db)["request_overdue:1:1"] == "open"
    led.db.execute("UPDATE requests SET status='done' WHERE request_id=1")
    _ev(led)
    assert _states(led.db)["request_overdue:1:1"] == "resolved"


def test_lifecycle_is_append_only(led):
    """open -> resolved -> open produces three rows, history intact."""
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    led.db.execute("UPDATE requests SET status='done' WHERE request_id=1")
    _ev(led)
    led.db.execute("UPDATE requests SET status='open' WHERE request_id=1")
    _ev(led)
    rows = [json.loads(r[0]) for r in led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1' "
        "ORDER BY artifact_id")]
    assert [r["state"] for r in rows] == ["open", "resolved", "open"]
    assert rows[0]["detected_at"] == NOW
    assert rows[1]["resolved_at"] is not None
    assert _states(led.db)["request_overdue:1:1"] == "open"


def test_no_duplicate_rows_when_unchanged(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    _ev(led)
    _ev(led)
    n = led.db.execute("SELECT COUNT(*) FROM artifacts "
                       "WHERE kind='signal_v1'").fetchone()[0]
    assert n == 1


# --- notify gating ---

def test_notify_off_by_default(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led, cfg={})
    n = led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    assert n == 0


def test_notify_requires_strict_true(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led, cfg={"signals": {"notify": "yes"}})   # truthy string != True
    n = led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    assert n == 0
    _ev(led, cfg={"signals": True})                # non-dict -> off
    n = led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    assert n == 0


def test_notify_enqueues_signal_kind_when_enabled(led):
    _req(led.db, "open", due="2026-09-10")
    res = _ev(led, cfg={"signals": {"notify": True}})
    row = led.db.execute("SELECT kind, payload FROM notify_outbox"
                         ).fetchone()
    assert res["notify_enqueued"] == 1 and row["kind"] == "signal"
    pl = json.loads(row["payload"])
    assert "レビュー候補" in pl["text"] and pl["signal_key"]


def test_notify_only_on_new_open_not_refresh(led):
    _req(led.db, "open", due="2026-09-10")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    _ev(led, cfg=cfg)  # still open, same evidence -> no write, no intent
    n = led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    assert n == 1


def test_notify_dedup_on_reopen_flap(led):
    """open -> resolved -> reopened while the first intent is still
    pending must not stack a second notification for the same key."""
    _req(led.db, "open", due="2026-09-10")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    led.db.execute("UPDATE requests SET status='done'")
    _ev(led, cfg=cfg)
    led.db.execute("UPDATE requests SET status='open'")
    res = _ev(led, cfg=cfg)
    n = led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    assert n == 1 and res["notify_enqueued"] == 0


# --- send-time gates (notifier._format_event) ---

def _sig_ev(led):
    row = led.db.execute(
        "SELECT kind, payload FROM notify_outbox").fetchone()
    return {"kind": row["kind"], "payload": row["payload"]}


def test_send_gate_flag_turned_off(led, monkeypatch):
    import notifier
    _req(led.db, "open", due="2026-09-10")
    _ev(led, cfg={"signals": {"notify": True}})
    monkeypatch.setattr(notifier, "_config", lambda: {})
    with pytest.raises(notifier._StaleSend,
                       match="signals_notify_disabled"):
        notifier._format_event(led, _sig_ev(led))


def test_send_gate_signal_resolved_while_queued(led, monkeypatch):
    import notifier
    _req(led.db, "open", due="2026-09-10")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    led.db.execute("UPDATE requests SET status='done'")
    _ev(led, cfg=cfg)              # resolves; intent still pending
    monkeypatch.setattr(notifier, "_config",
                        lambda: {"signals": {"notify": True}})
    with pytest.raises(notifier._StaleSend, match="signal_not_open"):
        notifier._format_event(led, _sig_ev(led))


def test_send_gate_open_signal_formats(led, monkeypatch):
    import notifier
    _req(led.db, "open", due="2026-09-10")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    monkeypatch.setattr(notifier, "_config", lambda: cfg)
    text, files = notifier._format_event(led, _sig_ev(led))
    assert "レビュー候補" in text and files == []


# --- malformed data resilience ---

def test_malformed_signal_artifact_skipped(led):
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
        "VALUES ('signal_v1',1,'{bad json','{bad json',0)")
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
        "VALUES ('signal_v1',1,'\"scalar\"','{\"key\":\"x\"}',0)")
    _req(led.db, "open", due="2026-09-10")
    res = _ev(led)                 # must not crash on opaque rows
    assert res["open"] == 1
    items = mcs_signals.current_open(led.db)["items"]
    assert len(items) == 1 and items[0]["type"] == "request_overdue"
