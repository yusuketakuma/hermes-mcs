"""Semantic delivery gates and current-thread rendering (AT-060/061/063)."""
import json
from types import SimpleNamespace

import mcs_adapter
import notifier
import semantic
import ledger
from test_mcs_semantic import _cfg


BODY = "原文の本文です。"


def _message(mid=1, parent=None, body=BODY, unread=True):
    return mcs_adapter.Message(
        message_id=mid, project_id=1, parent_id=parent, sender_id=1,
        sender_name="sender", sender_type="user", profession="",
        organization="", posted_at="2026-09-19T00:00:00+09:00",
        body_html=f"<p>{body}</p>", body_state="full",
        is_unread=unread, reply_count=0)


def _patient(messages=()):
    return SimpleNamespace(
        project_id=1, project_type="medical", patient_name="患者",
        disease="", station_name="",
        url="https://www.medical-care.net/projects/medical/1",
        fetch_state="complete", fetch_reason=None, messages=list(messages))


def _db(tmp_path, messages=()):
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    db.save_patient(_patient(messages), notify={"source": "unread"})
    return db


def _summary(db, claims=1):
    bundle = semantic.thread_bundle(db, 1, 1)
    content = {"claims": [
        {"text": f"claim-{i}-" + "x" * 220, "section": "status",
         "claim_kind": "reported_fact"}
        for i in range(claims)
    ], "limitations": ["制約-" + "y" * 120]}
    db.artifact_add(
        "semantic_summary", json.dumps(content, ensure_ascii=False),
        project_id=1, message_id=1, model="Qwen3.5-9B",
        meta={"fingerprint": bundle["source_fingerprint"],
              "policy_fingerprint": semantic.policy_fingerprint(semantic.semantic_config(_cfg("enforce"))[0]),
              "audit_status": "PASS",
              "publication_mode": "enforce",
              "target_revision": bundle["members"][0]["revision"]})


def _notice_text(body):
    return (
        "【患者】\n"
        "対象新着：1投稿｜対象投稿の最終時刻：2026/09/19 00:00 JST\n"
        "取得：完全\n"
        "要約：自動検査完了\n\n"
        "■ 今回の重要情報\n"
        f"{body}\n\n"
        "▶ MCSで確認\n"
        "https://www.medical-care.net/projects/medical/1"
    )


def _semantic_event(db, text="notice"):
    src = db.outbox_add("new_messages", 1, {"message_ids": [1]})
    for row in db.db.execute(
            "SELECT event_id FROM notify_outbox WHERE kind='new_messages'"):
        db.outbox_mark(row["event_id"], "accepted")
    bundle = semantic.thread_bundle(db, 1, 1)
    _summary(db, claims=1)
    payload = {
        "root_id": 1, "target_message_id": 1, "src_event_id": src,
        "target_revision": bundle["members"][0]["revision"],
        "fingerprint": bundle["source_fingerprint"],
        "policy_version": semantic.POLICY_VERSION,
        "policy_fingerprint": semantic.policy_fingerprint(semantic.semantic_config(_cfg("enforce"))[0]), "text": _notice_text(text),
    }
    db.outbox_add("semantic_notice", 1, payload)
    return db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()


def _prepare_send(monkeypatch, cfg):
    monkeypatch.setattr(notifier, "_config", lambda: cfg[0])
    monkeypatch.setattr(notifier, "_token", lambda: "token")
    monkeypatch.setattr(notifier, "_channel_id", lambda kind: "channel")


def test_shadow_and_off_keep_existing_notification_byte_identical(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    event = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'"
    ).fetchone()
    baseline = notifier._format_event(db, event)[0]
    _summary(db, claims=8)
    cfg = [{"semantic": {"mode": "shadow"}}]
    monkeypatch.setattr(notifier, "_config", lambda: cfg[0])
    shadow = notifier._format_event(db, event)[0]
    cfg[0] = {"semantic": {"mode": "off"}}
    off = notifier._format_event(db, event)[0]
    assert shadow == baseline == off
    db.close()


def test_semantic_block_requires_current_whole_thread_fingerprint(
        tmp_path, monkeypatch):
    root = _message()
    reply = _message(2, parent=1, body="返信本文", unread=False)
    db = _db(tmp_path, [root])
    db.save_thread_replies([reply], 1, notify=None)
    _summary(db, claims=1)
    event = db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='new_messages'"
    ).fetchone()
    cfg = [_cfg("enforce")]
    monkeypatch.setattr(notifier, "_config", lambda: cfg[0])
    before = notifier._format_event(db, event)[0]
    assert "claim-0-" in before
    db.save_thread_replies([_message(2, parent=1, body="返信が訂正された",
                                      unread=False)], 1, notify=None)
    after = notifier._format_event(db, event)[0]
    assert "claim-0-" not in after
    db.close()


def test_semantic_send_holds_after_source_change_mid_delivery(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    event = _semantic_event(db, "z" * 4000)
    cfg = [_cfg("enforce")]
    _prepare_send(monkeypatch, cfg)
    calls = []

    def post(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            db.save_patient(_patient([_message(body="本文が訂正された",
                                                unread=False)]), notify=None)
        return str(len(calls))

    monkeypatch.setattr(notifier, "_post", post)
    result = notifier.flush(db)
    row = db.db.execute(
        "SELECT state,next_try,progress FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()
    progress = json.loads(row["progress"])
    assert len(calls) == 1
    assert result["failed"] == 1
    assert row["state"] == "failed" and row["next_try"] is None
    assert progress["next"] == 1 and progress["sent"] == ["1"]
    db.close()


def test_semantic_send_holds_after_mode_change_mid_delivery(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    event = _semantic_event(db, "z" * 4000)
    cfg = [_cfg("enforce")]
    _prepare_send(monkeypatch, cfg)
    calls = []

    def post(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            cfg[0] = {"semantic": {"mode": "off"}}
        return str(len(calls))

    monkeypatch.setattr(notifier, "_post", post)
    result = notifier.flush(db)
    row = db.db.execute(
        "SELECT state,next_try,progress FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()
    progress = json.loads(row["progress"])
    assert len(calls) == 1
    assert result["failed"] == 1
    assert row["state"] == "failed" and row["next_try"] is None
    assert progress["next"] == 1 and progress["sent"] == ["1"]
    db.close()


def test_attached_summary_holds_when_mode_changes_mid_delivery(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    _summary(db, claims=8)
    cfg = [_cfg("enforce")]
    _prepare_send(monkeypatch, cfg)
    calls = []

    def post(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            cfg[0] = {"semantic": {"mode": "off"}}
        return str(len(calls))

    monkeypatch.setattr(notifier, "_post", post)
    result = notifier.flush(db)
    row = db.db.execute(
        "SELECT state,next_try,progress FROM notify_outbox "
        "WHERE kind='new_messages'"
    ).fetchone()
    progress = json.loads(row["progress"])
    assert len(calls) == 1
    assert result["failed"] == 1
    assert row["state"] == "failed" and row["next_try"] is None
    assert progress["next"] == 1 and progress["sent"] == ["1"]
    db.close()


def test_resumed_stale_semantic_notice_holds_receipt(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    event = _semantic_event(db, "z" * 4000)
    db.db.execute(
        "UPDATE notify_outbox SET state='failed',next_try=0,progress=? "
        "WHERE event_id=?",
        (json.dumps({"next": 1, "sent": ["1"], "fingerprint": "f"}),
         event["event_id"]))
    db.db.commit()
    db.save_patient(_patient([_message(body="別世代の本文", unread=False)]),
                    notify=None)
    cfg = [_cfg("enforce")]
    _prepare_send(monkeypatch, cfg)
    calls = []
    monkeypatch.setattr(notifier, "_post",
                        lambda *args, **kwargs: calls.append(args[2]) or "2")
    result = notifier.flush(db)
    row = db.db.execute(
        "SELECT state,next_try,progress FROM notify_outbox "
        "WHERE event_id=?", (event["event_id"],)
    ).fetchone()
    assert calls == []
    assert result["failed"] == 1
    assert row["state"] == "failed" and row["next_try"] is None
    assert json.loads(row["progress"])["sent"] == ["1"]
    db.close()


def test_semantic_notice_repeats_provenance_on_every_chunk(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    event = _semantic_event(db, "長文の根拠 " * 1800)
    cfg = [_cfg("enforce")]
    _prepare_send(monkeypatch, cfg)
    calls = []
    monkeypatch.setattr(
        notifier, "_post",
        lambda *args, **kwargs: calls.append(args[2]) or str(len(calls)))

    result = notifier.flush(db)
    row = db.db.execute(
        "SELECT progress FROM notify_outbox WHERE event_id=?",
        (event["event_id"],),
    ).fetchone()
    progress = json.loads(row["progress"])
    assert result["sent"] == 1
    assert len(calls) > 1
    assert progress["next"] == len(calls)
    assert all(len(part) <= notifier._MAX_LEN for part in calls)
    assert all("取得：完全" in part for part in calls)
    assert all("要約：自動検査完了" in part for part in calls)
    assert all("part " in part for part in calls)
    assert all(
        "▶ MCSで確認\nhttps://www.medical-care.net/projects/medical/1" in part
        for part in calls
    )
    db.close()


def test_semantic_retry_keeps_frozen_chunk_receipt(
        tmp_path, monkeypatch):
    db = _db(tmp_path, [_message()])
    event = _semantic_event(db, "固定payload " * 1800)
    cfg = [_cfg("enforce")]
    _prepare_send(monkeypatch, cfg)
    first_attempt = []

    def fail_after_first(*args, **kwargs):
        first_attempt.append(args[2])
        if len(first_attempt) == 1:
            return "1"
        raise OSError("synthetic transport failure")

    monkeypatch.setattr(notifier, "_post", fail_after_first)
    result = notifier.flush(db)
    row = db.db.execute(
        "SELECT state,progress FROM notify_outbox WHERE event_id=?",
        (event["event_id"],),
    ).fetchone()
    progress = json.loads(row["progress"])
    assert result["failed"] == 1
    assert row["state"] == "failed"
    assert progress["next"] == 1 and progress["sent"] == ["1"]
    first_part = first_attempt[0]

    retry = []
    monkeypatch.setattr(
        notifier, "_post", lambda *args, **kwargs: retry.append(args[2]) or "2"
    )
    db.db.execute(
        "UPDATE notify_outbox SET next_try=0 WHERE event_id=?",
        (event["event_id"],),
    )
    db.db.commit()
    result = notifier.flush(db)
    assert result["sent"] == 1
    assert retry
    assert first_part == first_attempt[0]
    assert all(
        "取得：完全" in part and "要約：自動検査完了" in part
        for part in [first_part, *retry]
    )
    assert all(
        "▶ MCSで確認\nhttps://www.medical-care.net/projects/medical/1" in part
        for part in [first_part, *retry]
    )
    db.close()
