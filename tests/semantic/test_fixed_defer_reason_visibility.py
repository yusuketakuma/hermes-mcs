"""Known semantic refusals survive the fixed backlog counters without free-form codes."""
import json

import pytest

import cloud_backlog
import semantic_drain
from test_semantic_fairness import _due, _job, _world


@pytest.mark.parametrize('field,reason,expected', [
    ('not_sent', 'cloud_capacity', 'cloud_capacity'),
    ('not_sent', 'cloud_profile_unavailable', 'cloud_profile_unavailable'),
    ('not_sent', 'cloud_source_unverified', 'cloud_source_unverified'),
    ('not_sent', 'cloud_target_unverified', 'cloud_target_unverified'),
    ('code', 'audit_pending', 'audit_pending'),
    ('code', 'loops_incomplete', 'loops_incomplete'),
    ('code', 'fact_stage_synthetic-private-detail', 'unclassified'),
    ('code', 'summary_synthetic-private-detail', 'unclassified'),
    ('not_sent', 'synthetic-private-detail', 'unclassified'),
] + [('code', reason, reason) for reason in sorted(cloud_backlog._SEMANTIC_STOP_REASONS)])
def test_semantic_drain_keeps_only_exact_fixed_reason(field, reason, expected, tmp_path, monkeypatch):
    ledger = _world(tmp_path, [1])
    try:
        _job(ledger, 1)
        reached = []
        def deferred(*args, diagnostics, **kwargs):
            reached.append(True)
            diagnostics[field] = reason
            return 'deferred'
        monkeypatch.setattr(semantic_drain, '_process_job', deferred)
        result = _due(ledger, max_jobs=1)
        assert reached == [True]
        assert result['done'] == result['failed'] == 0 and result['deferred'] == 1
        assert result['defer_reasons'] == {expected: 1}
        assert ('synthetic-private-detail' not in json.dumps(result['defer_reasons']))
        assert (reason in cloud_backlog._DEFER_REASONS) is (expected != 'unclassified')
    finally:
        ledger.close()
