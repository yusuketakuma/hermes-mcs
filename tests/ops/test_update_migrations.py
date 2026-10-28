"""Synthetic, idempotent pre-merge migration of installation options."""
import json

import install_options
import mcs_config
import pytest
import safe_io
import update_migrations


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
