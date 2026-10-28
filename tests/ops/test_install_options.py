"""Synthetic install option migration and legacy rollback safeguards."""
import json
from pathlib import Path
import subprocess
import sys

import config_edit
import install_options
import mcs_util
import pytest
import safe_io


def put(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def test_inspection_bootstraps_with_system_python(tmp_path):
    python = Path('/usr/bin/python3') if Path('/usr/bin/python3').exists() else Path(sys.executable)
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    script = Path(install_options.__file__)
    result = subprocess.run([str(python), str(script), 'inspect', str(config), str(record), '-', '--no-llm'],
                            text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'empty'


@pytest.mark.parametrize(('central', 'legacy', 'expected'), [
    ({}, None, None),
    ({'install_args': []}, None, []),
    ({}, {'install_args': ['--no-llm']}, ['--no-llm']),
    ({'install_args': []}, {'install_args': []}, []),
    ({'install_args': []}, {'install_args': ['--no-llm']}, None),
    ({'install_args': None}, {'install_args': ['--no-llm']}, None),
    ({'install_args': ['--no-services']}, None, None),
])
def test_recorded_distinguishes_missing_empty_invalid_and_conflict(tmp_path, central, legacy, expected):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, central)
    if legacy is not None:
        put(record, legacy)
    assert install_options.recorded(config, record) == expected


def test_recorded_does_not_fallback_from_unreadable_central(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    config.write_text('{broken')
    put(record, {'install_args': []})
    assert install_options.recorded(config, record) is None
    with pytest.raises(ValueError):
        install_options.inspect(config, record, [])


@pytest.mark.parametrize('body', [
    '{"v": 2, "install_args": []}',
    '{"v": true, "install_args": []}',
    '{"install_args": [], "install_args": ["--no-llm"]}',
    '{"install_args": [], "other": NaN}',
    '{"install_args": [], "other": 1e9999}',
])
def test_ambiguous_or_future_legacy_record_is_not_trusted(tmp_path, body):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'install_args': []})
    record.write_text(body)
    assert install_options.recorded(config, record) is None
    with pytest.raises(ValueError):
        install_options.inspect(config, record, [])
    assert record.read_text() == body


def test_save_updates_new_record_and_keeps_other_settings(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': {'new': 2}})
    base = install_options.inspect(config, record, ['--no-llm'])
    assert install_options.save(config, record, ['--no-llm'], base) == 0
    assert json.loads(config.read_text()) == {'other': {'new': 2}, 'install_args': ['--no-llm']}
    base = install_options.inspect(config, record, ['--no-llm'])
    before = (config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns)
    assert install_options.save(config, record, ['--no-llm'], base) == 0
    assert (config.read_bytes(), config.stat().st_ino, config.stat().st_mtime_ns) == before
    base = install_options.inspect(config, record, [])
    assert install_options.save(config, record, [], base) == 0
    assert json.loads(config.read_text()) == {'other': {'new': 2}, 'install_args': []}
    assert not record.exists()


def test_migrates_matching_legacy_without_changing_it(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    put(record, {'v': 1, 'install_args': ['--no-llm']})
    original = record.read_bytes()
    base = install_options.inspect(config, record, ['--no-llm'])
    assert install_options.save(config, record, ['--no-llm'], base) == 0
    assert record.read_bytes() == original
    assert json.loads(config.read_text()) == {'other': 1, 'install_args': ['--no-llm']}
    with pytest.raises(ValueError, match='legacy_rollback_conflict'):
        install_options.inspect(config, record, [])


def test_update_migrates_only_legacy_value_and_keeps_its_bytes(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    put(record, {'v': 1, 'install_args': ['--no-brew']})
    original = record.read_bytes()
    base = install_options.inspect(config, record)
    assert install_options.save(config, record, None, base) == 0
    assert json.loads(config.read_text()) == {'other': 1, 'install_args': ['--no-brew']}
    assert record.read_bytes() == original


def test_save_keeps_other_writer_value_but_rejects_changed_option(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    put(config, {'other': 2})
    assert install_options.save(config, record, [], base) == 0
    assert json.loads(config.read_text()) == {'other': 2, 'install_args': []}
    put(config, {'other': 3, 'install_args': ['--no-brew']})
    assert install_options.save(config, record, [], base) == 1
    assert json.loads(config.read_text()) == {'other': 3, 'install_args': ['--no-brew']}


def test_save_refuses_new_or_changed_legacy_after_inspection(tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    put(record, {'install_args': []})
    assert install_options.save(config, record, [], base) == 1
    assert json.loads(config.read_text()) == {'other': 1}
    assert json.loads(record.read_text()) == {'install_args': []}


def test_save_refuses_config_change_before_replace(monkeypatch, tmp_path):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    original = safe_io.atomic_write
    def raced(path, writer, *args, **kwargs):
        def stage(stream):
            put(config, {'other': 2})
            writer(stream)
        return original(path, stage, *args, **kwargs)
    monkeypatch.setattr(safe_io, 'atomic_write', raced)
    assert install_options.save(config, record, [], base) == 1
    assert json.loads(config.read_text()) == {'other': 2}


@pytest.mark.parametrize(('exception', 'status'), [(OSError, 1), (KeyboardInterrupt, 130)])
def test_save_reports_fsync_uncertainty(monkeypatch, tmp_path, capsys, exception, status):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    monkeypatch.setattr(safe_io, 'fsync_dir', lambda *_a, **_k: (_ for _ in ()).throw(exception('synthetic')))
    assert install_options.save(config, record, [], base) == status
    assert json.loads(config.read_text())['install_args'] == []
    assert '保存結果を確認できません' in capsys.readouterr().err


def test_lock_failure_does_not_claim_uncertain_save(tmp_path, capsys):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'other': 1})
    base = install_options.inspect(config, record, [])
    with config_edit._edit_lock(config):
        assert install_options.save(config, record, [], base) == 1
    assert json.loads(config.read_text()) == {'other': 1}
    assert '保存結果を確認できません' not in capsys.readouterr().err


def test_config_size_limit_before_save(tmp_path, monkeypatch):
    config, record = tmp_path / 'config.json', tmp_path / 'install_options.json'
    put(config, {'data': 'x' * 100})
    original = config.read_bytes()
    monkeypatch.setattr(mcs_util, 'CONFIG_MAX_BYTES', len(original) + 1)
    base = install_options.inspect(config, record, [])
    assert install_options.save(config, record, [], base) == 1
    assert config.read_bytes() == original


def test_symlink_legacy_does_not_fallback_or_get_overwritten(tmp_path):
    config, record, target = (tmp_path / n for n in ('config.json', 'install_options.json', 'target'))
    put(config, {'install_args': []})
    put(target, {'install_args': []})
    record.symlink_to(target)
    assert install_options.recorded(config, record) is None
    with pytest.raises(OSError):
        install_options.inspect(config, record, [])
    assert target.exists() and record.is_symlink()
