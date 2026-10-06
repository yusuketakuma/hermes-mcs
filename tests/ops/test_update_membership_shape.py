"""Unreadable service membership must be rejected before any external operation."""
import pytest

import mcs_setup
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

    monkeypatch.setattr(mcs_setup, '_hermes_exe', unexpected)
    monkeypatch.setattr(mcs_update.glob, 'glob', unexpected)
    monkeypatch.setattr(mcs_update.os, 'open', unexpected)
    monkeypatch.setattr(mcs_update.subprocess, 'run', unexpected)
    assert mcs_update._reconcile_membership(desired) == ['service_snapshot_invalid']
