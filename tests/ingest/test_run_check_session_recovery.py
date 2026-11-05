"""In-run login recovery budgets, replay journals and outage catch-up contracts."""
import collection_stages
import health_report
import mcs_contract
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import job_ops
import ledger
import run_check
from ingest_testkit import _ledger, _message, _unread_patient
from run_check_stage_testkit import _always_expired, _expire_once, _health_unread_at
from run_check_stage_testkit import _no_disk_guard as _no_disk_guard


def test_session_expired_alert_throttled(tmp_path):
    """A dead session alerts once per hour, not once per tick — the run
    row and health file still record every expiry."""
    db = _ledger(tmp_path)
    assert collection_stages.queue_session_expired(db, 1, "d") is True
    assert collection_stages.queue_session_expired(db, 2, "d") is False
    db.db.execute(
        "UPDATE notify_outbox SET created_at=?"
        " WHERE kind='session_expired'",
        (time.time() - collection_stages.ALERT_MIN_INTERVAL_S - 1,))
    db.db.commit()
    assert collection_stages.queue_session_expired(db, 3, "d") is True
    n = db.db.execute(
        "SELECT COUNT(*) c FROM notify_outbox"
        " WHERE kind='session_expired'").fetchone()["c"]
    assert n == 2
    db.close()


# ---------- in-run session recovery ----------

def test_with_relogin_recovers_and_replays_stage(tmp_path):
    """A mid-stage SessionExpired triggers one auto_login; on success the
    stage replays and a session_recovered notice is queued — the channel
    sees the blip and its resolution, not a manual-login demand."""
    db = _ledger(tmp_path)
    calls = []

    def stage():
        calls.append(1)
        if len(calls) == 1:
            raise mcs_contract.SessionExpired(status=403)
        return "done"

    adapter = SimpleNamespace(auto_login=lambda **kw: "ok")
    result = {"errors": [], "run_id": 7}
    assert collection_stages.with_relogin(
        adapter, db, result, "history_jobs", stage) == "done"
    assert len(calls) == 2
    row = db.db.execute(
        "SELECT kind,payload FROM notify_outbox").fetchone()
    assert row["kind"] == "session_recovered"
    assert "history_jobs" in json.loads(row["payload"])["detail"]
    assert result["relogin_attempts"] == [
        {"stage": "history_jobs", "error": "session_expired(status=403)",
         "state": "ok"}]
    db.close()


def test_relogin_replay_drops_the_aborted_attempts_records(tmp_path):
    """Recovered mid-stage expiry: the replay re-records the stage, so the
    aborted attempt's incomplete/errors/messages must not linger (F10)."""
    db = _ledger(tmp_path)
    expired = []

    def replies(m):
        if m.project_id == 2 and not expired:
            expired.append(1)
            raise mcs_contract.SessionExpired(status=403)
        return mcs_contract.ReplyBatch([], [])
    adapter = SimpleNamespace(
        auto_login=lambda **kw: "ok",
        list_unread=lambda: mcs_contract.UnreadSnapshot(
            timestamp=123, patients=[_unread_patient(1), _unread_patient(2)]),
        fetch_unread_messages=lambda pid, ts: mcs_contract.MessageBatch(
            messages=[_message(mid=pid, project_id=pid, unread=True)], reached=True),
        fetch_unread_replies=replies,
        mark_patient_read=lambda *a, **k: None)
    result = {"errors": [], "incomplete": [], "messages": 0,
              "new_messages": 0, "marked_read": [], "run_id": 7}
    collection_stages.with_relogin(
        adapter, db, result, "unread", collection_stages.stage_unread, adapter, db,
        result, time.monotonic() + 30,
        db.begin_run(None), mark_read=False)
    assert expired == [1]
    assert result["incomplete"] == [] and result["errors"] == []
    assert result["messages"] == 2                 # not double-counted
    assert result["new_messages"] == 2
    assert [a["state"] for a in result["relogin_attempts"]] == ["ok"]
    assert health_report.collection(result)["collection"] == "ok"
    db.close()


def test_relogin_replay_keeps_job_drain_failures(tmp_path):
    """A reply job deferred by job_retry in the aborted attempt is not due
    on replay, so its recorded failure must survive the relogin."""
    db = _ledger(tmp_path)
    db.ensure_patient(80)
    db.job_add("reply", 80, message_id=11, parent_id=10)
    db.job_add("reply", 80, message_id=21, parent_id=20)
    expired = []

    def fetch_thread(pid, parent):
        if parent == 10:
            raise mcs_contract.MCSError("network_error", retryable=True)
        if not expired:
            expired.append(1)
            raise mcs_contract.SessionExpired(status=403)
        return []
    adapter = SimpleNamespace(
        auto_login=lambda **kw: "ok", fetch_thread=fetch_thread,
        fetch_thread_window=lambda pid, parent, start_page=1: mcs_contract.MessageBatch(
            fetch_thread(pid, parent), pages=1, reached=True))
    result = {"errors": [], "run_id": 7}
    collection_stages.with_relogin(
        adapter, db, result, "reply_jobs", job_ops.run_reply_jobs,
        adapter, db, result, time.monotonic() + 60)
    assert expired == [1]
    assert result["errors"] == ["reply 11: network_error"]
    db.close()


def test_relogin_journals_heal_facts_without_seeding(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _health_unread_at(tmp_path, monkeypatch, time.time() - 3 * 3600)
    adapter = SimpleNamespace(auto_login=lambda **kw: "ok", healed_tabs=1,
                              chrome_restarted=True)
    result = {"errors": [], "run_id": 9}
    assert collection_stages.attempt_relogin(
        adapter, db, result, "unread", mcs_contract.SessionExpired(status=403)) == "ok"
    assert result["relogin_attempts"][0]["healed_tabs"] == 1
    assert result["relogin_attempts"][0]["chrome_restarted"] is True
    assert db.db.execute("SELECT COUNT(*) FROM fetch_jobs").fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("gap_s,outage", [(3 * 3600, True), (600, False), (None, False)])
def test_first_collecting_run_after_any_outage_announces_and_seeds(
        tmp_path, monkeypatch, gap_s, outage):
    """Any outage (session, sleep, network, stopped scheduler) leaves
    unread_at behind. The next collecting run's backfill announces every
    newly stored row — posts read elsewhere are no longer unread — and
    the notifying head walk is queued for whatever backfill does not reach."""
    db = _ledger(tmp_path)
    now = time.time()
    for pid, archived in ((1, 0), (2, 0), (3, 1)):
        db.db.execute("INSERT INTO patients(project_id,patient_name,is_archived)"
                      " VALUES(?,?,?)", (pid, f"synthetic-{pid}", archived))
    db.db.commit()
    _health_unread_at(tmp_path, monkeypatch,
                      None if gap_s is None else now - gap_s)
    seen = {}
    monkeypatch.setattr(collection_stages, 'stage_unread', lambda *a, **k: None)
    monkeypatch.setattr(collection_stages, 'stage_backfill',
                        lambda *a, **k: seen.update(k))
    args = SimpleNamespace(mark_read=False, jobs_only=False, no_backfill=False)
    result = {"errors": [], "run_id": 3}
    run_check._stage_fetch(None, db, args, {"self_posts": False}, result,
                           time.monotonic() + 300, 3, False, 86400)
    assert seen["notify_all_new"] is outage
    jobs = {pid: db.job_pending("history_head", pid) for pid in (1, 2, 3)}
    if not outage:
        assert "outage_since" not in result and not any(jobs.values())
        db.close()
        return
    assert result["outage_since"] == pytest.approx(now - gap_s)
    assert result["outage_catchup"] == 2 and jobs[3] is None   # archived skipped
    for pid in (1, 2):
        payload = json.loads(jobs[pid]["payload"])
        assert payload["notify"] is True and payload["trickle"] is False
    db.close()


def test_jobs_only_runs_never_open_an_outage_catchup(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _health_unread_at(tmp_path, monkeypatch, time.time() - 3 * 3600)
    result = {"errors": []}
    run_check._stage_fetch(None, db, SimpleNamespace(jobs_only=True), {}, result,
                           time.monotonic() + 300, 1, False, None)
    assert "outage_since" not in result
    db.close()


def test_with_relogin_failed_login_escalates_with_state(tmp_path):
    """A failed auto_login aborts with 'auto_login=<state>' detail so the
    session_expired alert carries the attempt's outcome."""
    db = _ledger(tmp_path)
    adapter = SimpleNamespace(auto_login=lambda **kw: "keychain_locked")
    result = {"errors": [], "run_id": 1}

    def stage():
        raise mcs_contract.SessionExpired(status=403)

    with pytest.raises(mcs_contract.SessionExpired) as exc:
        collection_stages.with_relogin(adapter, db, result, "discovery", stage)
    assert exc.value.detail == "auto_login=keychain_locked"
    assert db.db.execute("SELECT COUNT(*) FROM notify_outbox"
                         ).fetchone()[0] == 0
    db.close()


def test_with_relogin_no_double_attempt_for_stage_owned_login(tmp_path):
    """A stage that already ran its own auto_login (detail
    'auto_login=…') must not be re-attempted by the wrapper — one try
    per failure point."""
    db = _ledger(tmp_path)
    calls = []
    adapter = SimpleNamespace(
        auto_login=lambda **kw: calls.append(1) or "ok")
    result = {"errors": [], "run_id": 1}

    def stage():
        raise mcs_contract.SessionExpired("auto_login=manual_required")

    with pytest.raises(mcs_contract.SessionExpired) as exc:
        collection_stages.with_relogin(adapter, db, result, "unread", stage)
    assert exc.value.detail == "auto_login=manual_required"
    assert calls == []
    db.close()


def test_with_relogin_reexpiry_after_recovery_aborts(tmp_path):
    """Recovery followed by an immediate second expiry aborts — the
    wrapper never loops on a flapping session."""
    db = _ledger(tmp_path)
    calls = []
    adapter = SimpleNamespace(auto_login=lambda **kw: "ok")
    result = {"errors": [], "run_id": 1}

    def stage():
        calls.append(1)
        raise mcs_contract.SessionExpired(status=403)

    with pytest.raises(mcs_contract.SessionExpired) as exc:
        collection_stages.with_relogin(adapter, db, result, "unread", stage)
    assert exc.value.detail == "auto_login=ok_then_expired"
    assert len(calls) == 2
    db.close()


def test_with_relogin_budget_caps_attempts_per_run(tmp_path):
    """A session dying across several stages stops re-attempting after
    RELOGIN_MAX_PER_RUN — auth thrash never eats the run."""
    db = _ledger(tmp_path)
    logins = []
    adapter = SimpleNamespace(
        auto_login=lambda **kw: logins.append(1) or "ok")
    result = {"errors": [], "run_id": 1}

    def once_fail():
        state = {"n": 0}

        def stage():
            state["n"] += 1
            if state["n"] == 1:
                raise mcs_contract.SessionExpired(status=403)
            return "done"
        return stage

    for _ in range(collection_stages.RELOGIN_MAX_PER_RUN):
        assert collection_stages.with_relogin(
            adapter, db, result, "unread", once_fail()) == "done"
    assert len(logins) == collection_stages.RELOGIN_MAX_PER_RUN

    def expired():
        raise mcs_contract.SessionExpired(status=403)

    with pytest.raises(mcs_contract.SessionExpired) as exc:
        collection_stages.with_relogin(adapter, db, result, "unread", expired)
    assert exc.value.detail == ""      # raw — no further attempt made
    assert len(logins) == collection_stages.RELOGIN_MAX_PER_RUN
    db.close()


def test_session_recovered_alert_not_throttled(tmp_path):
    """session_recovered is deliberately NOT throttled: it resolves a
    possibly-visible session_expired alert, so suppressing it would
    leave a stale manual-login demand standing in the channel."""
    db = _ledger(tmp_path)
    collection_stages._alert_session_recovered(db, 1, "d")
    collection_stages._alert_session_recovered(db, 2, "d")
    rows = db.db.execute(
        "SELECT kind FROM notify_outbox WHERE kind='session_recovered'"
        ).fetchall()
    assert len(rows) == 2
    # session_expired itself stays throttled
    assert collection_stages.queue_session_expired(db, 3, "d") is True
    assert collection_stages.queue_session_expired(db, 4, "d") is False
    db.close()


def test_attempt_relogin_journals_exception_type_without_private_detail(tmp_path):
    """Login failure records its type without copying credential-bearing exception text."""
    db = _ledger(tmp_path)

    def boom(**kw):
        raise RuntimeError("SYNTHETIC_PRIVATE_CANARY")

    adapter = SimpleNamespace(auto_login=boom)
    result = {"errors": [], "run_id": 1}
    state = collection_stages.attempt_relogin(
        adapter, db, result, "unread",
        mcs_contract.SessionExpired(status=403))
    assert state == "failed"
    assert result["relogin_attempts"][-1]["detail"] == \
        "RuntimeError"
    assert "SYNTHETIC_PRIVATE_CANARY" not in json.dumps(result)
    db.close()


def test_tick_recovers_session_mid_run_and_notifies(tmp_path, monkeypatch,
                                                  capsys):
    """A session that dies inside a tick is re-logged-in at the failure
    point and the run completes; the session_recovered intent is queued
    for delivery (flush suppressed by --no-notify)."""
    import maintenance
    import self_identity
    import notify_cards
    import notify_spool
    import notify_cmds

    class Adapter:
        def __init__(self, **kwargs):
            self.list_calls = 0
            self.logins = 0

        def set_deadline(self, deadline):
            pass

        def list_unread(self):
            self.list_calls += 1
            if self.list_calls == 1:
                raise mcs_contract.SessionExpired(status=403)
            return mcs_contract.UnreadSnapshot(timestamp=123, patients=[])

        def auto_login(self, **kw):
            self.logins += 1
            return "ok"

        def self_profile(self):
            return {}

        def station_staffs(self, stations):
            return []

    adapter = Adapter()
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        '{"deep_history":false,"semantic":{"mode":"off"}}',
        encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "HEALTH_FILE": data / "health.json", "CONF_PATH": config,
        "MCSAdapter": lambda *a, **k: adapter,
        "stage_derive": lambda *a, **k: None,
    }.items():
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, str(value)
                            if isinstance(value, Path) else value)
    for name in ("drain_commands", "seed_discovery", "run_discovery",
                 "run_reply_jobs", "run_history_jobs", "run_reconcile_jobs",
                 "seed_trickle"):
        monkeypatch.setattr(job_ops, name, lambda *a, **k: None)
    for name in ("ensure_dirs", "recover", "sweep", "publish_flags",
                 "gc", "clear_snapshot_dirty"):
        monkeypatch.setattr((notify_spool if name in {'recover', 'publish_flags', 'ensure_dirs'} else notify_cards), name, lambda *a, **k: None)
    monkeypatch.setattr(notify_cmds, "drain_int_commands",
                        lambda *a, **k: None)
    monkeypatch.setattr(self_identity, "record_self_profile", lambda *a: False)
    monkeypatch.setattr(maintenance, "daily_backup", lambda *a: None)
    monkeypatch.setattr(maintenance, "rotate_log", lambda *a: None)
    monkeypatch.setattr(maintenance, "prune_attachments", lambda *a: 0)
    monkeypatch.setattr(maintenance, "publish_snapshot", lambda *a, **k: True)
    monkeypatch.setattr(
        sys, "argv", ["run_check", "--no-notify", "--no-backfill"])

    assert run_check.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["relogin_attempts"][0]["state"] == "ok"
    assert adapter.logins == 1 and adapter.list_calls == 2
    db = ledger.Ledger(str(data / "ledger.db"))
    row = db.db.execute(
        "SELECT kind,payload,state FROM notify_outbox").fetchone()
    assert row["kind"] == "session_recovered" and row["state"] == "pending"
    assert db.db.execute(
        "SELECT status FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()[0] == "ok"
    db.close()


def test_attempt_relogin_owns_run_budget(tmp_path):
    """The budget check lives inside the single auto_login entry point:
    with the journal already at the cap a request is declined as
    'budget_exhausted' — no adapter call, no journal entry, no
    session_recovered notice."""
    db = _ledger(tmp_path)
    logins = []
    adapter = SimpleNamespace(
        auto_login=lambda **kw: logins.append(1) or "ok")
    result = {"errors": [], "run_id": 1,
              "relogin_attempts": [
                  {"stage": "earlier", "error": "e", "state": "ok"}
                  for _ in range(collection_stages.RELOGIN_MAX_PER_RUN)]}
    assert collection_stages.attempt_relogin(
        adapter, db, result, "run",
        mcs_contract.SessionExpired(status=403)) == "budget_exhausted"
    assert logins == []
    assert len(result["relogin_attempts"]) == collection_stages.RELOGIN_MAX_PER_RUN
    assert db.db.execute("SELECT COUNT(*) FROM notify_outbox"
                         ).fetchone()[0] == 0
    db.close()


def test_main_relogin_budget_covers_run_boundary(tmp_path, monkeypatch,
                                                capsys):
    """RELOGIN_MAX_PER_RUN bounds the whole run including the run-boundary
    attempt: after three recovered stage failures a fourth expiry that
    escalates raw gets 'budget_exhausted' at the boundary — no fourth
    auto_login, no extra session_recovered, a session_expired alert,
    exit 2, and the exhaustion is recorded in the run row and health."""
    import maintenance
    import self_identity
    import notify_cards
    import notify_spool
    import notify_cmds

    class Adapter:
        def __init__(self):
            self.logins = 0

        def set_deadline(self, deadline):
            pass

        def list_unread(self):
            return mcs_contract.UnreadSnapshot(timestamp=123, patients=[])

        def auto_login(self, **kw):
            self.logins += 1
            return "ok"

        def self_profile(self):
            return {}

        def station_staffs(self, stations):
            return []

    adapter = Adapter()
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        '{"deep_history":false,"semantic":{"mode":"off"}}',
        encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "HEALTH_FILE": data / "health.json", "CONF_PATH": config,
        "MCSAdapter": lambda *a, **k: adapter,
        "stage_derive": lambda *a, **k: None,
    }.items():
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, str(value)
                            if isinstance(value, Path) else value)
    for name in ("drain_commands", "seed_discovery", "run_discovery",
                 "run_reply_jobs", "run_history_jobs", "run_reconcile_jobs",
                 "seed_trickle"):
        monkeypatch.setattr(job_ops, name, lambda *a, **k: None)
    for name in ("ensure_dirs", "recover", "sweep", "publish_flags",
                 "gc", "clear_snapshot_dirty"):
        monkeypatch.setattr((notify_spool if name in {'recover', 'publish_flags', 'ensure_dirs'} else notify_cards), name, lambda *a, **k: None)
    monkeypatch.setattr(notify_cmds, "drain_int_commands",
                        lambda *a, **k: None)
    monkeypatch.setattr(self_identity, "record_self_profile", lambda *a: False)
    monkeypatch.setattr(maintenance, "daily_backup", lambda *a: None)
    monkeypatch.setattr(maintenance, "rotate_log", lambda *a: None)
    monkeypatch.setattr(maintenance, "prune_attachments", lambda *a: 0)
    monkeypatch.setattr(maintenance, "publish_snapshot", lambda *a, **k: True)
    # three wrapped stages each recover once, spending the whole budget;
    # the fourth expiry escalates raw (wrapper declined) to the boundary
    monkeypatch.setattr(collection_stages, 'stage_unread', _expire_once())
    monkeypatch.setattr(collection_stages, 'stage_backfill', _expire_once())
    monkeypatch.setattr(job_ops, "run_discovery", _expire_once())
    monkeypatch.setattr(job_ops, "run_reply_jobs", _always_expired)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])

    assert run_check.main() == 2
    out = json.loads(capsys.readouterr().out)
    assert adapter.logins == collection_stages.RELOGIN_MAX_PER_RUN
    assert [a["stage"] for a in out["relogin_attempts"]] == [
        "unread", "backfill", "discovery"]
    assert "auto_login=budget_exhausted" in out["errors"][-1]
    health = json.loads((data / "health.json").read_text(encoding="utf-8"))
    assert any("auto_login=budget_exhausted" in e
               for e in health["errors"])
    db = ledger.Ledger(str(data / "ledger.db"))
    kinds = [r[0] for r in db.db.execute(
        "SELECT kind FROM notify_outbox ORDER BY rowid")]
    # session_recovered is intentionally unthrottled: one per REAL
    # recovery, none for the declined boundary attempt
    assert kinds == ["session_recovered"] * collection_stages.RELOGIN_MAX_PER_RUN \
        + ["session_expired"]
    row = db.db.execute(
        "SELECT status, error FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "session_expired"
    assert "auto_login=budget_exhausted" in row[1]
    db.close()
