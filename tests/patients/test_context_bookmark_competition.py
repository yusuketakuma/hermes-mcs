"""Keep clinical source scans independent of another patient's scan bookmark."""
from contextlib import closing

import pytest

from ledger import Ledger
import patient_context_plan as plan
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
import patient_context_schedule as schedule
from test_patient_context_refresh import document, runtime, seed, step

__all__ = ['runtime']


@pytest.mark.parametrize('lane', ['B', 'C'])
@pytest.mark.parametrize('drift', ['bookmark', 'unit_progress', 'metadata', 'bad_bookmark'])
def test_other_patient_bookmark_does_not_reject_selected_source(runtime, monkeypatch, lane, drift):
    led, clock, calls = runtime
    seed(led, mid=1, pid=1)
    if lane == 'C':
        first = step(runtime)
        assert first['status'] == 'progressed' and first['lane'] == 'B'
        assert document(led, state.SCAN)['complete']
        assert not document(led, state.SCAN)['audited']
    seed(led, mid=2, pid=2, proof=False)
    content, meta = state._latest(led.db, state.PROGRESS, 0)
    # Start this selection at patient 1; preserve the real B/C lane rotation.
    state._replace(led, state.PROGRESS, 0, {**content, 'patient_scan': None}, meta)
    led.db.commit()
    before = document(led, state.PROGRESS), document(led, state.SCAN), len(calls)
    select = refresh._select_patient
    write_readiness = readiness._readiness_for_write
    observed = {}
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]

    with closing(Ledger(db_path)) as other:
        def select_then_other_bookmark(*args, **kwargs):
            selected = select(*args, **kwargs)
            best, pid, rotation, *_ = selected
            assert pid == 1 and plan.lane_rotation(best, rotation[0])[0] == lane
            snapshot, _, published = best[lane]
            assert snapshot['_readiness_proof'] is not None
            observed['source_sig'] = snapshot['source_sig']
            assert state._published_version(led.db, pid) == published
            old_content, old_meta = state._latest(other.db, state.PROGRESS, 0)
            assert (old_content, old_meta) == rotation
            next_patient = schedule.plan(other.db, old_content['patient_scan'])
            assert next_patient['project_id'] == 2
            advanced = refresh._advance_patient_scan(other, rotation, next_patient['next'],
                                                     clock.mono + 600, lambda: True)
            assert advanced is not None and advanced[0]['patient_scan'] != old_content['patient_scan']
            assert {key: value for key, value in advanced[0].items() if key != 'patient_scan'} == {
                key: value for key, value in old_content.items() if key != 'patient_scan'}
            assert advanced[1] == old_meta
            if drift == 'unit_progress':
                state._replace(other, state.PROGRESS, 0,
                               {**advanced[0], 'cursor': (advanced[0].get('cursor', 0) + 1) % 10,
                                'units': advanced[0].get('units', 0) + 1}, advanced[1])
                other.db.commit()
            elif drift == 'metadata':
                state._replace(other, state.PROGRESS, 0, advanced[0],
                               {**advanced[1], 'synthetic_other_owner': True})
                other.db.commit()
            elif drift == 'bad_bookmark':
                state._replace(other, state.PROGRESS, 0,
                               {**advanced[0], 'patient_scan': {'version': False}}, advanced[1])
                other.db.commit()
            observed['rotation'] = state._latest(other.db, state.PROGRESS, 0)
            assert state._published_version(led.db, pid) == published
            assert document(led, state.PROGRESS) == before[0]
            assert document(led, state.SCAN) == before[1]
            return selected

        def verified_write(*args, **kwargs):
            result = write_readiness(*args, **kwargs)
            if 'verified' not in observed:
                assert result[0] == 'verified'
                assert result[1]['source_sig'] == observed['source_sig']
                observed['verified'] = True
            return result

        monkeypatch.setattr(refresh, '_select_patient', select_then_other_bookmark)
        monkeypatch.setattr(readiness, '_readiness_for_write', verified_write)
        result = step(runtime)

    assert observed['verified'], 'the competing bookmark must not invalidate clinical evidence'
    after = state._latest(led.db, state.PROGRESS, 0)
    if drift != 'bookmark':
        assert result['status'] == 'deferred' and result['done'] == 0
        assert after == observed['rotation']
        assert (document(led, state.PROGRESS), document(led, state.SCAN), len(calls)) == before
        return
    assert result['status'] == 'progressed' and result['lane'] == lane and result['done'] == 1, result
    assert after[0]['patient_scan'] == observed['rotation'][0]['patient_scan']
    assert after[0]['units'] == observed['rotation'][0].get('units', 0) + 1
    assert len(calls) == before[2] + (lane == 'B')
    scan = document(led, state.SCAN)
    assert scan['complete'] and scan['audited'] == (lane == 'C')
    assert document(led, state.PROGRESS, pid=2) is None
    assert document(led, state.SCAN, pid=2) is None


def test_competing_connection_bookmark_write_reports_cas_refusal(runtime):
    """A real second-connection bookmark write makes the stale expected
    tuple fail the CAS check with the fixed bookmark_cas reason."""
    led, clock, _ = runtime
    seed(led, mid=1, pid=1)
    expected = state._latest(led.db, state.PROGRESS, 0)
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    with closing(Ledger(db_path)) as other:
        content, meta = state._latest(other.db, state.PROGRESS, 0)
        state._replace(other, state.PROGRESS, 0, {**content, 'cursor': 9}, meta)
        other.db.commit()
        observed = {}
        assert refresh._advance_patient_scan(
            led, expected, schedule.advance_turn(None), clock.mono + 600,
            lambda: True, observed_out=observed) is None
    assert observed['scan_fence'] == 'bookmark_cas'
    assert state._latest(led.db, state.PROGRESS, 0)[0]['cursor'] == 9
