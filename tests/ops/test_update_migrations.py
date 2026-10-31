"""Synthetic, idempotent pre-merge migration of installation options."""
import json

import install_options
import mcs_config
import pytest
import safe_io
import update_migrations
import cloud_policy
import provider_settings


def put(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def test_migration_keeps_legacy_and_unrelated_fields_and_is_idempotent(tmp_path):
    config, legacy = tmp_path / 'config.json', tmp_path / 'install_options.json'
    credential, database = tmp_path / '.env', tmp_path / 'synthetic.db'
    credential.write_text('SYNTHETIC_CREDENTIAL=do-not-output\n')
    database.write_bytes(b'synthetic-only-data')
    put(config, {'mcs_login_id': 'synthetic', 'notify_target': 'local', 'unknown': {'keep': 1}})
    put(legacy, {'v': 1, 'install_args': ['--no-llm']})
    before_legacy = legacy.read_bytes()
    baseline = install_options.inspect(config, legacy)
    stages = []
    assert update_migrations.apply(config, legacy, baseline, lambda: stages.append('pending'))
    assert json.loads(config.read_text()) == {'mcs_login_id': 'synthetic', 'notify_target': 'local',
                                              'unknown': {'keep': 1}, 'install_args': ['--no-llm']}
    assert legacy.read_bytes() == before_legacy and stages == ['pending']
    assert credential.read_text() == 'SYNTHETIC_CREDENTIAL=do-not-output\n'
    assert database.read_bytes() == b'synthetic-only-data'
    before = (config.read_bytes(), config.stat().st_ino)
    assert not update_migrations.apply(config, legacy, install_options.inspect(config, legacy),
                                       lambda: pytest.fail('no-op wrote journal'))
    assert (config.read_bytes(), config.stat().st_ino) == before


def test_migration_stops_on_changed_record_before_journal_or_publication(tmp_path):
    config, legacy = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    put(legacy, {'v': 1, 'install_args': []})
    baseline = install_options.inspect(config, legacy)
    put(legacy, {'v': 1, 'install_args': ['--no-llm']})
    original = config.read_bytes()
    with pytest.raises(update_migrations.MigrationError):
        update_migrations.apply(config, legacy, baseline, lambda: pytest.fail('unexpected journal'))
    assert config.read_bytes() == original
    assert json.loads(legacy.read_text())['install_args'] == ['--no-llm']


def test_interruption_after_publication_is_resumable_without_replacing_user_data(monkeypatch, tmp_path):
    config, legacy = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'unknown': {'keep': True}})
    put(legacy, {'v': 1, 'install_args': ['--no-brew']})
    before_legacy = legacy.read_bytes()
    baseline = install_options.inspect(config, legacy)
    monkeypatch.setattr(safe_io, 'fsync_dir', lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(update_migrations.MigrationError, match='publication_unverified'):
        update_migrations.apply(config, legacy, baseline, lambda: None)
    assert json.loads(config.read_text()) == {'unknown': {'keep': True}, 'install_args': ['--no-brew']}
    assert legacy.read_bytes() == before_legacy
    assert not update_migrations.apply(config, legacy, install_options.inspect(config, legacy),
                                       lambda: pytest.fail('duplicate publication'))


def test_older_validator_treats_new_key_as_warning_and_keeps_existing_fields(monkeypatch, tmp_path):
    config, legacy = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'mcs_login_id': 'synthetic', 'notify_target': 'local'})
    put(legacy, {'v': 1, 'install_args': []})
    assert update_migrations.apply(config, legacy, install_options.inspect(config, legacy), lambda: None)
    rules = dict(mcs_config.CONFIG_RULES)
    rules.pop('install_args')
    monkeypatch.setattr(mcs_config, 'CONFIG_RULES', rules)
    errors, warnings = mcs_config.validate_config(json.loads(config.read_text()))
    assert not errors
    assert any('install_args' in warning for warning in warnings)
    assert legacy.exists()


def _provider_root(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    (tmp_path / 'data').mkdir(mode=0o700)
    raw = {'mcs_login_id': 'synthetic', 'notify_target': 'local',
           'cloud_llm': {'mode': 'off', 'provider': 'openai', 'model': 'gpt-6-luna',
                         'provider_budgets': {'openai': {'daily_cost_limit_usd': 1,
                                                         'monthly_cost_limit_usd': 10}}},
           'semantic': {'mode': 'off', 'daily_request_budget': 2},
           'unknown': {'keep': [1, None]}}
    put(tmp_path / 'config.json', raw)
    monkeypatch.setattr(cloud_policy, 'provider_key', lambda *_args: None)
    return raw


def test_provider_initial_migration_keeps_raw_values_and_split_noop(tmp_path, monkeypatch):
    raw = _provider_root(tmp_path, monkeypatch)
    assert update_migrations.migrate_provider_settings(tmp_path) is True
    common = json.loads((tmp_path / 'config.json').read_text())
    docs = {name: json.loads((tmp_path / 'data' / 'provider_settings' / f'{name}.json').read_text())
            for name in provider_settings.PROVIDERS}
    assert common['unknown'] == raw['unknown'] and common['cloud_llm'] == {'mode': 'off', 'provider': 'openai'}
    assert common['semantic'] == {'mode': 'off'}
    assert docs['openai'] == {'model': 'gpt-6-luna', 'budget': raw['cloud_llm']['provider_budgets']['openai']}
    assert docs['jev'] == {'daily_request_budget': 2} and docs['anthropic'] == {}
    paths = [tmp_path / 'config.json', tmp_path / 'data' / '.provider-settings-authority.json',
             *(tmp_path / 'data' / 'provider_settings' / f'{name}.json' for name in provider_settings.PROVIDERS)]
    before = [(p.read_bytes(), p.stat().st_ino) for p in paths]
    assert update_migrations.migrate_provider_settings(tmp_path) is False
    assert [(p.read_bytes(), p.stat().st_ino) for p in paths] == before
    assert not (tmp_path / '.env').exists()


@pytest.mark.parametrize('damage', ['pending', 'authority_missing', 'config_missing'])
def test_provider_initial_migration_refuses_unverified_state(tmp_path, monkeypatch, damage):
    _provider_root(tmp_path, monkeypatch)
    if damage == 'pending':
        put(tmp_path / 'data' / '.provider-settings-pending.json', {'synthetic': True})
    elif damage == 'authority_missing':
        (tmp_path / 'data' / 'provider_settings').mkdir(mode=0o700)
    else:
        (tmp_path / 'config.json').unlink()
    with pytest.raises(update_migrations.MigrationError):
        update_migrations.migrate_provider_settings(tmp_path)
    if damage == 'pending':
        assert (tmp_path / 'data' / '.provider-settings-pending.json').exists()
    assert not (tmp_path / 'data' / '.provider-settings-authority.json').exists()


def test_provider_initial_migration_keeps_pending_on_interrupted_publication(tmp_path, monkeypatch):
    _provider_root(tmp_path, monkeypatch)
    original = provider_settings._replace
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('synthetic interruption')
    monkeypatch.setattr(provider_settings, '_replace', interrupted)
    with pytest.raises(update_migrations.MigrationError):
        update_migrations.migrate_provider_settings(tmp_path)
    assert (tmp_path / 'data' / '.provider-settings-pending.json').exists()
    with pytest.raises(update_migrations.MigrationError):
        update_migrations.migrate_provider_settings(tmp_path)


def test_provider_initial_migration_rejects_postcommit_readback_mismatch(tmp_path, monkeypatch):
    _provider_root(tmp_path, monkeypatch)
    original = provider_settings.commit
    def inconsistent(*args, **kwargs):
        result = original(*args, **kwargs)
        return {**result, 'common': {}}
    monkeypatch.setattr(provider_settings, 'commit', inconsistent)
    with pytest.raises(update_migrations.MigrationError, match='readback_unverified'):
        update_migrations.migrate_provider_settings(tmp_path)
