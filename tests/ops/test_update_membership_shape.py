"""Unreadable service membership must be rejected before any external operation."""
import runtime_inspection
import pytest

import mcs_update


@pytest.mark.parametrize('desired', [
    ['synthetic'], 'synthetic', True, 1,
    {'cron': None}, {'agents': None}, {'cron': {}}, {'agents': {}},
    {'cron': [1]}, {'agents': ['synthetic']},
    {'cron': [{'script': []}]}, {'agents': [{'label': {}}]},
])
def test_invalid_membership_stops_before_external_operations(monkeypatch, desired):
    monkeypatch.setattr(mcs_update, 'load_config', lambda: {'runtime_mode': 'hermes'})

    def unexpected(*args, **kwargs):
        pytest.fail('invalid membership reached service or filesystem operation')

    monkeypatch.setattr(mcs_update, 'hermes_exe', unexpected)
    monkeypatch.setattr(mcs_update.glob, 'glob', unexpected)
    monkeypatch.setattr(mcs_update.os, 'open', unexpected)
    monkeypatch.setattr(mcs_update.subprocess, 'run', unexpected)
    assert mcs_update._reconcile_membership(desired) == ['service_snapshot_invalid']


def test_invalid_runtime_mode_is_reported_before_any_removal(monkeypatch):
    monkeypatch.setattr(mcs_update, 'load_config', lambda: {'runtime_mode': 'synthetic-bogus'})

    def unexpected(*args, **kwargs):
        pytest.fail('invalid runtime_mode reached cron or agent removal')

    monkeypatch.setattr(runtime_inspection, "executable_available", lambda hermes: True)
    monkeypatch.setattr(mcs_update.glob, 'glob', unexpected)
    monkeypatch.setattr(mcs_update.subprocess, 'run', unexpected)
    assert mcs_update._reconcile_membership({}) == ['runtime_mode_unverifiable']
