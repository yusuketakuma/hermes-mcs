"""Configuration input failures preserve settings and avoid credential writes."""
import service_management
import runtime_inspection
import mcs_runtime
import lifecycle_io
import setup_wizard
import json
import sys
import pytest

import mcs_setup
from test_mcs_setup import _init_env


def test_init_rejects_json_depth_before_any_secret_or_config_write(tmp_path, monkeypatch, capsys):
    cfg = {'mcs_login_id': 'fictional-login', 'notify_target': 'local'}
    _init_env(monkeypatch, tmp_path, cfg)
    path = tmp_path / 'c.json'
    path.write_text(json.dumps(cfg))
    before = path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError('configuration parse must precede credential writes')

    monkeypatch.setattr(lifecycle_io, 'store_keychain', forbidden)
    monkeypatch.setenv('MCS_SETUP_PASSWORD', 'SYNTHETIC_SECRET_CANARY')
    raw = '[' * 5000 + '0' + ']' * 5000
    decode = json.loads
    try:
        decode(raw)
    except RecursionError:
        pass
    else:
        def limited(value, *args, **kwargs):
            if value == raw:
                raise RecursionError('synthetic legacy decoder limit')
            return decode(value, *args, **kwargs)
        monkeypatch.setattr(json, 'loads', limited)
    monkeypatch.setattr(mcs_setup.sys, 'argv', ['mcs_setup', 'init', '--yes', '--set',
                         'semantic=' + raw])
    assert mcs_setup.main() == 1
    assert path.read_bytes() == before and not (tmp_path / '.env').exists()
    assert 'SYNTHETIC_SECRET_CANARY' not in capsys.readouterr().out


@pytest.mark.parametrize('setting', ['semantic=[]', 'deep_history="wrong"',
                                    'notify={"interactive":"slack"}'])
def test_invalid_initial_settings_do_not_rewrite_config_or_credentials(
        tmp_path, monkeypatch, capsys, setting):
    cfg = {'mcs_login_id': 'fictional-login', 'notify_target': 'slack:C1234567890'}
    _init_env(monkeypatch, tmp_path, cfg)
    path = tmp_path / 'c.json'
    path.write_text(json.dumps(cfg))
    before = path.read_bytes()
    seen = []
    monkeypatch.setattr(lifecycle_io, 'store_keychain', lambda *a, **k: seen.append(1) or True)
    monkeypatch.setenv('MCS_SETUP_PASSWORD', 'SYNTHETIC_SECRET_CANARY')
    monkeypatch.setattr(mcs_setup.sys, 'argv', ['mcs_setup', 'init', '--yes', '--set', setting])
    assert mcs_setup.main() == 1
    assert seen == [] and path.read_bytes() == before
    assert not (tmp_path / '.env').exists()
    assert 'SYNTHETIC_SECRET_CANARY' not in capsys.readouterr().out


def test_null_required_channel_still_requires_an_explicit_value(monkeypatch, capsys):
    replies = iter(['', 'C1234567890'])
    monkeypatch.setattr('builtins.input', lambda *a: next(replies))
    assert setup_wizard.ask('notify.slack.channel_id', 'req', None,
                         'fictional channel', None, True) == 'C1234567890'
    assert '必須項目です' in capsys.readouterr().out


@pytest.mark.parametrize('transport,channel', [('slack', 'C1234567890'), ('discord', '1234567890')])
def test_fresh_channel_default_reuses_the_operators_notification_target(monkeypatch, transport, channel):
    key = f'notify.{transport}.channel_id'
    monkeypatch.setattr(setup_wizard, 'WIZARD', [('synthetic', [(key, 'req', None, 'channel', None)])])
    replies = iter(['', '', 'いいえ'] * 2)
    monkeypatch.setattr('builtins.input', lambda *a: next(replies))
    cfg = {'notify_target': transport + ':' + channel}
    setup_wizard.wizard(cfg, home=mcs_setup.HOME)
    assert cfg['notify'][transport]['channel_id'] == channel
    cfg['notify'][transport]['channel_id'] = 'keep-existing'
    setup_wizard.wizard(cfg, home=mcs_setup.HOME)
    assert cfg['notify'][transport]['channel_id'] == 'keep-existing'


def test_secondary_system_transport_is_selected_before_its_scope_prompts(monkeypatch):
    cfg = {'runtime_mode': 'standalone', 'mcs_login_id': 'fictional-login',
           'notify_target': 'discord:1234567890'}
    answers = {'setup_detail': 'detailed', 'drug_map.bundled': 'いいえ', 'notify_system_target': 'slack:C1234567890',
               'notify.slack.profile': 'default', 'notify.slack.application_id': 'A1234567890',
               'notify.slack.team_id': 'T1234567890', 'notify.slack.allowed_user_ids': 'U1234567890',
               'notify.slack.project_ids': '101', 'notify.discord.profile': 'default',
               'notify.discord.application_id': '1234567891', 'notify.discord.guild_id': '1234567892',
               'notify.discord.allowed_user_ids': '1234567893', 'notify.discord.project_ids': '101'}
    seen = []
    kinds = {key: kind for _, items in setup_wizard.WIZARD
             for key, kind, *_ in items}
    kinds['setup_detail'] = 'choice:recommended,detailed'
    kinds['drug_map.bundled'] = 'bool'

    def answer(prompt):
        key = prompt.split('設定項目: ', 1)[1].split()[0].rstrip(':')
        assert key in kinds, f'unexpected setting prompt: {key}'
        assert key not in seen, f'fixture answer was rejected: {key}'
        if kinds[key].startswith('req') and '現在:' not in prompt and '推奨:' not in prompt:
            assert key in answers, f'missing fixture answer for required setting: {key}'
        seen.append(key)
        return answers.get(key, '')

    monkeypatch.setattr('builtins.input', answer)
    setup_wizard.wizard(cfg, home=mcs_setup.HOME)
    assert seen.index('notify_system_target') < seen.index('notify.slack.profile')
    assert cfg['notify']['slack']['channel_id'] == 'C1234567890'
    assert cfg['notify']['discord']['channel_id'] == '1234567890'
    assert mcs_setup.validate_config(cfg)[0] == []


@pytest.mark.parametrize('minor', [10, 11, 13])
def test_legacy_updater_can_import_setup_but_cannot_run_commands(monkeypatch, capsys, minor):
    import importlib.util
    spec = importlib.util.spec_from_file_location('setup_legacy_import', mcs_setup.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, 'version_info', (3, minor, 0))
    spec.loader.exec_module(module)
    errors, warnings = module.validate_config({})
    assert isinstance(errors, list) and isinstance(warnings, list)
    assert module.main(['services']) == 1
    assert 'Python 3.14以上が必要です' in capsys.readouterr().err


@pytest.mark.parametrize('python,sqlite,allowed,blocked', [
    ('3.13.8', '3.51.3', False, True),
    ('3.13.8', '3.51.3', True, False),
    ('3.10.0', '3.51.3', True, False),
    ('3.9.6', '3.51.3', True, True),
    ('3.13.8', '3.51.0', True, True),
    ('3.14.8', '3.51.3', False, False),
])
def test_migration_preflight_preserves_sqlite_and_metadata_gates(
        monkeypatch, tmp_path, python, sqlite, allowed, blocked):
    from test_runtime_compatibility import _facts
    monkeypatch.setattr(mcs_setup, 'AGENTS_DIR', str(tmp_path))
    facts = _facts(python, sqlite)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: facts)
    errors = service_management.runtime_gate_errors({}, allow_runtime_migration=allowed, paths=mcs_setup._service_paths())
    assert bool(errors) is blocked
    if tuple(map(int, python.split('.'))) < (3, 14):
        assert mcs_runtime.runtime_problem(facts, recovery=True, allow_runtime_migration=True)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: None)
    assert service_management.runtime_gate_errors({}, allow_runtime_migration=True, paths=mcs_setup._service_paths()) == [
        'selected_runtime: 実行環境の情報を取得できません']
