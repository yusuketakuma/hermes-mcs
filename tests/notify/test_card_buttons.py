"""Card buttons as state toggles, member names, tasks, summary, ⚠
report and ⏰ reminders — runner side. Synthetic temp ledger only."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

import extract_llm
import ledger as _ledger
import mcs_signals
import notify_cards
import notify_cmds
import notify_flush
import notify_render
import notify_views
from mcs_queries import JST, extract_feedback
from notify_testkit import (
    CFG, NOW, ORIGIN, _add_request, _click, _deliver, _delivered_card, _dispatch,
    _intent, _latest_render, _llm_extract, _msg, _patient, _seed_thread,
    _signal_row, _spec, led, pinned_clock)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")

CFG_OFF = {"notify": {**CFG["notify"], "interactive": "off"},
           "signals": CFG["signals"]}
A, B = "discord:1001", "discord:2002"
SLACK_ACTOR = "slack:T0SYN:U0SYN1"


def _button(spec, action):
    return next(b for row in spec["parts"]["action_rows"] for b in row
                if b["id"] == action)


def _ids(spec):
    return [b["id"] for row in spec["parts"]["action_rows"] for b in row]


def _footer(spec):
    return "\n".join(f.get("text", "") for f in spec["parts"]["footer"])


# ---------- ☐/✅ 確認 ------------------------------------------------------

def test_ack_toggles_label_footer_and_withdraws(led):
    spec1 = _delivered_card(led)
    ack = _button(spec1, "ack")
    assert (ack["label"], ack["style"]) == ("確認する", "secondary")
    assert "✅" not in _footer(spec1)

    r = _click(led, spec1, "ack", now=NOW + 1)
    assert r["outcome"] == "applied" and r["delivery_id"]
    spec2 = _spec(led)
    ack = _button(spec2, "ack")
    assert (ack["label"], ack["style"]) == ("確認する", "success")
    assert "✅ 確認: <@1001>" in _footer(spec2)
    # footer names are mentions — the worker must send them silently
    assert spec2["parts"]["mentions"] == "silent"
    assert "mentions" not in spec1["parts"]

    # a double tap on the stale face never withdraws the fresh ack
    again = _click(led, spec1, "ack", now=NOW + 2)
    assert again["absorbed"] is True
    assert _latest_render(led)["delivery_id"] == r["delivery_id"]

    # another member confirms too — both are listed, by name
    _deliver(led)
    _click(led, spec2, "ack", actor=SLACK_ACTOR, now=NOW + 3)
    spec3 = _spec(led)
    assert "✅ 確認: <@1001>・<@U0SYN1>" in _footer(spec3)

    # the first member taps ✅ 確認済み again: only their ack is withdrawn
    _deliver(led)
    out = _click(led, spec3, "ack", now=NOW + 4)
    assert out["withdrawn"] is True
    spec4 = _spec(led)
    assert "✅ 確認: <@U0SYN1>" in _footer(spec4)
    assert _button(spec4, "ack")["style"] == "success"
    rows = led.db.execute(
        "SELECT actor, withdrawn_at FROM notification_acknowledgements "
        "ORDER BY ack_id").fetchall()
    # the audit row stays — withdrawn, not deleted
    assert [(r["actor"], r["withdrawn_at"]) for r in rows] == [
        (A, NOW + 4), (SLACK_ACTOR, None)]

    _deliver(led)
    _click(led, spec4, "ack", actor=SLACK_ACTOR, now=NOW + 5)
    spec5 = _spec(led)
    assert _button(spec5, "ack")["style"] == "secondary"
    assert "✅" not in _footer(spec5)


def test_new_content_starts_unconfirmed(led):
    spec1 = _delivered_card(led)
    _click(led, spec1, "ack", now=NOW + 1)
    _deliver(led)
    assert _button(_spec(led), "ack")["style"] == "success"
    _msg(led, 102, 1, parent=100, body="新しい返信")
    notify_cards.sweep(led, CFG, now=NOW + 2)
    spec = _spec(led)
    assert _button(spec, "ack")["style"] == "secondary"
    assert "✅" not in _footer(spec)


def test_digest_ack_label_follows_toggle(led):
    _patient(led, 2)
    _msg(led, 200, 2)
    _signal_row(led, "sig-d", pid=2, mids=[200])
    _dispatch(led, _intent(led, kind="signal", pid=None, payload={
        "digest": True, "signal_keys": ["sig-d"]}))
    _deliver(led)
    spec = _spec(led)
    assert spec["kind"] == "digest"
    assert _button(spec, "ack")["label"] == "このページを確認する"
    # a digest spans projects: no MCS link, no single-patient summary
    assert not {"link", "summary", "request"} & set(_ids(spec))
    _click(led, spec, "ack", now=NOW + 1)
    assert _button(_spec(led), "ack")["style"] == "success"


# ---------- 👤 担当 --------------------------------------------------------

def test_assign_toggle_takeover_and_release(led):
    spec1 = _delivered_card(led)
    b = _button(spec1, "assign")
    assert (b["label"], b["style"]) == ("担当する", "secondary")

    _click(led, spec1, "assign", now=NOW + 1)
    spec2 = _spec(led)
    b = _button(spec2, "assign")
    assert (b["label"], b["style"]) == ("担当する", "primary")
    assert "👤 担当: <@1001>" in _footer(spec2)
    # double tap on the old face: still assigned, nothing new rendered
    assert _click(led, spec1, "assign", now=NOW + 2)["absorbed"] is True
    # another member on the face that predates A's assignment does not
    # silently take over — stale, refresh
    r = _click(led, spec1, "assign", actor=B, now=NOW + 2)
    assert (r["outcome"], r["error"], r["hint"]) == (
        "rejected", "stale_ui", "refresh")
    assert led.db.execute("SELECT owner FROM notification_triage"
                          ).fetchone()[0] == A

    _deliver(led)
    r = _click(led, spec2, "assign", actor=B, now=NOW + 3)   # takeover
    assert r["owner"] == B
    spec3 = _spec(led)
    assert "👤 担当: <@2002>" in _footer(spec3)
    assert _button(spec3, "assign")["style"] == "primary"

    _deliver(led)
    r = _click(led, spec3, "assign", actor=B, now=NOW + 4)   # release
    assert r["released"] is True and r["owner"] is None
    spec4 = _spec(led)
    assert _button(spec4, "assign")["style"] == "secondary"
    assert "👤" not in _footer(spec4)
    tri = led.db.execute("SELECT * FROM notification_triage").fetchone()
    assert tri["state"] == "open" and tri["owner"] is None


@pytest.mark.parametrize("actor,label", [
    ("discord:3922000000000001", "<@3922000000000001>"),
    ("slack:T123:U0AB12CD", "<@U0AB12CD>"),
    ("slack:U0AB12CD", notify_render.UNKNOWN_ACTOR),     # no team part
    ("nurse-1", notify_render.UNKNOWN_ACTOR),
    ("discord:<@everyone>", notify_render.UNKNOWN_ACTOR),
    (None, notify_render.UNKNOWN_ACTOR),
])
def test_actor_label_never_renders_raw_ids(actor, label):
    assert notify_render.actor_label(actor) == label


def test_stored_legacy_owner_renders_as_mention(led):
    """Triage rows written before this change hold the same actor
    strings — they render as names with no migration."""
    _delivered_card(led)
    led.db.execute(
        "INSERT INTO notification_triage(card_id,owner,state,revision,"
        "last_actor,updated_at) VALUES(1,'discord:3922000000000001',"
        "'assigned',1,'discord:3922000000000001',?)", (NOW,))
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 1)
    footer = _footer(_spec(led))
    assert "👤 担当: <@3922000000000001>" in footer
    assert "discord:" not in footer


def test_withdrawn_at_migration_is_additive_and_idempotent(tmp_path):
    path = tmp_path / "ledger.db"
    _ledger.Ledger(str(path)).close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE notification_acknowledgements "
               "DROP COLUMN withdrawn_at")           # a pre-change DB
    db.commit()
    db.close()
    for _ in range(2):
        led2 = _ledger.Ledger(str(path))
        cols = {r[1] for r in led2.db.execute(
            "PRAGMA table_info(notification_acknowledgements)")}
        led2.close()
        assert "withdrawn_at" in cols


# ---------- layout / 🔗 link ----------------------------------------------

def test_button_rows_layout_and_link(led):
    spec = _delivered_card(led)
    rows = [[b["id"] for b in row] for row in spec["parts"]["action_rows"]]
    # primary row first; card_thread on: 本文 lives in the thread; no
    # task yet -> no タスク一覧; no extract_llm result -> no 誤りを報告
    assert rows == [["ack", "assign", "request", "link"], ["summary"],
                    ["digest", "mytasks", "unacked", "search"]]
    link = _button(spec, "link")
    assert link == {"id": "link", "ui": "link", "label": "MCSで開く",
                    "url": "https://www.medical-care.net/projects/medical/1"}
    assert "defer" not in _ids(spec)
    assert all(len(row) <= 5 for row in rows) and len(rows) <= 5


# ---------- 📝 tasks in the footer / ☑ ------------------------------------

def test_footer_lists_open_tasks_and_tasks_button(led):
    _delivered_card(led)
    today = notify_render.today_jst(NOW)
    _add_request(led, title="期限切れの確認", assignee="山田（みどり薬局）",
             due="2020-01-01")
    _add_request(led, src_mid=101, title="<@999> 返信の件", due=today)
    _add_request(led, title="三件目")
    _add_request(led, title="四件目")
    _add_request(led, title="完了済み", status="done")
    notify_cards.sweep(led, CFG, now=NOW + 1)
    spec = _spec(led)
    footer = _footer(spec)
    # the face counts open tasks; titles live in タスク一覧
    assert "📝 タスク 4件（期限切れ 1）" in footer
    assert "完了済み" not in footer and "@999" not in footer
    assert today
    assert _button(spec, "tasks")["label"] == "タスク一覧"
    assert "mentions" not in spec["parts"]       # no member names shown

    led.db.execute("UPDATE requests SET status='done'")
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 2)
    spec = _spec(led)
    assert "tasks" not in _ids(spec) and "📝" not in _footer(spec)


def test_task_status_rerenders_the_card(led):
    spec = _delivered_card(led)
    _add_request(led, title="対応する")
    notify_cards.sweep(led, CFG, now=NOW + 1)
    _deliver(led)
    spec = _spec(led)
    r = _click(led, spec, "tasks", now=NOW + 2)
    done = r["tasks"][0]["transitions"]["done"]["token"]
    out = notify_cards.apply_notification(led, {
        "version": 1, "op": "notification",
        "command_id": f"{done}:{'cd' * 8}", "actor": A, "token": done,
        "origin": dict(ORIGIN, message_id="eph-1")}, CFG, now=NOW + 3)
    assert out["status"] == "done" and out["delivery_id"]
    spec = _spec(led)
    assert "tasks" not in _ids(spec) and "📝" not in _footer(spec)


def test_request_create_rerenders_anchored_card(led, tmp_path):
    _delivered_card(led)
    before = _latest_render(led)["render_rev"]
    root = str(tmp_path / "data")
    out = notify_cmds.dispatch(led, {
        "version": 1, "cmd": "request.create",
        "command_id": "11111111-2222-4333-8444-555555555555",
        "actor": A, "human_confirmed": True, "project_id": 1,
        "source_message_id": 101, "source_hash": f"{101:064x}",
        "title": "服薬状況を確認", "reason": "通知カードからタスク作成",
        "assignee": "山田", "due_date": "2026-10-01"}, CFG, root)
    assert out["outcome"] == "applied"
    render = _latest_render(led)
    assert render["render_rev"] == before + 1
    footer = _footer(json.loads(render["spec_json"]))
    assert "📝 タスク 1件" in footer


def test_applied_command_survives_a_failed_rerender(led, tmp_path,
                                                    monkeypatch):
    _delivered_card(led)

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(notify_cards, "rerender_message_cards", boom)
    out = notify_cmds.dispatch(led, {
        "version": 1, "cmd": "request.create",
        "command_id": "11111111-2222-4333-8444-555555555556",
        "actor": A, "human_confirmed": True, "project_id": 1,
        "source_message_id": 101, "source_hash": f"{101:064x}",
        "title": "件", "reason": "r"}, CFG, str(tmp_path / "data"))
    assert out["outcome"] == "applied"
    assert out["rerender_error"] == "OperationalError"
    assert led.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


# ---------- 📝 form: prefill + assignee roster ----------------------------

def test_request_click_returns_prefill_and_roster_never_persisted(
        led, tmp_path):
    _seed_thread(led)
    _llm_extract(led, 100, {"requests": [
        {"action": "残薬を  確認して\n報告", "to": "薬剤師"}]})
    with led.db:
        mcs_signals.record_station_staff(led.db, [
            {"staff_id": 1, "name": "山田 花子", "station": "みどり薬局"},
            {"staff_id": 2, "name": "佐藤 一郎", "station": "みどり薬局"},
            {"staff_id": 3, "name": "山田 花子", "station": "みどり薬局"}])
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    assert _button(spec, "request")["label"] == "タスク作成"
    r = _click(led, spec, "request")
    assert r["modal"] is True
    assert r["form"] == {"hint": "残薬を 確認して 報告",
                         "staff": ["山田 花子（みどり薬局）",
                                   "佐藤 一郎（みどり薬局）"]}
    stored = led.db.execute(
        "SELECT receipt_json FROM command_receipts WHERE command_id "
        "LIKE ?", (_button(spec, "request")["token"] + ":%",)
    ).fetchone()["receipt_json"]
    assert "form" not in json.loads(stored) and "残薬" not in stored


def test_roster_falls_back_to_own_station_senders(led):
    _patient(led, 1)
    _msg(led, 100, sender="佐藤 一郎", org="みどり薬局, 別の薬局")
    _msg(led, 101, sender="訪問 看護", org="あおば訪問看護")
    _msg(led, 102, sender="佐藤 一郎", org="みどり薬局, 別の薬局")
    assert notify_cards.assignee_choices(led.db) == []    # no identity
    with led.db:
        mcs_signals.record_self_profile(led.db, {
            "sender_id": 9, "name": "薬局 太郎", "professions": [],
            "organizations": ["みどり薬局"]})
    assert notify_cards.assignee_choices(led.db) == [
        "薬局 太郎（みどり薬局）", "佐藤 一郎（みどり薬局）"]


# ---------- 🧾 summary -----------------------------------------------------

def test_summary_without_rollup_says_so(led):
    spec = _delivered_card(led)
    r = _click(led, spec, "summary")
    assert r["outcome"] == "applied" and r["action"] == "summary"
    assert "暫定集約" not in r["title"]
    assert "※" not in r["body"].split("\n")[0]
    assert "集約資料がまだありません" in r["body"]
    assert "履歴取得: 未完了（完了記録なし）" in r["body"]
    assert "欠落なしの保証ではありません" not in r["body"]
    assert "■ 未完了タスク: なし" in r["body"]
    stored = led.db.execute(
        "SELECT receipt_json FROM command_receipts").fetchall()[-1][0]
    assert "集約資料" not in stored and "body" not in json.loads(stored)


def test_summary_with_rollup_and_coverage(led):
    spec = _delivered_card(led)
    led.db.execute("UPDATE patients SET history_floor=-1,"
                   "fetch_state='incomplete',fetch_reason='network_error'")
    led.db.execute("UPDATE messages SET reply_count=3 WHERE message_id=100")
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,model,meta,"
        "created_at) VALUES('patient_rollup',1,?,'rules-v1','{}',?)",
        (json.dumps({"medications": [
            {"name": "アムロジピン", "dose": "5mg", "freq": "1日1回",
             "last": "2026-09-20"}],
            "current_med_period": {"start": "2026-09-01",
                                   "end": "2026-09-28"},
            "latest_vitals": {"at": "2026-09-22", "sbp": 128, "dbp": 70,
                              "bt": 36.5},
            "next_planned": "10/3 訪問"}, ensure_ascii=False), NOW))
    _add_request(led, title="血圧記録の確認", assignee="山田", due="2026-10-01")
    body = _click(led, spec, "summary")["body"]
    assert ("履歴取得: 完了記録あり／直近の取得は未完了（network_error）"
            "／返信の取得未完了1件") in body
    assert "処方期間（抽出表現）: 2026-09-01〜2026-09-28" in body
    assert "・アムロジピン 5mg 1日1回（最終言及 2026-09-20）" in body
    assert "バイタル: BP 128/70  BT 36.5（2026-09-22）" in body
    assert "■ 次回予定（抽出表現）: 10/3 訪問" in body
    assert "血圧記録の確認 — 担当 山田 — 期限 2026-10-01" in body
    led.db.execute("UPDATE artifacts SET content=? WHERE kind='patient_rollup'",
                   (json.dumps({"medications": []}),))
    led.db.commit()
    body = _click(led, _spec(led), "summary")["body"]
    assert "■ 抽出されたバイタルなし" in body and "記録なし" not in body


@pytest.mark.parametrize("ks, line", [
    ("never", "連携サマリー（MCS）: 未取得"),
    (None, "連携サマリー（MCS）: 空"),       # fetched, nothing registered
    ({"comment": "病歴: 合成\n注意点 " + "あ" * 90,
      "updated_at": "2026-09-30T10:00:00+09:00",
      "user": {"profession": "看護師"}},
     "連携サマリー（MCS・更新 09/30・看護師）: 病歴: 合成 注意点 "
     + "あ" * 68 + "…"),
])
def test_summary_karte_summary_line(led, ks, line):
    """The line reads the newest stored 連携サマリー, never the copy a
    rollup froze at its last rebuild."""
    _patient(led, 1)
    stale = {"comment": "古い要約", "updated_at": "2026-09-01T00:00:00+09:00"}
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,content,model,meta,"
        "created_at) VALUES('patient_rollup',1,?,'rules-v1','{}',?)",
        (json.dumps({"medications": [], "karte_summary": stale},
                    ensure_ascii=False), NOW))
    if ks != "never":
        led.karte_summary_store(1, 1, ks)
    led.db.commit()
    body = notify_views.patient_summary_text(led.db, 1)[1]
    assert "古い要約" not in body
    assert f"{line}\n" in body or body.endswith(line)   # nothing after it
    assert body.count("\n連携サマリー（MCS") == 1
    if isinstance(ks, dict):
        assert ks["comment"][:80] not in body      # cut, not the raw text


# ---------- ⚠ extraction report --------------------------------------------

def _pending_ids(led):
    return {r[0] for r in led.db.execute(
        f"SELECT m.message_id FROM messages m WHERE {extract_llm.pending_pred()}")}


def _report(led, root, artifact_id, mid=101, field="meds", n=1):
    return notify_cmds.dispatch(led, {
        "version": 1, "cmd": "ops.extract_feedback",
        "command_id": f"22222222-3333-4444-8555-{n:012d}",
        "actor": A, "human_confirmed": True, "project_id": 1,
        "message_id": mid, "artifact_id": artifact_id, "field": field,
        "reason": "用量が違う"}, CFG, root)


def test_report_mark_clears_when_v4_becomes_current(led, tmp_path):
    _seed_thread(led)
    aid = _llm_extract(led, 101, {"summary": "s", "requests": []})
    led.db.commit()
    _dispatch(led, _intent(led))
    _deliver(led)
    assert _report(led, str(tmp_path / "data"), aid)["outcome"] == "applied"
    card = notify_cards._card_row(led.db, 1)
    assert notify_render.feedback_pending(led.db, card)
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES('semantic_facts_v4',1,101,'{}','v4',?,?)",
        (json.dumps({"hash": f"{101:064x}", "engine_version": 4}), NOW))
    led.db.commit()
    assert not notify_render.feedback_pending(led.db, card)
    assert extract_feedback(led.db, 1)[0]["current"] == 0


def test_report_pins_extraction_repends_once_and_marks_card(led, tmp_path):
    _seed_thread(led)
    aid = _llm_extract(led, 101, {"summary": "s", "requests": []})
    led.db.commit()
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    assert spec["parts"]["context"]["extract_ref"] == {
        "message_id": 101, "artifact_id": aid,
        "content_hash": f"{101:064x}"}
    assert _button(spec, "report")["label"] == "誤りを報告"
    assert _click(led, spec, "report")["modal"] is True
    assert 101 not in _pending_ids(led)

    root = str(tmp_path / "data")
    assert _report(led, root, aid)["outcome"] == "applied"
    rows = extract_feedback(led.db, 1)
    content = json.loads(rows[0]["content"])
    assert (content["artifact_id"], content["field"], content["note"],
            content["actor"]) == (aid, "meds", "用量が違う", A)
    assert rows[0]["current"] == 1
    assert 101 in _pending_ids(led)                     # one re-extract
    assert "⚠ 誤り報告あり" in _footer(_spec(led))       # re-rendered now

    # the re-extraction replaces the artifact: report settles, mark goes
    led.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (aid,))
    _llm_extract(led, 101, {"summary": "s2", "requests": []})
    led.db.commit()
    assert 101 not in _pending_ids(led)
    assert extract_feedback(led.db, 1)[0]["current"] == 0
    notify_cards.sweep(led, CFG, now=NOW + 1)
    assert "誤り報告" not in _footer(_spec(led))
    # a report on the superseded artifact is refused
    stale = _report(led, root, aid, n=2)
    assert stale["outcome"] == "rejected" \
        and stale["error"] == "extraction_changed"


def test_report_command_validation(led, tmp_path):
    root = str(tmp_path / "data")
    _seed_thread(led)
    for field, err in (("bogus", "bad_field"),):
        out = _report(led, root, 1, field=field, n=3)
        assert out["error"] == err
    out = notify_cmds.dispatch(led, {
        "version": 1, "cmd": "ops.extract_feedback",
        "command_id": "22222222-3333-4444-8555-000000000009",
        "actor": A, "human_confirmed": True, "project_id": 1,
        "message_id": 101, "artifact_id": 1, "field": "meds"}, CFG, root)
    assert out["outcome"] == "rejected"               # reason is required


# ---------- ⏰ reminders ---------------------------------------------------

def _at(day, hour):
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(
        tzinfo=JST).timestamp()


def test_due_and_overdue_reminders_fire_once(led):
    _delivered_card(led)
    # activation baseline before any task exists
    assert notify_cards.task_reminders(
        led, CFG, now=_at("2026-09-20", 9)) == 0
    due = _add_request(led, title="今日の確認", assignee="山田", due="2026-10-01")
    late = _add_request(led, src_mid=101, title="<@1> 昨日の件",
                    due="2026-09-30")
    _add_request(led, title="期限なし")
    _add_request(led, title="完了", due="2026-09-01", status="done")
    led.db.execute("UPDATE messages SET organization='合成所属' WHERE message_id=101")
    led.db.commit()
    morning = _at("2026-10-01", 9)
    assert notify_cards.task_reminders(led, CFG_OFF, now=morning) == 0
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-10-01", 23)) == 0
    assert notify_cards.task_reminders(led, CFG, now=morning) == 2
    assert notify_cards.task_reminders(led, CFG, now=morning + 60) == 0
    events = led.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='task_reminder' "
        "ORDER BY event_id").fetchall()
    texts = [notify_flush._format_event(led, e)[0] for e in events]
    assert texts == [
        "患者A: ＜＠1＞ 昨日の件 — ⚠ 期限切れ（期限 2026-09-30） / 職員（合成所属） / 09-24 08:41 — 担当 未設定",
        "患者A: 今日の確認 — ⏰ 期限リマインド（本日 2026-10-01） / 職員（所属未取得） / 09-24 08:40 — 担当 山田"]
    assert all(e["route"] == "text" for e in events)
    assert all("<@" not in text for text in texts)
    assert "昨日の件" in texts[0][:40] and "今日の確認" in texts[1][:40]
    # the next day the due-day task gets its one overdue reminder
    assert notify_cards.task_reminders(
        led, CFG, now=_at("2026-10-02", 9)) == 1
    stages = led.db.execute(
        "SELECT request_id, stage FROM notification_task_reminders "
        "WHERE request_id>0 ORDER BY request_id, stage").fetchall()
    assert [tuple(r) for r in stages] == [
        (due, "due"), (due, "overdue"), (late, "overdue")]
    assert notify_cards.task_reminders(
        led, CFG, now=_at("2026-10-03", 9)) == 0



def test_long_reminder_title_keeps_source_five_fields_in_outgoing_text(led):
    _delivered_card(led)
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-09-20", 9)) == 0
    _add_request(led, src_mid=101, title="承認済みの主内容" + "要" * 400, due="2026-10-01")
    with led.db:
        led.db.execute("UPDATE patients SET patient_name=? WHERE project_id=1", ("患" * 30,))
        led.db.execute("UPDATE messages SET sender_name=?,organization=?,posted_at='2026-10-06T08:30:00+09:00' WHERE message_id=101", ("発" * 24, "所" * 24))
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-10-01", 9)) == 1
    event = led.db.execute("SELECT * FROM notify_outbox WHERE kind='task_reminder' ORDER BY event_id DESC LIMIT 1").fetchone()
    text = notify_flush._format_event(led, event)[0]
    assert "患" * 30 in text and "承認済みの主内容" in text[:50]
    assert "発" * 24 in text and "所" * 24 in text and "10-06 08:30" in text
    assert "2026-10-01" in text and len(text) <= 600
    assert notify_cards.task_reminders(led, CFG, now=_at("2026-10-01", 10)) == 0


def test_first_activation_baselines_old_overdue_tasks(led):
    """Tasks already overdue when reminders first run are recorded, not
    announced — only tasks that fall due afterwards are sent."""
    _delivered_card(led)
    old = _add_request(led, title="古い期限切れ", due="2026-09-01")
    today = _add_request(led, title="本日", due="2026-10-01")
    # the baseline runs even outside the posting hours
    assert notify_cards.task_reminders(
        led, CFG, now=_at("2026-10-01", 23)) == 0
    assert notify_cards.task_reminders(
        led, CFG, now=_at("2026-10-02", 9)) == 1       # today's → overdue
    sent = [r["request_id"] for r in led.db.execute(
        "SELECT request_id FROM notification_task_reminders"
        " WHERE event_id IS NOT NULL")]
    assert sent == [today] and old != today


def test_reminders_capped_per_tick_and_rearmed_by_due_change(led):
    _delivered_card(led)
    assert notify_cards.task_reminders(
        led, CFG, now=_at("2026-09-20", 9)) == 0       # baseline
    ids = [_add_request(led, title=f"t{i}", due="2026-10-01") for i in range(5)]
    morning = _at("2026-10-01", 9)
    assert notify_cards.task_reminders(led, CFG, now=morning) \
        == notify_cards.REMINDER_LIMIT == 3
    assert notify_cards.task_reminders(led, CFG, now=morning + 300) == 2
    assert notify_cards.task_reminders(led, CFG, now=morning + 600) == 0
    # moving the due date re-arms the task (the rest are done)
    led.db.execute("UPDATE requests SET status='done' WHERE request_id!=?",
                   (ids[0],))
    led.db.execute("UPDATE requests SET due_date='2026-10-03'"
                   " WHERE request_id=?", (ids[0],))
    led.db.commit()
    assert notify_cards.task_reminders(
        led, CFG, now=_at("2026-10-03", 9)) == 1


# ---------- footer text budget ------------------------------------------

def test_worst_case_footer_stays_under_the_text_budget(led, tmp_path,
                                                       monkeypatch):
    """8+ acknowledgers, an owner, 3+ overdue tasks, the report mark and
    the page cap together: the spec validator's estimate covers the real
    Discord face and both stay under MAX_TOTAL_TEXT."""
    import sys
    from discord_testkit import _fake_discord
    from hermes_plugin.mcs_delivery import spec as spec_mod
    from hermes_plugin.mcs_discord import cards as discord_cards
    mids = tuple(range(100, 112))
    _patient(led, 1, name="患" * 60)
    for m in mids:
        _msg(led, m, 1, parent=None if m == 100 else 100, body="長" * 900)
    aid = _llm_extract(led, 101, {"summary": "s", "requests": []})
    led.db.commit()
    _dispatch(led, _intent(led, payload={"message_ids": list(mids)}))
    _deliver(led)
    for i in range(10):
        _click(led, _spec(led), "ack", actor=f"discord:{10 ** 18 + i}")
        _deliver(led)
    _click(led, _spec(led), "assign", actor=f"discord:{2 * 10 ** 18}")
    _deliver(led)
    for i in range(5):
        _add_request(led, title="題" * 200, assignee="担" * 120,
                 due="2020-01-01")
    assert _report(led, str(tmp_path), aid)["outcome"] == "applied"
    notify_cards.sweep(led, CFG, now=NOW + 1)
    spec = _spec(led)
    footer = _footer(spec)
    containers = "\n".join(c.get("text", "")
                           for c in spec["parts"]["containers"])
    assert "他2名" in footer and "📝 タスク 5件（期限切れ 5）" in footer \
        and "誤り報告" in footer and "ページ" in containers
    spec_mod.validate(spec)
    _, est = spec_mod._containers_cost(spec["parts"]["containers"])
    est += spec_mod._footer_cost(spec["parts"]["footer"])[1]
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    view = discord_cards.build_view(spec)
    real = sum(len(c.content) for c in view.items[0].children
               if hasattr(c, "content"))
    assert real <= est <= spec_mod.MAX_TOTAL_TEXT


# ---------- urgency badge -------------------------------------------------

def test_urgency_badge_names_its_source(led):
    import structured_view
    _seed_thread(led, mids=(100, 101, 102))
    led.db.execute("UPDATE messages SET body_text='本人が急変につき至急ご連絡ください。' WHERE message_id IN (100,101)")
    _llm_extract(led, 100, {"urgency": "high", "summary": "至急",
                            "urgency_evidence": ["本人が急変につき至急ご連絡ください。"]})
    h = f"{101:064x}"
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES('extract_v1',1,101,?,'rules',?,?)",
        (json.dumps({"urgency": "high"}), json.dumps({"hash": h}), NOW))
    led.db.commit()
    assert structured_view.message_urgency(led.db, 100) == "llm"
    assert structured_view.message_urgency(led.db, 101) == "rule"
    assert structured_view.message_urgency(led.db, 102) is None
    _dispatch(led, _intent(led, payload={"message_ids": [100, 101, 102]}))
    texts = [c["text"] for c in _spec(led)["parts"]["containers"]
             if c["type"] == "text"]
    lines = [ln for t in texts for ln in t.split("\n")]
    assert any(ln.startswith("・🚨 緊急度: 高 ") or ln == "・🚨 緊急度: 高" for ln in lines)
    assert not any("AI" in ln for ln in lines)
    assert "・🚨" in lines


def test_urgency_reads_the_same_artifact_as_the_body(led):
    """A current v4 read-model row carries no urgency of its own — the
    badge then reads the hash-current extract_llm verdict behind it; an
    urgency recorded on the v4 row itself still wins."""
    import structured_view
    _seed_thread(led, mids=(100,))
    led.db.execute("UPDATE messages SET body_text='本人が急変につき至急ご連絡ください。' WHERE message_id=100")
    _llm_extract(led, 100, {"urgency": "high", "summary": "旧",
                            "urgency_evidence": ["本人が急変につき至急ご連絡ください。"]})
    v4 = led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,"
        "meta,created_at) VALUES('semantic_facts_v4',1,100,?,'v4',?,?)",
        (json.dumps({"summary": "新"}),
         json.dumps({"hash": f"{100:064x}", "engine_version": 4}), NOW))
    led.db.commit()
    assert structured_view.latest_fact_artifact(led.db, 100)["summary"] \
        == "新"
    assert structured_view.message_urgency(led.db, 100) == "llm"
    led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                   (json.dumps({"urgency": "high", "urgency_evidence": ["本人が急変につき至急ご連絡ください。"]}), v4.lastrowid))
    led.db.commit()
    assert structured_view.message_urgency(led.db, 100) == "llm"


def test_summary_karte_summary_line_without_rollup_reads_artifact(led):
    """No patient_rollup yet (or one built before the fetch): the
    連携サマリー line still reflects the stored artifact."""
    _patient(led, 1)
    body = notify_views.patient_summary_text(led.db, 1)[1]
    assert "連携サマリー（MCS）: 未取得" in body
    led.karte_summary_store(1, 10, {
        "comment": "合成のサマリー本文", "updated_at": "2026-09-30T10:00:00+09:00",
        "user": {"profession": "薬剤師", "name": "SYNTH"},
        "is_editable": True})
    body = notify_views.patient_summary_text(led.db, 1)[1]
    assert "連携サマリー（MCS・更新 09/30・薬剤師）: 合成のサマリー本文" in body
    assert body.count("連携サマリー") == 1
