"""🌅 daily digest (ROADMAP #13) — once per JST day, ids and counts only,
coverage always disclosed. Synthetic temp ledger only."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

import notify_digest
import notify_flush
from mcs_queries import JST
from notify_testkit import _msg, _patient, _signal_row, led
from semantic_send_gate import StaleSend

__all__ = ["led"]

ON = {"notify_target": "discord:1", "daily_digest": {"enabled": True,
                                                     "hour_jst": 8}}


def _at(day, hour, minute=0):
    return datetime.fromisoformat(
        f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=JST).timestamp()


T = _at("2026-10-01", 8, 5)


def _seen(led, mid, at, pid=1, prof="看護師", body="秘密の本文", posted=None):
    _msg(led, mid, pid=pid, prof=prof, body=body,
         ts=int(posted if posted is not None else at))
    led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=?",
                   (at, mid))
    led.db.commit()


def _digests(led):
    return led.db.execute("SELECT * FROM notify_outbox "
                          "WHERE kind='daily_digest' ORDER BY event_id"
                          ).fetchall()


def _text(led, ev=None):
    return json.loads((ev or _digests(led)[-1])["payload"])["text"]


def test_once_per_day_after_the_hour(led):
    assert notify_digest.maybe_enqueue(led, ON, now=_at("2026-10-01", 7)) == 0
    assert notify_digest.maybe_enqueue(led, {**ON, "daily_digest": {}},
                                       now=T) == 0
    assert notify_digest.maybe_enqueue(
        led, {**ON, "notify_target": ""}, now=T) == 0
    assert notify_digest.maybe_enqueue(led, ON, now=T) == 1
    assert notify_digest.maybe_enqueue(led, ON, now=T + 600) == 0
    # a late tick the next day catches up; the window chains on
    nxt = _at("2026-10-02", 13)
    assert notify_digest.maybe_enqueue(led, ON, now=nxt) == 1
    first, second = (json.loads(e["payload"]) for e in _digests(led))
    assert (first["date"], second["date"]) == ("2026-10-01", "2026-10-02")
    assert first["since"] == T - 86400 and second["since"] == first["until"]
    assert all(e["route"] == "text" for e in _digests(led))


def test_body_is_built_outside_the_write_lock(led, monkeypatch):
    """build_text runs before BEGIN IMMEDIATE; a digest another writer
    queued meanwhile wins and no duplicate is written."""
    real = notify_digest.build_text

    def build(db, *a):
        assert not db.in_transaction
        led.outbox_add(notify_digest.KIND, None, {"text": "x",
                                                  "date": "2026-10-01"})
        return real(db, *a)
    monkeypatch.setattr(notify_digest, "build_text", build)
    assert notify_digest.maybe_enqueue(led, ON, now=T) == 0
    assert len(_digests(led)) == 1


def test_counts_ids_and_no_patient_content(led):
    _patient(led, 1, name="患者A")
    _patient(led, 2, name="患者B", archived=1)
    _seen(led, 100, T - 3600)
    _seen(led, 101, T - 60, prof="医師")
    _seen(led, 102, T - 60, prof="")
    _seen(led, 103, T - 2 * 86400)                   # before the window
    _seen(led, 104, T - 60, posted=T - 30 * 86400)   # history import
    _seen(led, 105, T - 60, pid=2)                   # archived room
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES('extract_v1',1,100,?,'rules',?,?)",
        (json.dumps({"urgency": "high"}), json.dumps({"hash": f"{100:064x}"}),
         T))
    led.db.commit()
    cfg = {**ON, "notify_max_age_h": 48}
    notify_digest.maybe_enqueue(led, cfg, now=T)
    text = _text(led)
    assert "■ 新着 3件（ルーム 1）: 看護師 1・医師 1・職種不明 1" in text
    assert "■ 緊急度: 高 1件（AI抽出 0・機械照合 1）" in text
    assert "・project 1 / message 100（機械照合）" in text
    for secret in ("秘密の本文", "患者A", "患者B", "職員"):
        assert secret not in text
    assert "記録が見つからないことは対応がなかったことを意味せず" in text
    assert "欠落なしの保証ではありません" in text


def test_patient_names_only_when_opted_in(led):
    """include_names adds the patient name next to each listed project;
    bodies and staff names never appear either way."""
    _patient(led, 1, name="患者A")
    _seen(led, 100, T - 3600)
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES('extract_v1',1,100,?,'rules',?,?)",
        (json.dumps({"urgency": "high"}), json.dumps({"hash": f"{100:064x}"}),
         T))
    led.db.commit()
    cfg = {**ON, "daily_digest": {**ON["daily_digest"], "include_names": True}}
    notify_digest.maybe_enqueue(led, cfg, now=T)
    text = _text(led)
    assert "・project 1 患者A / message 100（機械照合）" in text
    for secret in ("秘密の本文", "職員"):
        assert secret not in text


def _summary_at(led, pid, at, comment="連携の秘密本文", empty=False):
    led.karte_summary_store(pid, pid * 10, None if empty else
                            {"comment": comment, "updated_at": "2026-10-01",
                             "user": {"profession": "医師", "name": "職員X"},
                             "is_editable": False})
    led.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id="
                   "(SELECT max(artifact_id) FROM artifacts WHERE "
                   "kind='karte_summary' AND project_id=?)", (at, pid))
    led.db.commit()


def test_karte_summary_count_and_ids_without_comment(led):
    """Registered summaries stored in the window are counted with their
    project ids; empty stores and out-of-window ones are not; the
    comment and updater never appear."""
    _patient(led, 1, name="患者A")
    _patient(led, 2, name="患者B")
    _patient(led, 3, name="患者C")
    _patient(led, 4, name="患者D")
    _summary_at(led, 1, T - 3600)
    _summary_at(led, 1, T - 60, comment="二度目の秘密")      # same room: counted once
    _summary_at(led, 2, T - 60, empty=True)                # 空: not counted
    _summary_at(led, 3, T - 2 * 86400)                     # before window
    _summary_at(led, 4, T + 5)                             # after window
    notify_digest.maybe_enqueue(led, ON, now=T)
    text = _text(led)
    assert "■ 連携サマリー更新: 1件: project 1" in text     # rooms, not artifacts
    assert "project 2" not in text and "project 3" not in text
    assert "project 4" not in text
    for secret in ("連携の秘密本文", "二度目の秘密", "職員X", "患者A"):
        assert secret not in text


def test_karte_summary_names_when_opted_in(led):
    _patient(led, 1, name="患者A")
    _summary_at(led, 1, T - 60)
    cfg = {**ON, "daily_digest": {**ON["daily_digest"], "include_names": True}}
    notify_digest.maybe_enqueue(led, cfg, now=T)
    text = _text(led)
    assert "■ 連携サマリー更新: 1件: project 1 患者A" in text
    assert "連携の秘密本文" not in text and "職員X" not in text


def test_karte_summary_zero_line(led):
    notify_digest.maybe_enqueue(led, ON, now=T)
    assert "■ 連携サマリー更新: 0件\n" in _text(led)


def test_coverage_block_always_present(led):
    notify_digest.maybe_enqueue(led, ON, now=T)
    text = _text(led)
    assert "■ 取得状況（記録ベース）" in text
    assert "未完了として記録されたルーム: なし（完全性の保証ではありません）" in text

    _patient(led, 1)
    _patient(led, 2, name="患者B")
    led.db.execute("UPDATE patients SET fetch_state='incomplete',"
                   "fetch_reason='network_error' WHERE project_id=1")
    led.db.execute("UPDATE patients SET fetch_state='incomplete' "
                   "WHERE project_id=2")
    led.db.execute("INSERT INTO fetch_jobs(kind,project_id,state) "
                   "VALUES('reply',1,'failed')")
    _msg(led, 100)
    led.db.execute("UPDATE messages SET body_state='snippet'")
    led.db.execute("INSERT INTO notify_outbox(kind,payload,state,next_try) "
                   "VALUES('new_messages','{}','failed',NULL)")
    led.db.commit()
    notify_digest.maybe_enqueue(led, ON, now=_at("2026-10-02", 9))
    text = _text(led)
    assert ("・取得未完了のルーム 2: project 1（network_error）, "
            "project 2（unrecorded）") in text
    assert "・取得待ち/失敗ジョブ: reply 1" in text
    assert "・本文未取得の投稿: 1件" in text
    assert "・送信保留の通知: 1件" in text


def test_signal_block_excludes_request_and_deadline_types(led):
    _patient(led, 1)
    _signal_row(led, "a", stype="adherence_concern")
    _signal_row(led, "b", stype="request_overdue")
    _signal_row(led, "c", stype="rx_period_expiry")
    _signal_row(led, "d", stype="request_aging")
    _signal_row(led, "e", stype="adherence_concern", state="dismissed")
    led.db.commit()
    notify_digest.maybe_enqueue(led, {**ON, "signals": {"notify": True}},
                                now=T)
    assert "■ 確認候補（open）1件: adherence_concern 1" in _text(led)


def test_signal_block_needs_signals_notify(led):
    _patient(led, 1)
    _signal_row(led, "a", stype="adherence_concern")
    led.db.commit()
    notify_digest.maybe_enqueue(led, ON, now=T)
    assert "確認候補" not in _text(led)


def test_task_counts(led):
    for due, status in (("2026-09-30", "open"), ("2026-10-01", "in_progress"),
                        (None, "open"), ("2026-09-01", "done")):
        led.db.execute(
            "INSERT INTO requests(project_id,source_message_id,source_hash,"
            "title,due_date,status,revision,created_at,updated_at) "
            "VALUES(1,1,?,'t',?,?,1,0,0)", ("0" * 64, due, status))
    led.db.commit()
    notify_digest.maybe_enqueue(led, ON, now=T)
    assert "■ タスク: 未完了 3件（うち期限切れ 1件）" in _text(led)


def test_flush_relays_text_and_drops_when_turned_off(led, monkeypatch):
    notify_digest.maybe_enqueue(led, ON, now=T)
    ev = _digests(led)[0]
    monkeypatch.setattr(notify_flush, "_config", lambda: ON)
    text, files = notify_flush._format_event(led, ev)
    assert text.startswith("🌅 MCS 日次ダイジェスト") and files == []
    assert len(text) < 1900
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    with pytest.raises(StaleSend):
        notify_flush._format_event(led, ev)


def test_tick_hook_enqueues_even_without_notify(led, monkeypatch):
    from types import SimpleNamespace
    import run_check
    monkeypatch.setattr(notify_digest.time, "time", lambda: T)
    sent = []
    monkeypatch.setattr(notify_flush, "flush",
                        lambda *a, **k: sent.append(1))
    result = {"errors": []}
    run_check._deliver(led, SimpleNamespace(no_notify=True), ON, result,
                       None)
    assert result.get("daily_digest") == 1 and sent == []
    assert not [e for e in result["errors"] if "daily_digest" in e]
    assert len(_digests(led)) == 1
