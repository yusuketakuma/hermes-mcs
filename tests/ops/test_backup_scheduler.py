"""Disabled-by-default scheduler boundary; only synthetic policy and process stubs."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

import mcs_backup as backup
import mcs_setup


SCRIPT = Path(__file__).resolve().parents[2] / "deployment/scripts/mcs_offsite.sh"


def _wrapper(tmp_path, exit_code=0, *, enabled=False):
    data = tmp_path / "private data"
    data.mkdir(mode=0o700)
    interpreter = tmp_path / "python stub"
    calls = tmp_path / "calls.json"
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(calls)!r}, 'w') as f:\n"
        "    json.dump({'argv':sys.argv[1:], 'path':os.environ['PATH']}, f)\n"
        "print('synthetic output must not leak')\n"
        f"sys.exit({exit_code})\n")
    interpreter.chmod(0o700)
    runner = tmp_path / "wrapper.sh"
    subs = {"DATA": str(data), "PYTHON": str(interpreter),
            "REPO": str(tmp_path / "repo with spaces"),
            "BACKUP_ENABLED": "1" if enabled else "0",
            "BACKUP_POLICY": str(tmp_path / "owner policy.json"),
            "BACKUP_SNAPSHOT": "",
            "BACKUP_SNAPSHOT_DIR": str(tmp_path / "daily backups")}
    runner.write_text(next(body for name, body in mcs_setup._rendered_scripts(subs)
                           if name == SCRIPT.name))
    return runner, data, calls


def test_wrapper_is_disabled_without_explicit_enable(tmp_path):
    runner, _, calls = _wrapper(tmp_path)
    result = subprocess.run(["/bin/bash", str(runner)], capture_output=True,
                            env={"HOME": str(tmp_path)}, timeout=10)
    assert result.returncode == 0 and result.stdout == b"" and not calls.exists()


def test_wrapper_skips_during_quiesce_without_starting_work(tmp_path):
    runner, data, calls = _wrapper(tmp_path)
    (data / "update_in_progress.marker").write_text("synthetic update")
    result = subprocess.run(["/bin/bash", str(runner), "--enable"],
                            capture_output=True, env={"HOME": str(tmp_path)}, timeout=10)
    assert result.returncode == 0 and not calls.exists()


@pytest.mark.parametrize("code", [0, 1, 7])
def test_wrapper_forwards_explicit_policy_snapshot_and_redacts_output(tmp_path, code):
    runner, data, calls = _wrapper(tmp_path, code)
    args = ["--policy", str(tmp_path / "owner-policy.json"),
            "--snapshot-dir", str(tmp_path / "backups")]
    result = subprocess.run(["/bin/bash", str(runner), "--enable", *args],
                            capture_output=True, env={"HOME": str(tmp_path),
                                                      "PATH": "/synthetic/untrusted"},
                            timeout=10)
    invoked = json.loads(calls.read_text())
    assert invoked["argv"] == [
        str(tmp_path / "repo with spaces/mcs/ops/mcs_backup.py"), "offsite",
        "--scheduled", "--keychain", "--state-dir", str(data), *args]
    assert invoked["path"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert result.returncode == code
    assert result.stdout == (f"mcs offsite: backup failed (exit {code})\n".encode()
                             if code else b"")
    assert result.stderr == b""


def test_rendered_owner_opt_in_works_without_cron_arguments(tmp_path):
    runner, data, calls = _wrapper(tmp_path, enabled=True)
    result = subprocess.run(["/bin/bash", str(runner)], capture_output=True,
                            env={"HOME": str(tmp_path)}, timeout=10)
    assert result.returncode == 0 and result.stdout == b""
    assert json.loads(calls.read_text())["argv"] == [
        str(tmp_path / "repo with spaces/mcs/ops/mcs_backup.py"), "offsite",
        "--scheduled", "--keychain", "--state-dir", str(data),
        "--policy", str(tmp_path / "owner policy.json"),
        "--snapshot-dir", str(tmp_path / "daily backups")]


@pytest.mark.parametrize("scheduled", [None, False, "true", 1])
def test_cli_schedule_requires_literal_owner_opt_in_before_any_key_or_backup(tmp_path,
                                                                           scheduled, capsys):
    # Absent/false is a visible disabled no-op; a non-boolean value is an
    # invalid owner policy and must fail (exit 1), never look disabled.
    document = {} if scheduled is None else {"scheduled": scheduled}
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(document))
    policy.chmod(0o600)
    args = ["offsite", "--scheduled", "--keychain", "--policy", str(policy),
            "--state-dir", str(tmp_path / "not-created")]
    result = backup.main(args, keychain_reader=lambda: pytest.fail("disabled schedule read key"))
    expected = ((0, {"status": "disabled"}) if scheduled in (None, False)
                else (1, {"error": "backup_policy_required"}))
    assert (result, json.loads(capsys.readouterr().out)) == expected
    assert not (tmp_path / "not-created").exists()


def test_enabled_schedule_without_explicit_owner_policy_fails_closed(tmp_path, capsys):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"scheduled": True}))
    policy.chmod(0o600)
    result = backup.main(
        ["offsite", "--scheduled", "--keychain", "--policy", str(policy),
         "--state-dir", str(tmp_path)],
        keychain_reader=lambda: pytest.fail("invalid policy read key"))
    assert result == 1
    assert json.loads(capsys.readouterr().out) == {"error": "backup_io_or_policy_failed"}
    assert not (tmp_path / "backup_state.json").exists()
