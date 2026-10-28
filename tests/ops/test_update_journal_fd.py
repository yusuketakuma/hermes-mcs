"""Synthetic tests for journal publication against a stable directory fd."""
import json
import os
import stat

import pytest
import update_journal


def test_journal_roundtrip_uses_a_private_file(tmp_path):
    path = tmp_path / 'data' / 'update_state.json'
    value = update_journal.default_state()
    update_journal.save_state(path, value)
    assert update_journal.load_state(path) == value
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize('moment', ['before_replace', 'during_replace'])
def test_replaced_directory_never_receives_the_old_journal(tmp_path, monkeypatch, moment):
    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'update_state.json'
    path.write_text('{"old": true}')
    moved = tmp_path / 'previous-data'
    original_fsync, original_replace = os.fsync, os.replace
    switched = []

    def swap():
        data.rename(moved)
        data.mkdir()
        path.write_text('{"new": true}')
        switched.append(True)

    def fsync(fd):
        result = original_fsync(fd)
        if moment == 'before_replace' and stat.S_ISREG(os.fstat(fd).st_mode) and not switched:
            swap()
        return result

    def replace(src, dst, **kwargs):
        if moment == 'during_replace':
            swap()
        return original_replace(src, dst, **kwargs)

    monkeypatch.setattr(update_journal.os, 'fsync', fsync)
    monkeypatch.setattr(update_journal.os, 'replace', replace)
    with pytest.raises(OSError, match='journal_directory_changed'):
        update_journal.save_state(path, update_journal.default_state())
    assert json.loads(path.read_text()) == {'new': True}
    old = json.loads((moved / path.name).read_text())
    assert old == ({'old': True} if moment == 'before_replace' else update_journal.default_state())
    assert not list(moved.glob('.update_state.*.tmp'))


def test_directory_fsync_failure_can_follow_publication(tmp_path, monkeypatch):
    path = tmp_path / 'update_state.json'
    path.write_text('{"before": true}')
    original = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('synthetic directory fsync failure')
        return original(fd)

    monkeypatch.setattr(update_journal.os, 'fsync', fsync)
    value = update_journal.default_state()
    with pytest.raises(OSError, match='synthetic directory fsync failure'):
        update_journal.save_state(path, value)
    assert json.loads(path.read_text()) == value


def test_changed_temporary_file_is_preserved_and_not_published(tmp_path, monkeypatch):
    path = tmp_path / 'update_state.json'
    path.write_text('{"before": true}')
    original = os.fsync
    changed = []

    def fsync(fd):
        original(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode) and not changed:
            tmp, = tmp_path.glob('.update_state.*.tmp')
            tmp.rename(tmp_path / 'original-synthetic-temp')
            tmp.write_text('foreign synthetic file')
            changed.append(tmp)

    monkeypatch.setattr(update_journal.os, 'fsync', fsync)
    with pytest.raises(OSError, match='journal_temporary_changed'):
        update_journal.save_state(path, update_journal.default_state())
    assert path.read_text() == '{"before": true}'
    assert changed[0].read_text() == 'foreign synthetic file'


def test_existing_symlinked_directory_remains_usable(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    link = tmp_path / 'data'
    link.symlink_to(actual, target_is_directory=True)
    update_journal.save_state(link / 'update_state.json', update_journal.default_state())
    assert json.loads((actual / 'update_state.json').read_text()) == update_journal.default_state()
