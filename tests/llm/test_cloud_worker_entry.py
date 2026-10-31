"""The cloud worker bootstraps its imports from a clean process."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("isolated", [False, True])
def test_cloud_worker_help_bootstraps_before_flat_imports(tmp_path, isolated):
    script = Path(__file__).resolve().parents[2] / "mcs" / "llm" / "cloud_backlog.py"
    command = [sys.executable, "-B", *(["-I"] if isolated else []), str(script), "--help"]
    env = {"HOME": str(tmp_path), "MCS_ROOT": str(tmp_path), "PATH": os.defpath, "PYTHONPATH": ""}
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--parent" in result.stdout
    assert not (tmp_path / "data").exists()
