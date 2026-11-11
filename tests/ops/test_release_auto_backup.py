"""v3 backup_evidence deps面: local_backup_evidence_id発行の検証。"""
import json
import re

import pytest

import backup_evidence
from release_auto_backup import BackupEvidence, BackupEvidenceError


class _Policy:
    policy_id = 'test-policy'


def make_evidence(tmp_path, monkeypatch, status=None, status_exc=None, **kw):
    def fake_status(state_dir, policy):
        if status_exc is not None:
            raise status_exc
        return status if status is not None else {
            'within_rpo': True, 'last_attempt_failed': False,
            'policy_id': 'test-policy', 'rpo_seconds': 10.0,
            'bundle_age_seconds': 20.0}

    monkeypatch.setattr(backup_evidence, 'status', fake_status)
    return BackupEvidence(
        state_dir=str(tmp_path / 'state'), policy=_Policy(),
        evidence_dir=tmp_path / 'evidence', now_ms=lambda: 1700000000000,
        **kw)


def test_backup_evidence_returns_uuid_and_persists_mapping(tmp_path, monkeypatch):
    ev = make_evidence(tmp_path, monkeypatch)
    evidence_id = ev()
    assert re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
        evidence_id)
    record = json.loads(
        (tmp_path / 'evidence' / (evidence_id + '.json')).read_text())
    assert record['evidence_id'] == evidence_id
    assert record['within_rpo'] is True
    assert record['checked_at_ms'] == 1700000000000
    # wireへ出せないprivate値(実hash/path)を記録へ含めない
    assert 'backup_sha256' not in record and 'path' not in record


def test_backup_evidence_rejects_stale_or_failed(tmp_path, monkeypatch):
    cases = [
        {'within_rpo': False, 'last_attempt_failed': False},
        {'within_rpo': True, 'last_attempt_failed': True},
        {'within_rpo': None, 'last_attempt_failed': False},
        {},
    ]
    for i, status in enumerate(cases):
        ev = make_evidence(tmp_path / f'case{i}', monkeypatch, status=status)
        with pytest.raises(BackupEvidenceError):
            ev()


def test_backup_evidence_rejects_status_failure(tmp_path, monkeypatch):
    ev = make_evidence(tmp_path, monkeypatch,
                       status_exc=RuntimeError('corrupt'))
    with pytest.raises(BackupEvidenceError) as e:
        ev()
    assert e.value.code == 'BACKUP_STATUS'


def test_backup_evidence_unique_ids_and_capacity(tmp_path, monkeypatch):
    ev = make_evidence(tmp_path, monkeypatch)
    first, second = ev(), ev()
    assert first != second

    full = make_evidence(tmp_path / 'full', monkeypatch, max_entries=1)
    full()
    with pytest.raises(BackupEvidenceError) as e:
        full()
    assert e.value.code == 'BACKUP_EVIDENCE_FULL'
