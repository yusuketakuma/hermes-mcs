"""mcs.release-owner.v3 owner state: load/CAS mutate/start-intent/outbox。

v1のrelease_state.pyと同じStore面・lock・uncertain規約を使うが、schemaと
状態機械はv3契約(OwnerState/StartIntent/SpentRecord/OutboxEntry)に従う。
v1 owner fileをv3として読み替えない。移行は明示のOwnerMigration経路だけ。
"""
import json

from . import release_auto_contract as contract

SCHEMA = 'mcs.release-owner.v3'
INTENT_SCHEMA = 'mcs.start-intent.v3'
OWNER_NAME = 'release-owner.json'
INTENT_NAME = 'release-start-intent.json'
MIGRATION_NAME = 'release-migration-v3.json'
MAX_OWNER_BYTES = 8 * 1024 * 1024
MAX_INTENT_BYTES = 32 * 1024


class OwnerStateError(ValueError):
    def __init__(self, code='INVALID'):
        super().__init__(code)
        self.code = code


def _check(ok, code='INVALID'):
    if not ok:
        raise OwnerStateError(code)


def _validate(value):
    try:
        return contract.parse_document(
            json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii'),
            'OwnerState', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise OwnerStateError('INVALID') from None


def load(store):
    """OwnerState v3を読む。v1 fileや破損は拒否し、missingを初期化とみなさない。"""
    raw = store.read(OWNER_NAME)
    _check(raw is not None, 'UNKNOWN_BASELINE')
    try:
        value = contract.parse_document(
            json.dumps(raw, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii'),
            'OwnerState', max_bytes=MAX_OWNER_BYTES)
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


def save(store, value):
    """O lock下で最新stateへ単調revision検査を掛けて保存する。"""
    with store.release_owner_lock():
        current = load(store)
        _check(value['revision'] > current['revision'], 'REVISION_REGRESSION')
        _check(value['execution_revision'] >= current['execution_revision'], 'EXECUTION_REVISION_REGRESSION')
        # ACKだけの保存はexecution_revisionを進めない。安全項目の改変は必ず進める。
        if _safety_changed(current, value):
            _check(value['execution_revision'] > current['execution_revision'], 'EXECUTION_REVISION_REQUIRED')
        else:
            _check(value['execution_revision'] == current['execution_revision'], 'EXECUTION_REVISION_FROZEN')
        _write(store, value)
        return value


_SAFETY_KEYS = ('active', 'spent', 'outcomes', 'bindings', 'highest_seen', 'installed',
                'highest_installed', 'verified_installed', 'runtime_generation', 'consent',
                'trust_json', 'profile', 'prepare_budget')


def _safety_changed(before, after):
    return any(before[key] != after[key] for key in _SAFETY_KEYS)


def ack_outbox(store, *, report_id, outcome_sha256):
    """配信ACKの反映。revisionのみ進め、execution_revisionは据え置く。"""
    with store.release_owner_lock():
        value = load(store)
        entry = next((e for e in value['outbox'] if e['report_id'] == report_id), None)
        _check(entry is not None, 'OUTBOX_NOT_FOUND')
        _check(entry['sha256'] == outcome_sha256 or entry['acknowledged'], 'OUTBOX_CONFLICT')
        if entry['acknowledged']:
            return value
        for e in value['outbox']:
            if e['report_id'] == report_id:
                e['acknowledged'] = True
        value['revision'] += 1
        _write(store, value)
        return value


def append_outbox(store, entry):
    """Outcome報告をoutboxへ固定bytesで追加する。安全項目なのでexecution_revisionも進める。"""
    _check(contract.parse_document(
        json.dumps(entry, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii'),
        'OutboxEntry', max_bytes=MAX_OWNER_BYTES)['report_id'] == entry['report_id'], 'INVALID')
    with store.release_owner_lock():
        value = load(store)
        _check(all(e['report_id'] != entry['report_id'] for e in value['outbox']), 'OUTBOX_DUPLICATE')
        value['outbox'].append(entry)
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


def open_intent(store, intent):
    """START_INTENTを別fileへ耐久化する。owner本体とは別のfence。"""
    try:
        value = contract.parse_document(
            json.dumps(intent, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii'),
            'StartIntent', max_bytes=MAX_INTENT_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    _check(value['phase'] == 'open', 'INTENT_PHASE')
    _check(store.read(INTENT_NAME) is None, 'INTENT_EXISTS')
    store.write(INTENT_NAME, value)
    return value


def load_intent(store):
    raw = store.read(INTENT_NAME)
    if raw is None:
        return None
    try:
        return contract.parse_document(
            json.dumps(raw, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii'),
            'StartIntent', max_bytes=MAX_INTENT_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None


def retire_intent(store, *, operation_id, terminal_outcome_sha256):
    """終結証拠に整合したintentだけをretiredへ進める。消去ではない。"""
    value = load_intent(store)
    _check(value is not None and value['operation_id'] == operation_id, 'INTENT_MISMATCH')
    _check(value['phase'] == 'open', 'INTENT_PHASE')
    _check(type(terminal_outcome_sha256) is str and len(terminal_outcome_sha256) == 64
           and all(c in '0123456789abcdef' for c in terminal_outcome_sha256), 'INVALID')
    value['phase'] = 'retired'
    value['terminal_outcome_sha256'] = terminal_outcome_sha256
    store.write(INTENT_NAME, value)
    return value
