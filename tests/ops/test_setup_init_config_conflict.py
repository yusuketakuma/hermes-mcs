"""Synthetic init conflicts, including the side effects preceding publication."""
import json
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
