"""Bounded, synthetic observation of cloud accounting evidence."""
from decimal import Decimal
import json
import time
import uuid

import pytest

import cloud_cli
import cloud_observation as observation
import cloud_policy as policy
from ledger import Ledger


@pytest.fixture
def world(tmp_path):
    (tmp_path / 'data').mkdir()
    ledger = Ledger(str(tmp_path / 'data/ledger.db'))
    ledger.ensure_patient(1)
    yield ledger, tmp_path
    ledger.close()


def _reserve(ledger, now, *, rid=None, extra=None):
    rid = rid or uuid.uuid4().hex
    meta = {'reservation_id': rid, 'request_fp': 'a' * 64, 'route': 'reextract',
            'project_id': 1, 'unit_key': 'synthetic', 'message_id': None,
            'model': policy.MODEL, 'price_version': policy.PRICE_VERSION,
            'max_tokens': 100, 'input_bound': 256,
            'max_cost_usd': str(policy._reservation_cost(256, 100))}
    meta.update(extra or {})
    ledger.db.execute('INSERT INTO artifacts(kind,project_id,message_id,model,content,meta,created_at) '
                      'VALUES(?,?,?,?,?,?,?)',
                      (policy.KINDS[0], 1, None, policy.MODEL, '{}', json.dumps(meta), now))
    return meta


def _result(ledger, reserve, now, *, outcome='settled'):
    meta = {key: reserve.get(key) for key in policy._BINDING_KEYS}
    if outcome == 'settled':
        usage = {'input_tokens': 10, 'output_tokens': 2}
        cost = policy.usage_cost(usage)
        http = 'http_200'
    elif outcome == 'not_sent':
        usage, cost, http = None, Decimal(0), 'not_sent'
    else:
        usage, cost, http = None, Decimal(reserve['max_cost_usd']), 'transport'
    meta.update(usage=usage, cost_usd=str(cost), http_class=http)
    ledger.db.execute('INSERT INTO artifacts(kind,project_id,message_id,model,content,meta,created_at) '
                      'VALUES(?,?,?,?,?,?,?)',
                      (policy.KINDS[1], 1, None, policy.MODEL, '{}', json.dumps(meta), now))


def _observe(ledger, now):
    ledger.db.commit()
    ledger.db.execute('BEGIN')
    try:
        return observation.observe(ledger.db, now=now)
    finally:
        ledger.db.rollback()


def test_distinguishes_missing_ambiguous_and_settled(world):
    ledger, _ = world
    now = 1793240000.0
    _reserve(ledger, now - 1000)
    uncertain = _reserve(ledger, now - 700)
    _result(ledger, uncertain, now - 600, outcome='uncertain')
    for outcome in ('settled', 'not_sent'):
        reserve = _reserve(ledger, now - 200)
        _result(ledger, reserve, now - 100, outcome=outcome)
    result = _observe(ledger, now)
    assert result['state'] == 'complete'
    assert (result['missing_results'], result['unsettled_results'], result['settled_requests']) == (1, 1, 2)
    assert result['unresolved_requests'] == result['unresolved_over_10m'] == 2
    assert result['oldest_unresolved_age_s'] == 1000
    assert 'synthetic' not in json.dumps(result)


@pytest.mark.parametrize('damage', ['future', 'duplicate', 'orphan', 'invalid_json', 'duplicate_json', 'mismatch', 'result_before_reserve'])
def test_corruption_or_uncertainty_never_reports_zero(world, damage):
    ledger, _ = world
    now = 1793240000.0
    reserve = _reserve(ledger, now - 100)
    if damage == 'future':
        ledger.db.execute('UPDATE artifacts SET created_at=?', (now + 1,))
    elif damage == 'duplicate':
        _reserve(ledger, now - 100, rid=reserve['reservation_id'])
    elif damage == 'orphan':
        other = {**reserve, 'reservation_id': uuid.uuid4().hex}
        _result(ledger, other, now - 20)
    elif damage == 'invalid_json':
        ledger.db.execute("UPDATE artifacts SET meta='{broken'")
    elif damage == 'duplicate_json':
        ledger.db.execute("UPDATE artifacts SET meta=substr(meta,1,length(meta)-1)||',\"model\":\"other\"}'")
    elif damage == 'result_before_reserve':
        _result(ledger, reserve, now - 200)
    else:
        _result(ledger, reserve, now - 10)
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.request_fp',?) WHERE kind=?",
                          ('b' * 64, policy.KINDS[1]))
    result = _observe(ledger, now)
    assert result['state'] == 'partial'
    assert result['unresolved_requests'] is None and result['settled_requests'] is None


def test_large_unused_metadata_is_not_transferred_to_reader(world, monkeypatch):
    ledger, root = world
    monkeypatch.setattr(cloud_cli, 'READ_BYTES', 1024)
    now = time.time()
    for _ in range(900):
        reserve = _reserve(ledger, now - 700, extra={'irrelevant': 'x' * 6000})
        _result(ledger, reserve, now - 100, outcome='not_sent')
    ledger.db.commit()
    with cloud_cli._reader(root) as (db, _):
        result = observation.observe(db, now=now)
    assert result['state'] == 'complete' and result['settled_requests'] == 900
    assert result['unresolved_requests'] == 0


def test_observation_limit_is_unknown_not_zero(world, monkeypatch):
    ledger, _ = world
    now = 1793240000.0
    _reserve(ledger, now - 10)
    _reserve(ledger, now - 20)
    monkeypatch.setattr(observation, 'MAX_ROWS', 1)
    result = _observe(ledger, now)
    assert result['state'] == 'unknown' and result['unresolved_requests'] is None


def test_cli_outstanding_uses_independent_snapshot(world, monkeypatch, capsys):
    ledger, root = world
    _reserve(ledger, time.time() - 700)
    ledger.db.commit()
    monkeypatch.setattr(cloud_cli.mcs_util, 'HOME', str(root))
    monkeypatch.setattr(cloud_cli.mcs_util, 'load_config', lambda: {})
    import argparse
    parser = argparse.ArgumentParser()
    cloud_cli.add_parsers(parser.add_subparsers(dest='command', required=True))
    args = parser.parse_args(['cloud-llm', 'outstanding'])
    assert args.fn(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['unresolved_requests'] == 1 and result['unresolved_over_10m'] == 1


def test_legacy_reservation_without_input_bound_is_validated(world):
    ledger, _ = world
    now = 1793240000.0
    reserve = _reserve(ledger, now - 100)
    reserve.pop('input_bound')
    reserve['max_cost_usd'] = str(policy._reservation_cost(policy.INPUT_RESERVATION_BOUND, 100, legacy=True))
    ledger.db.execute('UPDATE artifacts SET meta=? WHERE kind=?', (json.dumps(reserve), policy.KINDS[0]))
    _result(ledger, reserve, now - 10, outcome='not_sent')
    report = _observe(ledger, now)
    assert report['state'] == 'complete' and report['settled_requests'] == 1


def test_openai_reservation_uses_its_own_usage_rules(world):
    ledger, _ = world
    now = 1793240000.0
    reserve = _reserve(ledger, now - 100)
    reserve.update(model=policy.openai_client.MODEL, price_version=policy.OPENAI_PRICE_VERSION,
                   max_cost_usd=str(policy._reservation_cost(256, 100, model=policy.openai_client.MODEL)))
    ledger.db.execute('UPDATE artifacts SET model=?,meta=? WHERE kind=?',
                      (reserve['model'], json.dumps(reserve), policy.KINDS[0]))
    usage = {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12,
             'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
             'output_tokens_details': {'reasoning_tokens': 0}}
    meta = {key: reserve.get(key) for key in policy._BINDING_KEYS}
    meta.update(usage=usage, cost_usd=str(policy.usage_cost(usage, model=reserve['model'])), http_class='http_200')
    ledger.db.execute('INSERT INTO artifacts(kind,project_id,message_id,model,content,meta,created_at) '
                      'VALUES(?,?,?,?,?,?,?)',
                      (policy.KINDS[1], 1, None, reserve['model'], '{}', json.dumps(meta), now - 10))
    report = _observe(ledger, now)
    assert report['state'] == 'complete' and report['settled_requests'] == 1


@pytest.mark.parametrize('damage', ['boolean_project', 'boolean_tokens', 'boolean_bound',
                                     'object_unit', 'object_fingerprint', 'object_id', 'long_id', 'nul_long_id', 'encoded_usage'])
def test_sql_projection_preserves_json_types_for_accounting_validation(world, damage):
    ledger, _ = world
    now = 1793240000.0
    extra = {}
    if damage == 'boolean_project':
        extra['project_id'] = True
    elif damage == 'boolean_tokens':
        extra.update(max_tokens=True, max_cost_usd=str(policy._reservation_cost(256, 1)))
    elif damage == 'boolean_bound':
        extra.update(input_bound=True, max_cost_usd=str(policy._reservation_cost(1, 100)))
    elif damage == 'object_unit':
        extra['unit_key'] = {'slot': 'synthetic'}
    elif damage == 'object_fingerprint':
        extra['request_fp'] = {'x': 'a' * 56}
    elif damage == 'object_id':
        extra['reservation_id'] = {'x': 'a' * 24}
    elif damage == 'long_id':
        extra['reservation_id'] = 'a' * 30000
    elif damage == 'nul_long_id':
        extra['reservation_id'] = 'a' * 32 + '\0' + 'x' * 1000
    reserve = _reserve(ledger, now - 100, extra=extra)
    _result(ledger, reserve, now - 10, outcome='settled' if damage == 'encoded_usage' else 'not_sent')
    if damage == 'encoded_usage':
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.usage',?) WHERE kind=?",
                          (json.dumps({'input_tokens': 10, 'output_tokens': 2}), policy.KINDS[1]))
    report = _observe(ledger, now)
    assert report['state'] == 'partial'
    assert report['settled_requests'] is None and report['unresolved_requests'] is None


def test_internal_proof_budget_is_bounded_and_does_not_write(world, monkeypatch):
    ledger, _ = world
    now = 1793240000.0
    for _ in range(3):
        _reserve(ledger, now - 100, extra={'unused': 'x' * 1000})
    before = ledger.db.total_changes
    monkeypatch.setattr(observation, 'MAX_PROOF_BYTES', 2048)
    report = _observe(ledger, now)
    assert report['state'] == 'unknown'
    assert report['unresolved_requests'] is None
    assert ledger.db.total_changes == before
    monkeypatch.setattr(observation, 'MAX_PROOF_BYTES', 16 * 1024 * 1024)
    assert _observe(ledger, now)['missing_results'] == 3
