"""Synthetic checks for the beginner setup path; no live services."""
import pytest

import mcs_setup
from test_mcs_setup import _init_env


@pytest.mark.parametrize('kind,answer,expected', [
    ('bool', 'はい', True), ('bool', 'いいえ', False), ('bool', '2', False),
    ('choice:hermes,standalone', '2', 'standalone'),
    ('choice:off,slack,discord,lineworks', '2', 'slack'),
])
def test_numbered_and_japanese_answers(kind, answer, expected):
    assert mcs_setup._parse_answer(kind, answer) == (True, expected)


def test_recommended_path_keeps_settings_and_collects_authority(monkeypatch):
    cfg = {'runtime_mode': 'standalone', 'mcs_login_id': 'synthetic-login',
           'notify_target': 'slack:C1234567890', 'trickle_pages': 7,
           'notify': {'interactive': 'slack'}}
    answers = {'notify.slack.profile': 'default', 'notify.slack.application_id': 'A123',
               'notify.slack.team_id': 'T123', 'notify.slack.allowed_user_ids': 'U123',
               'notify.slack.project_ids': '101'}
    seen = []

    def answer(prompt):
        key = prompt.split('設定項目: ', 1)[1].split()[0].rstrip(':')
        seen.append(key)
        return answers.get(key, '')

    monkeypatch.setattr('builtins.input', answer)
    mcs_setup._wizard(cfg)
    assert 'trickle_pages' not in seen and cfg['trickle_pages'] == 7
    assert 'semantic.mode' not in seen and cfg['semantic']['mode'] == 'off'
    assert cfg['notify']['slack']['allowed_user_ids'] == ['U123']
    assert cfg['notify']['slack']['project_ids'] == [101]
    assert mcs_setup.validate_config(cfg)[0] == []


def test_required_unknown_does_not_invent_an_identifier(monkeypatch, capsys):
    answers = iter(['?', '', 'synthetic-login'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    assert mcs_setup._ask('mcs_login_id', 'req', None, 'ログインID', None) == 'synthetic-login'
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
    assert not (tmp_path / 'c.json').exists()
    assert '保存していません' in capsys.readouterr().out


@pytest.mark.parametrize('interruption', [EOFError, KeyboardInterrupt])
def test_cancellation_after_save_reports_partial_progress(monkeypatch, tmp_path, capsys, interruption):
    _init_env(monkeypatch, tmp_path, {'mcs_login_id': 'synthetic-login', 'notify_target': 'slack'})

    def interrupt(*args):
        raise interruption

    monkeypatch.setattr(mcs_setup, '_apply_plugin_integration', interrupt)
    assert mcs_setup.main(['init', '--yes']) == 130
    assert (tmp_path / 'c.json').exists()
    assert '途中まで保存した設定は残っています' in capsys.readouterr().out


def test_choices_follow_the_question_and_explain_slack_recommendation(monkeypatch, capsys):
    def answer(prompt):
        output = capsys.readouterr().out
        assert output.index('通知画面の形式') < output.index('1. カードを使わない（文字だけの通知）')
        assert 'Slack のカード通知（通知を使う場合の推奨）' in output
        assert '初期値' in output
        assert '番号を選んでください' in prompt
        return ''

    monkeypatch.setattr('builtins.input', answer)
    assert mcs_setup._ask('notify.interactive', 'choice:off,slack,discord,lineworks',
                         'off', '通知画面の形式', None) == 'off'


def test_notification_target_help_explains_where_to_find_id(monkeypatch, capsys):
    answers = iter(['?', 'slack:C0123456789'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    assert mcs_setup._ask('notify_target', 'req', None, '通知先', None) == 'slack:C0123456789'
    output = capsys.readouterr().out
    assert 'チャンネル名 → 詳細の最下部' in output
    assert 'docs/guides/EASY_SETUP.md' in output
    assert 'docs/guides/INSTALLATION.md' in output
