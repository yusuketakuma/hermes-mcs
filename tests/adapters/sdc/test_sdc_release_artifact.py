"""完全合成packで、到達不能objectとdeltaをGit起動前に有界に検査する。"""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import zlib

import pytest

from adapters.sdc import release_artifact as artifact
import release_apply_owner as owner


def object_header(kind, size):
    first = (kind << 4) | (size & 15)
    size >>= 4
    out = bytearray([first | (128 if size else 0)])
    while size:
        byte = size & 127
        size >>= 7
        out.append(byte | (128 if size else 0))
    return bytes(out)


def entry(body, kind=3, *, declared=None, base=b''):
    return object_header(kind, len(body) if declared is None else declared) + base + zlib.compress(body)


def pack(*entries, version=2):
    data = b'PACK' + version.to_bytes(4, 'big') + len(entries).to_bytes(4, 'big') + b''.join(entries)
    return data + hashlib.sha1(data).digest()


def oid(body, kind='blob'):
    return hashlib.sha1(f'{kind} {len(body)}\0'.encode() + body).hexdigest()


def scan(tmp_path, data, *, budget=1000000, deadline=None):
    stage = tmp_path / 'stage'
    stage.mkdir()
    return artifact._scan_pack(io.BytesIO(data), stage, max_inflated_bytes=budget,
                               deadline=time.monotonic() + 10 if deadline is None else deadline)


@pytest.mark.parametrize('version', [2, 3])
def test_full_pack_exact_framing_zero_blob_and_hashes(tmp_path, version):
    result = scan(tmp_path, pack(entry(b''), entry(b'actual content'), version=version))
    assert result['object_count'] == 2 and result['inflated_bytes'] == len(b'actual content')
    assert set(result['objects']) == {oid(b''), oid(b'actual content')}
    assert result['objects'][oid(b'actual content')]['path'].read_bytes() == b'actual content'


@pytest.mark.parametrize('change', [
    lambda data: data[:-1],
    lambda data: data + b'SUFFIX',
    lambda data: data[:-20] + b'0' * 20,
    lambda data: b'NOPE' + data[4:],
    lambda data: data[:4] + (4).to_bytes(4, 'big') + data[8:],
])
def test_bad_pack_checksum_trailer_version_never_succeeds(tmp_path, change):
    with pytest.raises(artifact.ArtifactError):
        scan(tmp_path, change(pack(entry(b'x'))))


@pytest.mark.parametrize('declared,actual', [(0, b'x'), (2, b'xxx'), (3, b'x')])
def test_actual_inflate_length_must_equal_declared_length(tmp_path, declared, actual):
    with pytest.raises(artifact.ArtifactError, match='OBJECT_SIZE_MISMATCH'):
        scan(tmp_path, pack(entry(actual, declared=declared)))


def test_unreachable_bomb_counts_before_any_git_or_target_tree(tmp_path):
    with pytest.raises(artifact.ArtifactError, match='PACK_INFLATED_LIMIT'):
        scan(tmp_path, pack(entry(b'valid'), entry(b'x' * 100000)), budget=1000)


def test_actual_bomb_cannot_hide_behind_small_declared_length(tmp_path):
    with pytest.raises(artifact.ArtifactError, match='OBJECT_SIZE_MISMATCH'):
        scan(tmp_path, pack(entry(b'x' * 1000000, declared=1)), budget=1000)
    assert (tmp_path / 'stage/object-000000').stat().st_size == 0


def test_object_count_is_bounded_before_allocating_or_reading_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact, 'MAX_PACK_OBJECTS', 2)
    with pytest.raises(artifact.ArtifactError, match='PACK_OBJECT_LIMIT'):
        scan(tmp_path, b'PACK' + (2).to_bytes(4, 'big') + (3).to_bytes(4, 'big'))
    assert list((tmp_path / 'stage').iterdir()) == []


@pytest.mark.parametrize('total', [False, True])
def test_metadata_has_separate_individual_and_total_budgets(tmp_path, monkeypatch, total):
    monkeypatch.setattr(artifact, 'MAX_METADATA_BYTES', 4)
    monkeypatch.setattr(artifact, 'MAX_METADATA_TOTAL', 6)
    with pytest.raises(artifact.ArtifactError, match='METADATA_LIMIT'):
        scan(tmp_path, pack(entry(b'abcd', kind=1), entry(b'efgh', kind=2)) if total
             else pack(entry(b'x' * 5, kind=1)))


@pytest.mark.parametrize('kind', [6, 7])
def test_delta_copy_and_insert_reconstruct_without_unbounded_memory(tmp_path, kind):
    base = b'abc'
    delta = bytes([3, 4, 0x90, 3, 1]) + b'!'
    first = entry(base)
    base_ref = bytes([len(first)]) if kind == 6 else bytes.fromhex(oid(base))
    result = scan(tmp_path, pack(first, entry(delta, kind=kind, base=base_ref)))
    actual = result['objects'][oid(b'abc!')]
    assert actual['path'].read_bytes() == b'abc!' and actual['depth'] == 1
    assert result['inflated_bytes'] == len(base) + len(delta) + 4


def test_ref_delta_can_precede_its_base(tmp_path):
    delta = bytes([3, 4, 0x90, 3, 1]) + b'!'
    result = scan(tmp_path, pack(entry(delta, kind=7, base=bytes.fromhex(oid(b'abc'))), entry(b'abc')))
    assert result['objects'][oid(b'abc!')]['path'].read_bytes() == b'abc!'


@pytest.mark.parametrize('delta', [b'\x02\x01\x01x', b'\x03\x01\0', b'\x03\x01\x91\x03\x01',
                                    b'\x03\x02\x01x', b'\x03\x01\x01xx'])
def test_invalid_delta_size_opcode_copy_and_trailing_commands_are_rejected(tmp_path, delta):
    with pytest.raises(artifact.ArtifactError):
        scan(tmp_path, pack(entry(b'abc'), entry(delta, kind=7, base=bytes.fromhex(oid(b'abc')))))


def test_missing_external_delta_base_is_not_fetched(tmp_path):
    with pytest.raises(artifact.ArtifactError, match='DELTA_BASE_MISSING'):
        scan(tmp_path, pack(entry(b'\x03\x01\x01x', kind=7, base=b'0' * 20)))


def test_delta_target_size_is_charged_before_reconstruction(tmp_path):
    delta = bytes([3, 4, 0x90, 3, 1]) + b'!'
    with pytest.raises(artifact.ArtifactError, match='PACK_INFLATED_LIMIT'):
        scan(tmp_path, pack(entry(b'abc'), entry(delta, kind=7, base=bytes.fromhex(oid(b'abc')))),
             budget=3 + len(delta) + 3)
    assert not (tmp_path / 'stage/object-000001.resolved').exists()


def test_delta_chain_depth_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact, 'MAX_DELTA_DEPTH', 1)
    first = bytes([1, 2, 0x90, 1, 1]) + b'b'
    second = bytes([2, 3, 0x90, 2, 1]) + b'c'
    with pytest.raises(artifact.ArtifactError, match='DELTA_DEPTH_LIMIT'):
        scan(tmp_path, pack(entry(b'a'), entry(first, kind=7, base=bytes.fromhex(oid(b'a'))),
                            entry(second, kind=7, base=bytes.fromhex(oid(b'ab')))))


@pytest.mark.parametrize('deadline', [0, True, float('nan'), float('inf')])
def test_invalid_or_expired_deadline_never_creates_an_object(tmp_path, deadline):
    with pytest.raises(artifact.ArtifactError, match='DEADLINE'):
        scan(tmp_path, pack(entry(b'x')), deadline=deadline)
    assert list((tmp_path / 'stage').iterdir()) == []


@pytest.mark.parametrize('offsets', [False, True])
def test_real_git_delta_pack_matches_every_original_blob(tmp_path, offsets):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args, data=None):
        return subprocess.run(['/usr/bin/git', '-C', str(repo), *args], input=data,
                              capture_output=True, check=True, timeout=10).stdout

    git('init', '--bare', '--object-format=sha1', '--template=')
    base = bytes(range(256)) * 1024
    bodies = [base, base[:130000] + b'synthetic-change' + base[130016:], base + b'end']
    identities = [git('hash-object', '-w', '--stdin', data=body).strip() for body in bodies]
    options = ['--delta-base-offset'] if offsets else []
    raw = git('pack-objects', '--stdout', '--window=10', '--depth=10',
              '--no-reuse-delta', '--no-reuse-object', *options, data=b'\n'.join(identities) + b'\n')
    actual = scan(tmp_path, raw, budget=2000000)
    assert set(actual['objects']) == {value.decode('ascii') for value in identities}
    assert any(row['depth'] for row in actual['objects'].values())
    for identity, body in zip(identities, bodies):
        assert actual['objects'][identity.decode('ascii')]['path'].read_bytes() == body


def tree_pack(tmp_path, entries, *, subtrees=()):
    body = b'x'
    tree = b''.join(mode + b' ' + name + b'\0' + bytes.fromhex(identity)
                    for mode, name, identity in entries)
    commit = b'tree ' + oid(tree, 'tree').encode() + b'\n\nsynthetic\n'
    result = scan(tmp_path, pack(entry(body), *[entry(raw, kind=2) for raw in subtrees],
                                 entry(tree, kind=2), entry(commit, kind=1)))
    return result['objects'], oid(commit, 'commit')


@pytest.mark.parametrize('name', [b'../escape', b'/absolute', b'.git', b'.GIT', b'.gitmodules',
                                  b'.gitattributes', b'.GITATTRIBUTES',
                                  b'a\\b', b'colon:name', b'control\nname', b'config.json', b'data'])
def test_target_tree_refuses_unsafe_and_protected_paths(tmp_path, name):
    objects, commit = tree_pack(tmp_path, [(b'100644', name, oid(b'x'))])
    with pytest.raises(artifact.ArtifactError):
        artifact._target_tree(objects, commit, max_entries=50000, max_bytes=1000, deadline=time.monotonic() + 10)


@pytest.mark.parametrize('mode', [b'120000', b'160000', b'100664'])
def test_target_tree_refuses_symlinks_gitlinks_and_nonregular_modes(tmp_path, mode):
    objects, commit = tree_pack(tmp_path, [(mode, b'unsafe', oid(b'x'))])
    with pytest.raises(artifact.ArtifactError, match='UNSAFE_ENTRY_TYPE'):
        artifact._target_tree(objects, commit, max_entries=50000, max_bytes=1000, deadline=time.monotonic() + 10)


@pytest.mark.parametrize('entries,expected', [(1, 'TREE_ENTRY_LIMIT'), (10, 'TREE_EXPANDED_LIMIT')])
def test_shared_subtree_is_counted_for_each_path_occurrence(tmp_path, entries, expected):
    subtree = b'100644 x\0' + bytes.fromhex(oid(b'x'))
    objects, commit = tree_pack(tmp_path, [(b'40000', b'a', oid(subtree, 'tree')),
                                          (b'40000', b'b', oid(subtree, 'tree'))], subtrees=[subtree])
    with pytest.raises(artifact.ArtifactError, match=expected):
        artifact._target_tree(objects, commit, max_entries=entries, max_bytes=1, deadline=time.monotonic() + 10)


def test_casefold_tree_collision_is_not_a_second_file(tmp_path):
    objects, commit = tree_pack(tmp_path, [(b'100644', b'A', oid(b'x')), (b'100644', b'a', oid(b'x'))])
    with pytest.raises(artifact.ArtifactError, match='UNSAFE_PATH'):
        artifact._target_tree(objects, commit, max_entries=10, max_bytes=100, deadline=time.monotonic() + 10)


def profile(*, hermes=False):
    value = {'profile_id': 'synthetic', 'runtime_mode': 'standalone',
             'platform': {'os': 'linux', 'cpu': 'x86_64', 'python': [3, 14]},
             'requirements_sha256': hashlib.sha256(b'').hexdigest(),
             'host': None, 'packages': [], 'build_packages': []}
    if hermes:
        value.update(runtime_mode='hermes', host={'commit': 'a' * 40,
            'pyproject_sha256': value['requirements_sha256'], 'extras': ['messaging'], 'project_name': 'synthetic-host'})
    return value


@pytest.fixture
def bundle(tmp_path, request):
    repo = tmp_path / 'source'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=Synthetic',
                               '-c', 'user.email=synthetic@example.invalid', '-c', 'commit.gpgsign=false',
                               *args], check=True, capture_output=True, timeout=10).stdout

    git('init', '--object-format=sha1', '--template=')
    parameters = getattr(request, 'param', {})
    expected_profile = profile(hermes=parameters.get('hermes', False))
    selected = copy.deepcopy(expected_profile)
    selected.update(parameters.get('profile', {}))
    lock = {'schema_version': 'mcs.release-dependency-lock.v1', 'product': 'hermes-mcs',
            'core_kind': 'stdlib-only', 'profiles': [selected]}
    if parameters.get('unrelated'):
        other = profile()
        other.update(profile_id='unrelated', platform={'os': 'linux', 'cpu': 'arm64', 'python': [3, 14]})
        lock['profiles'].append(other)
    (repo / 'deployment').mkdir()
    raw = json.dumps(lock, sort_keys=True, separators=(',', ':')).encode('ascii')
    (repo / 'deployment/sdc-release-lock.json').write_bytes(raw)
    (repo / 'deployment/requirements-standalone.txt').write_bytes(parameters.get('requirements', b''))
    if parameters.get('hermes'):
        (repo / 'install.sh').write_text('HERMES_PIN="' + parameters.get('pin', 'a' * 40) + '"\n')
    (repo / 'never_run.py').write_text('raise RuntimeError("CANDIDATE_MUST_NOT_RUN")\n')
    if parameters.get('attributes_path'):
        attributes = repo / parameters['attributes_path']
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_text('* filter=synthetic text eol=crlf ident\n')
    git('add', '.')
    git('commit', '-m', 'synthetic inspection')
    commit = git('rev-parse', 'HEAD').strip().decode('ascii')
    target = tmp_path / 'artifact.bundle'
    git('bundle', 'create', '--version=2', str(target), '--all')
    content = target.read_bytes()
    core = {'commit': commit, 'lock_sha256': hashlib.sha256(raw).hexdigest(),
            'artifact': {'format': 'git-bundle-v2', 'object_format': 'sha1', 'size': len(content),
                         'sha256': hashlib.sha256(content).hexdigest()}}
    return target, core, expected_profile


def inspect(bundle, **options):
    target, core, expected_profile = bundle
    args = dict(core=core, policy={'max_artifact_bytes': 268435456, 'max_unpacked_bytes': 1073741824,
                                  'max_archive_entries': 50000}, profile_id='synthetic',
                profile_sha256=hashlib.sha256(json.dumps(expected_profile, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                deadline=time.monotonic() + 10)
    args.update(options)
    return owner._inspect_artifact(target, **args)


def test_real_bundle_is_inspected_without_candidate_or_ambient_execution(bundle, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.hooksPath')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', '/forbidden-synthetic-hooks')
    monkeypatch.setenv('GIT_TRACE', '/forbidden-synthetic-trace')
    calls = []
    popen = owner.subprocess.Popen

    def checked(argv, **kwargs):
        assert argv[0] == '/usr/bin/git'
        assert not any(name in kwargs['env'] for name in ('GIT_CONFIG_COUNT', 'GIT_TRACE'))
        assert kwargs['start_new_session'] and kwargs['close_fds']
        assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
        calls.append(argv)
        return popen(argv, **kwargs)

    monkeypatch.setattr(owner.subprocess, 'Popen', checked)
    with inspect(bundle) as result:
        private = result['bundle'].parent
        assert result['core']['commit'] == bundle[1]['commit']
        assert hashlib.sha256(result['lock_raw']).hexdigest() == bundle[1]['lock_sha256']
        assert result['entries'] == 4 and result['pack'].exists()
    assert not private.exists() and len(calls) == 2
    assert any('index-pack' in call and '--strict' in call for call in calls)


@pytest.mark.parametrize('bundle', [{'attributes_path': '.gitattributes'},
                                   {'attributes_path': 'nested/.GITATTRIBUTES'}], indirect=True)
def test_git_attributes_are_refused_at_every_depth_before_native_git(bundle, monkeypatch):
    monkeypatch.setattr(owner.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Git before attributes rejection'))
    with pytest.raises(artifact.ArtifactError, match='UNSAFE_PATH'):
        with inspect(bundle):
            pytest.fail('attributes must not yield')


def test_adapter_pack_inspection_has_no_native_flat_import_dependency(bundle):
    target, core, selected = bundle
    parameters = dict(core=core, policy={'max_artifact_bytes': 268435456,
                                        'max_unpacked_bytes': 1073741824, 'max_archive_entries': 50000},
                      profile_id='synthetic', profile_sha256=hashlib.sha256(
                          json.dumps(selected, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
    program = '''import json, sys, time
sys.path.insert(0, sys.argv[1])
from adapters.sdc import release_artifact
parameters = json.loads(sys.argv[3])
parameters['deadline'] = time.monotonic() + 10
with release_artifact._inspect_verified(sys.argv[2], **parameters) as result:
    assert result['pack'].is_file() and result['lock_raw']
assert 'release_dependency_lock' not in sys.modules
assert not any(name.startswith('mcs.') for name in sys.modules)
'''
    subprocess.run([sys.executable, '-I', '-S', '-c', program,
                    str(Path(__file__).resolve().parents[3]), str(target), json.dumps(parameters)],
                   check=True, capture_output=True, timeout=15)


def test_owner_strict_git_rejects_malformed_commit_after_bounded_tree_scan(bundle, tmp_path, monkeypatch):
    target, core, selected = bundle
    stream = io.BytesIO(target.read_bytes())
    artifact._bundle_header(stream)
    scanned = scan(tmp_path, stream.read())
    first = scanned['objects'][core['commit']]['path'].read_bytes().splitlines(keepends=True)[0]
    malformed = first + b'\nsynthetic commit missing author and committer\n'
    kinds = {name: number for number, name in artifact.KINDS.items()}
    raw = (b'# v2 git bundle\n' + oid(malformed, 'commit').encode() + b' refs/heads/invalid\n\n'
           + pack(*[entry(row['path'].read_bytes(), kind=kinds[row['kind']])
                    for row in scanned['objects'].values()], entry(malformed, kind=1)))
    target.write_bytes(raw)
    core['commit'] = oid(malformed, 'commit')
    core['artifact'].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    strict = owner._git_validation
    calls = []

    def required(*args):
        calls.append(True)
        return strict(*args)

    monkeypatch.setattr(owner, '_git_validation', required)
    with pytest.raises(artifact.ArtifactError, match='GIT_VALIDATION_FAILED'):
        with inspect((target, core, selected)):
            pytest.fail('owner yielded a malformed commit without Git strict')
    assert calls == [True]


@pytest.mark.parametrize('change', ['hash', 'size', 'commit', 'lock', 'source-link'])
def test_changed_or_unbound_bundle_never_reaches_git(bundle, monkeypatch, change):
    target, core, expected_profile = bundle
    if change == 'hash':
        core['artifact']['sha256'] = '0' * 64
    elif change == 'size':
        core['artifact']['size'] += 1
    elif change == 'commit':
        core['commit'] = '0' * 40
    elif change == 'lock':
        core['lock_sha256'] = '0' * 64
    else:
        linked = target.with_suffix('.link')
        linked.symlink_to(target)
        target = linked
    monkeypatch.setattr(owner.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Git before validation'))
    with pytest.raises(artifact.ArtifactError):
        with inspect((target, core, expected_profile)):
            pytest.fail('inspection should not yield')


def test_bundle_prerequisite_is_not_fetched_or_silently_completed():
    raw = b'# v2 git bundle\n-' + b'a' * 40 + b' missing prerequisite\n' + b'b' * 40 + b' HEAD\n\n'
    with pytest.raises(artifact.ArtifactError, match='PREREQUISITE_UNAVAILABLE'):
        artifact._bundle_header(io.BytesIO(raw))


def test_inspection_context_preserves_caller_failure_and_removes_private_files(bundle):
    with pytest.raises(ValueError, match='synthetic caller failure'):
        with inspect(bundle) as result:
            temporary = result['bundle']
            raise ValueError('synthetic caller failure')
    assert not temporary.exists()


@pytest.mark.parametrize('bundle', [{'unrelated': True}], indirect=True)
def test_other_profiles_can_change_without_changing_selected_runtime_profile(bundle):
    with inspect(bundle) as result:
        assert result['lock_raw']


@pytest.mark.parametrize('bundle', [{'profile': {'requirements_sha256': '1' * 64}}], indirect=True)
def test_selected_profile_change_is_refused_before_git_or_dependency_install(bundle, monkeypatch):
    monkeypatch.setattr(owner.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Git before profile check'))
    with pytest.raises(artifact.ArtifactError, match='PROFILE_CHANGED'):
        with inspect(bundle):
            pytest.fail('dependency change must not yield')


@pytest.mark.parametrize('bundle', [{'requirements': b'synthetic-demo==2.0\n'}], indirect=True)
def test_declared_dependency_source_cannot_change_behind_an_unchanged_profile(bundle, monkeypatch):
    monkeypatch.setattr(owner.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Git before provenance check'))
    with pytest.raises(artifact.ArtifactError, match='PROFILE_SOURCE_CHANGED'):
        with inspect(bundle):
            pytest.fail('unlocked source must not yield')


@pytest.mark.parametrize('bundle', [{'hermes': True}], indirect=True)
def test_hermes_literal_pin_matches_the_existing_host_profile_without_executing_it(bundle):
    with inspect(bundle) as result:
        assert result['pack'].exists()


@pytest.mark.parametrize('bundle', [{'hermes': True, 'pin': 'b' * 40}], indirect=True)
def test_hermes_pin_change_is_refused_without_host_checkout_or_sdk_install(bundle, monkeypatch):
    monkeypatch.setattr(owner.subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Git before host pin check'))
    with pytest.raises(artifact.ArtifactError, match='PROFILE_SOURCE_CHANGED'):
        with inspect(bundle):
            pytest.fail('host migration must not yield')


def test_git_timeout_signals_owned_group_and_reaps_before_return(tmp_path, monkeypatch):
    calls = []

    class Child:
        pid = 424242
        returncode = None

        def wait(self, timeout=None):
            calls.append(('wait', timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired('synthetic private Git', timeout)
            self.returncode = -9
            return self.returncode

    monkeypatch.setattr(owner.subprocess, 'Popen', lambda *_a, **_k: Child())
    monkeypatch.setattr(owner.os, 'killpg', lambda pid, sig: calls.append(('signal', pid, sig)))
    with pytest.raises(subprocess.TimeoutExpired):
        owner._git_validation(tmp_path, tmp_path / 'synthetic.pack', 1, time.monotonic() + 10)
    assert calls[0][0] == 'wait' and calls[1][0:2] == ('signal', 424242)
    assert calls[-1] == ('wait', None)
