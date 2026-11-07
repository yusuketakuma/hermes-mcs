"""Partial readiness retains the live QC time/cohort fence (synthetic inputs only)."""
import json

import pytest

import extract_llm
from inference_version import CURRENT_VERSION, VERSION_KEY
import patient_context_refresh_readiness as readiness
import patient_context_refresh_sources as sources
import semantic_qc
from test_patient_context_refresh import CFG, _raw_proof, _semantic_proof, runtime as runtime, seed


def _capture(runtime, *, old=False):
    led, clock, _ = runtime
    cfg = {**CFG, 'semantic': {**CFG['semantic'], 'extract_qc': 'annotate'}}
    seed(led, mid=1, proof=False)
    with led.db:
        led.db.execute('UPDATE messages SET posted_at_ts=? WHERE message_id=1',
            (clock.wall - semantic_qc.QC_REALTIME_MAX_AGE_S - 60 if old else clock.wall,))
    _raw_proof(led, 1, 1)
    _semantic_proof(led, 1, 1, cfg)
    if not old:
        raw = led.artifacts('extract_llm', project_id=1)[0]
        led.artifact_add('extract_qc', '{}', project_id=1, message_id=1,
            meta={VERSION_KEY: CURRENT_VERSION, 'hash': json.loads(raw['meta'])['hash'],
                  'extract_version': extract_llm.EXTRACT_VERSION, 'source_artifact_id': raw['artifact_id']})
    seed(led, mid=2, proof=False)
    with led.db:
        led.db.execute('UPDATE messages SET posted_at_ts=? WHERE message_id=2', (clock.wall,))
    assert not readiness._inference_ready(led, 1, cfg, deadline=clock.mono + 600)
    ready, proof, snapshot = readiness._capture_readiness(led, 1, cfg, deadline=clock.mono + 600,
        complete=_no_whole_patient_gate, allow_dependency_repair=True, source_keys=[1])
    assert ready and proof is not None and snapshot['source_scope'] == [1]
    return cfg, proof


def _no_whole_patient_gate(_pid):
    pytest.fail('an unrelated pending source must not gate the selected subset')


def _replay(runtime, cfg, proof):
    led, clock, _ = runtime
    led.db.execute('BEGIN IMMEDIATE')
    try:
        return readiness._readiness_for_write(led, 1, cfg, proof, deadline=clock.mono + 600,
            complete=_no_whole_patient_gate, allow_dependency_repair=True, source_keys=[1])
    finally:
        led.db.rollback()


def test_partial_qc_capture_records_only_selected_cohort_and_keeps_pending_source_out(runtime):
    led, clock, calls = runtime
    cfg, proof = _capture(runtime)
    assert proof['qc_cohort'] == (1,) and proof['qc_at'] == clock.wall
    assert _replay(runtime, cfg, proof)[0] == 'verified'
    assert not calls
    assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind LIKE 'cloud_%'").fetchone()


@pytest.mark.parametrize('change', ['clock_rollback', 'cohort_expansion'])
def test_partial_qc_replay_rechecks_live_window_when_old_sql_still_matches(runtime, monkeypatch, change):
    led, clock, calls = runtime
    cfg, proof = _capture(runtime, old=True)
    before = led.db.total_changes
    if change == 'clock_rollback':
        clock.wall -= 120
    else:
        monkeypatch.setattr(semantic_qc, 'QC_REALTIME_MAX_AGE_S', semantic_qc.QC_REALTIME_MAX_AGE_S + 120)
    # The old captured cutoff and source/config proof still match. Only live QC
    # eligibility changed; do not let a generic source mismatch make this vacuous.
    with sources._candidate_reader(led, clock.mono + 600) as reader:
        assert proof['environment'] == readiness._readiness_environment(
            cfg, 1, dictionary=proof['dictionary'], db=reader.db)
        assert proof['witness'].matches(reader.db, reader.db.scan_until, lambda: clock.mono)
    assert not readiness._inference_ready(led, 1, cfg, deadline=clock.mono + 600, source_keys=[1])
    original, rechecked = readiness._full_write_readiness, []

    def full(*args, **kwargs):
        rechecked.append(kwargs['source_keys'])
        return original(*args, **kwargs)

    monkeypatch.setattr(readiness, '_full_write_readiness', full)
    assert _replay(runtime, cfg, proof)[0] == 'unknown'
    assert rechecked == [[1]] and led.db.total_changes == before and not calls
