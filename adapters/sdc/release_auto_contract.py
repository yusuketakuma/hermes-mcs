"""自動更新v3のwire契約を純粋に検証する（署名pin・owner state・runner接続は別module）。

schema正本は adapters/sdc/contracts/mcs-auto/v3/（CSDC側とbyte一致mirror）。
context依存のSEM照合は verify_v3_envelope/release_auto_state 側が担う。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from adapters.sdc.support_contract import MAX_BODY, MAX_SAFE_INTEGER, canonical

SCHEMA = json.loads((Path(__file__).resolve().parent / 'contracts/mcs-auto/v3/contracts/contracts-v3.schema.json').read_text())
DEFS = SCHEMA['$defs']

AUTO_INPUT_BYTES = 16384
AUTO_DEPTH = 32
AUTO_NODES = 10000
AUTO_PAYLOAD_BYTES = 8192
AUTO_ERROR_SCHEMA = 'mcs.release-error.v3'
HASH_DOMAIN = 'MCS-AUTO-v3\n'
_UINT64_MAX = 2**64 - 1
_KIND = re.compile(r'[a-z0-9][a-z0-9._-]{0,62}')

AUTO_PURPOSES = (
    'mcs-auto-offer-v3', 'mcs-auto-compatibility-v3', 'mcs-auto-grant-v3', 'mcs-auto-ack-v3',
    'mcs-auto-status-v3', 'mcs-publication-plan-v3', 'mcs-release-acceptance-v3',
    'mcs-auto-health-ack-v3', 'mcs-auto-consent-preview-v3', 'mcs-auto-consent-receipt-v3',
    'mcs-rollout-policy-v3',
)
AUTO_PURPOSE_DEFINITIONS = {
    'mcs-auto-offer-v3': 'Offer', 'mcs-auto-compatibility-v3': 'Compatibility',
    'mcs-auto-grant-v3': 'StartGrant', 'mcs-auto-ack-v3': 'OutcomeAck',
    'mcs-auto-status-v3': 'OperationStatus', 'mcs-publication-plan-v3': 'PublishPlan',
    'mcs-release-acceptance-v3': 'ReleaseAcceptance', 'mcs-auto-health-ack-v3': 'HealthAck',
    'mcs-auto-consent-preview-v3': 'ConsentPreview', 'mcs-auto-consent-receipt-v3': 'ConsentReceipt',
    'mcs-rollout-policy-v3': 'RolloutPolicy',
}


class ContractError(ValueError):
    """wire契約違反。codeはmcs.release-error.v3のenumに揃える。"""

    def __init__(self, code='CONTRACT_INVALID'):
        super().__init__(code)
        self.code = code


class HoldError(RuntimeError):
    """安全側の保留。codeは同enum。"""

    def __init__(self, code='RUNTIME_HOLD'):
        super().__init__(code)
        self.code = code


class PersistenceUnknown(BaseException):
    """永続化の成否が不明。operation_idで追跡する。"""

    def __init__(self, operation_id):
        super().__init__(operation_id)
        self.operation_id = operation_id


def _check(ok, code='CONTRACT_INVALID'):
    if not ok:
        raise ContractError(code)


def _same_json(a, b):
    try:
        return canonical(a, max_bytes=MAX_BODY) == canonical(b, max_bytes=MAX_BODY)
    except Exception:
        return False


def _type_ok(t, v):
    if t == 'object':
        return type(v) is dict
    if t == 'array':
        return type(v) is list
    if t == 'string':
        return type(v) is str
    if t == 'integer':
        return type(v) is int  # boolはintの派生だがtype比較で除外される
    if t == 'boolean':
        return type(v) is bool
    if t == 'null':
        return v is None
    return False


# contracts-v3.schema.jsonが使う閉じたJSON Schema subsetのみ評価する。
def _match_node(d, v):
    if d is True:
        return True
    if d is False:
        return False
    ref = d.get('$ref')
    if ref is not None:
        name = ref.rsplit('/', 1)[-1]
        _check(name in DEFS)
        return _match_node(DEFS[name], v)
    for key, want in (('allOf', all), ('anyOf', any)):
        if isinstance(d.get(key), list) and not want(_match_node(x, v) for x in d[key]):
            return False
    if isinstance(d.get('oneOf'), list) and sum(1 for x in d['oneOf'] if _match_node(x, v)) != 1:
        return False
    if 'const' in d and not _same_json(d['const'], v):
        return False
    if isinstance(d.get('enum'), list) and not any(_same_json(e, v) for e in d['enum']):
        return False
    if 'type' in d:
        types = d['type'] if isinstance(d['type'], list) else [d['type']]
        if not any(_type_ok(t, v) for t in types):
            return False
    if isinstance(v, str):
        if 'pattern' in d and re.search(d['pattern'], v) is None:
            return False
        if 'minLength' in d and len(v) < d['minLength']:
            return False
        if 'maxLength' in d and len(v) > d['maxLength']:
            return False
    if type(v) is int:
        if 'minimum' in d and v < d['minimum']:
            return False
        if 'maximum' in d and v > d['maximum']:
            return False
    if isinstance(v, list):
        if 'minItems' in d and len(v) < d['minItems']:
            return False
        if 'maxItems' in d and len(v) > d['maxItems']:
            return False
        prefix = d.get('prefixItems')
        if isinstance(prefix, list):
            if len(v) < len(prefix):
                return False
            if not all(_match_node(prefix[i], v[i]) for i in range(len(prefix))):
                return False
            rest = v[len(prefix):]
            if 'items' in d:
                if not all(_match_node(d['items'], x) for x in rest):
                    return False
            elif rest:
                return False
        elif 'items' in d and not all(_match_node(d['items'], x) for x in v):
            return False
    if type(v) is dict:
        props = d.get('properties') or {}
        if isinstance(d.get('required'), list) and any(k not in v for k in d['required']):
            return False
        if d.get('additionalProperties') is False and any(k not in props for k in v):
            return False
        for k, pd in props.items():
            if k in v and not _match_node(pd, v[k]):
                return False
        extra = d.get('additionalProperties')
        if isinstance(extra, dict):
            for k, pv in v.items():
                if k not in props and not _match_node(extra, pv):
                    return False
    return True


# ENC01: 深さ32・node 10000・全numberはsafe integer。wire由来のfloatはparse hookで既にint化/拒否済み。
def _scan_value(v, depth, state):
    _check(depth <= AUTO_DEPTH)
    state['n'] += 1
    _check(state['n'] <= AUTO_NODES)
    if v is None or isinstance(v, (str, bool)):
        return
    if type(v) is int:
        _check(abs(v) <= MAX_SAFE_INTEGER)
        return
    if isinstance(v, list):
        for x in v:
            _scan_value(x, depth + 1, state)
        return
    if type(v) is dict:
        for x in v.values():
            _scan_value(x, depth + 1, state)
        return
    _check(False)  # float等のwire外number表現は拒否


def _definition(name):
    _check(type(name) is str and name in DEFS)
    return DEFS[name]


def _times_ordered(v, max_ttl=None):
    _check(v['issued_at'] <= v['not_before'] < v['expires_at'])
    if max_ttl is not None:
        _check(v['expires_at'] - v['issued_at'] <= max_ttl)


# context非依存のSEMのみ。残りは機能module側が照合する。
def _semantic_pure(definition, v):
    if definition == 'Offer':  # SEM12/13
        if v['status'] == 'candidate':
            _check(v['selected'] is not None and v['reason'] == 'ELIGIBLE')
        else:
            _check(v['selected'] is None and v['reason'] != 'ELIGIBLE')
        _times_ordered(v, 600000)
        return
    if definition == 'StartGrant':  # SEM18の時刻部
        _times_ordered(v, 30000)
        _check(v['window']['starts_at'] < v['window']['ends_at'] and v['expires_at'] <= v['window']['ends_at'])
        return
    if definition == 'Outcome':  # SEM24–26
        if v['state'] == 'verified':
            _check(v['readiness'] == 'ready' and v['error_code'] == 'NONE' and v['verified_at'] is not None
                   and _same_json(v['installed'], v['verified_installed']) and _same_json(v['installed'], v['target'])
                   and v['started_at'] <= v['verified_at'] <= v['observed_at'])
        elif v['state'] == 'unknown':
            _check(v['readiness'] == 'unknown' and v['verified_at'] is None and v['error_code'] != 'NONE')
        elif v['state'] == 'cancelled_no_effect':
            _check(v['readiness'] == 'not_started' and v['error_code'] == 'NO_EFFECT_CONFIRMED' and v['verified_at'] is None)
        return
    if definition == 'MaintenanceWindow':  # SEM11の窓順序
        _check(v['start_minute'] < v['end_minute'])
        return
    if definition == 'FileIdentity':  # SEM16のUInt64範囲とinode非零(ENC02)
        for k in ('device', 'inode', 'mtime_ns', 'ctime_ns'):
            _check(int(v[k]) <= _UINT64_MAX)
        _check(v['inode'] != '0')
        return
    # SEM11: windows配列内のweekday一意とstart<end
    windows = v.get('windows') if type(v) is dict else None
    if isinstance(windows, list) and all(type(w) is dict and 'weekday' in w for w in windows):
        days = set()
        for w in windows:
            _check(w['weekday'] not in days)
            days.add(w['weekday'])
            _check(w['start_minute'] < w['end_minute'])


# context非依存semanticを持つdef。$refで入れ子になるdef(PrivateFileWitness内の
# FileIdentity、ConsentConfirmRequest/OwnerState内のStandingConsent等)にも同じ検査を
# 再帰適用する。適用は_match_node通過時のみで、combinator循環は(node,value)対で打ち切る。
_SEMANTIC_DEFS = frozenset((
    'Offer', 'StartGrant', 'Outcome', 'FileIdentity', 'MaintenanceWindow',
    'StandingConsent', 'ConsentPreview', 'EnrollmentProposalRequest'))


def _semantic_deep(node, v, seen):
    if not isinstance(node, dict) or v is None or not isinstance(v, (dict, list)):
        return
    mark = (id(node), id(v))
    if mark in seen:
        return
    seen.add(mark)
    ref = node.get('$ref')
    if isinstance(ref, str):
        name = ref.rsplit('/', 1)[-1]
        d = DEFS.get(name)
        if d is None:
            return
        if name in _SEMANTIC_DEFS and _match_node(d, v):
            _semantic_pure(name, v)
        _semantic_deep(d, v, seen)
        return
    props = node.get('properties')
    if isinstance(props, dict) and type(v) is dict:
        for k, sub in props.items():
            if k in v:
                _semantic_deep(sub, v[k], seen)
    items = node.get('items')
    prefix = node.get('prefixItems')
    if isinstance(v, list):
        if isinstance(prefix, list):
            for i, sub in enumerate(prefix):
                if i < len(v):
                    _semantic_deep(sub, v[i], seen)
            rest = v[len(prefix):]
            if isinstance(items, dict):
                for x in rest:
                    _semantic_deep(items, x, seen)
        elif isinstance(items, dict):
            for x in v:
                _semantic_deep(items, x, seen)
    for key in ('allOf', 'anyOf', 'oneOf'):
        branches = node.get(key)
        if isinstance(branches, list):
            for sub in branches:
                _semantic_deep(sub, v, seen)


def _validate(definition, value):
    _check(_match_node(_definition(definition), value))
    _semantic_pure(definition, value)
    _semantic_deep(_definition(definition), value, set())
    return value


def _reject_constant(_s):
    raise ContractError('CONTRACT_INVALID')  # NaN/Infinity/-Infinity


def _parse_int(s):
    if s == '-0':
        raise ContractError('CONTRACT_INVALID')  # JSの-0拒否とparityを取る
    v = int(s)
    _check(abs(v) <= MAX_SAFE_INTEGER)
    return v


def _parse_float(s):
    try:
        f = float(s)
    except ValueError:
        raise ContractError('CONTRACT_INVALID') from None
    _check(math.isfinite(f))
    # JS側は「値がsafe integerなら受理」(1e3→1000, 0.0→0)。-0系は拒否。
    if f.is_integer() and abs(f) <= MAX_SAFE_INTEGER:
        if f == 0 and s.lstrip()[0] == '-':
            raise ContractError('CONTRACT_INVALID')
        return int(f)
    raise ContractError('CONTRACT_INVALID')


def _object_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ContractError('CONTRACT_INVALID')  # escape同値の重複keyもここで落ちる
        out[k] = v
    return out


def parse_typed(raw, definition, *, max_bytes=AUTO_INPUT_BYTES):
    """bytes/strのwire入力をdefinitionへ照合して返す。ContractError(422相当)のみ送出。"""
    _check(type(max_bytes) is int and 0 < max_bytes <= AUTO_INPUT_BYTES)
    data = raw.encode('utf-8') if isinstance(raw, str) else bytes(raw)
    _check(len(data) <= max_bytes)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ContractError('CONTRACT_INVALID') from None
    try:
        value = json.loads(text, parse_int=_parse_int, parse_float=_parse_float,
                           parse_constant=_reject_constant, object_pairs_hook=_object_pairs)
    except ContractError:
        raise
    except Exception:
        raise ContractError('CONTRACT_INVALID') from None
    _scan_value(value, 0, {'n': 0})
    return _validate(definition, value)


def parse_document(raw, definition, *, max_bytes):
    """wire上限を超えるlocal耐久文書(owner state等)用。depth/node検査は同一。"""
    _check(type(max_bytes) is int and AUTO_INPUT_BYTES < max_bytes <= 8 * 1024 * 1024)
    data = raw.encode('utf-8') if isinstance(raw, str) else bytes(raw)
    _check(len(data) <= max_bytes)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ContractError('CONTRACT_INVALID') from None
    try:
        value = json.loads(text, parse_int=_parse_int, parse_float=_parse_float,
                           parse_constant=_reject_constant, object_pairs_hook=_object_pairs)
    except ContractError:
        raise
    except Exception:
        raise ContractError('CONTRACT_INVALID') from None
    _scan_value(value, 0, {'n': 0})
    return _validate(definition, value)


def canonical_typed(value, definition):
    """definition適合値のcanonical CJSON bytesを返す。"""
    _validate(definition, value)
    try:
        return canonical(value, max_bytes=AUTO_INPUT_BYTES).encode('ascii')
    except Exception:
        raise ContractError('CONTRACT_INVALID') from None


def hash_v3(kind, value):
    """H(kind,payload)=sha256("MCS-AUTO-v3\n"+kind+"\n"+canonical(payload))。"""
    _check(type(kind) is str and _KIND.fullmatch(kind) is not None)
    try:
        c = canonical(value, max_bytes=AUTO_INPUT_BYTES)
    except Exception:
        raise ContractError('CONTRACT_INVALID') from None
    return hashlib.sha256((HASH_DOMAIN + kind + '\n' + c).encode('ascii')).hexdigest()
