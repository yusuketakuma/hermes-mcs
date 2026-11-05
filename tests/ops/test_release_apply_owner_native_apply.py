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
import copy
import os
import subprocess
import sys
import time
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


@pytest.mark.parametrize('kind', ['plan_report', 'preview', 'accept', 'poll', 'admit', 'grant', 'report', 'status'])
def test_fixed_apply_controls_use_fresh_device_hmac_and_immutable_request_bytes(kind):
    import hashlib
    import hmac
    import json
    from adapters.sdc import protocol
    from adapters.sdc import release_apply_contract as contract
    from adapters.sdc.support_contract import canonical
    from test_sdc_release_state import APPLY
    now = APPLY['now'] + 20000
    scope = {key: APPLY['controls']['grant']['state'][key] for key in (
        'product', 'trust_domain', 'environment', 'stream_id', 'tenant', 'installation', 'device_id', 'realm_epoch')}
    current = release_apply_owner._ReleaseCurrentness(
        bootstrap={'release_endpoint': {'origin': 'https://synthetic.invalid', 'audience': APPLY['expected']['audience'], 'environment': scope['environment']}},
        binding={'synthetic': True}, scope=scope,
        identity={key: APPLY['expected'][key] for key in ('claim_id', 'device_id', 'key_id')}, secret='a1' * 32)
    body = copy.deepcopy((dict(control=APPLY['controls']['accept'], receipt=APPLY['localReceipt']) if kind == 'accept' else APPLY['controls'][kind]))
    first = release_apply_owner._prepare_apply_control(current, kind, body, now_ms=now, deadline=time.monotonic() + 10)
    second = release_apply_owner._prepare_apply_control(current, kind, body, now_ms=now, deadline=time.monotonic() + 10)
    assert protocol.RELEASE_APPLY_PATHS[first.path] == kind
    assert first.body == second.body == canonical(body).encode('ascii')
    headers = dict(first.headers)
    assert headers['X-SDC-Nonce'] != dict(second.headers)['X-SDC-Nonce']
    message = '\n'.join(('SDC-HMAC-v1', current.bootstrap['release_endpoint']['audience'], 'POST', first.path,
        current.identity['device_id'], current.identity['key_id'], str(now // 1000), headers['X-SDC-Nonce'], hashlib.sha256(first.body).hexdigest()))
    assert headers['X-SDC-Signature'] == hmac.new(bytes.fromhex(current.secret), message.encode(), hashlib.sha256).hexdigest()
    body['synthetic_changed_after_prepare'] = True
    assert 'synthetic_changed_after_prepare' not in json.loads(first.body)
    assert current.secret not in repr(current) + repr(first)
    assert protocol._release_apply_body(kind, first.body, now_ms=now)
    if kind == 'accept':
        with pytest.raises(ValueError):
            protocol.sign_release_apply(current.bootstrap['release_endpoint'], first.path, canonical(APPLY['controls']['accept']).encode('ascii'), current.identity, current.secret, now=now / 1000, nonce='b2' * 16)
        invalid = json.loads(first.body)
        invalid['receipt']['receipt_hash'] = contract.ZERO
        with pytest.raises(ValueError):
            protocol.sign_release_apply(current.bootstrap['release_endpoint'], first.path, canonical(invalid).encode('ascii'), current.identity, current.secret, now=now / 1000, nonce='b2' * 16)
    for suffix in ('/', '?other=1'):
        with pytest.raises(ValueError):
            protocol.sign_release_apply(current.bootstrap['release_endpoint'], first.path + suffix, first.body,
                current.identity, current.secret, now=now / 1000, nonce='b2' * 16)
    for old in (protocol.sign_support, protocol.sign_restart, protocol.sign_release_registration):
        with pytest.raises(ValueError):
            old(current.bootstrap['release_endpoint'], first.path, first.body, current.identity, current.secret,
                now=now / 1000, nonce='b2' * 16)


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


@pytest.mark.parametrize('failure', [None, 'global-hook', 'signature', 'artifact', 'schema', 'disabled', 'uncertain_write',
    'run-registration', 'run-operation', 'run-grant', 'run-cancel', 'run-epoch', 'run-fsync-expiry', 'run-fsync-rollback', 'run-start-uncertain', 'run-unfinished-journal', 'run-git-attributes', 'run-git-grafts',
    'run-settings-required', 'run-settings-unverified', 'run-settings-late', 'run-settings-expiry'])
def test_native_plan_freezes_independent_report_after_real_tty_signature_and_artifact_checks(native_product, registered, tmp_path, native_signing_keys, failure):
    # Development-only accepted root and synthetic credentials; no authorization or crypto is mocked.
    import base64
    import hashlib
    import json
    import release_owner_source as source
    import sdc_support_signatures as signatures
    from adapters.sdc import provision, release_contract as contract
    from adapters.sdc.support_contract import canonical
    from test_sdc_provision import NOW
    from test_sdc_provision_profiles import install
    from test_sdc_support_state import preview as support_preview
    from test_sdc_release_state import APPLY, VECTORS
    repo, home, platform, supplied = native_product
    install(registered, 'support')
    assert registered.read('credential-store.json') is None
    snapshot = provision.profile_snapshot(registered, profile='monitor', now_ms=NOW)
    identity = registered.read('state.json')
    supplied['trust']['environment'] = snapshot['bundle']['environment']
    supplied['state'].update({key: snapshot['bundle'][key] for key in ('tenant', 'installation', 'environment', 'realm_epoch')})
    supplied['state']['device_id'] = identity['device_id']
    parent_consent = support_preview(now=NOW,
        **{key: snapshot['bundle'][key] for key in ('tenant', 'installation', 'environment', 'realm_epoch')},
        **{key: identity[key] for key in ('claim_id', 'device_id', 'key_id')},
        audience='sdc:support', case_id='00000000-0000-4000-8000-000000000009')
    signing = tmp_path / 'synthetic-signing'
    signing.mkdir(mode=0o700)
    private, operator_private, registration_private = (
        signing / name for name in ('test-only-key.pem', 'test-only-operator.pem', 'test-only-registration.pem'))
    for path, (pem, _) in zip((private, operator_private, registration_private), native_signing_keys):
        path.touch(mode=0o600, exist_ok=False)
        path.write_bytes(pem)
    public, operator_public, registration_public = (public for _, public in native_signing_keys)
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')  # noqa: E731
    operation_pins = [dict(kid='synthetic-operator', spki=b64(operator_public), purposes=APPLY['pins'][0]['purposes'], not_before=NOW - 1000, expires_at=NOW + 600000)]
    registration_pins = [dict(kid='synthetic-registration', spki=b64(registration_public), purposes=['release-apply-registration-current'], not_before=NOW - 1000, expires_at=NOW + 600000)]
    spki = b64(public)
    kid = signatures.release_key_fingerprint(spki)
    supplied['trust']['release_keys'] = [dict(kid=kid, spki=spki, not_before=NOW - 1000, expires_at=NOW + 600000)]
    supplied['state']['trust_digest'] = contract.hash_release_trust(supplied['trust'])
    target = tmp_path / 'target'
    subprocess.run(['/usr/bin/git', 'clone', '--no-hardlinks', '--quiet', str(repo), str(target)], check=True, capture_output=True, timeout=30)
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(target), *args], text=True).strip()
    (target / 'mcs/ops/p2_fixture_marker.py').write_text('raise AssertionError("CANDIDATE_MUST_NOT_EXECUTE")\n')
    git('add', 'mcs/ops/p2_fixture_marker.py')
    target_manifest = source.build_manifest(target, deadline=time.monotonic() + 30)
    (target / source.MANIFEST_PATH).write_bytes(source.canonical(target_manifest))
    git('add', source.MANIFEST_PATH)
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic target')
    bundle = tmp_path / 'target.bundle'
    git('bundle', 'create', '--version=2', str(bundle), '--all')
    raw = bundle.read_bytes()
    attestation = copy.deepcopy(VECTORS['Attestation'])
    core = attestation['core']
    for key in contract.EXPECTED_FIELDS:
        attestation[key] = core[key] = supplied['state'][key]
    core.update(code_seq=supplied['state']['highest_seen']['code_seq'] + 1, commit=git('rev-parse', 'HEAD'),
        lock_sha256=hashlib.sha256((repo / 'deployment/sdc-release-lock.json').read_bytes()).hexdigest(),
        artifact=dict(format='git-bundle-v2', object_format='sha1', sha256=hashlib.sha256(raw).hexdigest(), size=len(raw)),
        platforms=[platform], minimum_updater=[1, 0, 0],
        schema=dict(accepts_installed=[ledger.SCHEMA_VERSION - (failure == 'schema')],
                    target=ledger.SCHEMA_VERSION - (failure == 'schema'),
                    migration='none', rollback_requires_db_restore=False))
    attestation.update(metadata_epoch=supplied['trust']['metadata_epoch'], release_id=contract.hash_release_core(core),
        issued_at=NOW, not_before=NOW, expires_at=NOW + 60000)
    payload = canonical(attestation).encode('ascii')
    message = b'SDC-SIGNED-v1\nrelease-attestation\n' + kid.encode() + b'\n' + payload
    result = subprocess.run([signatures.OPENSSL, 'dgst', '-sha256', '-sign', str(private),
        '-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32', '-sigopt', 'rsa_mgf1_md:sha256'],
        input=message, capture_output=True, check=True, timeout=10)
    envelope = dict(alg='PS256', kid=kid, purpose='release-attestation', payload=b64(payload), signature=b64(result.stdout))
    # The child opens these existing synthetic profile files, never a real Keychain account.
    packet = dict(supplied=supplied, platform=platform, now=NOW, envelope=envelope, expected_release_id=attestation['release_id'], failure=failure, parent_consent=parent_consent,
        expected_target_source_sha256=source.digest(target_manifest), pins=operation_pins, operator_private=str(operator_private), target_commit=core['commit'],
        registration_pins=registration_pins, registration_private=str(registration_private))
    program = r'''import errno,hashlib,hmac,json,os,pathlib,pty,select,signal,sys,time
repo,home,monitor_root,bundle=map(pathlib.Path,sys.argv[1:5])
packet=json.loads(sys.stdin.read())
pid,master=pty.fork()
if pid==0:
 try:
  sys.path[:0]=[str(repo/'mcs'),str(repo)]
  import _mcs_path
  import release_apply_owner as owner
  import release_dependency_lock as dependencies
  from adapters.sdc import provision,release_contract as contract,release_state
  from adapters.sdc.storage import Store
  time.time=lambda:packet['now']/1000
  supplied=packet['supplied']
  observed=owner._observe_installed(repo,python=home/'venv/bin/python3',venv=home/'venv',runtime_mode='standalone',platform=packet['platform'],ledger_path=home/'data/ledger.db',deadline=time.monotonic()+30)
  supplied['repo_fingerprint']=observed['repo_fingerprint']
  supplied['baseline'].update({k:observed[k] for k in ('commit','lock_sha256','schema','profile_id','profile_sha256','runtime_fingerprint')})
  with Store(monitor_root) as monitor,owner._control_store(home) as control:
   with control.release_entry_lock():release_state._bootstrap_verified(control,**supplied)
   monitor_snapshot=provision.profile_snapshot(monitor,profile='monitor',now_ms=packet['now'])
   support_snapshot=provision.profile_snapshot(monitor,profile='support',now_ms=packet['now'])
   identity=monitor.read('state.json');enrollment=monitor.enrollment_binding()
   bound={k:enrollment[k] for k in ('root_fingerprint','endpoint_hash')}
   bound.update({k:identity[k] for k in ('claim_id','device_id','key_id')})
   bound.update({k:monitor_snapshot['bundle'][k] for k in ('tenant','installation','environment','realm_epoch')})
   control.write('release-bootstrap.json',dict(schema_version='mcs.release-bootstrap.v1',control_root_fingerprint=control.root_fingerprint(),monitor_binding=bound,support_snapshot_sha256=monitor._fingerprint(support_snapshot),release_endpoint=dict(origin='https://synthetic-release.invalid',audience='synthetic-release',environment=bound['environment']),operation_pins=packet['pins'],registration_pins=packet['registration_pins'],initial_trust_sha256=contract.hash_release_trust(supplied['trust'])))
   consent=owner._issue_planning_consent(control,monitor,runtime_home=home,repository=repo,case_id='00000000-0000-4000-8000-000000000009',deadline=time.monotonic()+30)
   assert consent is not None
   failure=packet['failure']
   if failure=='signature':
    import base64
    bad=bytearray(base64.urlsafe_b64decode(packet['envelope']['signature']+'='*(-len(packet['envelope']['signature'])%4)))
    bad[0]^=1
    packet['envelope']['signature']=base64.urlsafe_b64encode(bad).rstrip(b'=').decode()
   elif failure=='artifact':
    raw=bundle.read_bytes();bundle.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
   elif failure=='disabled':
    from contextlib import contextmanager
    original=owner._inspect_artifact
    @contextmanager
    def changed(*args,**kwargs):
     with original(*args,**kwargs) as result:
      with monitor.lock():
       state=monitor.read('state.json');state['enabled']=False;monitor.write('state.json',state)
      yield result
    owner._inspect_artifact=changed
   elif failure=='uncertain_write':
    original=control.write
    def uncertain(name,value,**kwargs):
     original(name,value,**kwargs)
     if name=='release-plan.json':raise OSError('synthetic post-publish uncertainty')
    control.write=uncertain
   try:
    record,prepared=owner._prepare_native_plan(control,monitor,runtime_home=home,repository=repo,case_id=consent['case_id'],attestation_envelope=packet['envelope'],artifact_source=bundle,deadline=time.monotonic()+120)
   except owner.ReleaseApplyError:
    assert failure in ('signature','artifact','schema','disabled','uncertain_write')
    assert (control.read('release-plan.json') is not None)==(failure=='uncertain_write')
    saved=release_state.status(control)
    assert saved['spent']==[] and saved['state']['unresolved'] is None
    if failure=='signature':assert saved['state']['highest_seen']==supplied['state']['highest_seen']
    else:assert saved['state']['highest_seen']['release_id']==packet['expected_release_id']
    assert saved['uncertain']==(failure=='uncertain_write')
    assert not owner._EFFECT_CAPS
    assert not (home/'data/update.lock').exists() and not (home/'data/run.lock').exists()
    os._exit(0)
   assert failure in (None,'global-hook') or failure.startswith('run-')
   assert owner._read_frozen_plan(control)==record
   body=json.loads(prepared.body)
   assert prepared.path=='/v2/releases/apply/plans/report'
   assert record['raw'].encode()==prepared.body
   headers=dict(prepared.headers)
   message='\n'.join(('SDC-HMAC-v1','synthetic-release','POST',prepared.path,identity['device_id'],identity['key_id'],str(packet['now']//1000),headers['X-SDC-Nonce'],hashlib.sha256(prepared.body).hexdigest()))
   assert headers['X-SDC-Signature']==hmac.new(bytes.fromhex('a'*64),message.encode(),hashlib.sha256).hexdigest()
   assert hashlib.sha256(prepared.body).hexdigest()==record['body_sha256']
   assert body['local_planning_consent_id']==consent['planning_consent_id']
   assert body['plan']['preconditions']['updater']==[1,0,0]
   assert body['plan']['modes']==[]
   assert record['target_source_sha256']==packet['expected_target_source_sha256']
   assert body['plan']['release_id']==packet['expected_release_id']
   assert release_state.status(control)['spent']==[]
   assert release_state.status(control)['state']['unresolved'] is None
   assert not owner._EFFECT_CAPS
   assert not (home/'data/update.lock').exists() and not (home/'data/run.lock').exists()
   from adapters.sdc import support_client,support_state
   endpoint={k:support_snapshot['bundle'][k] for k in ('origin','audience','environment')}
   binding=dict(root_fingerprint=monitor.root_fingerprint(),monitor_endpoint_hash=monitor.enrollment_binding()['endpoint_hash'],support_endpoint_hash=monitor._fingerprint(endpoint),pins_sha256=support_client._hash(support_snapshot['bundle']['pins']),secret_sha256=hashlib.sha256(bytes.fromhex('a'*64)).hexdigest(),key_id=identity['key_id'],profile=support_client._profile_binding(support_snapshot,now_ms=packet['now']))
   before=support_state.prepare(monitor,binding_supplier=lambda:binding,now_ms=packet['now'])
   support_state.confirm_consent(monitor,packet['parent_consent'],expected_owner_generation=before['owner_generation'],expected_binding=binding,binding_supplier=lambda:binding,now_ms=packet['now'],confirm_human=True)
   case=owner._capture_release_case(control,monitor,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
   for k in ('case_id','case_epoch','local_consent_epoch','device_epoch','policy_epoch'):
    assert case['expected'][k]==packet['parent_consent'][k]
   assert case['expected']['plan_hash']==contract.hash_release_plan(body['plan'])
   from adapters.sdc import release_apply_contract as apply
   from adapters.sdc.support_contract import canonical
   import base64,copy,subprocess
   operation_id='00000000-0000-4000-8000-000000000051'
   change,preview_request=owner._begin_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,deadline=time.monotonic()+30)
   assert owner._read_change(control)==change
   assert change['preview_raw'].encode()==preview_request.body
   assert change['expected']['case_epoch']==packet['parent_consent']['case_epoch']
   assert change['local_receipt'] is None and change['accept_raw'] is None
   def signed_payload(value,purpose,*,registration=False):
    raw=canonical(value).encode('ascii');kid='synthetic-registration' if registration else 'synthetic-operator'
    key=packet['registration_private'] if registration else packet['operator_private']
    signature=subprocess.check_output(['/usr/bin/openssl','dgst','-sha256','-sign',key,'-sigopt','rsa_padding_mode:pss','-sigopt','rsa_pss_saltlen:32','-sigopt','rsa_mgf1_md:sha256'],input=b'SDC-SIGNED-v1\n'+purpose.encode()+b'\n'+kid.encode()+b'\n'+raw,stderr=subprocess.DEVNULL,timeout=10)
    b64=lambda raw:base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
    return dict(alg='PS256',kid=kid,purpose=purpose,payload=b64(raw),signature=b64(signature))
   def signed_preview(*,foreign_plan=False):
    now=packet['now'];common={k:change['expected'][k] for k in apply.COMMON if k!='product'};common['product']='hermes-mcs'
    intent=dict(schema_version=apply.VERSIONS['intent'],**common,operation_id=operation_id,type='release.apply',purpose_code=case['purpose_code'],requested_by='a'*64,plan_report_id=body['report_id'],plan_report_hash=record['body_sha256'],window=body['plan']['window'],canary_stage='self_test',completion_timeout_ms=apply.COMPLETION_TIMEOUT_MS,issued_at=now,not_before=now,expires_at=body['plan']['expires_at'])
    if foreign_plan:intent['plan_report_hash']='f'*64
    intent_hash=apply.hash_apply_intent(intent)
    approval=dict(schema_version=apply.VERSIONS['approval'],**common,approval_id='00000000-0000-4000-8000-000000000052',intent_hash=intent_hash,approved_by='b'*64,approval_kind='individual',issued_at=now,not_before=now,expires_at=intent['expires_at'],approval_hash=apply.ZERO)
    approval['approval_hash']=apply.hash_apply_approval(approval)
    preview=dict(schema_version=apply.VERSIONS['preview'],**common,operation_id=operation_id,preview_id=change['expected']['preview_id'],intent=intent,approval=approval,intent_hash=intent_hash,change_consent_id='00000000-0000-4000-8000-000000000053',change_binding_hash=apply.ZERO,issued_at=now,not_before=now,expires_at=intent['expires_at'])
    preview['change_binding_hash']=apply.hash_apply_change_binding(preview)
    return dict(schema_version='sdc.release.apply-preview-response.v1',intent=intent,approval=approval,preview=signed_payload(preview,apply.PURPOSES[0]))
   response=signed_preview()
   invalid=copy.deepcopy(response);invalid['preview']['signature']=invalid['preview']['signature'][::-1]
   foreign=signed_preview(foreign_plan=True)
   outer=copy.deepcopy(response);outer['approval']['approved_by']='c'*64
   typed_outer=copy.deepcopy(response);typed_outer['intent']['policy_epoch']=float(typed_outer['intent']['policy_epoch'])
   for denied in (invalid,foreign,outer,typed_outer):
    try:owner._consent_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,preview_response=json.dumps(denied,ensure_ascii=True).encode('ascii'),deadline=time.monotonic()+30)
    except owner.ReleaseApplyError:pass
    else:raise AssertionError('invalid preview accepted')
    assert owner._read_change(control)==change
   saved,accept_request=owner._consent_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,preview_response=json.dumps(response,ensure_ascii=True).encode('ascii'),deadline=time.monotonic()+30)
   assert saved['accept_raw'].encode()==accept_request.body
   receipt=saved['local_receipt'];accept_body=json.loads(accept_request.body)
   assert accept_body['receipt']==receipt and accept_body['control']['accept_id']==receipt['change_accept_id']
   assert receipt['receipt_hash']==apply.hash_apply_local_receipt(receipt)
   assert saved['reason_sha256']==hashlib.sha256(b'synthetic independent plan').hexdigest()
   ack=dict(schema_version=apply.VERSIONS['acceptance'],accept_id=receipt['change_accept_id'],**{k:receipt[k] for k in ('preview_id','operation_id','change_consent_id','change_binding_hash','local_change_receipt_id','preview_payload_hash')},local_change_receipt_hash=receipt['receipt_hash'],state='accepted',accepted_at=packet['now'],expires_at=receipt['expires_at'])
   bad_ack={**ack,'local_change_receipt_hash':'e'*64}
   try:owner._accept_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,acceptance_response=canonical(dict(schema_version='sdc.release.apply-accept-response.v1',acceptance=bad_ack)).encode('ascii'),deadline=time.monotonic()+30)
   except owner.ReleaseApplyError:pass
   else:raise AssertionError('foreign ACK accepted')
   accepted=owner._accept_native_change(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,acceptance_response=canonical(dict(schema_version='sdc.release.apply-accept-response.v1',acceptance=ack)).encode('ascii'),deadline=time.monotonic()+30)
   assert accepted['acceptance']==ack
   from adapters.sdc import release_registration_contract as registration_contract
   import mcs_update,update_journal
   operation=dict(response['intent']);operation.update(schema_version=apply.VERSIONS['operation'],intent_hash=receipt['intent_hash'],approval_id=response['approval']['approval_id'],approval_hash=receipt['approval_hash'],approved_by=response['approval']['approved_by'],**{k:receipt[k] for k in ('change_consent_id','change_binding_hash','local_change_receipt_id','change_accept_id','preview_id','preview_payload_hash')},local_change_receipt_hash=receipt['receipt_hash'],payload_hash=apply.ZERO)
   operation['payload_hash']=apply.hash_apply_payload(operation)
   operation_envelope=signed_payload(operation,apply.PURPOSES[1])
   if failure=='run-operation':operation_envelope['signature']=operation_envelope['signature'][::-1]
   calls=[];admission_box={}
   def carrier(frame):
    admission=admission_box.get("value")
    request=json.loads(frame.body);calls.append(frame.path)
    assert not owner._EFFECT_CAPS
    assert release_state.status(control)['spent']==[]
    assert update_journal.load_state(home/'data/update_state.json').get('applying') is None
    headers=dict(frame.headers)
    message='\n'.join(('SDC-HMAC-v1','synthetic-release','POST',frame.path,identity['device_id'],identity['key_id'],str(packet['now']//1000),headers['X-SDC-Nonce'],hashlib.sha256(frame.body).hexdigest()))
    assert headers['X-SDC-Signature']==hmac.new(bytes.fromhex('a'*64),message.encode(),hashlib.sha256).hexdigest()
    if frame.path==registration_contract.PATH:
     expected=json.loads(frame.expected)
     reg={k:expected[k] for k in registration_contract.SCOPE if k!='audience'}
     reg.update(registration_id='00000000-0000-4000-8000-000000000054',owner_id='mcs.updater',owner_revision=1,capability_revision=apply.CAPABILITY_REVISION,verified_result_supported=False,device_evidence_sha256=frame.device_evidence_sha256,not_before=packet['now']-1000,expires_at=packet['now']+60000)
     proof=dict(schema_version=registration_contract.PROOF_VERSION,**expected,authority_revision=1,authority_sha256='d'*64,status='registered',owner=dict(product='hermes-mcs',owner_id='mcs.updater',owner_revision=1,owner_evidence_sha256=frame.owner_evidence_sha256),registration=reg,issued_at=packet['now'],not_before=packet['now'],expires_at=packet['now']+30000)
     value=dict(schema_version=registration_contract.RESPONSE_VERSION,request_id=request['request_id'],proof=signed_payload(proof,registration_contract.PURPOSE,registration=True))
    elif frame.path.endswith('/poll'):
     value=dict(schema_version='sdc.release.apply-poll-response.v1',context=dict(intent=response['intent'],approval=response['approval'],acceptance=ack,operation=operation_envelope,admission=None))
    else:
     assert frame.path.endswith(('/admit','/grant'))
     for lock_path in (control.root/'release-entry.lock',home/'data/update.lock',home/'data/run.lock'):
      descriptor=os.open(lock_path,os.O_RDONLY)
      try:
       import fcntl
       try:fcntl.flock(descriptor,fcntl.LOCK_SH|fcntl.LOCK_NB)
       except BlockingIOError:pass
       else:raise AssertionError('grant before G/U/R')
      finally:os.close(descriptor)
     if frame.path.endswith('/admit'):
      admission={**request,'schema_version':apply.VERSIONS['admission'],'admitted_at':packet['now']}
      admission_box['value']=admission
      value=dict(schema_version='sdc.release.apply-admit-response.v1',admission=admission)
     else:
      assert admission is not None
      assert request['state']['unresolved'] is None
      grant=dict(schema_version=apply.VERSIONS['grant'],**{k:operation[k] for k in (*apply.COMMON,*apply.BOUND)},grant_id='00000000-0000-4000-8000-000000000055',grant_request_id=request['grant_request_id'],**{k:admission[k] for k in ('receipt_id','admission_id','owner_generation')},preflight_sha256=contract.hash_release_preconditions(request['preconditions']),state_sha256=apply.hash_apply_state(request['state']),issued_at=packet['now'],not_before=packet['now'],expires_at=packet['now']+30000)
      value=dict(schema_version='sdc.release.apply-grant-response.v1',admission=admission,grant=signed_payload(grant,apply.PURPOSES[2]))
    if failure=='run-registration' and frame.path==registration_contract.PATH:value['proof']['signature']=value['proof']['signature'][::-1]
    if frame.path.endswith('/grant'):
     if failure=='run-grant':value['grant']['signature']=value['grant']['signature'][::-1]
     elif failure=='run-cancel':owner._cancel_native_change(control,runtime_home=home,operation_id=operation_id)
     elif failure=='run-epoch':support_state.disable(monitor,now_ms=packet['now'])
     elif failure=='run-settings-late':(home/'data/install_options.json').write_text('{"v":1,"install_args":[]}')
     elif failure=='run-settings-expiry':admission_box['grant_returned']=True
    return json.dumps(value,ensure_ascii=True).encode('ascii')
   original_child=owner._run_effect_post_merge
   def observed_child(*args,**kwargs):
    try:return original_child(*args,**kwargs)
    except BaseException:
     import traceback
     traceback.print_exc()
     print('synthetic native child tree',subprocess.check_output(['/usr/bin/git','-C',str(repo),'status','--porcelain=v1','--untracked-files=all'],text=True),flush=True)
     diagnostic=update_journal.load_state(home/'data/update_state.json')
     print('synthetic native child boundary',diagnostic.get('stages'),(diagnostic.get('applying') or {}).get('release_child'),flush=True)
     raise
   owner._run_effect_post_merge=observed_child
   import shlex
   hook_marker=home/'data/hook-must-not-run'
   hook=(repo/'.git/hooks/post-merge') if failure!='global-hook' else home/'global-hooks/post-merge'
   hook.parent.mkdir(exist_ok=True)
   hook.write_text('#!/bin/sh\nprintf invoked > '+shlex.quote(str(hook_marker))+'\n');hook.chmod(0o700)
   if failure=='global-hook':
    global_config=home/'synthetic-global.gitconfig'
    global_config.write_text('[core]\n hooksPath = '+str(hook.parent)+'\n')
    os.environ['GIT_CONFIG_GLOBAL']=str(global_config)
   if failure in ('run-git-attributes','run-git-grafts'):
    (repo/'.git/info'/('attributes' if failure.endswith('attributes') else 'grafts')).write_text('')
   previous=dict(update_journal.default_state(),stages=[dict(stage='local_checks',at=packet['now']/1000),dict(stage='done',at=packet['now']/1000)],applied=[dict(sha=supplied['baseline']['commit'],tag='synthetic-previous')])
   if failure=='run-unfinished-journal':previous['stages'].pop()
   update_journal.save_state(home/'data/update_state.json',previous)
   if failure=='run-settings-required':(home/'data/install_options.json').write_text('{"v":1,"install_args":[]}')
   if failure=='run-settings-unverified':(home/'data/install_options.json').write_text('{broken')
   if failure=='run-settings-expiry':
    check_settings=owner._require_settled_install_options;checks_after_grant=[]
    def expire_after_last_probe(*args,**kwargs):
     result=check_settings(*args,**kwargs)
     if admission_box.get('grant_returned'):
      checks_after_grant.append(True)
      if len(checks_after_grant)==2:time.time=lambda:(packet['now']+31000)/1000
     return result
    owner._require_settled_install_options=expire_after_last_probe
   assert not (local_problems:=mcs_update.precheck_local(mcs_update.mcs_util.load_config(),deadline=time.monotonic()+30)),local_problems
   if failure in ('run-fsync-expiry','run-fsync-rollback','run-start-uncertain'):
    write=control.write
    def after_start(name,value,**kwargs):
     write(name,value,**kwargs)
     if name==release_state.NAME and value.get('spent'):
      if failure=='run-start-uncertain':raise OSError('synthetic start publish uncertainty')
      time.time=lambda:(packet['now']+(-1000 if failure=='run-fsync-rollback' else 31000))/1000
    control.write=after_start
   try:
    result=owner._run_native_release(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,expected_registration_id=None,artifact_source=bundle,carrier=carrier,deadline=time.monotonic()+120)
   except owner.ReleaseApplyError as error:
    if failure is not None and failure.startswith('run-'):
     final=release_state.status(control)
     started=failure in ('run-fsync-expiry','run-fsync-rollback','run-start-uncertain')
     assert len(final['spent'])==int(started)
     assert (final['state']['unresolved'] is not None)==started
     assert final['uncertain']==(failure=='run-start-uncertain')
     assert len(final['outbox'])==int(failure=='run-fsync-expiry')
     if final['outbox']:
      report=json.loads(final['outbox'][0]['raw'])
      assert report['state']=='unknown' and report['result']['start_receipt_id']==final['spent'][0]['start_receipt_id']
     assert subprocess.check_output(['/usr/bin/git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()==supplied['baseline']['commit']
     assert subprocess.run(['/usr/bin/git','-C',str(repo),'cat-file','-e',packet['target_commit']],capture_output=True).returncode!=0
     assert update_journal.load_state(home/'data/update_state.json').get('applying') is None
     if failure.startswith('run-settings-'):
      assert 'release_ref' not in update_journal.load_state(home/'data/update_state.json')
      if failure=='run-settings-expiry':
       assert error.code=='RELEASE_APPLY_RUN_HOLD' and len(checks_after_grant)==2
      else:
       assert (home/'data/install_options.json').exists()
       assert error.code==('RELEASE_APPLY_SETTINGS_MIGRATION_UNVERIFIED' if failure=='run-settings-unverified' else 'RELEASE_APPLY_SETTINGS_MIGRATION_REQUIRED')
       if failure!='run-settings-late':assert calls==[]
     assert not owner._EFFECT_CAPS and not owner._EFFECT_PARENTS and not owner._LIVE_CONTINUATIONS
     assert control.read('release-successor.json') is None
     assert not hook_marker.exists()
     os._exit(0)
    import traceback
    print('synthetic carrier stages',calls,flush=True)
    cause=error.__context__
    while cause is not None:
     traceback.print_exception(type(cause),cause,cause.__traceback__,chain=False)
     cause=cause.__context__
    raise
   if result['pipeline_returncode']!=0:
    diagnostic=update_journal.load_state(home/'data/update_state.json')
    print('synthetic pipeline journal',diagnostic.get('attempts'),diagnostic.get('stages'),flush=True)
   assert result['started'] and result['readiness']=='unknown' and result['pipeline_returncode']==0,result
   final=release_state.status(control)
   assert len(final['spent'])==1 and final['state']['unresolved']['operation_id']==operation_id
   assert final['state']['installed']['release_id']==packet['expected_release_id'],final['state']
   successor=control.read('release-successor.json')
   assert successor['phase']=='pending' and successor['target_source_sha256']==record['target_source_sha256']
   assert successor['initial_source_sha256']==successor['parent_source_sha256']==supplied['owner_evidence_sha256']
   assert successor['state']['unresolved'] is not None
   assert final['owner_evidence_sha256']==supplied['owner_evidence_sha256']
   try:owner._run_native_release(control,monitor,runtime_home=home,repository=repo,operation_id=operation_id,expected_registration_id=None,artifact_source=bundle,carrier=lambda frame:(_ for _ in ()).throw(AssertionError('stored grant replayed')),deadline=time.monotonic()+120)
   except owner.ReleaseApplyError:pass
   else:raise AssertionError('unknown state resumed an effect')
   final_journal=update_journal.load_state(home/'data/update_state.json')
   assert final_journal['applying'] is None and final_journal['applied'][-1]['release_child']['phase']=='consumed'
   assert final_journal['applied'][-1]['release_ref']==final_journal['release_ref']
   report=json.loads(final['outbox'][-1]['raw'])
   assert report['state']=='unknown' and report['result']['verified_at'] is None and report['result']['start_receipt_id']==final['spent'][0]['start_receipt_id']
   assert final_journal['applied'][0]==previous['applied'][0]
   assert not hook_marker.exists()
   assert calls==['/v2/releases/apply/requests/poll',registration_contract.PATH,'/v2/releases/apply/operations/admit','/v2/releases/apply/operations/grant']
   assert not owner._EFFECT_CAPS and not owner._EFFECT_PARENTS and not owner._EFFECT_ARTIFACTS
   state_before=release_state.status(control)
   with monitor.lock():
    cancelled=owner._cancel_native_change(control,runtime_home=home,operation_id=operation_id)
    assert cancelled['revoked']
    assert owner._cancel_native_change(control,runtime_home=home,operation_id=operation_id)==cancelled
   assert release_state.status(control)==state_before
   assert not owner._EFFECT_CAPS
   support_state.disable(monitor,now_ms=packet['now'])
   try:owner._capture_release_case(control,monitor,runtime_home=home,repository=repo,deadline=time.monotonic()+30)
   except owner.ReleaseApplyError:pass
   else:raise AssertionError('disabled parent case accepted')
   changed=dict(record);changed['target_source_sha256']='0'*64
   control.write('release-plan.json',changed)
   try:owner._read_frozen_plan(control)
   except owner.ReleaseApplyError:pass
   else:raise AssertionError('corrupt local report accepted')
 except BaseException:
  import traceback;traceback.print_exc();os._exit(4)
 os._exit(0)
reaped=False;received=b'';answered=0;reason_sent=0
try:
 until=time.monotonic()+180
 while time.monotonic()<until:
  if select.select([master],[],[],.05)[0]:
   try:received+=os.read(master,65536)
   except OSError as error:
    if error.errno!=errno.EIO:raise
  if received.count('番号: '.encode())>answered:
   answered+=1;os.write(master,b'1\n')
  if received.count('半角の理由'.encode())>reason_sent:
   reason_sent+=1;os.write(master,b'synthetic independent plan\n')
  child,status=os.waitpid(pid,os.WNOHANG)
  if child:
   reaped=True
   assert os.waitstatus_to_exitcode(status)==0,received.decode(errors='replace')
   break
 assert reaped and answered and reason_sent
finally:
 if not reaped:
  os.killpg(pid,signal.SIGKILL);os.waitpid(pid,0)
 os.close(master)
print('synthetic native plan checked')
'''
    environment = dict(os.environ, HOME=str(tmp_path), MCS_ROOT=str(home), MCS_UPDATE_REPO=str(repo))
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(repo), str(home), str(registered.root), str(bundle)],
        input=json.dumps(packet), capture_output=True, text=True, env=environment, timeout=190)
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith('synthetic native plan checked\n')


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
