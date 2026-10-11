"""Regression: extract/semantic/export modules derive their data paths from MCS_ROOT."""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.parametrize("module,attrs", [
    ("extract_llm", ("HOME", "DB")),
    ("rollup", ("HOME", "DB")),
    ("extract", ("HOME", "DB")),
    ("semantic_observe", ("HOME", "DB")),
    ("semantic_bench", ("DB",)),
    ("brain_export", ("HOME", "SNAPSHOT", "OUT_DIR")),
])
def test_module_paths_follow_mcs_root(tmp_path, module, attrs):
    root = tmp_path / "root"
    code = ("import sys; sys.path.insert(0, %r); import _mcs_path, %s as m;"
            "print('\\n'.join(str(getattr(m, a)) for a in %r))"
            % (os.path.join(ROOT, "mcs"), module, attrs))
    env = {**os.environ, "MCS_ROOT": str(root), "HOME": str(tmp_path / "fakehome")}
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, check=True).stdout
    lines = out.splitlines()
    assert len(lines) == len(attrs)
    for path in lines:
        assert path == str(root) or path.startswith(str(root) + os.sep), path
