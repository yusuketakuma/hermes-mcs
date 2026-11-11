"""v3 owner orchestration (M4) を合成Store+fake depsで検証する。

実network/process/lockには触れない。offer/grantはexamplesを雛形に、
fake centralがrequest echoを組み立てる。
"""
import hashlib
import io
import json
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from adapters.sdc import release_auto_state as state
from adapters.sdc.storage import Store
import release_auto_owner as owner_mod

EXAMPLES = Path(__file__).resolve().parents[2] / 'adapters/sdc/contracts/mcs-auto/v3/examples'


def _example(name):
    return json.loads((EXAMPLES / name).read_text())['data']


SCOPE = _example('offer.json')['scope']
PROFILE = _example('standing-consent.json')['profile']
TARGET = _example('offer.json')['selected']['release']
CURRENT = _example('prepared-public.json')['current_release']
GRANT_ID = '3492ad65-d05a-473f-af8c-825521eeb41a'
DEVICE_U = '263a4dbe-4148-4fb8-b214-b0032339dbb9'

# 2026-11-09 (Monday) 02:00 Asia/Tokyo — consent window(weekday0,60-180分)の内側。
# exampleの時刻は個別fixtureで非整形なので、offer/consentの時刻はNOW基準にpatchする。
NOW_MS = int(datetime(2026, 11, 9, 2, 0,
                      tzinfo=ZoneInfo('Asia/Tokyo')).timestamp() * 1000)
NOW_S = NOW_MS / 1000


def _patched_offer():
    offer = json.loads(json.dumps(_example('offer.json')))
    offer['issued_at'] = NOW_MS - 2000
    offer['not_before'] = NOW_MS - 2000
    offer['expires_at'] = NOW_MS + 590_000  # TTL上限600s未満
    return offer


def _patched_consent():
    consent = json.loads(json.dumps(_example('standing-consent.json')))
    consent['confirmed_at'] = NOW_MS - 1000
    consent['expires_at'] = NOW_MS + 30 * 86_400_000
    return consent


OFFER = _patched_offer()
CONSENT = _patched_consent()


def release_ref(seq):
    if seq == 1:
        return dict(CURRENT)
    return dict(TARGET)


def owner_value(store, **over):
    value = {
        'schema_version': 'mcs.release-owner.v3',
        'scope': dict(SCOPE),
        'root_fingerprint': store.root_fingerprint(),
        'repository_fingerprint': 'd' * 64, 'activation_sha256': 'e' * 64,
        'control_closure_sha256': PROFILE['control_closure_sha256'],
        'revision': 1, 'execution_revision': 1,
        'trust_json': '{}', 'legacy_evidence': [], 'operation_evidence': [],
        'lock_fingerprints': {k: '0' * 64 for k in 'GUQR CAMO'.replace(' ', '')},
        'installed_source_manifest_sha256': '1' * 64,
        'verified_source_manifest_sha256': '2' * 64,
        'profile': dict(PROFILE),
        'consent': dict(CONSENT),
        'highest_seen': release_ref(1), 'installed': release_ref(1),
        'highest_installed': release_ref(1), 'verified_installed': release_ref(1),
        'runtime_generation': 1,
        'bindings': [{'code_seq': 1, 'release_id': CURRENT['release_id']}],
        'active': None, 'spent': [], 'outcomes': [], 'outbox': [], 'prepare_budget': [],
    }
    value.update(over)
    return value


def _file_identity(size=1):
    return {'device': '1', 'inode': '2', 'uid': 501, 'mode': 0o100644, 'nlink': 1,
            'size': size, 'mtime_ns': '3', 'ctime_ns': '4'}


def _file_witness(logical_id, tag):
    return {'logical_id': logical_id, 'identity': _file_identity(1),
            'sha256': hashlib.sha256(tag.encode()).hexdigest()}


def _witness_private(active):
    return {
        'root_fingerprint': 'a' * 64, 'repository_fingerprint': 'b' * 64,
        'lock_identities': [_file_witness(f'lock-{k}', f'lock-{k}')
                            for k in ('g', 'u', 'q', 'r', 'c', 'a', 'm', 'o')],
        'configuration_files': [_file_witness('config-main', 'config')],
        'source_tree_sha256': 'c' * 64, 'runtime_fingerprint': 'd' * 64,
        'ledger_device': '1', 'ledger_inode': '9',
        'stage_identity': _file_identity(4096),
        'stage_path': '/synthetic/stage', 'backup_path': '/synthetic/backup',
        'backup_sha256': 'e' * 64, 'retired_processes': [],
        'required_free_bytes': 1 << 30, 'reserved_owner_bytes': 4096,
        'prepared_until': active['prepare_deadline']}


class FakeDeps:
    """全depsを記録・差替え可能にしたfake。中央役はrequest echoから応答を組み立てる。"""

    def __init__(self, now_ms=NOW_MS):
        self.now_ms_value = now_ms
        self.sent = []
        self.lock_calls = []
        self.stops = []
        self.restarts = 0
        self.intake_stops = 0
        self.q_exclusive_calls = 0
        self.holds_list = []
        self.revoke = False
        self.eligible = True
        self.retire_ok = True
        self.grant_override = None
        self.offer_override = None
        self.abort_fail = False
        self.abort_status_override = None
        self.status_fail = False
        # START_PATH到達で中央がoperationを作成した記録。値は中央phase
        # (OP_STATE写像: signing_reserved/grant_issued→pending/granted、
        # cancelled/released_prestart→cancelled_prestart)。
        self.central_ops = {}
        self.evidence = {}
        self.meta = {'publication_id': OFFER['selected']['publication_id'],
                     'manifest_digest': OFFER['selected']['manifest_digest'],
                     'compatibility_sha256': 'f' * 64, 'rollout_epoch': 1}

    def now_ms(self):
        return self.now_ms_value

    def now_s(self):
        return self.now_ms_value / 1000

    def mono(self):
        return self.now_ms_value / 1000

    def signed_send(self, path, raw):
        self.sent.append((path, raw))
        if path == owner_mod.SELECT_PATH:
            request = json.loads(raw)
            offer = json.loads(json.dumps(self.offer_override or OFFER))
            offer['request_id'] = request['request_id']
            offer['request_nonce'] = request['request_nonce']
            offer['scope'] = request['scope']
            # 中央は要求時点でfreshなofferを発行する — TTL内に収まる時刻へ再スタンプ
            offer['issued_at'] = self.now_ms_value - 2000
            offer['not_before'] = self.now_ms_value - 2000
            offer['expires_at'] = self.now_ms_value + 590_000
            return json.dumps(offer).encode()
        if path == owner_mod.START_PATH:
            request = json.loads(raw)
            self.central_ops[request['operation_id']] = 'grant_issued'
            grant = self._grant_for(request)
            return json.dumps(grant).encode()
        if path == owner_mod.STATUS_PATH:
            if self.status_fail:
                raise OSError('synthetic status failure')
            request = json.loads(raw)
            op_state = {'signing_reserved': 'pending',
                        'grant_issued': 'granted',
                        'started': 'pending',
                        'reported': 'received',
                        'verified': 'received',
                        'unknown': 'pending',
                        'cancelled': 'cancelled_prestart',
                        'released_prestart': 'cancelled_prestart'}
            phase = self.central_ops.get(request['operation_id'])
            status = {'schema_version': 'mcs.operation-status.v3',
                      'request_id': request['request_id'],
                      'request_nonce': request['request_nonce'],
                      'scope': request['scope'],
                      'operation_id': request['operation_id'],
                      'kind': request['kind'],
                      'object_id': request['object_id'],
                      'object_sha256': None,
                      'state': op_state.get(phase, 'not_found'),
                      'issued_at': self.now_ms_value,
                      'expires_at': self.now_ms_value + 30000}
            return json.dumps(status).encode()
        if path == owner_mod.RESULT_PATH:
            outcome = json.loads(raw)
            ack = _example('outcome-ack.json')
            ack.update({'operation_id': outcome['operation_id'],
                        'report_id': outcome['report_id'],
                        'outcome_sha256': hashlib.sha256(raw).hexdigest(),
                        'scope': outcome['scope']})
            return json.dumps(ack).encode()
        if path == owner_mod.ABORT_PATH:
            if self.abort_fail:
                raise OSError('synthetic network failure')
            abort = json.loads(raw)
            # 中央はstart request未到達のoperationを持たず404を返す
            if abort['operation_id'] not in self.central_ops:
                raise OSError('operation not found')
            self.central_ops[abort['operation_id']] = 'cancelled'
            status = {'schema_version': 'mcs.operation-status.v3',
                      'request_id': abort['request_id'],
                      'request_nonce': abort['request_nonce'],
                      'scope': abort['scope'],
                      'operation_id': abort['operation_id'],
                      'kind': 'prestart_abort',
                      'object_id': abort['local_abort_receipt_id'],
                      'object_sha256': None,
                      'state': 'cancelled_prestart',
                      'issued_at': self.now_ms_value,
                      'expires_at': self.now_ms_value + 30000}
            if self.abort_status_override is not None:
                status = self.abort_status_override(status, abort)
            return json.dumps(status).encode()
        raise AssertionError('unexpected path ' + path)

    def _grant_for(self, request):
        now = self.now_ms_value
        grant = dict(_example('start-grant.json'))
        grant.update({
            'grant_id': GRANT_ID,
            'start_request_id': request['request_id'],
            'request_nonce': request['request_nonce'],
            'operation_id': request['operation_id'],
            'scope': request['scope'],
            'target': request['preflight']['target_release'],
            'consent_id': request['consent_id'],
            'local_consent_epoch': request['local_consent_epoch'],
            'policy_epoch': request['policy_epoch'],
            'rollout_epoch': request['rollout_epoch'],
            'publication_id': request['publication_id'],
            'manifest_digest': request['manifest_digest'],
            'compatibility_sha256': request['compatibility_sha256'],
            'profile_sha256': request['preflight']['profile_sha256'],
            'control_closure_sha256': request['preflight']['control_closure_sha256'],
            'prepare_nonce': request['preflight']['prepare_nonce'],
            'execution_revision': request['preflight']['execution_revision'],
            'runtime_generation': request['preflight']['runtime_generation'],
            'boot_id': request['preflight']['boot_id'],
            'preflight_sha256': request['preflight_sha256'],
            'attempt_key': request['attempt_key'],
            'window': request['window'],
            'issued_at': now - 1000, 'not_before': now - 1000,
            'expires_at': now + 20000,
            'rollout': request['rollout']})
        if self.grant_override is not None:
            grant = self.grant_override(request, grant)
        return grant

    def verify(self, path, response):
        return json.loads(response)

    def offer_eligible(self, offer, owner):
        return self.eligible

    def fetch_artifact(self, request_raw, *, expected_sha256, expected_size, sink):
        sink.write(b'artifact-bytes')

    def stage_sink(self, target):
        return io.BytesIO()

    def stage_verify(self, target, sink):
        return True

    def capacity_ok(self, required):
        return True

    def backup_evidence(self):
        return '54d00d86-7758-4ef8-96bc-4685f58e327b'

    def contract_digests(self):
        return {'config_contract_sha256': '7' * 64, 'data_contract_sha256': '8' * 64}

    def installed_schema(self):
        return 9

    def boot_id(self):
        return '6' * 64

    def witness_private(self, active):
        return _witness_private(active)

    def reserved_owner_bytes(self):
        return 4096

    def holds(self):
        return list(self.holds_list)

    def local_revoke(self):
        return self.revoke

    def stop_new_intake(self):
        self.intake_stops += 1

    def managed_services(self):
        return ['svc-a', 'svc-b']

    def request_stop(self, service):
        self.stops.append(service)

    def await_retirement(self, deadline_epoch_ms):
        return {'deadline': deadline_epoch_ms}

    def retirement_complete(self, evidence, services):
        return self.retire_ok

    def restart_same_version(self):
        self.restarts += 1

    @contextmanager
    def q_exclusive(self):
        self.q_exclusive_calls += 1
        yield

    @contextmanager
    def acquire_locks(self, names):
        self.lock_calls.append(tuple(names))
        yield

    def release_metadata(self, target, profile):
        return dict(self.meta)

    def evidence_store(self, raw):
        key = hashlib.sha256(bytes(raw)).hexdigest()
        self.evidence[key] = bytes(raw)
        return key

    def evidence_load(self, object_key):
        return self.evidence[object_key]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / 'private-auto', create=True) as value:
        yield value


def seed(store, **over):
    store.write(state.OWNER_NAME, owner_value(store, **over))


def staged(store, deps=None):
    """STAGEDまで進めた状態を返す。"""
    deps = deps or FakeDeps()
    offer = owner_mod.request_offer(store, deps)
    op_id = owner_mod.stage_candidate(store, deps, offer)
    assert op_id is not None
    return op_id, deps


def prepared(store, deps=None):
    """PREPAREDまで進めた状態を返す。"""
    deps = deps or FakeDeps()
    op_id, _ = staged(store, deps)
    owner_mod.prepare_runtime(store, deps, op_id)
    return op_id, deps


# --- request id / hash ----------------------------------------------------

def test_request_ids_deterministic_and_uuid_shaped():
    a = owner_mod._request_ids('start', 'op1', 'att1')
    b = owner_mod._request_ids('start', 'op1', 'att1')
    c = owner_mod._request_ids('start', 'op1', 'att2')
    assert a == b and a != c
    uuid_re = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
    assert uuid_re.match(a[0]) and re.fullmatch(r'[0-9a-f]{64}', a[1])


def test_attempt_key_binds_source_commit_not_artifact():
    owner = {'scope': SCOPE, 'profile': {'profile_sha256': PROFILE['profile_sha256']}}
    digests = {'config_contract_sha256': '7' * 64, 'data_contract_sha256': '8' * 64}
    k1 = owner_mod._attempt_key(owner, TARGET, digests)
    # artifact差替えは同じattempt(同source commit) — artifact_sha256を含まない
    t2 = dict(TARGET, artifact_sha256='9' * 64)
    assert owner_mod._attempt_key(owner, t2, digests) == k1
    # source commitが変われば別attempt
    t3 = dict(TARGET, source_commit='0' * 40)
    assert owner_mod._attempt_key(owner, t3, digests) != k1
    assert re.fullmatch(r'[0-9a-f]{64}', k1)


# --- window / budget -------------------------------------------------------

def _window_consent(weekday, start, end, enabled=True, tz='Asia/Tokyo'):
    return {'enabled': enabled, 'timezone': tz,
            'windows': [{'weekday': weekday, 'start_minute': start, 'end_minute': end}]}


def test_in_window_weekday_and_minute():
    # NOW_S = 2026-11-09 Mon 02:00 JST → weekday 0, minute 120
    assert owner_mod._in_window(_window_consent(0, 60, 180), NOW_S)
    assert owner_mod._in_window(_window_consent(0, 0, 1440), NOW_S)
    assert not owner_mod._in_window(_window_consent(0, 180, 240), NOW_S)
    assert not owner_mod._in_window(_window_consent(1, 60, 180), NOW_S)
    assert not owner_mod._in_window(_window_consent(0, 60, 180, enabled=False), NOW_S)
    assert not owner_mod._in_window(None, NOW_S)
    assert not owner_mod._in_window({'enabled': True, 'timezone': 'Bad/Zone',
                                     'windows': []}, NOW_S)


def test_in_window_dst_ambiguous_defers():
    # America/New_York 2026-11-01 01:30はfold曖昧 → 延期(False)
    tz = ZoneInfo('America/New_York')
    ambiguous = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=0).timestamp()
    consent = {'enabled': True, 'timezone': 'America/New_York',
               'windows': [{'weekday': 6, 'start_minute': 0, 'end_minute': 1440}]}
    assert owner_mod._in_window(consent, ambiguous) is False


def test_budget_state_rolling_24h():
    now = NOW_MS
    recent = {'operation_id': 'x' * 8 + '-0000-4000-8000-' + '0' * 12,
              'started_at': now - 1000, 'ended_at': None, 'reserved_ms': 60000}
    old = dict(recent, started_at=now - owner_mod.BUDGET_WINDOW_MS - 1)
    consent = {'max_prepare_seconds': 120, 'max_prepare_count_24h': 3,
               'max_prepare_seconds_24h': 360}
    owner = {'prepare_budget': [recent, old]}
    ok, per = owner_mod._budget_state(owner, consent, now)
    assert ok and per == 120000
    owner['prepare_budget'] = [recent] * 3
    ok, _ = owner_mod._budget_state(owner, consent, now)
    assert not ok  # count上限
    owner['prepare_budget'] = [dict(recent, reserved_ms=120000)] * 3
    ok, _ = owner_mod._budget_state(owner, consent, now)
    assert not ok  # 24h累積360s超過


# --- outbox ---------------------------------------------------------------

def test_deliver_outbox_acks_only_matching(store):
    seed(store)
    deps = FakeDeps()
    outcome = _example('outcome-verified.json')
    raw = json.dumps(outcome, ensure_ascii=True, separators=(',', ':')).encode()
    entry = {'report_id': outcome['report_id'], 'operation_id': outcome['operation_id'],
             'raw': raw.decode(), 'sha256': hashlib.sha256(raw).hexdigest(),
             'acknowledged': False}
    state.append_outbox(store, entry)
    assert owner_mod.deliver_outbox(store, deps) == 1
    owner = state.load(store)
    assert owner['outbox'][0]['acknowledged'] is True
    assert deps.sent[0][0] == owner_mod.RESULT_PATH
    assert deps.sent[0][1] == raw  # exact raw bytes再送


def test_deliver_outbox_keeps_pending_on_bad_ack(store):
    seed(store)
    deps = FakeDeps()
    outcome = _example('outcome-verified.json')
    raw = json.dumps(outcome, ensure_ascii=True, separators=(',', ':')).encode()
    entry = {'report_id': outcome['report_id'], 'operation_id': outcome['operation_id'],
             'raw': raw.decode(), 'sha256': hashlib.sha256(raw).hexdigest(),
             'acknowledged': False}
    state.append_outbox(store, entry)

    def bad_verify(path, response):
        ack = json.loads(response)
        ack['outcome_sha256'] = '0' * 64
        return ack
    deps.verify = bad_verify
    assert owner_mod.deliver_outbox(store, deps) == 0
    assert state.load(store)['outbox'][0]['acknowledged'] is False


# --- stage -----------------------------------------------------------------

def test_stage_candidate_happy(store):
    seed(store)
    deps = FakeDeps()
    op_id = staged(store, deps)[0]
    owner = state.load(store)
    active = owner['active']
    assert active['operation_id'] == op_id and active['phase'] == 'STAGED'
    assert active['target'] == TARGET and active['rollout'] == OFFER['selected']['rollout']
    assert owner['highest_seen']['code_seq'] == 2
    assert deps.sent[0][0] == owner_mod.SELECT_PATH


def test_stage_rejects_non_candidate_and_old_release(store):
    seed(store)
    deps = FakeDeps()
    deps.offer_override = dict(OFFER, status='none', selected=None, reason='NO_NEW_RELEASE')
    assert owner_mod.request_offer(store, deps) is not None  # status noneもoffer自体は返る
    op = owner_mod.stage_candidate(store, deps, deps.offer_override)
    assert op is None and state.load(store)['active'] is None
    # installedと同じseq(=1)のtargetは拒否
    old = json.loads(json.dumps(OFFER))
    old['selected']['release'] = release_ref(1)
    old['selected']['release']['release_id'] = '9' * 64
    assert owner_mod.stage_candidate(store, deps, old) is None


def test_stage_rejects_expired_and_scope_mismatch(store):
    seed(store)
    deps = FakeDeps()
    offer = owner_mod.request_offer(store, deps)
    expired = json.loads(json.dumps(offer))
    expired['expires_at'] = deps.now_ms() - 1
    assert owner_mod.stage_candidate(store, deps, expired) is None
    wrong_scope = json.loads(json.dumps(offer))
    wrong_scope['scope'] = dict(SCOPE, tenant='other')
    assert owner_mod.stage_candidate(store, deps, wrong_scope) is None


def test_stage_rejects_ineligible_and_no_consent(store):
    seed(store)
    deps = FakeDeps()
    deps.eligible = False
    offer = owner_mod.request_offer(store, deps)
    assert owner_mod.stage_candidate(store, deps, offer) is None
    seed(store, consent=None)
    assert owner_mod.request_offer(store, deps) is None


# --- prepare ---------------------------------------------------------------

def test_prepare_happy_path(store):
    seed(store)
    deps = FakeDeps()
    op_id, _ = staged(store, deps)
    witness = owner_mod.prepare_runtime(store, deps, op_id)
    owner = state.load(store)
    active = owner['active']
    assert active['phase'] == 'PREPARED' and active['private_witness'] == witness
    assert active['prepare_nonce'] and active['prepare_deadline'] > deps.now_ms()
    assert deps.intake_stops == 1 and deps.stops == ['svc-a', 'svc-b']
    assert deps.q_exclusive_calls == 1
    budget = owner['prepare_budget'][-1]
    assert budget['operation_id'] == op_id and budget['ended_at'] is not None


def test_prepare_outside_window_holds_staged(store):
    seed(store)
    deps = FakeDeps(now_ms=NOW_MS + 5 * 3600 * 1000)  # 15:30 JST → window外
    op_id, _ = staged(store, deps)
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.prepare_runtime(store, deps, op_id)
    assert e.value.code == 'OUTSIDE_WINDOW'
    assert state.load(store)['active']['phase'] == 'STAGED'


def test_prepare_drain_incomplete_defers_and_restarts(store):
    seed(store)
    deps = FakeDeps()
    deps.retire_ok = False
    op_id, _ = staged(store, deps)
    with pytest.raises(owner_mod.OwnerError):
        owner_mod.prepare_runtime(store, deps, op_id)
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'DEFERRED'
    assert owner['outcomes'][-1]['operation_id'] == op_id
    assert owner['prepare_budget'][-1]['ended_at'] is not None
    assert deps.restarts == 1


def test_prepare_budget_exceeded(store):
    seed(store)
    deps = FakeDeps()
    op_id, _ = staged(store, deps)
    def fill(new):
        for i in range(3):
            new['prepare_budget'].append({
                'operation_id': f'00000000-0000-4000-8000-00000000000{i}',
                'started_at': deps.now_ms() - 1000, 'ended_at': deps.now_ms(),
                'reserved_ms': 120000})
    owner_mod._mutate(store, fill)
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.prepare_runtime(store, deps, op_id)
    assert e.value.code == 'PREPARE_BUDGET_EXCEEDED'


def test_prepare_resumes_pending_without_duplicate_budget(store):
    seed(store)
    deps = FakeDeps()
    op_id, _ = staged(store, deps)
    # begin相当のPREPARING状態を直接作る(crash後再開を模倣)
    def begin(new):
        a = new['active']
        a['phase'] = 'PREPARING'
        a['prepare_nonce'] = 'a' * 64
        a['prepare_deadline'] = deps.now_ms() + 60000
        a['reserved_owner_bytes'] = 4096
        new['prepare_budget'].append({'operation_id': op_id,
                                      'started_at': deps.now_ms(), 'ended_at': None,
                                      'reserved_ms': 120000})
    owner_mod._mutate(store, begin)
    owner_mod.prepare_runtime(store, deps, op_id)
    owner = state.load(store)
    assert owner['active']['phase'] == 'PREPARED'
    assert owner['active']['prepare_nonce'] == 'a' * 64  # 永続nonce再利用
    assert len([b for b in owner['prepare_budget']
                if b['operation_id'] == op_id]) == 1  # 二重予約なし


def test_prepare_expired_pending_defers(store):
    seed(store)
    deps = FakeDeps()
    op_id, _ = staged(store, deps)
    def begin(new):
        a = new['active']
        a['phase'] = 'PREPARING'
        a['prepare_nonce'] = 'a' * 64
        a['prepare_deadline'] = deps.now_ms() - 1  # 期限切れ
        new['prepare_budget'].append({'operation_id': op_id,
                                      'started_at': deps.now_ms() - 70000,
                                      'ended_at': None, 'reserved_ms': 120000})
    owner_mod._mutate(store, begin)
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.prepare_runtime(store, deps, op_id)
    assert e.value.code == 'PREPARE_EXPIRED'
    assert state.load(store)['active'] is None
    assert deps.restarts == 1


# --- grant / consume --------------------------------------------------------

def _prepared_owner(store, deps):
    op_id, _ = prepared(store, deps)
    return op_id


def test_request_start_returns_verified_grant(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    assert grant['schema_version'] == 'mcs.start-grant.v3'
    assert grant['grant_id'] == GRANT_ID
    assert state.load(store)['active']['phase'] == 'PREPARED'  # requestは状態を変えない


def test_request_start_grant_mismatch_rejected(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    def bad_grant(request, grant):
        grant['target'] = dict(TARGET, artifact_sha256='0' * 64)
        return grant
    deps.grant_override = bad_grant
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.request_start(store, deps)
    assert e.value.code == 'GRANT_MISMATCH'


def test_request_start_expired_grant_rejected(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    def expired(request, grant):
        grant['expires_at'] = deps.now_ms() - 1
        return grant
    deps.grant_override = expired
    with pytest.raises(owner_mod.OwnerError):
        owner_mod.request_start(store, deps)


def test_consume_start_writes_intent_spent_started(store):
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    owner_mod.consume_start(store, deps, grant)
    owner = state.load(store)
    active = owner['active']
    assert active['phase'] == 'STARTED' and active['grant_raw']
    assert active['start_receipt_id'] == GRANT_ID
    assert owner['spent'][0]['operation_id'] == op_id
    assert owner['spent'][0]['grant_id'] == GRANT_ID
    intent = state.load_intent(store)
    assert intent['phase'] == 'open' and intent['operation_id'] == op_id
    assert deps.lock_calls == [('G', 'U', 'Q', 'R', 'C', 'A', 'M')]


def test_consume_rejects_stale_execution_revision(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    # PREPARED後のsafety変更を模倣(execution_revision前進)
    def touch(new):
        new['trust_json'] = '{"touched": true}'
    owner_mod._mutate(store, touch)
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.consume_start(store, deps, grant)
    assert e.value.code in ('STALE_STATE', 'STALE_EXECUTION', 'GRANT_MISMATCH')
    assert state.load(store)['active']['phase'] == 'PREPARED'


def test_consume_rejects_revoked_and_holds(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    deps.revoke = True
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.consume_start(store, deps, grant)
    assert e.value.code == 'CONSENT_REVOKED'
    deps.revoke = False
    deps.holds_list = ['unresolved-x']
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.consume_start(store, deps, grant)
    assert e.value.code == 'HOLD_PRESENT'
    assert deps.lock_calls == []  # lock取得前に拒否


def test_consume_rejects_grant_for_other_operation(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    grant = dict(grant, operation_id='00000000-0000-4000-8000-000000000099')
    with pytest.raises(owner_mod.OwnerError):
        owner_mod.consume_start(store, deps, grant)


def test_request_start_rejects_outside_window(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    deps.now_s = lambda: NOW_S + 7200  # 04:00 JST — window(60-180分)の外
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.request_start(store, deps)
    assert e.value.code == 'OUTSIDE_WINDOW'


def test_consume_rechecks_window_at_fresh_time(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    # lock前のrequest再構築では窓内、lock内の新時刻再確認で窓外(時計進行を模倣)
    ticks = iter([NOW_S, NOW_S + 7200])
    deps.now_s = lambda: next(ticks, NOW_S + 7200)
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.consume_start(store, deps, grant)
    assert e.value.code == 'OUTSIDE_WINDOW'
    assert state.load(store)['active']['phase'] == 'PREPARED'


# --- resume / tick -----------------------------------------------------------

def test_resume_prepared_expired_defers(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    deps.now_ms_value += 121000  # prepare_deadline超過
    info = owner_mod.resume(store, deps)
    assert info['action'] == 'deferred'
    owner = state.load(store)
    assert owner['active'] is None and owner['outcomes'][-1]['state'] == 'DEFERRED'
    assert deps.restarts == 1


def test_resume_started_is_observe_only(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    owner_mod.consume_start(store, deps, grant)
    info = owner_mod.resume(store, deps)
    assert info == {'phase': 'STARTED', 'action': 'observe_only'}
    exec_rev = state.load(store)['execution_revision']
    owner_mod.resume(store, deps)
    assert state.load(store)['execution_revision'] == exec_rev  # 無変更


def test_tick_full_path_to_started(store):
    seed(store)
    deps = FakeDeps()
    result = owner_mod.tick(store, deps)
    assert result['action'] == 'consumed'
    owner = state.load(store)
    assert owner['active']['phase'] == 'STARTED'
    assert owner['spent'] and state.load_intent(store)['phase'] == 'open'
    paths = [p for p, _ in deps.sent]
    assert paths == [owner_mod.SELECT_PATH, owner_mod.START_PATH]


def test_tick_outside_window_stages_but_holds(store):
    seed(store)
    deps = FakeDeps(now_ms=NOW_MS + 5 * 3600 * 1000)
    result = owner_mod.tick(store, deps)
    assert result['action'] == 'hold' and result['code'] == 'OUTSIDE_WINDOW'
    owner = state.load(store)
    assert owner['active']['phase'] == 'STAGED'  # stageは保持、prepareは延期


def test_tick_second_run_observe_only(store):
    seed(store)
    deps = FakeDeps()
    owner_mod.tick(store, deps)
    result = owner_mod.tick(store, deps)
    assert result['action'] == 'observe_only'
    # grant再要求・新operationは発生しない
    assert [p for p, _ in deps.sent].count(owner_mod.START_PATH) == 1


# --- prestart cancel (SEM21 / TX-ABORT) --------------------------------------

def test_cancel_staged_defers_without_central(store):
    seed(store)
    deps = FakeDeps()
    op_id, _ = staged(store, deps)
    result = owner_mod.cancel_prestart(store, deps, op_id)
    assert result == {'action': 'deferred', 'notified': False}
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'DEFERRED'
    assert owner['outcomes'][-1]['operation_id'] == op_id
    assert owner_mod.ABORT_PATH not in [p for p, _ in deps.sent]


def test_cancel_prepared_sends_abort_and_cancels(store):
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)
    attempt_key = state.load(store)['active']['attempt_key']
    owner_mod.request_start(store, deps)  # 中央にoperation(grant)を作成
    active = state.load(store)['active']
    public = active['private_witness']['public']
    result = owner_mod.cancel_prestart(store, deps, op_id,
                                       reason='REVOKED_BEFORE_START')
    assert result == {'action': 'cancelled', 'notified': True}
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'CANCELLED_NO_EFFECT'
    # abort証拠がoperation_evidenceへ記録されている
    entry = next(e for e in owner['operation_evidence']
                 if e['logical_id'] == 'prestart-abort-' + op_id)
    assert entry['object_key'] in deps.evidence
    # 送信bytesはschema-validでprepare/grant束縛を持つ
    abort_raw = deps.evidence[entry['object_key']]
    abort = json.loads(abort_raw)
    assert abort['schema_version'] == 'mcs.prestart-abort.v3'
    assert abort['operation_id'] == op_id
    assert abort['prepare_nonce'] == active['prepare_nonce']
    assert abort['execution_revision'] == public['execution_revision']
    assert abort['reason'] == 'REVOKED_BEFORE_START'
    # grant_idは中央採番値(derivedUuid)を再現 — 未消費でもbinding可能
    assert abort['grant_id'] == owner_mod._expected_grant_id(
        op_id, attempt_key)
    assert [p for p, _ in deps.sent][-1] == owner_mod.ABORT_PATH
    assert deps.restarts == 1
    assert deps.lock_calls[-1] == ('G', 'U', 'Q', 'R', 'C', 'A', 'M')


def test_cancel_rejects_started_and_invalid_reason(store):
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    owner_mod.consume_start(store, deps, grant)
    op_id = state.load(store)['active']['operation_id']
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.cancel_prestart(store, deps, op_id)
    assert e.value.code == 'PHASE_MISMATCH'
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.cancel_prestart(store, deps, op_id, reason='BOGUS')
    assert e.value.code == 'REASON_INVALID'


def test_cancel_pending_resumes_with_identical_bytes(store):
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)
    owner_mod.request_start(store, deps)  # 中央にoperationを作成
    deps.abort_fail = True
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.cancel_prestart(store, deps, op_id)
    assert e.value.code == 'ABORT_PENDING'
    owner = state.load(store)
    assert owner['active']['phase'] == 'PREPARED'  # 終端していない
    assert owner['operation_evidence']  # abort証拠は耐久済み
    # 失敗後にstatus観測(not_foundでない)を挟むため、abort bytesはpathで特定する
    first_raw = next(raw for p, raw in deps.sent
                     if p == owner_mod.ABORT_PATH)
    # abort証拠記録でexec_revが進み、prepared grant束縛は既に無効
    deps.abort_fail = False
    info = owner_mod.resume(store, deps)
    assert info['action'] == 'cancelled'
    assert deps.sent[-1] == (owner_mod.ABORT_PATH, first_raw)  # 同bytes冪等再送
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'CANCELLED_NO_EFFECT'
    assert deps.restarts == 1


def test_cancel_rejected_keeps_prepared_and_evidence(store):
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)
    owner_mod.request_start(store, deps)  # 中央にoperationを作成
    deps.abort_status_override = lambda status, abort: dict(
        status, state='granted')
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.cancel_prestart(store, deps, op_id)
    assert e.value.code == 'ABORT_REJECTED'
    owner = state.load(store)
    assert owner['active']['phase'] == 'PREPARED'  # 終端しない
    assert owner['operation_evidence']  # 証拠は残り再送可能


def test_abort_evidence_blocks_late_consume(store):
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)
    owner_mod.request_start(store, deps)  # 中央にoperationを作成
    deps.abort_fail = True
    with pytest.raises(owner_mod.OwnerError):
        owner_mod.cancel_prestart(store, deps, op_id)
    deps.abort_fail = False
    # abort証拠記録でexec_rev前進 → 旧prepared revisionのgrantはconsume不可
    grant = owner_mod.request_start(store, deps)
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.consume_start(store, deps, grant)
    assert e.value.code in ('STALE_STATE', 'STALE_EXECUTION', 'GRANT_MISMATCH')
    assert state.load(store)['active']['phase'] == 'PREPARED'


def test_abort_evidence_tamper_fails_closed(store):
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)
    owner_mod.request_start(store, deps)  # 中央にoperationを作成
    deps.abort_fail = True
    with pytest.raises(owner_mod.OwnerError):
        owner_mod.cancel_prestart(store, deps, op_id)
    deps.abort_fail = False
    entry = state.load(store)['operation_evidence'][0]
    deps.evidence[entry['object_key']] = b'{"tampered": true}'
    info = owner_mod.resume(store, deps)
    assert info['action'] == 'abort_pending'
    assert info['code'] in ('EVIDENCE_MISMATCH', 'EVIDENCE_UNAVAILABLE')
    assert state.load(store)['active']['phase'] == 'PREPARED'  # 終端しない


def test_central_uuid_matches_central_derived_uuid():
    # CSDC consent.js derivedUuid('start-grant-id', {...}) の実測値との一致。
    # node実機で算出した値をpinする(JS/Python canonical parityの回帰防止)。
    got = owner_mod._central_uuid('start-grant-id', {
        'request_id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        'operation_id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'})
    assert got == '08328f6d-a809-4f6a-9ec3-2996e804b36d'


def test_resume_prepared_expired_aborts_when_start_sent(store):
    """start request送信済みのPREPARED期限切れ: abortでcancelled_prestart確認まで終端。"""
    seed(store)
    deps = FakeDeps()
    _prepared_owner(store, deps)
    owner_mod.request_start(store, deps)  # grant発行・未consume
    deps.now_ms_value += 121000  # prepare_deadline超過
    info = owner_mod.resume(store, deps)
    assert info['action'] == 'cancelled' and info['notified'] is True
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'CANCELLED_NO_EFFECT'
    assert owner_mod.ABORT_PATH in [p for p, _ in deps.sent]
    assert deps.restarts == 1


def test_cancel_prepared_without_start_defers(store):
    """start request未到達のPREPARED取消: 中央not_found確認でlocal DEFERRED終端。"""
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)  # request_start未呼出
    result = owner_mod.cancel_prestart(store, deps, op_id)
    assert result == {'action': 'deferred', 'notified': False}
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'DEFERRED'
    # abort送信試行→404相当→status not_found確認の順で観測した
    paths = [p for p, _ in deps.sent]
    assert paths[-2:] == [owner_mod.ABORT_PATH, owner_mod.STATUS_PATH]


def test_cancel_prepared_released_prestart_converges(store):
    """abort未達でも中央が予約解放済み(cancelled/released_prestart)なら
    status観測でCANCELLED_NO_EFFECTへ収束する — reconciler解放で
    abort receipt無しの経路でも同bytes再送がALREADY_STARTEDで詰まらない。"""
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)
    owner_mod.request_start(store, deps)
    deps.central_ops[op_id] = 'released_prestart'  # reconciler解放を再現
    deps.abort_fail = True
    result = owner_mod.cancel_prestart(store, deps, op_id)
    assert result == {'action': 'cancelled', 'notified': True}
    owner = state.load(store)
    assert owner['active'] is None
    assert owner['outcomes'][-1]['state'] == 'CANCELLED_NO_EFFECT'
    assert deps.restarts == 1


def test_cancel_prepared_status_failure_stays_pending(store):
    """abort失敗+status観測も失敗: STATUS_PENDINGでPREPAREDを保持し再送可能。"""
    seed(store)
    deps = FakeDeps()
    op_id = _prepared_owner(store, deps)
    owner_mod.request_start(store, deps)
    deps.abort_fail = True
    deps.status_fail = True
    with pytest.raises(owner_mod.OwnerError) as e:
        owner_mod.cancel_prestart(store, deps, op_id)
    assert e.value.code == 'STATUS_PENDING'
    owner = state.load(store)
    assert owner['active']['phase'] == 'PREPARED'  # 終端しない
    assert owner['operation_evidence']  # abort証拠は残り再送可能
    # status復旧後のresumeで同bytes再送→cancelled_prestart確認→終端
    deps.abort_fail = False
    deps.status_fail = False
    info = owner_mod.resume(store, deps)
    assert info['action'] == 'cancelled'
