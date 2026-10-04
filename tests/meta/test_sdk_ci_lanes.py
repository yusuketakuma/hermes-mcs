"""CI must use the immutable-source and nonempty-collection SDK guards."""
import importlib.util
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_hermes_ci_runs_the_guarded_sdk_lane():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    job = workflow.split("  hermes-integration:\n", 1)[1]
    step = job.split("      - name: discord and slack plugin integration\n", 1)[1]
    # The guard verifies distribution versions and source against the CI pin,
    # installs process isolation before collection, and rejects missing tests.
    assert "working-directory: hermes-mcs" in step
    assert re.search(r"run:\s+sh scripts/check_pinned_sdks\.sh hermes\s*$", step)
    for name, suffix in (
        ("MCS_TEST_PYTHON", "hermes-agent/.venv/bin/python"),
        ("MCS_HERMES_SOURCE", "hermes-agent"),
        ("MCS_HERMES_REFERENCE", "hermes-agent"),
    ):
        assert f"{name}: ${{{{ github.workspace }}}}/{suffix}" in step


@pytest.mark.parametrize("lane", ["hermes", "standalone"])
def test_sdk_guard_rejects_zero_collected_tests(lane):
    spec = importlib.util.spec_from_file_location(
        "sdk_collection_guard", ROOT / "integration/sdk_safety.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    session = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda _name: lane), items=[])
    with pytest.raises(pytest.UsageError, match="required SDK tests not collected"):
        module.pytest_collection_finish(session)
