"""完全合成wheelだけでlock・offline解決・導入記録の境界を検査する。"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import zipfile

import pytest

from mcs.ops import release_dependency_lock as lock


def wheel(name='synthetic-demo', version='1.0', *, requirements=(), entries=()):
    output = io.BytesIO()
    directory = name.replace('-', '_') + '-' + version + '.dist-info'
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(directory + '/METADATA', 'Metadata-Version: 2.1\nName: ' + name
                         + '\nVersion: ' + version + '\n' + ''.join('Requires-Dist: ' + x + '\n' for x in requirements))
        archive.writestr(directory + '/WHEEL', 'Wheel-Version: 1.0\nGenerator: synthetic\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr(directory + '/RECORD', '')
        archive.writestr(name.replace('-', '_') + '.py', 'raise RuntimeError("SYNTHETIC_SDK_MUST_NOT_IMPORT")\n')
        for entry, content in entries:
            archive.writestr(entry, content)
    return output.getvalue()


@pytest.fixture
def product(tmp_path):
    repo = tmp_path / 'repo'
    wheels = repo / lock.WHEELS_PATH
    wheels.mkdir(parents=True)
    (repo / 'deployment/requirements-standalone.txt').write_text('synthetic-demo==1.0\n')
    (repo / 'install.sh').write_text('HERMES_PIN="' + 'a' * 40 + '"\n')
    raw = wheel()
    filename = 'synthetic_demo-1.0-py3-none-any.whl'
    (wheels / filename).write_bytes(raw)
    platform = {'os': 'macos' if sys.platform == 'darwin' else 'linux',
                'cpu': 'arm64', 'python': list(sys.version_info[:2])}
    specification = {'schema_version': lock.SCHEMA, 'product': 'hermes-mcs', 'core_kind': 'stdlib-only',
                     'profiles': [{'profile_id': 'synthetic', 'runtime_mode': 'standalone', 'platform': platform,
                                   'requirements_sha256': '0' * 64, 'host': None, 'build_packages': [],
                                   'packages': [{'name': 'synthetic-demo', 'version': '1.0',
                                                 'wheels': [{'filename': filename}]}]}]}
    value = lock.build_lock(specification, repo)
    (repo / lock.LOCK_PATH).write_bytes(lock.encoded(value))
    return repo, value, platform


def test_canonical_identity_profile_and_generator(product):
    repo, value, platform = product
    raw = (repo / lock.LOCK_PATH).read_bytes()
    assert lock.read_lock(repo) == value
    assert lock.digest(value) == hashlib.sha256(raw).hexdigest()
    profile = lock.select(raw, 'standalone', platform)
    assert profile == value['profiles'][0]
    assert set(lock.verify_wheels(profile, repo / lock.WHEELS_PATH)) == {'synthetic_demo-1.0-py3-none-any.whl'}
    with pytest.raises(lock.LockError, match='PROFILE_UNSUPPORTED'):
        lock.select(raw, 'hermes', platform)


@pytest.mark.parametrize('mutate', [
    lambda x: x.update(secret='SYNTHETIC_CANARY'),
    lambda x: x['profiles'][0].update(unknown=True),
    lambda x: x['profiles'].append(copy.deepcopy(x['profiles'][0])),
    lambda x: x['profiles'][0]['packages'][0].update(name='Synthetic_Demo'),
    lambda x: x['profiles'][0]['packages'][0]['wheels'][0].update(filename='../escape.whl'),
    lambda x: x['profiles'][0]['packages'][0]['wheels'][0].update(size=True),
    lambda x: x['profiles'][0]['packages'][0]['wheels'][0].update(size=2**53),
    lambda x: x['profiles'][0]['platform'].update(python=[3, True]),
    lambda x: x['profiles'][0].update(host={}),
    lambda x: x['profiles'][0].update(build_packages=x['profiles'][0]['packages']),
])
def test_closed_and_bounded_records(product, mutate):
    value = copy.deepcopy(product[1])
    mutate(value)
    with pytest.raises(ValueError) as error:
        lock.validate(value)
    assert 'SYNTHETIC_CANARY' not in str(error.value)


@pytest.mark.parametrize('transform', [
    lambda raw: b' ' + raw,
    lambda raw: raw + b'\n',
    lambda raw: b'\xef\xbb\xbf' + raw,
    lambda raw: raw.replace(b'"product":', b'"product":"hermes-mcs","product":'),
    lambda raw: raw.replace(b'"size":', b'"size":1.0,"size":'),
    lambda raw: b' ' * (lock.MAX_LOCK + 1),
])
def test_noncanonical_input_rejected(product, transform):
    with pytest.raises(ValueError):
        lock.validate(transform(lock.encoded(product[1])))


@pytest.mark.parametrize('entries', [
    [('../escape', 'x')], [('/absolute', 'x')], [('a\\b', 'x')],
    [('a/./b', 'x')], [('a//b', 'x')], [('A', 'x'), ('a', 'y')],
    [('Pkg/a', 'x'), ('pkg/b', 'y')], [('file', 'x'), ('file/child', 'y')],
    [('control\nname', 'x')],
    [('other-1.dist-info/METADATA', 'Name: other\nVersion: 1\n')],
])
def test_unsafe_wheel_entries_rejected(entries):
    with pytest.raises(ValueError):
        lock.wheel_metadata(wheel(entries=entries))


def test_links_metadata_mismatch_and_url_dependency_rejected():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        entry = zipfile.ZipInfo('symlink')
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, '../escape')
    with pytest.raises(ValueError):
        lock.wheel_metadata(output.getvalue())
    with pytest.raises(ValueError):
        lock.wheel_metadata(wheel(), expected={'name': 'other', 'version': '1.0'})
    with pytest.raises(ValueError):
        lock.wheel_metadata(wheel(requirements=['other @ file:///SYNTHETIC_PRIVATE']))


def test_count_and_zip_expansion_limits(product, monkeypatch):
    value = copy.deepcopy(product[1])
    value['profiles'] *= 17
    with pytest.raises(ValueError):
        lock.validate(value)
    value = copy.deepcopy(product[1])
    value['profiles'][0]['packages'] *= 257
    with pytest.raises(ValueError):
        lock.validate(value)
    value = copy.deepcopy(product[1])
    value['profiles'][0]['packages'][0]['wheels'] *= 9
    with pytest.raises(ValueError):
        lock.validate(value)
    monkeypatch.setattr(lock, 'MAX_EXPANDED', 10)
    with pytest.raises(ValueError):
        lock.wheel_metadata(wheel())


def test_one_wheel_filename_cannot_claim_two_package_identities(product):
    value = copy.deepcopy(product[1])
    alias = copy.deepcopy(value['profiles'][0]['packages'][0])
    alias.update(name='z-synthetic-alias', version='2.0')
    value['profiles'][0]['packages'].append(alias)
    with pytest.raises(ValueError):
        lock.validate(value)


def test_wheel_hash_mismatch_and_symlink(product, tmp_path):
    repo, value, _ = product
    profile = value['profiles'][0]
    path = repo / lock.WHEELS_PATH / profile['packages'][0]['wheels'][0]['filename']
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError):
        lock.verify_wheels(profile, path.parent)
    path.unlink()
    path.symlink_to(tmp_path / 'missing')
    with pytest.raises(OSError):
        lock.verify_wheels(profile, path.parent)


def test_literal_pin_and_unsupported_host_guards():
    assert lock.hermes_pin(b'HERMES_PIN="' + b'a' * 40 + b'"\n') == 'a' * 40
    for raw in (b'HERMES_PIN="$(touch NEVER)"', b'HERMES_PIN="' + b'a' * 40 + b'"\nHERMES_PIN=x'):
        with pytest.raises(ValueError):
            lock.hermes_pin(raw)
    lock._python_requirement('>=3.14,<3.15', [3, 14])
    assert lock._environment("python_version >= '3.14'", [3, 14])
    with pytest.raises(ValueError):
        lock._python_requirement('>=3.14,<3.15', [3, 13])
    with pytest.raises(ValueError):
        lock._environment("sys_platform == 'darwin'", [3, 14])


def test_generator_cli_writes_canonical_bytes(product, tmp_path):
    repo, value, _ = product
    specification = tmp_path / 'spec.json'
    specification.write_bytes(lock.encoded(value))
    output = tmp_path / 'generated.json'
    script = Path(__file__).resolve().parents[2] / 'scripts/development/build_release_lock.py'
    result = subprocess.run([sys.executable, str(script), '--spec', str(specification), '--repo', str(repo),
                             '--output', str(output)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == lock.encoded(value)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def runtime(tmp_path):
    venv = tmp_path / 'synthetic-venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True,
                   capture_output=True, timeout=30)
    return venv, venv / 'bin/python'


def test_offline_install_consumes_hash_input_and_metadata_without_sdk_import(product, tmp_path):
    repo, value, platform = product
    venv, python = runtime(tmp_path)
    site = venv / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
    sentinel = tmp_path / 'pth-executed'
    (site / 'synthetic.pth').write_text('import pathlib; pathlib.Path(' + repr(str(sentinel)) + ').write_text("executed")\n')
    receipt = lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    assert receipt['phase'] == 'completed'
    assert receipt['packages'][0]['wheel_sha256'] == value['profiles'][0]['packages'][0]['wheels'][0]['sha256']
    result = lock.observe_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    assert result['lock_sha256'] == lock.digest(value)
    assert result['runtime_fingerprint'] == receipt['runtime_fingerprint']
    assert stat.S_IMODE((venv / lock.RECEIPT_NAME).stat().st_mode) == 0o600
    assert not sentinel.exists()
    # Same profile under a different whole-lock hash is a read-only observation.
    other = copy.deepcopy(value['profiles'][0])
    other['profile_id'] = 'zz-other'
    other['platform']['cpu'] = 'other'
    value['profiles'].append(other)
    (repo / lock.LOCK_PATH).write_bytes(lock.encoded(value))
    before = (venv / lock.RECEIPT_NAME).read_bytes()
    assert lock.observe_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)['lock_sha256'] == lock.digest(value)
    assert (venv / lock.RECEIPT_NAME).read_bytes() == before


def test_observation_deadline_expires_before_any_probe(product, tmp_path, monkeypatch):
    monkeypatch.setattr(lock, '_runtime', lambda *_a, **_k: pytest.fail('probe after deadline'))
    with pytest.raises(lock.LockError, match='TIMEOUT'):
        lock.observe_locked(product[0], python=tmp_path / 'absent', venv=tmp_path,
                            runtime_mode='standalone', platform=product[2], deadline=time.monotonic() - 1)


def test_runtime_probe_rejects_interpreter_replacement(tmp_path, monkeypatch):
    venv = tmp_path / 'venv'
    (venv / 'bin').mkdir(parents=True)
    python = venv / 'bin/python'
    python.write_text('synthetic first interpreter')
    def probe(argv, **kwargs):
        assert '-B' in argv
        replacement = python.with_name('replacement')
        replacement.write_text('synthetic other interpreter')
        replacement.replace(python)
        return json.dumps({'python': [3, 14], 'executable': str(python)}).encode()
    monkeypatch.setattr(lock, '_run', probe)
    with pytest.raises(lock.LockError, match='RUNTIME_MISMATCH'):
        lock._runtime(python, venv, deadline=time.monotonic() + 10, cwd=tmp_path)


def test_hermes_source_observation_receives_the_same_absolute_deadline(product, tmp_path, monkeypatch):
    repo, value, _ = product
    profile = copy.deepcopy(value['profiles'][0])
    profile.update(runtime_mode='hermes', host={'commit': 'a' * 40})
    deadline = time.monotonic() + 0.1
    seen = []
    def git_observation(path, **kwargs):
        seen.append((path, kwargs['deadline']))
        raise lock.LockError('TIMEOUT')
    monkeypatch.setattr(lock, '_git_observation', git_observation)
    with pytest.raises(lock.LockError, match='TIMEOUT'):
        lock.source_requirements(profile, repo, tmp_path, deadline=deadline)
    assert seen == [(tmp_path, deadline)]


def test_resolver_missing_closure_stops_before_pending(product, tmp_path):
    repo, value, platform = product
    raw = wheel(requirements=['synthetic-missing==1'])
    package = value['profiles'][0]['packages'][0]
    path = repo / lock.WHEELS_PATH / package['wheels'][0]['filename']
    path.write_bytes(raw)
    package['wheels'][0].update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
    (repo / lock.LOCK_PATH).write_bytes(lock.encoded(value))
    venv, python = runtime(tmp_path)
    with pytest.raises(ValueError):
        lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    assert not (venv / lock.RECEIPT_NAME).exists()


def test_pending_precedes_mutation_and_uncertain_save_never_starts_manager(product, tmp_path, monkeypatch):
    repo, _, platform = product
    venv, python = runtime(tmp_path)
    original = lock._run
    called = []
    def run(argv, **kwargs):
        if argv[1:3] == ['pip', 'install']:
            called.append(argv)
            assert lock._read_receipt(venv)['phase'] == 'pending'
            raise TimeoutError('synthetic')
        return original(argv, **kwargs)
    monkeypatch.setattr(lock, '_run', run)
    with pytest.raises(TimeoutError):
        lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    assert len(called) == 1 and lock._read_receipt(venv)['phase'] == 'pending'
    called.clear()
    monkeypatch.setattr(lock, '_write_receipt', lambda *_: (_ for _ in ()).throw(OSError('fsync uncertain')))
    with pytest.raises(OSError):
        lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    assert called == []


def test_corrupt_receipt_and_absent_uv_do_not_fallback(product, tmp_path, monkeypatch):
    repo, _, platform = product
    venv, python = runtime(tmp_path)
    path = venv / lock.RECEIPT_NAME
    path.write_text('{}')
    path.chmod(0o600)
    with pytest.raises(ValueError):
        lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    assert path.read_text() == '{}'
    monkeypatch.setattr(lock.shutil, 'which', lambda _: None)
    with pytest.raises(ValueError, match='MANAGER_UNAVAILABLE'):
        lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)


@pytest.mark.parametrize('name', ['extra-pkg', 'pip'])
def test_existing_extraneous_distribution_stops_before_pending_or_install(product, tmp_path, monkeypatch, name):
    repo, _, platform = product
    venv, python = runtime(tmp_path)
    site = venv / 'lib/python3.14/site-packages'
    extra = site / (name.replace('-', '_') + '-1.0.dist-info')
    extra.mkdir()
    (extra / 'METADATA').write_text('Metadata-Version: 2.1\nName: ' + name + '\nVersion: 1.0\n')
    original = lock._run
    def only_metadata(argv, **kwargs):
        assert argv[1:3] != ['pip', 'install']
        assert argv[1:3] != ['pip', 'compile']
        return original(argv, **kwargs)
    monkeypatch.setattr(lock, '_run', only_metadata)
    for _ in range(2):
        with pytest.raises(ValueError, match='EXTRANEOUS_DISTRIBUTION'):
            lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
        assert not (venv / lock.RECEIPT_NAME).exists()
        assert not (site / 'synthetic_demo.py').exists()
        assert (extra / 'METADATA').exists()


def test_cli_extraneous_distribution_guides_new_environment_without_changing_existing(product, monkeypatch, capsys):
    repo, _, _ = product
    def stop(*args, **kwargs):
        raise lock.LockError('EXTRANEOUS_DISTRIBUTION')
    monkeypatch.setattr(lock, 'install_locked', stop)
    assert lock.main(['--repo', str(repo), '--python', '/synthetic/venv/bin/python',
                      '--venv', '/synthetic/venv', '--mode', 'standalone']) == 1
    message = capsys.readouterr().err
    assert '変更前' in message and '現在の環境を残したまま' in message and '新しいPython環境' in message


def test_owned_child_timeout_stops_before_return(tmp_path):
    pidfile = tmp_path / 'pid'
    script = 'import os,time,pathlib;pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()));time.sleep(30)'
    with pytest.raises(subprocess.TimeoutExpired):
        lock._run([sys.executable, '-I', '-S', '-c', script], cwd=tmp_path, deadline=time.monotonic() + 0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_hermes_override_build_pins_and_keep_checkout_boundary(product, tmp_path):
    repo, value, platform = product
    host = tmp_path / 'synthetic-host'
    host.mkdir()
    pyproject = '''[project]
name = "synthetic-host"
version = "1.0"
requires-python = ">=3.13,<3.15"
dependencies = ["synthetic-demo<2"]
[project.optional-dependencies]
messaging = []
[build-system]
requires = ["synthetic-builder==1.0"]
build-backend = "setuptools.build_meta"
[tool.uv]
environments = ["python_version >= '3.13'"]
override-dependencies = ["synthetic-demo==2.0"]
'''
    (host / 'pyproject.toml').write_text(pyproject)
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(host), *args], env=env, stderr=subprocess.DEVNULL)
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic')
    commit = git('rev-parse', 'HEAD').decode().strip()
    (repo / 'install.sh').write_text('HERMES_PIN="' + commit + '"\n')
    profile = value['profiles'][0]
    profile.update(runtime_mode='hermes', host={'commit': commit,
                   'pyproject_sha256': hashlib.sha256(pyproject.encode()).hexdigest(),
                   'extras': ['messaging'], 'project_name': 'synthetic-host'})
    profile['requirements_sha256'] = profile['host']['pyproject_sha256']
    def add_package(name, version):
        raw = wheel(name, version)
        filename = name.replace('-', '_') + '-' + version + '-py3-none-any.whl'
        (repo / lock.WHEELS_PATH / filename).write_bytes(raw)
        return {'name': name, 'version': version, 'wheels': [
            {'filename': filename, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}]}
    profile['packages'] = [add_package('synthetic-demo', '2.0')]
    profile['build_packages'] = [add_package('synthetic-builder', '1.0')]
    (repo / lock.LOCK_PATH).write_bytes(lock.encoded(value))
    index = host / '.git/index'
    index_before = (index.stat().st_mtime_ns, index.read_bytes())
    pyproject_path = host / 'pyproject.toml'
    info = pyproject_path.stat()
    os.utime(pyproject_path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    source, overrides, build = lock.source_requirements(profile, repo, host)
    assert (index.stat().st_mtime_ns, index.read_bytes()) == index_before
    venv, python = runtime(tmp_path)
    workspace = tmp_path / 'compile'
    workspace.mkdir()
    (workspace / 'wheels').mkdir()
    for filename, raw in lock.verify_wheels(profile, repo / lock.WHEELS_PATH).items():
        (workspace / 'wheels' / filename).write_bytes(raw)
    uv = lock.shutil.which('uv')
    assert uv
    selected = lock._compile(uv, source, overrides, profile['packages'], workspace, python,
                             deadline=time.monotonic() + 20, label='runtime')
    assert selected['synthetic-demo']['sha256'] == profile['packages'][0]['wheels'][0]['sha256']
    assert set(lock._compile(uv, build, '', profile['build_packages'], workspace, python,
                             deadline=time.monotonic() + 20, label='build')) == {'synthetic-builder'}
    with pytest.raises(ValueError):
        lock._compile(uv, source, '', profile['packages'], workspace, python,
                      deadline=time.monotonic() + 20, label='without-overrides')
    # A retained different clean checkout never becomes a locked baseline.
    original = (host / 'pyproject.toml').read_bytes()
    (repo / 'install.sh').write_text('HERMES_PIN="' + 'a' * 40 + '"\n')
    with pytest.raises(ValueError, match='HOST_MISMATCH'):
        lock.source_requirements(profile, repo, host)
    assert (host / 'pyproject.toml').read_bytes() == original


def test_same_version_other_wheel_profile_and_metadata_change_are_unknown(product, tmp_path):
    repo, value, platform = product
    venv, python = runtime(tmp_path)
    lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    profile = value['profiles'][0]
    profile['packages'][0]['wheels'][0]['sha256'] = 'e' * 64
    (repo / lock.LOCK_PATH).write_bytes(lock.encoded(value))
    with pytest.raises(ValueError, match='UNKNOWN_BASELINE'):
        lock.observe_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    # Owner control records are outside this venv; observation never edits or resets them.
    owner = tmp_path / 'private-owner.json'
    owner.write_text('{"synthetic_floor":99}')
    receipt = json.loads((venv / lock.RECEIPT_NAME).read_text())
    receipt['profile_sha256'] = lock.digest(profile)
    (venv / lock.RECEIPT_NAME).write_bytes(lock.encoded(receipt))
    metadata = next((venv / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages').glob('*.dist-info/METADATA'))
    metadata.write_text(metadata.read_text().replace('Version: 1.0', 'Version: 1.1'))
    with pytest.raises(ValueError, match='METADATA_MISMATCH'):
        lock.observe_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    assert owner.read_text() == '{"synthetic_floor":99}'


def test_after_manager_metadata_failure_keeps_pending(product, tmp_path, monkeypatch):
    repo, _, platform = product
    venv, python = runtime(tmp_path)
    original = lock._run
    def manager_did_nothing(argv, **kwargs):
        return b'' if argv[1:3] == ['pip', 'install'] else original(argv, **kwargs)
    monkeypatch.setattr(lock, '_run', manager_did_nothing)
    with pytest.raises(ValueError, match='METADATA_MISMATCH'):
        lock.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    assert lock._read_receipt(venv)['phase'] == 'pending'


def test_read_receipt_refuses_symlinks_hardlinks_and_wrong_permissions(product, tmp_path):
    venv, _ = runtime(tmp_path)
    other = tmp_path / 'other.json'
    other.write_text('{}')
    other.chmod(0o600)
    path = venv / lock.RECEIPT_NAME
    path.symlink_to(other)
    with pytest.raises(OSError):
        lock._read_receipt(venv)
    path.unlink()
    os.link(other, path)
    with pytest.raises(ValueError):
        lock._read_receipt(venv)
    path.unlink()
    path.write_text('{}')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        lock._read_receipt(venv)


def test_installer_invalid_lock_stops_before_runtime_or_sdk_mutation(tmp_path):
    from test_install_sh import _calls, _run, _world
    root = Path(__file__).resolve().parents[2]
    home, _, stubs, env = _world(tmp_path / 'world')
    repo = tmp_path / 'repo'
    (repo / 'deployment').mkdir(parents=True)
    (repo / 'deployment/sdc-release-lock.json').write_text('{}')
    (repo / 'install.sh').write_bytes((root / 'install.sh').read_bytes())
    for name in ('mcs', 'adapters'):
        (repo / name).symlink_to(root / name, target_is_directory=True)
    python_stub = stubs / 'bin/python3'
    original = python_stub.read_text()
    # Execute only the new read-only helper; all installer effects retain existing stubs.
    marker = 'case "$*" in\n'
    python_stub.write_text(original.replace(marker, marker +
                           '  *release_dependency_lock*) exec "$STUB_PARSE_PYTHON" "$@" ;;\n', 1))
    result = _run(env, None, '--mode', 'standalone', '--no-brew', '--no-services',
                  '--no-recovery', '--no-llm', '--no-plugin', script=repo / 'install.sh')
    assert result.returncode != 0
    assert '固定依存の入力を確認できません' in result.stderr
    assert not (home / '.mcs/venv').exists()
    assert not any(call.startswith(('uv venv', 'uv pip install')) for call in _calls(stubs))


def test_runner_separates_only_uv_and_keeps_home_secret_and_hermes_guards(tmp_path):
    root = Path(__file__).resolve().parents[2]
    tools = tmp_path / 'shared-bin'
    tools.mkdir()
    for name, text in (('uv', 'printf synthetic-uv'), ('hermes', 'exit 88')):
        path = tools / name
        path.write_text('#!/bin/sh\n' + text + '\n')
        path.chmod(0o755)
    fake_home = tmp_path / 'original-home'
    fake_home.mkdir()
    (fake_home / '.synthetic-credential').write_text('synthetic-only')
    test = tmp_path / 'test_tool_isolation.py'
    test.write_text('''import os,runpy,shutil,subprocess,socket
from pathlib import Path
runpy.run_path(''' + repr(str(root / 'tests/conftest.py')) + ''')
def test_boundaries():
 assert 'SYNTHETIC_SECRET' not in os.environ
 assert not (Path.home()/'.synthetic-credential').exists()
 assert socket.socket.connect.__name__ == '_blocked'
 uv=Path(shutil.which('uv'))
 assert list(p.name for p in uv.parent.iterdir()) == ['uv']
 assert subprocess.check_output(['uv','--version']).decode() == 'synthetic-uv'
 result=subprocess.run(['hermes'],capture_output=True)
 assert result.returncode == 97
''')
    env = {'HOME': str(fake_home), 'PATH': str(tools) + ':/usr/bin:/bin',
           'MCS_TEST_PYTHON': sys.executable, 'SYNTHETIC_SECRET': 'synthetic-only'}
    result = subprocess.run(['/bin/sh', str(root / 'scripts/run_tests.sh'), '-q', '-p', 'conftest',
                             '-p', 'no:cacheprovider', '-c', str(root / 'pyproject.toml'),
                             '--confcutdir', str(tmp_path), str(test)], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_synthetic_editable_backend_uses_fixed_build_wheel_without_pth(product, tmp_path):
    repo, value, platform = product
    host = tmp_path / 'synthetic-editable-host'
    host.mkdir()
    (host / 'synthetic_host.py').write_text('def main():\n print(__file__)\n')
    (host / 'pyproject.toml').write_text('''[project]
name="synthetic-host"
version="1.0"
requires-python=">=3.14,<3.15"
dependencies=["synthetic-demo==1.0"]
[project.optional-dependencies]
messaging=[]
[build-system]
requires=["synthetic-backend==1.0"]
build-backend="setuptools.build_meta"
''')
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(host), *args], stderr=subprocess.DEVNULL,
                                       env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull})
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic')
    commit = git('rev-parse', 'HEAD').decode().strip()
    (repo / 'install.sh').write_text('HERMES_PIN="' + commit + '"\n')
    # Minimal PEP660 fixture; executes only after the durable pending receipt.
    backend = '''import pathlib,zipfile
metadata="Metadata-Version: 2.1\\nName: synthetic-host\\nVersion: 1.0\\nProvides-Extra: messaging\\n"
wheel="Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
def get_requires_for_build_editable(config_settings=None): return []
def prepare_metadata_for_build_editable(metadata_directory,config_settings=None):
 path=pathlib.Path(metadata_directory)/'synthetic_host-1.0.dist-info'
 path.mkdir()
 (path/'METADATA').write_text(metadata)
 return path.name
def build_editable(wheel_directory,config_settings=None,metadata_directory=None):
 filename='synthetic_host-1.0-py3-none-any.whl'
 with zipfile.ZipFile(pathlib.Path(wheel_directory)/filename,'w') as z:
  z.writestr('synthetic_host-1.0.dist-info/METADATA',metadata)
  z.writestr('synthetic_host-1.0.dist-info/WHEEL',wheel)
  z.writestr('synthetic_host.pth',str(pathlib.Path.cwd())+'\\n')
  z.writestr('synthetic_host-1.0.dist-info/entry_points.txt','[console_scripts]\\nsynthetic-host = synthetic_host:main\\n')
  z.writestr('synthetic_host-1.0.dist-info/RECORD','')
 return filename
'''
    backend = ('import json,pathlib\nassert json.loads(pathlib.Path('
               + repr(str(tmp_path / 'synthetic-venv' / lock.RECEIPT_NAME))
               + ').read_text())["phase"] == "pending"\n') + backend
    raw = wheel('synthetic-backend', entries=[('setuptools/__init__.py', ''), ('setuptools/build_meta.py', backend)])
    filename = 'synthetic_backend-1.0-py3-none-any.whl'
    (repo / lock.WHEELS_PATH / filename).write_bytes(raw)
    profile = value['profiles'][0]
    digest = hashlib.sha256((host / 'pyproject.toml').read_bytes()).hexdigest()
    profile.update(runtime_mode='hermes', requirements_sha256=digest,
                   host={'commit': commit, 'pyproject_sha256': digest, 'extras': ['messaging'], 'project_name': 'synthetic-host'},
                   build_packages=[{'name': 'synthetic-backend', 'version': '1.0', 'wheels': [
                       {'filename': filename, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}]}])
    (repo / lock.LOCK_PATH).write_bytes(lock.encoded(value))
    venv, python = runtime(tmp_path)
    site = venv / 'lib/python3.14/site-packages'
    sentinel = tmp_path / 'pth-executed'
    (site / 'synthetic.pth').write_text('import pathlib; pathlib.Path(' + repr(str(sentinel)) + ').write_text("executed")\n')
    receipt = lock.install_locked(repo, python=python, venv=venv, runtime_mode='hermes', platform=platform, host_source=host)
    assert receipt['phase'] == 'completed' and not sentinel.exists()
    assert receipt['build_packages'][0]['wheel_sha256'] == hashlib.sha256(raw).hexdigest()
    assert lock.observe_locked(repo, python=python, venv=venv, runtime_mode='hermes', platform=platform, host_source=host)['profile_sha256'] == lock.digest(profile)
    # The ordinary synthetic CLI must survive private workspace deletion with its real runtime.
    script = venv / 'bin/synthetic-host'
    assert str(python) in script.read_text() and 'isolated-python' not in script.read_text()
    (site / 'synthetic.pth').unlink()
    started = subprocess.run([str(script)], capture_output=True, text=True, timeout=10)
    assert started.returncode == 0 and started.stdout.strip() == str(host / 'synthetic_host.py')
    import base64
    import csv
    direct = site / 'synthetic_host-1.0.dist-info/direct_url.json'
    rows = list(csv.reader((direct.parent / 'RECORD').read_text().splitlines()))
    matches = [row for row in rows if row[0] == 'synthetic_host-1.0.dist-info/direct_url.json']
    assert len(matches) == 1
    expected = base64.urlsafe_b64encode(hashlib.sha256(direct.read_bytes()).digest()).rstrip(b'=').decode()
    assert matches[0][1:] == ['sha256=' + expected, str(direct.stat().st_size)]
