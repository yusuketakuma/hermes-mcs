"""An unfinished run's cause is recorded only from its own private evidence.

Synthetic temp DB and evidence files only; boot time is always a stub."""
import io
import json
import subprocess
import time
from contextlib import closing

import pytest

import health_watch
import host_clock
import operational_status
import run_interrupt
from health_policy import INTERRUPT_REASONS
from ledger import Ledger


def _ledger(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


def _crashed_by(tmp_path, *, dump=None, boot_at_arm=None, boot_now=None, exit_in=600.0,
                exited=False, preempted=False, bind=True, started_shift=0.0, run_shift=0,
                session_at_arm="SESSION-A", session_now="SESSION-B"):
    """One run leaves evidence and dies; the next begin_run classifies it."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    with closing(_ledger(tmp_path)) as db:
        ev = run_interrupt.open_evidence(str(data), boot_time=lambda: boot_at_arm,
                                         boot_session=lambda: session_at_arm)
        assert ev is not None
        if exit_in is not None:
            ev.armed(time.time() + exit_in)
        run_id = db.begin_run(None)
        started = db.db.execute("SELECT started_at FROM runs WHERE run_id=?",
                                (run_id,)).fetchone()[0]
        if bind:
            ev.bind(run_id + run_shift, started + started_shift)
        if preempted:
            ev.mark_update_preempted()
        if dump is not None:
            ev.dump.write(dump)
            ev.dump.flush()
        ev.close(exited=exited)
        boot = (lambda: boot_now(started)) if callable(boot_now) else (lambda: boot_now)
        interrupt = run_interrupt.Classifier(str(data), boot_time=boot,
                                             boot_session=lambda: session_now)
        db.begin_run(None, interrupt_reason=interrupt)
        row = db.db.execute("SELECT status,error FROM runs WHERE run_id=?", (run_id,)).fetchone()
        assert row[0] == "crashed"
        return row[1], interrupt, db.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def test_no_evidence_reader_keeps_unknown(tmp_path):
    with closing(_ledger(tmp_path)) as db:
        db.begin_run(None)
        db.begin_run(None)
        row = db.db.execute("SELECT status,error FROM runs WHERE run_id=1").fetchone()
        assert tuple(row) == ("crashed", None)
        out = operational_status.interrupted_runs(db.db, now=time.time() + 1)
    assert out["reason"] == "unknown" and out["by_reason_24h"] == {"unknown": 1}


def test_watchdog_dump_is_proof_and_reaches_the_alert(tmp_path):
    error, interrupt, _ = _crashed_by(tmp_path, dump=b"Timeout (0:09:00)!\nThread 0x1:\n")
    assert error == "watchdog_exit"
    log = io.StringIO()
    run_interrupt.transcribe(interrupt, log)
    assert "Timeout (0:09:00)!" in log.getvalue()
    assert "traceback of run 1:" in log.getvalue()
    run_interrupt.transcribe(interrupt, log)        # copied once only
    assert log.getvalue().count("Timeout (0:09:00)!") == 1
    with closing(_ledger(tmp_path)) as db:
        now = time.time() + 1
        out = operational_status.interrupted_runs(db.db, now=now)
    assert out["by_reason_24h"] == {"watchdog_exit": 1}
    health = tmp_path / "health.json"
    health.write_text(json.dumps({
        "overall": "degraded", "at": now, "state_reasons": ["notification_pending"],
        "notify": {"pending": 1, "oldest_age_s": 7200.0},
        "run": {"last_interrupt": {"reason": out["reason"], "detected_at": out["detected_at"]}}}))
    report = health_watch.classify_health(str(health), now=now + 10, deadline_s=900)
    text = "\n".join(health_watch._alert_lines({**report, "alert": True, "disk_alert": False}))
    assert "直近の中断: 制限時間超過で打ち切り（" in text


@pytest.mark.parametrize("dump", [b"", b"Fatal Python error: x\n", b" Timeout (0:00:01)"])
def test_other_dumps_are_not_proof(tmp_path, dump):
    assert _crashed_by(tmp_path, dump=dump, exit_in=None)[0] is None


def test_symlinked_dump_is_not_proof(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with closing(_ledger(tmp_path)) as db:
        ev = run_interrupt.open_evidence(str(data), boot_time=lambda: None)
        run_id = db.begin_run(None)
        ev.bind(run_id, db.db.execute("SELECT started_at FROM runs").fetchone()[0])
        ev.close(exited=False)
        target = tmp_path / "elsewhere"
        target.write_bytes(b"Timeout (0:00:01)!\n")
        dump = data / run_interrupt.DIR_NAME / (ev.stem + ".dump")
        dump.unlink()
        dump.symlink_to(target)
        db.begin_run(None, interrupt_reason=run_interrupt.Classifier(
            str(data), boot_time=lambda: None))
        assert db.db.execute("SELECT error FROM runs WHERE run_id=?", (run_id,)).fetchone()[0] is None


@pytest.mark.parametrize("bind, started_shift, run_shift", [
    (False, 0.0, 0), (True, -1.0, 0), (True, 0.0, 1)])
def test_evidence_of_another_row_is_ignored(tmp_path, bind, started_shift, run_shift):
    # a restored DB can reuse run_id; only the exact started_at read back matches
    assert _crashed_by(tmp_path, dump=b"Timeout (0:00:01)!\n", bind=bind,
                       started_shift=started_shift, run_shift=run_shift)[0] is None


def test_update_preemption_is_recorded(tmp_path):
    assert _crashed_by(tmp_path, preempted=True, exited=True)[0] == "update_preempted"


@pytest.mark.parametrize("boot_at_arm, boot_now, exit_in, exited, expected", [
    (1000.0, lambda started: started + 30, 600.0, False, "host_restart"),
    (1000.0, lambda started: started + 2, 600.0, False, None),      # not after start
    (1000.0, lambda started: started + 900, 600.0, False, None),    # after the exit time
    (1000.0, None, 600.0, False, None),                             # unreadable now
    (None, lambda started: started + 30, 600.0, False, None),       # unreadable at arm
    (1000.0, lambda started: started + 30, 600.0, True, None),      # reached its exit
    (1000.0, lambda started: started + 30, None, False, None),      # no watchdog bound
])
def test_host_restart_needs_a_changed_boot_inside_the_run(
        tmp_path, boot_at_arm, boot_now, exit_in, exited, expected):
    assert _crashed_by(tmp_path, boot_at_arm=boot_at_arm, boot_now=boot_now,
                       exit_in=exit_in, exited=exited)[0] == expected


def test_unchanged_boot_is_not_a_restart(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with closing(_ledger(tmp_path)) as db:
        run_id = db.begin_run(None)
        started = db.db.execute("SELECT started_at FROM runs").fetchone()[0]
        ev = run_interrupt.open_evidence(str(data), boot_time=lambda: started + 30)
        ev.armed(time.time() + 600)
        ev.bind(run_id, started)
        ev.close(exited=False)
        db.begin_run(None, interrupt_reason=run_interrupt.Classifier(
            str(data), boot_time=lambda: started + 30))
        assert db.db.execute("SELECT error FROM runs WHERE run_id=?", (run_id,)).fetchone()[0] is None


@pytest.mark.parametrize("session_at_arm, session_now", [
    ("SESSION-A", "SESSION-A"),     # the clock was stepped, the boot is the same
    (None, "SESSION-B"), ("SESSION-A", None)])
def test_boot_time_shift_alone_is_not_a_restart(tmp_path, session_at_arm, session_now):
    assert _crashed_by(tmp_path, boot_at_arm=1000.0, boot_now=lambda started: started + 30,
                       session_at_arm=session_at_arm, session_now=session_now)[0] is None


@pytest.mark.parametrize("bind, started_shift", [(False, 0.0), (True, -1.0)])
def test_unmatched_watchdog_dump_still_reaches_the_log(tmp_path, bind, started_shift):
    error, interrupt, _ = _crashed_by(tmp_path, dump=b"Timeout (0:00:07)!\nThread 0x2:\n",
                                      bind=bind, started_shift=started_shift)
    assert error is None and interrupt.transcripts == []
    log = io.StringIO()
    run_interrupt.transcribe(interrupt, log)
    assert "watchdog_exit traceback of an unmatched run:\nTimeout (0:00:07)!" in log.getvalue()
    assert not list((tmp_path / "data" / run_interrupt.DIR_NAME).glob("*.dump"))
    run_interrupt.transcribe(interrupt, log)
    assert log.getvalue().count("Timeout (0:00:07)!") == 1


def test_dump_of_a_live_process_waits_and_prune_copies_it_first(tmp_path):
    import os
    data = tmp_path / "data"
    data.mkdir()
    ev = run_interrupt.open_evidence(str(data), boot_time=lambda: None)
    ev.close()
    folder = data / run_interrupt.DIR_NAME
    live = f"{time.time_ns() - 10**9}-{os.getppid()}"     # an alive, other process
    (folder / (live + ".dump")).write_bytes(b"Timeout (0:00:03)!\nThread 0x3:\n")
    (folder / (live + ".dump")).chmod(0o600)
    log = io.StringIO()
    run_interrupt.transcribe(run_interrupt.Classifier(str(data), boot_time=lambda: None), log)
    assert log.getvalue() == ""
    run_interrupt.prune(str(data), keep=0, stream=log)
    assert "traceback of an unmatched run:\nTimeout (0:00:03)!" in log.getvalue()
    assert not (folder / (live + ".dump")).exists()


def test_prune_keeps_a_dump_it_could_not_copy(tmp_path):
    import os

    class Broken:
        def write(self, _):
            raise OSError("synthetic")
        flush = write
    data = tmp_path / "data"
    data.mkdir()
    ev = run_interrupt.open_evidence(str(data), boot_time=lambda: None)
    ev.dump.write(b"Timeout (0:00:01)!\n")
    ev.close()
    run_interrupt.prune(str(data), keep=0, stream=Broken())
    assert os.path.exists(data / run_interrupt.DIR_NAME / (ev.stem + ".dump"))


def test_watchdog_proof_wins_over_a_restart(tmp_path):
    assert _crashed_by(tmp_path, dump=b"Timeout (0:00:01)!\n", boot_at_arm=1000.0,
                       boot_now=lambda started: started + 30)[0] == "watchdog_exit"


@pytest.mark.parametrize("answer", [RuntimeError("synthetic"), "synthetic-private-error-" * 3, 7])
def test_failing_or_invalid_reader_keeps_unknown_and_opens_the_run(tmp_path, answer):
    def reader(run_id, started_at):
        if isinstance(answer, Exception):
            raise answer
        return answer
    with closing(_ledger(tmp_path)) as db:
        db.begin_run(None)
        new = db.begin_run(None, interrupt_reason=reader)
        rows = db.db.execute("SELECT run_id,status,error FROM runs ORDER BY run_id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "crashed", None), (new, "running", None)]


def test_boot_time_is_read_once_for_many_rows(tmp_path):
    calls = []
    data = tmp_path / "data"
    data.mkdir()
    with closing(_ledger(tmp_path)) as db:
        for _ in range(3):
            ev = run_interrupt.open_evidence(str(data), boot_time=lambda: 1000.0,
                                             boot_session=lambda: "SESSION-A")
            ev.armed(time.time() + 600)
            run_id = db.db.execute("INSERT INTO runs(started_at,status,kind) VALUES(?,?,?)",
                                   (time.time(), "running", "tick")).lastrowid
            db.db.commit()
            ev.bind(run_id, db.db.execute("SELECT started_at FROM runs WHERE run_id=?",
                                          (run_id,)).fetchone()[0])
            ev.close(exited=False)
        db.begin_run(None, interrupt_reason=run_interrupt.Classifier(
            str(data), boot_time=lambda: calls.append(1),
            boot_session=lambda: "SESSION-B"))
    assert calls == [1]


def test_evidence_is_private_and_holds_fixed_keys_only(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    ev = run_interrupt.open_evidence(str(data), boot_time=lambda: 1000.5)
    ev.bind(3, 1234.5)
    ev.close()
    folder = data / run_interrupt.DIR_NAME
    assert folder.stat().st_mode & 0o777 == 0o700
    record = folder / (ev.stem + ".json")
    assert record.stat().st_mode & 0o777 == 0o600
    assert (folder / (ev.stem + ".dump")).stat().st_mode & 0o777 == 0o600
    body = json.loads(record.read_text())
    assert set(body) == run_interrupt._KEYS
    assert body["run_id"] == 3 and body["exited"] is True and body["boot"] == 1000.5


def test_unsafe_folder_never_blocks_the_watchdog(tmp_path, monkeypatch):
    import faulthandler
    import run_check
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (data / run_interrupt.DIR_NAME).symlink_to(tmp_path / "elsewhere")
    assert run_interrupt.open_evidence(str(data), boot_time=lambda: None) is None
    armed = []
    monkeypatch.setattr(faulthandler, "dump_traceback_later",
                        lambda delay, **kwargs: armed.append(kwargs))
    # _arm_watchdog records its exit time process-wide; restore it afterwards so
    # later ticks in this pytest process do not obey this test's hard deadline.
    monkeypatch.setattr(run_check, "_WATCHDOG_EXIT_AT", None)
    run_check._arm_watchdog(time.monotonic() + 60, 1, None)
    assert armed == [{"exit": True}]
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_prune_keeps_the_newest_pairs_and_its_own(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    made = [run_interrupt.open_evidence(str(data), boot_time=lambda: None) for _ in range(12)]
    for ev in made:
        ev.close()
    own = made[0].stem
    removed = run_interrupt.prune(str(data), own=own)
    left = {p.name.rpartition(".")[0] for p in (data / run_interrupt.DIR_NAME).iterdir()}
    assert removed == 2 * (12 - 8 - 1)
    assert own in left and len(left) == 9
    assert {ev.stem for ev in made[-8:]} <= left


def test_vocabulary_has_one_source():
    assert set(health_watch._INTERRUPT_JA) == set(INTERRUPT_REASONS)
    assert operational_status.INTERRUPT_REASONS is INTERRUPT_REASONS
    assert run_interrupt._PROVEN == set(INTERRUPT_REASONS) - {"unknown"}


@pytest.mark.parametrize("reply, expected", [
    ("{ sec = 1700000000, usec = 5 } Tue Nov 14 22:13:20 2023\n", 1700000000.000005),
    ("{ sec = 100 }", 100.0), ("garbage", None), ("{ usec = 5 }", None)])
def test_boot_time_parsing(monkeypatch, reply, expected):
    monkeypatch.setattr(host_clock.sys, "platform", "darwin")
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs["timeout"]))
        return subprocess.CompletedProcess(argv, 0, reply, "")
    monkeypatch.setattr(host_clock.subprocess, "run", run)
    assert host_clock.boot_time() == expected
    assert seen == [(["/usr/sbin/sysctl", "-n", "kern.boottime"], 2)]


@pytest.mark.parametrize("outcome", [
    subprocess.CompletedProcess([], 1, "{ sec = 5 }", ""),
    subprocess.TimeoutExpired("sysctl", 2), OSError("synthetic")])
def test_boot_time_failures_are_none(monkeypatch, outcome):
    monkeypatch.setattr(host_clock.sys, "platform", "darwin")

    def run(argv, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    monkeypatch.setattr(host_clock.subprocess, "run", run)
    assert host_clock.boot_time() is None


@pytest.mark.parametrize("reply, expected", [
    ("0d4f6b9e-1c2a-4b3d-8e5f-a1b2c3d4e5f6\n", "0D4F6B9E-1C2A-4B3D-8E5F-A1B2C3D4E5F6"),
    ("garbage", None), ("", None)])
def test_boot_session_parsing(monkeypatch, reply, expected):
    monkeypatch.setattr(host_clock.sys, "platform", "darwin")
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, reply, "")
    monkeypatch.setattr(host_clock.subprocess, "run", run)
    assert host_clock.boot_session() == expected
    assert seen == [["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]]


def test_boot_time_is_not_queried_off_macos(monkeypatch):
    monkeypatch.setattr(host_clock.sys, "platform", "linux")
    monkeypatch.setattr(host_clock.subprocess, "run",
                        lambda *a, **k: pytest.fail("no OS query off macOS"))
    assert host_clock.boot_time() is None
    assert host_clock.boot_session() is None
