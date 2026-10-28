"""API key setup uses only synthetic values and a private temporary root."""
import os

import pytest

import api_key_setup
import mcs_cli
import mcs_setup
import mcs_util
import setup_wizard
from test_mcs_setup import _init_env


@pytest.fixture
def terminal(monkeypatch, capsys):
    monkeypatch.setattr(api_key_setup, 'is_terminal', lambda: True)
    return monkeypatch


def answers(monkeypatch, selections, keys=()):
    choices, secrets = iter(selections), iter(keys)
    monkeypatch.setattr('builtins.input', lambda _: next(choices))
    monkeypatch.setattr(api_key_setup.getpass, 'getpass', lambda _: next(secrets))


def test_save_selected_key_preserves_other_credentials_and_never_prints_values(tmp_path, terminal, capsys):
    path = tmp_path / '.env'
    path.write_text('MCS_PASSWORD=synthetic-password\nOTHER=keep\nOTHER=keep-again\n')
    path.chmod(0o600)
    answers(terminal, ['1', '2', '1'], ['synthetic-jev-key'])
    assert api_key_setup.run(tmp_path) == 0
    assert path.read_text() == ('MCS_PASSWORD=synthetic-password\nOTHER=keep\nOTHER=keep-again\n'
                                'TYPESAFE_API_KEY=synthetic-jev-key\n')
    assert mcs_util.private_env_value('TYPESAFE_API_KEY', tmp_path) == 'synthetic-jev-key'
    assert path.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert 'synthetic-jev-key' not in captured.out + captured.err
    assert 'synthetic-password' not in captured.out + captured.err


def test_declining_save_preserves_file(tmp_path, terminal):
    path = tmp_path / '.env'
    path.write_text('MCS_PASSWORD=synthetic-password\n')
    path.chmod(0o600)
    before = path.read_bytes()
    answers(terminal, ['1', '2', '2'], ['synthetic-key'])
    assert api_key_setup.run(tmp_path) == 0
    assert path.read_bytes() == before


def test_both_keys_and_quoted_characters_round_trip(tmp_path, terminal):
    answers(terminal, ['1', '1', '1'], ['synthetic-jev', 'synthetic-"anthropic'])
    assert api_key_setup.run(tmp_path) == 0
    assert mcs_util.private_env_value('TYPESAFE_API_KEY', tmp_path) == 'synthetic-jev'
    assert mcs_util.private_env_value('MCS_ANTHROPIC_API_KEY', tmp_path) == 'synthetic-"anthropic'


def test_explicit_change_replaces_exported_duplicate(tmp_path, terminal):
    path = tmp_path / '.env'
    path.write_text('export TYPESAFE_API_KEY=synthetic-old\nOTHER=keep\n')
    path.chmod(0o600)
    answers(terminal, ['1', '2', '1'], ['synthetic-new'])
    assert api_key_setup.run(tmp_path) == 0
    assert path.read_text() == 'OTHER=keep\nTYPESAFE_API_KEY=synthetic-new\n'


def test_post_publication_failure_reports_unknown(tmp_path, terminal, capsys):
    answers(terminal, ['1', '2', '1'], ['synthetic-key'])
    original = api_key_setup.safe_io.atomic_write
    def write(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('synthetic directory sync failure')
    terminal.setattr(api_key_setup.safe_io, 'atomic_write', write)
    assert api_key_setup.run(tmp_path) == 1
    assert mcs_util.private_env_value('TYPESAFE_API_KEY', tmp_path) == 'synthetic-key'
    text = capsys.readouterr().out
    assert '保存結果を確認できません' in text and 'synthetic-key' not in text


def test_concurrent_change_is_not_overwritten(tmp_path, terminal, capsys):
    path = tmp_path / '.env'
    path.write_text('MCS_PASSWORD=before\n')
    path.chmod(0o600)
    def enter(_):
        path.write_text('MCS_PASSWORD=after\n')
        return 'synthetic-key'
    terminal.setattr(api_key_setup.getpass, 'getpass', enter)
    terminal.setattr('builtins.input', lambda _: next(selections))
    selections = iter(['1', '2', '1'])
    assert api_key_setup.run(tmp_path) == 1
    assert path.read_text() == 'MCS_PASSWORD=after\n'
    assert 'synthetic-key' not in capsys.readouterr().out


@pytest.mark.parametrize('bad', ['symlink', 'public', 'hardlink'])
def test_untrusted_env_is_not_replaced(tmp_path, terminal, bad):
    path = tmp_path / '.env'
    target = tmp_path / 'other'
    target.write_text('MCS_PASSWORD=synthetic\n')
    target.chmod(0o600)
    if bad == 'symlink':
        path.symlink_to(target)
    elif bad == 'hardlink':
        os.link(target, path)
    else:
        path.write_text('MCS_PASSWORD=synthetic\n')
        path.chmod(0o644)
    assert api_key_setup.run(tmp_path) == 1
    assert target.read_text() == 'MCS_PASSWORD=synthetic\n'
    assert path.is_symlink() if bad == 'symlink' else path.exists()


def test_noninteractive_does_not_prompt_or_write(tmp_path, monkeypatch):
    monkeypatch.setattr(api_key_setup, 'is_terminal', lambda: False)
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('unexpected prompt'))
    assert api_key_setup.run(tmp_path) == 2
    assert not (tmp_path / '.env').exists()


def test_cli_routes_to_api_key_setup(monkeypatch):
    calls = []
    monkeypatch.setenv('MCS_LIFECYCLE_PINNED', '1')
    monkeypatch.setattr(mcs_setup, 'main', lambda args: calls.append(args) or 0)
    assert mcs_cli.main(['setup', 'api-keys']) == 0
    assert calls == [['api-keys']]


def test_interactive_init_offers_keys_after_config_is_saved(tmp_path, terminal):
    _init_env(terminal, tmp_path, {'mcs_login_id': 'synthetic', 'notify_target': 'local'})
    terminal.setattr(setup_wizard, 'wizard', lambda cfg: 1)
    terminal.setattr(setup_wizard, 'wizard_optional_features', lambda cfg: None)
    terminal.setattr(setup_wizard, 'wizard_drug_map', lambda *a, **k: None)
    terminal.setattr(setup_wizard, 'confirm_save', lambda *a, **k: True)
    terminal.setattr(mcs_setup, '_init_store_secrets', lambda *a: None)
    calls = []
    def setup_keys(root):
        assert (tmp_path / 'c.json').is_file()
        calls.append(root)
        return 0
    terminal.setattr(api_key_setup, 'run', setup_keys)
    assert mcs_setup.main(['init']) == 0
    assert calls == [str(tmp_path)]


@pytest.mark.parametrize('key_result, expected, proceeds', [(1, 1, True), (130, 130, False)])
def test_init_handles_key_failure_without_hiding_partial_state(tmp_path, terminal, key_result, expected, proceeds, capsys):
    _init_env(terminal, tmp_path, {'mcs_login_id': 'synthetic', 'notify_target': 'local'})
    terminal.setattr(setup_wizard, 'wizard', lambda cfg: 1)
    terminal.setattr(setup_wizard, 'wizard_optional_features', lambda cfg: None)
    terminal.setattr(setup_wizard, 'wizard_drug_map', lambda *a, **k: None)
    terminal.setattr(setup_wizard, 'confirm_save', lambda *a, **k: True)
    terminal.setattr(mcs_setup, '_init_store_secrets', lambda *a: None)
    terminal.setattr(api_key_setup, 'run', lambda _: key_result)
    called = []
    terminal.setattr(mcs_setup, '_apply_plugin_integration', lambda *a: called.append(True) or True)
    assert mcs_setup.main(['init']) == expected
    assert bool(called) is proceeds
    assert '設定は保存済み' in capsys.readouterr().out
