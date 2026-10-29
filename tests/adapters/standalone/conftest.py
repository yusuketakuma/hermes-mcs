"""Supply installed synthetic runtime-log helpers without touching user services."""
from pathlib import Path
import hashlib

import pytest

from mcs_standalone import runtime
import run_log_install

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def installed_runtime_log_helper(tmp_path, monkeypatch):
    monkeypatch.delenv("MCS_RUN_LOG_HELPER", raising=False)
    real = run_log_install.helper_path
    prepared = set()
    def helper(root, recovery_dir=None, **kwargs):
        root = Path(root).resolve()
        assert root.is_relative_to(tmp_path), "synthetic runtime root required"
        recovery = tmp_path / ("recovery-" + hashlib.sha256(str(root).encode()).hexdigest()[:12])
        if root not in prepared:
            recovery.mkdir(mode=0o700)
            run_log_install.prepare(root, REPO, recovery, promote=True)
            prepared.add(root)
        return real(root, recovery, **kwargs)
    monkeypatch.setattr(runtime, "helper_path", helper)
