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
