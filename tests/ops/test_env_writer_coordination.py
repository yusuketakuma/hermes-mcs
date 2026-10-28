"""Coordination tests use only synthetic credentials and temporary files."""
from pathlib import Path

import pytest

import api_key_setup
import lifecycle_io


def _env(root):
    path = root / '.env'
    path.write_text('MCS_PASSWORD=synthetic-old\nTYPESAFE_API_KEY=synthetic-api\n')
    path.chmod(0o600)
    return path


def test_api_snapshot_rejects_password_writer_completed_during_input(tmp_path):
    path = _env(tmp_path)
    _, before = api_key_setup._snapshot(path)
    lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
    with pytest.raises(ValueError, match='env_changed'):
        api_key_setup._save(path, {'TYPESAFE_API_KEY': 'synthetic-later'}, before)
    assert path.read_text() == 'MCS_PASSWORD=synthetic-new\nTYPESAFE_API_KEY=synthetic-api\n'


def test_password_write_preserves_unrelated_duplicate_and_unknown_lines(tmp_path):
    path = tmp_path / '.env'
    before = ('# comment=first\nTYPESAFE_API_KEY=synthetic-first\n'
              'unknown=one\n# comment=second\nTYPESAFE_API_KEY=synthetic-last\n'
              'unknown=two\nMCS_PASSWORD=synthetic-old\nMCS_PASSWORD=synthetic-shadow\n')
    path.write_text(before)
    path.chmod(0o600)
    lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
    assert path.read_text() == before.replace(
        'MCS_PASSWORD=synthetic-old\nMCS_PASSWORD=synthetic-shadow\n',
        'MCS_PASSWORD=synthetic-new\n')
    assert api_key_setup.mcs_util.private_env_problem('TYPESAFE_API_KEY', tmp_path) == 'key_duplicate'


def test_password_write_preserves_other_line_endings_and_final_line(tmp_path):
    path = tmp_path / '.env'
    path.write_bytes(b'# unknown=one\r\nMCS_PASSWORD=synthetic-old\r\nUNKNOWN=value')
    lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
    assert path.read_bytes() == b'# unknown=one\r\nMCS_PASSWORD=synthetic-new\r\nUNKNOWN=value'


def test_lifecycle_writer_rejects_shared_lock_contention_and_reentry(tmp_path):
    path = _env(tmp_path)
    before = path.read_bytes()
    with api_key_setup._env_edit_lock(path):
        with pytest.raises(BlockingIOError):
            lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
        with pytest.raises(BlockingIOError):
            with api_key_setup._env_edit_lock(path):
                pytest.fail('lock must not be reentrant')
    assert path.read_bytes() == before
    lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
    assert 'TYPESAFE_API_KEY=synthetic-api\n' in path.read_text()


def test_api_writer_cannot_replace_while_lifecycle_publication_is_in_progress(tmp_path, monkeypatch):
    path = _env(tmp_path)
    _, before = api_key_setup._snapshot(path)
    original = lifecycle_io.atomic_write
    attempted = []

    def attempt_while_locked(*args, **kwargs):
        with pytest.raises(BlockingIOError):
            api_key_setup._save(path, {'TYPESAFE_API_KEY': 'synthetic-later'}, before)
        attempted.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle_io, 'atomic_write', attempt_while_locked)
    lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
    assert attempted == [True]
    assert path.read_text() == 'MCS_PASSWORD=synthetic-new\nTYPESAFE_API_KEY=synthetic-api\n'


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'nonempty'])
def test_unsafe_lock_fails_closed_without_changing_credentials(tmp_path, kind):
    path = _env(tmp_path)
    before = path.read_bytes()
    lock = Path(str(path) + '.edit.lock')
    other = tmp_path / 'other'
    other.write_text('')
    other.chmod(0o600)
    if kind == 'symlink':
        lock.symlink_to(other)
    elif kind == 'hardlink':
        lock.hardlink_to(other)
    else:
        lock.write_text('synthetic')
        lock.chmod(0o600)
    with pytest.raises(OSError):
        lifecycle_io.write_env(str(path), {'MCS_PASSWORD': 'synthetic-new'})
    assert path.read_bytes() == before
