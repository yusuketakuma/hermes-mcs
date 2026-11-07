"""Synthetic source-scoped publication without pretending whole-patient completion."""
import pytest

import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
from test_patient_context_refresh import CFG, document, runtime, seed, step

__all__ = ['runtime']


def publish(runtime):
    led, _, _ = runtime
    for _ in range(20):
        result = step(runtime)
        view = readiness.context_view_input(led.db, 1, cfg=CFG)
        if view is not None:
            return view
        assert result['status'] in ('progressed', 'deferred'), result
    pytest.fail('ready source never became readable')


def test_ready_source_publishes_while_another_local_extraction_is_pending(runtime):
    led, _, _ = runtime
    seed(led)
    seed(led, mid=2, body='生活環境: 合成の未解析記録。', proof=False)
    view = publish(runtime)
    assert view['facts'] and {fact['message_id'] for fact in view['facts']} == {1}
    assert view['coverage']['partial'] is True
    assert view['coverage']['unreflected'] == 1
    assert readiness.inference_coverage(led.db, 1, CFG)['complete'] is False
    _, meta = state._latest(led.db, state.KIND, 1)
    assert meta['source_scope'] == [1]


def test_new_unprocessed_source_does_not_hide_published_sources(runtime):
    led, _, _ = runtime
    seed(led)
    previous = publish(runtime)
    saved = document(led)
    seed(led, mid=2, proof=False)
    view = readiness.context_view_input(led.db, 1, cfg=CFG)
    assert view and view['summary'] == previous['summary']
    assert view['coverage']['unreflected'] == 1
    assert document(led) == saved


def test_verified_sources_remain_useful_during_incomplete_acquisition(runtime):
    led, _, _ = runtime
    seed(led)
    led.db.execute("UPDATE patients SET fetch_state='incomplete',history_floor=NULL WHERE project_id=1")
    led.db.commit()
    view = publish(runtime)
    assert view['coverage']['partial'] is True
    assert view['coverage']['source_coverage']['acquisition_complete'] is False
    assert readiness.inference_coverage(led.db, 1, CFG)['complete'] is False


@pytest.mark.parametrize('mutation', ['edit', 'delete', 'scope'])
def test_scoped_publication_still_rejects_changed_included_source(runtime, mutation):
    led, _, _ = runtime
    seed(led)
    publish(runtime)
    if mutation == 'edit':
        seed(led, body='生活環境: 合成の訂正記録。', proof=False)
    elif mutation == 'delete':
        seed(led, state='deleted', body='', proof=False)
    else:
        led.db.execute("UPDATE patients SET fetch_reason='access_revoked' WHERE project_id=1")
        led.db.commit()
    assert readiness.context_view_input(led.db, 1, cfg=CFG) is None


def test_summary_short_ids_map_back_to_frozen_evidence_without_new_prose(monkeypatch):
    import patient_context_quality_controller as quality
    facts = [{'id': marker * 64, 'project_id': 1, 'category': 'living',
              'posted_at': '2026-11-07', 'subject': 'patient', 'text': text}
             for marker, text in [('a', '合成の住環境記録。'), ('b', '合成の家族支援記録。')]]
    seen = []
    def answer(_cfg, payload, *_args, **_kwargs):
        seen.append(payload)
        return {'fact_ids': ['2', '1']}
    monkeypatch.setattr(quality, '_ask', answer)
    result = quality._summary(CFG, facts, 100, lambda: True)
    assert [r['id'] for r in seen[0]['reports']] == ['1', '2']
    assert result == [{'text': facts[1]['text'], 'fact_ids': ['b' * 64]},
                      {'text': facts[0]['text'], 'fact_ids': ['a' * 64]}]
    monkeypatch.setattr(quality, '_ask', lambda *_a, **_k: {'fact_ids': ['3']})
    with pytest.raises(ValueError, match='nightly_unsupported_summary'):
        quality._summary(CFG, facts, 100, lambda: True)
