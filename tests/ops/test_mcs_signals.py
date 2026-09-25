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


def _msg(db, mid, pid=1, ts=NOW - 30 * DAY, chash="h1", body="b",
         prof="看護師", org="org", parent=None):
    db.execute(
        "INSERT INTO messages(message_id,project_id,parent_id,sender_id,"
        "sender_name,sender_type,profession,organization,posted_at,"
        "posted_at_ts,body_text,body_state,content_hash,reply_count,"
        "is_unread,first_seen,updated_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, parent, 1, "n", "staff", prof, org,
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


def _extract_doc(db, mid, chash, **fields):
    """extract_llm artifact with an arbitrary v2 document — requests,
    symptoms, urgency, events, meds."""
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps(fields), json.dumps({"hash": chash})))


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
    """A bare discharge with no med change co-occurrence is NOT a
    transition_reconciliation signal (it IS a discharge_notice — that
    sibling owns bare transitions now)."""
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院しました", chash="h1")
    _extract_llm(led.db, 1, "h1", [], events=["discharge"])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬A", "action": "none"}])
    _ev(led)
    sigs = mcs_signals.current_open(led.db)["items"]
    assert not [s for s in sigs if s["type"] == "transition_reconciliation"]
    dn = [s for s in sigs if s["type"] == "discharge_notice"]
    assert len(dn) == 1
    assert dn[0]["evidence"]["discharge_message_id"] == 1


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
    """request_overdue is digest-tier: the intent is a pending digest
    row holding the key, scheduled for delayed delivery."""
    _req(led.db, "open", due="2026-09-10")
    res = _ev(led, cfg={"signals": {"notify": True}})
    row = led.db.execute(
        "SELECT kind, payload, next_try FROM notify_outbox").fetchone()
    assert res["notify_enqueued"] == 1 and row["kind"] == "signal"
    pl = json.loads(row["payload"])
    assert pl["digest"] is True
    assert pl["signal_keys"] == ["request_overdue:1:1"]
    assert "ダイジェスト" in pl["text"]
    assert row["next_try"] > NOW          # delayed, not immediate
    # signals.digest:false restores per-signal immediate intents
    led.db.execute("DELETE FROM notify_outbox")
    led.db.execute("DELETE FROM artifacts WHERE kind='signal_v1'")
    res = _ev(led, cfg={"signals": {"notify": True, "digest": False}})
    pl = json.loads(led.db.execute(
        "SELECT payload FROM notify_outbox").fetchone()["payload"])
    assert res["notify_enqueued"] == 1
    assert pl.get("digest") is not True and pl["signal_key"]


def test_signal_notice_renders_patient_and_snippet(led, monkeypatch):
    """The stored payload stays ids+frozen note, but the SENT text
    resolves patient name and the latest mention's snippet so the
    notice says who/what without a manual lookup."""
    _msg(led.db, 1, ts=NOW - 30 * DAY, body="エリキュースを開始しました")
    led.db.execute("UPDATE patients SET patient_name='山田テスト' "
                   "WHERE project_id=1")
    _extract_llm(led.db, 1, "h1",
                 [{"name": "エリキュース", "action": "start"}])
    _ev(led, cfg={"signals": {"notify": True}})
    ev = led.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='signal'").fetchone()
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    content, files = notify_flush._format_event(led, ev)
    assert "山田テスト（project 1 / med エリキュース）" in content
    assert "最新言及" in content and "エリキュースを開始しました" in content
    assert '"op":"timeline"' in content and '"project_id":1' in content
    assert files == []


def test_signal_notice_degrades_without_patient_or_message(
        led, monkeypatch):
    """No patient row / deleted message -> base text still sends."""
    _req(led.db, "open", due="2026-09-10")
    _ev(led, cfg={"signals": {"notify": True}})
    ev = led.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='signal'").fetchone()
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    content, _ = notify_flush._format_event(led, ev)
    assert "レビュー候補" in content and "request_overdue" in content


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


# --- send-time gates (notify_flush._format_event) ---

def _sig_ev(led):
    row = led.db.execute(
        "SELECT kind, payload FROM notify_outbox").fetchone()
    return {"kind": row["kind"], "payload": row["payload"]}


def test_send_gate_flag_turned_off(led, monkeypatch):
    import notify_flush
    _req(led.db, "open", due="2026-09-10")
    _ev(led, cfg={"signals": {"notify": True}})
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    with pytest.raises(notify_flush._StaleSend,
                       match="signals_notify_disabled"):
        notify_flush._format_event(led, _sig_ev(led))


def test_send_gate_signal_resolved_while_queued(led, monkeypatch):
    import notify_flush
    _req(led.db, "open", due="2026-09-10")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    led.db.execute("UPDATE requests SET status='done'")
    _ev(led, cfg=cfg)              # resolves; intent still pending
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    with pytest.raises(notify_flush._StaleSend, match="signal_not_open"):
        notify_flush._format_event(led, _sig_ev(led))


def test_send_gate_open_signal_formats(led, monkeypatch):
    import notify_flush
    _req(led.db, "open", due="2026-09-10")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    text, files = notify_flush._format_event(led, _sig_ev(led))
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
    def boom(db, now, th, sig_cfg):
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


# --- same-post med_change notification coalescing ---

def _set_meds(db, meds, mid=1):
    """Rewrite the med list of one extract_llm artifact — the detector
    toggle used to resolve/reopen med signals between evaluations."""
    db.execute(
        "UPDATE artifacts SET content=? WHERE kind='extract_llm' "
        "AND message_id=?", (json.dumps({"meds": meds}), mid))


def _outbox_payloads(led):
    return [json.loads(r[0]) for r in led.db.execute(
        "SELECT payload FROM notify_outbox ORDER BY event_id")]


def test_same_post_meds_merge_into_one_notice(led, monkeypatch):
    """Two meds change-mentioned in the SAME post fold into ONE digest
    intent and render as ONE merged unit inside it — per-med signal
    rows keep their own open state and keys."""
    _msg(led.db, 1, ts=NOW - 30 * DAY,
         body="インスリン管理は出来ない。在宅酸素は出来ない。")
    led.db.execute("UPDATE patients SET patient_name='山田 テスト' "
                   "WHERE project_id=1")
    _extract_llm(led.db, 1, "h1", [{"name": "インスリン", "action": "stop"},
                                   {"name": "在宅酸素", "action": "stop"}])
    res = _ev(led, cfg={"signals": {"notify": True}})
    # 2 med_change + 1 adherence_concern — the body phrase
    # 「管理は出来ない」 is itself an adherence mention
    assert res["open"] == 3 and res["notify_enqueued"] == 1
    pls = _outbox_payloads(led)
    assert len(pls) == 1
    pl = pls[0]
    assert pl["digest"] is True
    assert sorted(pl["signal_keys"]) == [
        "adherence_concern:1:1",
        "med_change_no_followup:1:インスリン",
        "med_change_no_followup:1:在宅酸素"]
    assert "signal_key" not in pl
    # per-med lifecycle untouched
    assert _states(led.db)["med_change_no_followup:1:インスリン"] == "open"
    assert _states(led.db)["med_change_no_followup:1:在宅酸素"] == "open"
    # digest render re-groups: both meds appear as ONE merged unit
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    text, files = notify_flush._format_event(led, ev)
    assert "ダイジェスト（3件）" in text
    assert "med インスリン・在宅酸素" in text
    assert "薬「インスリン」「在宅酸素」の変更言及後" in text
    assert "対応の有無を示すものではありません" in text
    assert "山田 テスト" in text and "最新言及" in text
    assert '"op":"timeline"' in text and files == []


def test_same_post_meds_merge_immediate_tier(led):
    """The same-post merge is the shared unit machinery — with
    digest off the merged intent is an immediate signal_keys payload."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"},
                                   {"name": "薬B", "action": "stop"}])
    res = _ev(led, cfg={"signals": {"notify": True, "digest": False}})
    assert res["notify_enqueued"] == 1
    pls = _outbox_payloads(led)
    assert len(pls) == 1 and pls[0].get("digest") is not True
    assert sorted(pls[0]["signal_keys"]) == [
        "med_change_no_followup:1:薬A", "med_change_no_followup:1:薬B"]
    assert pls[0]["type"] == "med_change_no_followup"


def test_shared_latest_mention_merges_despite_history(led):
    """A med with a deeper episode and a med mentioned only in the
    latest post still merge — the triggering post is the same. The
    signal evidence keeps its full mention history; only the
    notification coalesces."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 20 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬A", "action": "change"},
                                   {"name": "薬B", "action": "start"}])
    res = _ev(led, cfg={"signals": {"notify": True}})
    assert res["notify_enqueued"] == 1
    pls = _outbox_payloads(led)
    assert len(pls) == 1
    assert sorted(pls[0]["signal_keys"]) == [
        "med_change_no_followup:1:薬A", "med_change_no_followup:1:薬B"]
    items = {s["evidence"]["med"]: s["evidence"]["message_ids"]
             for s in mcs_signals.current_open(led.db)["items"]
             if s["type"] == "med_change_no_followup"}
    assert items == {"薬A": [1, 2], "薬B": [2]}     # evidence intact


def test_single_med_notice_unchanged(led, monkeypatch):
    """One med in the post -> one digest member rendering as a single
    unit with the original wording."""
    _msg(led.db, 1, ts=NOW - 30 * DAY, body="薬Aを中止しました")
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _ev(led, cfg={"signals": {"notify": True}})
    pls = _outbox_payloads(led)
    assert len(pls) == 1
    assert pls[0]["signal_keys"] == ["med_change_no_followup:1:薬A"]
    assert pls[0]["digest"] is True
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    text, _ = notify_flush._format_event(led, ev)
    assert "ダイジェスト（1件）" in text
    assert "med 薬A" in text and "薬「薬A」の変更言及後" in text
    assert "・" not in text


def test_distinct_posts_not_merged(led, monkeypatch):
    """Meds whose LATEST mentions are different posts share the digest
    intent but render as SEPARATE units — only the same triggering
    post coalesces."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 20 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    res = _ev(led, cfg={"signals": {"notify": True}})
    assert res["notify_enqueued"] == 1
    pls = _outbox_payloads(led)
    assert len(pls) == 1 and pls[0]["digest"] is True
    assert sorted(pls[0]["signal_keys"]) == [
        "med_change_no_followup:1:薬A", "med_change_no_followup:1:薬B"]
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    text, _ = notify_flush._format_event(led, ev)
    assert "med 薬A・薬B" not in text          # no coalesced unit
    assert "med 薬A" in text and "med 薬B" in text


def test_different_projects_not_merged(led):
    _msg(led.db, 1, pid=1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, pid=2, ts=NOW - 30 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    res = _ev(led, cfg={"signals": {"notify": True}})
    assert res["notify_enqueued"] == 1         # one digest, two keys
    pls = _outbox_payloads(led)
    assert len(pls) == 1
    assert sorted(pls[0]["signal_keys"]) == [
        "med_change_no_followup:1:薬A", "med_change_no_followup:2:薬B"]


def test_other_signal_types_not_merged(led):
    """Different signal TYPES never coalesce into a merged unit — the
    digest lists them individually (same-post meds are the only
    mergeable unit)."""
    _req(led.db, "open", due="2026-09-10", src_mid=999)
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    res = _ev(led, cfg={"signals": {"notify": True}})
    assert res["notify_enqueued"] == 1
    pls = _outbox_payloads(led)
    assert len(pls) == 1 and pls[0]["digest"] is True
    assert sorted(pls[0]["signal_keys"]) == [
        "med_change_no_followup:1:薬A", "request_overdue:1:1"]


def test_merged_intent_not_duplicated_on_reeval(led):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"},
                                   {"name": "薬B", "action": "stop"}])
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    res = _ev(led, cfg=cfg)     # unchanged -> nothing re-enqueued
    assert res["notify_enqueued"] == 0
    assert len(_outbox_payloads(led)) == 1


def test_send_gate_group_member_resolved(led, monkeypatch):
    """A member med that resolves while the merged intent is queued
    drops out of the notice — the still-open med still sends."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"},
                                   {"name": "薬B", "action": "stop"}])
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    _set_meds(led.db, [{"name": "薬A", "action": "stop"}])
    _ev(led, cfg=cfg)           # 薬B resolved; merged intent still queued
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    text, _ = notify_flush._format_event(led, ev)
    assert "薬「薬A」" in text and "薬B" not in text
    assert "med 薬A" in text


def test_send_gate_group_all_resolved(led, monkeypatch):
    """All members resolved while queued -> terminal drop."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"},
                                   {"name": "薬B", "action": "stop"}])
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    _set_meds(led.db, [])
    _ev(led, cfg=cfg)
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    with pytest.raises(notify_flush._StaleSend, match="signal_not_open"):
        notify_flush._format_event(led, ev)


def test_group_intent_covers_member_reopen(led):
    """Members re-opening while the merged intent is still undelivered
    must not stack a second notification."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    meds = [{"name": "薬A", "action": "stop"},
            {"name": "薬B", "action": "stop"}]
    _extract_llm(led.db, 1, "h1", meds)
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    _set_meds(led.db, [])
    _ev(led, cfg=cfg)           # resolve both
    _set_meds(led.db, meds)
    res = _ev(led, cfg=cfg)     # reopen both — pending group covers them
    assert res["notify_enqueued"] == 0
    assert len(_outbox_payloads(led)) == 1


def test_group_intent_cooldown_covers_members(led):
    """An ACCEPTED merged intent keeps each member key inside its
    cooldown — a reopened member stays silent, then notifies after the
    cooldown as usual."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    meds = [{"name": "薬A", "action": "stop"},
            {"name": "薬B", "action": "stop"}]
    _extract_llm(led.db, 1, "h1", meds)
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    led.db.execute(
        "UPDATE notify_outbox SET state='accepted', updated_at=?",
        (NOW - 2 * DAY,))                       # delivered 2d ago
    _set_meds(led.db, [])
    _ev(led, cfg=cfg)
    _set_meds(led.db, meds)
    res = _ev(led, cfg=cfg)                     # reopened inside cooldown
    assert res["notify_enqueued"] == 0
    assert len(_outbox_payloads(led)) == 1
    led.db.execute("UPDATE notify_outbox SET updated_at=?",
                   (NOW - 8 * DAY,))            # cooldown elapsed
    _set_meds(led.db, [])
    _ev(led, cfg=cfg)
    _set_meds(led.db, meds)
    res = _ev(led, cfg=cfg)
    assert res["notify_enqueued"] == 1          # merged again, one row
    assert len(_outbox_payloads(led)) == 2
    assert "signal_keys" in _outbox_payloads(led)[-1]


# --- self identity / capability / exclusions (priority 1+3+4) ---

def test_self_org_med_mention_excluded(led):
    """A med-change mention authored by the pharmacy itself is not a
    review candidate — our own reports need no follow-up ping to us.
    Without self_organizations configured nothing is suppressed."""
    _msg(led.db, 1, ts=NOW - 30 * DAY, org="みどり薬局")
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    res = _ev(led)
    assert res["open"] == 1            # unconfigured: org is just an org
    # configure self -> self-authored mentions drop out of the
    # evidence set entirely: new ones open nothing, and the
    # already-open one RESOLVES (evidence gone) — that is exactly how
    # the self-authored backlog cleans itself up, append-only.
    cfg = {"signals": {"self_organizations": ["みどり薬局"]}}
    _msg(led.db, 2, ts=NOW - 20 * DAY, chash="h2",
         org="みどり薬局")
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    _ev(led, cfg=cfg)
    states = _states(led.db)
    assert "med_change_no_followup:1:薬B" not in states
    assert states["med_change_no_followup:1:薬A"] == "resolved"


def test_capability_evidence_is_not_med_change(led):
    """「〜管理は出来ない」 is a capability statement, not a
    prescription change — the insulin false positive must not recur."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_doc(led.db, 1, "h1", meds=[
        {"name": "インスリン", "action": "stop",
         "evidence": "薬、インスリン管理は出来ない"},
        {"name": "在宅酸素", "action": "stop",
         "evidence": "在宅酸素は出来ない"},
        {"name": "薬C", "action": "stop", "evidence": "薬Cを中止した"}])
    _ev(led)
    states = _states(led.db)
    assert "med_change_no_followup:1:薬C" in states
    assert "med_change_no_followup:1:インスリン" not in states
    assert "med_change_no_followup:1:在宅酸素" not in states
    # the capability mentions land in adherence_concern instead
    ad = [s for s in mcs_signals.current_open(led.db)["items"]
          if s["type"] == "adherence_concern"]
    assert len(ad) == 1
    assert set(ad[0]["evidence"]["mentions"]) == {"インスリン", "在宅酸素"}
    assert "服薬管理" in ad[0]["note"]


def test_med_exclude_names(led):
    """Configured non-dispensed names never produce med_change
    signals; matching is whitespace-insensitive."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "在宅酸素", "action": "stop"},
                                   {"name": "薬A", "action": "stop"}])
    cfg = {"signals": {"med_exclude_names": ["在宅 酸素"]}}
    _ev(led, cfg=cfg)
    states = _states(led.db)
    assert "med_change_no_followup:1:薬A" in states
    assert "med_change_no_followup:1:在宅酸素" not in states


# --- pharmacist_request_unanswered / rx_request_visibility ---

def _req_item(to, action, unverified=None):
    r = {"to": to, "action": action}
    if unverified is not None:
        r["unverified"] = unverified
    return r


def test_pharmacist_request_unanswered(led):
    """A pharmacist-addressed request past the response window with no
    responder post and no registered request -> review candidate with
    honest 'no record' wording."""
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("薬剤師", "残薬調整の確認")])
    _ev(led)
    items = mcs_signals.current_open(led.db)["items"]
    ph = [s for s in items if s["type"] == "pharmacist_request_unanswered"]
    assert len(ph) == 1
    assert "記録上の確認" in ph[0]["note"]
    assert "残薬調整の確認" in ph[0]["note"]
    # a pharmacist-profession post after the mention counts as a
    # responder -> no signal
    led.db.execute("DELETE FROM artifacts WHERE kind='signal_v1'")
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2", prof="薬剤師")
    _ev(led)
    assert not [s for s in mcs_signals.current_open(led.db)["items"]
                if s["type"] == "pharmacist_request_unanswered"]


def test_pharmacist_request_registered_or_unverified(led):
    """Registered requests and unverified extractions are not
    unanswered-request candidates."""
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("薬剤師", "残薬調整の確認")])
    _req(led.db, "open", src_mid=1)
    _msg(led.db, 2, ts=NOW - 4 * DAY, chash="h2")
    _extract_doc(led.db, 2, "h2",
                 requests=[_req_item("薬局", "確認", unverified=True)])
    _ev(led)
    assert not [s for s in mcs_signals.current_open(led.db)["items"]
                if s["type"] == "pharmacist_request_unanswered"]


def test_pharmacist_request_recent_not_flagged(led):
    """Inside the response window the request is not yet a candidate."""
    _msg(led.db, 1, ts=NOW - 1 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("薬剤師", "残薬調整の確認")])
    _ev(led)
    assert not [s for s in mcs_signals.current_open(led.db)["items"]
                if s["type"] == "pharmacist_request_unanswered"]


def test_rx_request_visibility(led):
    """Med-related requests aimed at OTHER professions are FYI-visible
    to the pharmacy; pharmacist-addressed ones belong to the
    unanswered detector instead."""
    _msg(led.db, 1, ts=NOW - 1 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("医師", "フロセミド処方"),
                           _req_item("看護師", "バイタル測定")])
    _msg(led.db, 2, ts=NOW - 1 * DAY, chash="h2")
    _extract_doc(led.db, 2, "h2",
                 requests=[_req_item("薬剤師", "残薬調整の確認")])
    _ev(led)
    items = mcs_signals.current_open(led.db)["items"]
    rx = [s for s in items if s["type"] == "rx_request_visibility"]
    assert len(rx) == 1
    assert rx[0]["evidence"]["message_ids"] == [1]
    assert "フロセミド処方" in rx[0]["note"]
    assert "記録上の言及" in rx[0]["note"]


# --- discharge_notice / symptom_after_med_change ---

def test_discharge_notice_bare(led):
    """A bare discharge mention is a pharmacy-relevant heads-up even
    without a med-change co-occurrence; a co-occurring one stays with
    transition_reconciliation only."""
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院しました")
    _extract_doc(led.db, 1, "h1", events=["discharge"])
    _ev(led)
    items = mcs_signals.current_open(led.db)["items"]
    dn = [s for s in items if s["type"] == "discharge_notice"]
    assert len(dn) == 1 and dn[0]["evidence"]["discharge_message_id"] == 1
    # add a med change inside the window -> transition takes over,
    # the bare notice resolves
    _msg(led.db, 2, ts=NOW - 4 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬A", "action": "change"}])
    _ev(led)
    states = _states(led.db)
    assert states.get("transition_reconciliation:1:1") == "open"
    assert states.get("discharge_notice:1:1") == "resolved"


def test_discharge_notice_self_authored_excluded(led):
    _msg(led.db, 1, ts=NOW - 5 * DAY, body="退院報告",
         org="みどり薬局")
    _extract_doc(led.db, 1, "h1", events=["discharge"])
    cfg = {"signals": {"self_organizations": ["みどり薬局"]}}
    _ev(led, cfg=cfg)
    assert not [s for s in mcs_signals.current_open(led.db)["items"]
                if s["type"] in ("discharge_notice",
                                 "transition_reconciliation")]


def test_symptom_after_med_change_same_post(led):
    """Strict same-post coupling: med change + new symptom in ONE
    extraction -> ADR-triage prompt; a symptom in a DIFFERENT post
    does not couple."""
    _msg(led.db, 1, ts=NOW - 1 * DAY)
    _extract_doc(led.db, 1, "h1",
                 meds=[{"name": "薬A", "action": "start"}],
                 symptoms=[{"text": "浮腫", "status": "new",
                            "negated": False}])
    _msg(led.db, 2, pid=2, ts=NOW - 1 * DAY, chash="h2")
    _extract_doc(led.db, 2, "h2", meds=[{"name": "薬B", "action": "stop"}])
    _msg(led.db, 3, pid=2, ts=NOW - 1 * DAY, chash="h3")
    _extract_doc(led.db, 3, "h3",
                 symptoms=[{"text": "倦怠感", "status": "ongoing",
                            "negated": False}])
    _ev(led)
    items = [s for s in mcs_signals.current_open(led.db)["items"]
             if s["type"] == "symptom_after_med_change"]
    assert len(items) == 1
    assert items[0]["project_id"] == 1
    assert items[0]["evidence"]["meds"] == ["薬A"]
    assert items[0]["evidence"]["symptoms"] == ["浮腫"]
    assert "関連は人が原記録で判断" in items[0]["note"]


# --- adherence body phrases ---

def test_adherence_body_phrases(led):
    """Body-level adherence phrases that never become meds items are
    still candidates; plain negations do not match."""
    _msg(led.db, 1, ts=NOW - 1 * DAY, body="飲み忘れが多いとのこと")
    _msg(led.db, 2, ts=NOW - 1 * DAY, chash="h2", body="残薬はありません")
    _ev(led)
    items = [s for s in mcs_signals.current_open(led.db)["items"]
             if s["type"] == "adherence_concern"]
    assert len(items) == 1
    assert items[0]["evidence"]["message_ids"] == [1]


# --- tiers / urgency / digest machinery ---

def test_urgency_high_escalates_to_immediate(led, monkeypatch):
    """urgency:high on the source mention promotes a digest-tier
    signal to an immediate intent carrying the urgent flag."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_doc(led.db, 1, "h1", urgency="high",
                 meds=[{"name": "薬A", "action": "stop"}])
    res = _ev(led, cfg={"signals": {"notify": True}})
    assert res["notify_enqueued"] == 1 and res["notify_digest_merged"] == 0
    pl = _outbox_payloads(led)[0]
    assert pl.get("digest") is not True
    assert pl["urgent"] is True
    assert pl["signal_key"] == "med_change_no_followup:1:薬A"
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    text, _ = notify_flush._format_event(led, ev)
    assert "urgency:high" in text


def test_tier_override_config(led):
    """signals.tiers can pull a type back to immediate delivery."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    cfg = {"signals": {"notify": True,
                       "tiers": {"med_change_no_followup": "immediate"}}}
    _ev(led, cfg=cfg)
    pl = _outbox_payloads(led)[0]
    assert pl.get("digest") is not True
    assert pl["signal_key"] == "med_change_no_followup:1:薬A"


def test_digest_accumulates_across_evals(led):
    """A second digest-tier signal folds into the SAME pending digest
    intent — no second row — until the digest delivers."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    _msg(led.db, 2, ts=NOW - 29 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    res = _ev(led, cfg=cfg)
    assert res["notify_enqueued"] == 0
    assert res["notify_digest_merged"] == 1
    pls = _outbox_payloads(led)
    assert len(pls) == 1
    assert sorted(pls[0]["signal_keys"]) == [
        "med_change_no_followup:1:薬A", "med_change_no_followup:1:薬B"]
    assert "2件" in pls[0]["text"]


def test_digest_send_drops_resolved_members(led, monkeypatch):
    """At send time a digest member that resolved is dropped; the
    still-open members still render (the mixed-type case)."""
    _req(led.db, "open", due="2026-09-10", src_mid=999)
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    led.db.execute("UPDATE requests SET status='done'")
    _ev(led, cfg=cfg)              # request_overdue resolved
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    text, _ = notify_flush._format_event(led, ev)
    assert "ダイジェスト（1件）" in text
    assert "med 薬A" in text and "request_overdue" not in text


def test_self_org_request_responder(led):
    """A post by the configured self organization counts as a
    responder for pharmacist-directed requests."""
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("薬剤師", "残薬調整の確認")])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2", prof="その他",
         org="みどり薬局")
    cfg = {"signals": {"self_organizations": ["みどり薬局"]}}
    _ev(led, cfg=cfg)
    assert not [s for s in mcs_signals.current_open(led.db)["items"]
                if s["type"] == "pharmacist_request_unanswered"]


# --- MCS-fetched self identity (self_profile_v1 artifact) ---

def test_self_profile_artifact_supplies_defaults(led):
    """Without config, the fetched MCS self profile (recorded as an
    artifact by run_check) supplies self organizations/professions."""
    mcs_signals.record_self_profile(led.db, {
        "sender_id": 42, "name": "山田 薬剤",
        "professions": ["薬剤師"], "organizations": ["みどり薬局"]})
    _msg(led.db, 1, ts=NOW - 30 * DAY, org="みどり薬局")
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 20 * DAY, chash="h2")   # other org
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    _ev(led)
    states = _states(led.db)
    assert "med_change_no_followup:1:薬A" not in states
    assert states["med_change_no_followup:1:薬B"] == "open"


def test_config_overrides_self_profile_artifact(led):
    """An explicit signals.self_organizations wins over the fetched
    profile — manual override beats derivation."""
    mcs_signals.record_self_profile(led.db, {
        "sender_id": 42, "name": "山田 薬剤",
        "professions": [], "organizations": ["みどり薬局"]})
    _msg(led.db, 1, ts=NOW - 30 * DAY, org="みどり薬局")
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    cfg = {"signals": {"self_organizations": ["そら薬局"]}}
    _ev(led, cfg=cfg)
    # みどり薬局 is NOT self under the override -> signal opens
    assert _states(led.db)["med_change_no_followup:1:薬A"] == "open"


def test_record_self_profile_dedupes(led):
    """Identical profiles are not re-appended — append-only stays
    clean across ticks."""
    prof = {"sender_id": 42, "name": "山田 薬剤",
            "professions": ["薬剤師"], "organizations": ["みどり薬局"]}
    assert mcs_signals.record_self_profile(led.db, prof) is True
    assert mcs_signals.record_self_profile(led.db, prof) is False
    changed = dict(prof, organizations=["みどり薬局", "そら薬局"])
    assert mcs_signals.record_self_profile(led.db, changed) is True
    n = led.db.execute("SELECT COUNT(*) FROM artifacts "
                       "WHERE kind='self_profile_v1'").fetchone()[0]
    assert n == 2
    latest = mcs_signals._latest_self_profile(led.db)
    assert latest["organizations"] == ["みどり薬局", "そら薬局"]
    assert latest["name"] == "山田 薬剤"


# --- review fixes: negation handling, v1 urgency, salvage, archived ---

def test_adherence_negations_do_not_flag(led):
    """Explicit negations must not fire adherence_concern; affirmative
    capability/difficulty reports still do."""
    _msg(led.db, 1, ts=NOW - 1 * DAY, body="残薬ありません")
    _msg(led.db, 2, ts=NOW - 1 * DAY, chash="h2",
         body="飲み忘れていない")
    _msg(led.db, 3, ts=NOW - 1 * DAY, chash="h3",
         body="インスリン管理は出来ない")
    _msg(led.db, 4, ts=NOW - 1 * DAY, chash="h4",
         body="飲み忘れてしまったとのこと")
    _ev(led)
    items = [s for s in mcs_signals.current_open(led.db)["items"]
             if s["type"] == "adherence_concern"]
    mids = sorted(m for s in items
                  for m in s["evidence"]["message_ids"])
    assert mids == [3, 4]


def test_adherence_past_status_excluded(led):
    """A negated med reported as PAST history is not a current
    adherence concern."""
    _msg(led.db, 1, ts=NOW - 1 * DAY, body="記録のみ")
    _extract_doc(led.db, 1, "h1", meds=[
        {"name": "薬A", "action": None, "negated": True,
         "status": "past"}])
    _ev(led)
    assert not [s for s in mcs_signals.current_open(led.db)["items"]
                if s["type"] == "adherence_concern"]


@pytest.mark.parametrize("body,expected", [
    ("服薬管理できています", False),
    ("服薬管理できるようになりました", False),
    ("服薬管理できません", True),
    ("服薬管理できない", True),
])
def test_adherence_capability_distinguishes_ability(led, body, expected):
    _msg(led.db, 1, ts=NOW - DAY, body=body)
    _ev(led)
    found = any(s["type"] == "adherence_concern"
                for s in mcs_signals.current_open(led.db)["items"])
    assert found is expected


@pytest.mark.parametrize("evidence", [
    "薬Aを受け取り出来ない", "薬Aを受け取りできない",
    "薬Aを受け取り出来ません", "薬Aを受け取りできません",
])
def test_adherence_extracted_capability_spellings(led, evidence):
    _msg(led.db, 1, ts=NOW - DAY, body=evidence)
    _extract_doc(led.db, 1, "h1", meds=[
        {"name": "薬A", "action": "none", "evidence": evidence}])
    _ev(led)
    assert _states(led.db).get("adherence_concern:1:1") == "open"


@pytest.mark.parametrize("state", ["snippet", "unknown", "deleted"])
def test_adherence_body_scan_requires_available_full_record(led, state):
    _msg(led.db, 1, ts=NOW - DAY, body="飲み忘れが多い")
    led.db.execute("UPDATE messages SET body_state=?", (state,))
    _ev(led)
    assert "adherence_concern:1:1" not in _states(led.db)


def test_deleted_responder_does_not_resolve_unanswered_request(led):
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1", requests=[_req_item("薬剤師", "残薬確認")])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2", prof="薬剤師")
    led.db.execute("UPDATE messages SET body_state='deleted',body_text='' "
                   "WHERE message_id=2")
    _ev(led)
    assert _states(led.db).get("pharmacist_request_unanswered:1:1") == "open"


def test_task_like_to_not_pharmacist_addressed(led):
    """A free-text requests.to that merely contains 薬 (「薬の確認」)
    is not pharmacist-addressed — it is other-profession visibility."""
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("薬の確認", "残薬の確認")])
    _ev(led)
    items = mcs_signals.current_open(led.db)["items"]
    assert not [s for s in items
                if s["type"] == "pharmacist_request_unanswered"]
    assert [s for s in items if s["type"] == "rx_request_visibility"]


def test_urgency_high_from_extract_v1_escalates(led):
    """extract_v1 (rule extractor) carries urgency too — a message
    the LLM never processed still escalates its signal."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    led.db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',?,?,?)",
        (1, json.dumps({"urgency": "high"}), json.dumps({"hash": "h1"})))
    res = _ev(led, cfg={"signals": {"notify": True}})
    assert res["notify_enqueued"] == 1
    pl = _outbox_payloads(led)[0]
    assert pl.get("digest") is not True and pl["urgent"] is True


@pytest.mark.parametrize("kind", ["extract_llm", "extract_v1"])
def test_stale_high_urgency_does_not_escalate_current_signal(led, kind):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    led.db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) VALUES(?,1,?,?)",
        (kind, json.dumps({"urgency": "high"}), json.dumps({"hash": "old-body"})))
    res = _ev(led, cfg={"signals": {"notify": True}})
    assert res["notify_enqueued"] == 1
    pl = _outbox_payloads(led)[0]
    assert pl.get("digest") is True and not pl.get("urgent")


def test_self_sets_explicit_empty_is_not_unset(led):
    """signals.self_organizations=[] means 'no self org' — distinct
    from unset, which would fall back to the fetched profile."""
    mcs_signals.record_self_profile(
        led.db, {"sender_id": 1, "name": "n",
                 "professions": ["薬剤師"], "organizations": ["orgX"]})
    orgs, profs, _ = mcs_signals._self_sets(
        {"self_organizations": []}, led.db)
    assert orgs == [] and profs == ["薬剤師"]


def test_self_sets_empty_profile_professions_authoritative(led):
    """A fetched profile with no specialist categories means 'no
    listed profession' — the 薬剤師 default only applies when NO
    profile exists."""
    mcs_signals.record_self_profile(
        led.db, {"sender_id": 1, "name": "n",
                 "professions": [], "organizations": ["orgX"]})
    orgs, profs, _ = mcs_signals._self_sets({}, led.db)
    assert orgs == ["orgX"] and profs == []


def test_held_digest_members_salvaged(led):
    """A quarantined digest intent must not strand its member keys —
    still-open members fold into a fresh scheduled digest."""
    import notify_flush
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _ev(led, cfg={"signals": {"notify": True}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    assert json.loads(ev["payload"])["digest"] is True
    # quarantine with NO send receipt — nothing was ever delivered
    notify_flush._hold_event(
        led, {"event_id": ev["event_id"], "kind": "signal",
              "payload": ev["payload"],
              "progress": ev["progress"]}, {"signals": {}})
    rows = led.db.execute(
        "SELECT state,payload,next_try FROM notify_outbox "
        "ORDER BY event_id").fetchall()
    assert len(rows) == 2
    # quarantine = failed state with no retry timer
    assert rows[0]["state"] == "failed" and rows[0]["next_try"] is None
    pl = json.loads(rows[1]["payload"])
    assert rows[1]["state"] == "pending" and rows[1]["next_try"] > NOW
    assert pl["digest"] is True
    assert pl["signal_keys"] == ["med_change_no_followup:1:薬A"]


def test_held_digest_with_send_progress_not_salvaged(led):
    """A digest that may have partially delivered must NOT respawn —
    duplication risk outweighs the stranded-member fix."""
    import notify_flush
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _ev(led, cfg={"signals": {"notify": True}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    led.outbox_progress(ev["event_id"], 0, [], "fp", sending=1)
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    notify_flush._hold_event(
        led, {"event_id": ev["event_id"], "kind": "signal",
              "payload": ev["payload"],
              "progress": ev["progress"]}, {"signals": {}})
    assert led.db.execute(
        "SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 1


def test_signal_unit_text_renders_every_member(led):
    """A unit that cannot merge must render each member — never
    silently collapse to the first signal."""
    import notify_flush
    _msg(led.db, 1, ts=NOW - 1 * DAY)
    _msg(led.db, 2, ts=NOW - 1 * DAY, chash="h2")
    s1 = {"type": "adherence_concern", "project_id": 1,
          "evidence": {"message_ids": [1]}, "note": "note-one"}
    s2 = {"type": "adherence_concern", "project_id": 1,
          "evidence": {"message_ids": [2]}, "note": "note-two"}
    text = notify_flush._signal_unit_text(led, [s1, s2])
    assert "note-one" in text and "note-two" in text


def test_digest_render_drops_archived_member(led, monkeypatch):
    """A digest member whose patient was archived after enqueue must
    not render — digest intents carry project_id=None and bypass
    flush's per-event archived gate."""
    import notify_flush
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, pid=2, ts=NOW - 30 * DAY, chash="h2")
    _extract_llm(led.db, 2, "h2", [{"name": "薬B", "action": "stop"}])
    _ev(led, cfg={"signals": {"notify": True}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    assert len(json.loads(ev["payload"])["signal_keys"]) == 2
    led.db.execute("UPDATE patients SET is_archived=1 "
                   "WHERE project_id=2")
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: {"signals": {"notify": True}})
    text, _ = notify_flush._format_event(led, ev)
    assert "薬A" in text and "薬B" not in text
    assert "ダイジェスト（1件）" in text


def test_held_merged_intent_members_salvaged(led):
    """A quarantined NON-digest merged med_change intent must also
    rescue its members — they strand identically otherwise. Rescue
    preserves the non-digest shape (immediate, no 24h delay)."""
    import notify_flush
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1",
                 [{"name": "薬A", "action": "stop"},
                  {"name": "薬B", "action": "stop"}])
    _ev(led, cfg={"signals": {"notify": True, "tiers":
                              {"med_change_no_followup": "immediate"}}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    pl = json.loads(ev["payload"])
    assert len(pl["signal_keys"]) == 2 and "digest" not in pl
    notify_flush._hold_event(
        led, {"event_id": ev["event_id"], "kind": "signal",
              "payload": ev["payload"],
              "progress": ev["progress"]}, {"signals": {}})
    rows = led.db.execute(
        "SELECT state,payload,next_try FROM notify_outbox "
        "ORDER BY event_id").fetchall()
    assert len(rows) == 2
    assert rows[0]["state"] == "failed" and rows[0]["next_try"] is None
    new = json.loads(rows[1]["payload"])
    # immediate re-enqueue (not a digest delay) carrying every open key
    assert "digest" not in new and rows[1]["next_try"] <= time.time()
    assert set(new["signal_keys"]) == set(pl["signal_keys"])


def test_held_single_signal_key_salvaged(led):
    """A quarantined single signal_key intent re-enqueues its key —
    a legacy-shape payload is rescued via signal_key, not signal_keys."""
    import notify_flush
    _req(led.db, "open", created=NOW - 400 * DAY)
    led.db.execute("UPDATE requests SET status='open'")
    _ev(led, cfg={"signals": {"notify": True, "tiers":
                              {"request_aging": "immediate"}}})
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    pl = json.loads(ev["payload"])
    assert "signal_key" in pl
    notify_flush._hold_event(
        led, {"event_id": ev["event_id"], "kind": "signal",
              "payload": ev["payload"],
              "progress": ev["progress"]}, {"signals": {}})
    rows = led.db.execute(
        "SELECT payload FROM notify_outbox ORDER BY event_id").fetchall()
    assert len(rows) == 2
    new = json.loads(rows[1]["payload"])
    assert new["signal_key"] == pl["signal_key"]
