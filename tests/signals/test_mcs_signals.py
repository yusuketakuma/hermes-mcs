"""Validate signal detectors, lifecycle, policy and malformed evidence."""
import json
import time
import pytest
import mcs_signals
from semantic_projection import PROJECTION_VERSION
from inference_version import CURRENT_VERSION, LEGACY_VERSION, VERSION_KEY
from ops_testkit import DAY, NOW, _extract_v1, _msg
import signal_policy
from signal_detector_testkit import (
    _extract_llm as _extract_llm,
    _extract_doc as _extract_doc,
    _req as _req,
    _states as _states,
    _ev as _ev,
    _projection as _projection,
    _dismiss as _dismiss,
    _policy as _policy,
    _req_item as _req_item,
    _extract_llm_meds as _extract_llm_meds,
    _open_types as _open_types,
    led as led,
    _MISSING as _MISSING,
)


@pytest.mark.parametrize("raw", ["{broken", "[" * 1500 + "0" + "]" * 1500,
                                 "[]", "null"], ids=["malformed", "deep", "array", "null"])
def test_corrupt_latest_policy_never_revives_prior_approval(led, raw):
    assert _policy(led, {"req_age_days": 7})["outcome"] == "applied"
    _req(led.db, "open", created=NOW - 20 * DAY)
    led.artifact_add(signal_policy.POLICY_KIND, raw)
    before = led.db.total_changes
    current = mcs_signals.current_open(led.db)
    assert current["policy"] is None
    assert current["thresholds"]["req_age_days"] == signal_policy.REQ_AGE_DAYS
    assert led.db.total_changes == before
    assert _ev(led)["open"] == 0
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0



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
    assert "対応の有無を示すものではありません" not in sig["note"]



@pytest.mark.parametrize("case", ["current", "missing", "legacy", "null",
                                  "malformed_meta", "malformed_content",
                                  "stale_hash", "deleted"])
def test_signal_extraction_generation_and_source_boundaries(led, case):
    _msg(led.db, 1, ts=NOW - 30 * DAY, body="本人の合成薬Aを中止しました。")
    _extract_doc(led.db, 1, "h1", meds=[{"name": "合成薬A", "action": "stop"}])
    if case in {"missing", "legacy", "null", "malformed_meta", "stale_hash"}:
        meta = {"hash": "h1", VERSION_KEY: CURRENT_VERSION}
        if case == "missing":
            del meta[VERSION_KEY]
        elif case == "legacy":
            meta[VERSION_KEY] = LEGACY_VERSION
        elif case == "null":
            meta[VERSION_KEY] = None
        elif case == "stale_hash":
            meta["hash"] = "old-hash"
        raw = "{bad" if case == "malformed_meta" else json.dumps(meta)
        led.db.execute("UPDATE artifacts SET meta=? WHERE kind='extract_llm' "
                       "AND message_id=1", (raw,))
    elif case == "malformed_content":
        led.db.execute("UPDATE artifacts SET content=? WHERE kind='extract_llm' "
                       "AND message_id=1", ("{bad",))
    elif case == "deleted":
        led.db.execute("UPDATE messages SET body_state='deleted', body_text='' "
                       "WHERE message_id=1")
    res = _ev(led)
    assert res["errors"] == []
    meds = [s for s in mcs_signals.current_open(led.db)["items"]
            if s["type"] == "med_change_no_followup"]
    assert [(s["project_id"], s["evidence"]["med"]) for s in meds] == (
        [(1, "合成薬A")] if case == "current" else [])



def test_current_signal_fact_is_scoped_to_its_own_patient(led):
    _msg(led.db, 1, pid=1, ts=NOW - 30 * DAY, body="通常の連絡")
    _msg(led.db, 2, pid=2, ts=NOW - 30 * DAY, chash="h2",
         body="本人の合成薬Bを中止しました。")
    _extract_doc(led.db, 2, "h2", meds=[{"name": "合成薬B", "action": "stop"}])
    assert _ev(led)["errors"] == []
    meds = [s for s in mcs_signals.current_open(led.db)["items"]
            if s["type"] == "med_change_no_followup"]
    assert [(s["project_id"], s["evidence"]["med"]) for s in meds] == [
        (2, "合成薬B")]



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



def test_med_change_deleted_later_post_is_not_followup(led):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    _msg(led.db, 2, ts=NOW - 29 * DAY)
    led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=2")
    assert _ev(led)["open"] == 1



def test_med_change_recent_mention_not_yet_candidate(led):
    _msg(led.db, 1, ts=NOW - 2 * DAY)    # window hasn't elapsed
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "start"}])
    assert _ev(led)["open"] == 0



def test_med_none_action_not_flagged(led):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "none"}])
    assert _ev(led)["open"] == 0



def test_comm_concentration(led):
    for i in range(signal_policy.CONC_MIN_POSTS):
        _msg(led.db, 100 + i, ts=NOW - 3600)
    assert _ev(led)["open"] == 1
    sig = mcs_signals.current_open(led.db)["items"][0]
    assert sig["type"] == "comm_concentration"
    assert "重症度ではありません" not in sig["note"]



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



def test_med_change_detected_from_canonical_projection(led):
    """T5: a canonical fact reaching the signal layer — the detector
    reads canonical_projection through the same current_fact_pred rule
    as every other consumer."""
    _msg(led.db, 11, ts=NOW - 30 * DAY)
    _projection(led.db, 11, "h1", {
        "meds": [{"name": "薬A", "action": "stop", "subject": "patient"}],
        "canonical_facts": [
            {"fact_id": "f1", "kind": "medication_event",
             "statement": "薬A中止", "subject": "patient:1",
             "evidence_quote": "中止"}]})
    res = _ev(led)
    assert res["open"] == 1
    sig = mcs_signals.current_open(led.db)["items"][0]
    assert sig["type"] == "med_change_no_followup"
    assert sig["evidence"]["med"] == "薬A"
    assert sig["project_id"] == 1
    assert sig["evidence"]["message_ids"] == [11]



def test_canonical_projection_shadows_extract_llm_in_signals(led):
    """T5: one fact source per message — a current projection owns the
    read; the shadowed extract_llm row never double-counts."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "旧薬B", "action": "stop"}])
    _projection(led.db, 1, "h1", {
        "meds": [{"name": "薬A", "action": "stop"}]})
    _ev(led)
    sigs = mcs_signals.current_open(led.db)["items"]
    meds = [s["evidence"]["med"]
            for s in sigs if s["type"] == "med_change_no_followup"]
    assert meds == ["薬A"]        # 旧薬B shadowed, never signalled



def test_stale_projection_falls_back_to_extract_llm_in_signals(led):
    """T5 failure axis: source revision changed -> stale projection
    drops out; the current extract_llm drives the detector."""
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _projection(led.db, 1, "oldhash",
                {"meds": [{"name": "陳腐薬", "action": "stop"}]})
    _extract_llm(led.db, 1, "h1", [{"name": "現行薬", "action": "stop"}])
    _ev(led)
    meds = [s["evidence"]["med"]
            for s in mcs_signals.current_open(led.db)["items"]
            if s["type"] == "med_change_no_followup"]
    assert meds == ["現行薬"]



@pytest.mark.parametrize("version", ["missing", LEGACY_VERSION, None])
def test_old_projection_does_not_shadow_current_extract_in_signals(led, version):
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "現行薬", "action": "stop"}])
    _projection(led.db, 1, "h1", {"meds": [{"name": "旧薬", "action": "stop"}]})
    meta = {"hash": "h1", "projection_version": PROJECTION_VERSION}
    if version != "missing":
        meta[VERSION_KEY] = version
    led.db.execute("UPDATE artifacts SET meta=? WHERE kind='canonical_projection' "
                   "AND message_id=1", (json.dumps(meta),))
    assert _ev(led)["errors"] == []
    meds = [s["evidence"]["med"] for s in mcs_signals.current_open(led.db)["items"]
            if s["type"] == "med_change_no_followup"]
    assert meds == ["現行薬"]



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
                [{"start": "2026-09-01", "end": "2026-09-24",
                  "raw": "9/1-9/24"},
                 {"start": "2026-09-01", "end": "2026-09-25",
                  "raw": "9/1-9/25"},
                 {"start": "2020-01-01", "end": "2020-02-01",
                  "raw": "old"}])
    res = _ev(led)
    assert res["open"] == 1
    sig = mcs_signals.current_open(led.db)["items"][0]
    assert sig["type"] == "rx_period_expiry"
    assert sig["context"]["days_left"] == 3     # 4日先はまだ通知しない
    assert "確定ではありません" in sig["note"]



def test_rx_period_lapsed(led):
    """furthest-out period end has passed and nothing renews it —
    '内服切れ'の可能性として人が原記録を確認する候補。"""
    _msg(led.db, 1)
    _msg(led.db, 2, pid=2)
    _extract_v1(led.db, 1, "h1",
                [{"start": "2026-09-01", "end": "2026-09-20",
                  "raw": "9/1-9/20"}])
    _extract_v1(led.db, 2, "h2",
                [{"start": "2026-09-01", "end": "2026-09-28",
                  "raw": "9/1-9/28"}])
    res = _ev(led)
    assert res["open"] == 1
    sig = mcs_signals.current_open(led.db)["items"][0]
    assert sig["type"] == "rx_period_lapsed"
    assert sig["project_id"] == 1
    assert sig["context"]["days_since_end"] == 1
    assert "原記録で確認" in sig["note"]



def test_rx_period_lapsed_window_bound(led):
    """終了から rx_lapsed_days（既定14日）を超える沈黙は記録様式の
    問題であり、アラートにしない。"""
    _msg(led.db, 1)
    _extract_v1(led.db, 1, "h1",
                [{"start": "2026-08-01", "end": "2026-09-05",
                  "raw": "8/1-9/5"}])
    res = _ev(led)
    assert res["open"] == 0



@pytest.mark.parametrize("end,raw,stype", [
    ("2026-09-24", "9/1-9/24", "rx_period_expiry"),
    ("2026-09-20", "9/1-9/20", "rx_period_lapsed")])
def test_rx_period_skips_archived_room_and_resolves(led, end, raw, stype):
    """アーカイブ済みの部屋は期間表現シグナルの候補にならず、既存の
    open は project_archived として解消される。"""
    _msg(led.db, 1)
    _extract_v1(led.db, 1, "h1",
                [{"start": "2026-09-01", "end": end, "raw": raw}])
    assert _ev(led)["open"] == 1
    assert mcs_signals.current_open(led.db)["items"][0]["type"] == stype
    led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
    res = _ev(led, now=NOW + 1)
    assert (res["open"], res["resolved"]) == (0, 1)
    assert mcs_signals.current_open(led.db)["items"] == []
    row = next(c for c in mcs_signals.latest_signal_states(led.db).values()
               if c["type"] == stype)
    assert row["resolution"]["cause"] == "project_archived"



def test_lifecycle_is_append_only(led):
    """open -> resolved -> open produces three rows, history intact."""
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    assert _states(led.db)["request_overdue:1:1"] == "open"
    led.db.execute("UPDATE requests SET status='done' WHERE request_id=1")
    _ev(led)
    assert _states(led.db)["request_overdue:1:1"] == "resolved"
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



def test_dismiss_reason_code_is_optional_and_counted(led):
    import mcs_requests
    from uuid import uuid4
    _req(led.db, "open", due="2026-09-10")
    _req(led.db, "open", due="2026-09-11")
    _ev(led)
    base = {"cmd": "ops.signal_dismiss", "version": 1, "actor": "t",
            "human_confirmed": True, "project_id": 1, "reason": "誤検知"}
    bad = mcs_requests.apply_command(led, {
        **base, "command_id": str(uuid4()),
        "signal_key": "request_overdue:1:1", "reason_code": "wrong"})
    assert bad["error"] == "bad_reason_code"
    ok = mcs_requests.apply_command(led, {
        **base, "command_id": str(uuid4()),
        "signal_key": "request_overdue:1:1",
        "reason_code": "false_positive"})
    assert ok["outcome"] == "applied"
    # an old command without reason_code keeps working
    assert _dismiss(led, "request_overdue:1:2")["outcome"] == "applied"
    rows = [json.loads(x[0]) for x in led.db.execute(
        "SELECT content FROM artifacts WHERE kind='signal_v1' "
        "AND json_extract(content,'$.state')='dismissed' ORDER BY artifact_id")]
    assert [r.get("dismiss_reason_code") for r in rows] == [
        "false_positive", None]
    assert mcs_signals.dismiss_reason_counts(led.db) == {
        "request_overdue": {"false_positive": 1, "unclassified": 1}}



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



def test_policy_empty_window_is_clamped_and_reported(led):
    """followup_days past followup_max_age_d would leave no mention old enough yet recent
    enough — an empty window must not read as 'no signals'."""
    assert _policy(led, {"followup_days": 30, "followup_max_age_d": 7,
                         "request_response_days": 10, "fyi_max_age_d": 7})["outcome"] == "applied"
    _msg(led.db, 1, ts=NOW - 30 * DAY - 3600)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    res = _ev(led)
    codes = ["threshold_window_clamped:followup_max_age_d", "threshold_window_clamped:fyi_max_age_d"]
    assert res["warnings"] == codes
    assert _states(led.db)["med_change_no_followup:1:薬A"] == "open"
    current = mcs_signals.current_open(led.db)
    assert current["warnings"] == codes
    assert current["thresholds"]["followup_max_age_d"] == 31 and current["thresholds"]["fyi_max_age_d"] == 11



def test_default_policy_has_no_window_warning(led):
    assert _ev(led)["warnings"] == [] and mcs_signals.current_open(led.db)["warnings"] == []



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
    assert res["detectors_deferred"] == [name for name, _ in mcs_signals.DETECTORS]
    assert res["errors"] == []
    assert _states(led.db)["request_overdue:1:1"] == "open"
    assert _states(led.db)["med_change_no_followup:1:薬A"] == "open"



def test_partial_deadline_reports_only_unrun_types_and_keeps_their_state(led, monkeypatch):
    _req(led.db, "open", due="2026-09-10", created=NOW - 60 * DAY)
    _ev(led)
    clock = [0]

    def finished(db, now, th, sig_cfg):
        clock[0] = 2
        return []

    def unrun(*args):
        pytest.fail("deadline-deferred detector was called")

    monkeypatch.setattr(mcs_signals.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mcs_signals, "DETECTORS", (
        ("request_overdue", finished), ("request_aging", unrun)))
    res = mcs_signals.evaluate(led, {}, now=NOW, deadline=2)

    assert res["detectors_ran"] == ["request_overdue"]
    assert res["detectors_deferred"] == ["request_aging"]
    assert res["errors"] == []
    assert _states(led.db)["request_overdue:1:1"] == "resolved"
    assert _states(led.db)["request_aging:1:1"] == "open"



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
    assert res["detectors_deferred"] == []
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



def test_policy_view_requires_str_provenance(led):
    """current_open reports the same policy _thresholds applies: a latest
    row with a truthy non-str command_id is not shown as active."""
    _policy(led, {"req_age_days": 7})
    assert mcs_signals.current_open(led.db)["policy"] is not None
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
        "VALUES ('signal_policy_v1',NULL,?,?,0)",
        (json.dumps({"command_id": 1, "actor": "x",
                     "policy": {"req_age_days": 9}}), "{}"))
    view = mcs_signals.current_open(led.db)
    assert view["policy"] is None
    assert view["thresholds"] == signal_policy.resolved_thresholds(led.db)
    assert view["thresholds"]["req_age_days"] == \
        signal_policy.THRESHOLDS["req_age_days"][0]



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
    assert "確認できませんでした" in ph[0]["note"] and "記録上の確認であり" not in ph[0]["note"]
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



@pytest.mark.parametrize("kind, fires", [
    ("self_plan", False), ("question", True), (None, True),
    ("request", True)])
def test_pharmacist_request_kind_guard(led, kind, fires):
    """#20 order 3: a pharmacist's own plan is not an unanswered request;
    a question addressed to the pharmacy is a consultation and still
    fires, as do kind absent (pre-#20 artifacts) and request."""
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("薬剤師", "残薬調整の確認", kind=kind)])
    _ev(led)
    ph = [s for s in mcs_signals.current_open(led.db)["items"]
          if s["type"] == "pharmacist_request_unanswered"]
    assert bool(ph) is fires



def test_rx_request_visibility_kind_guard(led):
    """#20 order 3: a self_plan about a drug is not a prescription in
    the pipeline; a question about one is still a consultation and
    fires, as does kind absent."""
    _msg(led.db, 1, ts=NOW - 1 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[_req_item("医師", "フロセミド処方は必要か",
                                     kind="question")])
    _msg(led.db, 2, ts=NOW - 1 * DAY, chash="h2")
    _extract_doc(led.db, 2, "h2",
                 requests=[_req_item("医師", "フロセミド処方")])
    _msg(led.db, 3, ts=NOW - 1 * DAY, chash="h3")
    _extract_doc(led.db, 3, "h3",
                 requests=[_req_item("看護師", "薬の残数確認",
                                     kind="self_plan")])
    _ev(led)
    rx = [s for s in mcs_signals.current_open(led.db)["items"]
          if s["type"] == "rx_request_visibility"]
    assert [s["evidence"]["message_ids"] for s in rx] == [[1], [2]]



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
    assert "関連を原記録で確認" in items[0]["note"]



def test_symptom_after_med_change_ignores_unverified_symptoms(led):
    """An unverified symptom is not evidence of a patient symptom: it
    alone never couples, and it is left out beside a verified one."""
    _msg(led.db, 1, ts=NOW - 1 * DAY)
    _extract_doc(led.db, 1, "h1",
                 meds=[{"name": "薬A", "action": "start"}],
                 symptoms=[{"text": "浮腫", "status": "new",
                            "negated": False, "unverified": True}])
    _msg(led.db, 2, pid=2, ts=NOW - 1 * DAY, chash="h2")
    _extract_doc(led.db, 2, "h2",
                 meds=[{"name": "薬B", "action": "stop"}],
                 symptoms=[{"text": "倦怠感", "status": "new",
                            "negated": False, "unverified": True},
                           {"text": "発疹", "status": "new",
                            "negated": False}])
    _ev(led)
    items = [s for s in mcs_signals.current_open(led.db)["items"]
             if s["type"] == "symptom_after_med_change"]
    assert [i["project_id"] for i in items] == [2]
    assert items[0]["evidence"]["symptoms"] == ["発疹"]



@pytest.mark.parametrize("flag,confirmed", [
    (_MISSING, True), (False, True),
    (True, False), (1, False), (0, False), (None, False),
    ("", False), ("true", False), ("false", False), (2, False)])
def test_unverified_flag_fails_closed(led, flag, confirmed):
    def item(**d):
        if flag is not _MISSING:
            d["unverified"] = flag
        return d
    # pharmacist_request_unanswered (SQL)
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[item(to="薬剤師", action="残薬調整の確認")])
    # rx_request_visibility (SQL)
    _msg(led.db, 2, pid=2, ts=NOW - 1 * DAY, chash="h2")
    _extract_doc(led.db, 2, "h2",
                 requests=[item(to="医師", action="フロセミド処方")])
    # symptom_after_med_change: flagged symptom beside a confirmed med,
    # then a confirmed symptom beside a flagged med (Python predicates)
    _msg(led.db, 3, pid=3, ts=NOW - 1 * DAY, chash="h3")
    _extract_doc(led.db, 3, "h3",
                 meds=[{"name": "薬A", "action": "start"}],
                 symptoms=[item(text="浮腫", status="new", negated=False)])
    _msg(led.db, 4, pid=4, ts=NOW - 1 * DAY, chash="h4")
    _extract_doc(led.db, 4, "h4",
                 meds=[item(name="薬B", action="start")],
                 symptoms=[{"text": "発疹", "status": "new",
                            "negated": False}])
    _ev(led)
    got = {(s["type"], s["project_id"])
           for s in mcs_signals.current_open(led.db)["items"]}
    want = {("pharmacist_request_unanswered", 1),
            ("rx_request_visibility", 2),
            ("symptom_after_med_change", 3),
            ("symptom_after_med_change", 4)}
    assert (got & want) == (want if confirmed else set())



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



@pytest.mark.parametrize("body,hit", [
    ("飲み忘れは特にない", False), ("残薬が全くない", False), ("飲み忘れはほとんどないとのこと", False),
    ("飲み忘れは今のところありません", False), ("飲み忘れもあまりない", False),
    ("飲み忘れが特に多い", True), ("飲み忘れはほとんど毎日ある", True), ("残薬が全く減っていない", True)])
def test_adherence_adverb_before_negation(body, hit):
    assert bool(mcs_signals._adherence_phrases(body)) is hit



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



@pytest.mark.parametrize(("body", "expected"), [
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



def test_malformed_extraction_members_do_not_disable_detectors(led):
    _msg(led.db, 1, ts=NOW - 10 * DAY)
    _extract_doc(led.db, 1, "h1",
                 meds=["broken", {"name": "合成薬", "action": "start"}],
                 requests=["broken", _req_item("薬剤師", "残薬確認")],
                 symptoms=3)
    _msg(led.db, 2, pid=2, ts=NOW - 10 * DAY)
    _extract_doc(led.db, 2, "h1", meds=3)
    _extract_v1(led.db, 2, "h1", 3)
    res = _ev(led)
    assert res["errors"] == []
    types = {s["type"] for s in mcs_signals.current_open(led.db)["items"]}
    assert {"med_change_no_followup", "pharmacist_request_unanswered"} <= types



@pytest.mark.parametrize("invalid", ["foreign_patient", "bad_date", "bad_body"])
def test_latest_invalid_signal_never_revives_previous_open(led, invalid):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    key = "request_overdue:1:1"
    row = led.db.execute("SELECT content FROM artifacts WHERE kind='signal_v1'").fetchone()
    content = json.loads(row[0])
    if invalid == "foreign_patient":
        content["project_id"] = 2
    elif invalid == "bad_date":
        content["detected_at"] = "broken"
    else:
        content = "broken"
    led.artifact_add("signal_v1", json.dumps(content), project_id=1,
                     meta={"key": key})
    assert mcs_signals.current_open(led.db, project_id=1)["items"] == []
    assert mcs_signals.open_signal_rows(led.db, [key]) == []
    before = led.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert _ev(led)["opened"] == 0
    assert led.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == before



def test_nonstring_signal_key_does_not_crash_evaluation(led):
    led.artifact_add("signal_v1", json.dumps({"state": "open"}),
                     project_id=1, meta={"key": ["broken"]})
    _req(led.db, "open", due="2026-09-10")
    assert _ev(led)["opened"] == 1
    assert mcs_signals.current_open(led.db)["total"] == 1



def test_dismissed_signal_resolves_when_clear_and_reopens_on_recurrence(led):
    """A dismissal covers one episode: once the condition clears the key
    resolves, and a later burst in the same room opens again."""
    key = "comm_concentration:1:current"
    for i in range(signal_policy.CONC_MIN_POSTS):
        _msg(led.db, 100 + i, ts=NOW - 3600)
    _ev(led)
    assert _dismiss(led, key)["outcome"] == "applied"
    later = NOW + 30 * DAY
    r = _ev(led, now=later)
    assert _states(led.db)[key] == "resolved" and r["resolved"] == 1
    for i in range(signal_policy.CONC_MIN_POSTS):
        _msg(led.db, 500 + i, ts=later - 3600)
    r = _ev(led, now=later)
    assert _states(led.db)[key] == "open" and r["opened"] == 1



def test_dismiss_checks_project_scope_before_revealing_row_ids(led):
    import mcs_requests
    from uuid import uuid4
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    req = {"cmd": "ops.signal_dismiss", "version": 1,
           "command_id": str(uuid4()), "actor": "tester",
           "human_confirmed": True, "project_id": 2,
           "signal_key": "request_overdue:1:1", "reason": "確認済み",
           "expected_signal_artifact_id": 999999}
    r = mcs_requests.apply_command(led, req)
    assert r["outcome"] == "rejected"
    assert "project_mismatch" in json.dumps(r)
    assert "current_artifact_id" not in json.dumps(r)



def test_unreadable_latest_signal_reports_unknown_without_reviving_history(led):
    _req(led.db, "open", due="2026-09-10")
    _ev(led)
    key = "request_overdue:1:1"
    led.artifact_add("signal_v1", "{broken", project_id=1, meta={"key": key})
    before = led.db.total_changes
    result = mcs_signals.current_open(led.db)
    assert result["total"] == 0 and result["items"] == []
    assert result["errors"] == ["signal_state_corrupt"]
    assert led.db.total_changes == before



def test_measured_empty_signal_list_has_no_read_errors(led):
    result = mcs_signals.current_open(led.db)
    assert result["total"] == 0 and result["errors"] == []



def test_eleven_mentions_keep_latest_in_evidence(led):
    for mid in range(1, 12):
        _msg(led.db, mid, ts=NOW - (89 - 8 * (mid - 1)) * DAY,
             chash=f"h{mid}")
        meds = [{"name": "薬A", "action": "change"}]
        if mid == 11:
            meds.append({"name": "薬B", "action": "start"})
        _extract_llm_meds(led.db, mid, f"h{mid}", meds)
    mcs_signals.evaluate(led, {}, now=NOW)
    sigs = {s["evidence"]["med"]: s
            for s in mcs_signals.current_open(led.db)["items"]
            if s["type"] == "med_change_no_followup"}
    a = sigs["薬A"]["evidence"]
    assert a["mention_count"] == 11
    assert a["message_ids"] == list(range(2, 12))
    assert (mcs_signals.med_group_key(sigs["薬A"])
            == mcs_signals.med_group_key(sigs["薬B"]) == (1, 11))



@pytest.mark.parametrize("flag,opens", [
    (None, True), (False, True), (True, False), (1, False), ("", False)])
def test_adherence_meds_respect_unverified(led, flag, opens):
    med = {"name": "薬A", "action": None, "negated": True}
    if flag is not None:
        med["unverified"] = flag
    _msg(led.db, 1, ts=NOW - DAY, body="記録のみ")
    _extract_doc(led.db, 1, "h1", meds=[med])
    _ev(led)
    assert ("adherence_concern" in _open_types(led.db)) is opens



def test_initial_history_classification_preserves_existing_signal_candidates(led):
    import initial_import_state
    _msg(led.db, 1, ts=NOW - 30 * DAY)
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    assert _ev(led)["open"] == 1
    before = mcs_signals.current_open(led.db)["items"]
    with led.db:
        initial_import_state.start_tx(led, 1, int(NOW), NOW, boundary_source="server")
        assert initial_import_state.classify_tx(led, 1, [1], NOW) == {1}
    assert _ev(led)["open"] == 1
    assert mcs_signals.current_open(led.db)["items"] == before
