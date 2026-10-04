"""Rendered tick wrappers forward explicit grace without starting MCS."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

import mcs_setup


@pytest.mark.parametrize("script", ["mcs_check.sh", "mcs_deep.sh"])
@pytest.mark.parametrize("grace", [0, 60, 120])
def test_rendered_wrapper_uses_configured_watchdog_grace(tmp_path, script, grace):
    data = tmp_path / "data"
    data.mkdir()
    record = tmp_path / "argv.jsonl"
    python = tmp_path / "synthetic-python"
    python.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "with open(os.environ['ARGV_RECORD'], 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n")
    python.chmod(0o700)
    subs = {"DATA": str(data), "PYTHON": str(python), "REPO": str(tmp_path),
            "WATCHDOG_GRACE": mcs_setup._service_subs({"watchdog_grace_s": grace})["WATCHDOG_GRACE"]}
    source = Path(__file__).resolve().parents[2] / "deployment" / "scripts" / script
    target = tmp_path / script
    target.write_text(mcs_setup._render_template(
        source.read_text(), {key: shlex.quote(value) for key, value in subs.items()}))
    process = subprocess.run(["/bin/bash", str(target)], capture_output=True, text=True,
                             env={**os.environ, "ARGV_RECORD": str(record)}, timeout=5)
    assert process.returncode == 0, process.stderr
    assert process.stdout == "" and process.stderr == ""
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    argv = next(row for row in calls if row[0].endswith("/mcs/ingest/run_check.py"))
    if grace:
        assert argv[-2:] == ["--watchdog-grace", str(grace)]
    else:
        assert "--watchdog-grace" not in argv


def test_grace_default_and_invalid_values_are_checked_before_render():
    assert mcs_setup._service_subs({})["WATCHDOG_GRACE"] == "60"
    for value in (-1, True, "60", 3601):
        with pytest.raises(ValueError, match="watchdog_grace_config_invalid"):
            mcs_setup._service_subs({"watchdog_grace_s": value})
