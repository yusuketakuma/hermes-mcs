"""Deployment wrappers preserve quiesce guards and collect around the clock."""
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "deployment" / "scripts"

# hermes cron default script_timeout_seconds — keep a real margin for
# interpreter startup, lock waits, and the gap-fill spawn.
HERMES_CRON_SCRIPT_TIMEOUT_S = 3600
MARGIN_S = 300




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
    """Every scheduled tick runs at day and night, including 22:37."""
    import json
    import subprocess
    import sys
    body = (SCRIPTS / "mcs_check.sh").read_text(encoding="utf-8")
    runner = tmp_path / "runner.sh"
    body = body.replace("__DATA__", str(tmp_path)) \
        .replace("__REPO__", str(tmp_path)) \
        .replace("__PYTHON__", str(tmp_path / "py"))
    (tmp_path / "py").write_text(
        f'#!/bin/sh\n[ "$1" = "-" ] && exec {sys.executable} "$@"\n'
        'echo ran >> "$0.log"\n')
    (tmp_path / "py").chmod(0o755)
    runner.write_text(body)
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    for hour, minute, thinning, ran in (
            ("23", "02", None, True), ("23", "20", None, True),
            ("23", "07", None, True), ("12", "07", None, True),
            ("22", "37", False, True), ("06", "15", False, True)):
        health = {} if thinning is None else {"night_thinning": thinning}
        (tmp_path / "config.json").write_text(json.dumps({"health": health}))
        log = tmp_path / "py.log"
        log.unlink(missing_ok=True)
        (bindir / "date").write_text(
            "#!/bin/sh\ncase \"$1\" in\n"
            f"  +%H) echo {hour} ;;\n  +%M) echo {minute} ;;\n"
            "  *) /bin/date \"$@\" ;;\nesac\n")
        (bindir / "date").chmod(0o755)
        subprocess.run(["bash", str(runner)], env={"HOME": str(tmp_path)},
                       capture_output=True, timeout=30)
        assert log.exists() is ran, (hour, minute)


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
                      .replace("__PYTHON__", str(tmp_path / "py")))
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


def _llama_restart(tmp_path, launchctl_body):
    """Run llamacpp_restart_if_idle.sh with stub curl (unreachable ->
    no idle wait) and a stub launchctl; returns (proc, log text)."""
    import subprocess
    bindir = tmp_path / "stub"
    bindir.mkdir(exist_ok=True)
    (bindir / "curl").write_text("#!/bin/sh\nexit 7\n")
    (bindir / "launchctl").write_text("#!/bin/sh\n" + launchctl_body)
    for f in bindir.iterdir():
        f.chmod(0o755)
    body = (SCRIPTS / "llamacpp_restart_if_idle.sh").read_text(
        encoding="utf-8")
    runner = tmp_path / "runner.sh"
    runner.write_text(body.replace("/usr/bin/curl", str(bindir / "curl"))
                      .replace("/bin/launchctl", str(bindir / "launchctl")))
    log = tmp_path / ".hermes" / "logs" / "llamacpp-restart.log"
    log.unlink(missing_ok=True)
    proc = subprocess.run(["bash", str(runner)],
                          env={"HOME": str(tmp_path),
                               "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=30)
    return proc, log.read_text() if log.exists() else ""


def test_llama_restart_without_agent_is_a_clean_skip(tmp_path):
    """--no-llm / self-managed server: no agent loaded is normal — the
    daily cron must not fail every morning."""
    proc, log = _llama_restart(tmp_path, "exit 113\n")
    assert proc.returncode == 0
    assert "restart skipped" in log


def test_llama_restart_reports_failed_kickstart(tmp_path):
    """A failed `kickstart -k` is a failure, never logged as restarted."""
    proc, log = _llama_restart(
        tmp_path, '[ "$1" = kickstart ] && exit 5\nexit 0\n')
    assert proc.returncode == 5
    assert "FAILED rc=5" in log and "restarted" not in log
    proc, log = _llama_restart(tmp_path, "exit 0\n")
    assert proc.returncode == 0
    assert "restarted ai.hermes.llamacpp" in log
