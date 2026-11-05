"""Validate signal notification grouping, delivery, urgency and digest lifecycle."""
import semantic_send_gate
import json
import time
import pytest
import mcs_signals
from inference_version import CURRENT_VERSION, VERSION_KEY
from ops_testkit import DAY, NOW, _msg
from signal_detector_testkit import (
    _extract_llm as _extract_llm,
    _extract_doc as _extract_doc,
    _req as _req,
    _states as _states,
    _ev as _ev,
    _sig_ev as _sig_ev,
    _set_meds as _set_meds,
    _outbox_payloads as _outbox_payloads,
    led as led,
)


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
    assert "アラート" in content and "request_overdue" in content



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



def test_send_gate_flag_turned_off(led, monkeypatch):
    import notify_flush
    _req(led.db, "open", due="2026-09-10")
    _ev(led, cfg={"signals": {"notify": True}})
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    with pytest.raises(semantic_send_gate.StaleSend,
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
    with pytest.raises(semantic_send_gate.StaleSend, match="signal_not_open"):
        notify_flush._format_event(led, _sig_ev(led))



def test_send_gate_open_signal_formats(led, monkeypatch):
    import notify_flush
    _req(led.db, "open", due="2026-09-10")
    cfg = {"signals": {"notify": True}}
    _ev(led, cfg=cfg)
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    text, files = notify_flush._format_event(led, _sig_ev(led))
    assert "アラート" in text and files == []



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
    assert "対応の有無を示すものではありません" not in text
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
    with pytest.raises(semantic_send_gate.StaleSend, match="signal_not_open"):
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



def test_urgency_high_escalates_to_immediate(led, monkeypatch):
    """urgency:high on the source mention promotes a digest-tier
    signal to an immediate intent carrying the urgent flag."""
    _msg(led.db, 1, ts=NOW - 30 * DAY, body="本人の薬Aを中止。急変につき至急ご連絡ください。")
    _extract_doc(led.db, 1, "h1", urgency="high",
                 urgency_evidence=["急変につき至急ご連絡ください。"],
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
    import structured_view
    assert structured_view.URGENCY_LABEL_PLAIN["llm"] in text



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



def test_urgency_high_from_extract_v1_escalates(led):
    """extract_v1 (rule extractor) carries urgency too — a message
    the LLM never processed still escalates its signal."""
    _msg(led.db, 1, ts=NOW - 30 * DAY, body="本人の薬Aを中止。急変につき至急ご連絡ください。")
    _extract_llm(led.db, 1, "h1", [{"name": "薬A", "action": "stop"}])
    led.db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',?,?,?)",
        (1, json.dumps({"urgency": "high"}),
         json.dumps({"hash": "h1", VERSION_KEY: CURRENT_VERSION})))
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
        (kind, json.dumps({"urgency": "high"}),
         json.dumps({"hash": "old-body", VERSION_KEY: CURRENT_VERSION})))
    res = _ev(led, cfg={"signals": {"notify": True}})
    assert res["notify_enqueued"] == 1
    pl = _outbox_payloads(led)[0]
    assert pl.get("digest") is True and not pl.get("urgent")



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
    notify_flush.hold_event(
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
    notify_flush.hold_event(
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
    notify_flush.hold_event(
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
    notify_flush.hold_event(
        led, {"event_id": ev["event_id"], "kind": "signal",
              "payload": ev["payload"],
              "progress": ev["progress"]}, {"signals": {}})
    rows = led.db.execute(
        "SELECT payload FROM notify_outbox ORDER BY event_id").fetchall()
    assert len(rows) == 2
    new = json.loads(rows[1]["payload"])
    assert new["signal_key"] == pl["signal_key"]



def test_malformed_pending_digest_does_not_block_new_signal(led):
    led.outbox_add_tx("signal", None,
                      {"digest": True, "signal_keys": 7}, next_try=NOW + DAY)
    _req(led.db, "open", due="2026-09-10")
    result = _ev(led, cfg={"signals": {"notify": True}})
    assert result["notify_enqueued"] == 1
    assert _outbox_payloads(led)[-1]["signal_keys"] == ["request_overdue:1:1"]



@pytest.mark.parametrize("covered", [False, True])
def test_sql_valid_unreadable_pending_digest_does_not_block_new_signal(led, covered):
    key = "request_aging:1:1" if covered else "fictional-prior-key"
    raw = ('{"digest":true,"signal_keys":' + json.dumps([key]) + ','
           '"synthetic_unused":' + '9' * 5000 + '}')
    assert led.db.execute("SELECT json_valid(?)", (raw,)).fetchone()[0] == 1
    with pytest.raises(ValueError):
        json.loads(raw)
    old = led.outbox_add_tx("signal", None, {}, next_try=NOW + DAY)
    led.db.execute("UPDATE notify_outbox SET payload=? WHERE event_id=?", (raw, old))
    _req(led.db, "open", created=NOW - 40 * DAY)
    result = _ev(led, cfg={"signals": {"notify": True}})
    assert result["notify_enqueued"] == (0 if covered else 1) and result["opened"] == 1
    assert led.db.execute("SELECT payload FROM notify_outbox WHERE event_id=?", (old,)).fetchone()[0] == raw
    assert led.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == (1 if covered else 2)
    if not covered:
        latest = led.db.execute("SELECT payload FROM notify_outbox ORDER BY event_id DESC LIMIT 1").fetchone()[0]
        assert json.loads(latest)["signal_keys"] == ["request_aging:1:1"]
    assert mcs_signals.current_open(led.db)["total"] == 1



def test_rx_period_expiry_high_urgency_escalates(led):
    _msg(led.db, 1, body="本人の内服期間9/1-9/24。急変につき至急ご連絡ください。")
    led.db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',1,?,?)",
        (json.dumps({"urgency": "high", "med_periods": [
            {"start": "2026-09-01", "end": "2026-09-24",
             "raw": "9/1-9/24"}]}),
         json.dumps({"hash": "h1", VERSION_KEY: CURRENT_VERSION})))
    mcs_signals.evaluate(led, {"signals": {"notify": True}}, now=NOW)
    items = mcs_signals.current_open(led.db)["items"]
    assert [s["type"] for s in items] == ["rx_period_expiry"]
    pls = [json.loads(r[0]) for r in led.db.execute(
        "SELECT payload FROM notify_outbox ORDER BY event_id")]
    assert pls and all(p.get("urgent") for p in pls)
    assert not any(p.get("digest") for p in pls)
