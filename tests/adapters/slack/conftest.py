"""The Slack adapter family exercises thread-body delivery (chunk edits,
late-reply broadcast, block fallbacks), which stays live for legacy and
oversized posts. Keep bodies in the thread here; a test that wants the
card-face layout opts in with ``@pytest.mark.face_posts``."""
import pytest

import present_slack


@pytest.fixture(autouse=True)
def _thread_bodies(request, monkeypatch):
    if request.node.get_closest_marker("face_posts") is None:
        monkeypatch.setattr(present_slack, "BODY_ON_FACE", False)


def pytest_configure(config):
    config.addinivalue_line("markers", "face_posts: keep the Slack card-face body layout on")
