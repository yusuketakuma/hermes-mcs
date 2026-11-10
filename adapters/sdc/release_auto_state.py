"""mcs.release-owner.v3 owner state: load/CAS mutate/start-intent/outbox。

v1のrelease_state.pyと同じStore面・lock・uncertain規約を使うが、schemaと
状態機械はv3契約(OwnerState/StartIntent/SpentRecord/OutboxEntry)に従う。
v1 owner fileをv3として読み替えない。移行は明示のOwnerMigration経路だけ。

規約: 変更系の公開関数は各々 store.release_owner_lock() を取得するleaf。
load/load_intent等の読取りはlockを取らない。lock保持中に別のlock取得関数を
呼ばない(同process再入はdeadlock)。
"""
import copy
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
    """履歴の削除・後退・同key別bytes改変を両分類で拒否する。

    spent/outcomesはexact recordのappend-only: 同operation_id(+attempt_key)の
    既存recordは全値一致を要求し、削除・フィールド改変・重複keyを拒否する。
    """
    # spent/outcomesはexact recordのappend-onlyかつkey一意:
    # 同operation_id/attempt_keyの既存recordは全値一致を要求し、
    # 削除・フィールド改変・key再利用・同op別attempt/同attempt別opを拒否する。
    spent_ops, spent_keys, spent_grants = set(), set(), set()
    for s in after['spent']:
        _check(s['operation_id'] not in spent_ops and s['attempt_key'] not in spent_keys
               and s['grant_id'] not in spent_grants, 'SPENT_REGRESSION')
        spent_ops.add(s['operation_id'])
        spent_keys.add(s['attempt_key'])
        spent_grants.add(s['grant_id'])
    for old in before['spent']:
        match = [n for n in after['spent']
                 if n['operation_id'] == old['operation_id'] and n['attempt_key'] == old['attempt_key']]
        _check(len(match) == 1 and match[0] == old, 'SPENT_REGRESSION')
    outcome_ops = set()
    for o in after['outcomes']:
        _check(o['operation_id'] not in outcome_ops, 'OUTCOME_REGRESSION')
        outcome_ops.add(o['operation_id'])
    for old in before['outcomes']:
        _check(any(n == old for n in after['outcomes']), 'OUTCOME_REGRESSION')
    # bindingsはcode_seq→release_idの一意写像(02_DEVICE_PROTOCOL §3/SEM29):
    # 旧seqのrelease_id固定・同seq別ID/重複pair拒否・新seq追加のみ許可。
    seq_map = {}
    for b in after['bindings']:
        _check(b['code_seq'] not in seq_map, 'BINDINGS_REGRESSION')
        seq_map[b['code_seq']] = b['release_id']
    for old in before['bindings']:
        _check(seq_map.get(old['code_seq']) == old['release_id'], 'BINDINGS_REGRESSION')
    for name in _HIGH_WATER:
        _check(_ref_not_regressed(before[name], after[name]), 'HIGH_WATER_REGRESSION')
        # 同code_seq+release_idのrefは全値一致のみ許可(artifact/commit差替えを拒否)
        if after[name]['code_seq'] == before[name]['code_seq']:
            _check(after[name] == before[name], 'HIGH_WATER_MUTATION')
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
        # VERIFIEDはfinalize_verifiedのatomic確定経路のみ。save()経由の新規VERIFIED
        # outcome追加は原子的確定(outbox/baseline/active終結)を伴わないため拒否。
        _check(all(o['state'] != 'VERIFIED' or o in current['outcomes']
                   for o in value['outcomes']), 'VERIFIED_REQUIRES_FINALIZE')
        outbox_delta = _check_monotonic(current, value)
        safety = bool(changed & set(_SAFETY_KEYS)) or outbox_delta == 'append'
        if safety:
            _check(value['execution_revision'] > current['execution_revision'], 'EXECUTION_REVISION_REQUIRED')
        else:
            _check(value['execution_revision'] == current['execution_revision'], 'EXECUTION_REVISION_FROZEN')
            _check(outbox_delta in ('none', 'ack'), 'OUTBOX_REGRESSION')
        _write(store, value)
        return value


def install_initial(store, value):
    """bridgeのv3初期owner配置。O lock下で既存docを分類し原子的に置く。

    既存v3がvalueと完全一致なら冪等no-op、異なるv3は拒否。v1/missingは置換対象。
    """
    with store.release_owner_lock():
        raw = store.read(OWNER_NAME)
        if raw is not None:
            try:
                current = contract.parse_document(_dumps(raw), 'OwnerState',
                                                  max_bytes=MAX_OWNER_BYTES)
            except contract.ContractError:
                current = None
            if isinstance(current, dict) and current.get('schema_version') == SCHEMA:
                _check(current == _validate(value), 'ALREADY_V3')
                return current
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
        _check(all(s['grant_id'] != record['grant_id'] for s in value['spent']), 'GRANT_REUSE')
        value['spent'].append(record)
        value['revision'] += 1
        value['execution_revision'] += 1
        _write(store, value)
        return value


def record_terminal(store, record):
    """非VERIFIEDのTerminalRecordをoutcomesへ耐久化する。VERIFIEDはfinalize_verifiedの
    atomic確定経路のみが記録し、ここでは拒否する(原子的確定の証明を分離)。"""
    try:
        checked = contract.parse_document(_dumps(record), 'TerminalRecord', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    _check(checked['state'] in TERMINAL_STATES and checked['state'] != 'VERIFIED', 'INVALID')
    with store.release_owner_lock():
        value = load(store)
        _check(all(o['operation_id'] != checked['operation_id'] for o in value['outcomes']), 'TERMINAL_DUPLICATE')
        value['outcomes'].append(checked)
        value['revision'] += 1
        value['execution_revision'] += 1
        _write(store, value)
        return value


def finalize_verified(store, terminal, *, outcome_raw, source_manifest_sha256,
                      expected_execution_revision):
    """VERIFIED終端の線形化点（02_DEVICE_PROTOCOL §9-10、r16 Oracle照合設計）。

    O lock下で最新stateへ outcome・verified_installed・source successor
    baseline・active終結・fixed outbox を一つのatomic保存で確定する。

    - terminalはTerminalRecord VERIFIEDで、active.op/attemptと一致必須
    - spentの同op/attempt記録(grant証拠)が存在必須
    - outcome_rawはOutcome契約でstate=verified/readiness=ready、scope・
      operation/attempt/grant/runtime_generation/rollout/targetを
      active・SPENTの独立記録へ束縛し、sha256==terminal.outcome_sha256
    - 同一terminalが既にoutcomesへあれば、過去op自身の耐久証拠(SPENT・
      binding・outbox)が整合する場合のみ現stateをそのまま返す
      (再送・巻戻しなし)。この読取り専用再送ではsource_manifest_sha256
      引数は使用されず、新しいmanifest採用やeffectは生じない(r37)
    - expected_execution_revision不一致はstaleとして拒否
    - outbox未ACK・pause/同意撤回/旧grant期限は観測的終端を阻害しない
    - source_manifest_sha256は呼出し側(M6等)が認証済みtarget観測から供給する。
      本関数はその真正性を再検証しないため、認証caller接続まではpartial。
    """
    try:
        term = contract.parse_document(_dumps(terminal), 'TerminalRecord', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    _check(term['state'] == 'VERIFIED' and term['outcome_sha256'] is not None, 'INVALID')
    try:
        outcome = contract.parse_typed(outcome_raw.encode('utf-8'), 'Outcome')
    except (contract.ContractError, UnicodeEncodeError, AttributeError):
        raise OwnerStateError('INVALID') from None
    _check(outcome['operation_id'] == term['operation_id']
           and outcome['attempt_key'] == term['attempt_key']
           and outcome['state'] == 'verified' and outcome['readiness'] == 'ready',
          'OUTCOME_MISMATCH')
    outcome_sha = hashlib.sha256(outcome_raw.encode('utf-8')).hexdigest()
    _check(outcome_sha == term['outcome_sha256'], 'OUTCOME_HASH')
    entry = {'report_id': outcome['report_id'], 'operation_id': outcome['operation_id'],
             'raw': outcome_raw, 'sha256': outcome_sha, 'acknowledged': False}
    try:
        entry = contract.parse_document(_dumps(entry), 'OutboxEntry', max_bytes=MAX_OWNER_BYTES)
    except contract.ContractError:
        raise OwnerStateError('INVALID') from None
    with store.release_owner_lock():
        current = load(store)
        prior = next((o for o in current['outcomes'] if o['operation_id'] == term['operation_id']), None)
        if prior is not None:
            # exact finalize再送: 過去op自身の耐久証拠(SPENT・binding・outbox)
            # で確定を照合する。現在baselineは後続updateで進み得るため
            # receipt条件にせず、C採用後のB再送を拒否しない。
            _check(prior == term, 'TERMINAL_CONFLICT')
            spent_prior = next((s for s in current['spent']
                                if s['operation_id'] == term['operation_id']
                                and s['attempt_key'] == term['attempt_key']), None)
            bound = next((b for b in current['bindings']
                          if b['code_seq'] == outcome['target']['code_seq']), None)
            durable = (
                spent_prior is not None
                and outcome['scope'] == current['scope']
                and outcome['grant_id'] == spent_prior['grant_id']
                and outcome['rollout'] == spent_prior['public_preflight']['rollout']
                and outcome['runtime_generation']
                    > spent_prior['public_preflight']['runtime_generation']
                and outcome['verified_installed'] == outcome['target']
                and bound is not None
                and bound['release_id'] == outcome['target']['release_id']
                and _ref_not_regressed(outcome['verified_installed'],
                                       current['verified_installed'])
                and any(e['report_id'] == entry['report_id']
                        and e['operation_id'] == entry['operation_id']
                        and e['raw'] == entry['raw'] and e['sha256'] == entry['sha256']
                        for e in current['outbox']))
            _check(durable, 'TERMINAL_INCONSISTENT')
            return current
        _check(current['execution_revision'] == expected_execution_revision, 'STALE_EXECUTION')
        active = current['active']
        _check(active is not None and active['operation_id'] == term['operation_id']
               and active['attempt_key'] == term['attempt_key'], 'ACTIVE_MISMATCH')
        spent = next((s for s in current['spent']
                      if s['operation_id'] == term['operation_id']
                      and s['attempt_key'] == term['attempt_key']), None)
        _check(spent is not None, 'SPENT_MISSING')
        # Outcomeをactive/SPENTの独立記録へ相関束縛する。
        # runtime_generationは認証済みtarget世代(active.candidate_generation、
        # runtime_proof存在時はそのruntime_generation)への一致を要求する。
        # preflight時点・現在owner記録より大きいことも単調性として維持する。
        candidate = active['candidate_generation']
        _check(outcome['scope'] == current['scope']
               and outcome['grant_id'] == spent['grant_id']
               and candidate is not None
               and outcome['runtime_generation'] == candidate
               and outcome['runtime_generation'] > spent['public_preflight']['runtime_generation']
               and outcome['runtime_generation'] > current['runtime_generation']
               and outcome['rollout'] == active['rollout']
               and outcome['rollout'] == spent['public_preflight']['rollout']
               and outcome['target'] == active['target']
               and outcome['verified_installed'] == active['target'], 'OUTCOME_MISMATCH')
        proof = active['runtime_proof']
        if proof is not None:
            _check(proof['operation_id'] == term['operation_id']
                   and proof['release'] == active['target']
                   and proof['runtime_generation'] == outcome['runtime_generation'],
                  'RUNTIME_PROOF_MISMATCH')
        _check(_ref_not_regressed(current['verified_installed'], active['target']), 'TARGET_REGRESSION')
        new = copy.deepcopy(current)
        new['outcomes'].append(term)
        target = copy.deepcopy(active['target'])
        new['installed'] = copy.deepcopy(target)
        new['verified_installed'] = copy.deepcopy(target)
        if _ref_not_regressed(current['highest_installed'], target):
            new['highest_installed'] = copy.deepcopy(target)
        new['installed_source_manifest_sha256'] = source_manifest_sha256
        new['verified_source_manifest_sha256'] = source_manifest_sha256
        new['runtime_generation'] = outcome['runtime_generation']
        new['active'] = None
        bound = next((b for b in new['bindings'] if b['code_seq'] == target['code_seq']), None)
        if bound is None:
            new['bindings'].append({'code_seq': target['code_seq'],
                                    'release_id': target['release_id']})
        else:
            _check(bound['release_id'] == target['release_id'], 'BINDING_CONFLICT')
        existing = next((e for e in new['outbox'] if e['report_id'] == entry['report_id']), None)
        if existing is None:
            new['outbox'].append(entry)
        else:
            _check(existing['operation_id'] == entry['operation_id']
                   and existing['raw'] == entry['raw'] and existing['sha256'] == entry['sha256'],
                  'OUTBOX_CONFLICT')
        _check_monotonic(current, new)
        new['revision'] += 1
        new['execution_revision'] += 1
        _write(store, new)
        return new


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
