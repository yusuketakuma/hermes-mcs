"""Synthetic source metadata only; no source qualification or execution capability is issued."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest

import release_owner_source as source

ROOT = Path(source.__file__).resolve().parents[2]


def git(root, *args):
    return subprocess.check_output(['/usr/bin/git', '-C', str(root), *args], text=True)


@pytest.fixture
def repository(tmp_path):
    # A deliberately partial synthetic repository exercises metadata, never live factory acceptance.
    for name in (*source.REQUIRED, *source.SOURCE_FILES):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'add', '.')
    git(tmp_path, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic sources')
    return tmp_path


def manifest(root):
    return source.build_manifest(root, deadline=time.monotonic() + 10)


def test_complete_selected_paths_are_hashed_and_manifest_does_not_hash_itself(repository):
    value = manifest(repository)
    assert source.MANIFEST_PATH not in [row['path'] for row in value['files']]
    assert all(row['sha256'] == hashlib.sha256((repository / row['path']).read_bytes()).hexdigest() for row in value['files'])
    checked = source.verify_checkout(repository, value, deadline=time.monotonic() + 10, current=True)
    assert checked['sha256'] == source.digest(value) and checked['provider_profile'] == (1, 0, 0)
    (repository / source.MANIFEST_PATH).parent.mkdir(exist_ok=True)
    (repository / source.MANIFEST_PATH).write_text(json.dumps(value))
    assert manifest(repository) == value


@pytest.mark.parametrize('change', ['source', 'mode', 'omit', 'extra', 'untracked'])
def test_current_sources_cannot_change_or_narrow_the_reviewed_manifest(repository, change):
    value = manifest(repository)
    name = source.REQUIRED[0]
    if change == 'source':
        path = repository / name
        path.write_bytes(path.read_bytes() + b'\n# changed synthetic bytes\n')
    elif change == 'mode':
        (repository / name).chmod(0o700)
    elif change == 'omit':
        value['files'] = [v for v in value['files'] if v['path'] != name]
    elif change == 'extra':
        value['files'].append(dict(path='mcs/extra.py', mode='100644', sha256='a' * 64))
        value['files'].sort(key=lambda r: r['path'])
    else:
        (repository / 'mcs/untracked.py').write_text('raise RuntimeError("synthetic")')
    with pytest.raises(source.SourceError):
        source.verify_checkout(repository, value, deadline=time.monotonic() + 10, current=True)


@pytest.mark.parametrize('change', ['extra', 'bool_revision', 'bool_profile', 'large_profile', 'mode', 'unsafe_path', 'duplicate', 'negative_zero'])
def test_closed_manifest_metadata_rejects_ambiguous_or_unsupported_values(repository, change):
    value = manifest(repository)
    if change == 'extra':
        value['approved'] = True
    elif change == 'bool_revision':
        value['owner_revision'] = True
    elif change == 'bool_profile':
        value['provider_profile'][0] = True
    elif change == 'large_profile':
        value['provider_profile'][0] = 2**53
    elif change == 'mode':
        value['files'][0]['mode'] = '120000'
    elif change == 'unsafe_path':
        row = {**value['files'][0], 'path': value['files'][0]['path'].replace('sdc/', 'sdc/A/../', 1)}
        value['files'].append(row)
    elif change == 'duplicate':
        value = source.canonical(value).replace(b'{', b'{"owner_id":"mcs.updater",', 1)
    else:
        value = source.canonical(value).replace(b'[1,0,0]', b'[1,-0,0]', 1)
    with pytest.raises(source.SourceError):
        source.validate_manifest(value)


@pytest.mark.parametrize('replacement', ['(True, 0, 0)', '(1.0, 0, 0)', '(9, 0, 0)'])
def test_literal_declaration_cannot_inflate_or_coerce_the_verified_profile(repository, replacement):
    value = manifest(repository)
    raw = (repository / 'mcs/ops/release_owner_source.py').read_bytes().replace(b'PROVIDER_PROFILE = (1, 0, 0)', ('PROVIDER_PROFILE = ' + replacement).encode())
    with pytest.raises(source.SourceError):
        source.declarations(raw, value)


def test_staged_target_manifest_is_checked_without_running_candidate(repository):
    value = manifest(repository)
    path = repository / source.MANIFEST_PATH
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(source.canonical(value))
    files = {row['path']: dict(path=repository / row['path'], size=(repository / row['path']).stat().st_size,
        mode=row['mode']) for row in value['files']}
    files[source.MANIFEST_PATH] = dict(path=path, size=path.stat().st_size, mode='100644')
    checked = source.verify_target(files, deadline=time.monotonic() + 10)
    assert checked['sha256'] == source.digest(value)
    changed = copy.deepcopy(files)
    changed[source.REQUIRED[0]]['mode'] = '100755'
    with pytest.raises(source.SourceError):
        source.verify_target(changed, deadline=time.monotonic() + 10)


def test_development_writer_outputs_only_review_material_and_check_rejects_tampering(repository, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(repository / 'mcs'))
    monkeypatch.setattr(source, '__file__', str(repository / 'mcs/ops/release_owner_source.py'))
    assert source.main(['--write']) == 0
    saved = source.validate_manifest((repository / source.MANIFEST_PATH).read_bytes())
    assert capsys.readouterr().out.strip() == source.digest(saved)
    assert source.main(['--check']) == 0
    capsys.readouterr()
    target = repository / source.REQUIRED[0]
    target.write_bytes(target.read_bytes() + b'\n# synthetic changed source\n')
    assert source.main(['--check']) == 2
    assert capsys.readouterr().out.strip() == 'RELEASE_PROVIDER_SOURCE_HOLD'


def test_native_launcher_without_a_suffix_is_part_of_the_source_closure(repository):
    value = manifest(repository)
    assert 'scripts/mcs' in [row['path'] for row in value['files']]
    launcher = repository / 'scripts/mcs'
    launcher.write_bytes(launcher.read_bytes() + b'\n# synthetic launcher change\n')
    with pytest.raises(source.SourceError):
        source.verify_checkout(repository, value, deadline=time.monotonic() + 10, current=True)


@pytest.mark.parametrize('name', source.SOURCE_FILES)
def test_source_manifest_cannot_omit_a_mandatory_entry_file(repository, name):
    value = manifest(repository)
    value['files'] = [row for row in value['files'] if row['path'] != name]
    with pytest.raises(source.SourceError):
        source.validate_manifest(value)
