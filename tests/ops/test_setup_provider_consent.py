"""Setup consent previews provider selection from booleans without reading keys."""
import copy

import pytest

import cloud_policy
import mcs_util
import semantic_policy
import setup_wizard


@pytest.mark.parametrize('block,presence,provider,mode,sending', [
    ({}, {'openai': True}, 'openai', 'on', True),
    ({}, {'anthropic': True}, 'anthropic', 'on', True),
    ({}, {'anthropic': True, 'openai': True}, 'anthropic', 'on', True),
    ({'provider_order': ['openai', 'anthropic']}, {'anthropic': True, 'openai': True}, 'openai', 'on', True),
    ({'provider': 'openai'}, {'anthropic': True, 'openai': True}, 'openai', 'on', True),
    ({'provider': 'anthropic'}, {'openai': True}, 'anthropic', 'off', False),
    ({'provider': 'openai'}, {'anthropic': True}, 'openai', 'off', False),
    ({}, {}, 'anthropic', 'off', False),
    ({'mode': 'on', 'project_ids': None, 'provider': 'openai'}, {}, 'openai', 'on', False),
    ({'mode': 'off', 'provider': 'openai'}, {'openai': True}, 'openai', 'off', False),
    ({'mode': 'dry_run', 'project_ids': None, 'provider': 'openai'}, {'openai': True}, 'openai', 'dry_run', False),
    ({'mode': 'shadow', 'project_ids': None, 'provider': 'openai'}, {'openai': True}, 'openai', 'shadow', True),
    ({'daily_cost_limit_usd': None, 'monthly_cost_limit_usd': None}, {'openai': True}, 'openai', 'off', False),
])
def test_consent_matches_selected_provider_and_send_mode(monkeypatch, capsys, block, presence,
                                                       provider, mode, sending):
    cfg = {'notify_target': 'slack:synthetic', 'cloud_llm': block}
    before = copy.deepcopy(cfg)
    def no_key_read(*args, **kwargs):
        pytest.fail('consent must use presence, never read a credential')
    for name in ('selected_key', 'provider_key', 'api_key', 'resolve_settings'):
        monkeypatch.setattr(cloud_policy, name, no_key_read)
    monkeypatch.setattr(setup_wizard, 'ask', lambda *args, **kwargs: False)
    assert setup_wizard.confirm_save(cfg, plugin_profile=None, key_presence=presence) is False
    output = capsys.readouterr().out
    assert f"AI利用先: {provider} / モデル {cloud_policy.PROVIDERS[provider]}" in output
    assert f'外部AI追加処理: {mode} /' in output
    assert ('患者名を含む投稿本文・参考文脈を匿名化せず' in output) is sending
    if sending:
        label = 'OpenAI' if provider == 'openai' else 'Anthropic'
        assert f'選択した{label}のAPIへ送信します' in output
    assert cfg == before


def test_existing_key_preview_reads_only_presence_problem_codes(monkeypatch, capsys):
    seen = []
    def problem(name):
        seen.append(name)
        return None if name == 'MCS_OPENAI_API_KEY' else 'key_missing'
    monkeypatch.setattr(mcs_util, 'private_env_problem', problem)
    monkeypatch.setattr(cloud_policy, 'selected_key', lambda *a, **k: pytest.fail('credential read'))
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *a, **k: pytest.fail('credential read'))
    monkeypatch.setattr(semantic_policy, 'runtime_config',
                        lambda cfg: semantic_policy.semantic_config(cfg, key_present=False))
    monkeypatch.setattr(setup_wizard, 'ask', lambda *args, **kwargs: False)
    setup_wizard.confirm_save({'notify_target': 'slack:synthetic'}, plugin_profile=None)
    assert seen == ['MCS_ANTHROPIC_API_KEY', 'MCS_OPENAI_API_KEY']
    assert 'AI利用先: openai' in capsys.readouterr().out
