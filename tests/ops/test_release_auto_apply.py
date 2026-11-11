"""v3 code-only effect (M5) を合成Store+fake depsで検証する。

実git/process/networkには触れない。git_runはargvを記録してscripted応答を返す。
fixtureはtest_release_auto_ownerの合成一式を再利用する。
"""
import json

import pytest

import test_release_auto_owner as ot
from adapters.sdc import release_auto_contract as contract
from adapters.sdc import release_auto_state as state
import release_auto_apply as apply_mod
import release_auto_owner as owner_mod

TARGET = ot.TARGET
COMMIT = TARGET['source_commit']


class ApplyDeps(ot.FakeDeps):
    """owner FakeDepsへapply用のgit/journal/measure面を足したfake。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.git_calls = []
        self.git_fail_at = None      # argv部分文字列 → rc=1を返す段階
        self.head = ot.CURRENT['source_commit']
        self.ancestor_rc = 0
        # 実Git.git_inspectと同じ全監査keyを返す — applyは欠落keyを
        # unsafe扱いするため、safe既定は明示的なFalseで構成する。
        self.findings = {k: False for k in
                         ('alternates', 'replace_refs', 'includes',
                          'hooks_path', 'fsmonitor', 'external_filter')}
        self.digest_override = None
        self.measure_override = None
        self.journal = []
        self.apply_effect = self._apply_effect

    def _apply_effect(self, store):
        return apply_mod.apply_code_only_v3(
            store, apply_mod.issue_effect_capability(store), self)

    def bundle_path(self, target):
        return '/synthetic/bundle'

    def repository_path(self):
        return '/synthetic/repo'

    def file_digest(self, path):
        if self.digest_override is not None:
            return self.digest_override
        return {'size': TARGET['artifact_size'],
                'sha256': TARGET['artifact_sha256']}

    def git_inspect(self, repo):
        return dict(self.findings)

    def git_run(self, argv, env=None, timeout_s=None):
        self.git_calls.append((list(argv), dict(env or {}), timeout_s))
        joined = ' '.join(str(a) for a in argv)
        for marker, rc in (self.git_fail_at or {}).items():
            if marker in joined:
                return {'rc': rc, 'stdout': '', 'stderr': 'synthetic failure'}
        if 'rev-parse' in argv and 'HEAD' in argv:
            return {'rc': 0, 'stdout': self.head + '\n', 'stderr': ''}
        if 'merge-base' in argv:
            return {'rc': self.ancestor_rc, 'stdout': '', 'stderr': ''}
        if 'merge' in argv and '--ff-only' in argv:
            self.head = argv[-1]
            return {'rc': 0, 'stdout': '', 'stderr': ''}
        return {'rc': 0, 'stdout': '', 'stderr': ''}

    def measure_state(self):
        if self.measure_override is not None:
            return dict(self.measure_override)
        return {'head': self.head,
                'control_closure_sha256': ot.PROFILE['control_closure_sha256'],
                'config_contract_sha256': '7' * 64,
                'data_contract_sha256': '8' * 64,
                'installed_schema': 9,
                'profile_sha256': ot.PROFILE['profile_sha256']}

    def journal_apply(self, record):
        self.journal.append(record)


@pytest.fixture
def store(tmp_path):
    with ot.Store(tmp_path / 'private-apply', create=True) as value:
        yield value


def seed(store, **over):
    ot.seed(store, **over)


def started(store, deps=None):
    """STARTEDまで進めてoperation_idを返す。"""
    deps = deps or ApplyDeps()
    op_id = ot._prepared_owner(store, deps)
    grant = owner_mod.request_start(store, deps)
    owner_mod.consume_start(store, deps, grant)
    return op_id, deps


def _capability(store):
    return apply_mod.issue_effect_capability(store)


# --- capability ------------------------------------------------------------

def test_issue_capability_requires_started(store):
    seed(store)
    deps = ApplyDeps()
    ot._prepared_owner(store, deps)
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.issue_effect_capability(store)
    assert e.value.code == 'PHASE_MISMATCH'


def test_capability_not_constructible_without_seal(store):
    seed(store)
    _, deps = started(store)
    with pytest.raises(TypeError):
        apply_mod._EffectCapability(object(), operation_id='x')


def test_apply_rejects_non_capability(store):
    seed(store)
    _, deps = started(store)
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, object(), deps)
    assert e.value.code == 'CAPABILITY_INVALID'
    assert state.load(store)['active']['phase'] == 'STARTED'


# --- happy path ------------------------------------------------------------

def test_apply_happy_path_reaches_applied(store):
    seed(store)
    op_id, deps = started(store)
    cap = _capability(store)
    result = apply_mod.apply_code_only_v3(store, cap, deps)
    assert result['phase'] == 'APPLIED'
    owner = state.load(store)
    active = owner['active']
    assert active['phase'] == 'APPLIED'
    assert active['candidate_generation'] == owner['runtime_generation'] + 1
    assert deps.journal[-1]['event'] == 'applied'
    assert deps.journal[-1]['operation_id'] == op_id
    # git系列: bundle verify→fetch→cat-file→rev-parse→merge-base→ff-only→rev-parse→diff-index
    argvs = [argv for argv, _, _ in deps.git_calls]
    assert any('bundle' in a and 'verify' in a for a in argvs)
    assert any('fetch' in a for a in argvs)
    assert any('cat-file' in a for a in argvs)
    assert any('merge-base' in a for a in argvs)
    assert any('diff-index' in a for a in argvs)
    assert any('--ff-only' in ' '.join(argv) for argv, _, _ in deps.git_calls)
    merge_argv = next(argv for argv, _, _ in deps.git_calls if '--ff-only' in argv)
    assert merge_argv[-1] == COMMIT  # exact source_commit・tag再解決なし
    # 制限環境: 全git呼出しにGIT_CONFIG_NOSYSTEM等が渡る
    assert all(env.get('GIT_CONFIG_NOSYSTEM') == '1' for _, env, _ in deps.git_calls)
    assert all(env.get('GIT_CONFIG_GLOBAL') == '/dev/null' for _, env, _ in deps.git_calls)
    # reset/cleanは一度も呼ばない
    assert not any('reset' in argv or 'clean' in argv for argv, _, _ in deps.git_calls)


def test_apply_skips_merge_when_head_already_target(store):
    """crash後再適用: HEADが既にtargetならmergeせず事後検証だけでAPPLIED。"""
    seed(store)
    _, deps = started(store)
    deps.head = COMMIT  # merge済み・APPLIED未保存のcrashを模倣
    result = apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert result['phase'] == 'APPLIED'
    assert not any('--ff-only' in argv for argv, _, _ in deps.git_calls)
    assert state.load(store)['active']['phase'] == 'APPLIED'


def test_tick_with_apply_effect_reaches_applied(store):
    seed(store)
    deps = ApplyDeps()
    result = owner_mod.tick(store, deps)
    assert result['phase'] == 'APPLIED' and result['action'] == 'applied'
    assert state.load(store)['active']['phase'] == 'APPLIED'


# --- failure → UNKNOWN ------------------------------------------------------

def _assert_unknown(store, deps, code):
    owner = state.load(store)
    active = owner['active']
    assert active['phase'] == 'UNKNOWN' and active['error_code'] == code
    # Outcome unknownがoutboxへ固定済み
    entry = next((e for e in owner['outbox']), None)
    assert entry is not None
    # SEM27: OutcomeAckのoutcome_sha256は中央canonicalAuto hash — 送信rawは
    # canonical形(再canonicalizeで不変)でなければACK照合が一致しない。
    assert entry['raw'] == contract.canonical_typed(
        contract.parse_typed(entry['raw'], 'Outcome'),
        'Outcome').decode('utf-8')
    outcome = json.loads(entry['raw'])
    assert outcome['state'] == 'unknown' and outcome['readiness'] == 'unknown'
    assert outcome['error_code'] == 'RESULT_UNKNOWN' and outcome['verified_at'] is None
    # rollback系gitは呼ばれない
    assert not any('reset' in argv or 'clean' in argv for argv, _, _ in deps.git_calls)
    # outbox配送でACK一致まで確認
    assert owner_mod.deliver_outbox(store, deps) == 1
    assert state.load(store)['outbox'][0]['acknowledged'] is True


def test_apply_merge_failure_marks_unknown(store):
    seed(store)
    _, deps = started(store)
    deps.git_fail_at = {'--ff-only': 1}
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code == 'MERGE_FAILED'
    _assert_unknown(store, deps, 'MERGE_FAILED')


def test_apply_not_ff_only_marks_unknown(store):
    seed(store)
    _, deps = started(store)
    deps.ancestor_rc = 1  # HEADがtargetの祖先でない → ff-only不可
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code == 'NOT_FF_ONLY'
    _assert_unknown(store, deps, 'NOT_FF_ONLY')


def test_apply_head_mismatch_marks_unknown(store):
    seed(store)
    _, deps = started(store)
    real_run = deps.git_run

    def lying_run(argv, env=None, timeout_s=None):
        result = real_run(argv, env=env, timeout_s=timeout_s)
        if 'rev-parse' in argv and result.get('stdout', '').strip() == COMMIT:
            result = dict(result, stdout='f' * 40 + '\n')
        return result
    deps.git_run = lying_run
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code in ('HEAD_MISMATCH', 'INVARIANT_HEAD')
    owner = state.load(store)
    assert owner['active']['phase'] == 'UNKNOWN'


def test_apply_bundle_mismatch_marks_unknown(store):
    seed(store)
    _, deps = started(store)
    deps.digest_override = {'size': TARGET['artifact_size'],
                            'sha256': '0' * 64}
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code == 'BUNDLE_MISMATCH'
    _assert_unknown(store, deps, 'BUNDLE_MISMATCH')


def test_apply_unsafe_git_env_marks_unknown(store):
    seed(store)
    _, deps = started(store)
    deps.findings = {'alternates': True}
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code == 'GIT_ENV_UNSAFE'
    _assert_unknown(store, deps, 'GIT_ENV_UNSAFE')


def test_apply_partial_git_findings_marks_unknown(store):
    """監査key欠落のfindingsはunsafe扱い — 部分dictをsafeと誤認しない。"""
    seed(store)
    _, deps = started(store)
    deps.findings = {'alternates': False}  # 残key欠落
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code == 'GIT_ENV_UNSAFE'
    _assert_unknown(store, deps, 'GIT_ENV_UNSAFE')


def test_apply_invariant_violation_marks_unknown(store):
    seed(store)
    _, deps = started(store)
    deps.measure_override = {'head': COMMIT,
                             'control_closure_sha256': '0' * 64,
                             'config_contract_sha256': '7' * 64,
                             'data_contract_sha256': '8' * 64,
                             'installed_schema': 9,
                             'profile_sha256': ot.PROFILE['profile_sha256']}
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code == 'INVARIANT_CONTROL_CLOSURE_SHA256'
    _assert_unknown(store, deps, 'INVARIANT_CONTROL_CLOSURE_SHA256')


def test_apply_rejects_revoked_and_holds(store):
    seed(store)
    _, deps = started(store)
    deps.revoke = True
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code == 'CONSENT_REVOKED'
    assert state.load(store)['active']['phase'] == 'STARTED'  # effect未着手
    deps.revoke = False
    deps.holds_list = ['human-hold']
    with pytest.raises(owner_mod.OwnerError) as e:
        apply_mod.apply_code_only_v3(store, _capability(store), deps)
    assert e.value.code == 'HOLD_PRESENT'
    assert deps.git_calls == []  # git未実行
