"""Operation failures preserve prior reports and never leak temporary files or secrets."""
import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("writer", ["recovery_report", "restore_report"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_failed_report_publication_preserves_previous_file_and_removes_temporary(
        tmp_path, monkeypatch, writer, failure):
    module = _module("synthetic_recovery_cleanup", "deployment/recovery/mcs_recover.py")
    monkeypatch.setattr(module, "DATA", str(tmp_path))
    report = tmp_path / f"{writer}.json"
    report.write_text('{"synthetic":"prior"}')
    monkeypatch.setattr(module, "REPORT_PATH", str(report))
    live, backup = tmp_path / "live.db", tmp_path / "backup.db"
    for path in (live, backup):
        db = sqlite3.connect(path)
        try:
            db.executescript("CREATE TABLE messages(message_id INTEGER,posted_at_ts INTEGER);"
                             "INSERT INTO messages VALUES(1,1);")
        finally:
            db.close()
    monkeypatch.setattr(module, "LEDGER", str(live))

    def failed_replace(*args):
        raise failure("synthetic publish failure")

    monkeypatch.setattr(module.os, "replace", failed_replace)

    def publish():
        if writer == "recovery_report":
            return module._report("synthetic", "synthetic")
        return module._loss_report(str(backup))

    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            publish()
    else:
        for _ in range(20):
            result = publish()
            if writer == "restore_report":
                assert result["stored_since_backup"]["messages"] == 0
    assert report.read_text() == '{"synthetic":"prior"}'
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired, FileNotFoundError])
def test_keychain_failure_is_bounded_and_never_writes_secret(monkeypatch, capsys, failure):
    module = _module("synthetic_keychain_cleanup", "scripts/keychain_to_env.py")
    writes = []
    monkeypatch.setattr(module, "_env_write", lambda *args: writes.append(args))
    seen = []

    def failed_run(command, **kwargs):
        seen.append(kwargs)
        if failure is subprocess.TimeoutExpired:
            raise failure(command, 30, output="synthetic-private-canary")
        raise failure("synthetic-private-canary")

    monkeypatch.setattr(module.subprocess, "run", failed_run)
    assert module.main() == 1
    assert seen[0]["timeout"] == 30
    assert not writes
    output = capsys.readouterr()
    assert "synthetic-private-canary" not in output.out + output.err
