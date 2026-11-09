"""Real process signals at synthetic installer persistence boundaries."""
import json
import os
import signal
import subprocess
import time

import pytest

from install_sh_testkit import INSTALL, _calls, _inject_before_config_persistence, _run, _world


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize("boundary", ["before_config", "after_config"])
def test_signal_keeps_authority_and_rerun_never_duplicates(tmp_path, sig, boundary):
    home, hermes_home, stub, env = _world(tmp_path)
    ready = stub / "state/signal-ready"
    if boundary == "before_config":
        _inject_before_config_persistence(stub, '''
from pathlib import Path
import time
Path(os.environ['STUB_STATE'], 'signal-ready').touch()
while True:
    time.sleep(0.05)
''')
    else:
        runner = stub / "install-options-runner.py"
        original = runner.read_text()
        marker = "raise SystemExit(install_options.main(sys.argv[index + 1:]))"
        assert original.count(marker) == 1
        runner.write_text(original.replace(marker, """
action = sys.argv[index + 1]
result = install_options.main(sys.argv[index + 1:])
ready = Path(os.environ['STUB_STATE'], 'signal-ready')
if action == 'bootstrap-fresh' and result == 0 and not ready.exists():
    import time
    ready.touch()
    while True:
        time.sleep(0.05)
raise SystemExit(result)
"""))
    process = subprocess.Popen(
        ["sh", str(INSTALL), "--no-llm", "--no-recovery", str(hermes_home)],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        config = home / ".mcs/config.json"
        before = config.read_bytes() if config.exists() else None
        assert (before is not None) == (boundary == "after_config")
        if boundary == "after_config":
            assert (home / ".mcs/data/.provider-bootstrap-complete.json").is_file()
            assert not (home / ".mcs/data/.provider-bootstrap-fresh.json").exists()
        os.killpg(process.pid, sig)
        process.wait(timeout=5)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    (stub / "state/signal-release").touch()
    if boundary == "before_config":
        marker = home / ".mcs/data/.provider-bootstrap-fresh.json"
        saved = marker.read_bytes()
        calls = _calls(stub)
        result = _run(env, hermes_home, "--no-llm", "--no-recovery")
        assert result.returncode != 0
        assert marker.read_bytes() == saved and not config.exists()
        software = ("brew install", "git clone", "uv ", "launchctl bootstrap")
        assert [call for call in _calls(stub) if call.startswith(software)] == [
            call for call in calls if call.startswith(software)]
    else:
        result = _run(env, hermes_home, "--no-llm", "--no-recovery")
        assert result.returncode == 0, result.stdout + result.stderr
        completed = config.read_bytes()
        assert json.loads(before).items() <= json.loads(completed).items()
        calls = _calls(stub)
        assert sum(call.startswith("git clone") for call in calls) == 1
        again = _run(env, hermes_home, "--no-llm", "--no-recovery")
        assert again.returncode == 0, again.stdout + again.stderr
        assert config.read_bytes() == completed
        assert sum(call.startswith("git clone") for call in _calls(stub)) == 1
