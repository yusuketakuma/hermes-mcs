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
