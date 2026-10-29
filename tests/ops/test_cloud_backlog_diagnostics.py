"""Synthetic, independently bounded observation of the Jev backlog share."""
import json
from contextlib import closing

import pytest

import cloud_cli
from ledger import Ledger


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    with closing(Ledger(str(tmp_path / 'data' / 'ledger.db'))) as ledger:
        monkeypatch.setattr(cloud_cli.jev, 'api_key', lambda *args: 'synthetic')
        yield ledger, tmp_path


def _usage(ledger, value):
    ledger.artifact_add('semantic_usage', '{}', meta={'jev_requests': value})


def _cfg(limit=10000):
    return {'semantic': {'mode': 'assist', 'project_ids': None, 'daily_request_budget': limit}}


@pytest.mark.parametrize('used,reason', [(8999, None), (9000, 'backlog_budget_reserved'),
                                        (10000, 'daily_budget_exhausted')])
def test_reports_backlog_budget_and_realtime_reserve(world, used, reason):
    ledger, root = world
    _usage(ledger, used)
    out = cloud_cli.diagnostics(_cfg(), root=root, probe=True)['jev']['backlog_budget']
    assert out['state'] == 'recorded'
    assert (out['used_today'], out['backlog_limit'], out['realtime_reserve']) == (used, 9000, 1000)
    assert out['daily_remaining'] == max(0, 10000 - used)
    assert out['reason'] == reason
    assert 'synthetic' not in json.dumps(out)


def test_unchecked_probe_never_opens_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_cli.jev, 'api_key', lambda *args: 'synthetic')
    monkeypatch.setattr(cloud_cli, '_reader', lambda *args: pytest.fail('unexpected DB read'))
    assert cloud_cli.diagnostics(_cfg(), root=tmp_path)['jev']['backlog_budget'] is None


def test_invalid_usage_is_unknown(world):
    ledger, root = world
    _usage(ledger, 'synthetic-invalid')
    out = cloud_cli.diagnostics(_cfg(), root=root, probe=True)['jev']['backlog_budget']
    assert out['state'] == 'unknown' and out['used_today'] is None and out['reason'] is None


def test_backlog_reserve_uses_the_worker_rounding(world):
    ledger, root = world
    _usage(ledger, 90)
    out = cloud_cli.diagnostics(_cfg(101), root=root, probe=True)['jev']['backlog_budget']
    assert (out['backlog_limit'], out['realtime_reserve'], out['daily_remaining']) == (90, 11, 11)
    assert out['reason'] == 'backlog_budget_reserved'


def test_missing_key_does_not_claim_a_backlog_stop(world, monkeypatch):
    ledger, root = world
    _usage(ledger, 9000)
    monkeypatch.setattr(cloud_cli.jev, 'api_key', lambda *args: None)
    out = cloud_cli.diagnostics(_cfg(), root=root, probe=True)['jev']['backlog_budget']
    assert out['state'] == 'recorded' and out['reason'] is None


def test_jev_observation_is_independent_of_cloud_accounting_failure(world, monkeypatch):
    ledger, root = world
    _usage(ledger, 9000)
    monkeypatch.setattr(cloud_cli.policy, 'usage_report', lambda *a: (_ for _ in ()).throw(ValueError()))
    out = cloud_cli.diagnostics(_cfg(), root=root, probe=True)
    assert out['anthropic']['status'] == 'unknown'
    assert out['jev']['backlog_budget']['state'] == 'recorded'
    assert out['jev']['backlog_budget']['reason'] == 'backlog_budget_reserved'
