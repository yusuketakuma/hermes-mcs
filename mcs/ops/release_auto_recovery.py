"""v3 recovery controller (M7): 観測的終結とclosure世代の一式検証。

復旧controllerは本体checkoutをimportせず、bridgeが配備した固定closure
(recovery_root/runtime-<closure_sha256>)の自分の世代だけを使う
(02_DEVICE_PROTOCOL §11)。entrypoint+files+manifest descriptorを一式照合し、
旧.prev混成・未知file・hash不一致は全て拒否する(AT086-088)。

観測的終結は既に完了した同一操作のVERIFIED確定まで。APPLIED以降のphaseだけを
advance_runtime経由で前進させ、STAGED以前・UNKNOWN・v1 pendingは一切触らない
(新規grant/effect/コード変更/DB復元は別承認保守の領域)。
"""
import hashlib
import os
import stat

from mcs.ops import release_auto_bridge as bridge
from adapters.sdc import release_auto_state as auto_state

# closure内でrecoveryが必要とする最小file集合(descriptorを除く)。
# manifest自体はfilesに含めない(04 §4.3)。定義はbridge側の単一正本。
_REQUIRED_CLOSURE = bridge.RECOVERY_REQUIRED_CLOSURE


class RecoveryError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _check(ok, code='RECOVERY_HOLD'):
    if not ok:
        raise RecoveryError(code)


def verify_generation(generation_dir, manifest_value, canonical):
    """配備済みclosure世代を一式照合する。source_rootは読まない(本体非依存)。

    - generation_dirが`runtime-<sha256(canonical)>`名と一致
    - descriptor(sdc-update-control.json)のbytes==canonical
    - manifest files全件がpath/mode/sha256一致で存在
    - 未知file・symlink・欠落・hash不一致は拒否(.prev混成を許さない)
    """
    _check(isinstance(generation_dir, str) and generation_dir, 'GENERATION_INVALID')
    expected_sha = hashlib.sha256(canonical).hexdigest()
    _check(os.path.basename(os.path.normpath(generation_dir))
           == 'runtime-' + expected_sha, 'GENERATION_NAME')
    descriptor = os.path.join(generation_dir, bridge._CLOSURE_DESCRIPTOR)
    _check(os.path.isfile(descriptor) and not os.path.islink(descriptor),
           'GENERATION_DESCRIPTOR')
    try:
        with open(descriptor, 'rb') as f:
            _check(f.read() == canonical, 'GENERATION_DESCRIPTOR')
    except OSError:
        raise RecoveryError('GENERATION_DESCRIPTOR') from None
    expected = {bridge._safe_relpath(e['path']): e
                for e in manifest_value['files']}
    seen = set()
    for dirpath, dirnames, filenames in os.walk(generation_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, generation_dir)
            st = os.lstat(full)
            _check(stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode),
                   'GENERATION_TAMPER')
            if rel == bridge._CLOSURE_DESCRIPTOR:
                continue
            _check(rel in expected, 'GENERATION_TAMPER')
            entry = expected[rel]
            _check((st.st_mode & 0o777)
                   == bridge._MODE_TO_PERMISSION[entry['mode']],
                   'GENERATION_TAMPER')
            try:
                with open(full, 'rb') as f:
                    raw = f.read()
            except OSError:
                raise RecoveryError('GENERATION_TAMPER') from None
            _check(hashlib.sha256(raw).hexdigest() == entry['sha256'],
                   'GENERATION_TAMPER')
            seen.add(rel)
    _check(seen == set(expected), 'GENERATION_INCOMPLETE')
    _check(_REQUIRED_CLOSURE <= seen, 'GENERATION_INCOMPLETE')
    return True


def generation_for(canonical, recovery_root):
    """canonicalに対応する唯一の配備世代dirを返す。無ければNone。"""
    name = 'runtime-' + hashlib.sha256(canonical).hexdigest()
    path = os.path.join(recovery_root, name)
    return path if os.path.isdir(path) else None


def observe_status(store):
    """read-onlyのphase報告。W14/status表示が使う。状態は一切変更しない。"""
    owner = auto_state.load(store)
    active = owner['active']
    if active is None:
        return {'action': 'idle', 'phase': None,
                'verified_installed': owner.get('verified_installed')}
    return {'action': 'observe', 'phase': active['phase'],
            'operation_id': active['operation_id'],
            'candidate_generation': active.get('candidate_generation'),
            'runtime_proof': active.get('runtime_proof') is not None}


def recover_tick(store, deps):
    """1 tickの観測的終結。APPLIED以降だけを前進させ、それ以外はhold報告。

    新規offer/grant/effect・v1 pending成功化・コード変更は行わない。
    depsはrelease_runtime_proof.advance_runtimeと同じ注入面
    (runtime_advance hookを持つowner deps、またはadvance_runtimeを
    直接呼べるcallableをdeps.runtime_advanceへ束ねたもの)。
    """
    owner = auto_state.load(store)
    active = owner['active']
    if active is None:
        return {'action': 'idle'}
    phase = active['phase']
    if phase not in ('APPLIED', 'CANDIDATE_READY', 'ACTIVATING'):
        # STAGED以前・STARTED・UNKNOWN: 観測のみ。新grantもeffectも出さない。
        return {'action': 'hold', 'phase': phase,
                'reason': 'BEFORE_RUNTIME_PROOF'}
    advance = getattr(deps, 'runtime_advance', None)
    if advance is None:
        return {'action': 'hold', 'phase': phase, 'reason': 'NO_ADVANCE_DEPS'}
    try:
        result = advance(store)
    except Exception as e:
        code = getattr(e, 'code', None) or type(e).__name__
        return {'action': 'hold', 'phase': phase, 'code': code}
    if isinstance(result, dict):
        return {'action': result.get('action') or result.get('phase',
                                                         'advanced'),
                'phase': phase}
    return {'action': 'advanced', 'phase': phase}
