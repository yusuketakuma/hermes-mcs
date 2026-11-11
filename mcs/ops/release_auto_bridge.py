"""v3 bridge core: sidecar classification, ControlManifest, baseline and migration drive."""
import hashlib
import json
import os
import stat
import uuid

from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_migration as migration
from adapters.sdc import release_auto_state as auto_state
from adapters.sdc.storage import Store

MAX_OWNER_BYTES = 8 * 1024 * 1024
MAX_INTENT_BYTES = 32 * 1024

# logical_id -> MigrationFile.disposition (§11.1 classes). Locks are runtime files,
# not migrated records; unknown entries refuse migration instead of guessing.
_SIDECAR_DISPOSITION = {
    'release-owner.json': 'translate_verified',
    'release-owner-activation.json': 'archive_exact',
    'release-bootstrap.json': 'archive_exact',
    'release-planning.json': 'archive_exact',
    'release-plan.json': 'archive_exact',
    'release-change.json': 'archive_exact',
    'release-successor.json': 'archive_exact',
}
_KNOWN_FILES = frozenset(_SIDECAR_DISPOSITION) | {'release-start-intent.json', 'release-migration-v3.json'}

# control closure内でrecoveryが必要とする最小file集合(descriptorを除く)。
# manifest自体はfilesに含めない(04 §4.3)。run_migrationはこの部分集合を
# 必須として拒否する — 配備後にrecoveryが動けないclosureを作らない。
RECOVERY_REQUIRED_CLOSURE = frozenset({
    'mcs/ops/release_auto_state.py',
    'mcs/ops/release_auto_owner.py',
    'mcs/ops/release_auto_apply.py',
    'mcs/ops/release_runtime_proof.py',
    'mcs/ops/release_auto_recovery.py',
    'adapters/sdc/release_auto_contract.py',
    'adapters/sdc/release_auto_state.py',
    'mcs/core/runtime_entry.py',
})


class BridgeError(RuntimeError):
    """Bridge refusal; the code selects the safe hold."""

    def __init__(self, code='BRIDGE_HOLD'):
        super().__init__(code)
        self.code = code


# m3-autoupdate-design.md §6: every prerequisite must be explicitly measured.
# The evaluator never guesses — a missing or non-True measurement is a blocker.
_ELIGIBILITY_PREREQUISITES = (
    'profile_measured',               # 実機profileが実測済み
    'lock_profile_present',           # deployment/sdc-release-lock.json が実profile値で存在
    'unmanaged_processes_cleared',    # 旧worker/別checkout/旧watchdogの適格性確認済み(AT090)
    'hermes_contract_applicable',     # Hermes起動経路へbootstrap契約を適用可能
    'lineworks_lock_verified',        # lineworks_adapterのrun.lock遵守を確認済み
    'lock_dag_verified',              # C↔release lock交差・_apply_pipeline DAGを網羅済み
    'bridge_completed',               # v1→v3 bridgeが verified まで完了
)


def evaluate_eligibility(measurements):
    """§6の全前提を明示測定値だけで評価する。AUTO_ELIGIBLEの唯一の導出点。

    measurementsは {前提名: True} のみを適格と認める。dict以外・欠落・
    True以外の値(1/'yes'等)は全てblockerとしてfail-closedに倒す。
    """
    if not isinstance(measurements, dict):
        measurements = {}
    blockers = [name for name in _ELIGIBILITY_PREREQUISITES
                if measurements.get(name) is not True]
    return {'auto_eligible': not blockers, 'blockers': list(blockers)}


def _check(ok, code='BRIDGE_HOLD'):
    if not ok:
        raise BridgeError(code)


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def classify_sidecars(store):
    """Enumerate control-root records into MigrationFile rows plus blockers.

    Every directory entry must be a known sidecar, a known lock, or a Store temp
    prefix; anything else refuses migration rather than being silently skipped.
    """
    blockers = []
    files = []
    entries = set(os.listdir(store.fd))
    for name in sorted(entries):
        if name.startswith('.sdc-'):
            continue
        if name.endswith('.lock') or name == 'runtime-entry.lock':
            if name not in {'release-entry.lock', 'release-owner.lock', 'release-authority.lock',
                            'runtime-entry.lock'}:
                blockers.append('UNLISTED_SIDECAR:' + name)
            continue
        if name not in _KNOWN_FILES:
            blockers.append('UNLISTED_SIDECAR:' + name)
            continue
        raw = store.read_migration_raw(name)
        if raw is None:
            continue
        disposition = _SIDECAR_DISPOSITION.get(name)
        if disposition is None:
            blockers.append('UNRESOLVED_' + name.upper().replace('-', '_').replace('.', '_'))
            continue
        files.append({'logical_id': name, 'before_sha256': _sha256(raw),
                      'size': len(raw), 'disposition': disposition})
    if blockers:
        raise BridgeError(blockers[0])
    return files


def check_v1_blockers(store):
    """§11.1 migration prohibitions against the v1 owner and pending v3 records."""
    raw = store.read_migration_raw('release-owner.json')
    if raw is not None:
        try:
            value = json.loads(raw)
        except Exception:
            raise BridgeError('V1_UNREADABLE') from None
        schema = value.get('schema_version') if isinstance(value, dict) else None
        if schema == 'mcs.release-owner.v3':
            raise BridgeError('ALREADY_V3')
        if schema == 'mcs.release-owner.v1':
            if value.get('spent'):
                raise BridgeError('V1_SPENT_PRESENT')
            outbox = value.get('outbox') or []
            if any(isinstance(e, dict) and e.get('acknowledged') is not True for e in outbox):
                raise BridgeError('V1_UNKNOWN_PRESENT')
        else:
            raise BridgeError('OWNER_SCHEMA_UNKNOWN')
    if store.read_migration_raw('release-successor.json') is not None:
        raise BridgeError('V1_SUCCESSOR_PENDING')
    if store.read_migration_raw('release-start-intent.json') is not None:
        raise BridgeError('INTENT_PENDING')
    intent = migration.load(store)
    if intent is not None and intent['phase'] != 'verified':
        raise BridgeError('MIGRATION_PENDING')
    return True


def build_control_manifest(*, entrypoints, files, dependency_policy_sha256,
                           recovery_generation_sha256):
    """Validate and canonicalize a ControlManifest; return bytes and closure digest."""
    value = {'schema_version': 'mcs.release-control-manifest.v3', 'owner_id': 'mcs.updater',
             'owner_revision': 2, 'entrypoints': list(entrypoints), 'files': list(files),
             'dependency_policy_sha256': dependency_policy_sha256,
             'recovery_generation_sha256': recovery_generation_sha256}
    try:
        canonical = contract.canonical_typed(value, 'ControlManifest')
    except contract.ContractError:
        raise BridgeError('MANIFEST_INVALID') from None
    return {'manifest': value, 'canonical': canonical,
            'sha256': hashlib.sha256(canonical).hexdigest()}


def initial_owner_value(*, scope, root_fingerprint, repository_fingerprint, activation_sha256,
                        control_closure_sha256, profile, lock_fingerprints,
                        installed_source_manifest_sha256, verified_source_manifest_sha256,
                        baseline, runtime_generation=1):
    """Construct the initial v3 OwnerState document; schema-validated before return.

    baseline is the ReleaseRef of the currently running release; all installed
    pointers and the single binding start from it.
    """
    ref = dict(baseline)
    value = {
        'schema_version': 'mcs.release-owner.v3', 'scope': dict(scope),
        'root_fingerprint': root_fingerprint,
        'repository_fingerprint': repository_fingerprint,
        'activation_sha256': activation_sha256,
        'control_closure_sha256': control_closure_sha256,
        'revision': 1, 'execution_revision': 1,
        'trust_json': '{}', 'legacy_evidence': [], 'operation_evidence': [],
        'lock_fingerprints': dict(lock_fingerprints),
        'installed_source_manifest_sha256': installed_source_manifest_sha256,
        'verified_source_manifest_sha256': verified_source_manifest_sha256,
        'profile': dict(profile), 'consent': None,
        'highest_seen': dict(ref), 'installed': dict(ref),
        'highest_installed': dict(ref), 'verified_installed': dict(ref),
        'runtime_generation': runtime_generation,
        'bindings': [{'code_seq': ref['code_seq'], 'release_id': ref['release_id']}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [],
        'prepare_budget': [],
    }
    try:
        contract.parse_document(json.dumps(value, ensure_ascii=True, separators=(',', ':'),
                                           allow_nan=False).encode('ascii'),
                                'OwnerState', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise BridgeError('BASELINE_INVALID') from None
    return value


def plan_migration(*, migration_id=None, root_fingerprint, repository_fingerprint,
                   source_schema, bridge_sha256, control_closure_sha256,
                   recovery_generation_sha256, external_floor_sha256, files,
                   human_receipt_id, created_at):
    """Build a schema-valid MigrationIntent at phase=prepared."""
    value = {'schema_version': 'mcs.release-owner-migration.v3',
             'migration_id': migration_id or str(uuid.uuid4()),
             'root_fingerprint': root_fingerprint,
             'repository_fingerprint': repository_fingerprint,
             'source_schema': source_schema, 'target_schema': 'mcs.release-owner.v3',
             'bridge_sha256': bridge_sha256, 'control_closure_sha256': control_closure_sha256,
             'recovery_generation_sha256': recovery_generation_sha256,
             'external_floor_sha256': external_floor_sha256, 'files': list(files),
             'human_receipt_id': human_receipt_id, 'created_at': created_at,
             'phase': 'prepared'}
    try:
        contract.parse_document(json.dumps(value, ensure_ascii=True, separators=(',', ':'),
                                           allow_nan=False).encode('ascii'),
                                'MigrationIntent', max_bytes=MAX_INTENT_BYTES)
    except contract.ContractError:
        raise BridgeError('INTENT_INVALID') from None
    return value


def drive(store, intent, *, archive, install_owner, install_q, verify):
    """Advance a MigrationIntent through its phases with injected side effects.

    archive(name, before_sha256) preserves exact old bytes; install_owner(doc) writes
    the v3 baseline; install_q() creates the Q inode once; verify() re-checks the
    deployed closure. Each hook must be idempotent: a crash resumes from the recorded
    phase and only the same intent/before/after is honoured.
    """
    value = migration.prepare(store, intent) if migration.load(store) is None else migration.load(store)
    _check(value['migration_id'] == intent['migration_id'], 'INTENT_MISMATCH')
    while value['phase'] != 'verified':
        phase = value['phase']
        if phase == 'prepared':
            for entry in value['files']:
                if entry['disposition'] in ('archive_exact', 'preserve', 'translate_verified'):
                    archive(entry['logical_id'], entry['before_sha256'])
            value = migration.advance(store, migration_id=value['migration_id'],
                                      phase='old_archived')
        elif phase == 'old_archived':
            install_owner()
            install_q()
            value = migration.advance(store, migration_id=value['migration_id'],
                                      phase='owner_replaced')
        elif phase == 'owner_replaced':
            _check(verify(), 'VERIFY_FAILED')
            value = migration.advance(store, migration_id=value['migration_id'],
                                      phase='verified')
    return value


# --- real side-effect hooks for drive() -------------------------------------
#
# archive_root defaults to a sibling of the control root so migration evidence
# never appears inside the Store-managed directory (classify_sidecars would
# refuse unknown entries). recovery_root is the existing independent recovery
# area; the closure lands in a content-addressed runtime-<sha256> generation.

_CLOSURE_DESCRIPTOR = 'sdc-update-control.json'
_MODE_TO_PERMISSION = {'100644': 0o644, '100755': 0o755}


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        os.close(fd)


def _write_private(path, raw, permission):
    """Private file write: O_EXCL create + fchmod + fsync; unlinks on failure."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, permission)
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(path)
        raise
    else:
        os.close(fd)


def _ensure_private_dir(path):
    """Create path and every missing ancestor with 0700, up to the first existing dir."""
    missing = []
    current = path
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        os.chmod(directory, 0o700)


def _safe_relpath(path):
    _check(isinstance(path, str) and 0 < len(path) <= 4096, 'MANIFEST_INVALID')
    _check(not path.startswith('/') and '\\' not in path
           and all(part not in ('', '.', '..') for part in path.split('/')),
           'MANIFEST_INVALID')
    return path


def archive_exact(store, archive_root, migration_id):
    """drive()のarchive hook実体: 旧sidecar bytesをmigration_id配下へ不変保存する。

    再開時は既存fileのbytesがbefore_sha256と一致することを確認してskipする。
    """
    target_dir = os.path.join(archive_root, migration_id)
    os.makedirs(target_dir, mode=0o700, exist_ok=True)
    os.chmod(target_dir, 0o700)

    def archive(name, before_sha256):
        raw = store.read_migration_raw(name)
        _check(raw is not None, 'ARCHIVE_MISSING')
        _check(_sha256(raw) == before_sha256, 'ARCHIVE_HASH_MISMATCH')
        dest = os.path.join(target_dir, name)
        if os.path.lexists(dest):
            _check(_sha256(_read_file(dest)) == before_sha256, 'ARCHIVE_CONFLICT')
            return
        _write_private(dest, raw, 0o600)
        _fsync_dir(target_dir)

    return archive


def install_owner(store, owner_value):
    """drive()のinstall_owner hook実体: v3 baselineをO lock下で原子的に配置する。"""
    def install():
        auto_state.install_initial(store, owner_value)
    return install


def install_q(store):
    """drive()のinstall_q hook実体: Q inodeを一度だけ作成する。既存は再利用。"""
    def install():
        try:
            os.stat('runtime-entry.lock', dir_fd=store.fd, follow_symlinks=False)
            return
        except FileNotFoundError:
            pass
        store.install_runtime_entry_lock()
    return install


def deploy_closure(manifest_value, canonical, source_root, recovery_root):
    """ControlManifestのclosureをcontent-addressed generationへ配備する。

    generation dirは `<recovery_root>/runtime-<closure_sha256>`。全fileを
    path/mode/sha256照合で配置し、manifest canonicalを`sdc-update-control.json`
    として同梱する。冪等: 同名同bytesはskip、異bytesはtmp+renameで修復する。
    """
    closure_sha = hashlib.sha256(canonical).hexdigest()
    generation = os.path.join(recovery_root, 'runtime-' + closure_sha)
    _ensure_private_dir(generation)
    for entry in manifest_value['files']:
        rel = _safe_relpath(entry['path'])
        source = os.path.join(source_root, *rel.split('/'))
        raw = _read_file(source)
        _check(_sha256(raw) == entry['sha256'], 'CLOSURE_SOURCE_HASH')
        _check(entry['mode'] in _MODE_TO_PERMISSION, 'MANIFEST_INVALID')
        dest = os.path.join(generation, *rel.split('/'))
        _ensure_private_dir(os.path.dirname(dest))
        permission = _MODE_TO_PERMISSION[entry['mode']]
        if os.path.lexists(dest):
            st = os.lstat(dest)
            if (stat.S_ISREG(st.st_mode) and (st.st_mode & 0o777) == permission
                    and _sha256(_read_file(dest)) == entry['sha256']):
                continue
        tmp = dest + '.tmp-' + os.urandom(8).hex()
        _write_private(tmp, raw, permission)
        os.replace(tmp, dest)
        _fsync_dir(os.path.dirname(dest))
    desc = os.path.join(generation, _CLOSURE_DESCRIPTOR)
    if not (os.path.lexists(desc) and not os.path.islink(desc)
            and _read_file(desc) == canonical):
        tmp = desc + '.tmp-' + os.urandom(8).hex()
        _write_private(tmp, canonical, 0o600)
        os.replace(tmp, desc)
        _fsync_dir(generation)
    return generation


def verify_closure(manifest_value, canonical, source_root, recovery_root):
    """drive()のverify hook実体: 配備を冪等に整えてからclosureを再検証する。"""
    generation = deploy_closure(manifest_value, canonical, source_root, recovery_root)
    expected = {_safe_relpath(e['path']): e for e in manifest_value['files']}
    seen = set()
    for dirpath, dirnames, filenames in os.walk(generation):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, generation)
            st = os.lstat(full)
            _check(stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode),
                   'VERIFY_FAILED')
            if rel == _CLOSURE_DESCRIPTOR:
                _check(_read_file(full) == canonical, 'VERIFY_FAILED')
                continue
            _check(rel in expected, 'VERIFY_FAILED')
            entry = expected[rel]
            _check((st.st_mode & 0o777) == _MODE_TO_PERMISSION[entry['mode']],
                   'VERIFY_FAILED')
            _check(_sha256(_read_file(full)) == entry['sha256'], 'VERIFY_FAILED')
            seen.add(rel)
    _check(seen == set(expected), 'VERIFY_FAILED')
    return True


def real_hooks(store, *, owner_value, manifest_value, canonical,
               source_root, recovery_root, archive_root, migration_id):
    """drive()へ渡す実副作用hook一式を束ねる。"""
    return {
        'archive': archive_exact(store, archive_root, migration_id),
        'install_owner': install_owner(store, owner_value),
        'install_q': install_q(store),
        'verify': lambda: verify_closure(manifest_value, canonical,
                                         source_root, recovery_root),
    }


# --- one-shot CLI (DEV01: separate approved maintenance operation) -----------

_SPEC_REQUIRED = ('control_root', 'source_root', 'recovery_root', 'scope',
                  'baseline', 'repository_fingerprint', 'activation_sha256',
                  'profile', 'lock_fingerprints',
                  'installed_source_manifest_sha256',
                  'verified_source_manifest_sha256', 'manifest',
                  'source_schema', 'bridge_sha256', 'external_floor_sha256',
                  'human_receipt_id', 'created_at')


def run_migration(spec):
    """spec dictからbridge全工程を駆動する。戻り値は結果summary dict。

    specは人間が用意する証拠bundle。構造・schema・hashはここで検証するが、
    baseline/profile/lock_fingerprints等の実値の真正性は上位の承認経路が担う。
    """
    missing = [key for key in _SPEC_REQUIRED if key not in spec]
    _check(not missing, 'SPEC_INCOMPLETE:' + ','.join(missing))
    store = Store(spec['control_root'])
    try:
        check_v1_blockers(store)
        files = classify_sidecars(store)
        manifest = build_control_manifest(**spec['manifest'])
        covered = {e['path'] for e in manifest['manifest']['files']}
        missing = sorted(RECOVERY_REQUIRED_CLOSURE - covered)
        _check(not missing, 'CLOSURE_INCOMPLETE:' + ','.join(missing))
        owner = initial_owner_value(
            scope=spec['scope'], root_fingerprint=store.root_fingerprint(),
            repository_fingerprint=spec['repository_fingerprint'],
            activation_sha256=spec['activation_sha256'],
            control_closure_sha256=manifest['sha256'],
            profile=spec['profile'], lock_fingerprints=spec['lock_fingerprints'],
            installed_source_manifest_sha256=spec['installed_source_manifest_sha256'],
            verified_source_manifest_sha256=spec['verified_source_manifest_sha256'],
            baseline=spec['baseline'])
        intent = plan_migration(
            root_fingerprint=store.root_fingerprint(),
            repository_fingerprint=spec['repository_fingerprint'],
            source_schema=spec['source_schema'],
            bridge_sha256=spec['bridge_sha256'],
            control_closure_sha256=manifest['sha256'],
            recovery_generation_sha256=spec['manifest']['recovery_generation_sha256'],
            external_floor_sha256=spec['external_floor_sha256'],
            files=files, human_receipt_id=spec['human_receipt_id'],
            created_at=spec['created_at'])
        archive_root = spec.get('archive_root') or os.path.join(
            os.path.dirname(store.root), store.root.name + '.migration-archive')
        hooks = real_hooks(
            store, owner_value=owner, manifest_value=manifest['manifest'],
            canonical=manifest['canonical'], source_root=spec['source_root'],
            recovery_root=spec['recovery_root'], archive_root=archive_root,
            migration_id=intent['migration_id'])
        # Bridge runs as a maintenance frame: once the Q inode exists, hold it
        # EX for the rest of drive() so no business entry can start mid-migration.
        q_ex = None
        base_install_q = hooks['install_q']

        def install_q_and_hold():
            nonlocal q_ex
            base_install_q()
            q_ex = store.runtime_entry_lock(shared=False)
            try:
                q_ex.__enter__()
            except OSError as error:
                q_ex = None
                raise BridgeError('Q_EX_BUSY') from error

        hooks['install_q'] = install_q_and_hold
        try:
            result = drive(store, intent, **hooks)
        finally:
            if q_ex is not None:
                q_ex.__exit__(None, None, None)
    finally:
        store.close()
    return {'phase': result['phase'], 'migration_id': intent['migration_id'],
            'control_closure_sha256': manifest['sha256'],
            'auto_eligible': False,
            'note': 'AUTO_ELIGIBLE remains false until external profile/worker/lock prerequisites are measured'}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description='v3 bridge: migrate release-owner v1 state to v3 (separate approved maintenance op)')
    sub = parser.add_subparsers(dest='command', required=True)
    migrate = sub.add_parser('migrate', help='run the one-shot v1->v3 bridge from a spec JSON')
    migrate.add_argument('--spec', required=True, help='path to the bridge evidence spec JSON')
    args = parser.parse_args(argv)
    if args.command == 'migrate':
        try:
            with open(args.spec, 'rb') as fh:
                raw = fh.read(1024 * 1024)
            spec = json.loads(raw)
            _check(isinstance(spec, dict), 'SPEC_INVALID')
            summary = run_migration(spec)
        except BridgeError as error:
            print(json.dumps({'ok': False, 'code': error.code}))
            return 2
        except (OSError, ValueError):
            print(json.dumps({'ok': False, 'code': 'SPEC_UNREADABLE'}))
            return 2
        print(json.dumps({'ok': True, **summary}, ensure_ascii=False))
        return 0
    return 2


if __name__ == '__main__':
    import sys
    sys.exit(main())
