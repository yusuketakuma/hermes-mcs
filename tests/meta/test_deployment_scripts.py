"""deployment/scripts — static contract checks.

The cron wrappers run under `hermes cron`, whose default
script_timeout_seconds is 3600. Any --stop-after/loop window the
script hands to a drain must fit UNDER that bound with margin, or the
job is killed every single night mid-drain (observed: mcs-llm-catchup
timing out at 3600s while WINDOW_S asked for 5h).
"""
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "deployment" / "scripts"

# hermes cron default script_timeout_seconds — keep a real margin for
# interpreter startup, lock waits, and the gap-fill spawn.
HERMES_CRON_SCRIPT_TIMEOUT_S = 3600
MARGIN_S = 300


def test_llm_catchup_window_fits_cron_timeout():
    body = (SCRIPTS / "mcs_llm_catchup.sh").read_text(encoding="utf-8")
    m = re.search(r"^WINDOW_S=(\d+)$", body, re.M)
    assert m, "WINDOW_S assignment missing"
    window = int(m.group(1))
    assert 0 < window <= HERMES_CRON_SCRIPT_TIMEOUT_S - MARGIN_S


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


def test_llm_catchup_gap_fill_matches_shard_zero_only():
    """The RT drainer (shard 1/2) also runs `extract_llm.py --all` — the
    gap-fill probe must look for the shard-0 drainer specifically."""
    body = (SCRIPTS / "mcs_llm_catchup.sh").read_text(encoding="utf-8")
    m = re.search(r'pgrep -f "([^"]+)"', body)
    assert m and "--shard 0/2" in m.group(1)


def test_check_night_filter_is_a_window_not_an_exact_minute(tmp_path):
    """A night tick that starts a few minutes late still runs; the
    minutes between slots stay skipped."""
    import subprocess
    body = (SCRIPTS / "mcs_check.sh").read_text(encoding="utf-8")
    runner = tmp_path / "runner.sh"
    body = body.replace("__DATA__", str(tmp_path)) \
        .replace("__REPO__", str(tmp_path)) \
        .replace("__PYTHON__", str(tmp_path / "py"))
    (tmp_path / "py").write_text("#!/bin/sh\necho ran >> \"$0.log\"\n")
    (tmp_path / "py").chmod(0o755)
    runner.write_text(body)
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    for hour, minute, ran in (("23", "02", True), ("23", "20", True),
                              ("23", "07", False), ("12", "07", True)):
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
