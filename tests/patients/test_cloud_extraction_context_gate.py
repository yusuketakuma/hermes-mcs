"""A completed local pass cannot satisfy a required cloud context dependency."""
import time

import pytest

import patient_context_refresh as refresh
from test_cloud_after_local import local_then_cloud, routed, _local_pass, _cloud_pass
from test_patient_context_refresh import runtime, seed, finish, CFG

__all__ = ['local_then_cloud', 'routed', 'runtime']


def test_existing_local_context_is_not_shown_as_cloud_complete(runtime):
    ledger, _, _ = runtime
    seed(ledger)
    finish(runtime)
    assert refresh.context_view_input(ledger.db, 1, cfg=CFG)
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'project_ids': [1], 'routes': ['reextract']}}
    assert refresh.context_view_input(ledger.db, 1, cfg=cfg) is None
    coverage = refresh.inference_coverage(ledger.db, 1, cfg)
    assert coverage['status'] == 'cloud_extraction_pending'
    assert not coverage['complete'] and not coverage['publication_eligible']


def test_patient_readiness_waits_for_cloud_after_real_local_save(local_then_cloud):
    ledger, cfg, _, calls, _ = local_then_cloud
    cfg['semantic'] = {'mode': 'off'}
    with ledger.db:
        ledger.db.execute("INSERT INTO patients(project_id,patient_name,fetch_state,history_floor) "
                          "VALUES(1,'synthetic-patient','complete',-1)")
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    deadline = time.monotonic() + 600
    assert not refresh._inference_ready(ledger, 1, cfg, deadline=deadline)
    assert not calls
    assert _cloud_pass(ledger)['done'] == 1
    assert refresh._inference_ready(ledger, 1, cfg, deadline=deadline)


def test_local_then_cloud_context_gate_uses_real_saved_proofs(local_then_cloud):
    ledger, cfg, _, calls, _ = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    assert refresh._raw_extraction_current(ledger.db, source, cfg)[0]
    cfg['cloud_llm']['mode'] = 'on'
    before = ledger.db.total_changes
    assert not refresh._raw_extraction_current(ledger.db, source, cfg)[0]
    assert not refresh._cloud_extraction_ready(ledger.db, 1, cfg)
    assert ledger.db.total_changes == before and not calls
    assert _cloud_pass(ledger)['done'] == 1
    assert refresh._raw_extraction_current(ledger.db, source, cfg)[0]
    assert refresh._cloud_extraction_ready(ledger.db, 1, cfg)
    assert len(calls) == 1
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.prompt','old') WHERE kind='extract_llm'")
    assert not refresh._cloud_extraction_ready(ledger.db, 1, cfg)


@pytest.mark.parametrize('scope', ['off', 'excluded', 'other_route'])
def test_disabled_or_out_of_scope_cloud_does_not_block_local_context(local_then_cloud, scope):
    ledger, cfg, _, calls, _ = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    if scope == 'off':
        cfg['cloud_llm']['mode'] = 'off'
    elif scope == 'excluded':
        cfg['cloud_llm']['exclude_project_ids'] = [1]
    else:
        cfg['cloud_llm']['routes'] = ['patient_context']
    assert refresh._cloud_extraction_ready(ledger.db, 1, cfg)
    assert not calls
