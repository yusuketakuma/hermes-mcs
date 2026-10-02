"""install.sh runtime selection: --mode standalone installs no Hermes,
an existing standalone config keeps its mode, non-interactive stays hermes."""
import json

from test_install_sh import _calls, _run, _world


def test_standalone_preflight_and_plan_skip_hermes(tmp_path):
    home, _, stub_root, env = _world(tmp_path)
    r = _run(env, None, "--preflight", "--mode", "standalone")
    assert r.returncode == 0, r.stdout
    assert "runtime mode: standalone" in r.stdout
    assert "hermes venv" not in r.stdout and "hermes on PATH" not in r.stdout
    r = _run(env, None, "--dry-run", "--no-llm", "--mode", "standalone")
    assert r.returncode == 0, r.stdout
    assert "2/6 standalone" in r.stdout and "2/6 hermes" not in r.stdout
    assert "not used in standalone mode" in r.stdout.split("3/6 plugin")[1].splitlines()[0]
    assert "--no-plugin" not in r.stdout
    assert list(home.iterdir()) == []


def test_mode_defaults_follow_existing_config(tmp_path):
    home, _, _, env = _world(tmp_path)
    r = _run(env, None, "--preflight")              # no tty, no config -> hermes
    assert "runtime mode: hermes" in r.stdout
    (home / ".mcs").mkdir()
    (home / ".mcs" / "config.json").write_text(json.dumps({"runtime_mode": "standalone"}))
    r = _run(env, None, "--preflight")
    assert "runtime mode: standalone" in r.stdout
    assert _run(env, None, "--preflight", "--mode", "bogus").returncode == 2


def test_standalone_install_never_clones_hermes(tmp_path):
    home, hermes_home, stub_root, env = _world(tmp_path)
    r = _run(env, None, "--mode", "standalone", "--no-llm", "--no-brew")
    calls = _calls(stub_root)
    assert not any("hermes-agent" in c and c.startswith("git clone") for c in calls), calls
    assert not hermes_home.exists()
    assert r.returncode == 0, r.stdout + r.stderr
    venv = str(home / ".mcs" / "venv")
    assert any(c.startswith("uv pip install") and "requirements-standalone.txt" in c
               for c in calls), calls
    python = [c for c in calls if c.startswith("venv-python")]
    # the chosen mode is persisted to config.json, then services run on ~/.mcs/venv
    assert any(c.endswith(" standalone") and c.startswith("venv-python - ") for c in python)
    assert any("mcs_setup.py services" in c for c in python)
    assert (home / ".mcs" / "venv" / "bin" / "python3").exists() and venv


def test_interactive_install_asks_for_the_runtime(tmp_path):
    import shutil
    import subprocess
    import sys
    import pytest
    if sys.platform != "darwin" or not shutil.which("script"):
        pytest.skip("BSD script(1) gives the installer a terminal")
    from test_install_sh import INSTALL
    home, hermes_home, stub_root, env = _world(tmp_path)
    # type the answer only once the prompt is up, as a person would
    feeder = subprocess.Popen(["sh", "-c", "sleep 2; printf '2\\n'; sleep 60"],
                              stdout=subprocess.PIPE)
    try:
        r = subprocess.run(["script", "-q", "/dev/null", "sh", str(INSTALL), "--no-llm", "--no-brew"],
                           stdin=feeder.stdout, env=env, capture_output=True, text=True, timeout=120)
    finally:
        feeder.kill()
    assert "choose 1 or 2" in r.stdout, r.stdout
    assert "2/6 standalone runtime" in r.stdout, r.stdout
    assert not hermes_home.exists()


def test_rerun_on_an_existing_install_never_prompts_or_writes(tmp_path):
    import shutil
    import subprocess
    import sys
    import pytest
    if sys.platform != "darwin" or not shutil.which("script"):
        pytest.skip("BSD script(1) gives the installer a terminal")
    from test_install_sh import INSTALL
    home, hermes_home, stub_root, env = _world(tmp_path)
    (home / ".mcs").mkdir()
    cfg = home / ".mcs" / "config.json"
    cfg.write_text('{"mcs_login_id": "synthetic"}\n')
    r = subprocess.run(["script", "-q", "/dev/null", "sh", str(INSTALL), "--preflight"],
                       stdin=subprocess.DEVNULL, env=env, capture_output=True, text=True, timeout=120)
    assert "choose 1 or 2" not in r.stdout and "runtime mode: hermes" in r.stdout
    r = subprocess.run(["sh", str(INSTALL), "--no-llm", "--no-brew"], env=env,
                       capture_output=True, text=True, timeout=120)
    assert cfg.read_text() == '{"mcs_login_id": "synthetic"}\n'
    assert not any(c.startswith("venv-python - ") for c in _calls(stub_root))
