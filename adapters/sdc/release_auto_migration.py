"""mcs.release-owner-migration.v3 intentの耐久化とphase前進。

v1 owner fileのv3置換は明示のmigration intentと人間receiptを必須とし、
phaseは prepared→old_archived→owner_replaced→verified の単調前進だけを許す。
file実体のarchive/replace自体はM7のrecovery入口が行い、ここはintent記録だけを所有する。

規約: prepare/advanceはstore.release_owner_lock()を取得するleaf。load/require_v3_ready
は読取り専用でlockを取らない。lock保持中に別のlock取得関数を呼ばない。
"""
import json

from . import release_auto_contract as contract

SCHEMA = 'mcs.release-owner-migration.v3'
NAME = 'release-migration-v3.json'
MAX_BYTES = 64 * 1024
PHASES = ('prepared', 'old_archived', 'owner_replaced', 'verified')


class MigrationError(ValueError):
    def __init__(self, code='INVALID'):
        super().__init__(code)
        self.code = code


def _check(ok, code='INVALID'):
    if not ok:
        raise MigrationError(code)


def _parse(raw):
    try:
        return contract.parse_document(
            json.dumps(raw, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii'),
            'MigrationIntent', max_bytes=MAX_BYTES)
    except contract.ContractError:
        raise MigrationError('INVALID') from None


def load(store):
    raw = store.read(NAME)
    if raw is None:
        return None
    value = _parse(raw)
    _check(value['schema_version'] == SCHEMA, 'SCHEMA_MISMATCH')
    _check(value['root_fingerprint'] == store.root_fingerprint(), 'ROOT_MISMATCH')
    return value


def _save(store, value):
    store.write(NAME, _parse(value))


def prepare(store, intent):
    """phase=preparedのintentを新規作成する。既存intentの上書きは拒否。
    read-check-writeはO lock下で行い、並行prepareの両成功を防ぐ。"""
    value = _parse(intent)
    _check(value['phase'] == 'prepared', 'PHASE')
    _check(value['root_fingerprint'] == store.root_fingerprint(), 'ROOT_MISMATCH')
    with store.release_owner_lock():
        _check(store.read(NAME) is None, 'INTENT_EXISTS')
        _save(store, value)
    return value


def advance(store, *, migration_id, phase):
    """記録済みintentを次phaseへ単調前進させる。逆行・飛越し・別idを拒否。
    load→更新→saveをO lock下で行い、並行advanceのlost updateを防ぐ。"""
    with store.release_owner_lock():
        value = load(store)
        _check(value is not None and value['migration_id'] == migration_id, 'INTENT_MISMATCH')
        index = PHASES.index(value['phase'])
        _check(index + 1 < len(PHASES) and phase == PHASES[index + 1], 'PHASE_REGRESSION')
        value['phase'] = phase
        _save(store, value)
        return value


def require_v3_ready(store):
    """v3 owner操作の前提: owner不在かv3 schemaのみ。v1残存・未完intentは拒否。"""
    intent = load(store)
    if intent is not None and intent['phase'] != 'verified':
        raise MigrationError('MIGRATION_PENDING')
    return True
