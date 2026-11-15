"""Synthetic release apply owner planning contracts; shared helpers preserve isolated runtime state."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_preflight
import update_recovery
import update_repository
import update_runtime_migration
import update_services
from pathlib import Path
import copy
import os
import subprocess
import sys
import time
from dataclasses import replace
import pytest
import update_journal
import release_apply_owner
from release_apply_owner_testkit import current_release as current_release, installed_current as installed_current, native_product as native_product, native_signing_keys as native_signing_keys, registered_effect as registered_effect
from test_release_dependency_lock import product as product, runtime as runtime
from test_sdc_release_state import native_child as native_child, store as store
from test_sdc_provision import world as world
from test_sdc_provision_profiles import registered as registered


@pytest.mark.parametrize('failure', [None, 'tampered', 'expired'])
def test_started_artifact_imports_fixed_bundle_without_selecting_its_advertised_ref(registered_effect, tmp_path, monkeypatch, failure):
    # Explicit seeded component fixture; this is not live-factory or signature acceptance.
    import hashlib
    import json
    import mcs_update
    import release_owner_source
    from adapters.sdc import release_contract
    cap, entry, parameters = registered_effect
    release_apply_owner._claim_effect_entry(cap, **parameters)
    producer = tmp_path / 'producer'
    producer.mkdir()
    def git(root, *args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(root), *args], text=True).strip()
    git(producer, 'init', '-q')
    (producer / 'synthetic.py').write_text('raise RuntimeError("CANDIDATE_MUST_NOT_EXECUTE")\n')
    git(producer, 'add', '.')
    git(producer, '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid', 'commit', '-qm', 'synthetic target')
    target = git(producer, 'rev-parse', 'HEAD')
    git(producer, 'branch', 'untrusted-advertised-name')
    bundle = tmp_path / 'fixed.bundle'
    git(producer, 'bundle', 'create', str(bundle), '--all')
    git(entry.repository, 'init', '-q')
    raw = bundle.read_bytes()
    artifact_sha = hashlib.sha256(raw).hexdigest()
    attestation = {'release_id': entry.reference['release_id'], 'core': {
        'commit': target, 'code_seq': entry.reference['target_code_seq'],
        'lock_sha256': entry.reference['lock_sha256'], 'schema': {'target': entry.reference['target_schema']},
        'artifact': {'size': len(raw), 'sha256': artifact_sha}}}
    manifest = {'schema_version': release_owner_source.MANIFEST_VERSION,
        'owner_id': release_owner_source.OWNER_ID, 'owner_revision': 1, 'provider_profile': [1, 0, 0],
        'source_policy': release_owner_source.SOURCE_POLICY,
        'files': [dict(path=name, mode='100644', sha256='1' * 64)
            for name in sorted((*release_owner_source.REQUIRED, *release_owner_source.SOURCE_FILES))]}
    # Adjust only the explicit unit fixture, after claim; no production issuer uses these writes.
    old = entry
    spent = copy.deepcopy(entry.spent)
    spent['operation']['manifest_digest'] = release_contract.hash_release_manifest(attestation)
    entry = replace(entry, reference={**entry.reference, 'target_commit': target, 'artifact_sha256': artifact_sha},
        spent=spent, prequiesce_deadline=time.monotonic() + (-1 if failure == 'expired' else 30))
    release_apply_owner._EFFECT_RUNNING.discard(old)
    release_apply_owner._EFFECT_RUNNING.add(entry)
    release_apply_owner._EFFECT_PARENTS[entry.owner] = entry
    monkeypatch.setattr(release_apply_owner, '_EFFECT_IMPORTED', set())
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
    if failure == 'tampered':
        bundle.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    descriptor = os.open(bundle, os.O_RDONLY)
    try:
        frame = release_apply_owner._StartedArtifact(descriptor,
            release_apply_owner._effect_fd_identity(descriptor), json.dumps(attestation).encode(),
            release_owner_source.canonical(manifest), release_owner_source.digest(manifest))
        monkeypatch.setattr(release_apply_owner, '_EFFECT_ARTIFACTS', {entry.owner: frame})
        if failure is None:
            release_apply_owner._import_started_artifact(entry)
            assert git(entry.repository, 'cat-file', '-t', target) == 'commit'
            assert git(entry.repository, 'for-each-ref') == ''
            with pytest.raises(release_apply_owner.ReleaseApplyError, match='ARTIFACT_HOLD'):
                release_apply_owner._import_started_artifact(entry)
        else:
            with pytest.raises((release_apply_owner.ReleaseApplyError, mcs_update.UpdateError)):
                release_apply_owner._import_started_artifact(entry)
            result = subprocess.run(['/usr/bin/git', '-C', entry.repository, 'cat-file', '-e', target], capture_output=True)
            assert result.returncode != 0
        assert release_apply_owner._effect_fd_identity(descriptor) == frame.descriptor_identity
        assert all(release_apply_owner._effect_fd_identity(fd) == identity for fd, identity in zip(entry.locks, entry.lock_identities))
        assert update_journal.load_state(parameters['journal_path']) == old.journal
    finally:
        release_apply_owner._finish_effect_entry(entry)
        os.close(descriptor)


@pytest.mark.parametrize('decision', ['confirm', 'cancel', 'disabled', 'revision', 'corrupt', 'write_error'])
def test_planning_receipt_rechecks_real_currentness_after_the_prompt(current_release, monkeypatch, decision):
    # Prompt outcome is injected only for store/CAS component tests, never factory acceptance.
    import hashlib
    from adapters.sdc import release_state
    control, monitor, home, now, _ = current_release
    monkeypatch.setattr(release_apply_owner.time, 'time', lambda: now / 1000)
    owner_before = copy.deepcopy(control.read(release_state.NAME))
    def prompt(*_a, **_k):
        assert control._release_authority_fd is None and monitor._tick_fd is None
        assert control._release_entry_fd is None
        if decision == 'cancel':
            return None
        if decision == 'disabled':
            with monitor.lock():
                value = monitor.read('state.json')
                value['enabled'] = False
                monitor.write('state.json', value)
        elif decision == 'revision':
            with control.release_owner_lock():
                value = control.read(release_state.NAME)
                value['revision'] += 1
                control.write(release_state.NAME, value)
        elif decision == 'corrupt':
            control.write('release-planning.json', {'schema_version': 'corrupt'})
        elif decision == 'write_error':
            original = control.write
            def fail(name, value, **kwargs):
                if name == 'release-planning.json':
                    raise OSError('synthetic write failure')
                return original(name, value, **kwargs)
            monkeypatch.setattr(control, 'write', fail)
        return hashlib.sha256(b'synthetic reason').hexdigest()
    monkeypatch.setattr(release_apply_owner, '_tty_planning_confirmation', prompt)
    def call():
        return release_apply_owner._issue_planning_consent(control, monitor, runtime_home=home,
            repository=home, case_id='00000000-0000-4000-8000-000000000009', deadline=time.monotonic() + 10)
    if decision in ('confirm', 'cancel'):
        result = call()
        if decision == 'cancel':
            assert result is None and control.read('release-planning.json') is None
        else:
            assert release_apply_owner._validate_planning_consent(result) == result
            assert control.read('release-planning.json') == result
            assert 'synthetic reason' not in str(result)
            with pytest.raises(release_apply_owner.ReleaseApplyError, match='CONSENT_HOLD'):
                call()
            assert control.read('release-planning.json') == result
        assert control.read(release_state.NAME) == owner_before
    else:
        with pytest.raises(OSError if decision == 'write_error' else release_apply_owner.ReleaseApplyError):
            call()
        assert control.read('release-planning.json') == ({'schema_version': 'corrupt'} if decision == 'corrupt' else None)
        if decision == 'write_error':
            assert control._release_owner_uncertain


@pytest.mark.parametrize('decision', ['confirm', 'default', 'other', 'blank_reason', 'oversized_reason', 'timeout'])
def test_planning_confirmation_reads_a_real_controlling_terminal_only(decision):
    root = Path(__file__).resolve().parents[2]
    program = r'''import errno, hashlib, os, pathlib, pty, select, signal, sys, time
sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'mcs'))
sys.path.insert(0,str(pathlib.Path(sys.argv[1])))
import _mcs_path
import release_apply_owner as owner
mode=sys.argv[2]
pid,master=pty.fork()
if pid==0:
 try:
  result=owner._tty_planning_confirmation('synthetic',deadline=time.monotonic()+2)
  expected=hashlib.sha256(b'synthetic reason').hexdigest() if mode=='confirm' else None
  os._exit(0 if result==expected and mode not in ('blank_reason','oversized_reason','timeout') else 3)
 except owner.ReleaseApplyError:
  os._exit(0 if mode in ('blank_reason','oversized_reason','timeout') else 4)
reaped=False
try:
 received=b''; answered=False; reason_sent=False; until=time.monotonic()+6
 while time.monotonic()<until:
  if select.select([master],[],[],.05)[0]:
   try: data=os.read(master,4096)
   except OSError as error:
    if error.errno!=errno.EIO: raise
    data=b''
   received+=data
  if not answered and '番号: '.encode() in received:
   answered=True
   if mode!='timeout': os.write(master,b'\n' if mode=='default' else b'2\n' if mode=='other' else b'1\n')
  if not reason_sent and '理由'.encode() in received:
   reason_sent=True
   os.write(master,b'\n' if mode=='blank_reason' else b'x'*129+b'\n' if mode=='oversized_reason' else b'synthetic reason\n')
  child,status=os.waitpid(pid,os.WNOHANG)
  if child:
   reaped=True
   assert os.waitstatus_to_exitcode(status)==0,(mode,status)
   break
 assert reaped and answered
finally:
 if not reaped:
  os.killpg(pid,signal.SIGKILL)
  os.waitpid(pid,0)
 os.close(master)
print('synthetic tty checked')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(root), decision],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'synthetic tty checked\n'


def test_stdin_cannot_approve_without_a_controlling_terminal():
    root = Path(__file__).resolve().parents[2]
    program = '''import pathlib,sys,time
sys.path.insert(0,str(pathlib.Path(sys.argv[1])/'mcs'))
sys.path.insert(0,str(pathlib.Path(sys.argv[1])))
import _mcs_path
import release_apply_owner as owner
try: owner._tty_planning_confirmation('synthetic',deadline=time.monotonic()+1)
except owner.ReleaseApplyError: print('held')
else: raise AssertionError('stdin approved')
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(root)],
        input='1\nsynthetic reason\n', capture_output=True, text=True, start_new_session=True, timeout=5)
    assert result.returncode == 0 and result.stdout == 'held\n'
