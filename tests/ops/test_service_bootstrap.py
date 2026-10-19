"""Candidate configuration preflight needs only the extracted mcs source tree."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_old_updater_mcs_only_validate_entrance_has_no_outside_runtime_import(tmp_path):
    source = Path(__file__).resolve().parents[2] / "mcs"
    candidate = tmp_path / "candidate" / "mcs"
    shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    program = """
import sys
sys.path.insert(0, sys.argv[1])
class RejectOutsideRuntime:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'mcs_standalone', 'hermes_cli', 'gateway', 'discord', 'slack_bolt', 'slack_sdk'}:
            raise AssertionError('mcs-only validate tried outside runtime import: ' + fullname)
sys.meta_path.insert(0, RejectOutsideRuntime())
import _mcs_path
import mcs_setup
import mcs_config
assert mcs_setup.validate_config is mcs_config.validate_config
errors, warnings = mcs_setup.validate_config({'runtime_mode': 'standalone',
    'mcs_login_id': 'synthetic-login', 'notify_target': 'local'})
assert isinstance(errors, list) and isinstance(warnings, list)
assert 'service_management' in sys.modules and 'service_contract' in sys.modules
assert not any(name.split('.')[0] == 'mcs_standalone' for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-I", "-c", program, str(candidate)],
        env={**os.environ, "MCS_ROOT": str(tmp_path / "root"), "MCS_TEST_SANDBOX": "1"},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
