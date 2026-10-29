"""Synthetic failure evidence and retry-diagnosis boundaries."""
import json

import pytest

from ledger import Ledger
from mcs_util import payload_hash
from inference_version import CURRENT_VERSION, VERSION_KEY
import job_diagnostics as diag


@pytest.fixture
def data(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.ensure_patient(1)
    ledger.db.execute("INSERT INTO messages(message_id,project_id,body_text,body_state) VALUES(42,1,?,?)",
                      ("synthetic-source-never-return", "full"))
    payload = {VERSION_KEY: CURRENT_VERSION, "generation": "synthetic-generation", "targets": [42]}
    job = ledger.job_add("semantic", 1, 42, payload=payload)
    ledger.db.execute("UPDATE fetch_jobs SET state='failed',attempts=3 WHERE job_id=?", (job,))
    ledger.db.commit()
    cfg = {"semantic": {"mode": "assist", "project_ids": [1]}}
    yield ledger, job, payload, cfg
    ledger.close()


def _metric(ledger, job, payload, **changes):
    metric = {"job_id": job, "project_id": 1, "kind": "semantic", "generation": payload["generation"],
              "payload_digest": payload_hash(payload), "attempts": 2, "status": "retry",
              "retry_reason": "budget_short", "llm_error": "synthetic-secret-never-return"}
    metric.update(changes)
    ledger.artifact_add("semantic_drain_run", json.dumps({"job_metrics": [metric]}))
    ledger.db.commit()


def _read(ledger, job, cfg):
    ledger.db.execute("BEGIN")
    try:
        return diag.diagnose_job(ledger.db, cfg, job, now=1700000000)
    finally:
        ledger.db.rollback()


def test_old_metrics_do_not_fabricate_reason_and_never_expose_data(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload, payload_digest=None, attempts=None)
    before = ledger.db.total_changes
    report = _read(ledger, job, cfg)
    assert report["reason_code"] == "unknown"
    assert "reason_unverified" in report["warnings"]
    assert "reason_unverified" not in report["blockers"]
    assert "synthetic" not in json.dumps(report)
    assert ledger.db.total_changes == before
    assert report["expected_payload_hash"] == payload_hash(payload)


def test_current_metric_is_bound_and_hash_ignores_unrelated_usage(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    first = _read(ledger, job, cfg)
    assert first["reason_code"] == "budget_short"
    assert first["reason_basis"] == "bound_job_metric"
    assert first["remediation_code"] == "budget"
    assert not first["blockers"]
    ledger.artifact_add("semantic_usage", "{}")
    ledger.db.commit()
    assert _read(ledger, job, cfg)["diagnosis_hash"] == first["diagnosis_hash"]
    ledger.db.execute("UPDATE fetch_jobs SET attempts=4 WHERE job_id=?", (job,))
    ledger.db.commit()
    later = _read(ledger, job, cfg)
    assert later["reason_code"] == "unknown"
    assert later["diagnosis_hash"] != first["diagnosis_hash"]


@pytest.mark.parametrize("changed", [
    {"project_id": 2}, {"generation": "other"}, {"payload_digest": "0" * 64},
    {"attempts": 1}, {"status": "done"}, {"retry_reason": {"secret": "synthetic"}},
])
def test_unbound_or_unknown_evidence_stays_unknown(data, changed):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload, **changed)
    assert _read(ledger, job, cfg)["reason_code"] == "unknown"


def test_scope_permission_source_and_invalid_payload_block_retry(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    ledger.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
    ledger.db.execute("UPDATE messages SET body_state='unavailable' WHERE message_id=42")
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", ('{"bad":', job))
    ledger.db.commit()
    report = _read(ledger, job, {"semantic": {"mode": "assist", "project_ids": [2]}})
    assert {"permission", "source_unavailable", "out_of_scope", "invalid_payload"} <= set(report["blockers"])
    assert report["expected_payload_hash"] is None


def test_list_is_bounded_and_requires_caller_snapshot(data):
    ledger, job, payload, cfg = data
    with pytest.raises(Exception, match="job_diagnostics_snapshot_required"):
        diag.list_failed(ledger.db, cfg)
    ledger.db.execute("BEGIN")
    try:
        result = diag.list_failed(ledger.db, cfg, limit=1)
        assert len(result["jobs"]) == 1 and not result["truncated"]
        with pytest.raises(ValueError):
            diag.list_failed(ledger.db, cfg, limit=0)
    finally:
        ledger.db.rollback()


def test_source_change_invalidates_diagnosis(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    first = _read(ledger, job, cfg)
    ledger.db.execute("UPDATE messages SET body_text='synthetic-updated' WHERE message_id=42")
    ledger.db.commit()
    assert _read(ledger, job, cfg)["diagnosis_hash"] != first["diagnosis_hash"]


def test_unverified_cloud_send_is_blocked(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload, retry_reason=None, llm_error="cloud_failed")
    result = _read(ledger, job, cfg)
    assert result["reason_code"] == "llm_cloud_failed"
    assert "send_unverified" in result["blockers"]


def test_missing_reply_target_is_a_blocker(data):
    ledger, job, payload, cfg = data
    payload["targets"] = [42, 43]
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (json.dumps(payload), job))
    ledger.db.commit()
    _metric(ledger, job, payload)
    assert "source_unavailable" in _read(ledger, job, cfg)["blockers"]


@pytest.mark.parametrize("targets", [None, 12, "synthetic", [[42]], [{"bad": 42}]])
def test_bad_targets_fail_closed_without_exceptions(data, targets):
    ledger, job, payload, cfg = data
    payload["targets"] = targets
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (json.dumps(payload), job))
    ledger.db.commit()
    result = _read(ledger, job, cfg)
    assert "invalid_payload" in result["blockers"]


def test_old_generation_is_held_even_when_otherwise_eligible(data):
    ledger, job, payload, cfg = data
    payload.pop(VERSION_KEY)
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE job_id=?", (json.dumps(payload), job))
    ledger.db.commit()
    result = _read(ledger, job, cfg)
    assert result["reason_code"] == "unknown"
    assert "needs_current_generation" in result["blockers"]


def test_error_metric_is_bound_but_exception_name_is_not_exposed(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload, status="error", retry_reason="exception_synthetic_secret")
    result = _read(ledger, job, cfg)
    assert result["reason_code"] == "internal_error"
    assert result["reason_basis"] == "bound_job_metric"
    assert result["remediation_code"] == "investigate"
    assert "synthetic_secret" not in json.dumps(result)


def test_list_reads_bounded_metric_artifacts_once(data):
    ledger, job, payload, cfg = data
    _metric(ledger, job, payload)
    ledger.artifact_add("semantic_drain_run", "x" * (1048576 + 1))
    ledger.db.commit()
    queries = []
    ledger.db.set_trace_callback(queries.append)
    ledger.db.execute("BEGIN")
    try:
        result = diag.list_failed(ledger.db, cfg)
    finally:
        ledger.db.rollback()
        ledger.db.set_trace_callback(None)
    matching = [query for query in queries if "CASE WHEN typeof(content)='text'" in query]
    assert len(matching) == 1
    assert result["jobs"][0]["reason_code"] == "budget_short"


def test_old_unbound_reason_does_not_hide_unsettled_cloud_send(data):
    ledger, job, payload, cfg = data
    from datetime import datetime
    import cloud_policy
    from unittest.mock import patch
    now = datetime(2026, 10, 29, 12, tzinfo=cloud_policy.JST).timestamp()
    cloud_cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    with patch.object(cloud_policy.time, "time", return_value=now):
        reserve = cloud_policy.usage_reserver(ledger, cloud_cfg, route="semantic_backlog", project_id=1,
            message_id=42, unit_key=f"semantic:{job}:{payload['generation']}")(
                {"model": cloud_policy.MODEL, "max_tokens": 100}, 2)
    assert "send_unverified" in _read(ledger, job, cfg)["blockers"]
    cloud_policy.record_result(ledger, reserve, {"model": cloud_policy.MODEL,
        "request_fp": reserve["request_fp"], "usage": None, "http_class": "not_sent", "error_kind": "budget_exceeded"})
    result = _read(ledger, job, cfg)
    assert result["reason_code"] == "unknown"
    assert "send_unverified" not in result["blockers"]
