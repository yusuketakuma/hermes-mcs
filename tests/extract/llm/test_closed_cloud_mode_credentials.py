"""Closed individual routes never need credentials to select local inference."""
import pytest

import cloud_inference
import cloud_policy
import extract_llm_provider as provider


@pytest.mark.parametrize('route', ['reextract', 'semantic_backlog'])
def test_closed_mode_does_not_resolve_cloud_credentials(monkeypatch, route):
    assert not cloud_policy.send_route(route)

    def forbidden(*args, **kwargs):
        pytest.fail('closed route resolved cloud settings or credentials')

    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', forbidden)
    assert provider._cloud_mode(None, {}, route, 1) == 'off'


def test_patient_context_still_resolves_its_real_profile(monkeypatch):
    cfg = {'cloud_llm': {'mode': 'on', 'provider': 'anthropic',
                        'project_ids': [1], 'routes': ['patient_context']}}
    seen = []

    def key(backend, root=None):
        seen.append(backend)
        return 'synthetic-key'

    monkeypatch.setattr(cloud_policy, 'provider_key', key)
    assert cloud_policy.send_route('patient_context')
    assert provider._cloud_mode(None, cfg, 'patient_context', 1) == 'on'
    assert seen and set(seen) == {'anthropic'}


_ON = {'mode': 'on', 'provider': 'anthropic', 'project_ids': [1], 'routes': ['patient_context']}
_REVIEW_OPEN = {'state': 'not_required', 'auto_enable': True}


@pytest.mark.parametrize('block,key_problem,review,pid,reason', [
    (_ON, None, _REVIEW_OPEN, 1, None),
    ({**_ON, 'mode': 'off'}, None, _REVIEW_OPEN, 1, 'mode_off'),
    (_ON, None, _REVIEW_OPEN, 2, 'scope'),
    (_ON, None, {'state': 'pending', 'auto_enable': False}, 1, 'provider_not_approved'),
    (_ON, None, {'state': 'acknowledged', 'auto_enable': True, 'acknowledged': {'services': ['openai']}},
     1, 'provider_not_approved'),
    (_ON, None, 'unreadable', 1, 'transfer_review_unreadable'),
    (_ON, 'env_file_unreadable', _REVIEW_OPEN, 1, 'env_file_unreadable'),
    (_ON, 'env_file_missing', _REVIEW_OPEN, 1, 'env_file_missing'),
    (_ON, 'key_duplicate', _REVIEW_OPEN, 1, 'key_duplicate'),
])
def test_off_reason_names_the_check_that_closed_the_cloud(monkeypatch, block, key_problem, review, pid, reason):
    import external_transfer
    cfg = {'cloud_llm': dict(block)}
    state = dict(external_transfer.UNREADABLE) if review == 'unreadable' else dict(review)
    monkeypatch.setattr(external_transfer, 'status', lambda root=None: dict(state))
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda backend, root=None: None if key_problem else 'synthetic-key')
    monkeypatch.setattr(cloud_policy, 'private_env_problem', lambda *args: key_problem)
    assert provider.cloud_off_reason(None, cfg, 'patient_context', pid) == reason
    # The reason exists exactly when the mode check answers off, and is always a declared code.
    assert (provider._cloud_mode(None, cfg, 'patient_context', pid) == 'off') == (reason is not None)
    import cloud_send_admission
    assert reason is None or reason in cloud_send_admission.PRE_SEND_VERDICTS


def test_off_reason_of_a_closed_route_reads_no_credentials(monkeypatch):
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings',
                        lambda *args, **kwargs: pytest.fail('closed route resolved cloud settings'))
    assert provider.cloud_off_reason(None, {}, 'reextract', 1) == 'route_closed'


def test_off_reason_of_an_unresolvable_profile_and_an_unnamed_key_problem(monkeypatch):
    cfg = {'cloud_llm': dict(_ON)}
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda backend, root=None: 'synthetic-key')
    monkeypatch.setattr(cloud_policy, 'resolve_profile', lambda *args, **kwargs: (None, ['config: synthetic']))
    assert provider.cloud_off_reason(None, cfg, 'patient_context', 1) == 'profile_invalid'
    monkeypatch.undo()
    # The key read failed although the next read of its file finds no problem: a temporary read
    # failure, still named and never left as available.
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda backend, root=None: None)
    monkeypatch.setattr(cloud_policy, 'private_env_problem', lambda *args: None)
    assert provider._cloud_mode(None, cfg, 'patient_context', 1) == 'off'
    assert provider.cloud_off_reason(None, cfg, 'patient_context', 1) == 'env_file_unreadable'


def _flaky_once(monkeypatch, name, first, then):
    """Answer ``first`` on the first read of ``cloud_policy.<name>`` and ``then`` afterwards."""
    calls = []

    def read(*args, **kwargs):
        calls.append(1)
        return first() if len(calls) == 1 else then()

    monkeypatch.setattr(cloud_policy, name, read)
    return calls


_OMITTED = {'provider': 'anthropic', 'project_ids': [1], 'routes': ['patient_context']}


@pytest.mark.parametrize('flaky', ['key', 'review'])
def test_a_read_that_fails_once_waits_instead_of_a_scope_stop(monkeypatch, flaky):
    import cloud_send_admission
    import external_transfer
    cfg = {'cloud_llm': dict(_OMITTED)}
    monkeypatch.setattr(cloud_policy, 'private_env_problem', lambda *args: None)
    if flaky == 'key':
        monkeypatch.setattr(external_transfer, 'status', lambda root=None: dict(_REVIEW_OPEN))
        calls = _flaky_once(monkeypatch, 'provider_key', lambda: None, lambda: 'synthetic-key')
    else:
        monkeypatch.setattr(cloud_policy, 'provider_key', lambda backend, root=None: 'synthetic-key')
        reads = []

        def status(root=None):
            reads.append(1)
            return ({'state': 'pending', 'auto_enable': False} if len(reads) == 1 else dict(_REVIEW_OPEN))

        monkeypatch.setattr(external_transfer, 'status', status)
        calls = reads
    mode, reason = provider.cloud_route_mode(None, cfg, 'patient_context', 1)
    assert len(calls) >= 2
    # The configuration names this patient for the cloud: the stop is a read that recovered,
    # which waits with any paid answer kept, never a definite scope that pins the input locally.
    assert (mode, reason) == ('off', 'env_file_unreadable')
    for progress in (False, True):
        assert cloud_send_admission.classify_refusal('pre_send', reason, progress=progress) == 'defer'


def test_a_declared_exclusion_stays_a_scope_stop_after_a_flaky_key_read(monkeypatch):
    import external_transfer
    monkeypatch.setattr(external_transfer, 'status', lambda root=None: dict(_REVIEW_OPEN))
    monkeypatch.setattr(cloud_policy, 'private_env_problem', lambda *args: None)
    _flaky_once(monkeypatch, 'provider_key', lambda: None, lambda: 'synthetic-key')
    cfg = {'cloud_llm': {**_OMITTED, 'project_ids': [2]}}
    assert provider.cloud_route_mode(None, cfg, 'patient_context', 1) == ('off', 'scope')
    assert cloud_policy.refusal_code(cfg, 'scope', model=None, route='patient_context',
                                     project_id=1) == 'scope'
