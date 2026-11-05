"""Tick lifecycle, writer fencing, command entry and deadline contracts."""
import extract_llm_resident
import extract_llm_runner
import writer_lock
import restore_evidence
import collection_stages
import update_launch
import health_report
import mcs_contract
import json
import os
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import pytest
import extract_llm
import job_ops
import ledger
import mcs_adapter
import notify_flush
import run_check
from run_check_stage_testkit import _point_run_check_at
from run_check_stage_testkit import _no_disk_guard as _no_disk_guard


def test_tick_real_storage_snapshot_and_replay(tmp_path, monkeypatch, capsys):
    import maintenance

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if path == "/projects":
                return {"projects": [{"id": 1, "is_unread": True,
                                      "karte": {"id": 5}}],
                        "paginate": {"has_next": False, "timestamp": 123}}
            if path == "/kartes/5/memo_summary":
                return {"memo_summary": {"is_editable": True, "is_read": True,
                                         "read_style": "single_line"}}
            assert path == "/projects/1/messages"
            assert params["keep_read_status"] == 1
            return {"messages": [{"id": 1, "comment": "synthetic body",
                                  "created_at": "2026-09-19T00:00:00+09:00"}],
                    "paginate": {"has_next": False}}

        def _request(self, *args, **kwargs):
            pytest.fail("external request forbidden")

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text('{"deep_history":false}', encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "CONF_PATH": config, "CACHE": tmp_path / "absent-token.json",
    }.items():
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, str(value))
    for name, value in {
        "BACKUP_DIR": data / "backups", "SNAPSHOT_DIR": data / "snapshots",
        "LOGFILE": data / "run.log",
    }.items():
        monkeypatch.setattr(maintenance, name, str(value))
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, "drain_commands", lambda db, result:
                        drain(db, result, str(data / "cmd")))
    monkeypatch.setattr(run_check, "MCSAdapter", Adapter)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: None)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: False)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    monkeypatch.setattr(notify_flush, "flush", lambda *a, **k:
                        pytest.fail("notification forbidden"))
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify", "--no-backfill"])

    assert run_check.main() == 0
    import mcs_requests
    import uuid

    source = ledger.LedgerReader(str(data / "snapshots" / "ledger-snapshot.db"))
    source_hash = source.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    source.close()
    (data / "cmd").mkdir()
    mcs_requests.enqueue({
        "version": 1, "cmd": "request.create", "command_id": str(uuid.uuid4()),
        "actor": "synthetic reviewer", "human_confirmed": True, "project_id": 1,
        "source_message_id": 1, "source_hash": source_hash,
        "title": "synthetic task", "reason": "synthetic confirmation",
    }, data / "cmd")
    assert run_check.main() == 0
    capsys.readouterr()
    db = ledger.LedgerReader(str(data / "snapshots" / "ledger-snapshot.db"))
    assert db.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    # the newly discovered patient also gets its initial-history progress rows
    assert db.db.execute("SELECT count(*) FROM notify_outbox WHERE kind!='patient_history'").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM read_marks").fetchone()[0] == 0
    assert db.db.execute("SELECT count(*) FROM artifacts WHERE kind='karte_summary'").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM command_receipts WHERE outcome='applied'").fetchone()[0] == 1
    assert db.db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0] == 0
    assert db.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    db.close()


def test_run_lock_excludes_second_writer(tmp_path):
    """FIX-R00-01: one flock per run — a second writer must be refused,
    and closing the fd must release it for the next holder."""
    lock = tmp_path / "run.lock"
    fd = writer_lock.acquire_run_lock(str(lock))
    assert fd is not None
    second = writer_lock.acquire_run_lock(str(lock))
    assert second is None
    os.close(fd)
    third = writer_lock.acquire_run_lock(str(lock))
    assert third is not None
    os.close(third)


def test_cli_writer_holds_run_lock(tmp_path, monkeypatch, capsys):
    """A manual write CLI must refuse to run while a tick holds the lock —
    the shared queue is drained by exactly one writer at a time."""
    lock = tmp_path / "run.lock"
    held = writer_lock.acquire_run_lock(str(lock))
    assert held is not None
    monkeypatch.setattr(writer_lock, "RUN_LOCK", str(lock))
    monkeypatch.setattr(extract_llm, "DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(extract_llm_resident, 'DB', extract_llm.DB)
    monkeypatch.setattr(sys, "argv", ["extract_llm", "--limit", "1"])
    try:
        assert extract_llm.main() == 1
        assert json.loads(capsys.readouterr().out)["error"] == "writer_unavailable"
    finally:
        os.close(held)
    # free lock -> the CLI proceeds against the throwaway DB
    assert extract_llm.main() == 0


def test_tail_command_failure_marks_run_partial(tmp_path, monkeypatch, capsys):
    import maintenance
    import self_identity
    import notify_cards
    import notify_spool
    import notify_cmds

    class Adapter:
        def __init__(self, **kwargs):
            pass

        def set_deadline(self, deadline):
            pass

        def list_unread(self):
            return mcs_contract.UnreadSnapshot(timestamp=123, patients=[])

        def list_projects(self):
            return []

        def self_profile(self):
            return {}

        def station_staffs(self, stations):
            return []

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
        "MCSAdapter": Adapter, "stage_derive": lambda *a, **k: None,
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
    monkeypatch.setattr(self_identity, "record_self_profile", lambda *a: False)
    monkeypatch.setattr(maintenance, "daily_backup", lambda *a: None)
    monkeypatch.setattr(maintenance, "rotate_log", lambda *a: None)
    monkeypatch.setattr(maintenance, "prune_attachments", lambda *a: 0)
    monkeypatch.setattr(maintenance, "publish_snapshot", lambda *a, **k: True)
    calls = []

    def fail_tail(*args, **kwargs):
        calls.append(None)
        if len(calls) == 2:
            raise RuntimeError("synthetic tail failure")

    monkeypatch.setattr(notify_cmds, "drain_int_commands", fail_tail)
    monkeypatch.setattr(
        sys, "argv", ["run_check", "--no-notify", "--no-backfill"])

    assert run_check.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["errors"] == ["cmd_int_tail: RuntimeError"]
    db = ledger.Ledger(str(data / "ledger.db"))
    assert db.db.execute(
        "SELECT status FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()[0] == "partial"
    db.close()
    assert json.loads((data / "health.json").read_text())["run_status"] == "partial"


def test_consent_hold_tick_freezes_all_but_approve(tmp_path, monkeypatch,
                                                   capsys):
    """An awaiting_consent restore marker switches the tick to
    consent-only: no adapter, no ingest, no reconcile — only the
    ops.restore_approve drain runs so the approved loss report cannot
    drift (T13)."""
    import notify_spool
    import mcs_requests
    import uuid

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.json"
    config.write_text('{"deep_history":false}', encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "CONF_PATH": config, "CACHE": tmp_path / "absent-token.json",
    }.items():
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, str(value))
    monkeypatch.setattr(run_check, "MCSAdapter",
                        lambda *a, **k: pytest.fail(
                            "adapter must not be built during the hold"))
    notify_spool.mark_restored(str(data), backup_path=str(data / "b.db"),
                               phase="awaiting_consent")
    cmd = data / "cmd"
    cmd.mkdir()
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, "drain_commands",
                        lambda db, result, **kw: drain(db, result, str(cmd), **kw))
    # an unrelated op stays queued — its writes would be wiped by the swap
    mcs_requests.enqueue(
        {"version": 1, "cmd": "ops.pause",
         "command_id": str(uuid.uuid4()), "actor": "t",
         "human_confirmed": True, "project_id": 1,
         "feature": "semantic"}, str(cmd))
    rid = "a" * 64
    mcs_requests.enqueue(
        {"version": 1, "cmd": "ops.restore_approve",
         "command_id": str(uuid.uuid4()), "actor": "t",
         "human_confirmed": True, "project_id": None,
         "report_id": rid, "backup_sha256": "b" * 64,
         "backup_schema": 7, "reason": "synthetic"}, str(cmd))
    spawned = []
    monkeypatch.setattr(update_launch, 'spawn_detached',
                        lambda**_lif05_paths: spawned.append(1))
    # the respawn path recomputes the report — stub the backup read but
    # keep the REAL consent scan against the tick's own ledger
    monkeypatch.setattr(restore_evidence, "create_loss_report",
                        lambda **_paths: {"report_id": rid,
                                   "backup_sha256": "b" * 64,
                                   "backup_schema": 7})
    import builtins
    original_import = builtins.__import__

    def without_updater(name, *args, **kwargs):
        if name == "mcs_update":
            pytest.fail("consent-only tick must not import updater")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_updater)
    monkeypatch.setattr(sys, "argv", ["run_check"])
    assert run_check.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["restore_consent_hold"] is True
    assert out["commands"] == 1                # only the consent op ran
    assert spawned                             # updater re-launched
    left = [p.name for p in cmd.glob("*.json")]
    assert len(left) == 1                      # pause stayed queued


def test_overdue_tick_defers_later_stages_but_records_run_and_health(
        tmp_path, monkeypatch, capsys):
    import sqlite3
    from unittest.mock import Mock

    data = _point_run_check_at(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(run_check, "_config", lambda: {})
    monkeypatch.setattr(run_check, "_semantic_enabled", lambda *args: False)
    monkeypatch.setattr(run_check, "_code_changed", lambda *args: False)
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10000)
    monkeypatch.setattr(run_check, "MCSAdapter",
                        lambda **kwargs: SimpleNamespace(set_deadline=lambda deadline: None))
    def slow_fetch(*args):
        clock[0] += run_check.RUN_DEADLINE_S + 1

    monkeypatch.setattr(run_check, "_stage_fetch", slow_fetch)
    later = Mock()
    for name in ("_run_jobs", "stage_derive", "_deliver", "with_relogin",
                 "_run_semantic", "_run_metadata_shadow", "_housekeeping"):
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, later)
    # the real _finish_run decides the stored status; only its I/O tail is stubbed
    import maintenance
    import notify_cards
    import notify_spool
    import notify_cmds
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **k: None)
    for name in ("publish_flags", "clear_snapshot_dirty"):
        monkeypatch.setattr((notify_spool if name in {'publish_flags'} else notify_cards), name, lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "publish_snapshot", lambda *a, **k: True)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    assert run_check._main() == 0
    later.assert_not_called()
    result = json.loads(capsys.readouterr().out)
    assert [e for e in result["errors"] if e.startswith("deadline_deferred:")] == [
        "deadline_deferred:jobs,derive,deliver,karte_summary,semantic,"
        "metadata_shadow,housekeeping"]
    assert result["deferred_stages"] == [
        "jobs", "derive", "deliver", "karte_summary", "semantic",
        "metadata_shadow", "housekeeping"]
    health = json.loads((data / "health.json").read_text())
    # New read-only interruption evidence is separate from this run's timing.
    interruption = health["run"].pop("last_interrupt")
    assert interruption["status"] == "recorded"
    assert interruption["count_24h"] == 0
    assert interruption["detected_at"] is None
    assert health["run"] == {
        "elapsed_s": 481, "overshoot_s": 1, "slowest_stage": "fetch",
        "deferred_stages": result["deferred_stages"]}
    assert health["overall"] == "degraded"
    assert "run_deadline_exceeded" in health["state_reasons"]
    with closing(sqlite3.connect(data / "ledger.db")) as db, db:
        assert db.execute("SELECT status FROM runs").fetchone()[0] == "partial"


def test_crash_before_config_load_reports_backup_unknown(
        tmp_path, monkeypatch, capsys):
    data = _point_run_check_at(tmp_path, monkeypatch)
    monkeypatch.setattr(run_check, "MCSAdapter",
                        lambda **kwargs: SimpleNamespace(set_deadline=lambda deadline: None))
    loads = []

    def config():
        loads.append(None)
        if len(loads) > 1:          # the Ledger probe succeeds, the run load crashes
            raise RuntimeError("synthetic config failure")
        return {}

    monkeypatch.setattr(run_check, "_config", config)
    local = {"state": "missing", "newest_age_s": None}
    monkeypatch.setattr(run_check.maintenance, "local_backup_status",
                        lambda **_: local)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    assert run_check._main() == 1
    capsys.readouterr()
    health = json.loads((data / "health.json").read_text())
    assert health["backup"] == {"state": "unknown", "reasons": ["config_not_loaded"],
                                "local": local}
    assert "backup_not_verified" in health["state_reasons"]


def test_tick_waits_for_a_run_lock_released_between_batches(
        tmp_path, monkeypatch):
    """The nightly drain releases run.lock between batches — a tick
    must win it within its wait budget instead of exiting on one try."""
    _point_run_check_at(tmp_path, monkeypatch)
    fake_fd = os.open(str(tmp_path / "fake_lock"), os.O_CREAT | os.O_RDWR)
    attempts = 0
    clock = 0.0

    def acquire_after_retries(path):
        nonlocal attempts
        attempts += 1
        return None if attempts < 4 else fake_fd

    def advance_clock(seconds):
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(writer_lock, "acquire_run_lock", acquire_after_retries)
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock)
    monkeypatch.setattr(run_check.time, "sleep", advance_clock)
    fd = writer_lock.wait_run_lock(run_check.LOCKFILE, wait_s=5, expected_code_stamp=run_check._CODE_STAMP,
                                    marker_path=os.path.join(run_check.HOME, "data", writer_lock.UPDATE_MARKER_NAME))
    assert fd is not None
    assert fd == fake_fd
    assert attempts == 4
    os.close(fd)


@pytest.mark.parametrize("argv, waits", [
    (["run_check", "--no-notify"], True),
    (["run_check", "--commands-only"], True)])
def test_lock_held_exit_waits_for_scheduled_and_command_runs(
        tmp_path, monkeypatch, capsys, argv, waits):
    data = _point_run_check_at(tmp_path, monkeypatch)
    held = run_check.acquire_run_lock(str(data / "run.lock"))
    waited = []
    monkeypatch.setattr(writer_lock, "wait_run_lock",
                        lambda path, *, wait_s, expected_code_stamp, marker_path: waited.append(wait_s))
    monkeypatch.setattr(sys, "argv", argv)
    try:
        assert run_check.main() == 3
    finally:
        os.close(held)
    assert bool(waited) is waits
    assert "lock_held" in capsys.readouterr().out


@pytest.mark.parametrize("fence", [None, "marker", "code_changed"])
def test_command_worker_catches_short_lock_window_without_crossing_update(
        tmp_path, monkeypatch, fence):
    data = _point_run_check_at(tmp_path, monkeypatch)
    fd = os.open(str(tmp_path / "synthetic-lock"), os.O_CREAT | os.O_RDWR, 0o600)
    clock, calls = [100.0], []
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(run_check.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(run_check, "_CODE_STAMP", 123)
    monkeypatch.setattr(writer_lock, "code_stamp", lambda: 124 if fence == "code_changed" else 123)
    monkeypatch.setattr(writer_lock, "code_stamp_checked", lambda: 123)
    monkeypatch.setattr(run_check, "acquire_run_lock", lambda path: None)
    monkeypatch.setattr(writer_lock, "acquire_run_lock",
                        lambda path: fd if clock[0] >= 100.6 else None)
    if fence == "marker":
        (data / writer_lock.UPDATE_MARKER_NAME).write_text("synthetic update")

    def commands(held_fd, deadline):
        calls.append((held_fd, deadline, clock[0]))
        os.close(held_fd)
        return 0

    monkeypatch.setattr(run_check, "_commands_only", commands)
    monkeypatch.setattr(sys, "argv", ["run_check", "--commands-only"])
    try:
        assert run_check.main() == (0 if fence is None else 3)
        if fence is None:
            assert len(calls) == 1 and calls[0][0] == fd
            assert calls[0][1] == 100 + run_check.RUN_DEADLINE_S
            assert calls[0][2] > 100  # lock waiting spent this same budget
        else:
            assert calls == []
        with pytest.raises(OSError):
            os.fstat(fd)  # every accepted or refused lock is released
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_command_worker_does_not_reset_budget_after_slow_entry_check(
        tmp_path, monkeypatch):
    _point_run_check_at(tmp_path, monkeypatch)
    clock, calls = [100.0], []
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])

    def slow_entry_check():
        clock[0] += run_check.RUN_DEADLINE_S + 1
        return True

    monkeypatch.setattr(run_check, "_entry_code_current", slow_entry_check)
    monkeypatch.setattr(run_check, "_commands_only", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(sys, "argv", ["run_check", "--commands-only"])
    assert run_check.main() == 3
    assert calls == []
    fd = run_check.acquire_run_lock(run_check.LOCKFILE)
    assert fd is not None
    os.close(fd)


def test_scheduled_worker_keeps_full_budget_after_waiting_for_writer(
        tmp_path, monkeypatch):
    _point_run_check_at(tmp_path, monkeypatch)
    clock, deadlines = [100.0], []
    fd = os.open(str(tmp_path / "synthetic-lock"), os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(run_check, "acquire_run_lock", lambda path: None)

    def wait(path, **kwargs):
        assert kwargs["wait_s"] == run_check.LOCK_WAIT_S
        clock[0] += run_check.LOCK_WAIT_S
        return fd

    monkeypatch.setattr(writer_lock, "wait_run_lock", wait)
    monkeypatch.setattr(run_check, "_entry_code_current", lambda: True)
    monkeypatch.setattr(run_check, "_config", lambda: {})
    monkeypatch.setattr(run_check, "MCSAdapter", lambda **kwargs: SimpleNamespace(
        set_deadline=deadlines.append))

    def refuse_ledger(*args, **kwargs):
        raise RuntimeError("synthetic stop before database work")

    monkeypatch.setattr(run_check, "Ledger", refuse_ledger)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    assert run_check.main() == 1
    assert deadlines == [100 + run_check.LOCK_WAIT_S + run_check.RUN_DEADLINE_S]
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("cause", ["marker", "code_changed"])
def test_waited_lock_is_given_back_after_an_update(tmp_path, monkeypatch,
                                                   cause):
    """A tick that waited out an updater must not run its pre-update
    code against the post-update tree."""
    data = _point_run_check_at(tmp_path, monkeypatch)
    if cause == "marker":
        (data / "update_in_progress.marker").write_text("x")
    else:
        monkeypatch.setattr(run_check, "_CODE_STAMP", -1)
    monkeypatch.setattr(writer_lock, "RUN_LOCK_POLL_S", 0.01)
    assert writer_lock.wait_run_lock(run_check.LOCKFILE, wait_s=0.2, expected_code_stamp=run_check._CODE_STAMP,
                                    marker_path=os.path.join(run_check.HOME, "data", writer_lock.UPDATE_MARKER_NAME)) is None
    fd = run_check.acquire_run_lock(str(data / "run.lock"))
    assert fd is not None      # the lock was released, not leaked
    os.close(fd)


def test_code_changed_is_recorded_once(monkeypatch):
    result = {"errors": []}
    assert run_check._code_changed(result) is False
    monkeypatch.setattr(run_check, "_CODE_STAMP", -1)
    assert run_check._code_changed(result) is True
    assert run_check._code_changed(result) is True
    assert result["errors"] == ["code_changed"]


@pytest.mark.parametrize("held, status", [(True, "partial"),
                                          (False, "running")])
def test_run_lock_lost_ends_tick_without_later_stages(
        tmp_path, monkeypatch, capsys, held, status):
    """Held: the run is recorded partial. Not held: no DB write at all —
    the next begin_run closes the row as crashed."""
    import sqlite3
    from unittest.mock import Mock
    from writer_lock import RunLockLost

    data = _point_run_check_at(tmp_path, monkeypatch)
    monkeypatch.setattr(run_check, "_config", lambda: {})
    monkeypatch.setattr(run_check, "_semantic_enabled", lambda *args: True)
    monkeypatch.setattr(run_check, "_code_changed", lambda *args: False)
    monkeypatch.setattr(run_check, "MCSAdapter",
                        lambda **kwargs: SimpleNamespace(set_deadline=lambda deadline: None))
    for name in ("_stage_fetch", "_run_jobs", "stage_derive", "_deliver",
                 "with_relogin"):
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, lambda *a, **k: None)

    def lost(*a, **k):
        raise RunLockLost(held=held)

    monkeypatch.setattr(run_check, "_run_semantic", lost)
    later = Mock()
    for name in ("_run_metadata_shadow", "_housekeeping", "_finish_run"):
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (health_report if name == "HEALTH_FILE" else run_check),
            name, later)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify"])
    assert run_check._main() == 1
    later.assert_not_called()
    assert "run_lock_lost" in json.loads(capsys.readouterr().out)["errors"]
    assert (data / "health.json").exists() is held
    with closing(sqlite3.connect(data / "ledger.db")) as db, db:
        assert db.execute("SELECT status FROM runs").fetchone()[0] == status
    fd = run_check.acquire_run_lock(str(data / "run.lock"))
    assert fd is not None          # the run lock was released on exit
    os.close(fd)


def test_commands_snapshot_keeps_original_deadline_and_dirty_on_failure(tmp_path, monkeypatch, capsys):
    import maintenance
    import notify_cards
    import notify_cmds
    import notify_spool

    class FakeLedger:
        def __init__(self, path):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_check, "Ledger", FakeLedger)
    monkeypatch.setattr(run_check, "_config", lambda: {})
    monkeypatch.setattr(run_check, "_hard_deadline", lambda: None)
    for name in ("ensure_dirs", "recover", "publish_flags"):
        monkeypatch.setattr(notify_spool, name, lambda *a, **kw: None)
    for name in ("restore_awaiting_consent", "restore_pending"):
        monkeypatch.setattr(notify_spool, name, lambda *a, **kw: None)
    monkeypatch.setattr(job_ops, "drain_commands", lambda *a, **kw: None)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **kw: None)
    monkeypatch.setattr(notify_cards, "gc", lambda *a, **kw: None)
    monkeypatch.setattr(notify_cards, "snapshot_dirty", lambda ledger: 42)
    cleared, calls = [], []
    monkeypatch.setattr(notify_cards, "clear_snapshot_dirty", lambda *a: cleared.append(a))
    monkeypatch.setattr(maintenance, "publish_snapshot", lambda db, *, deadline: calls.append(deadline) or False)
    fd = os.open(str(tmp_path / "synthetic.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() - 1
    assert run_check._commands_only(fd, deadline) == 1
    assert calls == [deadline] and not cleared
    assert "snapshot_publish_failed" in capsys.readouterr().out
