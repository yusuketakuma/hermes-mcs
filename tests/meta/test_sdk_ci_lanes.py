"""CI must use the immutable-source and nonempty-collection SDK guards."""
import builtins
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
    assert re.search(r"run:\s+sh scripts/development/check_pinned_sdks\.sh hermes\s*$", step)
    install = job.split("      - name: install hermes (pinned ref) + integration test deps\n", 1)[1]
    assert "cd hermes-agent\n" in install
    assert "uv pip install --python .venv/bin/python -e '.[messaging]'" in install
    assert "uv pip install --python .venv/bin/python --group dev" in install
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


def test_optional_sdk_module_can_collect_without_python311_tomllib(monkeypatch):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("synthetic Python 3.10 lacks tomllib")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    spec = importlib.util.spec_from_file_location(
        "sdk_optional_lane", ROOT / "integration/test_pinned_sdk_versions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def _sdk_lane_module():
    spec = importlib.util.spec_from_file_location(
        "sdk_pinned_versions", ROOT / "integration/test_pinned_sdk_versions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_core_pins_are_found_by_normalized_name():
    module = _sdk_lane_module()
    dependencies = [
        "requests==2.32.5",
        'pyjwt[crypto]==2.10.1; python_version >= "3.14"',
        "Cryptography==46.0.1",
    ]
    pins = module._core_pins(dependencies)
    assert pins == ["pyjwt[crypto]==2.10.1", "Cryptography==46.0.1"]
    # The exact pinned version reaches the distribution comparison unchanged.
    assert [module._pin(pin) for pin in pins] == [("pyjwt", "2.10.1"), ("Cryptography", "46.0.1")]
    assert module._pin("PyJWT==9.9.9") == ("PyJWT", "9.9.9")


@pytest.mark.parametrize("dependencies", [
    ["PyJWT==2.10.1"],
    ["cryptography==46.0.1", "pyjwt-stub==1.0"],
    [],
])
def test_core_pins_require_both_packages(dependencies):
    module = _sdk_lane_module()
    with pytest.raises(AssertionError):
        module._core_pins(dependencies)
