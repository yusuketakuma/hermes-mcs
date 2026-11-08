"""Canonical publication does not hide required raw-cloud work from bounded estimates."""
import time

import pytest

import cloud_policy
import mcs_util
import semantic_candidate
import semantic_policy
from candidate_testkit import approved_config
import extract_llm as worker
from extract_testkit import _message
from test_cloud_after_local import (
    local_then_cloud, routed, _local_pass, _cloud_pass, _selected_contract, _financial_rows,
)
from test_cloud_followup_estimate import _count

__all__ = ['local_then_cloud', 'routed']


def _canonical_source(local_then_cloud, monkeypatch, tmp_path, *, local_raw, attention=False):
    ledger, cfg, local_calls, cloud_calls, answers = local_then_cloud
    if attention:
        body = '本人は合成薬Aを中止したかもしれません。'
        ledger.save_messages([_message(body=body)])
        with ledger.db:
            ledger.db.execute('UPDATE messages SET first_seen=?', (time.time() - 1200,))
            ledger.db.execute('UPDATE message_revisions SET observed_at=?', (time.time() - 1200,))
            cloud_policy.capture_history_tx(ledger.db, 'synthetic-canonical-estimate', time.time())
        answers['local'] = answers['cloud'] = {'symptoms': [], 'meds': [{'name': '合成薬A', 'dose': None,
            'status': 'current', 'subject': 'patient', 'action': 'stop', 'negated': False, 'evidence': body}]}
    if local_raw:
        assert _local_pass(ledger)['done'] == 1 and local_calls
    else:
        assert not ledger.artifacts(worker.KIND) and not local_calls
    # Shared canonical-seal fixture is supplied by the canonical worker fix;
    # no alternative projection/proof shape is invented in this test module.
    from test_cloud_after_local import _seal_canonical_v4
    projection = _seal_canonical_v4(ledger)
    saved = tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (projection,)).fetchone())
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path))
    monkeypatch.setattr(semantic_candidate, '_LOADED_SOURCES', None)
    cfg['semantic'] = {'mode': 'shadow', 'fact_source': 'canonical', 'project_ids': [1]}
    cfg['cloud_llm']['mode'] = 'on'  # The activation fingerprint includes this setting.
    approved = approved_config(cfg)
    cfg.clear()
    cfg.update(approved)  # Preserve the dict captured by both real worker config readers.
    assert not semantic_policy.runtime_config(cfg)[1]
    assert worker.legacy_admissions(ledger, cfg) == set()
    assert worker.cloud_backlog_admissions(ledger, cfg) is None
    return ledger, cfg, cloud_calls, projection, saved


@pytest.mark.parametrize('local_raw', [True, False], ids=['local-complete', 'rawless'])
def test_canonical_source_counts_required_cloud_then_stops_after_proven_completion(
        local_then_cloud, monkeypatch, tmp_path, local_raw):
    ledger, cfg, calls, projection, saved = _canonical_source(local_then_cloud, monkeypatch, tmp_path, local_raw=local_raw)
    evidence = _financial_rows(ledger)
    assert _count(ledger, cfg)['targets'] == 1
    assert not calls and _financial_rows(ledger) == evidence
    assert _cloud_pass(ledger)['done'] == 1
    source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    assert worker.cloud_completion(ledger.db, source, _selected_contract(cfg)) == 'complete'
    count, evidence = len(calls), _financial_rows(ledger)
    assert count > 0 and _count(ledger, cfg)['targets'] == 0
    assert len(calls) == count and _financial_rows(ledger) == evidence
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (projection,)).fetchone()) == saved


@pytest.mark.parametrize('local_raw', [True, False], ids=['local-complete', 'rawless'])
def test_canonical_attention_does_not_estimate_a_fresh_dispatch_or_publish_complete(
        local_then_cloud, monkeypatch, tmp_path, local_raw):
    ledger, cfg, calls, projection, saved = _canonical_source(
        local_then_cloud, monkeypatch, tmp_path, local_raw=local_raw, attention=True)
    assert _count(ledger, cfg)['targets'] == 1 and not calls
    assert _cloud_pass(ledger)['done'] == 1
    source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    contract = _selected_contract(cfg)
    assert worker.cloud_completion(ledger.db, source, contract) == 'attention'
    assert worker.progress_for_message(ledger.db, {**dict(source), '_provider_contract': contract},
                                      model=contract['model'])['state'] != 'complete'
    count, evidence = len(calls), _financial_rows(ledger)
    assert _count(ledger, cfg)['targets'] == 0
    assert len(calls) == count and _financial_rows(ledger) == evidence
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (projection,)).fetchone()) == saved


@pytest.mark.parametrize('local_raw', [True, False], ids=['local-complete', 'rawless'])
@pytest.mark.parametrize('boundary', ['unreceipted', 'excluded'])
def test_canonical_presence_never_grants_history_permission_or_project_scope(
        local_then_cloud, monkeypatch, tmp_path, local_raw, boundary):
    ledger, cfg, calls, projection, saved = _canonical_source(local_then_cloud, monkeypatch, tmp_path, local_raw=local_raw)
    if boundary == 'unreceipted':
        with ledger.db:
            ledger.db.execute("DELETE FROM artifacts WHERE kind='cloud_history_scope'")
    else:
        cfg['cloud_llm']['exclude_project_ids'] = [1]
        approved = approved_config(cfg)  # Keep this a valid canonical config after its scope change.
        cfg.clear()
        cfg.update(approved)
        assert not semantic_policy.runtime_config(cfg)[1]
    evidence = _financial_rows(ledger)
    assert _count(ledger, cfg)['targets'] == 0
    assert not calls and _financial_rows(ledger) == evidence
    assert tuple(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (projection,)).fetchone()) == saved
