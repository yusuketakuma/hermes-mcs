"""Deployment wrappers preserve quiesce guards and collect around the clock."""
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deployment" / "scripts"

def test_drainer_spawn_scripts_guard_quiesce_marker():
    """Any wrapper that can spawn drainer work (extract_llm /
    semantic_drain) must exit on the update-in-progress marker — a
    cron firing mid-quiesce must never respawn what the updater just
    stopped (S8). Plain tick wrappers don't need the guard: they
    serialize on run.lock, which the updater holds."""
    for path in sorted(SCRIPTS.glob("*.sh")):
        body = path.read_text(encoding="utf-8")
        if "extract_llm.py" in body or "semantic_drain.py" in body:
            assert "update_in_progress.marker" in body, path.name




def test_check_runs_around_the_clock(tmp_path):
    """The scheduled tick always runs — no time-of-day thinning."""
    import subprocess
    import sys
    body = (SCRIPTS / "mcs_check.sh").read_text(encoding="utf-8")
    runner = tmp_path / "runner.sh"
    body = body.replace("__DATA__", str(tmp_path)) \
        .replace("__REPO__", str(tmp_path)) \
        .replace("__PYTHON__", str(tmp_path / "py")) \
        .replace("__WATCHDOG_GRACE__", "60")
    (tmp_path / "py").write_text(
        f'#!/bin/sh\n[ "$1" = "-" ] && exec {sys.executable} "$@"\n'
        'echo ran >> "$0.log"\n')
    (tmp_path / "py").chmod(0o755)
    runner.write_text(body)
    subprocess.run(["bash", str(runner)], env={"HOME": str(tmp_path)},
                   capture_output=True, timeout=30)
    assert (tmp_path / "py.log").exists()


def test_check_incomplete_alert_names_its_cause(tmp_path):
    """collection=incomplete names unread gaps and stalled backfills
    separately and never prints an empty project list; ok is silent."""
    import json
    import subprocess
    import sys
    body = (SCRIPTS / "mcs_check.sh").read_text(encoding="utf-8")
    runner = tmp_path / "runner.sh"
    runner.write_text(body.replace("__DATA__", str(tmp_path))
                      .replace("__REPO__", str(tmp_path))
                      .replace("__PYTHON__", str(tmp_path / "py"))
                      .replace("__WATCHDOG_GRACE__", "60"))
    # run_check stub exits 0; the health heredoc runs on a real python
    (tmp_path / "py").write_text(
        f'#!/bin/sh\n[ "$1" = "-" ] && exec {sys.executable} "$@"\nexit 0\n')
    (tmp_path / "py").chmod(0o755)
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "date").write_text(
        "#!/bin/sh\ncase \"$1\" in\n  +%H) echo 12 ;;\n  +%M) echo 00 ;;\n"
        "  *) /bin/date \"$@\" ;;\nesac\n")
    (bindir / "date").chmod(0o755)
    for health, want in (
            ({"collection": "incomplete", "incomplete_projects": [7],
              "coverage_stalled": []},
             "mcs check: collection incomplete — projects 7\n"),
            ({"collection": "incomplete", "incomplete_projects": [],
              "coverage_stalled": [12, 34]},
             "mcs check: collection incomplete — stalled=12,34\n"),
            ({"collection": "ok", "incomplete_projects": [],
              "coverage_stalled": []}, "")):
        (tmp_path / "health.json").write_text(json.dumps(health))
        out = subprocess.run(["bash", str(runner)],
                             env={"HOME": str(tmp_path)},
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0 and out.stdout == want, health


def _llama_restart(tmp_path, launchctl_body, *,
                   curl_body="printf '[{\"is_processing\":false}]\\n'\n",
                   extra_env=None, clock_step=900, monotonic_values=None, probe_timeout=False, log_failure=None):
    """Exercise the actual wrapper using only synthetic local command stubs."""
    import subprocess
    import sys
    bindir = tmp_path / "stub"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text(
        '#!/bin/sh\n' + 'printf "%s\\n" "$@" >> "$0.args"\n' + curl_body)
    (bindir / "launchctl").write_text(
        '#!/bin/sh\n' + 'printf "%s\\n" "$@" >> "$0.args"\n' + launchctl_body)
    for f in (bindir / "curl", bindir / "launchctl"):
        f.chmod(0o755)
    for path in bindir.glob("*.args"):
        path.unlink()
    body = (SCRIPTS / "llamacpp_restart_if_idle.sh").read_text(
        encoding="utf-8")
    # Keep subprocess.wait's clock real; only the wrapper's polling is synthetic.
    clock = (
        "from types import SimpleNamespace\n"
        "_clock = [0]\n"
        "def wall_clock_jump():\n"
        "    raise AssertionError('wall clock must not control the wait')\n"
        "time = SimpleNamespace(monotonic=lambda: _clock[0], time=wall_clock_jump,\n"
        f"    sleep=lambda seconds: _clock.__setitem__(0, _clock[0] + max(seconds, {clock_step})))\n"
    )
    if log_failure:
        method = "mkdir" if log_failure == "mkdir" else "open"
        clock += (
            "from pathlib import Path\n"
            "def fail_log(*args, **kwargs):\n"
            "    raise OSError('synthetic-private-detail:/private/injected-path')\n"
            f"Path.{method} = fail_log\n"
        )
    if monotonic_values is not None:
        clock += (f"_ticks = iter({monotonic_values!r})\n"
                  f"time.monotonic = lambda: next(_ticks, {monotonic_values[-1]!r})\n")
    if probe_timeout:
        body = body.replace("import selectors\n",
                            "import selectors\n"
                            "selectors.DefaultSelector.select = lambda self, timeout: []\n")
    runner = tmp_path / "runner.sh"
    runner.write_text(body.replace("/usr/bin/curl", str(bindir / "curl"))
                      .replace("/bin/launchctl", str(bindir / "launchctl"))
                      .replace("/usr/bin/python3", sys.executable)
                      .replace("import time\n", "import time\n" + clock)
                      .replace("__DATA__", str(tmp_path / "data")))
    log = tmp_path / "data" / "llamacpp-restart.log"
    log.unlink(missing_ok=True)
    proc = subprocess.run(["bash", str(runner)],
                          env={"HOME": str(tmp_path),
                               "PATH": "/usr/bin:/bin", **(extra_env or {})},
                          capture_output=True, text=True, timeout=30)
    return proc, log.read_text() if log.exists() else ""


def test_llama_restart_without_agent_is_a_clean_skip(tmp_path):
    """--no-llm / self-managed server does not even query the server."""
    proc, log = _llama_restart(tmp_path, "exit 113\n")
    assert proc.returncode == 0
    assert "restart skipped" in log
    assert not (tmp_path / "stub/curl.args").exists()
    arguments = (tmp_path / "stub/launchctl.args").read_text()
    assert "ai.hermes.llamacpp" in arguments and "ai.mcs.llamaserver" in arguments
    assert "kickstart" not in arguments


def test_llama_restart_reports_failed_kickstart(tmp_path):
    """A failed kickstart preserves its exit status and never reports success."""
    proc, log = _llama_restart(
        tmp_path, '[ "$1" = kickstart ] && exit 5\nexit 0\n')
    assert proc.returncode == 5
    assert "FAILED rc=5" in log and "restarted" not in log
    proc, log = _llama_restart(tmp_path, "exit 0\n")
    assert proc.returncode == 0
    assert "restarted ai.hermes.llamacpp" in log


def test_llama_restart_falls_back_to_loaded_repo_label(tmp_path):
    proc, log = _llama_restart(
        tmp_path, 'case "$2" in */ai.hermes.llamacpp) exit 113;; esac\nexit 0\n')
    assert proc.returncode == 0
    assert "restarted ai.mcs.llamaserver" in log


def test_llama_restart_waits_for_probe_exit_after_stdout_closes(tmp_path):
    proc, log = _llama_restart(
        tmp_path, "exit 0\n",
        curl_body="printf '[{\"is_processing\":false}]\\n'\nexec 1>&-\nsleep 0.1\n")
    assert proc.returncode == 0
    assert "restarted ai.hermes.llamacpp" in log
    assert (tmp_path / "stub/launchctl.args").read_text().splitlines().count("kickstart") == 1


def test_llama_slot_probe_bypasses_inherited_proxy(tmp_path):
    """The loopback probe has no proxy, redirect, or unbounded request."""
    proc, log = _llama_restart(
        tmp_path, "exit 0\n",
        extra_env={"http_proxy": "http://synthetic-proxy.invalid:8080",
                   "ALL_PROXY": "http://synthetic-proxy.invalid:8080"})
    assert proc.returncode == 0 and "idle" in log
    arguments = (tmp_path / "stub/curl.args").read_text().splitlines()
    assert arguments[0] == "--disable"
    assert arguments[arguments.index("--noproxy") + 1] == "*"
    assert arguments[arguments.index("--max-redirs") + 1] == "0"
    assert arguments[arguments.index("--max-time") + 1] == "5"
    assert "--fail" in arguments
    assert "-L" not in arguments and "--location" not in arguments
    assert arguments[-1] == "http://127.0.0.1:8080/slots"


def test_llama_restart_waits_until_busy_becomes_idle(tmp_path):
    curl = (
        'if [ -f "$0.count" ]; then\n'
        "  printf '[{\"is_processing\":false},{\"is_processing\":false}]\\n'\n"
        'else\n'
        '  touch "$0.count"\n'
        "  printf '[{\"is_processing\":true},{\"is_processing\":false}]\\n'\n"
        'fi\n'
    )
    proc, log = _llama_restart(tmp_path, "exit 0\n", curl_body=curl, clock_step=0)
    assert proc.returncode == 0 and "restarted" in log
    probes = (tmp_path / "stub/curl.args").read_text().splitlines()
    assert probes.count("http://127.0.0.1:8080/slots") == 2
    assert (tmp_path / "stub/launchctl.args").read_text().splitlines().count("kickstart") == 1


@pytest.mark.parametrize("payload", [
    '[{"is_processing":true}]',
    '', 'not-json', 'null', '{}', '[]', '[false]', '[{}]',
    '[{"processing":false}]',
    '[{"is_processing":true,"is_processing":false}]',
    '[{"is_processing":false,"is_processing":true}]',
    '[{"is_processing":false,"id":0,"id":1}]',
    '[{"is_processing":false},{}]',
    *[json.dumps([{"is_processing": value}]) for value in (None, 0, 1, "false", [], {})],
])
def test_llama_restart_skips_busy_or_unknown_at_deadline(tmp_path, payload):
    # The payload file and command are wholly synthetic; no real curl executes.
    (tmp_path / "slots.json").write_text(payload)
    proc, log = _llama_restart(
        tmp_path, "exit 0\n", curl_body='cat "$HOME/slots.json"\n')
    assert proc.returncode == 0 and "after 15m — restart skipped" in log
    assert "kickstart" not in (tmp_path / "stub/launchctl.args").read_text()


@pytest.mark.parametrize("curl_body", [
    "exit 7\n",
    "printf '[{\"is_processing\":false}]'; exit 22\n",
])
def test_llama_restart_skips_failed_probe(tmp_path, curl_body):
    proc, log = _llama_restart(tmp_path, "exit 0\n", curl_body=curl_body)
    assert proc.returncode == 0 and "restart skipped" in log
    assert "kickstart" not in (tmp_path / "stub/launchctl.args").read_text()


def test_llama_restart_does_not_use_wall_clock_or_restart_after_deadline(tmp_path):
    proc, log = _llama_restart(
        tmp_path, "exit 0\n", monotonic_values=[0, 0, 0, 0, 0, 0, 900])
    assert proc.returncode == 0 and "restart skipped" in log
    assert "kickstart" not in (tmp_path / "stub/launchctl.args").read_text()


def test_llama_restart_skips_probe_timeout(tmp_path):
    proc, log = _llama_restart(tmp_path, "exit 0\n", probe_timeout=True)
    assert proc.returncode == 0 and "restart skipped" in log
    assert "kickstart" not in (tmp_path / "stub/launchctl.args").read_text()


def test_llama_restart_rejects_oversized_valid_idle_json(tmp_path):
    (tmp_path / "slots.json").write_text('[{"is_processing":false}]' + " " * 65536)
    proc, log = _llama_restart(
        tmp_path, "exit 0\n", curl_body='cat "$HOME/slots.json"\n')
    assert proc.returncode == 0 and "restart skipped" in log
    assert "kickstart" not in (tmp_path / "stub/launchctl.args").read_text()


@pytest.mark.parametrize("failure", ["mkdir", "open"])
def test_llama_restart_log_failure_has_no_private_traceback(tmp_path, failure):
    proc, log = _llama_restart(
        tmp_path, "exit 0\n", log_failure=failure)
    assert proc.returncode == 1
    assert proc.stdout == "llamacpp restart: failed (OSError)\n"
    assert proc.stderr == ""
    assert "synthetic-private-detail" not in proc.stdout + proc.stderr + log
    assert "/private/injected-path" not in proc.stdout + proc.stderr + log
    trace = tmp_path / "stub/launchctl.args"
    assert not trace.exists() or "kickstart" not in trace.read_text()
