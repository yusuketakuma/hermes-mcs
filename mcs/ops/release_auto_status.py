"""v3 status/doctor表示とlegacy auto抑止 (M8/W14)。

update_cli/doctor/schedulerが使う固定DTOをowner正本から組み立てる。
表示はallowlist構築のみ — 生exception・絶対path・hostname・原logを出さない
(03_CONTRACTS: 外部用DTOはallowlistから新たに構築)。UNKNOWN/DEFERRED/
HELD/unresolvedは正常表示へ変換しない。

legacy_auto_blocked: v3 owner stateが存在する(または読めない)限り
legacy auto更新経路を抑止する。file不在=未移行でのみFalse。
"""
import os

from adapters.sdc import release_auto_state as auto_state

OWNER_NAME = 'release-owner.json'

# UNKNOWN phaseは正常系の語へ写像しない — 表示stateを'unknown'へ固定する。
_UNKNOWN_PHASES = frozenset({'UNKNOWN'})


def status_dto(store):
    """owner正本から固定DTOを組み立てる。read-only・状態を変更しない。

    戻り値のキーは固定。phase/error_codeはcontract enum値のみ通し、
    それ以外の文字列・例外詳細は含めない。
    """
    try:
        if store.read(OWNER_NAME) is None:
            return {'schema_version': 'mcs.release-status-local.v1',
                    'state': 'uninitialized', 'phase': None, 'reason': None,
                    'verified_release': None, 'pending_outbox': None,
                    'consent': None, 'health': None}
        owner = auto_state.load(store)
    except Exception:
        return {'schema_version': 'mcs.release-status-local.v1',
                'state': 'unreadable', 'phase': None, 'reason': None,
                'verified_release': None, 'pending_outbox': None,
                'consent': None, 'health': None}
    active = owner.get('active')
    verified = owner.get('verified_installed')
    outbox_pending = sum(1 for e in owner.get('outbox', [])
                         if not e.get('acknowledged'))
    dto = {'schema_version': 'mcs.release-status-local.v1',
           'state': 'idle' if active is None else 'active',
           'phase': None, 'reason': None,
           'verified_release': (verified or {}).get('release_id'),
           'pending_outbox': outbox_pending,
           'consent': (owner.get('consent') or {}).get('state'),
           'health': None}
    if active is not None:
        phase = active.get('phase')
        dto['phase'] = phase if isinstance(phase, str) else 'unknown'
        if dto['phase'] in _UNKNOWN_PHASES or dto['phase'] == 'unknown':
            dto['state'] = 'unknown'
        error = active.get('error_code')
        # contract enum(NONE/大文字snake)のみ通す。それ以外は詳細を出さない。
        dto['reason'] = error if (isinstance(error, str)
                                  and error.replace('_', '').isalnum()
                                  and error.isupper()) else None
    try:
        import release_auto_health as health
        dto['health'] = health.health_status(store)
    except Exception:
        dto['health'] = {'state': 'unreadable'}
    return dto


def status_dto_at(state_root):
    """state_root dirから固定DTOを組み立てる。root未設定/非strはNone。

    dir未作成=uninitialized、Storeを開けない・stateが読めない=unreadable
    固定形。update_cli/doctor等の呼出側がroot解決と失敗写像を重複しない
    ための入口。
    """
    base = {'schema_version': 'mcs.release-status-local.v1',
            'state': 'unreadable', 'phase': None, 'reason': None,
            'verified_release': None, 'pending_outbox': None,
            'consent': None, 'health': None}
    if not isinstance(state_root, str) or not state_root:
        return None
    if not os.path.isdir(state_root):
        return {**base, 'state': 'uninitialized'}
    try:
        from adapters.sdc.storage import Store
        with Store(state_root) as store:
            return status_dto(store)
    except Exception:
        return base


def legacy_auto_blocked(store):
    """legacy auto更新を抑止すべきか。v3 owner stateの存在=抑止。

    - file不在 → False(未移行、legacy経路は従来どおり)
    - parse可能なv3 owner state → True(v3 protocolが更新を所有)
    - 読取/parse失敗 → True(fail-closed: 状態不明でlegacyを開かない)
    """
    try:
        if store.read(OWNER_NAME) is None:
            return False
        auto_state.load(store)
    except Exception:
        return True
    return True
