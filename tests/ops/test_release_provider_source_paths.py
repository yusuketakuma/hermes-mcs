"""The split provider gate pins both facade and implementation before accepting bytes."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

import release_apply_checks as checks
import release_apply_owner as facade
import release_owner_source as source
from adapters.sdc import release_state


@pytest.mark.parametrize('wrong_path', [None, 'checks', 'facade', 'source', 'repository'])
def test_initial_source_pins_every_loaded_owner_before_verifying_bytes(monkeypatch, wrong_path):
    repository = Path(__file__).resolve().parents[2]
    saved = {'state': {'unresolved': None}, 'repo_fingerprint': 'synthetic-repository',
             'owner_evidence_sha256': 'a' * 64}
    monkeypatch.setattr(release_state, '_read', lambda _: saved)
    monkeypatch.setattr(checks, '_repository_fingerprint', lambda _: 'synthetic-repository')
    monkeypatch.setattr(checks, '_check_git_metadata', lambda *args, **kwargs: None)
    reads, verified = [], []
    def read(root, path, deadline):
        reads.append((root, path))
        return b'{}', '100644'
    def verify(root, raw, *, deadline, current):
        verified.append((root, raw, current))
        return {'sha256': 'a' * 64}
    monkeypatch.setattr(source, '_read', read)
    monkeypatch.setattr(source, 'verify_checkout', verify)
    modules = {'checks': checks, 'facade': facade, 'source': source}
    if wrong_path == 'repository':
        repository = repository / 'different-repository'
    elif wrong_path is not None:
        monkeypatch.setattr(modules[wrong_path], '__file__', str(repository / 'different-owner.py'))
    control = SimpleNamespace(release_owner_lock=lambda: nullcontext())
    if wrong_path is not None:
        with pytest.raises(facade.ReleaseApplyError, match='PROVIDER_HOLD'):
            checks._initial_provider_source(control, repository, deadline=time.monotonic() + 5)
        assert reads == verified == []
    else:
        assert checks._initial_provider_source(control, repository,
                    deadline=time.monotonic() + 5) == {'sha256': 'a' * 64}
        assert reads == [(repository, source.MANIFEST_PATH)]
        assert verified == [(repository, b'{}', True)]
