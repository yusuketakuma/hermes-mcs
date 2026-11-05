"""Synthetic release apply owner testkit contracts; shared helpers preserve isolated runtime state."""
from pathlib import Path
import copy
import os
import sqlite3
import subprocess
import time
import pytest
import ledger
import update_journal
import release_apply_owner
from test_release_dependency_lock import product as product, runtime as runtime
from test_sdc_release_state import native_child as native_child, store as store
from test_sdc_provision import world as world
from test_sdc_provision_profiles import registered as registered





@pytest.fixture
def registered_effect(native_child, store, monkeypatch):
    # Test-only registration is not a live factory or signature integration acceptance.
    from adapters.sdc import release_state
    child = native_child
    ref = copy.deepcopy(child['journal']['release_ref'])
    journal = {**update_journal.default_state(), 'release_ref': ref}
    update_journal.save_state(child['path'], journal)
    saved = store.read(release_state.NAME)
    locks = child['parameters']['locks']
    entry = release_apply_owner._StartedEffect(
        owner=object(), process_id=os.getpid(), store=store, locks=locks,
        lock_identities=tuple(release_apply_owner._effect_fd_identity(fd) for fd in locks),
        runtime_home=str(child['home']), repository=str(child['repo']), reference=ref,
        spent=copy.deepcopy(saved['spent'][-1]), published_revision=saved['revision'],
        journal=copy.deepcopy(journal), configuration={}, start_deadline=time.monotonic() + 30,
        prequiesce_deadline=time.monotonic() + 120)
    cap = release_apply_owner._EffectCapability()
    monkeypatch.setattr(release_apply_owner, '_EFFECT_CAPS', {cap: entry})
    monkeypatch.setattr(release_apply_owner, '_EFFECT_PARENTS', {entry.owner: entry})
    monkeypatch.setattr(release_apply_owner, '_EFFECT_CLAIMED', set())
    monkeypatch.setattr(release_apply_owner, '_EFFECT_RUNNING', set())
    args = (None, ref['target_commit'], None, entry.spent['preconditions']['current_commit'], False, (), None, False, None)
    return cap, entry, dict(arguments=args, runtime_home=entry.runtime_home,
                           repository=entry.repository, journal_path=child['path'])


@pytest.fixture
def installed_current(product, tmp_path):
    import platform as host_platform
    import release_dependency_lock as dependencies
    repo, value, platform = product
    platform = {**platform, 'cpu': {'aarch64': 'arm64'}.get(host_platform.machine().lower(), host_platform.machine().lower())}
    value['profiles'][0]['platform'] = platform
    (repo / dependencies.LOCK_PATH).write_bytes(dependencies.encoded(value))
    (repo / 'candidate.py').write_text("raise AssertionError('candidate must not execute')\n")
    for args in (['init', '-q'], ['add', '.'],
                 ['-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic']):
        subprocess.run(['/usr/bin/git', '-C', str(repo), *args], check=True, capture_output=True, timeout=10)
    venv, python = runtime(tmp_path)
    dependencies.install_locked(repo, python=python, venv=venv, runtime_mode='standalone', platform=platform)
    ledger_path = tmp_path / 'synthetic-ledger.db'
    with sqlite3.connect(ledger_path) as db:
        db.execute('PRAGMA user_version=9')
    return repo, {'python': python, 'venv': venv, 'runtime_mode': 'standalone',
                  'platform': platform, 'ledger_path': ledger_path, 'deadline': time.monotonic() + 30}


def reference():
    return {'schema_version': 'mcs.release-reference.v1', 'root_fingerprint': 'a' * 64,
            'repo_fingerprint': 'b' * 64, 'operation_id': '00000000-0000-4000-8000-000000000001',
            'grant_id': '00000000-0000-4000-8000-000000000002', 'plan_sha256': 'c' * 64,
            'release_id': 'd' * 64, 'target_code_seq': 2, 'target_commit': 'e' * 40,
            'artifact_sha256': 'f' * 64, 'lock_sha256': '1' * 64, 'target_schema': 1,
            'start_receipt_id': '00000000-0000-4000-8000-000000000003', 'started_at': 1000}


@pytest.fixture
def current_release(registered, tmp_path, monkeypatch):
    from adapters.sdc import provision, release_contract, release_state
    from test_sdc_provision import NOW
    from test_sdc_provision_profiles import install
    from test_sdc_release_state import baseline, APPLY
    import json
    install(registered, 'support')
    monkeypatch.setenv('HOME', str(tmp_path))
    home = tmp_path / 'runtime'
    home.mkdir()
    with release_apply_owner._control_store(home) as control:
        monitor = provision.profile_snapshot(registered, profile='monitor', now_ms=NOW)
        support = provision.profile_snapshot(registered, profile='support', now_ms=NOW)
        state = registered.read('state.json')
        supplied = baseline()
        supplied['repo_fingerprint'] = release_apply_owner._repository_fingerprint(home)
        supplied['trust']['environment'] = monitor['bundle']['environment']
        supplied['state'].update({k: monitor['bundle'][k] for k in ('tenant', 'installation', 'environment', 'realm_epoch')})
        supplied['state'].update(device_id=state['device_id'], trust_digest=release_contract.hash_release_trust(supplied['trust']))
        with control.release_entry_lock():
            release_state._bootstrap_verified(control, **supplied)
        enrollment = registered.enrollment_binding()
        binding = {k: enrollment[k] for k in ('root_fingerprint', 'endpoint_hash')}
        binding.update({k: state[k] for k in ('claim_id', 'device_id', 'key_id')})
        binding.update({k: monitor['bundle'][k] for k in ('tenant', 'installation', 'environment', 'realm_epoch')})
        gold = json.loads((Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/releases/registration-vectors.json').read_text())
        config = dict(schema_version='mcs.release-bootstrap.v1', control_root_fingerprint=control.root_fingerprint(),
            monitor_binding=binding, support_snapshot_sha256=registered._fingerprint(support),
            release_endpoint={'origin': 'https://release.invalid', 'audience': 'synthetic-sdc', 'environment': binding['environment']},
            operation_pins=APPLY['pins'], registration_pins=gold['pins'], initial_trust_sha256=release_contract.hash_release_trust(supplied['trust']))
        control.write('release-bootstrap.json', config)
        yield control, registered, home, NOW, config


@pytest.fixture
def native_product(product, tmp_path):
    # Synthetic accepted initial baseline only; not a live factory or deployment acceptance.
    import json
    import shutil
    import platform as host_platform
    import release_dependency_lock as dependencies
    import release_owner_source as source
    from test_sdc_release_state import baseline
    root = Path(__file__).resolve().parents[2]
    repo, value, platform = product
    platform['cpu'] = {'aarch64': 'arm64'}.get(host_platform.machine().lower(), host_platform.machine().lower())
    value['profiles'][0]['platform'] = platform
    (repo / dependencies.LOCK_PATH).write_bytes(dependencies.encoded(value))
    # Copy development source only. Existing synthetic lock/wheels/requirements stay synthetic.
    tracked = subprocess.check_output(['/usr/bin/git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
    for name in tracked:
        if not source.source_path(name):
            continue
        path, target = root / name, repo / name
        assert path.is_file() and not path.is_symlink()
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(path.stat().st_mode & 0o777)
    shutil.copyfile(root / '.gitignore', repo / '.gitignore')
    # Only service/SDK boundaries are synthetic; authorization, locks, bundle and updater stay actual.
    (repo / 'mcs/ops/mcs_setup.py').write_text('import sys\nassert sys.argv[1:] == ["services"]\n')
    (repo / 'mcs_standalone/service.py').write_text('from pathlib import Path\nimport os\n'
        'def status(root, *, now=None):\n'
        ' stopped=(Path(root)/"data/update_in_progress.marker").exists()\n'
        ' return {"update_in_progress":stopped,"children":{} if stopped else {k:{"kind":"background","pid":os.getpid()} for k in ("extract-0","extract-2")}}\n'
        'def request_restart(root): return "synthetic-service-boundary"\n')
    home = tmp_path / 'native-runtime'
    home.mkdir(mode=0o700)
    venv, _ = runtime(home)
    venv.rename(home / 'venv')
    python = home / 'venv/bin/python3'
    dependencies.install_locked(repo, python=python, venv=home / 'venv', runtime_mode='standalone', platform=platform)
    data = home / 'data'
    data.mkdir(mode=0o700)
    ledger.Ledger(str(data / 'ledger.db')).close()
    backups = data / 'backups'
    backups.mkdir(mode=0o700)
    shutil.copyfile(data / 'ledger.db', backups / 'ledger-20261026.db')
    (backups / 'ledger-20261026.db').chmod(0o600)
    (home / 'config.json').write_text(json.dumps({'runtime_mode': 'standalone', 'mcs_login_id': 'synthetic', 'notify_target': 'local'}))
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q')
    git('add', '.')
    manifest = source.build_manifest(repo, deadline=time.monotonic() + 30)
    (repo / source.MANIFEST_PATH).write_bytes(source.canonical(manifest))
    git('add', source.MANIFEST_PATH)
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic native source')
    supplied = baseline()
    supplied['owner_evidence_sha256'] = source.digest(manifest)
    return repo, home, platform, supplied


@pytest.fixture(scope='module')
def native_signing_keys(tmp_path_factory):
    import sdc_support_signatures as signatures

    signing = tmp_path_factory.mktemp('synthetic-signing-keys')
    signing.chmod(0o700)
    keys = []
    for name in ('release', 'operator', 'registration'):
        private = signing / (name + '.pem')
        private.touch(mode=0o600)
        subprocess.run([signatures.OPENSSL, 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(private)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
        public = subprocess.check_output([signatures.OPENSSL, 'pkey', '-in', str(private), '-pubout', '-outform', 'DER'], stderr=subprocess.DEVNULL, timeout=10)
        keys.append((private.read_bytes(), public))
    assert len({public for _, public in keys}) == 3
    return tuple(keys)
