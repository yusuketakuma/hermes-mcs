"""Patient context units report fixed wait causes, hold unresolved sources and claim patients."""
import patient_context_refresh as refresh
import patient_context_refresh_state as state
from test_patient_context_refresh import finish, runtime, seed, step

__all__ = ['runtime']


def _lane_sum(result, lane):
    prefix = f'wait_no_due_{lane}_'
    return sum(value for key, value in result.items() if key.startswith(prefix))


def test_no_due_lane_breakdown_matches_the_wait_counter(runtime):
    seed(runtime[0])
    finish(runtime)
    result = step(runtime)
    assert result['status'] == 'complete' and result['wait_no_due_lane'] >= 1
    assert set(refresh._NO_DUE_LANE_KEYS) <= set(result)
    # B and C always share a scope; every hit includes A or B.
    assert _lane_sum(result, 'B') == _lane_sum(result, 'C') == result['wait_no_due_B_none_incomplete']
    assert result['wait_no_due_C_none_unaudited'] == _lane_sum(result, 'C')
    assert max(_lane_sum(result, 'A'), _lane_sum(result, 'B')) <= result['wait_no_due_lane']
    assert result['wait_no_due_lane'] <= _lane_sum(result, 'A') + _lane_sum(result, 'B')


def test_other_source_scan_saved_during_inference_reports_scans(runtime, monkeypatch):
    led, _, calls = runtime
    seed(led, mid=1, pid=1, body='生活環境: 合成の第一記録。')
    seed(led, mid=2, pid=1, body='生活環境: 合成の第二記録。')
    chat = refresh.local_llm.admitted_chat

    def chat_then_other_scan(owner, text, **kwargs):
        answer = chat(owner, text, **kwargs)
        if len(calls) == 1:
            other = 2 if '第一' in calls[0].get('source', '') else 1
            state._replace(led, state.SCAN, 1, {'complete': False}, {'source_key': other}, other)
            led.db.commit()
        return answer

    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', chat_then_other_scan)
    result = step(runtime)
    assert len(calls) == 1
    assert result['status'] == 'source_changed' and result['changed'] == 'scans'
