"""CI workflow guards: bounded jobs, complete failure output, a weekly run
and a macOS lane. (The OpenWiki workflow is maintained separately.)"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = [ROOT / ".github/workflows" / name
             for name in ("ci.yml", "release-notes.yml")]


def _jobs(text):
    """Job name -> job block text (two-space keys under ``jobs:``)."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_has_a_timeout(workflow):
    jobs = _jobs(workflow.read_text())
    assert jobs
    for name, block in jobs.items():
        assert re.search(r"^    timeout-minutes: \d+$", block, re.M), name


def test_ci_reports_every_failure_and_runs_weekly():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert not re.search(r"run_tests\.sh[^\n]*\s-x\b", text)
    assert "--maxfail" not in text
    assert re.search(r"^  schedule:\n    - cron: \"[^\"]+\"$", text, re.M)


def test_ci_has_a_macos_lane_with_the_pinned_actions():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    jobs = _jobs(text)
    mac = [block for block in jobs.values() if "runs-on: macos-latest" in block]
    assert len(mac) == 1
    linux = jobs["lint-test"]
    for action in ("actions/checkout@", "actions/setup-python@"):
        pin = re.search(re.escape(action) + r"[0-9a-f]{40}", linux)[0]
        assert pin in mac[0]
    assert 'python-version: "3.14"' in mac[0]
    assert re.search(r"scripts/run_tests\.sh tests/meta tests/ops", mac[0])
