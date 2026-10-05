"""semantic.py の保存先が MCS_ROOT に従うことの回帰テスト。"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_semantic_paths_follow_mcs_root(tmp_path):
    root = tmp_path / "root"
    code = (
        "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
        "import _mcs_path, semantic;"
        "print(semantic.DB); print(semantic.CONF_PATH);"
        "print(semantic._FMT_PATH); print(semantic._LONG_PATH)"
        % (os.path.join(ROOT, "mcs"), os.path.join(ROOT, "mcs", "semantic")))
    env = {**os.environ, "MCS_ROOT": str(root),
           "HOME": str(tmp_path / "fakehome")}
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, check=True).stdout
    lines = out.split()
    assert len(lines) == 4
    for p in lines:
        assert p.startswith(str(root) + os.sep), p
