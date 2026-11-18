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
    # the job ends where the next top-level job starts (hermes-integration is not the last one)
    job = re.split(r"\n  [a-z][a-z0-9-]*:\n", workflow.split("  hermes-integration:\n", 1)[1], maxsplit=1)[0] + "\n"
    step = job.split("      - name: discord and slack plugin integration\n", 1)[1]
    step = step.split("\n      - ", 1)[0]
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


def _sdk_collection_guard():
    spec = importlib.util.spec_from_file_location(
        "sdk_collection_guard", ROOT / "integration/sdk_safety.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("lane", ["hermes", "standalone"])
def test_sdk_guard_rejects_zero_collected_tests(lane):
    module = _sdk_collection_guard()
    session = SimpleNamespace(
        config=SimpleNamespace(getoption=lambda _name: lane), items=[])
    with pytest.raises(pytest.UsageError, match="required SDK tests not collected"):
        module.pytest_collection_finish(session)


def test_hermes_guard_requires_all_file_group_cases():
    module = _sdk_collection_guard()
    names = ["test_pinned_sdk_versions.py", "test_hermes_discord.py", "test_hermes_slack.py",
             "test_hermes_config.py", "test_discord_sdk_real.py"]
    items = [SimpleNamespace(path=Path(name)) for name in names]
    items += [SimpleNamespace(path=Path("test_discord_file_group_views.py"), nodeid=f"case-{i}") for i in range(12)]
    session = SimpleNamespace(config=SimpleNamespace(getoption=lambda _name: "hermes"), items=items)
    with pytest.raises(pytest.UsageError, match="expected at least 13"):
        module.pytest_collection_finish(session)
    items.append(SimpleNamespace(path=Path("test_discord_file_group_views.py"), nodeid="case-0"))
    with pytest.raises(pytest.UsageError, match="expected at least 13"):
        module.pytest_collection_finish(session)
    items[-1].nodeid = "case-12"
    module.pytest_collection_finish(session)


def _discord_sdk_guard():
    spec = importlib.util.spec_from_file_location(
        "discord_sdk_guard", ROOT / "tests/adapters/discord/discord_sdk_testkit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discord_sdk_guard_skips_only_when_distribution_is_absent(monkeypatch):
    module = _discord_sdk_guard()

    def missing(_name):
        raise module.metadata.PackageNotFoundError('discord.py')

    monkeypatch.setattr(module.metadata, 'distribution', missing)
    with pytest.raises(pytest.skip.Exception, match='discord.py is not installed'):
        module.require_real_discord()


def test_discord_sdk_guard_fails_for_broken_installed_sdk(monkeypatch, tmp_path):
    module = _discord_sdk_guard()
    dist = SimpleNamespace(locate_file=lambda _name: tmp_path / 'discord' / '__init__.py')
    monkeypatch.setattr(module.metadata, 'distribution', lambda _name: dist)

    def broken(_name):
        raise ModuleNotFoundError('synthetic missing dependency')

    monkeypatch.setattr(module, 'import_module', broken)
    with pytest.raises(ModuleNotFoundError, match='synthetic missing dependency'):
        module.require_real_discord()
    monkeypatch.setattr(module, 'import_module', lambda _name: SimpleNamespace(__file__=str(tmp_path / 'fake.py')))
    with pytest.raises(ImportError, match='does not match'):
        module.require_real_discord()
    sdk = SimpleNamespace(__file__=str(tmp_path / 'discord' / '__init__.py'))
    monkeypatch.setattr(module, 'import_module', lambda _name: sdk)
    assert module.require_real_discord() is sdk


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
