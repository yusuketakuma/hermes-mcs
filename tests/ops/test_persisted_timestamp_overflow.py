"""Huge persisted numbers are invalid timestamps, never process-level failures."""
from copy import deepcopy
from pathlib import Path

import pytest

import c1_records
import mcs_restore


def test_paired_record_timestamp_is_unknown_when_integer_cannot_be_finite():
    assert c1_records._timestamp(10 ** 1000) is None


@pytest.mark.parametrize('field', ['verified_at', 'at'])
def test_restore_timestamp_overflow_is_typed_refusal(monkeypatch, field):
    source = 'a' * 64
    report = {'v': 1, 'sha256': source, 'plain_sha256': 'b' * 64,
              'verified_at': 1, 'inventory': {}, 'restore_pending': True,
              'attachment_payloads_included': False}
    marker = {'v': 1, 'phase': 'awaiting_consent', 'backup_path': None,
              'by': 'mcs_backup_restore', 'at': 1, 'report_id': 'synthetic'}
    (report if field == 'verified_at' else marker)[field] = 10 ** 1000

    def read(fd, name):
        return deepcopy(report if name == 'restore.json' else marker), 'c' * 64, {}

    monkeypatch.setattr(mcs_restore, '_read', read)
    with pytest.raises(mcs_restore.RestoreConsentError, match='restore_metadata_mismatch'):
        mcs_restore._binding(Path('/synthetic'), 0, source)
