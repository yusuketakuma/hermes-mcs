"""Dormant-lane cloud estimates share the worker's local-followup evidence checks.

The individual reextract lane is closed to new sends; its estimate contract is
kept inside bounded legacy_cloud_routes blocks, with the real closed policy
asserting the dormant lane reports zero targets, reservations and POSTs."""
from contextlib import closing
from pathlib import Path
import sqlite3
import time

import pytest

import cloud_cli as cli
import cloud_policy
import extract_llm as worker
from extract_testkit import _message
from test_cloud_after_local import local_then_cloud, routed, _local_pass, _cloud_pass
from test_cloud_cli import world
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ['local_then_cloud', 'routed', 'world']


def _count(ledger, cfg):
    # Read an independent, temporary synthetic snapshot through the real CLI
    # bounded reader; no writable connection is passed to the estimate code.
    source = Path(ledger.db.execute('PRAGMA database_list').fetchone()[2])
    root = source.parent / 'synthetic-estimate'
    (root / 'data').mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / 'data' / 'ledger.db')) as target:
        ledger.db.backup(target)
    settings = cloud_policy.resolve_settings(cfg)[0]
    settings['routes'] = ['reextract']
    with cli._reader(root) as (reader, deadline):
        before = reader.total_changes
        out = cli._route_counts(reader, cfg, settings, deadline)['reextract']
        cli._check_reader(reader, deadline)
        assert reader.total_changes == before
        return out


def test_estimate_counts_real_local_then_excludes_completed_cloud(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    # Closed control: the dormant lane counts nothing and sends nothing.
    assert _count(ledger, cfg)['targets'] == 0
    assert _cloud_pass(ledger)['done'] == 0 and not cloud_calls
    assert not ledger.artifacts('cloud_usage_reserve')
    with legacy_cloud_routes():
        assert _count(ledger, cfg)['targets'] == 1
        assert _count(ledger, cfg)['requests_estimate'] >= 1
        assert not cloud_calls
        assert _cloud_pass(ledger)['done'] == 1
        before = len(cloud_calls)
        assert _count(ledger, cfg)['targets'] == 0
        assert len(cloud_calls) == before


def test_estimate_scans_completed_cloud_before_raw_history_without_prior(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 0 and not cloud_calls
    with legacy_cloud_routes():
        assert _cloud_pass(ledger)['done'] == 1
        ledger.save_messages([_message(2, body='完全合成の未解析履歴です。', posted_at='2026-09-18T00:00:00+09:00')])
        with ledger.db:
            ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
            ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
            cloud_policy.capture_history_tx(ledger.db, 'synthetic-estimate-tail', time.time())
        before = len(cloud_calls)
        assert _count(ledger, cfg)['targets'] == 1
        assert len(cloud_calls) == before


@pytest.mark.parametrize('boundary', ['no_scope', 'edited_scope', 'recent', 'lease', 'hold', 'excluded'])
def test_estimate_preserves_worker_admission_and_ownership_boundaries(local_then_cloud, boundary):
    ledger, cfg, _local_calls, cloud_calls, _answers = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    source = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    with ledger.db:
        if boundary == 'no_scope':
            ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_history_scope'")
        elif boundary == 'edited_scope':
            ledger.save_messages([_message(body='完全合成の承認後に訂正した記録です。')])
        elif boundary == 'recent':
            ledger.db.execute('UPDATE messages SET notified_at=?', (time.time(),))
        elif boundary == 'lease':
            ledger.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try) VALUES('extract_claim',1,1,?)",
                              (time.time() + 900,))
        elif boundary == 'hold':
            prior = worker._prior_extract(ledger.db, source)
            worker._record_hold_tx(ledger, source, prior, 'protected_loss')
        else:
            cfg['cloud_llm']['exclude_project_ids'] = [1]
    assert _count(ledger, cfg)['targets'] == 0
    with legacy_cloud_routes():
        # Under the open regime the boundary, not the route gate, excludes it.
        assert _count(ledger, cfg)['targets'] == 0
    assert not cloud_calls


def test_estimate_skips_attested_attention_without_promoting_quality(local_then_cloud):
    ledger, cfg, _local_calls, cloud_calls, answers = local_then_cloud
    body = '本人は合成薬Aを中止したかもしれません。'
    ledger.save_messages([_message(body=body)])
    with ledger.db:
        ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
        ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
        cloud_policy.capture_history_tx(ledger.db, 'synthetic-estimate-attention', time.time())
    answers['local'] = answers['cloud'] = {'symptoms': [], 'meds': [{'name': '合成薬A', 'dose': None,
        'status': 'current', 'subject': 'patient', 'action': 'stop', 'negated': False, 'evidence': body}]}
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    assert _cloud_pass(ledger)['done'] == 0 and not cloud_calls
    with legacy_cloud_routes():
        assert _cloud_pass(ledger)['done'] == 1
        selection, errors = cloud_policy.resolve_profile(cfg, route='reextract', purpose='backlog')
        assert not errors
        contract = worker.cloud_inference.cloud_provider_contract(selection=selection)
        source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
        assert worker.cloud_completion(ledger.db, source, contract) == 'attention'
        before = len(cloud_calls)
        assert _count(ledger, cfg)['targets'] == 0
        assert len(cloud_calls) == before


def test_estimate_without_history_permission_never_counts_legacy(world):
    root, path, cfg = world
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM artifacts WHERE kind='cloud_history_scope'")
    assert cli.observe(cfg, root=str(root))['routes']['reextract']['targets'] == 0
    with legacy_cloud_routes():
        # Missing history permission still counts nothing under the open regime.
        assert cli.observe(cfg, root=str(root))['routes']['reextract']['targets'] == 0
