"""Synthetic release apply owner native apply contracts; shared helpers preserve isolated runtime state."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
from pathlib import Path
import os
import subprocess
import sys
import pytest
import ledger
import update_journal
import release_apply_owner
from release_apply_owner_testkit import current_release as current_release, installed_current as installed_current, native_product as native_product, native_signing_keys as native_signing_keys, registered_effect as registered_effect
from test_release_dependency_lock import product as product, runtime as runtime
from test_sdc_release_state import native_child as native_child, store as store
from test_sdc_provision import world as world
from test_sdc_provision_profiles import registered as registered


def test_native_preflight_in_a_fresh_synthetic_product_process(native_product, tmp_path):
    import json
    repo, home, platform, supplied = native_product
    program = r'''import json,os,pathlib,sys,time
repo,home=map(pathlib.Path,sys.argv[1:3])
sys.path[:0]=[str(repo/'mcs'),str(repo)]
import _mcs_path
import release_apply_owner as owner
import release_dependency_lock as dependencies
from adapters.sdc import release_state
supplied=json.loads(sys.stdin.read())
platform=supplied.pop('synthetic_platform')
expected_schema=supplied.pop('synthetic_schema')
observed=owner._observe_installed(repo,python=home/'venv/bin/python3',venv=home/'venv',runtime_mode='standalone',platform=platform,ledger_path=home/'data/ledger.db',deadline=time.monotonic()+30)
supplied['repo_fingerprint']=observed['repo_fingerprint']
supplied['baseline'].update({k:observed[k] for k in ('commit','lock_sha256','schema','profile_id','profile_sha256','runtime_fingerprint')})
with owner._control_store(home) as control:
 with control.release_entry_lock(): release_state._bootstrap_verified(control,**supplied)
 result=owner._native_preflight(control,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
 assert result['preconditions']['updater']==[1,0,0]
 assert result['preconditions']['backup_verified'] is True
 assert result['preconditions']['installed_schema']==expected_schema
 assert result['preconditions']['hand_edited'] is False
 assert result['source_sha256']==supplied['owner_evidence_sha256']
 assert control.read(release_state.NAME)['spent']==[]
 assert not (home/'data/update.lock').exists() and not (home/'data/run.lock').exists()
 legacy=home/'data/install_options.json';before=(home/'config.json').read_bytes();state=control.read(release_state.NAME)
 for raw,code in ((b'{"v":1,"install_args":[]}','RELEASE_APPLY_SETTINGS_MIGRATION_REQUIRED'),(b'{broken','RELEASE_APPLY_SETTINGS_MIGRATION_UNVERIFIED')):
  legacy.write_bytes(raw)
  try:owner._native_preflight(control,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
  except owner.ReleaseApplyError as error:assert error.code==code
  else:raise AssertionError('unsettled settings accepted')
  assert legacy.read_bytes()==raw and (home/'config.json').read_bytes()==before and control.read(release_state.NAME)==state
 legacy.unlink()
 path=repo/'mcs/ops/release_apply_owner.py'
 path.write_bytes(path.read_bytes()+b'\n# synthetic changed source\n')
 try: owner._native_preflight(control,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
 except owner.ReleaseApplyError: pass
 else: raise AssertionError('changed provider accepted')
print('synthetic native preflight checked')
'''
    environment = dict(os.environ, HOME=str(tmp_path), MCS_ROOT=str(home), MCS_UPDATE_REPO=str(repo))
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(repo), str(home)],
        input=json.dumps({**supplied, 'synthetic_platform': platform, 'synthetic_schema': ledger.SCHEMA_VERSION}), capture_output=True, text=True,
        env=environment, timeout=80)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith('synthetic native preflight checked\n')


def test_apply_control_signing_rejects_admin_registration_cross_kind_and_unknown_bodies():
    import json
    from adapters.sdc import protocol
    from test_sdc_release_state import APPLY
    now = APPLY['now'] + 20000
    current = dict(origin='https://synthetic.invalid', audience=APPLY['expected']['audience'], environment='synthetic')
    identity = {key: APPLY['expected'][key] for key in ('device_id', 'key_id')}
    raw = json.dumps(APPLY['controls']['admit']).encode()
    for path in ('/admin/v1/support/releases/operations/approve', '/v2/releases/apply/registration/current',
                 '/v2/releases/apply/operations/grant', '/v2/releases/apply/unknown'):
        with pytest.raises(ValueError, match='SDC_REQUEST_INVALID'):
            protocol.sign_release_apply(current, path, raw, identity, 'a1' * 32, now=now / 1000, nonce='b2' * 16)
    for body in (b'x' * 16385, json.dumps({**APPLY['controls']['admit'], 'secret': 'SYNTHETIC_CANARY'}).encode()):
        with pytest.raises(ValueError, match='SDC_REQUEST_INVALID') as error:
            protocol.sign_release_apply(current, '/v2/releases/apply/operations/admit', body, identity, 'a1' * 32,
                now=now / 1000, nonce='b2' * 16)
        assert 'SYNTHETIC_CANARY' not in str(error.value)


def test_release_owned_migration_needed_holds_before_merge(registered_effect, monkeypatch):
    """A spent, bound release capability cannot silently migrate personal settings."""
    import json
    import mcs_update
    import mcs_util
    cap, entry, parameters = registered_effect
    config = Path(entry.runtime_home) / 'config.json'
    legacy = Path(entry.runtime_home) / 'data' / 'install_options.json'
    config.write_text('{}')
    legacy.write_text('{"v": 1, "install_args": ["--no-llm"]}')
    original = config.read_bytes(), legacy.read_bytes()
    monkeypatch.setattr(mcs_util, 'CONF_PATH', str(config))
    monkeypatch.setattr(mcs_util, 'load_config', lambda: {})
    monkeypatch.setattr(mcs_update, 'HOME', entry.runtime_home)
    monkeypatch.setattr(update_apply, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_cli, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_execution, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_lifecycle_context, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_preflight, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_recovery, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(update_runtime_migration, 'HOME', mcs_update.HOME)
    monkeypatch.setattr(mcs_update, 'REPO', entry.repository)
    monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
    monkeypatch.setattr(mcs_update, 'DATA', str(legacy.parent))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(mcs_update, 'STATE_PATH', str(parameters['journal_path']))
    monkeypatch.setattr(update_apply, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_cli, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_execution, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_lifecycle_context, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_planning, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_preflight, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_recovery, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_restore, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_runtime_migration, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(release_apply_owner, '_effect_artifact', lambda *_a: None)
    monkeypatch.setattr(release_apply_owner, '_import_started_artifact', lambda *_a: None)
    monkeypatch.setattr(mcs_update, 'precheck_local', lambda *_a, **_k: [])
    monkeypatch.setattr(update_apply, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(update_planning, 'precheck_local', mcs_update.precheck_local)
    monkeypatch.setattr(mcs_update, '_precheck_target_tree', lambda *_a, **_k: ([], []))
    monkeypatch.setattr(update_apply, '_precheck_target_tree', mcs_update._precheck_target_tree)
    monkeypatch.setattr(update_preflight, '_precheck_target_tree', mcs_update._precheck_target_tree)
    monkeypatch.setattr(mcs_update, '_precheck_target_runtime_files', lambda *_a, **_k: [])
    monkeypatch.setattr(update_apply, '_precheck_target_runtime_files', mcs_update._precheck_target_runtime_files)
    monkeypatch.setattr(update_preflight, '_precheck_target_runtime_files', mcs_update._precheck_target_runtime_files)
    monkeypatch.setattr(mcs_update, '_enqueue_notice', lambda *_a, **_k: True)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(mcs_update, 'quiesce', lambda: pytest.fail('quiesce before migration hold'))
    monkeypatch.setattr(update_apply, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(update_recovery, 'quiesce', mcs_update.quiesce)
    monkeypatch.setattr(mcs_update, '_git', lambda *_a, **_k: pytest.fail('merge before migration hold'))
    monkeypatch.setattr(update_apply, '_git', mcs_update._git)
    monkeypatch.setattr(update_cli, '_git', mcs_update._git)
    monkeypatch.setattr(update_planning, '_git', mcs_update._git)
    monkeypatch.setattr(update_preflight, '_git', mcs_update._git)
    monkeypatch.setattr(update_recovery, '_git', mcs_update._git)
    monkeypatch.setattr(update_repository, '_git', mcs_update._git)
    monkeypatch.setattr(update_restore, '_git', mcs_update._git)
    monkeypatch.setattr(update_runtime_migration, '_git', mcs_update._git)
    arguments = parameters['arguments']
    assert mcs_update._apply_pipeline(*arguments[:6], candidate_sha=arguments[6],
                                      confirm_human=arguments[7], reason=arguments[8], _release_cap=cap) == 1
    state = update_journal.load_state(parameters['journal_path'])
    assert state.get('release_ref') == entry.reference and not state.get('applied')
    assert 'settings_migration_requires_manual_update' in json.dumps(state)
    assert (config.read_bytes(), legacy.read_bytes()) == original
