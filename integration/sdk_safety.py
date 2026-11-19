"""Load the existing repository process guards before SDK test collection."""
from pathlib import Path
import runpy
import socket

import pytest


# integration/conftest.py checks the same marker, so the guards install once.
if socket.socket.connect.__name__ != "_blocked":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/conftest.py"))


def pytest_addoption(parser):
    parser.addoption("--sdk-lane", choices=("standalone", "hermes"))
    parser.addoption("--hermes-project")
    parser.addoption("--hermes-reference-root")


def pytest_collection_finish(session):
    lane = session.config.getoption("--sdk-lane")
    if lane is None:
        raise pytest.UsageError("select --sdk-lane for the pinned SDK guard")
    expected = ({"test_standalone_discord.py",
                 "test_standalone_slack_sdk.py"} if lane == "standalone" else
                {"test_hermes_discord.py", "test_hermes_slack.py",
                 "test_hermes_config.py", "test_discord_sdk_real.py",
                 "test_discord_file_group_views.py"})
    expected.add("test_pinned_sdk_versions.py")
    missing = expected - {Path(item.path).name for item in session.items}
    if missing:
        raise pytest.UsageError("required SDK tests not collected: " + ", ".join(sorted(missing)))
    if lane == 'hermes':
        nodes = {item.nodeid for item in session.items if Path(item.path).name == 'test_discord_file_group_views.py'}
        if len(nodes) < 13:
            raise pytest.UsageError('required Discord file group cases not collected: expected at least 13')
