"""Synthetic contracts for default-off private previsit delivery."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import json
import threading
import uuid
from urllib.parse import parse_qs

import pytest

import mcs_requests
import notify_flush
import notify_sender
import private_previsit
from ledger import Ledger
from test_patient_context_v3_views import ctx  # noqa: F401
from semantic_testkit import _message


CFG = {"private_previsit": {"enabled": True, "owner_gate_approved": True}}
JST = timezone(timedelta(hours=9))
PREP_TIME = datetime(2026, 6, 1, 20, tzinfo=JST).timestamp()
VISIT_DAY = date(2026, 6, 2)
NOTE_ROW = {"group": "合成の申し送り", "quotes": ["合成記録です。"],
            "source_rest": "#1", "message_id": 1, "project_id": 1}


__all__ = ["ctx"]


def _schedule(records=(), *, fetch_state="complete", state="observed"):
    return {"project_id": 1, "records": list(records),
            "coverage": {"state": state, "fetch_state": fetch_state,
                         "truncated": False, "records_truncated": False,
                         "incomplete": 0, "deleted": 0, "invalid_source": 0,
                         "oversize": 0}}


def _planned(day="2026-06-02"):
    return {"project_id": 1, "role": "pharmacist", "role_text": "薬剤師",
            "state": "planned", "event_day": day, "assertion_only": True,
            "message_id": 1, "content_hash": "a" * 64, "span": [0, len("合成予定")],
            "quote": "合成予定"}


def _set_schedule(monkeypatch, schedule):
    source = {"value": deepcopy(schedule)}
    monkeypatch.setattr(private_previsit, "read_source_schedule",
                        lambda db, project_id, **kwargs: deepcopy(source["value"]))
    return source


def _set_view(monkeypatch):
    calls = []

    def view(db, project_id, section, **kwargs):
        calls.append((project_id, section, kwargs))
        return {"title": "合成患者", "head": [], "items": [dict(NOTE_ROW)],
                "page": 0, "pages": 1}

    monkeypatch.setattr(private_previsit.notify_views, "patient_context_list", view)
    return calls


def _command(command, **fields):
    return {"version": 1, "cmd": command, "command_id": str(uuid.uuid4()),
            "actor": "synthetic-reviewer", "human_confirmed": True,
            "project_id": 1, **fields}


def _assign(ledger, *, transport="lineworks", expected_revision=0):
    req = _command("ops.previsit_assignment_set", expected_revision=expected_revision,
                   staff_id="staff-synthetic-1", transport=transport,
                   account_scope={"profile": "synthetic", "application_id": "bot-synthetic",
                                  "team_id": "team-synthetic", "route_epoch": 4},
                   recipient_id="staff-synthetic@example.invalid",
                   reason="synthetic approval")
    return mcs_requests.apply_command(ledger, req)


def _register_date(ledger, day=VISIT_DAY):
    req = _command("ops.previsit_visit_date_set", expected_revision=0,
                   visit_date=day.isoformat(), reason="synthetic date approval")
    return mcs_requests.apply_command(ledger, req)


def _prepare(ctx, monkeypatch, *, registered=True, schedule=None, cfg=CFG):
    _set_view(monkeypatch)
    _set_schedule(monkeypatch, schedule or _schedule())
    assert _assign(ctx.db)["outcome"] == "applied"
    if registered:
        assert _register_date(ctx.db)["outcome"] == "applied"
    return private_previsit.prepare_night(ctx.db, cfg, now=PREP_TIME)


class StubPrivateSender:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def send(self, content, *, project_id, recipient_id, transport, account_scope,
             verify_authorization=None):
        self.calls.append((content, project_id, recipient_id, transport, account_scope))
        if verify_authorization is not None and not verify_authorization():
            return {"result": "not_sent", "error_code": "private_verification_failed"}
        outcome = self.outcomes.pop(0) if self.outcomes else {"result": "delivered"}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _at_jst(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), JST).timestamp()


def _event(ledger):
    row = ledger.db.execute("SELECT * FROM notify_outbox WHERE kind=?", (private_previsit.KIND,)).fetchone()
    assert row is not None
    return dict(row)


def test_default_off_and_utc_host_jst_0759_0800_and_success_no_resend(ctx, monkeypatch):
    calls = _set_view(monkeypatch)
    _set_schedule(monkeypatch, _schedule())
    assert _assign(ctx.db)["outcome"] == "applied"
    assert private_previsit.prepare_night(ctx.db, {}, now=PREP_TIME)["state"] == "disabled"
    assert ctx.db.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
    assert private_previsit.prepare_night(ctx.db, CFG,
                                         now=_at_jst(date(2026, 6, 1), 19, 59))["state"] == "outside_night_window"

    assert _register_date(ctx.db)["outcome"] == "applied"
    result = private_previsit.prepare_night(ctx.db, CFG, now=PREP_TIME)
    assert result["prepared"] == 1
    assert result["items"][0]["note"] == (
        "訪問前メモ: 合成患者\n"
        "予定日: 2026-06-02（日付根拠: 登録済み薬局訪問日）\n"
        "・合成の申し送り: 合成記録です。（#1）")
    assert calls and calls[-1][1] == "previsit"
    assert _event(ctx.db)["next_try"] == _at_jst(VISIT_DAY, 8)

    sender = StubPrivateSender()
    before_0800_utc = datetime(2026, 6, 1, 22, 59, tzinfo=timezone.utc).timestamp()
    assert private_previsit.dispatch_due(ctx.db, CFG, sender=sender,
                                         now=before_0800_utc) == []
    assert sender.calls == []
    at_0800_utc = datetime(2026, 6, 1, 23, 0, tzinfo=timezone.utc).timestamp()
    outcomes = private_previsit.dispatch_due(ctx.db, CFG, sender=sender, now=at_0800_utc)
    assert [row["state"] for row in outcomes] == ["accepted"]
    assert sender.calls[0][0] == result["items"][0]["note"]
    assert ctx.db.db.execute("SELECT state,attempts,accepted_ref FROM notify_outbox WHERE event_id=?",
                             (outcomes[0]["event_id"],)).fetchone()[:] == ("accepted", 1, "lineworks:201")
    assert private_previsit.dispatch_due(ctx.db, CFG, sender=sender, now=at_0800_utc) == []
    assert len(sender.calls) == 1


def test_registered_date_precedes_ambiguous_source_schedule(ctx, monkeypatch):
    ambiguous = _schedule([{"role": "pharmacist", "state": "schedule_mention_unconfirmed"}])
    result = _prepare(ctx, monkeypatch, registered=True, schedule=ambiguous)
    assert result["prepared"] == 1
    assert result["items"][0]["visit_date"] == VISIT_DAY.isoformat()


def test_rendered_note_identifies_patient_and_marks_visible_truncation(ctx, monkeypatch):
    monkeypatch.setattr(private_previsit.notify_views, "patient_context_list",
                        lambda *args, **kwargs: {
                            "title": "合成患者" + "詳細" * 100, "pages": 1,
                            "items": [
                                {"group": "短い記録", "quotes": ["確認事項"], "message_id": 1},
                                {"group": "長い記録", "quotes": ["合成" * 1000], "message_id": 1}]})
    note, _pins, _watermark = private_previsit._render_view(
        ctx.db.db, 1, {}, PREP_TIME, VISIT_DAY, None)
    title = note.splitlines()[0].removeprefix("訪問前メモ: ")
    assert title.startswith("合成患者") and title.endswith("…") and len(title) == 120
    assert "日付根拠: 原文の薬剤師予定" in note
    assert "（文字数上限のため一部省略）" in note
    assert len(note) <= private_previsit.MAX_NOTE_CHARS


def test_empty_previsit_view_is_skipped(ctx, monkeypatch):
    monkeypatch.setattr(private_previsit.notify_views, "patient_context_list",
                        lambda *args, **kwargs: {"title": "合成患者", "pages": 1, "items": []})
    with pytest.raises(private_previsit.PrevisitError, match="previsit_empty"):
        private_previsit._render_view(ctx.db.db, 1, {}, PREP_TIME, VISIT_DAY, None)


@pytest.mark.parametrize(("schedule", "reason"), [
    (_schedule([{"role": "pharmacist", "state": "schedule_mention_unconfirmed"}]),
     "schedule_ambiguous"),
    (_schedule([{**_planned(), "state": "cancelled"}]), "schedule_cancelled"),
    (_schedule([_planned(), _planned("2026-06-03")]), "schedule_conflict"),
    (_schedule([_planned()], fetch_state="incomplete"), "schedule_coverage_unknown"),
    (_schedule([]), "schedule_unknown"),
])
def test_source_schedule_fallback_requires_one_complete_pharmacy_plan(schedule, reason):
    with pytest.raises(private_previsit.PrevisitError, match=reason):
        private_previsit._fallback_day(schedule)


def test_source_schedule_fallback_accepts_only_explicit_pharmacy_plan():
    assert private_previsit._fallback_day(_schedule([_planned()])) == VISIT_DAY


def test_source_schedule_fallback_rejects_unbound_pharmacy_record():
    record = _planned()
    record["assertion_only"] = False
    with pytest.raises(private_previsit.PrevisitError, match="schedule_ambiguous"):
        private_previsit._fallback_day(_schedule([record]))


def test_missing_assignment_and_unsupported_transport_skip(ctx, monkeypatch):
    _set_view(monkeypatch)
    _set_schedule(monkeypatch, _schedule([_planned()]))
    assert private_previsit.prepare_night(ctx.db, CFG, now=PREP_TIME)["prepared"] == 0
    assert ctx.db.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
    assert _assign(ctx.db, transport="slack")["outcome"] == "applied"
    result = private_previsit.prepare_night(ctx.db, CFG, now=PREP_TIME)
    assert result["items"][0]["reason"] == "unsupported_transport"
    assert ctx.db.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["source_edit", "revoke", "archive"])
def test_changed_source_revoked_assignment_or_archive_suppresses_before_send(ctx, monkeypatch, change):
    _prepare(ctx, monkeypatch)
    if change == "source_edit":
        ctx.db.db.execute("UPDATE messages SET content_hash=? WHERE project_id=1 AND message_id=1",
                          ("f" * 64,))
        ctx.db.db.commit()
    elif change == "revoke":
        req = _command("ops.previsit_assignment_revoke", expected_revision=1,
                       reason="synthetic revoke")
        assert mcs_requests.apply_command(ctx.db, req)["outcome"] == "applied"
    else:
        ctx.db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=1")
        ctx.db.db.commit()
    sender = StubPrivateSender()
    result = private_previsit.dispatch_due(ctx.db, CFG, sender=sender,
                                           now=_at_jst(VISIT_DAY, 8))
    assert result[0]["state"] == "suppressed"
    assert sender.calls == []


def test_cancelled_source_schedule_stales_prepared_fallback(ctx, monkeypatch):
    schedule = {"value": _schedule([_planned()])}
    _set_view(monkeypatch)
    _set_schedule(monkeypatch, schedule["value"])
    _assign(ctx.db)
    result = private_previsit.prepare_night(ctx.db, CFG, now=PREP_TIME)
    assert result["prepared"] == 1
    monkeypatch.setattr(private_previsit, "read_source_schedule",
                        lambda db, project_id, **kwargs: _schedule([
                            {"role": "pharmacist", "state": "cancelled"}]))
    sender = StubPrivateSender()
    outcome = private_previsit.dispatch_due(ctx.db, CFG, sender=sender,
                                            now=_at_jst(VISIT_DAY, 8))
    assert outcome[0]["state"] == "suppressed"
    assert sender.calls == []


def test_retry_uses_same_event_and_bounded_backoff_then_accepts(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    sender = StubPrivateSender([{"result": "not_sent", "error_code": "rate_limited"},
                               {"result": "delivered"}])
    now = _at_jst(VISIT_DAY, 8)
    first = private_previsit.dispatch_due(ctx.db, CFG, sender=sender, now=now)
    row = ctx.db.db.execute("SELECT state,attempts,next_try FROM notify_outbox WHERE event_id=?",
                            (first[0]["event_id"],)).fetchone()
    assert first[0]["state"] == "retry" and row["state"] == "failed"
    assert row["attempts"] == 1 and row["next_try"] == now + 60
    assert private_previsit.dispatch_due(ctx.db, CFG, sender=sender, now=now + 59) == []
    second = private_previsit.dispatch_due(ctx.db, CFG, sender=sender, now=now + 60)
    assert second[0]["state"] == "accepted"
    assert second[0]["event_id"] == first[0]["event_id"]
    assert len(sender.calls) == 2


def test_permanent_private_rejection_is_held_without_retry(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    sender = StubPrivateSender([{"result": "not_sent", "error_code": "http_4xx_rejected"}])
    now = _at_jst(VISIT_DAY, 8)
    result = private_previsit.dispatch_due(ctx.db, CFG, sender=sender, now=now)
    row = ctx.db.db.execute("SELECT state,attempts,next_try,progress FROM notify_outbox WHERE event_id=?",
                            (result[0]["event_id"],)).fetchone()
    assert result[0]["state"] == "held"
    assert row["state"] == "failed" and row["attempts"] == 0 and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "private_send_rejected"
    assert len(sender.calls) == 1


def test_authorization_change_during_provider_verification_holds_claim(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)

    class RevokingSender:
        def send(self, _content, *, verify_authorization=None, **_kwargs):
            revoked = mcs_requests.apply_command(
                ctx.db, _command("ops.previsit_assignment_revoke", expected_revision=1,
                                 reason="synthetic race revoke"))
            assert revoked["outcome"] == "applied"
            assert verify_authorization is not None and verify_authorization() is False
            return {"result": "not_sent", "error_code": "private_verification_failed"}

    now = _at_jst(VISIT_DAY, 8)
    result = private_previsit.dispatch_due(ctx.db, CFG, sender=RevokingSender(), now=now)
    row = ctx.db.db.execute("SELECT state,attempts,next_try,progress FROM notify_outbox WHERE event_id=?",
                            (result[0]["event_id"],)).fetchone()
    assert result[0]["state"] == "held"
    assert row["state"] == "failed" and row["attempts"] == 0 and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "private_send_rejected"


def test_missing_private_provider_holds_without_shared_fallback(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    import notify_spool
    monkeypatch.setattr(notify_spool, "restore_pending", lambda root: None)
    monkeypatch.setattr(private_previsit, "_provider", lambda root: None)
    now = _at_jst(VISIT_DAY, 8)
    event_id = _event(ctx.db)["event_id"]
    result = private_previsit.dispatch_event(ctx.db, _event(ctx.db), CFG, now=now)
    row = ctx.db.db.execute("SELECT state,attempts,next_try,progress FROM notify_outbox WHERE event_id=?",
                            (event_id,)).fetchone()
    assert result["state"] == "held"
    assert row["state"] == "failed" and row["attempts"] == 0 and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "private_provider_unavailable"


def test_unknown_result_and_expired_crash_claim_are_held(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    now = _at_jst(VISIT_DAY, 8)
    sender = StubPrivateSender([RuntimeError("synthetic unknown")])
    result = private_previsit.dispatch_due(ctx.db, CFG, sender=sender, now=now)
    row = ctx.db.db.execute("SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
                            (result[0]["event_id"],)).fetchone()
    assert result[0]["state"] == "held" and row["next_try"] is None
    assert json.loads(row["progress"])["hold_reason"] == "private_send_unknown"
    assert len(sender.calls) == 1

    event = _event(ctx.db)
    payload = json.loads(event["payload"])
    ctx.db.db.execute("UPDATE notify_outbox SET state='pending',progress=?,next_try=? WHERE event_id=?",
                      (json.dumps({"private_previsit": {"state": "sending",
                                                         "started_at": now - 121,
                                                         "logical_key": payload["logical_key"]}}),
                       now - 1, event["event_id"]))
    ctx.db.db.commit()
    crash = StubPrivateSender()
    held = private_previsit.dispatch_due(ctx.db, CFG, sender=crash, now=now)
    assert held[0]["state"] == "held" and crash.calls == []
    marker = json.loads(ctx.db.db.execute("SELECT progress FROM notify_outbox WHERE event_id=?",
                                          (event["event_id"],)).fetchone()[0])
    assert marker["hold_reason"] == "private_send_crash_unknown"


def test_next_day_event_is_stale_and_never_replayed(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    sender = StubPrivateSender()
    result = private_previsit.dispatch_due(ctx.db, CFG, sender=sender,
                                           now=_at_jst(date(2026, 6, 3), 8))
    assert result[0]["state"] == "suppressed"
    assert sender.calls == []


def test_malformed_outbox_payload_and_progress_are_held(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    first = _event(ctx.db)
    ctx.db.db.execute("UPDATE notify_outbox SET payload='{' WHERE event_id=?", (first["event_id"],))
    ctx.db.db.commit()
    sender = StubPrivateSender()
    held = private_previsit.dispatch_due(ctx.db, CFG, sender=sender,
                                         now=_at_jst(VISIT_DAY, 8))
    assert held[0]["state"] == "held" and sender.calls == []
    first_payload = ctx.db.db.execute("SELECT payload FROM notify_outbox WHERE event_id=?",
                                      (first["event_id"],)).fetchone()[0]
    assert first_payload == "{"

    second = _event(ctx.db)
    ctx.db.db.execute("UPDATE notify_outbox SET payload=?,state='pending',progress='[]',next_try=? "
                      "WHERE event_id=?",
                      (first["payload"], _at_jst(VISIT_DAY, 8), second["event_id"]))
    ctx.db.db.commit()
    progress_hold = private_previsit.dispatch_due(ctx.db, CFG, sender=sender,
                                                  now=_at_jst(VISIT_DAY, 8))
    assert progress_hold[0]["state"] == "held" and sender.calls == []


def test_restore_pending_defers_without_consuming_attempt(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    import notify_spool
    monkeypatch.setattr(notify_spool, "restore_pending", lambda root: {"state": "pending"})
    now = _at_jst(VISIT_DAY, 8)
    sender = StubPrivateSender()
    result = private_previsit.dispatch_due(ctx.db, CFG, sender=sender, now=now)
    row = ctx.db.db.execute("SELECT state,attempts,next_try FROM notify_outbox WHERE event_id=?",
                            (result[0].get("event_id", _event(ctx.db)["event_id"]),)).fetchone()
    assert result[0]["state"] == "restore_pending" and row["attempts"] == 0
    assert row["state"] == "pending" and row["next_try"] == now + 300
    assert sender.calls == []


def test_preparation_deduplicates_concurrent_runners_and_restart(ctx, monkeypatch):
    _set_view(monkeypatch)
    _set_schedule(monkeypatch, _schedule())
    _assign(ctx.db)
    _register_date(ctx.db)
    path = ctx.db.db.execute("PRAGMA database_list").fetchone()[2]

    def prepare_in_new_connection():
        ledger = Ledger(path)
        try:
            return private_previsit.prepare_night(ledger, CFG, now=PREP_TIME)
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare_in_new_connection(), range(2)))
    assert sorted(item["state"] for result in results for item in result["items"]) == [
        "duplicate", "prepared"]
    reopened = Ledger(path)
    try:
        again = private_previsit.prepare_night(reopened, CFG, now=PREP_TIME)
        assert again["items"][0]["state"] == "duplicate"
        assert reopened.db.execute("SELECT COUNT(*) FROM notify_outbox WHERE kind=?",
                                   (private_previsit.KIND,)).fetchone()[0] == 1
    finally:
        reopened.close()


def test_concurrent_dispatch_has_one_private_send(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    path = ctx.db.db.execute("PRAGMA database_list").fetchone()[2]
    second = Ledger(path)
    entered, release = threading.Event(), threading.Event()

    class SlowSender(StubPrivateSender):
        def send(self, content, **kwargs):
            self.calls.append(content)
            entered.set()
            assert release.wait(timeout=5)
            if not kwargs["verify_authorization"]():
                return {"result": "not_sent", "error_code": "private_verification_failed"}
            return {"result": "delivered"}

    sender = SlowSender()
    now = _at_jst(VISIT_DAY, 8)

    def first_runner():
        runner = Ledger(path)
        try:
            return private_previsit.dispatch_due(runner, CFG, sender=sender, now=now)
        finally:
            runner.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(first_runner)
            assert entered.wait(timeout=5)
            concurrent = private_previsit.dispatch_due(second, CFG, sender=sender, now=now)
            assert concurrent[0]["state"] == "sending"
            release.set()
            assert first.result(timeout=5)[0]["state"] == "accepted"
        assert len(sender.calls) == 1
    finally:
        release.set()
        second.close()


def test_normal_flush_intercepts_private_kind_before_shared_routing(ctx, monkeypatch):
    _prepare(ctx, monkeypatch)
    now = _at_jst(VISIT_DAY, 8)
    event = _event(ctx.db)
    ctx.db.db.execute("UPDATE notify_outbox SET next_try=? WHERE event_id=?",
                      (now - 1, event["event_id"]))
    ctx.db.db.commit()
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    monkeypatch.setattr(notify_sender, "send_argv",
                        lambda *args, **kwargs: pytest.fail("private event reached shared sender"))
    result = notify_flush.flush(ctx.db)
    row = ctx.db.db.execute("SELECT state,progress FROM notify_outbox WHERE event_id=?",
                            (event["event_id"],)).fetchone()
    assert row["state"] == "failed"
    assert json.loads(row["progress"])["hold_reason"] == "private_previsit_rollout_gate_closed"
    assert result["sent"] == 0


def test_full_synthetic_path_uses_real_schedule_view_provider_and_lineworks_wire(
        ctx, monkeypatch, tmp_path):
    from adapters.lineworks import private_previsit as lineworks_provider
    from adapters.lineworks.delivery import Sender
    from lineworks_client_testkit import TOKEN, Wire, client as lineworks_client
    import notify_spool

    recipient = "staff-synthetic@example.invalid"
    account_scope = {"profile": "synthetic", "application_id": "2000001",
                     "team_id": "team-synthetic", "route_epoch": 4}
    assert mcs_requests.apply_command(ctx.db, _command(
        "ops.previsit_assignment_set", expected_revision=0, staff_id="staff-synthetic-1",
        transport="lineworks", account_scope=account_scope, recipient_id=recipient,
        reason="synthetic approval"))["outcome"] == "applied"
    ctx.db.db.execute("UPDATE patients SET patient_name='合成患者' WHERE project_id=1")
    source_body = "薬剤師が2026年6月2日に本人への服薬指導を予定しています。"
    ctx.db.save_messages([_message(40, body=source_body)], project_id=1)
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=? WHERE project_id=1 AND message_id=40",
                      (datetime(2026, 6, 1, 12, tzinfo=JST).timestamp(),))
    ctx.db.db.commit()

    schedule = private_previsit._schedule(ctx.db.db, 1)
    assert private_previsit._fallback_day(schedule) == VISIT_DAY
    assert any(record["role"] == "pharmacist" and record["state"] == "planned"
               for record in schedule["records"])
    view = private_previsit.notify_views.patient_context_list(
        ctx.db.db, 1, "previsit", cfg={}, since=PREP_TIME - 30 * 86400, now=PREP_TIME)
    assert any(source_body in quote for row in view["items"] for quote in row.get("quotes", []))

    prepared = private_previsit.prepare_night(ctx.db, CFG, now=PREP_TIME)
    assert prepared["prepared"] == 1
    note = prepared["items"][0]["note"]
    assert note.startswith("訪問前メモ: 合成患者 — 訪問前の確認ノート")
    assert "日付根拠: 原文の薬剤師予定" in note and source_body in note

    lineworks_root = tmp_path / "synthetic-lineworks"
    lineworks_root.mkdir()
    scope = {**account_scope, "transport": "lineworks", "channel_id": "synthetic-shared-channel",
             "allowed_user_ids": frozenset({recipient}), "project_ids": frozenset({1}),
             "data_root": str(lineworks_root / "data")}
    wire = Wire([(200, TOKEN), (201, b"")])
    api = lineworks_client(wire)
    monkeypatch.setattr(lineworks_provider, "settings",
                        lambda root, require_interactive=False: dict(scope))
    monkeypatch.setattr(lineworks_provider, "load_credentials",
                        lambda root, current: (api, "synthetic-only"))
    route_epochs = []
    monkeypatch.setattr(Sender, "route_current", lambda self, spec:
                        route_epochs.append(spec["delivery"]["route_epoch"]) or True)
    monkeypatch.setattr(notify_spool, "restore_pending", lambda root: None)
    provider = lineworks_provider.PrivatePrevisitProvider(lineworks_root)
    outcome = private_previsit.dispatch_due(
        ctx.db, CFG, sender=provider, now=_at_jst(VISIT_DAY, 8))

    assert outcome[0]["state"] == "accepted"
    assert route_epochs == [4]
    assert len(wire.calls) == 2
    assert wire.calls[1][1].endswith("/bots/2000001/users/staff-synthetic%40example.invalid/messages")
    assert "/channels/" not in wire.calls[1][1]
    assert wire.calls[1][2]["Authorization"] == "Bearer token-synthetic"
    assert json.loads(wire.calls[1][3]) == {"content": {"type": "text", "text": note}}
    assert parse_qs(wire.calls[0][3].decode())["scope"] == ["bot.message"]
