"""An unpublished snapshot records which branch refused it, without paths or content. Synthetic DB only."""
import fcntl
import sqlite3
from time import monotonic

import pytest

import snapshots


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE snapshot_meta(singleton INTEGER PRIMARY KEY, generation_id, generated_at)")
    return str(path)


def _child(monkeypatch, code):
    monkeypatch.setattr(snapshots, "_SNAPSHOT_VALIDATION_CODE", code)


def test_success_leaves_no_failure(db, tmp_path, monkeypatch):
    _child(monkeypatch, "import sys; sys.exit(0)")
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"))
    assert snapshots.last_failure() == {}


def test_deadline_before_copy(db, tmp_path):
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"), deadline=monotonic() - 1) is None
    assert snapshots.last_failure() == {"reason": "deadline_before_copy"}


@pytest.mark.parametrize("code,reason", [("import sys; sys.exit(1)", "validator_rejected"),
                                         ("import sys; sys.exit(3)", "validator_bootstrap_error"),
                                         ("import sys; sys.exit(4)", "validator_exception"),
                                         ("import sys; sys.exit(5)", "validator_timeout"),
                                         ("import sys; sys.exit(7)", "validator_exit")])
def test_validator_exit_codes_are_told_apart(db, tmp_path, monkeypatch, code, reason):
    _child(monkeypatch, code)
    assert snapshots.publish_snapshot(db, str(tmp_path / "out")) is None
    failure = snapshots.last_failure()
    assert failure["reason"] == reason and failure["exit_code"] == int(code[-2])
    assert failure["capped_by_deadline"] is False and failure["copy_s"] >= 0 and failure["verify_s"] >= 0
    assert all(isinstance(v, (str, int, float, bool)) for v in failure.values())


def test_validator_timeout_reports_budget_and_deadline_cap(db, tmp_path, monkeypatch):
    _child(monkeypatch, "import time; time.sleep(30)")
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"), deadline=monotonic() + 1) is None
    failure = snapshots.last_failure()
    assert failure["reason"] == "validator_timeout" and failure["capped_by_deadline"] is True
    assert failure["budget_s"] <= 1


def test_busy_validation_lock(db, tmp_path, monkeypatch):
    _child(monkeypatch, "import sys; sys.exit(0)")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    with open(out / ".validation.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert snapshots.publish_snapshot(db, str(out)) is None
    assert snapshots.last_failure()["reason"] == "validation_lock_busy"
    assert snapshots.publish_snapshot(db, str(out))          # the next publish resets the reason
    assert snapshots.last_failure() == {}


def _real_child(*argv):
    import subprocess
    import sys
    return subprocess.run([sys.executable, "-I", "-c", snapshots._SNAPSHOT_VALIDATION_CODE, *argv],
                          capture_output=True, timeout=60).returncode


def test_real_child_separates_bootstrap_errors_exceptions_and_refusals(tmp_path):
    from pathlib import Path
    mcs_root = str(Path(snapshots.__file__).resolve().parents[1])
    far = str(monotonic() + 60)
    assert _real_child(str(tmp_path), str(tmp_path / "x.db"), far) == 3      # no _mcs_path there
    assert _real_child(mcs_root, str(tmp_path / "x.db"), "not-a-number") == 4
    assert _real_child(mcs_root, str(tmp_path / "missing.db"), far) == 1     # explicit refusal


def test_real_child_reports_its_own_deadline_as_timeout(tmp_path):
    from pathlib import Path
    mcs_root = str(Path(snapshots.__file__).resolve().parents[1])
    db = tmp_path / "x.db"
    db.write_bytes(b"not empty")
    assert _real_child(mcs_root, str(db), str(monotonic() - 1)) == 5      # deadline already past


def test_verify_budget_follows_a_slow_copy_within_the_cap(db, tmp_path, monkeypatch):
    seen, clock = {}, {"t": 0.0}

    def verify(path, *, deadline):
        seen["budget"] = deadline - clock["t"]
        return True
    monkeypatch.setattr(snapshots, "_valid_snapshot_candidate", verify)
    monkeypatch.setattr(snapshots, "monotonic", lambda: clock["t"])
    real_connect, opened = snapshots.sqlite3.connect, []

    def connect(*a, **k):
        opened.append(1)
        if len(opened) % 2 == 0:                      # the copy target: the copy "takes" 100 s
            clock["t"] += 100.0
        return real_connect(*a, **k)
    monkeypatch.setattr(snapshots.sqlite3, "connect", connect)
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"))
    assert seen["budget"] == 150.0                    # 1.5 x copy, not the ~30 s size floor
    clock["t"] = 0.0
    assert snapshots.publish_snapshot(db, str(tmp_path / "out"), deadline=120.0)
    assert seen["budget"] == 20.0                     # never past the caller's hard deadline
