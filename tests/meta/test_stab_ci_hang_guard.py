"""The root conftest.py per-test hang guard: a wedged test is cut with every
thread's traceback; the limit applies to each test, not to the session."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path, body, limit):
    shutil.copy(ROOT / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(body)
    env = dict(os.environ, MCS_TEST_TIMEOUT=str(limit))
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         "-o", "addopts=", "--rootdir", str(tmp_path), str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    return proc, time.monotonic() - started


def test_hung_test_is_cut_with_its_traceback(tmp_path):
    proc, elapsed = _run(tmp_path, (
        "import time\n\n"
        "def test_wedged():\n"
        "    print('captured output')\n"
        "    time.sleep(60)\n"), limit=1)
    assert proc.returncode != 0
    assert elapsed < 50
    # the dump reaches the real stderr, not pytest's capture buffer
    assert "Timeout" in proc.stderr
    assert "test_wedged" in proc.stderr


def test_limit_is_per_test_and_disarmed_after_each(tmp_path):
    body = "import time\n\n" + "".join(
        f"def test_short_{i}():\n    time.sleep(0.6)\n\n" for i in range(3))
    proc, _ = _run(tmp_path, body, limit=1.5)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "3 passed" in proc.stdout
