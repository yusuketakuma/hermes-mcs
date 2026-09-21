"""Tests for the prospective signal layer against a REAL Ledger on
tmp_path (same pattern as test_mcs_ingestion): detector correctness,
open/resolved lifecycle, append-only history, notify gating, honest
wording. No network or live data."""
import json
import time

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


def _extract_llm(db, mid, chash, meds, events=None):
    content = {"meds": meds}
    if events is not None:
        content["events"] = events
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps(content), json.dumps({"hash": chash})))


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
        meta_d, content_d = json.loads(meta), json.loads(content)
        if isinstance(meta_d, dict) and isinstance(content_d, dict):
            out[meta_d["key"]] = content_d["state"]
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
    assert sig["evidence"]["med"] == "薬A"
    assert sig["evidence"]["message_ids"] == [1]
    assert "確認できませんでした" in sig["note"]
    assert "対応の有無を示すものではありません" in sig["note"]


def test_med_episode_groups_same_med(led):
    """Repeated mentions of the same med collapse into ONE signal per
    room — the episode keeps all qualifying mention ids as evidence."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 20 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬A", "action": "change"},
                                   {"name": "薬B", "action": "start"}])
    _msg(led.db, 3, ts=NOW - 10 * DAY, chash="h3")   # last room post
    res = _ev(led)
    # last post is inside neither 30d+7d nor 20d+7d window -> both
    # mentions qualify; one signal for 薬A AND one for 薬B
    sigs = mcs_signals.current_open(led.db)["items"]
    meds = {s["evidence"]["med"]: s["evidence"]["message_ids"]
            for s in sigs if s["type"] == "med_change_no_followup"}
    assert meds == {"薬A": [1, 2], "薬B": [2]}
    assert res["open"] == 2


def test_med_episode_suppressed_by_later_answered_mention(led):
    """An early unanswered mention is suppressed when the LATEST mention
    of the same med did get a follow-up post in its window."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 15 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬A", "action": "change"}])
    _msg(led.db, 3, ts=NOW - 13 * DAY)   # answers the -15d mention only
    assert _ev(led)["open"] == 0


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
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院しました", chash="h1")
    _extract_llm(led.db, 1, "h1", [], events=["discharge"])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬A", "action": "change"}])
    _ev(led)
    sigs = mcs_signals.current_open(led.db)["items"]
    sig = [s for s in sigs
           if s["type"] == "transition_reconciliation"]
    assert len(sig) == 1
    assert sig[0]["evidence"]["discharge_message_id"] == 1
    assert sig[0]["evidence"]["med_change_message_ids"] == [2]


def test_transition_ignores_surface_only_text(led):
    """退院 substring without a typed discharge event is not evidence."""
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院できませんでした")
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬A", "action": "change"}])
    assert _ev(led)["open"] == 0


def test_transition_requires_med_change(led):
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院しました", chash="h1")
    _extract_llm(led.db, 1, "h1", [], events=["discharge"])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬A", "action": "none"}])
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


def test_notify_cooldown_suppresses_renotify(led):
    """A key notified recently stays silent on reopen; after the
    cooldown a genuine reopen notifies again."""
    _req(led.db, "open", due="2026-09-10")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    led.db.execute(
        "UPDATE notify_outbox SET state='accepted', updated_at=?",
        (NOW - 2 * DAY,))                        # delivered 2d ago
    led.db.execute("UPDATE requests SET status='done'")
    _ev(led, cfg=cfg)
    led.db.execute("UPDATE requests SET status='open'")
    res = _ev(led, cfg=cfg)                      # reopened inside cooldown
    assert res["notify_enqueued"] == 0
    n = led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0]
    assert n == 1
    led.db.execute("UPDATE notify_outbox SET updated_at=?",
                   (NOW - 8 * DAY,))             # cooldown elapsed
    led.db.execute("UPDATE requests SET status='done'")
    _ev(led, cfg=cfg)
    led.db.execute("UPDATE requests SET status='open'")
    res = _ev(led, cfg=cfg)
    assert res["notify_enqueued"] == 1


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


# --- human dismissal (ops.signal_dismiss via the command path) ---

def _dismiss(led, key, pid=1, reason="原記録を確認済み"):
    import mcs_requests
    from uuid import uuid4
    req = {"cmd": "ops.signal_dismiss", "version": 1,
           "command_id": str(uuid4()), "actor": "tester",
           "human_confirmed": True, "project_id": pid,
           "signal_key": key, "reason": reason}
    return mcs_requests.apply_command(led, req)


def test_dismiss_open_signal(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    key = "request_overdue:1:1"
    r = _dismiss(led, key)
    assert r["outcome"] == "applied"
    assert _states(led.db)[key] == "dismissed"
    assert mcs_signals.current_open(led.db)["items"] == []
    rows = [json.loads(x[0]) for x in led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1'")]
    assert rows[-1]["dismissed_by"] == "tester"
    assert rows[-1]["dismiss_reason"] == "原記録を確認済み"


def test_dismissed_signal_stays_down_while_evidence_same(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    _dismiss(led, "request_overdue:1:1")
    res = _ev(led)                 # condition still holds -> no reopen
    assert res["open"] == 1 and res["opened"] == 0
    assert _states(led.db)["request_overdue:1:1"] == "dismissed"


def test_dismissed_signal_reopens_on_evidence_change(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    _dismiss(led, "request_overdue:1:1")
    led.db.execute("UPDATE requests SET due_date='2026-09-05'")
    res = _ev(led)                 # evidence moved on -> reopen
    assert res["opened"] == 1
    assert _states(led.db)["request_overdue:1:1"] == "open"


def test_dismiss_unknown_and_nonopen(led):
    r = _dismiss(led, "request_overdue:1:999")
    assert r["outcome"] == "rejected" and r["error"] == "signal_not_found"
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    led.db.execute("UPDATE requests SET status='done'")
    _ev(led)                       # resolves -> no longer open
    r = _dismiss(led, "request_overdue:1:1")
    assert r["outcome"] == "rejected" and r["error"] == "signal_not_open"


def test_dismiss_validation(led):
    import mcs_requests
    from uuid import uuid4
    base = {"cmd": "ops.signal_dismiss", "version": 1, "actor": "t",
            "human_confirmed": True, "project_id": 1}
    # missing reason -> rejected before touching the ledger
    r = mcs_requests.apply_command(
        led, {**base, "command_id": str(uuid4()), "signal_key": "k"})
    assert r["outcome"] == "rejected" and r["error"] == "bad_reason"
    r = mcs_requests.apply_command(
        led, {**base, "command_id": str(uuid4()),
              "reason": "r"})                  # missing signal_key
    assert r["outcome"] == "rejected" and r["error"] == "bad_signal_key"


# --- human-approved threshold policy (ops.signal_policy) ---

def _policy(led, policy, reason="閾値承認"):
    import mcs_requests
    from uuid import uuid4
    req = {"cmd": "ops.signal_policy", "version": 1,
           "command_id": str(uuid4()), "actor": "tester",
           "human_confirmed": True, "project_id": 1,
           "policy": policy, "reason": reason}
    return mcs_requests.apply_command(led, req)


def test_policy_override_changes_detection(led):
    _req(led.db, "open", created=NOW - 10 * DAY)   # 10d old
    assert _ev(led)["open"] == 0                 # default: 30d
    r = _policy(led, {"req_age_days": 7})
    assert r["outcome"] == "applied"
    res = _ev(led)                               # approved 7d -> flags
    assert res["open"] == 1
    assert mcs_signals.current_open(led.db)["items"][0]["type"] \
        == "request_aging"


def test_policy_latest_wins_and_audited(led):
    _policy(led, {"req_age_days": 10})
    _policy(led, {"req_age_days": 40}, reason="再調整")
    _req(led.db, "open", created=NOW - 20 * DAY)
    assert _ev(led)["open"] == 0                 # latest = 40d
    rows = led.db.execute(
        "SELECT content, project_id FROM artifacts "
        "WHERE kind='signal_policy_v1' ORDER BY artifact_id").fetchall()
    assert len(rows) == 2 and rows[0]["project_id"] is None
    c = json.loads(rows[-1]["content"])
    assert c["policy"] == {"req_age_days": 40}
    assert c["actor"] == "tester" and c["reason"] == "再調整"


def test_policy_validation(led):
    r = _policy(led, {"bogus_key": 5})
    assert r["outcome"] == "rejected" and r["error"] == "unknown_policy_key"
    r = _policy(led, {"req_age_days": 9999})
    assert r["outcome"] == "rejected" and r["error"] == "bad_policy_value"
    r = _policy(led, {"req_age_days": "five"})
    assert r["outcome"] == "rejected" and r["error"] == "bad_policy_value"
    import mcs_requests
    from uuid import uuid4
    r = mcs_requests.apply_command(led, {
        "cmd": "ops.signal_policy", "version": 1,
        "command_id": str(uuid4()), "actor": "t",
        "human_confirmed": True, "project_id": 1,
        "policy": {}, "reason": "r"})
    assert r["outcome"] == "rejected" and r["error"] == "bad_policy"


# --- deadline / partial evaluation must not fabricate resolutions ---

def test_deadline_stop_never_resolves_unrun_types(led):
    """A run cut short by the deadline (or a crashed detector) must not
    write 'resolved' for signal types it never inspected — 'could not
    check' is not 'checked, nothing found'."""
    # src_mid=999: the request must not sit on the med-mention message
    # (a request on the mention is a visible follow-up and suppresses it)
    _req(led.db, "open", due="2026-09-10", src_mid=999)  # request_overdue
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1",
                 [{"name": "薬A", "action": "stop"}])  # med episode
    _ev(led)
    assert _states(led.db)["request_overdue:1:1"] == "open"
    assert _states(led.db)["med_change_no_followup:1:薬A"] == "open"
    # deadline already past -> NO detector runs -> nothing resolves
    # (deadline is a monotonic clock, not epoch — pass an expired one)
    res = mcs_signals.evaluate(led, {}, now=NOW,
                               deadline=time.monotonic() - 1)
    assert res["resolved"] == 0 and res["open"] == 0
    assert res["detectors_ran"] == []
    assert _states(led.db)["request_overdue:1:1"] == "open"
    assert _states(led.db)["med_change_no_followup:1:薬A"] == "open"


def test_crashed_detector_type_is_not_resolved(led, monkeypatch):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    def boom(db, now, th):
        raise RuntimeError("detector exploded")
        yield
    monkeypatch.setattr(mcs_signals, "DETECTORS", (
        ("request_overdue", boom),))
    res = _ev(led)
    assert res["errors"] == ["request_overdue:RuntimeError"]
    assert _states(led.db)["request_overdue:1:1"] == "open"


def test_policy_requires_provenance(led):
    """A signal_policy_v1 artifact without command_id/actor (i.e. not
    written via the human-confirmed command path) is ignored."""
    _req(led.db, "open", created=NOW - 10 * DAY)
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
        "VALUES ('signal_policy_v1',NULL,?,?,0)",
        (json.dumps({"policy": {"req_age_days": 7}}), "{}"))
    assert _ev(led)["open"] == 0          # provenance missing -> default
    _policy(led, {"req_age_days": 7})
    assert _ev(led)["open"] == 1          # approved policy applies


def test_dismiss_corrupt_signal_row(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
        "VALUES ('signal_v1',1,'\"scalar\"',?,0)",
        (json.dumps({"key": "request_overdue:1:1"}),))
    led.db.commit()   # apply_command opens BEGIN IMMEDIATE itself
    r = _dismiss(led, "request_overdue:1:1")
    assert r["outcome"] == "rejected" and r["error"] == "signal_corrupt"


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
