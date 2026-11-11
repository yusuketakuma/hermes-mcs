"""v3 owner orchestration (M4): outbox配送・stage・prepare/drain・grant・consume。

spec 02§3–§9・04 W08前半。外部作用(通信・process・lock・時計・stage配置)は全て
depsへ注入し、本moduleは状態遷移と契約recordの構築・照合だけを行う。製品配線は
後続milestone。UNKNOWN/DEFERREDを成功へ推測せず、証拠不足は保留(fail-closed)。

Deps contract (全て必須、testはfakeを注入):
  signed_send(path, raw_bytes) -> response_bytes   # 署名+POST、非200は例外
  verify(path, response_bytes) -> typed dict        # SignedEnvelope剥がし+契約parse
  offer_eligible(offer, owner) -> bool              # cohort/互換/closure/最小updater判定
  fetch_artifact(request_raw, *, expected_sha256, expected_size, sink) -> None
                                                 # bounded stream+size/hash検証、sinkへ書込み
  stage_sink(target) -> binary file-like           # 私有stage先(呼出毎に新規)
  stage_verify(target, sink) -> bool               # stage内容の追加検査
  capacity_ok(required_bytes) -> bool
  backup_evidence() -> uuid str                     # local_backup_evidence_id
  contract_digests() -> {'config_contract_sha256','data_contract_sha256'}
  installed_schema() -> Positive int
  boot_id() -> Sha256                               # 仮名boot識別子
  witness_private(active) -> dict                   # PrivatePreparedWitnessの
      # public/schema_version以外の必須項目全て(stage_identity/path, backup_*,
      # lock_identities(8), configuration_files, retired_processes, prepared_until等)
  reserved_owner_bytes() -> int                     # 1..8388608
  holds() -> list[str]                              # 未解決hold一覧(空=なし)
  local_revoke() -> bool                            # 同意撤回の最新確認
  stop_new_intake()
  managed_services() -> list                        # drain対象の管理service記述子
  request_stop(service)
  await_retirement(deadline_epoch_ms) -> evidence   # 全員退役まで待機
  retirement_complete(evidence, services) -> bool
  restart_same_version()                            # 中断prepareの安全復帰(冪等)
  q_exclusive() -> ctx mgr                          # Q EX取得(SH保持者0を証明)
  acquire_locks(names) -> ctx mgr                   # ('G','U','Q','R','C','A','M')
      # Oはstate関数が各mutationで取得する(DAG順 G→…→M→O を維持)
  release_metadata(target, profile) -> dict         # 検証済み公開metadata:
      # {'publication_id','manifest_digest','compatibility_sha256','rollout_epoch'}
  evidence_store(raw_bytes) -> object_key str       # control root evidence/への
      # O_EXCL耐久保存(0600/no symlink)。戻り値はsha256名のobject_key
  evidence_load(object_key) -> bytes                # 保存済み証拠の読戻し
  now_ms() -> int  now_s() -> float  mono() -> float
"""
import copy
import hashlib
import json
import secrets
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_state as auto_state
from adapters.sdc import release_auto_transport as transport

PREPARE_MAX_S = 120
BUDGET_WINDOW_MS = 86_400_000
MAX_BUDGET_ENTRIES = 128
GRANT_TTL_MS = 30_000
DOC_BYTES = 256 * 1024
RESULT_PATH = '/v3/mcs/releases/result'
SELECT_PATH = '/v3/mcs/releases/select'
START_PATH = '/v3/mcs/releases/start'
STATUS_PATH = '/v3/mcs/releases/status'
ABORT_PATH = '/v3/mcs/releases/cancel-prestart'
for _p in (RESULT_PATH, SELECT_PATH, START_PATH, STATUS_PATH, ABORT_PATH):
    if _p not in transport.JSON_PATHS:
        raise RuntimeError('release-auto path missing from api-contract: ' + _p)
del _p


class OwnerError(RuntimeError):
    """v3 orchestration refusal; code selects the safe hold."""

    def __init__(self, code='OWNER_HOLD'):
        super().__init__(code)
        self.code = code


def _nonce():
    return secrets.token_hex(32)


def _det_uuid(seed):
    """sha256 seedから決定論的なv4形uuidを作る。論理requestのretryで固定するため。"""
    h = hashlib.sha256(seed.encode()).digest()
    b = bytearray(h[:16])
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    s = b.hex()
    return f'{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}'


def _request_ids(kind, *parts):
    """(request_id, request_nonce)を論理request内容から決定論的に導出する。"""
    seed = hashlib.sha256(
        ('mcs-auto-v3|' + kind + '|' + '|'.join(str(p) for p in parts)).encode()
    ).hexdigest()
    request_id = _det_uuid('id|' + seed)
    request_nonce = hashlib.sha256(('nonce|' + seed).encode()).hexdigest()
    return request_id, request_nonce


def _safety_changed(before, after):
    return any(before.get(k) != after.get(k) for k in auto_state._SAFETY_KEYS)


def _mutate(store, fn):
    """O lock下のCAS保存。fnはnew owner dictを変更する。

    safety項目またはoutbox appendでのみexecution_revisionを進める
    (state.saveの規約と一致)。それ以外の差分はsave側が拒否する。
    """
    current = auto_state.load(store)
    new = copy.deepcopy(current)
    fn(new)
    delta = auto_state._outbox_delta(current['outbox'], new['outbox'])
    safety = _safety_changed(current, new) or delta == 'append'
    new['revision'] = current['revision'] + 1
    new['execution_revision'] = (current['execution_revision'] + 1 if safety
                                 else current['execution_revision'])
    return auto_state.save(store, new)


def _blank_active(operation_id, phase, target, attempt_key, rollout, **over):
    active = {'operation_id': operation_id, 'phase': phase, 'target': target,
              'attempt_key': attempt_key, 'prepare_nonce': None,
              'prepare_deadline': None, 'private_witness': None, 'grant_raw': None,
              'start_receipt_id': None, 'candidate_generation': None,
              'startup_nonce': None, 'activation_receipt_id': None,
              'runtime_proof': None, 'reserved_owner_bytes': 1, 'error_code': None,
              'rollout': rollout}
    active.update(over)
    return active


def _attempt_key(owner, target, digests):
    """ENC03: tag/日付/policy epochを含めないattempt同一性。"""
    return contract.hash_v3('automatic-attempt', {
        'scope': owner['scope'], 'source_target_commit': target['source_commit'],
        'profile_sha256': owner['profile']['profile_sha256'],
        'config_contract_sha256': digests['config_contract_sha256'],
        'data_contract_sha256': digests['data_contract_sha256']})


def _in_window(consent, now_s):
    """StandingConsent.windowsをzoneinfoで評価。weekdayはPython流儀(月=0)。

    DST曖昧境界(foldでoffsetが変わる時刻)は開始を延期してFalseを返す。
    """
    if not isinstance(consent, dict) or consent.get('enabled') is not True:
        return False
    try:
        tz = ZoneInfo(consent['timezone'])
        dt = datetime.fromtimestamp(now_s, tz)
    except Exception:
        return False
    try:
        if dt.replace(fold=0).utcoffset() != dt.replace(fold=1).utcoffset():
            return False
    except Exception:
        return False
    minute = dt.hour * 60 + dt.minute
    return any(w['weekday'] == dt.weekday()
               and w['start_minute'] <= minute < w['end_minute']
               for w in consent['windows'])


def _consent_live(consent, now_ms):
    return (isinstance(consent, dict) and consent.get('enabled') is True
            and isinstance(consent.get('expires_at'), int)
            and consent['expires_at'] > now_ms)


def _budget_state(owner, consent, now_ms):
    """rolling 24hのprepare回数/累積予約を評価する。entriesは自動削除しない。"""
    cutoff = now_ms - BUDGET_WINDOW_MS
    recent = [b for b in owner['prepare_budget'] if b['started_at'] > cutoff]
    used_ms = sum(b['reserved_ms'] for b in recent)
    per_attempt_ms = min(PREPARE_MAX_S * 1000, consent['max_prepare_seconds'] * 1000)
    ok = (len(owner['prepare_budget']) < MAX_BUDGET_ENTRIES
          and len(recent) < consent['max_prepare_count_24h']
          and used_ms + per_attempt_ms <= consent['max_prepare_seconds_24h'] * 1000)
    return ok, per_attempt_ms


# --- outbox / 結果配送 -------------------------------------------------

def deliver_outbox(store, deps):
    """pending outboxを固定raw bytesでresultへ送り、ACK一致分だけacknowledgedにする。

    失敗はentryをpendingのまま残し業務を止めない。同一rawの再送は冪等。
    """
    delivered = 0
    owner = auto_state.load(store)
    for entry in owner['outbox']:
        if entry['acknowledged']:
            continue
        try:
            raw = entry['raw'].encode('utf-8')
            response = deps.signed_send(RESULT_PATH, raw)
            ack = deps.verify(RESULT_PATH, response)
            if not _ack_matches(entry, ack, owner['scope']):
                continue
            auto_state.ack_outbox(store, report_id=entry['report_id'],
                                  outcome_sha256=entry['sha256'])
            delivered += 1
        except Exception:
            continue
    return delivered


def _ack_matches(entry, ack, scope):
    return (isinstance(ack, dict)
            and ack.get('schema_version') == 'mcs.outcome-ack.v3'
            and ack.get('state') == 'received'
            and ack.get('report_id') == entry['report_id']
            and ack.get('operation_id') == entry['operation_id']
            and ack.get('outcome_sha256') == entry['sha256']
            and ack.get('scope') == scope)


# --- offer / STAGED -----------------------------------------------------

def request_offer(store, deps):
    """SelectionRequest→/select→検証済みOfferを返す。consent無し・active有りはNone。"""
    owner = auto_state.load(store)
    if owner['active'] is not None or owner['consent'] is None:
        return None
    consent = owner['consent']
    request_id, request_nonce = _request_ids(
        'select', owner['scope'], owner['installed']['release_id'],
        owner['verified_installed']['release_id'], owner['runtime_generation'],
        consent['consent_id'], consent['local_consent_epoch'], consent['policy_epoch'])
    request = {'schema_version': 'mcs.selection-request.v3',
               'request_id': request_id, 'request_nonce': request_nonce,
               'scope': owner['scope'], 'profile': owner['profile'],
               'current_release': owner['installed'],
               'verified_release': owner['verified_installed'],
               'consent_id': consent['consent_id'],
               'local_consent_epoch': consent['local_consent_epoch'],
               'policy_epoch': consent['policy_epoch'],
               'runtime_generation': owner['runtime_generation']}
    request = contract.parse_typed(json.dumps(request).encode(), 'SelectionRequest')
    raw = json.dumps(request, ensure_ascii=True, separators=(',', ':')).encode()
    response = deps.signed_send(SELECT_PATH, raw)
    offer = deps.verify(SELECT_PATH, response)
    if not isinstance(offer, dict) or offer.get('schema_version') != 'mcs.offer.v3':
        raise OwnerError('OFFER_INVALID')
    if offer.get('request_id') != request_id or offer.get('request_nonce') != request_nonce:
        raise OwnerError('OFFER_MISMATCH')
    return contract.parse_typed(json.dumps(offer).encode(), 'Offer')


def stage_candidate(store, deps, offer):
    """DEV10: signed Offerを検証し適格候補だけをSTAGEDへ保存する。

    scope/期限/cohort・current→target互換・profile・closure・最小updater・
    forward ancestryはdeps.offer_eligibleとdeps.fetch_artifact/stage_verifyへ委譲。
    未適格・期限切れ・旧releaseは何も保存しない。
    """
    owner = auto_state.load(store)
    if owner['active'] is not None:
        return None
    offer = contract.parse_typed(json.dumps(offer).encode(), 'Offer')
    now_ms = deps.now_ms()
    if offer['status'] != 'candidate' or offer['selected'] is None:
        return None
    if (offer['scope'] != owner['scope']
            or offer['expires_at'] <= now_ms or offer['not_before'] > now_ms):
        return None
    selected = offer['selected']
    target = selected['release']
    if target['code_seq'] <= owner['installed']['code_seq']:
        return None
    if not deps.offer_eligible(offer, owner):
        return None
    if not deps.capacity_ok(target['artifact_size'] * 2):
        raise OwnerError('CAPACITY_HOLD')
    sink = deps.stage_sink(target)
    request = {'schema_version': 'mcs.read-artifact-request.v3',
               'request_id': _det_uuid('artifact-request|' + target['artifact_sha256']),
               'request_nonce': hashlib.sha256(
                   ('artifact|' + target['artifact_sha256']).encode()).hexdigest(),
               'scope': owner['scope'], 'release_id': target['release_id'],
               'artifact_sha256': target['artifact_sha256'],
               'artifact_size': target['artifact_size']}
    request = contract.parse_typed(json.dumps(request).encode(), 'ReadArtifactRequest')
    raw = json.dumps(request, ensure_ascii=True, separators=(',', ':')).encode()
    deps.fetch_artifact(raw, expected_sha256=target['artifact_sha256'],
                        expected_size=target['artifact_size'], sink=sink)
    if not deps.stage_verify(target, sink):
        raise OwnerError('STAGE_INVALID')
    operation_id = str(uuid.uuid4())
    digests = deps.contract_digests()
    attempt_key = _attempt_key(owner, target, digests)

    def apply(new):
        if target['code_seq'] > new['highest_seen']['code_seq']:
            new['highest_seen'] = copy.deepcopy(target)
        new['active'] = _blank_active(operation_id, 'STAGED', target, attempt_key,
                                      selected['rollout'])

    saved = _mutate(store, apply)
    return saved['active']['operation_id'] if saved['active'] else None


# --- PREPARING / PREPARED ------------------------------------------------

def _prepare_entry(owner, operation_id):
    active = owner['active']
    if active is None or active['operation_id'] != operation_id:
        raise OwnerError('PHASE_MISMATCH')
    return active


def _check_prepare_gate(owner, deps):
    consent = owner['consent']
    if not _consent_live(consent, deps.now_ms()):
        raise OwnerError('CONSENT_REQUIRED')
    if not _in_window(consent, deps.now_s()):
        raise OwnerError('OUTSIDE_WINDOW')
    holds = deps.holds()
    if holds:
        raise OwnerError('HOLDS_PRESENT:' + ','.join(sorted(holds)))
    ok, per_attempt_ms = _budget_state(owner, consent, deps.now_ms())
    if not ok:
        raise OwnerError('PREPARE_BUDGET_EXCEEDED')
    return per_attempt_ms


def prepare_runtime(store, deps, operation_id):
    """DEV30: STAGED→PREPARING→drain→PREPARED。失敗時は自分のprepareだけをDEFERRED終端。

    drain・network・process待機はlock外。Q EXはwitness構築中だけ保持する。
    PREPARING再開時は永続化済みnonce/deadline/budget予約を再利用する。
    """
    owner = auto_state.load(store)
    active = _prepare_entry(owner, operation_id)
    phase = active['phase']
    if phase == 'STAGED':
        per_attempt_ms = _check_prepare_gate(owner, deps)
        now_ms = deps.now_ms()
        prepare_nonce = _nonce()
        deadline_ms = now_ms + per_attempt_ms
        reserved = deps.reserved_owner_bytes()

        def begin(new):
            a = new['active']
            if a is None or a['operation_id'] != operation_id or a['phase'] != 'STAGED':
                raise OwnerError('PHASE_MISMATCH')
            a['phase'] = 'PREPARING'
            a['prepare_nonce'] = prepare_nonce
            a['prepare_deadline'] = deadline_ms
            a['reserved_owner_bytes'] = reserved
            new['prepare_budget'] = list(new['prepare_budget']) + [{
                'operation_id': operation_id, 'started_at': now_ms,
                'ended_at': None, 'reserved_ms': per_attempt_ms}]

        _mutate(store, begin)
        owner = auto_state.load(store)
        active = owner['active']
    elif phase == 'PREPARING':
        if deps.now_ms() > active['prepare_deadline']:
            _defer_active(store, deps, operation_id, 'DEFERRED')
            deps.restart_same_version()
            raise OwnerError('PREPARE_EXPIRED')
    else:
        raise OwnerError('PHASE_MISMATCH')

    deps.stop_new_intake()
    services = deps.managed_services()
    for service in services:
        deps.request_stop(service)
    try:
        evidence = deps.await_retirement(active['prepare_deadline'])
        if deps.now_ms() > active['prepare_deadline']:
            raise OwnerError('DRAIN_INCOMPLETE')
        if not deps.retirement_complete(evidence, services):
            raise OwnerError('DRAIN_INCOMPLETE')
        with deps.q_exclusive():
            witness_private = deps.witness_private(active)
            public = _build_prepared_public(store, owner, deps, active,
                                            active['prepare_nonce'])
            witness = contract.parse_document(json.dumps(dict(
                witness_private, schema_version='mcs.private-prepared-witness.v3',
                public=public)).encode(), 'PrivatePreparedWitness',
                max_bytes=DOC_BYTES)

            def ready(new):
                a = new['active']
                if (a is None or a['operation_id'] != operation_id
                        or a['phase'] != 'PREPARING'):
                    raise OwnerError('PHASE_MISMATCH')
                a['phase'] = 'PREPARED'
                a['private_witness'] = witness
                for entry in reversed(new['prepare_budget']):
                    if (entry['operation_id'] == operation_id
                            and entry['ended_at'] is None):
                        entry['ended_at'] = deps.now_ms()
                        break

            _mutate(store, ready)
    except OwnerError:
        _defer_active(store, deps, operation_id, 'DEFERRED')
        deps.restart_same_version()
        raise
    except Exception:
        _defer_active(store, deps, operation_id, 'DEFERRED')
        deps.restart_same_version()
        raise OwnerError('PREPARE_UNKNOWN') from None
    return witness


def _build_prepared_public(store, owner, deps, active, prepare_nonce):
    """PreparedPublic。execution_revisionはPREPARED保存後の値(現行+1)を先取りする。

    PREPARED遷移が唯一のpending safety変更なので post-save値は一意に決まる。
    grantはこのrevisionへ束縛され、以後のsafety変更をconsume時に検出する。
    """
    digests = deps.contract_digests()
    public = {'schema_version': 'mcs.prepared-public.v3',
              'local_preflight_id': _det_uuid('preflight|' + active['operation_id']),
              'local_backup_evidence_id': deps.backup_evidence(),
              'current_release': owner['installed'],
              'target_release': active['target'],
              'profile_sha256': owner['profile']['profile_sha256'],
              'control_closure_sha256': owner['control_closure_sha256'],
              'config_contract_sha256': digests['config_contract_sha256'],
              'data_contract_sha256': digests['data_contract_sha256'],
              'installed_schema': deps.installed_schema(),
              'prepare_nonce': prepare_nonce,
              'execution_revision': owner['execution_revision'] + 1,
              'runtime_generation': owner['runtime_generation'],
              'boot_id': deps.boot_id(),
              'resources_verified': True,
              'observed_at': deps.now_ms(),
              'rollout': active['rollout']}
    return contract.parse_typed(json.dumps(public).encode(), 'PreparedPublic')


def _defer_active(store, deps, operation_id, state_name='DEFERRED',
                  allowed_phases=('STAGED', 'PREPARING', 'PREPARED')):
    """自分のactive操作をTerminalRecord+active終結+budget endedで原子的に終端する。

    state_nameはDEFERRED/CANCELLED_NO_EFFECT(VERIFIEDはfinalize_verified経路のみ)。
    allowed_phasesで終端可能な開始前phaseを限定し、mutation内でphaseを再検査する
    (検査〜保存の間にSTARTED等へ進んだ場合は書き込まない)。
    """
    owner = auto_state.load(store)
    active = owner['active']
    if active is None or active['operation_id'] != operation_id:
        return None
    if state_name not in ('DEFERRED', 'CANCELLED_NO_EFFECT'):
        raise OwnerError('TERMINAL_STATE')
    if active['phase'] not in allowed_phases:
        raise OwnerError('PHASE_MISMATCH')
    terminal_at = deps.now_ms()
    # proof_sha256はschema必須。DEFERRED/CANCELLEDにはruntime proofが無いため、
    # 終端record内容への決定論的束縛digestを採用する(local記録・wire非送信)。
    proof_sha256 = contract.hash_v3('terminal-proof', {
        'operation_id': operation_id, 'state': state_name,
        'attempt_key': active['attempt_key'],
        'outcome_sha256': None, 'terminal_at': terminal_at})
    terminal = {'schema_version': 'mcs.terminal-record.v3',
                'operation_id': operation_id, 'state': state_name,
                'attempt_key': active['attempt_key'],
                'outcome_sha256': None,
                'proof_sha256': proof_sha256, 'terminal_at': terminal_at}
    terminal = contract.parse_document(json.dumps(terminal).encode(), 'TerminalRecord',
                                       max_bytes=DOC_BYTES)

    def apply(new):
        a = new['active']
        if (a is None or a['operation_id'] != operation_id
                or a['phase'] not in allowed_phases):
            raise OwnerError('PHASE_MISMATCH')
        if any(o['operation_id'] == operation_id for o in new['outcomes']):
            raise OwnerError('TERMINAL_DUPLICATE')
        new['outcomes'] = list(new['outcomes']) + [terminal]
        new['active'] = None
        for entry in reversed(new['prepare_budget']):
            if (entry['operation_id'] == operation_id
                    and entry['ended_at'] is None):
                entry['ended_at'] = terminal['terminal_at']
                break

    return _mutate(store, apply)


def _abort_evidence_id(operation_id):
    return 'prestart-abort-' + operation_id


def _has_abort_evidence(owner, operation_id):
    return any(e.get('logical_id') == _abort_evidence_id(operation_id)
               for e in owner['operation_evidence'])


def _load_abort_raw(store, deps, owner, operation_id):
    """耐久済みabort証拠のraw bytesをobject_key経由で復元する。

    証拠が無ければNone。hash/size不一致は改竄として拒否する。
    """
    entry = next((e for e in owner['operation_evidence']
                  if e.get('logical_id') == _abort_evidence_id(operation_id)), None)
    if entry is None:
        return None
    try:
        raw = deps.evidence_load(entry['object_key'])
    except Exception as e:
        raise OwnerError('EVIDENCE_UNAVAILABLE') from e
    if (not isinstance(raw, (bytes, bytearray)) or len(raw) != entry['size']
            or hashlib.sha256(bytes(raw)).hexdigest() != entry['sha256']):
        raise OwnerError('EVIDENCE_MISMATCH')
    return bytes(raw)


def _build_abort(store, deps, owner, active, operation_id, reason, grant_id):
    """PrestartAbortを決定論的に構築する(receipt_idもseed由来で固定)。"""
    public = active['private_witness']['public']
    abort_request_id, abort_nonce = _request_ids(
        'prestart-abort', operation_id, active['attempt_key'])
    start_request_id, _ = _request_ids('start', operation_id, active['attempt_key'])
    abort = {'schema_version': 'mcs.prestart-abort.v3',
             'request_id': abort_request_id, 'request_nonce': abort_nonce,
             'scope': owner['scope'], 'operation_id': operation_id,
             'start_request_id': start_request_id, 'grant_id': grant_id,
             'prepare_nonce': active['prepare_nonce'],
             'execution_revision': public['execution_revision'],
             'local_abort_receipt_id': _det_uuid(
                 'local-abort-receipt|' + operation_id + '|' + active['attempt_key']),
             'reason': reason, 'observed_at': deps.now_ms()}
    abort = contract.parse_typed(json.dumps(abort).encode(), 'PrestartAbort')
    return json.dumps(abort, ensure_ascii=True, separators=(',', ':')).encode()


def _send_abort(store, deps, owner, operation_id, abort_raw):
    """中央cancel-prestartへ通知しcancelled_prestartを確認する。

    送信/検証の失敗はABORT_PENDING(証拠は耐久済み・resumeが再送する)。
    中央の明示拒否のみABORT_REJECTEDで区別する。
    """
    try:
        response = deps.signed_send(ABORT_PATH, abort_raw)
        payload = deps.verify(ABORT_PATH, response)
        status = contract.parse_typed(json.dumps(payload).encode(), 'OperationStatus')
    except OwnerError:
        raise
    except Exception as e:
        raise OwnerError('ABORT_PENDING') from e
    abort = json.loads(abort_raw)
    known_objects = {abort['start_request_id'], abort['request_id']}
    if abort.get('grant_id'):
        known_objects.add(abort['grant_id'])
    if not (status['schema_version'] == 'mcs.operation-status.v3'
            and status['request_id'] == abort['request_id']
            and status['request_nonce'] == abort['request_nonce']
            and status['kind'] == 'prestart_abort'
            and status['operation_id'] == operation_id
            and status['scope'] == owner['scope']
            and status['state'] == 'cancelled_prestart'
            and status['object_id'] in known_objects):
        raise OwnerError('ABORT_REJECTED')


def cancel_prestart(store, deps, operation_id, reason='PREPARE_DEFERRED',
                    grant_id=None):
    """pre-start撤回(02§8/SEM21): intent/SPENT不在を証明して終端する。

    - STAGED/PREPARING: 中央へstart要求未到達なのでlocal DEFERRED終端のみ。
    - PREPARED: abort証拠をevidence/へ耐久保存→operation_evidenceへ記録
      (exec_rev前進でprepared grant束縛も無効化)→中央cancel-prestartで
      cancelled_prestartを確認→CANCELLED_NO_EFFECT終端。送信失敗時は証拠が
      残りresumeが同bytesを冪等再送して終端する。証拠既存時は保存bytesを使う。
    - START_INTENT以降・intent/SPENT有りは拒否(観測的復旧の領域)。
    検査〜終端はconsume_startと同じlock集合で直列化する(CAS)。
    """
    if reason not in ('EXPIRED_BEFORE_START', 'REVOKED_BEFORE_START',
                      'PREPARE_DEFERRED'):
        raise OwnerError('REASON_INVALID')
    owner = auto_state.load(store)
    active = owner['active']
    if active is None or active['operation_id'] != operation_id:
        raise OwnerError('PHASE_MISMATCH')
    if active['phase'] not in ('STAGED', 'PREPARING', 'PREPARED'):
        raise OwnerError('PHASE_MISMATCH')

    if active['phase'] != 'PREPARED':
        with deps.acquire_locks(('G', 'U', 'Q', 'R', 'C', 'A', 'M')):
            _defer_active(store, deps, operation_id, 'DEFERRED')
        return {'action': 'deferred', 'notified': False}

    # PREPARED: lock内で証拠の有無を確定し、無ければ構築→耐久保存→記録する。
    # 記録はexec_revを進めるため、以後のconsume_startはSTALEで拒否される。
    with deps.acquire_locks(('G', 'U', 'Q', 'R', 'C', 'A', 'M')):
        owner = auto_state.load(store)
        active = owner['active']
        if (active is None or active['operation_id'] != operation_id
                or active['phase'] != 'PREPARED'
                or active['private_witness'] is None):
            raise OwnerError('STALE_STATE')
        intent = auto_state.load_intent(store)
        if intent is not None and intent['operation_id'] == operation_id:
            raise OwnerError('START_ALREADY_ATTEMPTED')
        if any(s['operation_id'] == operation_id for s in owner['spent']):
            raise OwnerError('START_ALREADY_ATTEMPTED')
        abort_raw = _load_abort_raw(store, deps, owner, operation_id)
        if abort_raw is None:
            abort_raw = _build_abort(store, deps, owner, active, operation_id,
                                     reason, grant_id)
            object_key = deps.evidence_store(abort_raw)
            evidence_sha = hashlib.sha256(abort_raw).hexdigest()

            def record_evidence(new):
                a = new['active']
                if (a is None or a['operation_id'] != operation_id
                        or a['phase'] != 'PREPARED'):
                    raise OwnerError('PHASE_MISMATCH')
                new['operation_evidence'] = list(new['operation_evidence']) + [{
                    'logical_id': _abort_evidence_id(operation_id),
                    'sha256': evidence_sha, 'size': len(abort_raw),
                    'object_key': object_key}]

            _mutate(store, record_evidence)
            owner = auto_state.load(store)

    # lock解放後に中央へ通知(networkはlock外)。同bytesの再送は中央で冪等。
    _send_abort(store, deps, owner, operation_id, abort_raw)

    with deps.acquire_locks(('G', 'U', 'Q', 'R', 'C', 'A', 'M')):
        _defer_active(store, deps, operation_id, 'CANCELLED_NO_EFFECT')
    deps.restart_same_version()
    return {'action': 'cancelled', 'notified': True}


# --- grant / consume -----------------------------------------------------

def _release_meta(deps, active, owner):
    meta = deps.release_metadata(active['target'], owner['profile'])
    required = ('publication_id', 'manifest_digest', 'compatibility_sha256',
                'rollout_epoch')
    if not isinstance(meta, dict) or any(meta.get(k) is None for k in required):
        raise OwnerError('METADATA_MISSING')
    return meta


def _start_request(store, deps):
    """決定論的StartRequestを再構築する。request_id/nonce/windowは永続値のみから導出。"""
    owner = auto_state.load(store)
    active = owner['active']
    if (active is None or active['phase'] != 'PREPARED'
            or active['private_witness'] is None):
        raise OwnerError('PHASE_MISMATCH')
    public = active['private_witness']['public']
    consent = owner['consent']
    if not _consent_live(consent, deps.now_ms()):
        raise OwnerError('CONSENT_REQUIRED')
    if not _in_window(consent, deps.now_s()):
        raise OwnerError('OUTSIDE_WINDOW')
    meta = _release_meta(deps, active, owner)
    request_id, request_nonce = _request_ids(
        'start', active['operation_id'], active['attempt_key'])
    window = {'starts_at': public['observed_at'],
              'ends_at': active['prepare_deadline']}
    request = {'schema_version': 'mcs.start-request.v3',
               'request_id': request_id, 'request_nonce': request_nonce,
               'operation_id': active['operation_id'], 'scope': owner['scope'],
               'consent_id': consent['consent_id'],
               'local_consent_epoch': consent['local_consent_epoch'],
               'policy_epoch': consent['policy_epoch'],
               'rollout_epoch': meta['rollout_epoch'],
               'publication_id': meta['publication_id'],
               'manifest_digest': meta['manifest_digest'],
               'compatibility_sha256': meta['compatibility_sha256'],
               'preflight': public,
               'preflight_sha256': contract.hash_v3('prepared-public', public),
               'attempt_key': active['attempt_key'],
               'window': window,
               'rollout': active['rollout']}
    request = contract.parse_typed(json.dumps(request).encode(), 'StartRequest')
    return request, request_id, request_nonce, meta, public


def _check_grant(grant, request, owner, active, public, deps):
    """StartGrantを送信request・active・preflight・owner独立記録へ全項目照合する。"""
    now_ms = deps.now_ms()
    checks = (
        grant['schema_version'] == 'mcs.start-grant.v3'
        and grant['start_request_id'] == request['request_id']
        and grant['request_nonce'] == request['request_nonce']
        and grant['operation_id'] == active['operation_id']
        and grant['attempt_key'] == active['attempt_key']
        and grant['prepare_nonce'] == active['prepare_nonce']
        and grant['scope'] == owner['scope']
        and grant['target'] == active['target']
        and grant['consent_id'] == request['consent_id']
        and grant['local_consent_epoch'] == request['local_consent_epoch']
        and grant['policy_epoch'] == request['policy_epoch']
        and grant['rollout_epoch'] == request['rollout_epoch']
        and grant['publication_id'] == request['publication_id']
        and grant['manifest_digest'] == request['manifest_digest']
        and grant['compatibility_sha256'] == request['compatibility_sha256']
        and grant['profile_sha256'] == public['profile_sha256']
        and grant['control_closure_sha256'] == public['control_closure_sha256']
        and grant['execution_revision'] == public['execution_revision']
        and grant['runtime_generation'] == public['runtime_generation']
        and grant['boot_id'] == public['boot_id']
        and grant['preflight_sha256'] == request['preflight_sha256']
        and grant['window'] == request['window']
        and grant['rollout'] == active['rollout']
        and grant['issued_at'] <= now_ms
        and grant['not_before'] <= now_ms <= grant['expires_at']
        and 0 < grant['expires_at'] - grant['not_before'] <= GRANT_TTL_MS)
    if not checks:
        raise OwnerError('GRANT_MISMATCH')


def request_start(store, deps):
    """DEV30 grant取得: PREPARED後のlock外request。状態は変更しない(再送は同bytes)。"""
    request, _, _, _, public = _start_request(store, deps)
    raw = json.dumps(request, ensure_ascii=True, separators=(',', ':')).encode()
    response = deps.signed_send(START_PATH, raw)
    grant = deps.verify(START_PATH, response)
    if not isinstance(grant, dict) or grant.get('schema_version') != 'mcs.start-grant.v3':
        raise OwnerError('GRANT_INVALID')
    owner = auto_state.load(store)
    active = owner['active']
    grant = contract.parse_typed(json.dumps(grant).encode(), 'StartGrant')
    _check_grant(grant, request, owner, active, public, deps)
    return grant


def consume_start(store, deps, grant):
    """DEV31: PREPARED+grant→lock群→再検証→intent+SPENT+STARTEDを段階的に耐久化。

    lock順はG→U→Q→R→C→A→M(Oはstate関数が各mutationで取得)。repo書込みは
    M5のapply_code_only_v3が担い、ここではcapability発行の直前状態までを確定する。
    intent→START_INTENT→SPENT→STARTEDの各書込みは独立に耐久化され、途中crashは
    observe/recovery側が観測的に処理する(推測で進めない)。
    """
    grant = contract.parse_typed(json.dumps(grant).encode(), 'StartGrant')
    owner = auto_state.load(store)
    active = owner['active']
    if active is None or active['phase'] != 'PREPARED' or active['private_witness'] is None:
        raise OwnerError('PHASE_MISMATCH')
    public = active['private_witness']['public']
    request, _, _, _, _ = _start_request(store, deps)
    _check_grant(grant, request, owner, active, public, deps)
    if deps.local_revoke():
        raise OwnerError('CONSENT_REVOKED')
    if deps.holds():
        raise OwnerError('HOLD_PRESENT')

    with deps.acquire_locks(('G', 'U', 'Q', 'R', 'C', 'A', 'M')):
        owner = auto_state.load(store)
        active = owner['active']
        if (active is None or active['operation_id'] != grant['operation_id']
                or active['phase'] != 'PREPARED'
                or active['private_witness'] is None
                or active['private_witness']['public'] != public):
            raise OwnerError('STALE_STATE')
        # currentness: PREPARED以降にsafety項目が変化していないこと
        if owner['execution_revision'] != public['execution_revision']:
            raise OwnerError('STALE_EXECUTION')
        if not _consent_live(owner['consent'], deps.now_ms()):
            raise OwnerError('CONSENT_REQUIRED')
        # DEV31-4: grant期限・保守窓を重い検査後の新しい時刻で再確認する。
        if not _in_window(owner['consent'], deps.now_s()):
            raise OwnerError('OUTSIDE_WINDOW')
        if deps.local_revoke() or deps.holds():
            raise OwnerError('HOLD_PRESENT')
        _check_grant(grant, request, owner, active, public, deps)

        grant_raw = json.dumps(grant, ensure_ascii=True, separators=(',', ':'))
        grant_sha = hashlib.sha256(grant_raw.encode()).hexdigest()
        intent = {'schema_version': 'mcs.start-intent.v3',
                  'operation_id': grant['operation_id'], 'grant_id': grant['grant_id'],
                  'grant_sha256': grant_sha, 'attempt_key': grant['attempt_key'],
                  'prepare_nonce': grant['prepare_nonce'],
                  'root_fingerprint': owner['root_fingerprint'],
                  'source_before': owner['installed']['source_commit'],
                  'source_target': active['target']['source_commit'],
                  'created_at': deps.now_ms(), 'phase': 'open',
                  'terminal_outcome_sha256': None}
        intent = contract.parse_document(json.dumps(intent).encode(), 'StartIntent',
                                         max_bytes=DOC_BYTES)
        auto_state.open_intent(store, intent)

        def mark_phase(new):
            a = new['active']
            if (a is None or a['operation_id'] != grant['operation_id']
                    or a['phase'] != 'PREPARED'):
                raise OwnerError('PHASE_MISMATCH')
            a['phase'] = 'START_INTENT'

        _mutate(store, mark_phase)

        spent = {'operation_id': grant['operation_id'], 'grant_id': grant['grant_id'],
                 'attempt_key': grant['attempt_key'],
                 'start_receipt_id': grant['grant_id'],
                 'started_at': deps.now_ms(), 'grant_raw': grant_raw,
                 'grant_sha256': grant_sha, 'public_preflight': public,
                 'manifest_evidence_sha256': grant['manifest_digest'],
                 'consent_evidence_sha256': grant['preflight_sha256']}
        spent = contract.parse_document(json.dumps(spent).encode(), 'SpentRecord',
                                        max_bytes=DOC_BYTES)
        auto_state.record_spent(store, spent)

        def started(new):
            a = new['active']
            if (a is None or a['operation_id'] != grant['operation_id']
                    or a['phase'] != 'START_INTENT'):
                raise OwnerError('PHASE_MISMATCH')
            a['phase'] = 'STARTED'
            a['grant_raw'] = grant_raw
            a['start_receipt_id'] = grant['grant_id']

        _mutate(store, started)
    # STARTEDは耐久化済み。effect直前のrevoke/期限再確認はM5のapply_code_only_v3が担う
    owner = auto_state.load(store)
    active = owner['active']
    if active is None or active['phase'] != 'STARTED':
        raise OwnerError('PHASE_MISMATCH')
    return grant


# --- 観測 / tick ---------------------------------------------------------

def resume(store, deps):
    """active操作の観測的継続/復旧。START後は証拠収集のみで状態を進めない。

    PREPARING中断は期限内ならprepare再開、期限切れはDEFERRED+同版再起動。
    PREPARED期限切れも同様。STAGED以降の推測進行は行わない。
    """
    owner = auto_state.load(store)
    active = owner['active']
    if active is None:
        return {'action': 'idle'}
    phase = active['phase']
    if phase == 'PREPARING':
        if deps.now_ms() > active['prepare_deadline']:
            _defer_active(store, deps, active['operation_id'], 'DEFERRED')
            deps.restart_same_version()
            return {'phase': phase, 'action': 'deferred'}
        return {'phase': phase, 'action': 'resume_prepare'}
    if phase == 'PREPARED':
        # abort証拠があればcancel-prestartが途中で止まった状態。
        # 保存bytesを冪等再送し、確認できたらCANCELLED_NO_EFFECTで終端する。
        if _has_abort_evidence(owner, active['operation_id']):
            try:
                abort_raw = _load_abort_raw(store, deps, owner,
                                            active['operation_id'])
                _send_abort(store, deps, owner, active['operation_id'], abort_raw)
            except (OwnerError, auto_state.OwnerStateError) as e:
                return {'phase': phase, 'action': 'abort_pending', 'code': e.code}
            with deps.acquire_locks(('G', 'U', 'Q', 'R', 'C', 'A', 'M')):
                _defer_active(store, deps, active['operation_id'],
                              'CANCELLED_NO_EFFECT')
            deps.restart_same_version()
            return {'phase': phase, 'action': 'cancelled'}
        if deps.now_ms() > active['prepare_deadline']:
            _defer_active(store, deps, active['operation_id'], 'DEFERRED')
            deps.restart_same_version()
            return {'phase': phase, 'action': 'deferred'}
        return {'phase': phase, 'action': 'await_grant'}
    if phase == 'STAGED':
        return {'phase': phase, 'action': 'await_window'}
    # APPLIED/CANDIDATE_READY/ACTIVATING: runtime proof前進はdeps注入hook
    # (release_runtime_proof.advance_runtimeを束ねたcallable)へ委譲する。
    # hook未設定・UNKNOWNは観測のみで状態を進めない。
    advance = getattr(deps, 'runtime_advance', None)
    if advance is not None and phase in ('APPLIED', 'CANDIDATE_READY',
                                         'ACTIVATING'):
        try:
            result = advance(store)
        except (OwnerError, auto_state.OwnerStateError) as e:
            return {'phase': phase, 'action': 'hold', 'code': e.code}
        if isinstance(result, dict):
            return {'phase': phase,
                    'action': result.get('action') or result.get('phase',
                                                                 'advanced')}
    return {'phase': phase, 'action': 'observe_only'}


def _maybe_apply_effect(store, deps, info):
    """consume直後の同一processでだけeffectを試す任意hook。

    deps.apply_effect(store)があれば呼ぶ(本番はrelease_auto_applyの
    issue_effect_capability→apply_code_only_v3を束ねたcallable)。
    capabilityはRAMのみなのでcrash後のresumeからは再発行しない。
    """
    apply_effect = getattr(deps, 'apply_effect', None)
    if apply_effect is None:
        return info
    try:
        result = apply_effect(store)
    except (OwnerError, auto_state.OwnerStateError) as e:
        # effect失敗時の実phase(UNKNOWN等)を正本から読み直して報告する
        try:
            phase = auto_state.load(store)['active']
            phase = phase['phase'] if phase else None
        except Exception:
            phase = info.get('phase')
        return {'phase': phase, 'action': 'hold', 'code': e.code}
    if isinstance(result, dict) and result.get('phase'):
        info = dict(info, phase=result['phase'], action=result.get('action', 'applied'))
    return info


def tick(store, deps):
    """1 tick: outbox配送→active観測→(無ければoffer→stage)→prepare→grant→consume→effect。

    各段階は独立に再開可能。OwnerErrorはholdとして記録し例外を外へ流さない
    (失敗しても安全側: DEFERRED済み・pending outbox・active保持のいずれか)。
    effectはdeps.apply_effect設定時のみ同一tick内で実行する。
    """
    try:
        deliver_outbox(store, deps)
    except Exception:
        pass
    owner = auto_state.load(store)
    active = owner['active']
    if active is not None:
        info = resume(store, deps)
        action = info.get('action')
        try:
            if action == 'resume_prepare':
                prepare_runtime(store, deps, active['operation_id'])
                info = resume(store, deps)
                action = info.get('action')
            if action == 'await_grant':
                grant = request_start(store, deps)
                consume_start(store, deps, grant)
                info = {'phase': 'STARTED', 'action': 'consumed'}
                info = _maybe_apply_effect(store, deps, info)
        except (OwnerError, auto_state.OwnerStateError) as e:
            info = {'phase': info.get('phase'), 'action': 'hold', 'code': e.code}
        return info
    try:
        offer = request_offer(store, deps)
    except (OwnerError, auto_state.OwnerStateError) as e:
        return {'action': 'hold', 'code': e.code}
    if offer is None:
        return {'action': 'idle'}
    try:
        operation_id = stage_candidate(store, deps, offer)
    except (OwnerError, auto_state.OwnerStateError) as e:
        return {'action': 'hold', 'code': e.code}
    if operation_id is None:
        return {'action': 'idle'}
    try:
        prepare_runtime(store, deps, operation_id)
    except (OwnerError, auto_state.OwnerStateError) as e:
        return {'action': 'hold', 'code': e.code, 'operation_id': operation_id}
    owner = auto_state.load(store)
    active = owner['active']
    if active is None or active['phase'] != 'PREPARED':
        return {'action': 'hold', 'operation_id': operation_id}
    try:
        grant = request_start(store, deps)
        consume_start(store, deps, grant)
        info = _maybe_apply_effect(store, deps,
                                   {'phase': 'STARTED', 'action': 'consumed',
                                    'operation_id': operation_id})
    except (OwnerError, auto_state.OwnerStateError) as e:
        return {'action': 'hold', 'code': e.code, 'operation_id': operation_id}
    return info
