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
