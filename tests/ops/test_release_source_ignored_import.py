"""Synthetic repositories distinguish importable ignored code from private stores."""
from pathlib import Path
import shutil
import subprocess
import time

import pytest

import release_owner_source as source

ROOT = Path(source.__file__).resolve().parents[2]


def _git(repository, *args):
    return subprocess.check_output(['/usr/bin/git', '-C', str(repository), *args], text=True)


@pytest.fixture
def repository(tmp_path):
    for name in (*source.REQUIRED, *source.SOURCE_FILES):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (tmp_path / '.gitignore').write_text('venv/\ndata/\n*.pyc\n', encoding='ascii')
    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
         'commit', '-qm', 'synthetic sources')
    return tmp_path


@pytest.mark.parametrize('name', [
    'mcs/venv/ignored.py', 'mcs/data/ignored.py', 'mcs/ops/ignored.py',
    'mcs/ops/ignored.pyc', 'mcs/ignored.pyc',
])
def test_source_verification_rejects_ignored_importable_python(repository, name):
    manifest = source.build_manifest(repository, deadline=time.monotonic() + 10)
    if name == 'mcs/ops/ignored.py':
        (repository / '.git/info/exclude').write_text('/mcs/ops/ignored.py\n', encoding='ascii')
    target = repository / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'# synthetic source, never executed\n')
    assert _git(repository, 'check-ignore', name).strip() == name
    with pytest.raises(source.SourceError):
        source.verify_checkout(repository, manifest, deadline=time.monotonic() + 10, current=True)


@pytest.mark.parametrize('name', [
    'venv/ignored.py', 'data/ignored.py', 'mcs/.private/data/ignored.py',
    'mcs/_private/data/ignored.py', 'mcs/extract/v2/data/ignored.py',
    'mcs/extract/v3/data/ignored.py', 'mcs/venv/ignored.pyc',
    'mcs/ops/__pycache__/ignored.pyc',
])
def test_private_or_bootstrap_excluded_python_does_not_block_sources(repository, name):
    manifest = source.build_manifest(repository, deadline=time.monotonic() + 10)
    target = repository / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'# synthetic private store, never executed\n')
    assert _git(repository, 'check-ignore', name).strip() == name
    result = source.verify_checkout(repository, manifest, deadline=time.monotonic() + 10, current=True)
    assert result['sha256'] == source.digest(manifest)
