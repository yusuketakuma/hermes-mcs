"""Synthetic lock windows for updater wait without touching live services."""
from types import SimpleNamespace

import pytest

import mcs_update


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    def sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(mcs_update, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now


def test_catches_short_free_window_before_original_deadline(monkeypatch, clock):
    seen = []
    def acquire(_path):
        seen.append(clock[0])
        return 123 if 0.3 <= clock[0] <= 0.5 else None
    monkeypatch.setattr(mcs_update, 'acquire_run_lock', acquire)
    assert mcs_update._acquire_run_lock_wait(tries=2) == 123
    assert 0.3 <= seen[-1] <= 0.5


def test_busy_wait_stops_at_original_deadline(monkeypatch, clock):
    monkeypatch.setattr(mcs_update, 'acquire_run_lock', lambda _: None)
    assert mcs_update._acquire_run_lock_wait(tries=2) is None
    assert clock[0] == 30


def test_zero_and_single_try_do_not_wait(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(mcs_update, 'acquire_run_lock', lambda _: calls.append(clock[0]))
    assert mcs_update._acquire_run_lock_wait(tries=0) is None and calls == []
    assert mcs_update._acquire_run_lock_wait(tries=1) is None and calls == [0.0]
    assert clock[0] == 0.0


def test_immediate_success_returns_owned_descriptor(monkeypatch, clock):
    monkeypatch.setattr(mcs_update, 'acquire_run_lock', lambda _: 123)
    assert mcs_update._acquire_run_lock_wait(tries=2) == 123 and clock[0] == 0.0


def test_sleep_overshoot_never_acquires_after_deadline(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(mcs_update, 'acquire_run_lock', lambda _: calls.append(clock[0]))
    def delayed(_):
        clock[0] += 31
    monkeypatch.setattr(mcs_update.time, 'sleep', delayed)
    assert mcs_update._acquire_run_lock_wait(tries=2) is None
    assert calls == [0.0]
