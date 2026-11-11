"""v3 owner deps factory: canonical runtime pathから全deps面を組み立てる。

build_deps()はowner/apply/runtime-proofが要求するdeps名前空間を、
実Store・時計・lock・stage・git・wire・service drain・private witness・
apply/runtime-proof hookの合成として返す。配備依存値(transport/
credential/pin・backup policy・quarantine構成・contract digest規則・
witness副provider)は呼出側が明示注入し、未設定面は全てfail-closed
stub(WIRE_UNAVAILABLE/OwnerError/BACKUP_*等) — 未設定を成功扱いしない。

canonical path解決(全てmcs_util/runtime_entry/service_contract正本):
- control_root = runtime_entry.control_root(runtime_home)
- data_dir    = HOME/data、ledger = data_dir/ledger.db
- monitor_root= cfg['sdc']['state_root'](未設定=None→M lock取得不可)
- stage_root  = control_root/'stage'
- repository  = mcs_util.REPO(MCS_UPDATE_REPO優先の既存解決)
- agents_dir/uid = service_management.default_paths

service drain既定labelはservice_contract正本から導出:
hermes=RESIDENT_LABELS+設定時LINEWORKS_LABEL、standalone/空は
未対応面としてfail-closed stub(standalone supervisor drainは別binding)。
"""
import functools
import hashlib
import json
import os
import stat
import time
import types

from adapters.sdc import release_auto_state as auto_state
from release_auto_owner import OwnerError
from release_auto_services import ServiceDrain, ServiceError

_UNSET = object()
_WITNESS_FILE_MAX = 4 * 1024 * 1024
_DEFAULT_RESERVED_OWNER_BYTES = 8 * 1024 * 1024  # spec 03 schema上限=保守的既定
_RUNTIME_DIRS = ('mcs', 'adapters', 'mcs_standalone', 'hermes_plugin',
                 'lineworks_adapter')


class _Unavailable:
    """未注入deps面のfail-closed stub。codeは各面のerror型で固定token。"""

    def __init__(self, error, code):
        self._error = error
        self._code = code

    def __call__(self, *args, **kwargs):
        raise self._error(self._code)

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise self._error(self._code)
        return fail


def _unavailable_wire():
    from release_auto_wire import WireError
    return _Unavailable(WireError, 'WIRE_UNAVAILABLE')


def _unavailable_backup():
    from release_auto_backup import BackupEvidenceError
    return _Unavailable(BackupEvidenceError, 'BACKUP_UNAVAILABLE')


def _unavailable_digests():
    def fail():
        raise OwnerError('CONTRACT_DIGESTS_UNAVAILABLE')
    return fail


def _service_drain_unavailable():
    """drain非対応runtime(standalone等)のfail-closed面。"""
    def fail(*args, **kwargs):
        raise ServiceError('SERVICE_UNAVAILABLE')
    return types.SimpleNamespace(
        stop_new_intake=fail, managed_services=fail, request_stop=fail,
        await_retirement=fail, retirement_complete=lambda *a: False,
        restart_same_version=fail, last_evidence=None)


def _default_drain_labels(cfg):
    """service_contract正本からdrain対象labelを導出。None=未対応。"""
    import mcs_runtime
    import service_contract
    if mcs_runtime.standalone(cfg):
        return None
    labels = list(service_contract.RESIDENT_LABELS)
    ntf = cfg.get('notify')
    if isinstance(ntf, dict) and ntf.get('interactive') == 'lineworks':
        labels.append(service_contract.LINEWORKS_LABEL)
    return labels


def _file_witness(path, logical_id):
    """私有fileのFileIdentity+内容sha256。openしたfd自身をfstatし、
    identityとhashが同一inodeを指すことを保証する(symlink/TOCTOU排除)。"""
    from release_auto_witness import WitnessError
    if not isinstance(path, str) or not path:
        raise WitnessError('WITNESS_FILES')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise WitnessError('WITNESS_FILES') from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise WitnessError('WITNESS_FILES')
        h = hashlib.sha256()
        remaining = _WITNESS_FILE_MAX + 1
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            while remaining > 0:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise WitnessError('WITNESS_FILES')
        return {'logical_id': logical_id, 'identity': st,
                'sha256': h.hexdigest()}
    finally:
        os.close(fd)


def _stage_witness(stage, active):
    """staged artifactのpath+FileIdentity(active['target']から導出)。"""
    from release_auto_witness import WitnessError
    target = active.get('target') if isinstance(active, dict) else None
    try:
        path = stage.bundle_path(target)
    except Exception as e:
        raise WitnessError('WITNESS_STAGE') from e
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise WitnessError('WITNESS_STAGE') from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise WitnessError('WITNESS_STAGE')
        return {'path': path, 'identity': st}
    finally:
        os.close(fd)


def _runtime_fingerprint(repo):
    """配備runtime dirsのfile manifest digest(measure_tree正本を再利用)。

    sorted({dir: digest})のcanonical sha256 — working tree実体の証拠。
    """
    from release_quarantine import measure_tree
    entries = {}
    for name in _RUNTIME_DIRS:
        root = os.path.join(repo, name)
        if os.path.isdir(root):
            entries[name] = measure_tree(root)
    if not entries:
        raise OwnerError('SOURCE_UNOBSERVED')
    canonical = json.dumps(
        entries, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


def _native_source_facts(git, repo):
    """source_tree_sha256(committed tree)とruntime_fingerprint(配備実体)。"""
    result = git.run(['-C', repo, 'ls-tree', '-r', '-z', 'HEAD'],
                     timeout_s=30)
    if not isinstance(result, dict) or result.get('rc') != 0:
        raise OwnerError('SOURCE_UNOBSERVED')
    tree_sha = hashlib.sha256(
        str(result.get('stdout', '')).encode('utf-8')).hexdigest()
    return {'source_tree_sha256': tree_sha,
            'runtime_fingerprint': _runtime_fingerprint(repo)}


def _required_free(store):
    """prepare時点のactive targetに対する必要空き容量(stage時capacity_okと同見積り)。"""
    owner = auto_state.load(store)
    active = owner.get('active') if isinstance(owner, dict) else None
    target = active.get('target') if isinstance(active, dict) else None
    size = target.get('artifact_size') if isinstance(target, dict) else None
    if type(size) is not int or size < 0:
        raise OwnerError('CAPACITY_UNOBSERVED')
    return size * 2


def _runtime_generation(store):
    """RetiredProcess.generationへ写す現行runtime_generation(呼出毎fresh)。"""
    def generation():
        owner = auto_state.load(store)
        value = owner.get('runtime_generation') \
            if isinstance(owner, dict) else None
        if type(value) is not int or value <= 0:
            raise ServiceError('SERVICE_EVIDENCE')
        return value
    return generation


def _marker_restart():
    """drain対象0件時の同版再起動 — intake marker除去のみ(冪等)。"""
    from update_lifecycle_context import MARKER_PATH, _remove_marker
    _remove_marker()
    if os.path.lexists(MARKER_PATH):
        raise ServiceError('SERVICE_RESTART')


def build_deps(*, store, cfg=None, runtime_home=None, paths=None,
               drain_labels=_UNSET, stage_root=None, repository_path=None,
               data_dir=None, ledger_path=None, capacity_path=None,
               config_files=None, monitor_root=_UNSET,
               wire=None, contract_digests=None,
               backup_evidence=None, backup_detail=None,
               quarantine=None, local=None, git=None,
               reserved_owner_bytes=_UNSET,
               source_facts=None, file_witnesses=None,
               required_free_bytes=None,
               uid=None, run=None, sleep=None):
    """deps契約の全surfaceを組み立てて返す。storeは呼出側が開いたowner Store。

    未注入の任意面はfail-closed stubになる。構造pathはcanonical解決、
    配備値(transport/credential/pin・backup・quarantine・digest規則・
    witness副provider)だけが引数。
    """
    import mcs_util
    import release_auto_apply as apply_mod
    import release_auto_drain
    import release_auto_git
    import release_auto_journal
    import release_auto_locks
    import release_auto_offer
    import release_auto_stage
    import release_auto_witness as witness_mod
    import runtime_entry
    from adapters.sdc.storage import Store

    if cfg is None:
        cfg = mcs_util.load_config()
    if not isinstance(cfg, dict):
        raise OwnerError('CONFIG_INVALID')
    home = os.fspath(runtime_home) if runtime_home is not None \
        else mcs_util.HOME
    control_root = str(runtime_entry.control_root(home))
    data_dir = os.fspath(data_dir) if data_dir is not None \
        else os.path.join(home, 'data')
    ledger_path = os.fspath(ledger_path) if ledger_path is not None \
        else os.path.join(data_dir, 'ledger.db')
    capacity_path = os.fspath(capacity_path) if capacity_path is not None \
        else data_dir
    config_files = ([os.fspath(p) for p in config_files]
                    if config_files is not None else [mcs_util.CONF_PATH])
    if monitor_root is _UNSET:
        sdc = cfg.get('sdc')
        monitor_root = sdc.get('state_root') \
            if isinstance(sdc, dict) else None
    stage_root = os.fspath(stage_root) if stage_root is not None \
        else os.path.join(control_root, 'stage')

    if local is None:
        reserved = (_DEFAULT_RESERVED_OWNER_BYTES
                    if reserved_owner_bytes is _UNSET
                    else reserved_owner_bytes)
        from release_auto_local import LocalFacts
        local = LocalFacts(capacity_path=capacity_path,
                           ledger_path=ledger_path,
                           reserved_owner_bytes=reserved)
    if git is None:
        git = release_auto_git.Git(repository_path)
    repo = git.repository_path()

    locks = release_auto_locks.NativeLocks(
        control_root=control_root, monitor_root=monitor_root,
        data_dir=data_dir)
    stage = release_auto_stage.Stage(stage_root=stage_root,
                                     git_run=git.run)

    if paths is None:
        import service_management
        paths = service_management.default_paths(root=home, checkout=repo)

    # --- service drain ----------------------------------------------------
    if drain_labels is _UNSET:
        drain_labels = _default_drain_labels(cfg)
    drain_impl = None
    if drain_labels:
        drain_impl = release_auto_drain.native_launchd_drain(
            drain_labels,
            uid=os.getuid() if uid is None else uid,
            runtime_generation=_runtime_generation(store),
            boot_id=local.boot_id, run=run)
        service_drain = ServiceDrain(
            services=drain_impl.services, stoppers=drain_impl.stopper,
            probers=drain_impl.prober,
            restarter=release_auto_drain.native_restart_same_version(
                drain_impl, agents_dir=paths.agents_dir, run=run),
            intake_stop=release_auto_drain.native_intake_stop(),
            now_ms=local.now_ms, mono=local.mono,
            sleep=time.sleep if sleep is None else sleep)
    elif drain_labels is not None and len(drain_labels) == 0:
        # 明示的空drain対象: process退役は空証明で完結するが、
        # intake marker停止/除去は実施する
        service_drain = ServiceDrain(
            services=lambda: [], stoppers=lambda svc: None,
            probers=lambda svc: {'retired': True},
            restarter=_marker_restart,
            intake_stop=release_auto_drain.native_intake_stop(),
            now_ms=local.now_ms, mono=local.mono,
            sleep=time.sleep if sleep is None else sleep)
    else:
        service_drain = _service_drain_unavailable()

    # --- private witness providers ----------------------------------------
    if monitor_root:
        m_lock = os.path.join(monitor_root, 'tick.lock')
    else:
        m_lock = None
    lock_paths = [
        ('G', os.path.join(control_root, 'release-entry.lock')),
        ('U', os.path.join(data_dir, 'update.lock')),
        ('Q', os.path.join(control_root, 'runtime-entry.lock')),
        ('R', os.path.join(data_dir, 'run.lock')),
        ('C', str(mcs_util.CONF_PATH) + '.edit.lock'),
        ('A', os.path.join(control_root, 'release-authority.lock')),
        ('M', m_lock),
        ('O', os.path.join(control_root, 'release-owner.lock'))]

    def _default_file_witnesses():
        locks_out = [_file_witness(path, lid)
                     for lid, path in lock_paths]
        configs_out = [_file_witness(path, 'config:' + os.path.basename(path))
                       for path in config_files]
        return {'locks': locks_out, 'configs': configs_out}

    def _default_stage_stat(active):
        return _stage_witness(stage, active)

    def _default_retired():
        if drain_impl is None or service_drain.last_evidence is None:
            raise ServiceError('SERVICE_EVIDENCE')
        return drain_impl.retired_processes(service_drain.last_evidence)

    def _default_backup_detail():
        raise RuntimeError('BACKUP_DETAIL_UNAVAILABLE')

    witness = witness_mod.PrivateWitness(
        store,
        file_witnesses=(file_witnesses if file_witnesses is not None
                        else _default_file_witnesses),
        source_facts=(source_facts if source_facts is not None
                      else (lambda: _native_source_facts(git, repo))),
        ledger_stat=lambda: os.stat(ledger_path, follow_symlinks=False),
        stage_stat=_default_stage_stat,
        backup_detail=(backup_detail if backup_detail is not None
                       else _default_backup_detail),
        retired_processes=_default_retired,
        required_free_bytes=(required_free_bytes
                             if required_free_bytes is not None
                             else functools.partial(_required_free, store)),
        reserved_owner_bytes=local.reserved_owner_bytes)

    # --- deps名前空間 ------------------------------------------------------
    if wire is None:
        wire = _unavailable_wire()
    if contract_digests is None:
        digests = _unavailable_digests()
    else:
        digests = contract_digests
    if backup_evidence is None:
        backup_fn = _unavailable_backup()
    else:
        backup_fn = backup_evidence
    from release_auto_holds import NativeHolds
    from release_auto_revoke import LocalRevoke
    deps = types.SimpleNamespace(
        # transport/wire
        signed_send=wire.signed_send, verify=wire.verify,
        fetch_artifact=wire.fetch_artifact,
        release_metadata=wire.release_metadata,
        # offer/stage/git
        offer_eligible=release_auto_offer.OfferEligible(),
        stage_sink=stage.stage_sink, stage_verify=stage.stage_verify,
        bundle_path=stage.bundle_path, file_digest=stage.file_digest,
        git_run=git.run, git_inspect=git.git_inspect,
        repository_path=git.repository_path,
        # local facts
        now_ms=local.now_ms, now_s=local.now_s, mono=local.mono,
        boot_id=local.boot_id, capacity_ok=local.capacity_ok,
        installed_schema=local.installed_schema,
        reserved_owner_bytes=local.reserved_owner_bytes,
        # holds/revoke/backup/offer/digests
        holds=NativeHolds(data_dir).holds,
        local_revoke=LocalRevoke(
            lambda: Store(control_root)),
        backup_evidence=backup_fn,
        contract_digests=digests,
        # service drain
        stop_new_intake=service_drain.stop_new_intake,
        managed_services=service_drain.managed_services,
        request_stop=service_drain.request_stop,
        await_retirement=service_drain.await_retirement,
        retirement_complete=service_drain.retirement_complete,
        restart_same_version=service_drain.restart_same_version,
        # locks/evidence/witness
        acquire_locks=locks.acquire_locks, q_exclusive=locks.q_exclusive,
        evidence_store=lambda raw: _store_evidence(home, raw),
        evidence_load=lambda key: _load_evidence(home, key),
        witness_private=witness.witness_private)

    # --- apply/runtime-proof hook(late binding: deps自身を参照) -------------
    def _apply_effect(s):
        return apply_mod.apply_code_only_v3(
            s, apply_mod.issue_effect_capability(s), deps)

    deps.apply_effect = _apply_effect
    deps.measure_state = lambda: _measure_state(
        git=git, repo=repo, store=store, digests=digests, local=local)
    deps.journal_apply = lambda record: \
        release_auto_journal.apply_journal(
            record, state_path=release_auto_journal.journal_state_path(
                data_dir))
    if quarantine is not None:
        for name in ('service_desires', 'release_update_fence',
                     'quarantine_launch', 'probe_runtime',
                     'heartbeat_observations', 'loaded_source_expect',
                     'measure_source_manifest'):
            setattr(deps, name, getattr(quarantine, name))
        import release_runtime_proof
        deps.runtime_advance = lambda s: \
            release_runtime_proof.advance_runtime(s, deps)
    return deps


def _store_evidence(runtime_home, raw):
    from release_auto_evidence import store_evidence
    return store_evidence(runtime_home, raw)


def _load_evidence(runtime_home, object_key):
    from release_auto_evidence import load_evidence
    return load_evidence(runtime_home, object_key)


def _measure_state(*, git, repo, store, digests, local):
    """apply後の不変項目再測定(head+owner不変項目+契約digest+schema+profile)。

    deps実装の例外はOwnerError/OwnerStateError系のみ許容 — その他の
    観測失敗はMEASURE_FAILEDへ正規化し、apply側のhold経路へ載せる。
    """
    import re
    try:
        result = git.run(['-C', repo, 'rev-parse', 'HEAD'], timeout_s=10)
        head = result.get('stdout', '').strip() \
            if isinstance(result, dict) and result.get('rc') == 0 else ''
        if re.fullmatch(r'[0-9a-f]{40}', head) is None:
            raise OwnerError('MEASURE_HEAD')
        owner = auto_state.load(store)
        if not isinstance(owner, dict):
            raise OwnerError('MEASURE_MISSING')
        values = digests()
        if not isinstance(values, dict):
            raise OwnerError('MEASURE_MISSING')
        profile = owner.get('profile')
        profile_sha = profile.get('profile_sha256') \
            if isinstance(profile, dict) else None
        return {'head': head,
                'control_closure_sha256': owner.get('control_closure_sha256'),
                'config_contract_sha256': values.get('config_contract_sha256'),
                'data_contract_sha256': values.get('data_contract_sha256'),
                'installed_schema': local.installed_schema(),
                'profile_sha256': profile_sha}
    except (OwnerError, auto_state.OwnerStateError):
        raise
    except Exception as e:
        raise OwnerError('MEASURE_FAILED') from e
