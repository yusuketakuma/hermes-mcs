"""A cloud-only priority pass whose head always falls back (known outcome, nothing sent) moves
past it, so the next pass reaches a healthy candidate; an unknown send keeps its claim, and a
non-priority pass is left as it was (fully synthetic)."""
import extract_llm_runner
import cloud_inference
import extract_llm
from test_cloud_response_recovery import legacy_cloud_routes
from cloud_selection_progress_testkit import (_claim_count, _cloud_pass, _select,
                                              cloud_candidates as cloud_candidates, routed as routed)

__all__ = ["cloud_candidates", "routed"]


def _infer(head, seen, *, unknown=False):
    def infer(_body, **kwargs):
        provider = kwargs.get('provider')
        assert provider is not None                      # cloud only: never the local backend
        seen.append(provider.message_id)
        if provider.message_id == head:
            if unknown:
                provider.possibly_sent = True
            raise cloud_inference.CloudFallback('cloud_invalid_response')
        provider('synthetic complete request', deadline=kwargs['deadline'], max_tokens=100)
        return {}
    return infer


def test_a_head_that_always_falls_back_is_moved_past_and_the_next_pass_reaches_the_llm(
        cloud_candidates, monkeypatch):
    led, _, calls, *_ = cloud_candidates
    head = _select(led, 'extract')[0]['message_id']
    seen = []
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', _infer(head, seen))
    first = _cloud_pass(led)
    assert seen == [head] and not calls and first['done'] == 0
    assert first['defer_reasons'] == {'cloud_fallback': 1}
    assert first['fallback_codes'] == {'cloud_invalid_response': 1}   # the fixed code, apart
    assert _claim_count(led) == 0                       # a known outcome keeps no claim
    second = _cloud_pass(led)
    assert seen[1] != head and len(calls) == 1 and second['done'] == 1
    for _ in range(3):                                  # the head never takes the front back
        _cloud_pass(led)
    assert seen.count(head) == 1 and len(calls) == 4


def test_an_unknown_send_keeps_its_claim_and_is_not_counted_as_a_fallback(cloud_candidates, monkeypatch):
    led, _, calls, *_ = cloud_candidates
    head = _select(led, 'extract')[0]['message_id']
    seen = []
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', _infer(head, seen, unknown=True))
    first = _cloud_pass(led)
    assert first['defer_reasons'] == {'cloud_unit_unresolved': 1} and first['fallback_codes'] == {}
    assert _claim_count(led) == 1 and not calls


def test_a_non_priority_cloud_pass_does_not_move_its_selection(cloud_candidates, monkeypatch):
    led, *_ = cloud_candidates
    head = _select(led, 'extract')[0]['message_id']
    discards = []
    monkeypatch.setattr(extract_llm_runner, '_cloud_priority_discard', lambda _l, mid: discards.append(mid))
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', _infer(head, []))
    with legacy_cloud_routes():
        result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True, cloud_only=True,
                                         prioritize_patients=False, batch_k=4)
    assert discards == [] and result['fallback_codes'] in ({}, {'cloud_invalid_response': 1})

