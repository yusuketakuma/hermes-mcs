"""Backup failures must release source connections and their private temp files."""
from pathlib import Path

import pytest

import maintenance


@pytest.mark.parametrize('operation', ['daily_backup', 'preupdate_backup'])
@pytest.mark.parametrize('failure', ['destination_open', 'destination_close'])
def test_backup_failure_always_closes_source(tmp_path, monkeypatch, operation, failure):
    source = tmp_path / 'synthetic.db'
    source.write_bytes(b'synthetic')
    backup_dir = tmp_path / 'backups'
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(backup_dir))
    monkeypatch.setattr(maintenance, 'valid_mcs_db', lambda path: False)
    monkeypatch.setattr(maintenance, '_verified_unchanged', lambda path: False)
    monkeypatch.setattr(maintenance, 'disk_floor_mb', lambda: 0)
    closed = []

    class Source:
        def backup(self, destination):
            pass

        def close(self):
            closed.append('source')

    class Destination:
        def execute(self, sql):
            pass

        def close(self):
            raise OSError('synthetic_close_failure')

    def connect(path, **kwargs):
        if kwargs.get('uri'):
            return Source()
        Path(path).write_bytes(b'synthetic')
        if failure == 'destination_open':
            raise OSError('synthetic_open_failure')
        return Destination()

    monkeypatch.setattr(maintenance.sqlite3, 'connect', connect)
    with pytest.raises(OSError):
        getattr(maintenance, operation)(str(source))
    assert closed == ['source']
    assert list(backup_dir.glob('*.tmp')) == []


def test_preupdate_source_open_failure_removes_its_new_temp_file(tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backups'
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(backup_dir))

    def unavailable(*args, **kwargs):
        raise OSError('synthetic_source_failure')

    monkeypatch.setattr(maintenance.sqlite3, 'connect', unavailable)
    with pytest.raises(OSError):
        maintenance.preupdate_backup(str(tmp_path / 'synthetic.db'))
    assert list(backup_dir.glob('*.tmp')) == []


@pytest.mark.parametrize('operation', ['daily_backup', 'preupdate_backup'])
@pytest.mark.parametrize('error', [OSError, KeyboardInterrupt])
@pytest.mark.parametrize('published', [False, True])
def test_backup_publication_failure_cleans_only_staging(tmp_path, monkeypatch, operation, error, published):
    import ledger
    source = tmp_path / 'synthetic.db'
    writer = ledger.Ledger(str(source))
    writer.ensure_patient(1)
    writer.close()
    backup_dir = tmp_path / 'backups'
    backup_dir.mkdir()
    existing = backup_dir / 'ledger-prior.db'
    existing.write_bytes(b'synthetic-prior')
    monkeypatch.setattr(maintenance, 'BACKUP_DIR', str(backup_dir))
    real_publish = maintenance.publish_tmp
    targets = []

    def fail_publication(tmp, dest, mode=None):
        targets.append(Path(dest))
        if published:
            real_publish(tmp, dest, mode=mode)
        raise error('synthetic publication failure')

    monkeypatch.setattr(maintenance, 'publish_tmp', fail_publication)
    with pytest.raises(error):
        getattr(maintenance, operation)(str(source))
    assert list(backup_dir.glob('*.tmp')) == []
    assert existing.read_bytes() == b'synthetic-prior'
    assert targets and (targets[0].is_file() is published)
    if published:
        assert ledger.valid_mcs_db(str(targets[0]))
