"""Synthetic checks for the beginner setup path; no live services."""
import lifecycle_io
import setup_wizard
import json

import pytest

import mcs_setup
import mcs_util
import semantic_policy
import cloud_policy
from test_mcs_setup import _init_env


@pytest.mark.parametrize('kind,answer,expected', [
    ('bool', 'はい', True), ('bool', 'いいえ', False), ('bool', '2', False),
    ('choice:hermes,standalone', '2', 'standalone'),
    ('choice:off,slack,discord,lineworks', '2', 'slack'),
])
def test_numbered_and_japanese_answers(kind, answer, expected):
    assert setup_wizard.parse_answer(kind, answer) == (True, expected)


def test_recommended_path_keeps_settings_and_collects_authority(monkeypatch):
    cfg = {'runtime_mode': 'standalone', 'mcs_login_id': 'synthetic-login',
           'notify_target': 'slack:C1234567890', 'trickle_pages': 7,
           'notify': {'interactive': 'slack', 'slack': {'channel_id': 'C1234567890'}}}
    answers = {'notify.slack.profile': 'default', 'notify.slack.application_id': 'A1234567890',
               'notify.slack.team_id': 'T1234567890',
               'notify.slack.allowed_user_ids': 'U1234567890',
               'notify.slack.project_ids': '101', 'drug_map.bundled': 'いいえ'}
    seen = []

    def answer(prompt):
        key = prompt.split('設定項目: ', 1)[1].split()[0].rstrip(':')
        assert key not in seen, f'fixture answer was rejected: {key}'
        seen.append(key)
        return answers.get(key, '')

    monkeypatch.setattr('builtins.input', answer)
    setup_wizard.wizard(cfg)
    assert 'trickle_pages' not in seen and cfg['trickle_pages'] == 7
    assert 'semantic.mode' not in seen and 'mode' not in cfg.get('semantic', {})
    assert semantic_policy.semantic_config(cfg, key_present=False)[0]['mode'] == 'off'
    assert semantic_policy.semantic_config(cfg, key_present=True)[0]['mode'] == 'assist'
    assert semantic_policy.semantic_config(cfg, key_present=True)[0]['daily_request_budget'] == 10000
    assert 'cloud_llm' not in cfg
    cloud, errors = cloud_policy.cloud_config(cfg, key_present=True)
    assert errors == [] and cloud['mode'] == 'on'
    assert (cloud['daily_cost_limit_usd'], cloud['monthly_cost_limit_usd']) == (25, 75)
    assert cloud_policy.cloud_config(cfg, key_present=False)[0]['mode'] == 'off'
    assert cfg['notify']['slack']['allowed_user_ids'] == ['U1234567890']
    assert cfg['notify']['slack']['project_ids'] == [101]
    assert mcs_setup.validate_config(cfg)[0] == []


def test_recommended_path_preserves_explicit_semantic_off(monkeypatch):
    cfg = {'mcs_login_id': 'synthetic-login', 'notify_target': 'local',
           'semantic': {'mode': 'off', 'daily_request_budget': 17}}
    seen = []

    def answer(prompt):
        key = prompt.split('設定項目: ', 1)[1].split()[0].rstrip(':')
        assert key not in seen, f'fixture answer was rejected: {key}'
        seen.append(key)
        return 'いいえ' if key == 'drug_map.bundled' else ''

    monkeypatch.setattr('builtins.input', answer)
    setup_wizard.wizard(cfg)
    assert 'semantic.mode' not in seen
    assert cfg['semantic'] == {'mode': 'off', 'daily_request_budget': 17}
    for key_present in (False, True):
        actual, errors = semantic_policy.semantic_config(cfg, key_present=key_present)
        assert not errors
        assert actual['mode'] == 'off' and actual['daily_request_budget'] == 17


@pytest.mark.parametrize('daily,monthly', [(None, None), (0, 0), (2, 20)])
def test_recommended_path_preserves_explicit_cloud_caps_and_scope(monkeypatch, daily, monthly):
    cloud = {'mode': 'off', 'project_ids': [101], 'exclude_project_ids': [102],
             'daily_cost_limit_usd': daily, 'monthly_cost_limit_usd': monthly}
    cfg = {'mcs_login_id': 'synthetic-login', 'notify_target': 'local', 'cloud_llm': dict(cloud)}
    monkeypatch.setattr('builtins.input', lambda prompt: 'いいえ' if 'drug_map.bundled' in prompt else '')
    setup_wizard.wizard(cfg)
    assert cfg['cloud_llm'] == cloud
    for key_present in (False, True):
        normalized, errors = cloud_policy.cloud_config(cfg, key_present=key_present)
        assert errors == [] and normalized['mode'] == 'off'
        assert (normalized['daily_cost_limit_usd'], normalized['monthly_cost_limit_usd']) == (daily, monthly)
        assert normalized['project_ids'] == [101] and normalized['exclude_project_ids'] == [102]


def test_required_unknown_does_not_invent_an_identifier(monkeypatch, capsys):
    answers = iter(['?', '', 'synthetic-login'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    assert setup_wizard.ask('mcs_login_id', 'req', None, 'ログインID', None) == 'synthetic-login'
    output = capsys.readouterr().out
    assert '管理者に確認' in output and '必須項目' in output


@pytest.mark.parametrize('interruption', ['q', EOFError, KeyboardInterrupt])
def test_cancellation_does_not_write_config_or_secrets(monkeypatch, tmp_path, capsys, interruption):
    _init_env(monkeypatch, tmp_path, {'mcs_login_id': 'synthetic-login', 'notify_target': 'slack'})

    def answer(prompt):
        if isinstance(interruption, str):
            return interruption
        raise interruption

    monkeypatch.setattr('builtins.input', answer)
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *a: pytest.fail('secret write'))
    assert mcs_setup.main(['init']) == 130
    assert json.loads((tmp_path / 'c.json').read_text()) == {
        'mcs_login_id': 'synthetic-login', 'notify_target': 'slack'}
    assert '保存していません' in capsys.readouterr().out


@pytest.mark.parametrize('interruption', [EOFError, KeyboardInterrupt])
@pytest.mark.parametrize('existing', [False, True])
def test_cancellation_during_password_input_does_not_claim_config_saved(
        monkeypatch, tmp_path, capsys, interruption, existing):
    cfg = {'mcs_login_id': 'synthetic-login', 'notify_target': 'slack', 'trickle_pages': 7}
    _init_env(monkeypatch, tmp_path, cfg)
    path = tmp_path / 'c.json'
    original = json.dumps(cfg).encode()
    if existing:
        path.write_bytes(original)
    else:
        path.unlink()
    monkeypatch.setattr(setup_wizard, 'wizard', lambda cfg, **kwargs: cfg.update(trickle_pages=2))
    monkeypatch.setattr(setup_wizard, 'confirm_save', lambda cfg, **kwargs: True)

    def interrupt(prompt):
        raise interruption('synthetic-private-input')

    monkeypatch.setattr(mcs_setup.getpass, 'getpass', interrupt)
    monkeypatch.setattr(lifecycle_io, 'store_keychain', lambda *a: pytest.fail('secret write'))
    monkeypatch.setattr(mcs_setup, '_init_save_config', lambda *a: pytest.fail('config write'))
    assert mcs_setup.main(['init', '--login-id', 'synthetic-login', '--notify-target', 'slack']) == 130
    assert path.exists() is existing
    if existing:
        assert path.read_bytes() == original
    assert not (tmp_path / '.env').exists()
    output = capsys.readouterr().out
    assert '今回の変更は設定ファイルに保存していません' in output
    assert '途中まで保存した設定は残っています' not in output
    assert 'synthetic-private-input' not in output


@pytest.mark.parametrize('interruption', [EOFError, KeyboardInterrupt])
def test_cancellation_after_save_reports_partial_progress(monkeypatch, tmp_path, capsys, interruption):
    _init_env(monkeypatch, tmp_path, {'mcs_login_id': 'synthetic-login', 'notify_target': 'slack'})

    def interrupt(*args):
        raise interruption

    monkeypatch.setattr(mcs_setup, '_apply_plugin_integration', interrupt)
    assert mcs_setup.main(['init', '--yes']) == 130
    assert (tmp_path / 'c.json').exists()
    assert '途中まで保存した設定は残っています' in capsys.readouterr().out


@pytest.mark.parametrize('interruption', [EOFError, KeyboardInterrupt])
@pytest.mark.parametrize('check_status', [0, 1])
def test_reinit_after_saved_cancellation_keeps_choices_and_returns_check_result(
        monkeypatch, tmp_path, capsys, interruption, check_status):
    initial = {'runtime_mode': 'standalone', 'notify_system_target': 'slack:G1234567890',
               'notify': {'slack': {'channel_id': 'C1234567890'}}}
    _init_env(monkeypatch, tmp_path, initial)
    path = tmp_path / 'c.json'
    monkeypatch.setattr(mcs_util, 'load_config', lambda: (
        json.loads(path.read_text()) if path.exists() else dict(initial)))
    monkeypatch.setattr(mcs_setup.getpass, 'getpass', lambda _: '')
    monkeypatch.setattr(lifecycle_io, 'store_keychain', lambda *a: pytest.fail('secret write'))
    answers = {
        'setup.save': 'はい',
        'mcs_login_id': 'synthetic-login', 'notify_target': 'slack:C1234567890',
        'notify.interactive': '2', 'notify.slack.profile': 'synthetic-profile',
        'notify.slack.application_id': 'A1234567890', 'notify.slack.team_id': 'T1234567890',
        'notify.slack.allowed_user_ids': 'U1234567890', 'notify.slack.project_ids': '101',
    }
    seen = set()

    def answer(prompt):
        key = prompt.split('設定項目: ', 1)[1].split()[0].rstrip(':')
        assert key not in seen, f'fixture answer was rejected: {key}'
        seen.add(key)
        return answers.get(key, '')

    def interrupt(*args):
        raise interruption

    monkeypatch.setattr('builtins.input', answer)
    monkeypatch.setattr(mcs_setup, '_apply_plugin_integration', interrupt)
    assert mcs_setup.main(['init']) == 130
    saved = json.loads(path.read_text())
    assert saved['runtime_mode'] == 'standalone'
    assert saved['notify_target'] == 'slack:C1234567890'
    assert saved['notify']['slack']['allowed_user_ids'] == ['U1234567890']
    assert saved['notify']['slack']['project_ids'] == [101]
    assert path.stat().st_mode & 0o777 == 0o600
    assert '途中まで保存した設定は残っています' in capsys.readouterr().out

    answers.clear()
    answers['setup.save'] = 'はい'
    seen.clear()
    integrated, checked = [], []
    monkeypatch.setattr(mcs_setup, '_apply_plugin_integration',
                        lambda cfg, args: integrated.append(cfg) or True)
    monkeypatch.setattr(mcs_setup, '_init_standalone_connectors', lambda *a: True)
    monkeypatch.setattr(mcs_setup, 'cmd_check', lambda args: checked.append(True) or check_status)
    assert mcs_setup.main(['init']) == check_status
    assert integrated == [saved] and checked == [True]
    assert json.loads(path.read_text()) == saved
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / '.env').exists()
    assert ('利用開始に必要な確認が残っています' in capsys.readouterr().out) is bool(check_status)


def test_choices_follow_the_question_and_explain_slack_recommendation(monkeypatch, capsys):
    def answer(prompt):
        output = capsys.readouterr().out
        assert output.index('通知画面の形式') < output.index('1. カードを使わない（文字だけの通知）')
        assert 'Slack のカード通知（通知を使う場合の推奨）' in output
        assert '初期値' in output
        assert '番号を選んでください' in prompt
        return ''

    monkeypatch.setattr('builtins.input', answer)
    assert setup_wizard.ask('notify.interactive', 'choice:off,slack,discord,lineworks',
                         'off', '通知画面の形式', None) == 'off'


def test_notification_target_help_explains_where_to_find_id(monkeypatch, capsys):
    answers = iter(['?', 'slack:C0123456789'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    assert setup_wizard.ask('notify_target', 'req', None, '通知先', None) == 'slack:C0123456789'
    output = capsys.readouterr().out
    assert 'チャンネル名 → 詳細の最下部' in output
    assert 'docs/guides/EASY_SETUP.md' in output
    assert 'docs/guides/INSTALLATION.md' in output
