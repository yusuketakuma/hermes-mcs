"""Synthetic mcs semantic notices contracts with isolated shared fixtures."""
import semantic_send_gate
import semantic_drain
import json
import time
from types import SimpleNamespace
import pytest
import ledger
import mcs_view
import semantic
from semantic_testkit import BODY, _FakeJev, _cfg, _ledger, _llm, _message, _patient, _seeded
from mcs_semantic_testkit import _cfg_path as _cfg_path


def test_enforce_enqueues_notice_per_target(tmp_path):
    """A PASS generation covering two posts must emit one notice PER
    audited target — a shared (root, fp) delivery key used to dedup
    every later target's claims out of the notice entirely, while the
    header still claimed them."""
    db = _seeded(tmp_path)
    res = {"errors": []}
    out = semantic_drain.run_due(db, _cfg("enforce"), res,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    rows = db.db.execute("SELECT payload FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchall()
    assert len(rows) == 2
    pls = [json.loads(r["payload"]) for r in rows]
    assert {p["target_message_id"] for p in pls} == {1, 2}
    assert len({p["delivery_key"] for p in pls}) == 2
    for p in pls:
        # multi-target notices name which covered post they report on
        assert "要約対象" in p["text"]
        assert "要約" in p["text"] and "medical-care.net" in p["text"]
    # replay same generation -> deduped delivery keys, no second intents
    db.semantic_seed(1, [1], {"origin": "replay"})
    semantic_drain.run_due(db, _cfg("enforce"), res, time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    assert db.db.execute("SELECT count(*) FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchone()[0] == 2
    db.close()


def test_degraded_notice_enforce_only(tmp_path):
    db = _seeded(tmp_path)
    # age the notify intent past the delay, leave the job unprocessed
    db.db.execute("UPDATE notify_outbox SET created_at=?",
                  (time.time() - 3600,))
    db.db.commit()
    res = {"errors": []}
    semantic_drain.run_due(db, _cfg("enforce", delayed_notice_seconds=900,
                            budget=0),
                           res, time.monotonic() + 60,
                           jev_client=None, llm_fn=lambda _prompt: None)
    rows = db.db.execute("SELECT payload FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchall()
    assert len(rows) == 1
    pl = json.loads(rows[0]["payload"])
    assert pl["degraded"] is True and "原文" in pl["text"]
    # second drain -> same delivery_key -> no duplicate
    semantic_drain.run_due(db, _cfg("enforce", delayed_notice_seconds=900,
                            budget=0),
                     res, time.monotonic() + 60, jev_client=None,
                     llm_fn=lambda _prompt: None)
    assert db.db.execute("SELECT count(*) FROM notify_outbox "
                         "WHERE kind='semantic_notice'").fetchone()[0] == 1
    # shadow mode must never enqueue (AT-049)
    db2_path = tmp_path / "b.db"
    db2 = ledger.Ledger(str(db2_path))
    p2 = SimpleNamespace(project_id=1, project_type="medical",
                         patient_name="t", disease="", station_name="",
                         url="", fetch_state="complete",
                         fetch_reason=None,
                         messages=[_message(9)])
    db2.save_patient(p2, notify={"source": "unread"}, semantic=True)
    db2.db.execute("UPDATE notify_outbox SET created_at=?",
                   (time.time() - 3600,))
    db2.db.commit()
    semantic_drain.run_due(db2, _cfg("shadow", delayed_notice_seconds=60),
                     {"errors": []}, time.monotonic() + 60,
                     jev_client=_FakeJev(), llm_fn=_llm)
    assert db2.db.execute("SELECT count(*) FROM notify_outbox "
                          "WHERE kind='semantic_notice'").fetchone()[0] == 0
    db.close()
    db2.close()


def test_notifier_semantic_notice_render(tmp_path, monkeypatch):
    """The sender renders the frozen payload text — no re-derivation,
    no patient lookups at send time (INV-14). The send-time gates
    (enforce mode + live fingerprint) pass on the live generation."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    ev = db.db.execute("SELECT * FROM notify_outbox "
                       "WHERE kind='semantic_notice'").fetchone()
    assert ev is not None
    frozen = json.loads(ev["payload"])["text"]
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: _cfg("enforce"))
    content, files = notify_flush._format_event(db, ev)
    assert content == frozen and files == []
    db.close()


def test_snapshot_view_semantic(tmp_path):
    """Artifacts land in the published snapshot and mcs_view exposes
    them read-only — same reader gate as every other table (AT-065)."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("shadow"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"),
                                   str(tmp_path / "snaps"))
    view = mcs_view.View(snap)
    res = view.read("semantic", project=1, message_id=1)
    assert res["semantic"]["semantic_summary"]
    assert res["semantic"]["semantic_audit"][0]["meta"][
        "audit_status"] == "PASS"
    db.close()


def test_replay_heals_missing_notify_intent(tmp_path):
    """§18.3 regression: a run that committed summary+audit but lost
    the plan/outbox rows (the old crash window) must recreate both on
    the next run instead of marking done with the intent gone."""
    db = _seeded(tmp_path)
    res = {"errors": []}
    out = semantic_drain.run_due(db, _cfg("enforce"), res,
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    # simulate the pre-fix crash state
    db.db.execute("DELETE FROM artifacts WHERE kind='notify_plan'")
    db.db.execute("DELETE FROM notify_outbox "
                  "WHERE kind='semantic_notice'")
    db.db.commit()
    db.semantic_seed(1, [1], {"origin": "replay"})
    out2 = semantic_drain.run_due(db, _cfg("enforce"), res,
                            time.monotonic() + 300,
                            jev_client=_FakeJev(), llm_fn=_llm)
    assert out2["done"] == 1
    assert db.artifacts("notify_plan", message_id=1)
    assert db.db.execute(
        "SELECT 1 FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()
    db.close()


def test_off_mode_parks_queued_notice(tmp_path, monkeypatch):
    """§22.1: an enforce notice committed before an OFF flip must never
    send — the intent stays queued for a later enforce flush."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    n = db.db.execute("SELECT COUNT(*) c FROM notify_outbox "
                      "WHERE kind='semantic_notice'").fetchone()["c"]
    assert n == 2          # one queued intent per audited target
    import notify_flush
    import notify_sender
    import mcs_util
    monkeypatch.setattr(
        notify_flush, "_config",
        lambda: {"semantic": {"mode": "off"},
                 "notify_target": "slack"})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda *a: "/bin/sh")
    calls = []
    monkeypatch.setattr(notify_sender, 'send',
                        lambda *a, **k: calls.append(a) or None)
    base = db.db.execute("SELECT * FROM notify_outbox WHERE kind='new_messages'").fetchone()
    expected, files = notify_flush._format_event(db, base)
    frozen = [json.loads(row["payload"])["text"] for row in db.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='semantic_notice'").fetchall()]
    assert len(frozen) == 2 and all("要約：自動検査完了" in text for text in frozen)
    notify_flush.flush(db)
    # Exactly the current base notification is emitted; the ordinary 📋
    # summary heading does not imply an audited semantic notice was sent.
    assert [call[1] for call in calls] == [notify_sender._compose_body(expected, files)]
    assert calls[0][1].startswith("[MCS unread] 新着 2 件")
    assert all(text not in calls[0][1] for text in frozen)
    assert "要約：自動検査完了" not in calls[0][1]
    assert "カロナールを300mg×3回に変更する記載がある" not in calls[0][1]
    rows = db.db.execute(
        "SELECT state FROM notify_outbox WHERE kind='semantic_notice' ORDER BY event_id"
    ).fetchall()
    assert [row["state"] for row in rows] == ["pending", "pending"]
    assert db.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (base["event_id"],)).fetchone()[0] == "accepted"
    db.close()


def test_stale_generation_notice_suppressed(tmp_path, monkeypatch):
    """§18.4: a PASS notice enqueued for fingerprint fp must suppress
    terminally once the thread's live fingerprint has moved on."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    p = _patient(db)
    p.messages = [_message(1, body=BODY + "（編集）", unread=False)]
    db.save_patient(p)                 # fingerprint moves
    import notify_flush
    import notify_sender
    import mcs_util
    monkeypatch.setattr(
        notify_flush, "_config",
        lambda: {**_cfg("enforce"),
                 "notify_target": "slack"})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda *a: "/bin/sh")
    calls = []
    monkeypatch.setattr(notify_sender, 'send',
                        lambda *a, **k: calls.append(a) or None)
    base = db.db.execute("SELECT * FROM notify_outbox WHERE kind='new_messages'").fetchone()
    expected, files = notify_flush._format_event(db, base)
    frozen = [json.loads(row["payload"])["text"] for row in db.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='semantic_notice'").fetchall()]
    assert len(frozen) == 2 and all("要約：自動検査完了" in text for text in frozen)
    notify_flush.flush(db)
    assert [call[1] for call in calls] == [notify_sender._compose_body(expected, files)]
    assert calls[0][1].startswith("[MCS unread] 新着 2 件")
    assert all(text not in calls[0][1] for text in frozen)
    assert "要約：自動検査完了" not in calls[0][1]
    assert "カロナールを300mg×3回に変更する記載がある" not in calls[0][1]
    rows = db.db.execute(
        "SELECT state FROM notify_outbox WHERE kind='semantic_notice' ORDER BY event_id"
    ).fetchall()
    assert [row["state"] for row in rows] == ["suppressed", "suppressed"]
    assert db.db.execute("SELECT state FROM notify_outbox WHERE event_id=?", (base["event_id"],)).fetchone()[0] == "accepted"
    db.close()


def test_replay_of_imported_message_no_notice(tmp_path):
    """AT-055: replaying a thread that was never in a new_messages
    intent re-evaluates but cannot create notification eligibility."""
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(50)], project_id=1, semantic=False)
    semantic.seed(db, 50, cfg_path=_cfg_path(tmp_path, "enforce"))
    out = semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                           time.monotonic() + 300,
                           jev_client=_FakeJev(), llm_fn=_llm)
    assert out["done"] == 1
    assert db.db.execute("SELECT COUNT(*) c FROM notify_outbox"
                         ).fetchone()["c"] == 0
    db.close()


def test_notice_suppressed_when_src_event_suppressed(tmp_path,
                                                   monkeypatch):
    """Send-time gate: if the origin arrival intent was suppressed
    (e.g. archive), the semantic follow-up must not deliver."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    ev = db.db.execute("SELECT * FROM notify_outbox "
                       "WHERE kind='semantic_notice'").fetchone()
    src = json.loads(ev["payload"])["src_event_id"]
    db.outbox_suppress(src)
    import notify_flush
    monkeypatch.setattr(notify_flush, "_config",
                        lambda: _cfg("enforce"))
    with pytest.raises(semantic_send_gate.StaleSend):
        notify_flush._format_event(db, ev)
    db.close()


def test_parked_notice_not_counted_skipped(tmp_path, monkeypatch):
    """A policy-parked intent is not 'skipped' — otherwise run_check's
    notify_incomplete fires on every hourly re-check while the mode
    gate is down."""
    db = _seeded(tmp_path)
    semantic_drain.run_due(db, _cfg("enforce"), {"errors": []},
                     time.monotonic() + 300,
                     jev_client=_FakeJev(), llm_fn=_llm)
    import notify_flush
    import notify_sender
    import mcs_util
    monkeypatch.setattr(
        notify_flush, "_config",
        lambda: {"semantic": {"mode": "shadow"},
                 "notify_target": "slack"})
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_sender, 'send', lambda *a, **k: None)
    res = notify_flush.flush(db)
    assert res["parked"] == 2 and res["skipped"] == 0
    db.close()
