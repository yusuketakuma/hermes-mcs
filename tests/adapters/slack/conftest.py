"""Use the shipping Slack layout; only explicit legacy-body tests opt out."""
import pytest

import present_slack


@pytest.fixture(autouse=True)
def _thread_bodies(request, monkeypatch):
    if request.node.get_closest_marker("thread_bodies") is not None:
        monkeypatch.setattr(present_slack, "BODY_ON_FACE", False)


def pytest_configure(config):
    config.addinivalue_line("markers", "thread_bodies: exercise legacy Slack thread-body delivery")
    config.addinivalue_line("markers", "face_posts: exercise the default Slack card-face body layout")
