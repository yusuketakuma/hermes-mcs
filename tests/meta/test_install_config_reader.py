"""Installer rejects unreadable configuration before installing any runtime."""
import os
import signal
import subprocess
import sys

import pytest

from test_install_sh import INSTALL, _calls, _world


@pytest.mark.parametrize("mode", ["install", "preflight", "dry-run"])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "fifo", "directory"])
def test_unsafe_config_stops_before_installer_effects(tmp_path, mode, kind):
    home, _, stub, env = _world(tmp_path)
    env["STUB_REAL_PYTHON"] = sys.executable
    config = home / ".mcs/config.json"
    config.parent.mkdir()
    target = tmp_path / "elsewhere.json"
    if kind in ("symlink", "dangling"):
        if kind == "symlink":
            target.write_text('{"runtime_mode":"standalone"}')
        config.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(config, 0o600)
    else:
        config.mkdir()
    flags = ["--mode", "standalone", "--no-brew", "--no-llm", "--no-plugin",
             "--no-services", "--no-recovery"]
    if mode != "install":
        flags.append("--" + mode)
    child = subprocess.Popen(["sh", str(INSTALL), *flags], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    try:
        try:
            stdout, stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            assert any(line.startswith("python3 - ") for line in _calls(stub))
            pytest.fail("installer config reader blocked on a FIFO")
        assert child.returncode != 0, stdout + stderr
        assert "unreadable" in stderr
        assert sorted(path.name for path in home.iterdir()) == [".mcs"]
        assert not any(line.startswith(("git clone", "uv ", "brew install",
                                        "venv-hermes", "launchctl"))
                       for line in _calls(stub))
        if kind in ("symlink", "dangling"):
            assert config.is_symlink()
        if kind == "symlink":
            assert target.read_text() == '{"runtime_mode":"standalone"}'
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate(timeout=5)
