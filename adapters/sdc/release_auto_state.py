"""mcs.release-owner.v3 owner state: load/CAS mutate/start-intent/outbox。

v1のrelease_state.pyと同じStore面・lock・uncertain規約を使うが、schemaと
状態機械はv3契約(OwnerState/StartIntent/SpentRecord/OutboxEntry)に従う。
v1 owner fileをv3として読み替えない。移行は明示のOwnerMigration経路だけ。

規約: 変更系の公開関数は各々 store.release_owner_lock() を取得するleaf。
load/load_intent等の読取りはlockを取らない。lock保持中に別のlock取得関数を
呼ばない(同process再入はdeadlock)。
"""
import hashlib
import json

from . import release_auto_contract as contract

SCHEMA = 'mcs.release-owner.v3'
INTENT_SCHEMA = 'mcs.start-intent.v3'
OWNER_NAME = 'release-owner.json'
INTENT_NAME = 'release-start-intent.json'
MIGRATION_NAME = 'release-migration-v3.json'
MAX_OWNER_BYTES = 8 * 1024 * 1024
MAX_INTENT_BYTES = 32 * 1024
TERMINAL_STATES = ('VERIFIED', 'DEFERRED', 'CANCELLED_NO_EFFECT')

# root/repository/lock/activation/closure/source manifestはbootstrap時に固定される
# identity項目。save()での変更はrevisionに関係なく拒否する。
_IMMUTABLE_KEYS = ('schema_version', 'scope', 'root_fingerprint', 'repository_fingerprint',
                   'activation_sha256', 'control_closure_sha256', 'lock_fingerprints',
                   'installed_source_manifest_sha256', 'verified_source_manifest_sha256')
# 安全項目の変更はexecution_revision前進を必須とする。
_SAFETY_KEYS = ('trust_json', 'legacy_evidence', 'operation_evidence', 'profile', 'consent',
                'highest_seen', 'installed', 'highest_installed', 'verified_installed',
                'runtime_generation', 'bindings', 'active', 'spent', 'outcomes', 'prepare_budget')
_HIGH_WATER = ('highest_seen', 'highest_installed', 'verified_installed', 'installed')


class OwnerStateError(ValueError):
    def __init__(self, code='INVALID'):
        super().__init__(code)
        self.code = code


def _check(ok, code='INVALID'):
    if not ok:
        raise OwnerStateError(code)


def _dumps(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def _validate(value):
    try:
        return contract.parse_document(_dumps(value), 'OwnerState', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise OwnerStateError('INVALID') from None


def load(store):
    """OwnerState v3を読む。v1 fileや破損は拒否し、missingを初期化とみなさない。"""
    raw = store.read(OWNER_NAME)
    _check(raw is not None, 'UNKNOWN_BASELINE')
    try:
        value = contract.parse_document(_dumps(raw), 'OwnerState', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    _check(value['schema_version'] == SCHEMA, 'SCHEMA_MISMATCH')
    _check(value['root_fingerprint'] == store.root_fingerprint(), 'ROOT_MISMATCH')
    return value


def _write(store, value):
    _check(not getattr(store, '_release_owner_uncertain', False), 'UNCERTAIN')
    value = _validate(value)
    try:
        _check(value['root_fingerprint'] == store.root_fingerprint(), 'ROOT_MISMATCH')
        store.write(OWNER_NAME, value)
    except BaseException:
        store._release_owner_uncertain = True
        raise


def _ref_key(ref):
    return (ref['code_seq'], ref['release_id'])


def _ref_not_regressed(before, after):
    return after['code_seq'] > before['code_seq'] or (
        after['code_seq'] == before['code_seq'] and after['release_id'] == before['release_id'])


def _outbox_delta(before, after):
    """'none'|'ack'|'append'|'mutated'。既存entryの削除・並べ替え・hash/ID/raw改変はmutated。"""
    if before == after:
        return 'none'
    if len(after) < len(before):
        return 'mutated'
    for old, new in zip(before, after):
        if any(old[k] != new[k] for k in ('report_id', 'operation_id', 'raw', 'sha256')):
            return 'mutated'
        if old['acknowledged'] and not new['acknowledged']:
            return 'mutated'
    if all(old == new for old, new in zip(before, after)):
        return 'append' if len(after) > len(before) else 'none'
    return 'ack' if len(after) == len(before) else 'append'


def _check_monotonic(before, after):
    """履歴の削除・後退を両分類で拒否する。"""
    _check(all(any(old['operation_id'] == new['operation_id'] and old['attempt_key'] == new['attempt_key']
                   for new in after['spent']) for old in before['spent']), 'SPENT_REGRESSION')
    _check(all(any(old['code_seq'] == new['code_seq'] and old['release_id'] == new['release_id']
                   for new in after['bindings']) for old in before['bindings']), 'BINDINGS_REGRESSION')
    for name in _HIGH_WATER:
        _check(_ref_not_regressed(before[name], after[name]), 'HIGH_WATER_REGRESSION')
    delta = _outbox_delta(before['outbox'], after['outbox'])
    _check(delta != 'mutated', 'OUTBOX_REGRESSION')
    return delta


def save(store, value):
    """O lock下で最新stateへ単調revision検査を掛けて保存する。

    - identity項目(_IMMUTABLE_KEYS)の変更は常に拒否
    - 安全項目(_SAFETY_KEYS)またはoutbox追加の変更はexecution_revision前進を必須
    - ACKのみ(outbox acknowledged false→true)はrevisionのみ前進
    - spent/bindings/high-water/outboxの削除・後退は常に拒否
    """
    with store.release_owner_lock():
        current = load(store)
        _check(value['revision'] > current['revision'], 'REVISION_REGRESSION')
        _check(value['execution_revision'] >= current['execution_revision'], 'EXECUTION_REVISION_REGRESSION')
        changed = {k for k in set(current) | set(value) if current.get(k) != value.get(k)}
        changed -= {'revision', 'execution_revision'}
        _check(not (changed & set(_IMMUTABLE_KEYS)), 'IMMUTABLE_FIELD')
        outbox_delta = _check_monotonic(current, value)
        safety = bool(changed & set(_SAFETY_KEYS)) or outbox_delta == 'append'
        if safety:
            _check(value['execution_revision'] > current['execution_revision'], 'EXECUTION_REVISION_REQUIRED')
        else:
            _check(value['execution_revision'] == current['execution_revision'], 'EXECUTION_REVISION_FROZEN')
            _check(outbox_delta in ('none', 'ack'), 'OUTBOX_REGRESSION')
        _write(store, value)
        return value


def ack_outbox(store, *, report_id, outcome_sha256):
    """配信ACKの反映。revisionのみ進め、execution_revisionは据え置く。
    既ACKのreplayでもhash不一致は拒否する。"""
    with store.release_owner_lock():
        value = load(store)
        entry = next((e for e in value['outbox'] if e['report_id'] == report_id), None)
        _check(entry is not None, 'OUTBOX_NOT_FOUND')
        _check(entry['sha256'] == outcome_sha256, 'OUTBOX_CONFLICT')
        if entry['acknowledged']:
            return value
        for e in value['outbox']:
            if e['report_id'] == report_id:
                e['acknowledged'] = True
        value['revision'] += 1
        _write(store, value)
        return value


def append_outbox(store, entry):
    """Outcome報告をoutboxへ固定。rawはOutcome契約・ID一致・sha256(raw bytes)を検証する。"""
    try:
        checked = contract.parse_document(_dumps(entry), 'OutboxEntry', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    try:
        outcome = contract.parse_typed(checked['raw'], 'Outcome')
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    _check(outcome['report_id'] == checked['report_id'] and outcome['operation_id'] == checked['operation_id'], 'OUTBOX_MISMATCH')
    _check(hashlib.sha256(checked['raw'].encode('utf-8')).hexdigest() == checked['sha256'], 'OUTBOX_HASH')
    with store.release_owner_lock():
        value = load(store)
        _check(all(e['report_id'] != checked['report_id'] for e in value['outbox']), 'OUTBOX_DUPLICATE')
        value['outbox'].append(checked)
        value['revision'] += 1
        value['execution_revision'] += 1
        _write(store, value)
        return value


def record_spent(store, record):
    """SPENT耐久化。spent集合・attempt消費は安全項目としてexecution_revisionを進める。"""
    with store.release_owner_lock():
        value = load(store)
        _check(all(s['operation_id'] != record['operation_id'] for s in value['spent']), 'SPENT_DUPLICATE')
        _check(all(s['attempt_key'] != record['attempt_key'] for s in value['spent']), 'ATTEMPT_REUSE')
        value['spent'].append(record)
        value['revision'] += 1
        value['execution_revision'] += 1
        _write(store, value)
        return value


def record_terminal(store, record):
    """TerminalRecordをoutcomesへ耐久化する。operation_id一意・安全項目としてexecution_revision前進。"""
    try:
        checked = contract.parse_document(_dumps(record), 'TerminalRecord', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    _check(checked['state'] in TERMINAL_STATES, 'INVALID')
    with store.release_owner_lock():
        value = load(store)
        _check(all(o['operation_id'] != checked['operation_id'] for o in value['outcomes']), 'TERMINAL_DUPLICATE')
        value['outcomes'].append(checked)
        value['revision'] += 1
        value['execution_revision'] += 1
        _write(store, value)
        return value


def open_intent(store, intent):
    """START_INTENTを別fileへ耐久化する。owner本体とは別のfence。O lock下でread-check-write。"""
    try:
        value = contract.parse_document(_dumps(intent), 'StartIntent', max_bytes=MAX_INTENT_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    _check(value['phase'] == 'open', 'INTENT_PHASE')
    with store.release_owner_lock():
        _check(store.read(INTENT_NAME) is None, 'INTENT_EXISTS')
        store.write(INTENT_NAME, value)
    return value


def load_intent(store):
    raw = store.read(INTENT_NAME)
    if raw is None:
        return None
    try:
        return contract.parse_document(_dumps(raw), 'StartIntent', max_bytes=MAX_INTENT_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None


def retire_intent(store, *, operation_id, terminal_record):
    """ownerへ耐久化済みのTerminalRecordと一致するintentだけをretiredへ進める。

    operation_id/attempt_key/state/outcome_sha256/proof_sha256/terminal_atが
    owner.outcomesの記録と完全一致する場合のみ退役し、未保存・別op・偽hashではfenceを保持する。
    """
    try:
        terminal = contract.parse_document(_dumps(terminal_record), 'TerminalRecord', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    _check(terminal['state'] in TERMINAL_STATES and terminal['operation_id'] == operation_id, 'INTENT_EVIDENCE')
    with store.release_owner_lock():
        intent = load_intent(store)
        _check(intent is not None and intent['operation_id'] == operation_id, 'INTENT_MISMATCH')
        _check(intent['phase'] == 'open', 'INTENT_PHASE')
        owner = load(store)
        _check(any(o['operation_id'] == terminal['operation_id']
                   and o['attempt_key'] == intent['attempt_key']
                   and o['attempt_key'] == terminal['attempt_key']
                   and o['state'] == terminal['state']
                   and o['outcome_sha256'] == terminal['outcome_sha256']
                   and o['proof_sha256'] == terminal['proof_sha256']
                   and o['terminal_at'] == terminal['terminal_at']
                   for o in owner['outcomes']), 'INTENT_EVIDENCE')
        intent['phase'] = 'retired'
        intent['terminal_outcome_sha256'] = terminal['outcome_sha256']
        store.write(INTENT_NAME, intent)
        return intent
