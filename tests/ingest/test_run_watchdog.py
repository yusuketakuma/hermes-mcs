"""A process watchdog terminates a wedged synthetic tick without polling."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import run_check
from ledger import Ledger


def test_watchdog_exits_wedged_child_with_a_bounded_parent_timeout(tmp_path):
    module_file = run_check.__file__
    assert isinstance(module_file, str)
    source = str(Path(module_file).resolve().parent)
    database = tmp_path / "synthetic.db"
    script = (
        "import sys,time,threading\n"
        f"sys.path.insert(0,{source!r})\n"
        "import run_check\n"
        "from ledger import Ledger\n"
        "db=Ledger(sys.argv[1])\n"
        "db.begin_run(None)\n"
        "run_check._arm_watchdog(time.monotonic(),1)\n"
        "print('ARMED',flush=True)\n"
        "threading.Event().wait()\n")
    result = subprocess.run(
        [sys.executable, "-c", script, str(database)], capture_output=True, text=True,
        env={**os.environ, "HOME": str(tmp_path), "MCS_ROOT": str(tmp_path)},
        timeout=10)
    assert result.stdout.strip() == "ARMED"
    assert result.returncode != 0
    assert result.stderr
    db = Ledger(str(database))
    try:
        db.begin_run(None)
        assert db.db.execute("SELECT status FROM runs ORDER BY run_id").fetchall()[0][0] == "crashed"
    finally:
        db.close()


@pytest.mark.parametrize("grace", ["0", "-1", "3601"])
def test_invalid_watchdog_grace_is_rejected_before_runtime_access(monkeypatch, grace):
    monkeypatch.setattr(sys, "argv", ["run_check.py", "--watchdog-grace", grace])
    with pytest.raises(SystemExit) as result:
        run_check._main()
    assert result.value.code == 2


def test_watchdog_covers_ledger_initialization_and_cleans_up_on_failure(
        tmp_path, monkeypatch, capsys):
    import faulthandler
    from types import SimpleNamespace
    from test_run_check_stages import _point_run_check_at

    _point_run_check_at(tmp_path, monkeypatch)
    fd = os.open(tmp_path / "synthetic-lock", os.O_CREAT | os.O_RDWR, 0o600)
    events = []
    monkeypatch.setattr(run_check, "acquire_run_lock", lambda _: fd)
    monkeypatch.setattr(run_check, "_config", lambda: {})
    monkeypatch.setattr(run_check, "MCSAdapter",
                        lambda **kwargs: SimpleNamespace(set_deadline=lambda _: None))
    monkeypatch.setattr(run_check, "_arm_watchdog",
                        lambda deadline, grace: events.append(("armed", grace)))
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later",
                        lambda: events.append(("cancelled", None)))
    def fail(*args, **kwargs):
        events.append(("ledger", None))
        raise ValueError("synthetic unsupported DB")
    monkeypatch.setattr(run_check, "Ledger", fail)
    monkeypatch.setattr(sys, "argv", ["run_check", "--watchdog-grace", "60"])
    assert run_check._main() == 1
    assert events == [("armed", 60), ("ledger", None), ("cancelled", None)]
    assert json.loads(capsys.readouterr().out)["error"] == "ledger_init_failed"
    with pytest.raises(OSError):
        os.fstat(fd)


def test_expired_stage_defers_work_but_finishes_and_records_elapsed(monkeypatch):
    from unittest.mock import Mock

    clock = iter([100, 103, 103, 103, 104])
    monkeypatch.setattr(run_check.time, "monotonic", lambda: next(clock))
    result = {}
    work = Mock(return_value="done")
    assert run_check._run_stage(result, "fetch", 102, work) == "done"
    assert run_check._run_stage(result, "derive", 102, work) is None
    assert run_check._run_stage(result, "finish", 102, work, required=True) == "done"
    assert work.call_count == 2
    assert result["deferred_stages"] == ["derive"]
    assert result["run"] == {
        "elapsed_s": 482, "overshoot_s": 2, "slowest_stage": "fetch"}
    assert result["stage_elapsed_s"] == {"fetch": 3, "finish": 1}
