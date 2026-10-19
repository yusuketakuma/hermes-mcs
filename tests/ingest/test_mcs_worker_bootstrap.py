"""Direct worker entry bootstraps without inherited checkout import paths."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("python_flags", [(), ("-I",)], ids=["direct", "isolated"])
def test_direct_worker_bootstraps_from_outside_checkout(tmp_path, python_flags):
    worker = Path(__file__).resolve().parents[2] / "mcs/ingest/mcs_worker.py"
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    # Unknown operations are refused before constructing an adapter or opening I/O.
    envelope = {"operation": "synthetic_invalid_operation", "timeout": 1}
    result = subprocess.run(
        [sys.executable, *python_flags, "-B", str(worker)],
        input=json.dumps(envelope), text=True, capture_output=True,
        cwd=outside,
        env={"HOME": str(home), "MCS_ROOT": str(home / "mcs"),
             "PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "ok": False, "kind": "url_not_allowed", "status": None,
        "retryable": False,
    }
