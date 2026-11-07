"""A completed local pass stays final while the per-post cloud lane is closed."""
import time

import pytest

import patient_context_refresh as refresh
from extract_testkit import _message
from test_cloud_after_local import local_then_cloud, routed, _local_pass, _cloud_pass
from test_patient_context_refresh import runtime, seed, finish, CFG

__all__ = ['local_then_cloud', 'routed', 'runtime']


def test_existing_local_context_stays_usable_with_closed_cloud_route(runtime):
    ledger, _, _ = runtime
    seed(ledger)
    finish(runtime)
    assert refresh.context_view_input(ledger.db, 1, cfg=CFG)
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'project_ids': [1], 'routes': ['reextract']}}
    # A closed route creates no cloud requirement: the verified local
    # publication stays usable without a cloud reservation or send.
    assert refresh.context_view_input(ledger.db, 1, cfg=cfg)
    coverage = refresh.inference_coverage(ledger.db, 1, cfg)
    assert coverage['status'] != 'cloud_extraction_pending'
    assert coverage['complete'] and coverage['publication_eligible']


def test_patient_readiness_accepts_local_final_extraction(local_then_cloud):
    ledger, cfg, _, calls, _ = local_then_cloud
    cfg['semantic'] = {'mode': 'off'}
    with ledger.db:
        ledger.db.execute("INSERT INTO patients(project_id,patient_name,fetch_state,history_floor) "
                          "VALUES(1,'synthetic-patient','complete',-1)")
    assert _local_pass(ledger)['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    deadline = time.monotonic() + 600
    # Local extraction is final for individual posts: the closed per-post
    # route adds no readiness requirement, reservation or send.
    assert refresh._inference_ready(ledger, 1, cfg, deadline=deadline)
    assert not calls
    assert _cloud_pass(ledger)['done'] == 0
    assert refresh._inference_ready(ledger, 1, cfg, deadline=deadline)
    assert not calls
    assert not ledger.artifacts('cloud_usage_reserve') and not ledger.artifacts('cloud_usage_result')


def test_local_final_context_gate_uses_real_saved_proofs(local_then_cloud):
    ledger, cfg, _, calls, _ = local_then_cloud
    assert _local_pass(ledger)['done'] == 1
    source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    assert refresh._raw_extraction_current(ledger.db, source, cfg)[0]
    cfg['cloud_llm']['mode'] = 'on'
    before = ledger.db.total_changes
    assert refresh._raw_extraction_current(ledger.db, source, cfg)[0]
    assert refresh._cloud_extraction_ready(ledger.db, 1, cfg)
    assert ledger.db.total_changes == before and not calls
    assert _cloud_pass(ledger)['done'] == 0
    assert refresh._raw_extraction_current(ledger.db, source, cfg)[0]
    assert refresh._cloud_extraction_ready(ledger.db, 1, cfg)
    assert not calls
    assert not ledger.artifacts('cloud_usage_reserve') and not ledger.artifacts('cloud_usage_result')
    # A real source edit breaks the saved proof's binding: stale is never ready.
    ledger.save_messages([_message(1, body='合成の経過記録を追記します。',
                                   project_id=source['project_id'],
                                   posted_at=source['posted_at'])])
    source = ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    assert not refresh._raw_extraction_current(ledger.db, source, cfg)[0]
    assert not refresh._inference_ready(ledger, 1, cfg,
                                        deadline=time.monotonic() + 600)


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
