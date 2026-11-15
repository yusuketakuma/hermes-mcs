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
import copy
import os
import subprocess
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


