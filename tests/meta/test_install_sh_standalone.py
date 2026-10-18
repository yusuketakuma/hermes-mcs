"""install.sh runtime selection: --mode standalone installs no Hermes,
an existing standalone config keeps its mode, non-interactive stays hermes."""
import json

import pytest

from test_install_sh import _calls, _run, _world


def test_standalone_preflight_and_plan_skip_hermes(tmp_path):
    home, _, stub_root, env = _world(tmp_path)
    r = _run(env, None, "--preflight", "--mode", "standalone")
    assert r.returncode == 0, r.stdout
    assert "接続方法: standalone" in r.stdout
    assert "Hermes 用 Python" not in r.stdout and "Hermes コマンド" not in r.stdout
    r = _run(env, None, "--dry-run", "--no-llm", "--mode", "standalone")
    assert r.returncode == 0, r.stdout
    assert "2/6 独立接続" in r.stdout and "2/6 Hermes" not in r.stdout
    assert "MCS 単独接続では使用しません" in r.stdout.split("3/6 プラグイン")[1].splitlines()[0]
    assert "--no-plugin" not in r.stdout
    assert list(home.iterdir()) == []


def test_mode_defaults_follow_existing_config(tmp_path):
    home, _, _, env = _world(tmp_path)
    r = _run(env, None, "--preflight")              # no tty, no config -> hermes
    assert "接続方法: hermes" in r.stdout
    (home / ".mcs").mkdir()
    (home / ".mcs" / "config.json").write_text(json.dumps({"runtime_mode": "standalone"}))
    r = _run(env, None, "--preflight")
    assert "接続方法: standalone" in r.stdout
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
    # (an empty trailing argument means no --recovery-python was given)
    assert any(c.rstrip().endswith(" standalone") and c.startswith("venv-python - ")
               for c in python)
    assert any("mcs_setup.py services" in c for c in python)
    assert (home / ".mcs" / "venv" / "bin" / "python3").exists() and venv


@pytest.mark.parametrize(("setup_answer", "setup_status"), [
    (b"1\n", 0), (b"2\n", 0), (b"0\n", 0), (b"1\n", 1), (b"1\n", 130)])
def test_interactive_install_asks_for_the_runtime(tmp_path, setup_answer, setup_status):
    import os
    import selectors
    import shutil
    import subprocess
    import sys
    import time
    import pytest
    if sys.platform != "darwin" or not shutil.which("script"):
        pytest.skip("BSD script(1) gives the installer a terminal")
    from test_install_sh import INSTALL
    home, hermes_home, stub_root, env = _world(tmp_path)
    env["STUB_SETUP_EXIT"] = str(setup_status)
    # The pipe retains the exact prompt until subscribed; answer only on that signal.
    with subprocess.Popen(
        ["script", "-q", "/dev/null", "sh", str(INSTALL), "--no-llm", "--no-brew"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    ) as process, selectors.DefaultSelector() as ready:
        assert process.stdout is not None and process.stdin is not None
        ready.register(process.stdout, selectors.EVENT_READ)
        output = b""
        deadline = time.monotonic() + 10
        try:
            for prompt, answer in [
                ("選択 [1・推奨 / 2 / 0・中止]: ", b"wrong\n"),
                ("1 または 2 を入力してください。中止は 0 です。", b"2\n"),
                ("この内容で導入する（推奨）", b"1\n"),
                ("続けて初期設定を始める（推奨）", setup_answer),
            ]:
                signal = prompt.encode()
                while signal not in output:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and ready.select(remaining), output
                    chunk = os.read(process.stdout.fileno(), 4096)
                    assert chunk and len(output) + len(chunk) <= 65536, output
                    output += chunk
                process.stdin.write(answer)
                process.stdin.flush()
            # Keep BSD script's input open until the last answer has been consumed.
            deadline = time.monotonic() + 120
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0 and ready.select(remaining), output
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
            process.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            if process.poll() is None:
                process.kill()
        assert process.returncode == setup_status, output
    assert "【2/6】MCS 単独接続の実行環境".encode() in output, output
    assert not hermes_home.exists()
    if setup_status:
        assert "初期設定は完了していません".encode() in output
        assert "導入が途中で停止しました".encode() not in output
    assert "LINE WORKS を選んだ場合".encode() in output
    assert "自動実行の登録を省略した場合".encode() in output
    assert any("mcs_setup.py init" in call for call in _calls(stub_root)) == (setup_answer == b"1\n")


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
    assert "選択 [1・推奨" not in r.stdout and "接続方法: hermes" in r.stdout
    r = subprocess.run(["sh", str(INSTALL), "--no-llm", "--no-brew"], env=env,
                       capture_output=True, text=True, timeout=120)
    assert cfg.read_text() == '{"mcs_login_id": "synthetic"}\n'
    assert not any(c.startswith("venv-python - ") for c in _calls(stub_root))


def test_mode_selection_refuses_a_symlinked_config(tmp_path):
    """The --mode writer reads config.json like the recovery check: a symlink is
    refused, not followed and then replaced by a regular file."""
    import sys
    home, _, _, env = _world(tmp_path)
    env["STUB_REAL_PYTHON"] = sys.executable
    (home / ".mcs").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"runtime_mode": "hermes"}))
    (home / ".mcs" / "config.json").symlink_to(elsewhere)
    r = _run(env, None, "--mode", "standalone", "--no-llm", "--no-brew", "--no-recovery")
    assert r.returncode == 1 and "config.json を読み取れない" in r.stderr, r.stdout + r.stderr
    assert (home / ".mcs" / "config.json").is_symlink()
    assert json.loads(elsewhere.read_text()) == {"runtime_mode": "hermes"}
