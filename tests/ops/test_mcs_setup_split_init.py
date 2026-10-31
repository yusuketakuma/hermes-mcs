"""Synthetic setup edits preserve and revalidate split provider settings."""
import hashlib
import json
from types import SimpleNamespace

import pytest

import cloud_policy
import mcs_setup
import provider_settings
import semantic_jev


def _put(path, value):
    raw = (json.dumps(value, sort_keys=True) + '\n').encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def split(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    directory = data / 'provider_settings'
    directory.mkdir(mode=0o700)
    common = {'mcs_login_id': 'synthetic', 'notify_target': 'local',
              'cloud_llm': {'mode': 'off', 'provider': 'openai'}, 'semantic': {'mode': 'off'}}
    providers = {
        'anthropic': {'model': cloud_policy.PROVIDERS['anthropic'],
                      'budget': {'daily_cost_limit_usd': 1, 'monthly_cost_limit_usd': 2}},
        'openai': {'model': cloud_policy.PROVIDERS['openai'],
                   'budget': {'daily_cost_limit_usd': 0, 'monthly_cost_limit_usd': 3}},
        'jev': {'daily_request_budget': 0, 'until': None},
    }
    _put(tmp_path / 'config.json', common)
    hashes = {name: _put(directory / (name + '.json'), value) for name, value in providers.items()}
    _put(data / '.provider-settings-authority.json', {'version': 2, 'state': 'committed', 'files': hashes,
         'common_policy_sha256': provider_settings.common_policy_digest(common)})
    monkeypatch.setattr(mcs_setup, 'CONF_PATH', str(tmp_path / 'config.json'))
    monkeypatch.setattr(mcs_setup, 'HOME', str(tmp_path))
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda name, root=None: None)
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    return tmp_path, providers, owners


def _read(root, owners):
    return provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)


def test_split_init_rechecks_after_own_directory_changes(split):
    root, providers, owners = split
    cfg, initial = mcs_setup._init_config_snapshot()
    cfg['self_posts'] = True
    cfg['semantic']['daily_request_budget'] = 5
    cfg['cloud_llm']['provider_budgets']['openai']['daily_cost_limit_usd'] = 0.5
    cfg['new_synthetic_section'] = {'null': None, 'zero': 0, 'off': False}
    (root / 'data' / 'cmd').mkdir(mode=0o700)
    (root / 'data' / 'drug-map').mkdir(mode=0o700)
    (root / 'data').chmod(0o700)
    mcs_setup._init_save_config(cfg, initial, SimpleNamespace(_config_publication_started=False))
    saved = _read(root, owners)
    assert saved['providers']['anthropic'] == providers['anthropic']
    assert saved['providers']['openai']['budget']['daily_cost_limit_usd'] == 0.5
    assert saved['providers']['jev'] == {'daily_request_budget': 5, 'until': None}
    assert saved['common']['self_posts'] is True
    assert saved['common']['new_synthetic_section'] == cfg['new_synthetic_section']
    assert not (root / '.env').exists()


def test_split_init_switches_provider_without_erasing_previous_model(split):
    root, providers, owners = split
    cfg, initial = mcs_setup._init_config_snapshot()
    cfg['cloud_llm']['provider'] = 'anthropic'
    mcs_setup._init_reselect_provider(cfg, initial, ['cloud_llm.provider="anthropic"'])
    mcs_setup._init_save_config(cfg, initial, SimpleNamespace(_config_publication_started=False))
    saved = _read(root, owners)
    assert saved['providers'] == providers
    assert saved['common']['cloud_llm']['provider'] == 'anthropic'


def test_split_init_can_replace_cloud_block_without_erasing_unselected_model(split):
    root, providers, owners = split
    cfg, initial = mcs_setup._init_config_snapshot()
    cfg['cloud_llm'] = {}
    mcs_setup._init_reselect_provider(cfg, initial, ['cloud_llm={}'])
    mcs_setup._init_save_config(cfg, initial, SimpleNamespace(_config_publication_started=False))
    saved = _read(root, owners)
    assert saved['common']['cloud_llm'] == {}
    assert 'model' not in saved['providers']['anthropic']
    assert saved['providers']['openai']['model'] == providers['openai']['model']
    assert saved['providers']['jev'] == providers['jev']


def test_setup_declined_confirmation_has_no_secret_or_config_writes(split, monkeypatch):
    import cloud_cli
    import setup_wizard

    root, _, _ = split
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    monkeypatch.setattr(setup_wizard, 'wizard', lambda cfg: 1)
    monkeypatch.setattr(setup_wizard, 'wizard_optional_features', lambda cfg: None)
    monkeypatch.setattr(setup_wizard, 'confirm_save', lambda *args, **kwargs: False)
    monkeypatch.setattr(cloud_cli, 'pending_key_presence', lambda *args: {})
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *args: pytest.fail('secret access'))
    monkeypatch.setattr(setup_wizard, 'wizard_drug_map', lambda *args, **kwargs: pytest.fail('dictionary write'))
    assert mcs_setup.main(['init']) == 130
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('changed', ['common', 'provider', 'pending', 'authority', 'backup', 'stage'])
def test_split_init_holds_changed_snapshot_and_migration_artifacts(split, changed):
    root, _, _ = split
    cfg, initial = mcs_setup._init_config_snapshot()
    data = root / 'data'
    path = {'common': root / 'config.json', 'provider': data / 'provider_settings' / 'anthropic.json',
            'pending': data / '.provider-settings-pending.json',
            'authority': data / '.provider-settings-authority.json',
            'backup': data / '.provider-settings-backup-synthetic.bin',
            'stage': data / '.provider-settings-stage-synthetic'}[changed]
    if changed == 'stage':
        path.mkdir(mode=0o700)
    else:
        _put(path, {'synthetic': 'changed'})
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises((OSError, ValueError)):
        mcs_setup._init_save_config(cfg, initial, SimpleNamespace(_config_publication_started=False))
    assert all(path.read_bytes() == value for path, value in before.items())


@pytest.mark.parametrize('damage', ['pending', 'missing_common', 'bad_provider'])
def test_split_init_never_treats_provider_failure_as_legacy_corruption(split, damage):
    root, _, _ = split
    data = root / 'data'
    if damage == 'pending':
        _put(data / '.provider-settings-pending.json', {})
    elif damage == 'missing_common':
        (root / 'config.json').unlink()
    else:
        _put(data / 'provider_settings' / 'openai.json', {'synthetic': True})
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises((OSError, ValueError)):
        mcs_setup._init_config_snapshot()
    assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_init_command_edits_split_without_touching_credentials(split, monkeypatch, capsys):
    import mcs_drug
    import safe_io

    root, providers, owners = split
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: None)
    monkeypatch.setattr(mcs_setup, '_apply_plugin_integration', lambda *_: True)
    monkeypatch.setattr(mcs_setup, 'cmd_check', lambda *_: 0)
    monkeypatch.setattr(mcs_drug, 'BUNDLE', root / 'absent-bundle')
    monkeypatch.setattr(safe_io, 'atomic_write', lambda *args, **kwargs: pytest.fail('legacy atomic write'))
    monkeypatch.setattr(safe_io, 'publish_new_bytes', lambda *args, **kwargs: pytest.fail('legacy new write'))
    assert mcs_setup.main(['init', '--yes', '--set', 'self_posts=true',
                           '--set', 'semantic.daily_request_budget=5',
                           '--set', 'cloud_llm.provider="anthropic"']) == 0
    saved = _read(root, owners)
    assert saved['common']['self_posts'] is True
    assert saved['common']['cloud_llm']['provider'] == 'anthropic'
    assert saved['providers']['anthropic'] == providers['anthropic']
    assert saved['providers']['openai'] == providers['openai']
    assert saved['providers']['jev'] == {'daily_request_budget': 5, 'until': None}
    assert not (root / '.env').exists()
    output = capsys.readouterr().out
    assert '設定ファイルを保存しました' in output
    assert '保存結果を確認できません' not in output


def test_split_init_rejects_replaced_root_link_to_same_target(split, monkeypatch):
    import os

    root, _, _ = split
    alias = root.parent / (root.name + '-synthetic-link')
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(mcs_setup, 'CONF_PATH', str(alias / 'config.json'))
    monkeypatch.setattr(mcs_setup, 'HOME', str(alias))
    cfg, initial = mcs_setup._init_config_snapshot()
    cfg['self_posts'] = True
    watched = [root / 'config.json', root / 'data' / '.provider-settings-authority.json',
               *(root / 'data' / 'provider_settings' / (name + '.json') for name in provider_settings.PROVIDERS)]
    before = {path: path.read_bytes() for path in watched}
    replacement = root.parent / (root.name + '-synthetic-replacement')
    replacement.symlink_to(root, target_is_directory=True)
    os.replace(replacement, alias)
    with pytest.raises(ValueError, match='config_changed'):
        mcs_setup._init_save_config(cfg, initial, SimpleNamespace(_config_publication_started=False))
    assert alias.resolve() == root
    assert {path: path.read_bytes() for path in watched} == before
    assert not (root / 'data' / '.provider-settings-pending.json').exists()
