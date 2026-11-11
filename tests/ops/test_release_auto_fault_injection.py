"""I4相当のM側fault injection: 連続更新A→B→C・process再起動・旧grant再送。

全て合成Store+fake deps。実git/process/network/実データには触れない。
owner→apply→runtime proofの全段をowner.tick経由で駆動し、各opの
release/manifest/publication identityを一意に変えて逐次検証する。
"""
import copy
import hashlib
import uuid

import pytest

import test_release_auto_apply as at
import test_release_auto_owner as ot
import test_release_runtime_proof as rt_test
from adapters.sdc import release_auto_state as state
import release_auto_apply as apply_mod
import release_auto_owner as owner_mod
import release_runtime_proof as rt


class FaultDeps(rt_test.RuntimeDeps):
    """連続op対応: file_digestを現在opのtargetへ合わせ、grant_idを一意化。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active_target = None

    def _grant_for(self, request):
        grant = super()._grant_for(request)
        # grant_idはop毎に一意必須(record_spentのGRANT_REUSE拒否)。
        # FakeDeps既定は固定GRANT_IDなのでoperation_idからv4形式を導出する。
        raw = bytearray(hashlib.sha256(
            ('grant|' + request['operation_id']).encode()).digest()[:16])
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        grant['grant_id'] = str(uuid.UUID(bytes=bytes(raw)))
        return grant

    def file_digest(self, path):
        target = self.active_target or ot.TARGET
        return {'size': target['artifact_size'],
                'sha256': target['artifact_sha256']}


def _offer_for(letter, seq):
    """release/manifest/publication identityを一意に変えたofferを返す。"""
    offer = copy.deepcopy(ot.OFFER)
    sel = offer['selected']
    rel = sel['release']
    rel.update({
        'release_id': hashlib.sha256(f'release-{letter}'.encode()).hexdigest(),
        'code_seq': seq,
        'source_commit': hashlib.sha256(f'commit-{letter}'.encode()).hexdigest()[:40],
        'artifact_sha256': hashlib.sha256(f'artifact-{letter}'.encode()).hexdigest(),
        'artifact_size': 4096 + seq})
    sel['manifest_digest'] = hashlib.sha256(f'manifest-{letter}'.encode()).hexdigest()
    sel['compatibility_sha256'] = hashlib.sha256(f'compat-{letter}'.encode()).hexdigest()
    sel['publication_id'] = str(uuid.uuid4())
    sel['publication_proof_sha256'] = hashlib.sha256(
        f'pubproof-{letter}'.encode()).hexdigest()
    sel['acceptance_sha256'] = hashlib.sha256(f'acc-{letter}'.encode()).hexdigest()
    return offer


def _use_offer(deps, offer):
    deps.offer_override = offer
    deps.active_target = offer['selected']['release']
    sel = offer['selected']
    deps.meta.update({'publication_id': sel['publication_id'],
                      'manifest_digest': sel['manifest_digest'],
                      'compatibility_sha256': sel['compatibility_sha256']})


def _drive(store, deps, limit=80):
    """activeが現れて終端(None)になるまでtickを回す。"""
    saw_active = False
    last_info = None
    for _ in range(limit):
        if state.load(store)['active'] is not None:
            saw_active = True
        last_info = owner_mod.tick(store, deps)
        if saw_active and state.load(store)['active'] is None:
            owner_mod.tick(store, deps)  # outbox/終端を1回流す
            return state.load(store)
    owner = state.load(store)
    raise AssertionError(
        f"owner did not converge: phase="
        f"{owner['active']['phase'] if owner['active'] else None} "
        f"last_info={last_info} outcomes={owner.get('outcomes')}")


def _verified_target(store):
    return state.load(store)['verified_installed']['release_id']


# --- 連続更新 A→B→C ---------------------------------------------------------

def test_consecutive_updates_a_b_c(tmp_path):
    """VERIFIED済みopの直後に新しいofferで次opが開始・完走できる。"""
    with ot.Store(tmp_path / 'private-i4', create=True) as store:
        at.seed(store)
        deps = FaultDeps()
        # A: 既定OFFER (release code_seq=2)
        owner_a = _drive(store, deps)
        assert owner_a['active'] is None, \
            f"outcomes={owner_a.get('outcomes')} active={owner_a.get('active')}"
        assert _verified_target(store) == ot.TARGET['release_id'], \
            f"outcomes={owner_a.get('outcomes')} vi={_verified_target(store)}"
        # B: 一意identityのofferへ切替
        offer_b = _offer_for('B', 3)
        _use_offer(deps, offer_b)
        owner_b = _drive(store, deps)
        assert owner_b['active'] is None
        assert _verified_target(store) == \
            offer_b['selected']['release']['release_id']
        # C
        offer_c = _offer_for('C', 4)
        _use_offer(deps, offer_c)
        owner_c = _drive(store, deps)
        assert owner_c['active'] is None
        # verified_installedがCへ進み、現在releaseもCであること
        assert _verified_target(store) == \
            offer_c['selected']['release']['release_id']
        installed = state.load(store)['installed']
        assert installed['source_commit'] == \
            offer_c['selected']['release']['source_commit']


def test_retire_intent_crash_does_not_block_next_update(tmp_path,
                                                        monkeypatch):
    """finalize_verified後・retire_intent前のcrash: intent='open'残存でも
    terminal証拠があれば次opが開始できる(連続更新を止めない)。"""
    with ot.Store(tmp_path / 'private-i4h', create=True) as store:
        at.seed(store)
        deps = FaultDeps()
        real_retire = state.retire_intent
        crashed = []

        def flaky_retire(*a, **k):
            if not crashed:
                crashed.append(1)
                raise RuntimeError('synthetic crash')
            return real_retire(*a, **k)

        monkeypatch.setattr(state, 'retire_intent', flaky_retire)
        # A: VERIFIED確定(finalize)は成功しretire_intentでcrash
        with pytest.raises(RuntimeError):
            _drive(store, deps)
        owner = state.load(store)
        assert owner['active'] is None
        assert any(o['state'] == 'VERIFIED' for o in owner['outcomes'])
        assert state.load_intent(store)['phase'] == 'open'
        # B: open残存intentをterminal証拠で置き換えて完走する
        offer_b = _offer_for('B', 3)
        _use_offer(deps, offer_b)
        owner_b = _drive(store, deps)
        assert owner_b['active'] is None
        assert _verified_target(store) == \
            offer_b['selected']['release']['release_id']
        # intentはBのopを指す(Aのopen残存は置き換え済み)
        assert state.load_intent(store)['operation_id'] == \
            owner_b['outcomes'][-1]['operation_id']


def test_second_update_rejects_stale_offer(tmp_path):
    """A適用後にAより古いcode_seqのofferはstageされない(fail-closed)。"""
    with ot.Store(tmp_path / 'private-i4b', create=True) as store:
        at.seed(store)
        deps = FaultDeps()
        _drive(store, deps)  # A完走
        stale = _offer_for('S', 1)  # code_seq=1 < installed seq=2
        _use_offer(deps, stale)
        owner_mod.tick(store, deps)
        assert state.load(store)['active'] is None


# --- process再起動 -----------------------------------------------------------

def test_restart_at_applied_resumes_to_verified(tmp_path):
    """APPLIED永続化後にStore/depsを作り直してもruntime proofが完走する。"""
    root = tmp_path / 'private-i4c'
    with ot.Store(root, create=True) as store:
        at.seed(store)
        deps = rt_test.RuntimeDeps()
        at.started(store, deps)
        apply_mod.apply_code_only_v3(store, at._capability(store), deps)
        assert state.load(store)['active']['phase'] == 'APPLIED'
    # 「再起動」: 同じrootを新しいStore+depsで開き直す
    deps2 = rt_test.RuntimeDeps()
    with ot.Store(root) as store:
        owner_mod.resume(store, deps2)
        for _ in range(10):
            active = state.load(store)['active']
            if active is None:
                break
            rt.advance_runtime(store, deps2)
        owner = state.load(store)
        assert owner['active'] is None
        assert owner['verified_installed']['release_id'] == ot.TARGET['release_id']


# --- 旧grant再送 -------------------------------------------------------------

def test_consumed_grant_cannot_be_replayed(tmp_path):
    """consume済みgrantの再送は拒否される(SPENT/phaseでfail-closed)。"""
    with ot.Store(tmp_path / 'private-i4d', create=True) as store:
        at.seed(store)
        deps = rt_test.RuntimeDeps()
        ot._prepared_owner(store, deps)
        grant = owner_mod.request_start(store, deps)
        owner_mod.consume_start(store, deps, grant)
        with pytest.raises(owner_mod.OwnerError):
            owner_mod.consume_start(store, deps, grant)
        # 状態はSTARTEDのまま(二重effectなし)
        assert state.load(store)['active']['phase'] == 'STARTED'


# --- consume_start耐久化点のcrash注入 ----------------------------------------

def test_crash_after_open_intent_holds_fail_closed(tmp_path, monkeypatch):
    """intent書込み後・START_INTENT前のcrash: open intentが残り再送は拒否。"""
    with ot.Store(tmp_path / 'private-i4e', create=True) as store:
        at.seed(store)
        deps = rt_test.RuntimeDeps()
        ot._prepared_owner(store, deps)
        grant = owner_mod.request_start(store, deps)

        def boom(*a, **k):
            raise RuntimeError('synthetic crash')

        monkeypatch.setattr(owner_mod, '_mutate', boom)
        with pytest.raises(RuntimeError):
            owner_mod.consume_start(store, deps, grant)
        monkeypatch.undo()
        # intent='open'・active=PREPAREDが残り、推測進行せずholdする
        assert state.load_intent(store)['phase'] == 'open'
        assert state.load(store)['active']['phase'] == 'PREPARED'
        info = owner_mod.resume(store, deps)
        assert info['action'] == 'await_grant'
        with pytest.raises(state.OwnerStateError, match='INTENT_EXISTS'):
            owner_mod.consume_start(store, deps, grant)
        assert state.load(store)['active']['phase'] == 'PREPARED'


def test_crash_after_start_intent_observes_without_advancing(
        tmp_path, monkeypatch):
    """START_INTENT永続化後・SPENT前のcrash: observe_onlyで保持・再consume拒否。"""
    with ot.Store(tmp_path / 'private-i4f', create=True) as store:
        at.seed(store)
        deps = rt_test.RuntimeDeps()
        ot._prepared_owner(store, deps)
        grant = owner_mod.request_start(store, deps)

        def boom(*a, **k):
            raise RuntimeError('synthetic crash')

        monkeypatch.setattr(state, 'record_spent', boom)
        with pytest.raises(RuntimeError):
            owner_mod.consume_start(store, deps, grant)
        monkeypatch.undo()
        assert state.load(store)['active']['phase'] == 'START_INTENT'
        info = owner_mod.resume(store, deps)
        assert info['action'] == 'observe_only'
        # phaseがPREPAREDでないためconsume再送は拒否(二重start防止)
        with pytest.raises(owner_mod.OwnerError):
            owner_mod.consume_start(store, deps, grant)


def test_crash_after_spent_keeps_grant_burned(tmp_path, monkeypatch):
    """SPENT永続化後・STARTED前のcrash: spent記録が残りgrantは消費済み扱い。"""
    with ot.Store(tmp_path / 'private-i4g', create=True) as store:
        at.seed(store)
        deps = rt_test.RuntimeDeps()
        ot._prepared_owner(store, deps)
        grant = owner_mod.request_start(store, deps)
        real_mutate = owner_mod._mutate
        calls = []

        def flaky(store_, fn):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('synthetic crash')
            return real_mutate(store_, fn)

        monkeypatch.setattr(owner_mod, '_mutate', flaky)
        with pytest.raises(RuntimeError):
            owner_mod.consume_start(store, deps, grant)
        monkeypatch.undo()
        owner = state.load(store)
        assert owner['active']['phase'] == 'START_INTENT'
        # SPENTは耐久化済み: grant_idがspentへ記録され二重消費を防ぐ
        assert [s['grant_id'] for s in owner['spent']] == [grant['grant_id']]
        info = owner_mod.resume(store, deps)
        assert info['action'] == 'observe_only'
        with pytest.raises(owner_mod.OwnerError):
            owner_mod.consume_start(store, deps, grant)
