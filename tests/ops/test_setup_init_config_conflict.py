"""Synthetic init conflicts, including the side effects preceding publication."""
import json
import pytest
import config_edit
import mcs_setup
import safe_io
from test_mcs_setup import _init_env


def _setup(monkeypatch, tmp_path):
    cfg = {'mcs_login_id': 'synthetic', 'notify_target': 'local'}
    _init_env(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: None)
    return cfg, tmp_path / 'c.json'


def test_init_preserves_competing_edit_after_confirmation(monkeypatch, tmp_path, capsys):
    cfg, path = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard', lambda _: 1)
    monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard_optional_features', lambda *_a, **_k: None)
    def confirm(*_a, **_k):
        path.write_text(json.dumps({**cfg, 'self_posts': True}))
        return True
    monkeypatch.setattr(mcs_setup.setup_wizard, 'confirm_save', confirm)
    monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard_drug_map', lambda *_a, **_k: None)
    assert mcs_setup.main(['init']) == 1
    assert json.loads(path.read_text())['self_posts'] is True
    assert '認証情報や辞書' in capsys.readouterr().out


def test_init_does_not_replace_a_concurrent_creator(monkeypatch, tmp_path):
    _, path = _setup(monkeypatch, tmp_path)
    path.unlink()
    winner = {'mcs_login_id': 'winner', 'notify_target': 'local'}
    def secret_side_effect(*_):
        path.write_text(json.dumps(winner))
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', secret_side_effect)
    assert mcs_setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert json.loads(path.read_text()) == winner


def test_init_does_not_archive_a_repaired_config(monkeypatch, tmp_path):
    _, path = _setup(monkeypatch, tmp_path)
    path.write_text('{broken')
    winner = {'mcs_login_id': 'winner', 'notify_target': 'local'}
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: path.write_text(json.dumps(winner)) and None)
    assert mcs_setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 1
    assert json.loads(path.read_text()) == winner
    assert not list(tmp_path.glob('c.json.corrupt-*'))


def test_init_shared_lock_rejects_without_rewriting(monkeypatch, tmp_path):
    cfg, path = _setup(monkeypatch, tmp_path)
    with config_edit._edit_lock(path):
        assert mcs_setup.main(['init', '--yes']) == 1
    assert json.loads(path.read_text()) == cfg


def test_init_detects_change_just_before_publication(monkeypatch, tmp_path):
    cfg, path = _setup(monkeypatch, tmp_path)
    original = safe_io.atomic_write
    winner = {**cfg, 'self_posts': True}
    def raced(name, writer, *args, **kwargs):
        def stage(stream):
            path.write_text(json.dumps(winner))
            writer(stream)
        return original(name, stage, *args, **kwargs)
    monkeypatch.setattr(safe_io, 'atomic_write', raced)
    assert mcs_setup.main(['init', '--yes']) == 1
    assert json.loads(path.read_text()) == winner


def test_init_reports_uncertain_result_after_rename(monkeypatch, tmp_path, capsys):
    _, path = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(safe_io, 'fsync_dir', lambda *_a, **_k: (_ for _ in ()).throw(OSError('synthetic')))
    assert mcs_setup.main(['init', '--yes', '--set', 'self_posts=true']) == 1
    assert json.loads(path.read_text())['self_posts'] is True
    assert '保存結果を確認できません' in capsys.readouterr().out


@pytest.mark.parametrize('existing', [True, False])
def test_init_interrupt_after_config_publication_reports_unknown(monkeypatch, tmp_path, capsys, existing):
    _, path = _setup(monkeypatch, tmp_path)
    if not existing:
        path.unlink()
    def interrupted(*_a, **_k):
        raise KeyboardInterrupt
    monkeypatch.setattr(safe_io, 'fsync_dir', interrupted)
    assert mcs_setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local',
                           '--set', 'self_posts=true']) == 130
    assert json.loads(path.read_text())['self_posts'] is True
    out = capsys.readouterr().out
    assert 'config.jsonの保存結果を確認できません' in out
    assert '認証情報や辞書の準備が済んでいる場合があります' in out
    assert '今回の変更はconfig.jsonに保存していません' not in out


def test_init_interrupt_before_publication_reports_prior_side_effects(monkeypatch, tmp_path, capsys):
    cfg, path = _setup(monkeypatch, tmp_path)
    snapshot = mcs_setup._init_config_snapshot
    calls = []
    def interrupted():
        calls.append(True)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return snapshot()
    monkeypatch.setattr(mcs_setup, '_init_config_snapshot', interrupted)
    assert mcs_setup.main(['init', '--yes']) == 130
    assert json.loads(path.read_text()) == cfg
    out = capsys.readouterr().out
    assert '今回の変更は設定ファイルに保存していません' in out
    assert '認証情報や辞書の準備が済んでいる場合があります' in out
    assert '保存結果を確認できません' not in out


def test_init_interrupt_after_dictionary_preparation_reports_partial_state(monkeypatch, tmp_path, capsys):
    cfg, path = _setup(monkeypatch, tmp_path)
    dictionary = tmp_path / 'synthetic-dictionary'
    monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard', lambda _: 1)
    monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard_optional_features', lambda *_a, **_k: None)
    monkeypatch.setattr(mcs_setup.setup_wizard, 'confirm_save', lambda *_a, **_k: True)
    def interrupted(*_a, **_k):
        dictionary.write_text('synthetic')
        raise KeyboardInterrupt
    monkeypatch.setattr(mcs_setup.setup_wizard, 'wizard_drug_map', interrupted)
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', lambda *_: pytest.fail('secret mutation'))
    assert mcs_setup.main(['init']) == 130
    assert json.loads(path.read_text()) == cfg and dictionary.read_text() == 'synthetic'
    assert '認証情報や辞書の準備が済んでいる場合があります' in capsys.readouterr().out


def test_init_env_fsync_failure_reports_unknown_after_keychain_save(monkeypatch, tmp_path, capsys):
    original_store = mcs_setup._init_store_secrets
    cfg, path = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mcs_setup, '_init_store_secrets', original_store)
    monkeypatch.setenv('MCS_SETUP_PASSWORD', 'SYNTHETIC_PRIVATE_MARKER')
    saved = []
    monkeypatch.setattr(mcs_setup.lifecycle_io, 'store_keychain', lambda *_a, **_k: saved.append(True) or True)
    def failed(*_a, **_k):
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr(safe_io, 'fsync_dir', failed)
    assert mcs_setup.main(['init', '--yes']) == 1
    assert saved == [True] and json.loads(path.read_text()) == cfg
    assert (tmp_path / '.env').exists()
    out = capsys.readouterr().out
    assert '.env: 保存結果を確認できません' in out
    assert 'config.jsonは保存していません' in out and '認証情報や辞書' in out
    assert 'SYNTHETIC_PRIVATE_MARKER' not in out


def test_broken_symlink_backup_keeps_the_target(monkeypatch, tmp_path):
    _, path = _setup(monkeypatch, tmp_path)
    path.unlink()
    target = tmp_path / 'original.json'
    target.write_text('synthetic')
    path.symlink_to(target)
    assert mcs_setup.main(['init', '--yes', '--login-id', 'synthetic', '--notify-target', 'local']) == 0
    (backup,) = tmp_path.glob('c.json.corrupt-*')
    assert backup.is_symlink() and target.read_text() == 'synthetic'
    assert json.loads(path.read_text())['notify_target'] == 'local'
