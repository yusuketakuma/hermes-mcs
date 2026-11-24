from datetime import datetime, timedelta, timezone

import common_testkit


def test_recent_posted_at_is_stable_within_test_id(monkeypatch):
    start = datetime(2026, 1, 1, 12, 0, 59, tzinfo=timezone.utc)
    calls = 0

    class Clock:
        @staticmethod
        def now(tz):
            nonlocal calls
            current = start + timedelta(seconds=calls)
            calls += 1
            return current.astimezone(tz)

    monkeypatch.setattr(common_testkit, "datetime", Clock)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic::first (call)")

    first = common_testkit.recent_posted_at()
    again = common_testkit.recent_posted_at()
    assert first == again
    assert calls == 1

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "synthetic::second (call)")
    second_test = common_testkit.recent_posted_at()
    assert second_test != first
    assert calls == 2


def test_recent_posted_at_does_not_cache_without_test_id(monkeypatch):
    start = datetime(2026, 1, 1, 12, 0, 59, tzinfo=timezone.utc)
    calls = 0

    class Clock:
        @staticmethod
        def now(tz):
            nonlocal calls
            current = start + timedelta(seconds=calls)
            calls += 1
            return current.astimezone(tz)

    monkeypatch.setattr(common_testkit, "datetime", Clock)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")

    first = common_testkit.recent_posted_at()
    second = common_testkit.recent_posted_at()
    assert first != second
