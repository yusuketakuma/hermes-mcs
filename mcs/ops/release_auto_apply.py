"""v3 code-only effect (M5/W09): private capability→apply_code_only_v3。

STARTEDを耐久化した同一processだけが、issue_effect_capabilityが発行する
private live capability経由で一回だけ実行できる。公開CLIのboolean/JSON/IDは
capabilityの代用にならない(02_DEVICE_PROTOCOL §8)。

効果は固定bundleの制限Git環境でのff-only mergeのみ。失敗・不一致は自動
rollbackせずactive.phase=UNKNOWN＋Outcome unknownをoutboxへ記録する。
post-merge失敗でreset/clean/DB restoreは行わない(v1 bailとは別policy)。
"""
import hashlib
import json

from release_auto_owner import OwnerError, _det_uuid, _mutate
from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_state as auto_state

APPLY_GIT_TIMEOUT_S = 120.0
LOCK_SET = ('G', 'U', 'Q', 'R', 'C', 'A', 'M')

# git実行環境: system/global config・hook・fsmonitor・promptを全て排除する。
# local repo configの影響はdeps.git_inspectの監査で拒否側へ倒す。
_GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_SYSTEM': '/dev/null',
            'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_ASKPASS': '/bin/false',
            'GIT_SSH_COMMAND': '/bin/false',
            'GIT_EDITOR': '/bin/false',
            'GIT_PAGER': 'cat',
            'PAGER': 'cat'}

# git_inspectが返す監査項目のうち、存在自体を拒否するもの
_UNSAFE_GIT_FINDINGS = ('alternates', 'replace_refs', 'includes',
                        'hooks_path', 'fsmonitor', 'external_filter')

# measure_stateが返すべき不変計測項目(欠落はfail-closedでMISMATCH)
_MEASURE_KEYS = ('head', 'control_closure_sha256', 'config_contract_sha256',
                 'data_contract_sha256', 'installed_schema', 'profile_sha256')


class _ApplyMismatch(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


_SEAL = object()


class _EffectCapability:
    """issue_effect_capabilityだけが発行するprivate live capability。

    module外からはsealを持てないため構築できない。状態の再検証は
    apply_code_only_v3がlock下で別途行う(偽造されても束縛が合わなければ拒否)。
    """

    __slots__ = ('operation_id', 'grant_id', 'attempt_key', 'grant_sha256',
                 'prepare_nonce', 'source_target')

    def __init__(self, seal, **binding):
        if seal is not _SEAL:
            raise TypeError('capabilityはissue_effect_capability経由でのみ発行')
        for key in self.__slots__:
            setattr(self, key, binding[key])


def _spent_for(owner, operation_id):
    return next((s for s in owner['spent']
                 if s['operation_id'] == operation_id), None)


def _check_bindings(store, owner, active, capability=None):
    """intent/SPENT/active/capabilityの束縛一致を検査する。capability=Noneは発行時。"""
    intent = auto_state.load_intent(store)
    if (intent is None or intent['phase'] != 'open'
            or intent['operation_id'] != active['operation_id']):
        raise OwnerError('START_EVIDENCE_MISSING')
    spent = _spent_for(owner, active['operation_id'])
    if spent is None:
        raise OwnerError('SPENT_MISSING')
    cap_ok = (capability is None
              or (intent['grant_sha256'] == spent['grant_sha256']
                  == capability.grant_sha256
                  and intent['attempt_key'] == spent['attempt_key']
                  == active['attempt_key'] == capability.attempt_key
                  and intent['prepare_nonce'] == active['prepare_nonce']
                  == capability.prepare_nonce
                  and capability.source_target
                  == active['target']['source_commit']
                  == intent['source_target']))
    if not (intent['grant_id'] == spent['grant_id'] == active['start_receipt_id']
            and intent['attempt_key'] == spent['attempt_key']
            == active['attempt_key']
            and intent['prepare_nonce'] == active['prepare_nonce']
            and intent['source_target'] == active['target']['source_commit']
            and cap_ok):
        raise OwnerError('BINDING_MISMATCH')
    return intent, spent


def issue_effect_capability(store):
    """STARTED耐久化済みの同一processへ一回限りのeffect capabilityを発行する。

    crash後の読取りからは再発行しない(RAM capabilityのみ・SEM20)。
    """
    owner = auto_state.load(store)
    active = owner['active']
    if active is None or active['phase'] != 'STARTED':
        raise OwnerError('PHASE_MISMATCH')
    intent, spent = _check_bindings(store, owner, active)
    return _EffectCapability(_SEAL,
                             operation_id=active['operation_id'],
                             grant_id=intent['grant_id'],
                             attempt_key=active['attempt_key'],
                             grant_sha256=intent['grant_sha256'],
                             prepare_nonce=active['prepare_nonce'],
                             source_target=intent['source_target'])


def _git(deps, repo, *args):
    return deps.git_run(['-C', repo, *args], env=dict(_GIT_ENV),
                        timeout_s=APPLY_GIT_TIMEOUT_S)


def _require_git(result, code):
    if not isinstance(result, dict) or result.get('rc') != 0:
        raise _ApplyMismatch(code)
    return result


def _git_effect(deps, owner, active):
    """固定bundleの検査→制限環境fetch→ff-only merge→事後検証。"""
    target = active['target']
    bundle = deps.bundle_path(target)
    digest = deps.file_digest(bundle)
    if (not isinstance(digest, dict)
            or digest.get('sha256') != target['artifact_sha256']
            or digest.get('size') != target['artifact_size']):
        raise _ApplyMismatch('BUNDLE_MISMATCH')
    repo = deps.repository_path()
    findings = deps.git_inspect(repo)
    if not isinstance(findings, dict):
        raise _ApplyMismatch('GIT_ENV_UNSAFE')
    if any(findings.get(k) for k in _UNSAFE_GIT_FINDINGS):
        raise _ApplyMismatch('GIT_ENV_UNSAFE')
    commit = target['source_commit']
    _require_git(_git(deps, repo, 'bundle', 'verify', bundle), 'BUNDLE_INVALID')
    _require_git(_git(deps, repo, 'fetch', '--no-tags', bundle, 'refs/*'),
                 'FETCH_FAILED')
    _require_git(_git(deps, repo, 'cat-file', '-e', commit + '^{commit}'),
                 'OBJECT_MISSING')
    head = _require_git(_git(deps, repo, 'rev-parse', 'HEAD'),
                        'HEAD_UNKNOWN').get('stdout', '').strip()
    if head != commit:
        # ff-only事前条件: HEADがtargetの祖先であること。既にtargetなら
        # crash後の再適用としてmergeをskipし事後検証へ進む。
        ancestor = _git(deps, repo, 'merge-base', '--is-ancestor', 'HEAD', commit)
        if not isinstance(ancestor, dict) or ancestor.get('rc') != 0:
            raise _ApplyMismatch('NOT_FF_ONLY')
        _require_git(_git(deps, repo, '-c', 'core.hooksPath=/dev/null',
                          'merge', '--ff-only', commit), 'MERGE_FAILED')
    head2 = _require_git(_git(deps, repo, 'rev-parse', 'HEAD'),
                         'HEAD_UNKNOWN').get('stdout', '').strip()
    if head2 != commit:
        raise _ApplyMismatch('HEAD_MISMATCH')
    # tracked tree/indexの不変を検査(untracked/ignoredはmerge失敗側で処理済み)
    _require_git(_git(deps, repo, 'diff-index', '--quiet', 'HEAD', '--'),
                 'TREE_DIRTY')


def _check_postconditions(deps, owner, active):
    """APPLIED前の不変項目再測定。control closure・契約・schema・profileは不変必須。"""
    measured = deps.measure_state()
    if not isinstance(measured, dict):
        raise _ApplyMismatch('MEASURE_MISSING')
    if any(k not in measured for k in _MEASURE_KEYS):
        raise _ApplyMismatch('MEASURE_MISSING')
    public = active['private_witness']['public']
    expected = {'head': active['target']['source_commit'],
                'control_closure_sha256': public['control_closure_sha256'],
                'config_contract_sha256': public['config_contract_sha256'],
                'data_contract_sha256': public['data_contract_sha256'],
                'installed_schema': public['installed_schema'],
                'profile_sha256': owner['profile']['profile_sha256']}
    for key, want in expected.items():
        if measured[key] != want:
            raise _ApplyMismatch('INVARIANT_' + key.upper())


def _unknown_outcome(store, deps, owner, active, spent):
    """Outcome unknownを固定rawでoutboxへ記録する(ACKはdeliver_outboxが担う)。

    report_sequenceは同operationの既存report数+1。report_id/request_nonceは
    決定論的で、crash後の再構築でも同一bytesにはならないがreport_id一意で
    OUTBOX_DUPLICATEを防ぐ。local_proof_idは証拠不在を示す決定論的uuid。
    """
    operation_id = active['operation_id']
    seq = sum(1 for e in owner['outbox']
              if e['operation_id'] == operation_id) + 1
    report_id = _det_uuid(f'outcome|{operation_id}|{seq}')
    outcome = {'schema_version': 'mcs.outcome.v3', 'report_id': report_id,
               'request_nonce': hashlib.sha256(
                   ('outcome-nonce|' + report_id).encode()).hexdigest(),
               'report_sequence': seq, 'scope': owner['scope'],
               'operation_id': operation_id, 'grant_id': spent['grant_id'],
               'attempt_key': active['attempt_key'], 'target': active['target'],
               'installed': owner['installed'],
               'verified_installed': owner['verified_installed'],
               'state': 'unknown', 'readiness': 'unknown',
               'runtime_generation': owner['runtime_generation'],
               'started_at': spent['started_at'],
               'observed_at': deps.now_ms(), 'verified_at': None,
               'local_proof_id': _det_uuid(
                   f'local-proof-absent|{operation_id}|{seq}'),
               'error_code': 'RESULT_UNKNOWN', 'rollout': active['rollout']}
    outcome = contract.parse_typed(json.dumps(outcome).encode(), 'Outcome')
    # ACKのoutcome_sha256は中央がcanonicalAuto hashで返す(SEM27) — 送信bytesを
    # canonical形に揃え、保存sha256と中央hashを一致させる。
    raw = contract.canonical_typed(outcome, 'Outcome')
    entry = {'report_id': report_id, 'operation_id': operation_id,
             'raw': raw.decode('utf-8'),
             'sha256': hashlib.sha256(raw).hexdigest(), 'acknowledged': False}
    try:
        auto_state.append_outbox(store, entry)
    except auto_state.OwnerStateError as e:
        if getattr(e, 'code', None) != 'OUTBOX_DUPLICATE':
            raise


def _mark_unknown(store, deps, operation_id, code):
    """activeをUNKNOWNへ終端記録しOutcome unknownをoutboxへ送る。rollbackしない。"""
    def mark(new):
        a = new['active']
        if (a is None or a['operation_id'] != operation_id
                or a['phase'] != 'STARTED'):
            raise OwnerError('PHASE_MISMATCH')
        a['phase'] = 'UNKNOWN'
        a['error_code'] = code

    owner = _mutate(store, mark)
    active = owner['active']
    spent = _spent_for(owner, operation_id)
    if spent is not None:
        _unknown_outcome(store, deps, owner, active, spent)


def apply_code_only_v3(store, capability, deps):
    """DEV40手前のcode-only effect: STARTED→APPLIED。失敗はUNKNOWNで保持。

    1. capability型とSTARTED/intent/SPENT/束縛をlock前後で再検証
    2. bundle hash・size照合→制限Git環境でfetch→ff-only merge
    3. HEAD/tree/index・control closure・契約・schema・profileを再測定
    4. APPLIEDをownerへ保存しjournalへ記録。途中不一致はUNKNOWN・自動rollbackなし
    """
    if not isinstance(capability, _EffectCapability):
        raise OwnerError('CAPABILITY_INVALID')
    owner = auto_state.load(store)
    active = owner['active']
    _check_started(owner, store, active, capability)
    _check_gates(deps, owner)

    with deps.acquire_locks(LOCK_SET):
        owner = auto_state.load(store)
        active = owner['active']
        _check_started(owner, store, active, capability)
        _check_gates(deps, owner)
        try:
            _git_effect(deps, owner, active)
            _check_postconditions(deps, owner, active)
        except _ApplyMismatch as e:
            _mark_unknown(store, deps, active['operation_id'], e.code)
            raise OwnerError(e.code) from None

        def applied(new):
            a = new['active']
            if (a is None or a['operation_id'] != active['operation_id']
                    or a['phase'] != 'STARTED'):
                raise OwnerError('PHASE_MISMATCH')
            a['phase'] = 'APPLIED'
            a['candidate_generation'] = new['runtime_generation'] + 1

        _mutate(store, applied)
        # journalはeffect位置の補助記録。owner正本のAPPLIED確定後に記録する。
        try:
            deps.journal_apply({
                'kind': 'release-auto-v3', 'event': 'applied',
                'operation_id': active['operation_id'],
                'grant_id': capability.grant_id,
                'attempt_key': capability.attempt_key,
                'source_commit': active['target']['source_commit'],
                'applied_at': deps.now_ms()})
        except Exception as e:
            raise OwnerError('JOURNAL_FAILED') from e
    return {'phase': 'APPLIED', 'operation_id': active['operation_id']}


def _check_started(owner, store, active, capability):
    if (active is None or active['phase'] != 'STARTED'
            or active['private_witness'] is None):
        raise OwnerError('PHASE_MISMATCH')
    if (capability.operation_id != active['operation_id']
            or capability.attempt_key != active['attempt_key']
            or capability.prepare_nonce != active['prepare_nonce']):
        raise OwnerError('CAPABILITY_MISMATCH')
    _check_bindings(store, owner, active, capability)


def _check_gates(deps, owner):
    consent = owner['consent']
    if not (isinstance(consent, dict) and consent.get('enabled') is True
            and isinstance(consent.get('expires_at'), int)
            and consent['expires_at'] > deps.now_ms()):
        raise OwnerError('CONSENT_REQUIRED')
    if deps.local_revoke():
        raise OwnerError('CONSENT_REVOKED')
    holds = deps.holds()
    if holds:
        raise OwnerError('HOLD_PRESENT')
